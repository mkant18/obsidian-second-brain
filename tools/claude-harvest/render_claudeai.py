#!/usr/bin/env python3
"""
Render downloaded claude.ai conversations to markdown transcripts.

Separate from render_transcripts.py: that one reads Claude Code's
line-delimited JSONL session logs; this reads claude.ai's conversation JSON —
one object per conversation with a chat_messages array.

Doing this locally means a browser agent only has to fetch raw JSON. Two
agents already died mid-run trying to fetch and format in one pass.

Usage:
    python3 render_claudeai.py
"""

from __future__ import annotations

import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from harvest_core import (  # noqa: E402
    STAGING,
    scan_for_secrets,
    windows_safe_name,
)

ROOT = STAGING / "claude-ai"
RAW = ROOT / "transcripts-raw"
OUT = ROOT / "transcripts"
ARTIFACTS = ROOT / "artifacts"
REPORT = STAGING / "_reports"


def esc(v) -> str:
    return '"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'


def message_text(msg: dict) -> str:
    """Prefer the content blocks; fall back to the flat text field."""
    blocks = msg.get("content")
    if isinstance(blocks, list):
        parts = [
            b.get("text", "")
            for b in blocks
            if isinstance(b, dict) and b.get("type") == "text" and b.get("text")
        ]
        if parts:
            return "\n\n".join(parts).strip()
    return (msg.get("text") or "").strip()


def render(raw_path: Path) -> tuple[str, dict] | None:
    try:
        conv = json.loads(raw_path.read_text(encoding="utf-8", errors="replace"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(conv, dict):
        return None

    uuid = conv.get("uuid") or raw_path.stem
    title = (conv.get("name") or "").strip() or "Untitled conversation"
    created = str(conv.get("created_at") or "")[:10] or "unknown"

    body: list[str] = []
    counts: Counter = Counter()
    attachments: list[str] = []

    for m in conv.get("chat_messages") or []:
        if not isinstance(m, dict):
            continue
        sender = m.get("sender") or m.get("role") or "unknown"
        for coll in ("attachments", "files"):
            for f in m.get(coll) or []:
                if isinstance(f, dict):
                    name = f.get("file_name") or f.get("name")
                    if name:
                        attachments.append(name)
        text = message_text(m)
        if not text:
            continue
        if sender == "human":
            counts["human"] += 1
            body.append(f"## User\n\n{text}\n")
        else:
            counts["assistant"] += 1
            body.append(f"## Claude\n\n{text}\n")

    if not counts:
        return None  # nothing a person said or was told

    art_dir = ARTIFACTS / uuid
    art_files = sorted(p for p in art_dir.glob("*") if p.is_file()) if art_dir.is_dir() else []

    secrets = scan_for_secrets("\n".join(body))

    tags = [
        "claude-export", "claude/source/claude-ai", "claude/type/transcript",
        f"claude/status/{'substantial' if counts['assistant'] >= 5 else 'brief'}",
    ]
    if created != "unknown":
        tags += [f"year/{created[:4]}", f"month/{created[:7]}"]
    if secrets:
        tags.append("claude/flagged/credentials")

    fm = ["---", "source: claude-ai",
          f"conversation-id: {esc(uuid)}",
          f"session-date: {esc(created)}",
          f"title: {esc(title)}",
          f"model: {esc(conv.get('model') or 'unknown')}",
          f"user-turns: {counts['human']}",
          f"assistant-turns: {counts['assistant']}",
          f"artifacts-produced: {len(art_files)}",
          "needs-content-tags: true"]
    if secrets:
        fm.append(f"secret-findings: {esc(', '.join(sorted({s.kind for s in secrets})))}")
        fm.append("sensitive: true")
    fm.append("tags:")
    fm += [f"  - {esc(t)}" for t in tags]
    fm.append("---")

    head = [f"\n# {title}\n"]
    if art_files:
        head.append("## Artifacts from this conversation\n")
        head += [f"- `{p.name}`" for p in art_files]
        head.append("")
    if attachments:
        head.append("## Files the user attached\n")
        head += [f"- `{a}`" for a in sorted(set(attachments))]
        head.append("")
    if secrets:
        head += [
            "> [!danger] Possible credentials detected",
            f"> Pattern(s): {', '.join(sorted({s.kind for s in secrets}))}. "
            f"Line(s): {', '.join(str(s.line_number) for s in secrets)}.",
            "",
        ]
    head.append("---\n")

    meta = {
        "uuid": uuid,
        "title": title,
        "session_date": created,
        "model": conv.get("model"),
        "message_count": counts["human"] + counts["assistant"],
        "user_turns": counts["human"],
        "assistant_turns": counts["assistant"],
        "raw_path": str(raw_path.relative_to(STAGING)),
        "artifacts": [
            {"name": p.name,
             "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
             "path": str(p.relative_to(STAGING))}
            for p in art_files
        ],
        "attachments_noted": sorted(set(attachments)),
        "secret_findings": [
            {"kind": s.kind, "line": s.line_number, "excerpt": s.excerpt}
            for s in secrets
        ],
        "status": "ok",
        "needs_content_tags": True,
    }
    return "\n".join(fm) + "\n".join(head) + "\n".join(body) + "\n", meta


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    REPORT.mkdir(parents=True, exist_ok=True)

    manifest: list[dict] = []
    used: set[str] = set()
    stats: Counter = Counter()

    for raw in sorted(RAW.glob("*.json")):
        stats["raw_seen"] += 1
        result = render(raw)
        if result is None:
            stats["unrenderable"] += 1
            manifest.append({"uuid": raw.stem, "status": "failed",
                             "failure_reason": "empty or unparseable conversation JSON"})
            continue
        md, meta = result

        stem = windows_safe_name(
            f"{meta['session_date']} {meta['title']} [{meta['uuid'][:8]}]", max_len=110
        )
        rel = f"{stem}.md"
        n = 1
        while rel.lower() in used:
            n += 1
            rel = f"{stem} ({n}).md"
        used.add(rel.lower())

        (OUT / rel).write_text(md, encoding="utf-8")
        meta["transcript_path"] = str((OUT / rel).relative_to(STAGING))
        manifest.append(meta)
        stats["rendered"] += 1
        stats["artifacts_linked"] += len(meta["artifacts"])
        if meta["secret_findings"]:
            stats["with_secret_findings"] += 1

    # Conversations enumerated but never downloaded stay visible as explicit
    # failures rather than silently vanishing from the count.
    lst_path = ROOT / "conversations_list.json"
    if lst_path.exists():
        have = {m.get("uuid") for m in manifest}
        for c in json.loads(lst_path.read_text()):
            cid = c.get("uuid") or c.get("id")
            if cid and cid not in have:
                manifest.append({"uuid": cid, "title": c.get("name") or "",
                                 "status": "failed",
                                 "failure_reason": "never downloaded"})
                stats["never_downloaded"] += 1

    (REPORT / "manifest_claudeai.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    print(json.dumps({"stats": dict(stats),
                      "manifest": str(REPORT / "manifest_claudeai.json")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
