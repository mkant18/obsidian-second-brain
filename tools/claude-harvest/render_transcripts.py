#!/usr/bin/env python3
"""
Render Claude Code session transcripts to readable markdown notes.

The raw JSONL is a machine log: tool payloads, thinking blocks, hook context
and system reminders vastly outweigh the conversation. This collapses each
session into something a person can read — user turns and assistant prose
verbatim, every tool call reduced to one descriptive line.

Writes only under the staging tree. The Obsidian vault is never touched.

Usage:
    python3 render_transcripts.py [--limit N] [--min-turns N]
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from harvest_core import (  # noqa: E402
    STAGING,
    clean_subject,
    is_excluded,
    iter_all_sessions,
    local_date,
    parent_session_of,
    project_slug_of,
    windows_safe_name,
)

TRANSCRIPT_DIR = STAGING / "transcripts"
REPORT_DIR = STAGING / "_reports"

# Injected context that is machinery, not conversation. A user turn made
# entirely of these is a hook firing, not the person typing.
NOISE_PREFIXES = (
    "<system-reminder",
    "<local-command",
    "<command-name",
    "<command-message",
    "<user-prompt-submit-hook",
    "Caveat: The messages below were generated",
    "[SYSTEM NOTIFICATION",
    "<task-notification",
)

MAX_TEXT = 4000        # per turn, before truncation
MAX_TOOL_LINES = 400   # per session, before collapsing the tail


# Sessions produced by background tooling rather than by the user. These are
# a program prompting itself — 517 of them exist, each ~2 turns with zero
# artifacts, and importing them would bury the real conversations.
MACHINE_SESSION_MARKERS = (
    "/.claude-mem/observer-sessions",
    "claude-mem-observer-sessions",
)


def is_machine_session(jsonl: Path, cwd: str | None) -> bool:
    haystack = f"{jsonl} {cwd or ''}"
    return any(m in haystack for m in MACHINE_SESSION_MARKERS)


def is_noise(text: str) -> bool:
    t = text.lstrip()
    return any(t.startswith(p) for p in NOISE_PREFIXES)


def describe_tool(name: str, inp: dict) -> str:
    """One line per tool call. The payloads are the bulk of the raw log and
    are already captured as artifact notes elsewhere."""
    def short(p: str) -> str:
        return p.replace(str(Path.home()), "~")

    if name == "Write":
        return f"wrote `{short(str(inp.get('file_path', '?')))}` ({len(inp.get('content') or ''):,} bytes)"
    if name in ("Edit", "MultiEdit"):
        return f"edited `{short(str(inp.get('file_path', '?')))}`"
    if name == "Read":
        return f"read `{short(str(inp.get('file_path', '?')))}`"
    if name == "Bash":
        cmd = " ".join(str(inp.get("command", "")).split())
        return f"ran `{cmd[:110]}`" + ("…" if len(cmd) > 110 else "")
    if name in ("Grep", "Glob"):
        return f"searched `{inp.get('pattern', '?')}`"
    if name in ("Agent", "Task"):
        return f"dispatched subagent: {inp.get('description', '?')}"
    if name == "WebFetch":
        return f"fetched {inp.get('url', '?')}"
    if name == "WebSearch":
        return f"searched the web: {inp.get('query', '?')}"
    if name == "TodoWrite":
        return "updated the task list"
    return f"used {name}"


def render(jsonl: Path) -> tuple[str, dict] | None:
    """Return (markdown, metadata), or None if the session holds no conversation."""
    turns: list[str] = []
    session_id = jsonl.stem
    started = ended = None
    cwd = branch = None
    first_user: str | None = None
    counts: Counter = Counter()
    artifacts: list[str] = []
    tool_lines = 0

    with jsonl.open("r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError:
                counts["malformed_lines"] += 1
                continue
            if not isinstance(rec, dict):
                continue

            ts = rec.get("timestamp")
            if ts:
                started = ts if started is None or ts < started else started
                ended = ts if ended is None or ts > ended else ended
            if rec.get("sessionId"):
                session_id = rec["sessionId"]
            cwd = rec.get("cwd") or cwd
            branch = rec.get("gitBranch") or branch

            msg = rec.get("message")
            if not isinstance(msg, dict):
                continue
            role = msg.get("role")
            content = msg.get("content")
            blocks = content if isinstance(content, list) else (
                [{"type": "text", "text": content}] if isinstance(content, str) else []
            )

            for b in blocks:
                if not isinstance(b, dict):
                    continue
                btype = b.get("type")

                if btype == "text":
                    text = (b.get("text") or "").strip()
                    if not text or is_noise(text):
                        continue
                    if len(text) > MAX_TEXT:
                        text = text[:MAX_TEXT] + f"\n\n*…truncated, {len(text):,} chars total*"
                    if role == "user":
                        counts["user_turns"] += 1
                        if first_user is None:
                            first_user = text
                        turns.append(f"## User\n\n{text}\n")
                    elif role == "assistant":
                        counts["assistant_turns"] += 1
                        turns.append(f"## Claude\n\n{text}\n")

                elif btype == "tool_use":
                    name = b.get("name", "?")
                    inp = b.get("input") if isinstance(b.get("input"), dict) else {}
                    counts["tool_calls"] += 1
                    fp = inp.get("file_path")
                    if name in ("Write", "Edit") and isinstance(fp, str) and not is_excluded(fp):
                        artifacts.append(fp)
                    if tool_lines < MAX_TOOL_LINES:
                        turns.append(f"> `→` {describe_tool(name, inp)}\n")
                        tool_lines += 1
                    elif tool_lines == MAX_TOOL_LINES:
                        turns.append("> `→` *…further tool calls omitted*\n")
                        tool_lines += 1

    if not counts["user_turns"] and not counts["assistant_turns"]:
        return None  # pure machinery; nothing a person said or was told
    if is_machine_session(jsonl, cwd):
        return None  # background tooling talking to itself, not a conversation

    title = clean_subject(first_user, max_len=70) or "Untitled session"
    date = local_date(ended)
    unique_artifacts = sorted(set(artifacts))

    tags = [
        "claude-export", "claude/source/claude-code", "claude/type/transcript",
        f"claude/status/{'substantial' if counts['assistant_turns'] >= 5 else 'brief'}",
    ]
    if date != "unknown":
        tags += [f"year/{date[:4]}", f"month/{date[:7]}"]
    project = project_slug_of(jsonl).lstrip("-").replace("Users-michaelkanter-", "")
    if project:
        tags.append(f"claude/project/{windows_safe_name(project, max_len=60)}")

    def esc(v: str) -> str:
        return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'

    fm = ["---", "source: claude-code",
          f"conversation-id: {esc(session_id)}",
          f"session-date: {esc(date)}",
          f"title: {esc(title)}",
          f"project: {esc(project or 'unknown')}"]
    if branch:
        fm.append(f"git-branch: {esc(branch)}")
    if cwd:
        fm.append(f"cwd: {esc(cwd)}")
    fm += [f"user-turns: {counts['user_turns']}",
           f"assistant-turns: {counts['assistant_turns']}",
           f"tool-calls: {counts['tool_calls']}",
           f"artifacts-produced: {len(unique_artifacts)}",
           "needs-content-tags: true",
           "tags:"]
    fm += [f"  - {esc(t)}" for t in tags]
    fm.append("---")

    body = [f"\n# {title}\n"]
    if unique_artifacts:
        body.append("## Files authored in this conversation\n")
        body += [f"- `{p.replace(str(Path.home()), '~')}`" for p in unique_artifacts[:60]]
        if len(unique_artifacts) > 60:
            body.append(f"- *…and {len(unique_artifacts) - 60} more*")
        body.append("")
    body.append("---\n")
    body += turns

    meta = {
        "conversation_id": session_id,
        "session_date": date,
        "title": title,
        "project": project,
        "parent_session": parent_session_of(jsonl),
        "user_turns": counts["user_turns"],
        "assistant_turns": counts["assistant_turns"],
        "tool_calls": counts["tool_calls"],
        "artifacts_produced": unique_artifacts,
        "malformed_lines": counts["malformed_lines"],
        "needs_content_tags": True,
    }
    return "\n".join(fm) + "\n".join(body) + "\n", meta


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--min-turns", type=int, default=1,
                    help="skip sessions with fewer assistant turns than this")
    args = ap.parse_args()

    TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)

    manifest: list[dict] = []
    used: set[str] = set()
    stats: Counter = Counter()

    for i, jsonl in enumerate(iter_all_sessions()):
        if args.limit and i >= args.limit:
            break
        stats["sessions_seen"] += 1
        try:
            result = render(jsonl)
        except Exception as exc:  # one bad session must not kill the batch
            stats["render_errors"] += 1
            manifest.append({"transcript": None, "source_jsonl": str(jsonl),
                             "error": f"{type(exc).__name__}: {exc}"})
            continue
        if result is None:
            stats["skipped_no_conversation"] += 1
            continue
        md, meta = result
        if meta["assistant_turns"] < args.min_turns:
            stats["skipped_too_short"] += 1
            continue

        project = windows_safe_name(meta["project"] or "unknown", max_len=60)
        stem = windows_safe_name(f"{meta['session_date']} {meta['title']}", max_len=100)
        rel = f"{project}/{stem}.md"
        n = 1
        while rel.lower() in used:
            n += 1
            rel = f"{project}/{stem} ({n}).md"
        used.add(rel.lower())

        out = TRANSCRIPT_DIR / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(md, encoding="utf-8")

        stats["transcripts_written"] += 1
        stats["total_artifacts_referenced"] += len(meta["artifacts_produced"])
        meta["transcript"] = rel
        meta["source_jsonl"] = str(jsonl)
        manifest.append(meta)

    (REPORT_DIR / "manifest_transcripts.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    print(json.dumps({"stats": dict(stats),
                      "manifest": str(REPORT_DIR / "manifest_transcripts.json")},
                     indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
