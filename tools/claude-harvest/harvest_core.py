"""
Core extraction engine for Claude-authored artifacts.

Two entry points share this module:
  - the one-time harvest, which runs over every session in ~/.claude/projects/
  - the session hook, which runs over exactly one session

Everything here is read-only with respect to source data and writes only into
the staging tree. Nothing in this module may write to the Obsidian vault.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterator

HOME = Path.home()
PROJECTS_DIR = HOME / ".claude" / "projects"
FILE_HISTORY_DIR = HOME / ".claude" / "file-history"
VAULT = HOME / "Desktop" / "OBSIDIAN"
STAGING = HOME / "Desktop" / "claude-harvest-staging"

# Paths we never harvest. Vault paths are excluded because those artifacts
# already exist as live notes; importing them would duplicate the vault into
# itself. The rest is dependency and build noise.
EXCLUDED_PATH_PARTS = (
    "/node_modules/", "/.git/", "/dist/", "/build/", "/.next/", "/target/",
    "/venv/", "/.venv/", "/__pycache__/", "/site-packages/", "/vendor/",
    "/.cache/", "/coverage/", "/.pytest_cache/", "/.mypy_cache/",
)

EXCLUDED_BASENAMES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock",
    "Cargo.lock", "uv.lock", "Gemfile.lock", "composer.lock", ".DS_Store",
}

EXCLUDED_SUFFIXES = (".min.js", ".min.css", ".map", ".pyc", ".lock")

# Windows reserved device names. These fail as filenames even with an
# extension, and the vault syncs to a Windows machine.
WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

WINDOWS_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# Map file extension to a markdown fence language. Artifacts are wrapped in a
# fenced block rather than written raw, because YAML frontmatter cannot live
# inside a .py or .ts file without breaking it, and Obsidian only parses
# frontmatter in markdown.
FENCE_LANG = {
    ".py": "python", ".ts": "typescript", ".tsx": "tsx", ".js": "javascript",
    ".jsx": "jsx", ".mjs": "javascript", ".mts": "typescript", ".cjs": "javascript",
    ".json": "json", ".jsonl": "json", ".sh": "bash", ".bash": "bash",
    ".zsh": "bash", ".fish": "fish", ".toml": "toml", ".yaml": "yaml",
    ".yml": "yaml", ".html": "html", ".htm": "html", ".css": "css",
    ".scss": "scss", ".sql": "sql", ".rs": "rust", ".go": "go", ".java": "java",
    ".rb": "ruby", ".php": "php", ".swift": "swift", ".kt": "kotlin",
    ".c": "c", ".h": "c", ".cpp": "cpp", ".hpp": "cpp", ".cs": "csharp",
    ".xml": "xml", ".csv": "csv", ".txt": "text", ".env": "bash",
    ".ini": "ini", ".cfg": "ini", ".conf": "ini", ".md": "markdown",
    ".mdx": "markdown",
}

DOCUMENT_EXTS = {".md", ".mdx", ".txt", ".csv", ".html", ".htm"}


# --------------------------------------------------------------------------
# Secrets detection
# --------------------------------------------------------------------------

# Tuned to the credential classes already confirmed present on this machine
# (a Doppler service token and a Syncthing API key), plus common cloud formats.
SECRET_PATTERNS: list[tuple[str, re.Pattern]] = [
    # Doppler tokens carry an inner config segment: dp.st.<config>.<secret>
    ("doppler_service_token", re.compile(r"\bdp\.(?:st|pt|sa|scim)\.[A-Za-z0-9_.-]{20,}")),
    ("anthropic_api_key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}")),
    ("openai_api_key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9]{32,}")),
    ("aws_access_key_id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("aws_secret_access_key", re.compile(r"(?i)aws.{0,20}secret.{0,20}['\"][A-Za-z0-9/+=]{40}['\"]")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}")),
    ("gitlab_token", re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}")),
    ("stripe_key", re.compile(r"\b[rs]k_(?:live|test)_[A-Za-z0-9]{20,}")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("sendgrid_key", re.compile(r"\bSG\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}")),
    ("npm_token", re.compile(r"\bnpm_[A-Za-z0-9]{36}\b")),
    ("private_key_block", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    ("syncthing_apikey", re.compile(r"(?i)\bapi[_-]?key['\"\s:=]{1,4}[A-Za-z0-9]{24,}")),
    ("bearer_token", re.compile(r"(?i)\bauthorization['\"\s:=]{1,4}bearer\s+[A-Za-z0-9._-]{20,}")),
    ("env_assignment_secret", re.compile(
        r"(?im)^\s*(?:export\s+)?[A-Z0-9_]*"
        r"(?:SECRET|PASSWORD|PASSWD|TOKEN|APIKEY|API_KEY|PRIVATE_KEY|ACCESS_KEY|CREDENTIAL)"
        r"[A-Z0-9_]*\s*=\s*['\"]?"
        r"(?!\s*$)(?!.*(?:\$\{|<your|xxx|placeholder|changeme|example|\.\.\.))"
        r"[^\s'\"]{12,}"
    )),
]

# Substrings that mark an apparent hit as documentation rather than a live
# credential. Checked against the matched line, case-insensitively.
SECRET_FALSE_POSITIVE_HINTS = (
    "example", "placeholder", "your-", "your_", "<token>", "dummy", "sample",
    "redacted", "xxxxx", "changeme", "fake", "notreal", "dp.st.prd.xxx",
)


@dataclass
class SecretFinding:
    kind: str
    line_number: int
    excerpt: str  # masked; never the raw secret


def scan_for_secrets(text: str) -> list[SecretFinding]:
    """Return credential findings. Excerpts are masked, never raw values."""
    findings: list[SecretFinding] = []
    lines = text.splitlines()
    for kind, pattern in SECRET_PATTERNS:
        for match in pattern.finditer(text):
            line_no = text.count("\n", 0, match.start()) + 1
            line = lines[line_no - 1] if line_no <= len(lines) else ""
            if any(h in line.lower() for h in SECRET_FALSE_POSITIVE_HINTS):
                continue
            raw = match.group(0)
            findings.append(SecretFinding(
                kind, line_no, f"{raw[:6]}…[{len(raw)} chars redacted]"
            ))
    seen: set[tuple[str, int]] = set()
    unique: list[SecretFinding] = []
    for f in findings:
        if (f.kind, f.line_number) not in seen:
            seen.add((f.kind, f.line_number))
            unique.append(f)
    return unique


# --------------------------------------------------------------------------
# Filename safety
# --------------------------------------------------------------------------

def windows_safe_name(name: str, *, max_len: int = 120) -> str:
    """Make one path component safe on Windows, APFS and Obsidian.

    The vault syncs to a Windows PC, where illegal characters, trailing dots
    and reserved device names cause files to silently fail to arrive.
    """
    name = unicodedata.normalize("NFC", name)
    name = WINDOWS_ILLEGAL.sub("-", name)
    # Obsidian reads these as link/embed syntax inside note titles.
    name = name.replace("[", "(").replace("]", ")").replace("#", "-").replace("^", "-")
    name = name.strip()
    while name.endswith((".", " ")):
        name = name[:-1]
    # A leading dash makes the name look like a CLI flag and breaks shell
    # tooling. A leading dot hides the note from Obsidian entirely, but
    # stripping it would conflate .gitignore with gitignore — so rename
    # rather than truncate, preserving which one it was.
    name = name.lstrip("- ")
    if name.startswith("."):
        name = "dot-" + name[1:]
    stem, dot, ext = name.rpartition(".")
    if dot and stem.upper() in WINDOWS_RESERVED:
        name = f"{stem}_.{ext}"
    elif not dot and name.upper() in WINDOWS_RESERVED:
        name = f"{name}_"
    if len(name) > max_len:
        stem, dot, ext = name.rpartition(".")
        if dot and len(ext) < 12:
            name = f"{stem[:max_len - len(ext) - 1]}.{ext}"
        else:
            name = name[:max_len]
    return name or "unnamed"


def is_excluded(file_path: str) -> bool:
    """True if this artifact path should never be harvested."""
    p = file_path.replace("\\", "/")
    if p.startswith(str(VAULT)):
        return True  # already a live vault note
    if any(part in p for part in EXCLUDED_PATH_PARTS):
        return True
    base = p.rsplit("/", 1)[-1]
    if base in EXCLUDED_BASENAMES:
        return True
    return any(base.endswith(s) for s in EXCLUDED_SUFFIXES)


# --------------------------------------------------------------------------
# Transcript parsing
# --------------------------------------------------------------------------

@dataclass
class ToolEvent:
    kind: str              # "write" | "edit"
    file_path: str
    timestamp: str
    content: str | None = None       # full body, Write only
    old_string: str | None = None    # Edit only
    new_string: str | None = None    # Edit only


@dataclass
class SessionData:
    session_id: str
    transcript_path: Path
    project_slug: str
    cwd: str | None = None
    git_branch: str | None = None
    first_user_message: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    events: list[ToolEvent] = field(default_factory=list)


def project_slug_of(jsonl_path: Path) -> str:
    """The top-level project directory this transcript belongs to.

    Transcripts nest as <project>/<session>.jsonl but also as
    <project>/<session>/subagents/agent-<id>.jsonl and under workflow
    directories. Using the immediate parent lumps every subagent under a
    literal "subagents" folder, so take the first component below
    PROJECTS_DIR instead.
    """
    try:
        rel = jsonl_path.relative_to(PROJECTS_DIR)
    except ValueError:
        return jsonl_path.parent.name
    return rel.parts[0] if rel.parts else "unknown"


def clean_subject(text: str | None, *, max_len: int = 110) -> str | None:
    """Collapse a first user message into a one-line subject.

    Raw prompts run to hundreds of characters and contain newlines, which
    make an unreadable note title and produce fragile YAML.
    """
    if not text:
        return None
    flat = " ".join(text.split())
    # Drop a leading command invocation like "/loop " or "REPO: <path>".
    flat = re.sub(r"^(?:REPO|FILE|PATH):\s*\S+\s*", "", flat)
    if len(flat) <= max_len:
        return flat or None
    cut = flat[:max_len].rsplit(" ", 1)[0]
    return (cut or flat[:max_len]) + "…"


def _iter_tool_use(message) -> Iterator[dict]:
    """Yield tool_use blocks. message.content is usually a list, sometimes a str."""
    if not isinstance(message, dict):
        return
    content = message.get("content")
    if not isinstance(content, list):
        return  # bare string content cannot carry tool_use blocks
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            yield block


def parse_session(jsonl_path: Path) -> SessionData:
    """Stream one transcript. Never loads the whole file into memory."""
    session = SessionData(
        session_id=jsonl_path.stem,
        transcript_path=jsonl_path,
        project_slug=project_slug_of(jsonl_path),
    )
    with jsonl_path.open("r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError:
                continue  # malformed lines are skipped, not guessed at
            if not isinstance(rec, dict):
                continue

            ts = rec.get("timestamp")
            if ts:
                if session.started_at is None or ts < session.started_at:
                    session.started_at = ts
                if session.ended_at is None or ts > session.ended_at:
                    session.ended_at = ts

            if rec.get("sessionId"):
                session.session_id = rec["sessionId"]
            session.cwd = rec.get("cwd") or session.cwd
            session.git_branch = rec.get("gitBranch") or session.git_branch

            message = rec.get("message")
            if not isinstance(message, dict):
                continue

            if session.first_user_message is None and message.get("role") == "user":
                c = message.get("content")
                text = c if isinstance(c, str) else None
                if isinstance(c, list):
                    for b in c:
                        if isinstance(b, dict) and b.get("type") == "text":
                            text = b.get("text")
                            break
                # Skip system-injected reminders and hook context.
                if text and not text.lstrip().startswith("<"):
                    session.first_user_message = text.strip()[:500]

            for block in _iter_tool_use(message):
                name = block.get("name")
                inp = block.get("input")
                if not isinstance(inp, dict):
                    continue
                fp = inp.get("file_path")
                if not isinstance(fp, str) or not fp:
                    continue
                if name == "Write":
                    session.events.append(ToolEvent(
                        kind="write", file_path=fp, timestamp=ts or "",
                        content=inp.get("content") or "",
                    ))
                elif name == "Edit":
                    session.events.append(ToolEvent(
                        kind="edit", file_path=fp, timestamp=ts or "",
                        old_string=inp.get("old_string"),
                        new_string=inp.get("new_string"),
                    ))
    return session


# --------------------------------------------------------------------------
# Content resolution
# --------------------------------------------------------------------------

@dataclass
class Artifact:
    session_id: str
    project_slug: str
    file_path: str
    content: str | None
    provenance: str      # how the body was obtained
    confidence: str      # "full" | "reconstructed" | "fragments-only"
    write_count: int
    edit_count: int
    first_seen: str
    last_seen: str
    cwd: str | None
    git_branch: str | None
    content_hash: str | None
    secrets: list[SecretFinding] = field(default_factory=list)

    @property
    def is_document(self) -> bool:
        return Path(self.file_path).suffix.lower() in DOCUMENT_EXTS


def _apply_edits(base: str, edits: list[ToolEvent]) -> tuple[str, bool]:
    """Replay edits onto a base body. Returns (text, all_applied_cleanly)."""
    text = base
    clean = True
    for e in edits:
        if not e.old_string:
            clean = False
            continue
        if e.old_string in text:
            text = text.replace(e.old_string, e.new_string or "", 1)
        else:
            clean = False  # edit does not match this base; do not force it
    return text, clean


def resolve_artifacts(session: SessionData) -> list[Artifact]:
    """Reconstruct the final state of each file touched in this session.

    Content is only claimed when it can be established honestly. Where a path
    was only ever Edited and no full body is recoverable, the artifact is
    marked fragments-only and carries no content. Concatenating new_strings
    into something file-shaped would be a fabrication, not a recovery.
    """
    by_path: dict[str, list[ToolEvent]] = {}
    for ev in session.events:
        if is_excluded(ev.file_path):
            continue
        by_path.setdefault(ev.file_path, []).append(ev)

    artifacts: list[Artifact] = []
    for fp, events in by_path.items():
        events.sort(key=lambda e: e.timestamp)
        writes = [e for e in events if e.kind == "write"]
        edits = [e for e in events if e.kind == "edit"]

        content: str | None = None
        provenance = "none"
        confidence = "fragments-only"

        if writes:
            last_write = writes[-1]
            base = last_write.content or ""
            later_edits = [e for e in edits if e.timestamp > last_write.timestamp]
            if later_edits:
                content, clean = _apply_edits(base, later_edits)
                provenance = "transcript-write+edits"
                confidence = "full" if clean else "reconstructed"
            else:
                content = base
                provenance = "transcript-write"
                confidence = "full"
        else:
            # Edit-only path. Try file-history, then the live file on disk.
            snap = _find_file_history_snapshot(session.session_id)
            # file-history is content-addressed with no path index, so a
            # session's lone snapshot family does NOT prove it belongs to this
            # file. Demand evidence: an edit's old_string must actually occur
            # in the snapshot. Without that check a snapshot of file X gets
            # served as the body of file Y, labelled "full".
            if snap is not None and not _snapshot_matches_edits(snap, edits):
                snap = None
            if snap is not None:
                content, provenance, confidence = snap, "file-history-snapshot", "full"
            else:
                disk = Path(fp)
                if disk.is_file():
                    try:
                        content = disk.read_text(encoding="utf-8", errors="replace")
                        provenance = "on-disk-current"
                        # May have changed since the session ended.
                        confidence = "reconstructed"
                    except OSError:
                        content = None
                if content is None:
                    provenance, confidence = "edit-fragments-only", "fragments-only"

        artifacts.append(Artifact(
            session_id=session.session_id,
            project_slug=session.project_slug,
            file_path=fp,
            content=content,
            provenance=provenance,
            confidence=confidence,
            write_count=len(writes),
            edit_count=len(edits),
            first_seen=events[0].timestamp,
            last_seen=events[-1].timestamp,
            cwd=session.cwd,
            git_branch=session.git_branch,
            content_hash=(
                hashlib.sha256(content.encode("utf-8", "replace")).hexdigest()
                if content is not None else None
            ),
            secrets=scan_for_secrets(content) if content else [],
        ))
    return artifacts


def _snapshot_matches_edits(snapshot: str, edits: list[ToolEvent]) -> bool:
    """True only if this snapshot demonstrably belongs to the edited file.

    An Edit's old_string was, by definition, present in the file at the time
    of the edit; new_string is present immediately after. If neither appears
    anywhere in the snapshot, the snapshot is some other file's content and
    must not be served as this file's body.
    """
    for e in edits:
        for frag in (e.old_string, e.new_string):
            if not frag:
                continue
            probe = frag.strip()
            if len(probe) < 12:
                continue  # too short to be distinctive
            if probe in snapshot:
                return True
    return False


_FH_CACHE: dict[str, dict[str, list[Path]]] = {}


def _find_file_history_snapshot(session_id: str) -> str | None:
    """Return the latest file-history snapshot body for this session.

    file-history addresses content by an opaque 16-hex hash with no path
    index, so an exact path match is impossible. When a session holds exactly
    one snapshot family the association is unambiguous; otherwise we decline.
    Returning the wrong file's body would be worse than returning nothing.
    """
    if session_id not in _FH_CACHE:
        sess_dir = FILE_HISTORY_DIR / session_id
        families: dict[str, list[Path]] = {}
        if sess_dir.is_dir():
            for s in sess_dir.glob("*@v*"):
                families.setdefault(s.name.split("@v")[0], []).append(s)
        _FH_CACHE[session_id] = families
    families = _FH_CACHE[session_id]
    if len(families) != 1:
        return None  # ambiguous or absent; refuse to guess
    only = next(iter(families.values()))
    latest = max(only, key=lambda p: int(p.name.split("@v")[1]))
    try:
        return latest.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def iter_all_sessions(include_subagents: bool = True) -> Iterator[Path]:
    """Every session transcript.

    Subagent transcripts live in a `subagents/` directory beside their parent
    and are included by default: a file authored by a subagent is still a file
    Claude authored, and excluding them silently drops more than half the
    artifacts. Each subagent transcript carries its own sessionId, so the
    parent session is recovered from the containing directory name.
    """
    for p in sorted(PROJECTS_DIR.rglob("*.jsonl")):
        if not include_subagents and "/subagents/" in str(p):
            continue
        yield p


def parent_session_of(jsonl_path: Path) -> str | None:
    """For a subagent transcript, the parent session UUID; else None.

    Layout is <project>/<parent-session>/subagents/agent-<id>.jsonl, so the
    grandparent directory name is the parent session.
    """
    if jsonl_path.parent.name != "subagents":
        return None
    return jsonl_path.parent.parent.name


def local_date(ts: str | None) -> str:
    """ISO date in local time, matching how the user reads session dates."""
    if not ts:
        return "unknown"
    try:
        return (
            datetime.fromisoformat(ts.replace("Z", "+00:00"))
            .astimezone()
            .strftime("%Y-%m-%d")
        )
    except ValueError:
        return ts[:10]
