"""Obsidian Second Brain memory provider for Hermes Agent.

Backs Hermes's pluggable memory with an Obsidian vault of AI-first markdown
notes, replacing the default vector store with a human-readable, self-rewriting
wiki. Implements the `MemoryProvider` ABC from `agent/memory_provider.py`.

Install: copy this folder into a Hermes checkout as `plugins/memory/obsidian/`,
set `OBSIDIAN_VAULT_PATH`, then `hermes config set memory.provider obsidian`.

Status: v0 scaffold. The core path (system-prompt injection, search-based
prefetch, turn logging, explicit save/search/read tools) is implemented in pure
stdlib. It has NOT yet been run inside a live Hermes runtime against a model -
see README.md for the testing checklist. Known risk: `prefetch()` runs before
every turn and must stay fast; the search here is a bounded linear scan, fine
for small/medium vaults but needs an index for large ones.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.memory_provider import MemoryProvider

logger = logging.getLogger(__name__)

_VAULT_ENV = "OBSIDIAN_VAULT_PATH"

# Subfolders Hermes writes into (kept separate from hand-authored vault notes).
_SESSIONS_DIR = "Hermes/sessions"
_NOTES_DIR = "Hermes/notes"

# Directories never scanned during search (config, vcs, immutable sources, exports).
_SKIP_DIRS = {".obsidian", ".git", ".trash", "_export", "templates"}

# Bounds to keep prefetch fast and notes readable.
_MAX_FILES_SCANNED = 2000
_MAX_FILE_BYTES = 200_000
_SNIPPET_CHARS = 320
_TURN_TRUNC = 2000


# ---------------------------------------------------------------------------
# Tool schemas (OpenAI function-calling format)
# ---------------------------------------------------------------------------

_SAVE_SCHEMA = {
    "name": "obsidian_save_note",
    "description": (
        "Save a durable note to the Obsidian vault (long-term memory). Use for "
        "facts, decisions, people, or anything worth recalling in future sessions."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Short note title."},
            "content": {"type": "string", "description": "Note body in markdown."},
            "type": {
                "type": "string",
                "description": "Note type (e.g. concept, decision, person, fact). Default: note.",
            },
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Lowercase tags.",
            },
        },
        "required": ["title", "content"],
    },
}

_SEARCH_SCHEMA = {
    "name": "obsidian_search",
    "description": "Search the Obsidian vault for relevant notes. Returns ranked matches with snippets.",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What to search for."},
            "limit": {"type": "integer", "description": "Max results (default 6, max 20)."},
        },
        "required": ["query"],
    },
}

_READ_SCHEMA = {
    "name": "obsidian_read_note",
    "description": "Read the full content of a vault note by its relative path (as returned by obsidian_search).",
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Vault-relative path to the note."},
        },
        "required": ["path"],
    },
}


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class ObsidianMemoryProvider(MemoryProvider):
    """Use an Obsidian vault as Hermes's memory backend."""

    def __init__(self) -> None:
        self._vault: Optional[Path] = None
        self._session_id: str = ""

    @property
    def name(self) -> str:
        return "obsidian"

    # -- Core lifecycle ------------------------------------------------------

    def is_available(self) -> bool:
        """Ready if the vault path is configured and the directory exists. No network."""
        raw = os.environ.get(_VAULT_ENV, "").strip()
        if not raw:
            return False
        try:
            return Path(raw).expanduser().is_dir()
        except OSError:
            return False

    def initialize(self, session_id: str, **kwargs) -> None:
        raw = os.environ.get(_VAULT_ENV, "").strip()
        # resolve() so the path-traversal guard in _tool_read compares like
        # with like (e.g. macOS /tmp -> /private/tmp symlink resolution).
        self._vault = Path(raw).expanduser().resolve() if raw else None
        self._session_id = session_id or "default"
        if self._vault is not None:
            for sub in (_SESSIONS_DIR, _NOTES_DIR):
                try:
                    (self._vault / sub).mkdir(parents=True, exist_ok=True)
                except OSError as exc:
                    logger.warning("obsidian memory: could not create %s: %s", sub, exc)

    def system_prompt_block(self) -> str:
        """Inject the vault's standing memory (MEMORY.md / world note) at session start."""
        if self._vault is None:
            return ""
        for candidate in ("MEMORY.md", "CRITICAL_FACTS.md", "SOUL.md"):
            text = self._read_safe(self._vault / candidate)
            if text:
                trimmed = text.strip()[:4000]
                return (
                    "## Long-term memory (Obsidian vault)\n"
                    "The following is your standing memory, drawn from the user's vault. "
                    "Use obsidian_search to recall more, obsidian_save_note to remember.\n\n"
                    f"{trimmed}"
                )
        return (
            "## Long-term memory (Obsidian vault)\n"
            "Your memory is an Obsidian vault. Use obsidian_search to recall and "
            "obsidian_save_note to persist anything worth keeping."
        )

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Recall relevant notes for the upcoming turn (bounded linear search)."""
        if self._vault is None or not query.strip():
            return ""
        hits = self._search(query, limit=4)
        if not hits:
            return ""
        lines = ["## Recalled from vault"]
        for h in hits:
            lines.append(f"- [[{h['title']}]] ({h['path']}): {h['snippet']}")
        return "\n".join(lines)

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """Append the completed turn to this session's vault note."""
        if self._vault is None:
            return
        sid = session_id or self._session_id
        note = self._vault / _SESSIONS_DIR / f"{self._slug(sid)}.md"
        try:
            if not note.exists():
                note.write_text(self._session_header(sid), encoding="utf-8")
            block = (
                f"\n### {self._now()}\n"
                f"**User:** {self._trunc(user_content)}\n\n"
                f"**Assistant:** {self._trunc(assistant_content)}\n"
            )
            with note.open("a", encoding="utf-8") as fh:
                fh.write(block)
        except OSError as exc:
            logger.warning("obsidian memory: turn write failed: %s", exc)

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [_SAVE_SCHEMA, _SEARCH_SCHEMA, _READ_SCHEMA]

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        try:
            if tool_name == "obsidian_save_note":
                return self._tool_save(args)
            if tool_name == "obsidian_search":
                return self._tool_search(args)
            if tool_name == "obsidian_read_note":
                return self._tool_read(args)
        except Exception as exc:  # never let a tool crash the agent loop
            logger.exception("obsidian memory: tool %s failed", tool_name)
            return json.dumps({"error": f"{type(exc).__name__}: {exc}"})
        return json.dumps({"error": f"unknown tool {tool_name}"})

    def shutdown(self) -> None:
        """Stateless writer; nothing to flush."""

    # -- Setup wizard --------------------------------------------------------

    def get_config_schema(self) -> List[Dict[str, Any]]:
        return [
            {
                "key": "vault_path",
                "description": "Absolute path to your Obsidian vault.",
                "secret": False,
                "required": True,
                "env_var": _VAULT_ENV,
            }
        ]

    def save_config(self, values: Dict[str, Any], hermes_home: str) -> None:
        """Env-var-only provider: the vault path lives in OBSIDIAN_VAULT_PATH."""

    # -- Optional hooks ------------------------------------------------------

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        """Mark the session note closed so future-Claude knows it is complete."""
        if self._vault is None:
            return
        note = self._vault / _SESSIONS_DIR / f"{self._slug(self._session_id)}.md"
        try:
            if note.exists():
                with note.open("a", encoding="utf-8") as fh:
                    fh.write(f"\n---\n_Session ended {self._now()} ({len(messages)} messages)._\n")
        except OSError as exc:
            logger.warning("obsidian memory: session-end write failed: %s", exc)

    # -- Tool implementations ------------------------------------------------

    def _tool_save(self, args: Dict[str, Any]) -> str:
        title = str(args.get("title", "")).strip()
        content = str(args.get("content", "")).strip()
        if not title or not content:
            return json.dumps({"error": "title and content are required"})
        ntype = str(args.get("type", "note")).strip() or "note"
        tags = args.get("tags") or [ntype]
        path = self._write_note(title, content, ntype, [str(t) for t in tags])
        return json.dumps({"saved": str(path.relative_to(self._vault))})

    def _tool_search(self, args: Dict[str, Any]) -> str:
        query = str(args.get("query", "")).strip()
        if not query:
            return json.dumps({"error": "query is required"})
        limit = min(int(args.get("limit", 6) or 6), 20)
        return json.dumps({"results": self._search(query, limit=limit)})

    def _tool_read(self, args: Dict[str, Any]) -> str:
        rel = str(args.get("path", "")).strip()
        if not rel:
            return json.dumps({"error": "path is required"})
        target = (self._vault / rel).resolve()
        # Guard against path traversal outside the vault.
        if self._vault not in target.parents and target != self._vault:
            return json.dumps({"error": "path is outside the vault"})
        text = self._read_safe(target)
        if text is None:
            return json.dumps({"error": f"not found: {rel}"})
        return json.dumps({"path": rel, "content": text[:20_000]})

    # -- Vault helpers -------------------------------------------------------

    def _search(self, query: str, *, limit: int) -> List[Dict[str, Any]]:
        """Bounded case-insensitive term-frequency search over vault markdown."""
        terms = [t for t in re.split(r"\W+", query.lower()) if len(t) > 2]
        if not terms:
            return []
        scored: List[Dict[str, Any]] = []
        for i, md in enumerate(self._iter_notes()):
            if i >= _MAX_FILES_SCANNED:
                break
            text = self._read_safe(md, limit=_MAX_FILE_BYTES)
            if not text:
                continue
            low = text.lower()
            title = md.stem
            title_low = title.lower()
            score = 0
            for t in terms:
                score += low.count(t)
                score += 5 * title_low.count(t)  # title matches weighted
            if score:
                scored.append(
                    {
                        "path": str(md.relative_to(self._vault)),
                        "title": title,
                        "score": score,
                        "snippet": self._snippet(text, terms),
                    }
                )
        scored.sort(key=lambda r: r["score"], reverse=True)
        for r in scored:
            r.pop("score", None)
        return scored[:limit]

    def _iter_notes(self):
        if self._vault is None:
            return
        for md in self._vault.rglob("*.md"):
            parts = set(md.relative_to(self._vault).parts)
            if parts & _SKIP_DIRS:
                continue
            yield md

    @staticmethod
    def _snippet(text: str, terms: List[str]) -> str:
        low = text.lower()
        pos = min((low.find(t) for t in terms if low.find(t) >= 0), default=-1)
        if pos < 0:
            return text.strip()[:_SNIPPET_CHARS]
        start = max(0, pos - _SNIPPET_CHARS // 2)
        return text[start : start + _SNIPPET_CHARS].replace("\n", " ").strip()

    def _write_note(self, title: str, content: str, ntype: str, tags: List[str]) -> Path:
        """Write an AI-first note (see references/ai-first-rules.md)."""
        date = datetime.now().strftime("%Y-%m-%d")
        fname = f"{date} - {self._slug(title)}.md"
        path = self._vault / _NOTES_DIR / fname
        tag_block = "\n".join(f"  - {t}" for t in tags)
        preamble = content.strip().split("\n", 1)[0][:280]
        body = (
            f"---\n"
            f"type: {ntype}\n"
            f"date: {date}\n"
            f"tags:\n{tag_block}\n"
            f"ai-first: true\n"
            f"source: hermes-agent\n"
            f"session: {self._session_id}\n"
            f"---\n\n"
            f"## For future Claude\n"
            f"{preamble}\n\n"
            f"{content.strip()}\n"
        )
        path.write_text(body, encoding="utf-8")
        return path

    def _session_header(self, sid: str) -> str:
        date = datetime.now().strftime("%Y-%m-%d")
        return (
            f"---\n"
            f"type: hermes-session\n"
            f"date: {date}\n"
            f"tags:\n  - hermes-session\n"
            f"ai-first: true\n"
            f"source: hermes-agent\n"
            f"session: {sid}\n"
            f"---\n\n"
            f"## For future Claude\n"
            f"Transcript memory written by the Hermes agent for session {sid}. "
            f"Each entry is one turn (user + assistant), truncated. Started {self._now()}.\n"
        )

    def _read_safe(self, path: Path, *, limit: int = 4_000_000) -> Optional[str]:
        try:
            if not path.is_file():
                return None
            return path.read_text(encoding="utf-8", errors="replace")[:limit]
        except OSError:
            return None

    @staticmethod
    def _slug(text: str) -> str:
        s = re.sub(r"[^\w\s-]", "", text).strip().lower()
        s = re.sub(r"[\s_-]+", "-", s)
        return s[:80] or "untitled"

    @staticmethod
    def _trunc(text: str) -> str:
        text = (text or "").strip()
        return text if len(text) <= _TURN_TRUNC else text[:_TURN_TRUNC] + " [...]"

    @staticmethod
    def _now() -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M")


def register(ctx) -> None:
    """Entry point: Hermes calls this to register the provider."""
    ctx.register_memory_provider(ObsidianMemoryProvider())
