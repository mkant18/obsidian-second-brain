#!/usr/bin/env python3
"""
Phase 1 batch harvest: Claude Code JSONL artifacts -> staging markdown notes.

Emits one .md note per distinct (session, file_path), plus a manifest the
classification agents consume in Phase 2. Writes only under the staging tree;
the Obsidian vault is never touched here.

Usage:
    python3 harvest_all.py [--limit N] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from harvest_core import (  # noqa: E402
    FENCE_LANG,
    clean_subject,
    STAGING,
    Artifact,
    iter_all_sessions,
    local_date,
    parent_session_of,
    parse_session,
    resolve_artifacts,
    windows_safe_name,
)

ARTIFACT_DIR = STAGING / "artifacts"
REPORT_DIR = STAGING / "_reports"

# Cowork/Code material the user was warned about and chose to import anyway.
# Tagged so it stays findable rather than silently blended in.
SENSITIVE_MARKERS = (
    "incentive plan",
    "lawyer",
    "reimbursement reconciliation",
    "paulweiss",
    "paul weiss",
)


def yaml_escape(value: str) -> str:
    """Quote a scalar for YAML. Frontmatter breaks on unescaped colons."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def yaml_block(fields: dict[str, object]) -> str:
    """Render frontmatter. Lists become YAML sequences, not inline strings."""
    lines = ["---"]
    for key, value in fields.items():
        if value is None:
            continue
        if isinstance(value, list):
            if not value:
                continue
            lines.append(f"{key}:")
            lines.extend(f"  - {yaml_escape(str(v))}" for v in value)
        elif isinstance(value, bool):
            lines.append(f"{key}: {str(value).lower()}")
        elif isinstance(value, int):
            lines.append(f"{key}: {value}")
        else:
            lines.append(f"{key}: {yaml_escape(str(value))}")
    lines.append("---")
    return "\n".join(lines)


def base_tags(art: Artifact) -> list[str]:
    """Deterministic tags only.

    Content tags are added in Phase 2 by an agent that actually reads the
    file. Anything derivable from metadata belongs here; anything requiring
    comprehension does not.
    """
    tags = ["claude-export", "claude/code"]
    suffix = Path(art.file_path).suffix.lower().lstrip(".")
    if suffix:
        tags.append(f"filetype/{suffix}")
    lang = FENCE_LANG.get(Path(art.file_path).suffix.lower())
    if lang and lang not in {"text", "markdown"}:
        tags.append(f"lang/{lang}")
    tags.append(f"confidence/{art.confidence}")
    date = local_date(art.last_seen)
    if date != "unknown":
        tags.append(f"year/{date[:4]}")
        tags.append(f"month/{date[:7]}")
    if art.secrets:
        tags.append("needs-secrets-review")
    project = art.project_slug.lstrip("-").replace("Users-michaelkanter-", "")
    if project:
        tags.append(f"project/{windows_safe_name(project, max_len=60)}")
    return tags


def looks_sensitive(art: Artifact) -> bool:
    haystack = f"{art.file_path} {art.project_slug}".lower()
    return any(m in haystack for m in SENSITIVE_MARKERS)


def render_note(art: Artifact, subject: str | None) -> str:
    """One artifact -> one markdown note.

    Original content goes in a fenced block tagged with its language rather
    than raw, so the note stays valid markdown regardless of source file type
    and Obsidian can parse the frontmatter above it.
    """
    original = Path(art.file_path)
    fields: dict[str, object] = {
        "source": "claude-code",
        "conversation-id": art.session_id,
        "session-date": local_date(art.last_seen),
        "original-path": art.file_path,
        "original-filename": original.name,
        "subject": subject or original.stem.replace("_", " ").replace("-", " "),
        "provenance": art.provenance,
        "content-confidence": art.confidence,
        "write-count": art.write_count,
        "edit-count": art.edit_count,
        "project": art.project_slug,
        "git-branch": art.git_branch,
        "cwd": art.cwd,
        "content-hash": art.content_hash,
        "sensitive": True if looks_sensitive(art) else None,
        "tags": base_tags(art),
    }

    parts = [yaml_block(fields), "", f"# {original.name}", ""]

    if art.confidence == "fragments-only":
        parts += [
            "> [!warning] No recoverable content",
            "> This file was only ever modified with `Edit`, and no full body",
            "> survives in the transcript, in file-history, or on disk. Only",
            "> edit fragments exist, and stitching them together would produce",
            f"> a plausible file that was never real. {art.edit_count} edit(s)",
            "> were recorded against this path.",
            "",
        ]
        return "\n".join(parts) + "\n"

    if art.confidence == "reconstructed":
        note = {
            "on-disk-current": (
                "Body read from the file as it exists on disk now. It may have "
                "changed since this session ended."
            ),
            "transcript-write+edits": (
                "Body rebuilt by replaying edits onto the last full write. At "
                "least one edit did not match cleanly and was skipped."
            ),
        }.get(art.provenance, "Body reconstructed rather than read verbatim.")
        parts += [f"> [!info] Reconstructed content", f"> {note}", ""]

    if art.secrets:
        kinds = sorted({s.kind for s in art.secrets})
        parts += [
            "> [!danger] Possible credentials detected",
            f"> Pattern(s): {', '.join(kinds)}. Line(s): "
            f"{', '.join(str(s.line_number) for s in art.secrets)}.",
            "> Must be reviewed before import; see the quarantine report.",
            "",
        ]

    lang = FENCE_LANG.get(original.suffix.lower(), "")
    body = art.content or ""
    # The fence must be longer than any backtick run inside the content, or
    # the block closes early and the rest of the file leaks into the note.
    longest = run = 0
    for ch in body:
        run = run + 1 if ch == "`" else 0
        longest = max(longest, run)
    fence = "`" * max(3, longest + 1)
    parts += [f"{fence}{lang}", body.rstrip("\n"), fence, ""]
    return "\n".join(parts) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="stop after N sessions")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)

    manifest: list[dict] = []
    seen_hashes: dict[str, str] = {}   # content hash -> first note path
    used_names: set[str] = set()
    stats: Counter = Counter()
    by_project: dict[str, int] = defaultdict(int)
    secret_rows: list[dict] = []

    for i, jsonl in enumerate(iter_all_sessions()):
        if args.limit and i >= args.limit:
            break
        session = parse_session(jsonl)
        parent = parent_session_of(jsonl)
        subject = clean_subject(session.first_user_message)

        for art in resolve_artifacts(session):
            stats["artifacts_seen"] += 1

            # Collapse identical bodies. A file written twelve times in one
            # session is one artifact, and so is the same body across sessions.
            if art.content_hash and art.content_hash in seen_hashes:
                stats["duplicate_bodies_skipped"] += 1
                continue
            if art.content_hash:
                seen_hashes[art.content_hash] = art.file_path

            project = windows_safe_name(
                art.project_slug.lstrip("-").replace("Users-michaelkanter-", "")
                or "unknown", max_len=60
            )
            sess_short = art.session_id[:8]
            stem = windows_safe_name(Path(art.file_path).name, max_len=90)
            rel = f"{project}/{sess_short}/{stem}.md"

            # Case-insensitive collision guard: APFS tolerates Readme.md beside
            # README.md, the Windows side of the sync does not.
            n = 1
            while rel.lower() in used_names:
                n += 1
                rel = f"{project}/{sess_short}/{stem} ({n}).md"
            used_names.add(rel.lower())

            note = render_note(art, subject)
            out = ARTIFACT_DIR / rel
            if not args.dry_run:
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(note, encoding="utf-8")

            stats["notes_written"] += 1
            stats[f"confidence_{art.confidence}"] += 1
            by_project[project] += 1

            if art.secrets:
                stats["notes_with_secret_findings"] += 1
                secret_rows.append({
                    "note": rel,
                    "original_path": art.file_path,
                    "session_id": art.session_id,
                    "findings": [
                        {"kind": s.kind, "line": s.line_number, "excerpt": s.excerpt}
                        for s in art.secrets
                    ],
                })

            manifest.append({
                "note": rel,
                "source": "claude-code",
                "conversation_id": art.session_id,
                "parent_session": parent,
                "session_date": local_date(art.last_seen),
                "original_path": art.file_path,
                "project": art.project_slug,
                "provenance": art.provenance,
                "confidence": art.confidence,
                "content_hash": art.content_hash,
                "content_bytes": len(art.content or ""),
                "base_tags": base_tags(art),
                "sensitive": looks_sensitive(art),
                "session_subject": subject,
                "needs_content_tags": True,
            })

    if not args.dry_run:
        (REPORT_DIR / "manifest_code.json").write_text(
            json.dumps(manifest, indent=2), encoding="utf-8"
        )
        (REPORT_DIR / "secrets_code.json").write_text(
            json.dumps(secret_rows, indent=2), encoding="utf-8"
        )

    print(json.dumps({
        "stats": dict(stats),
        "top_projects": dict(sorted(by_project.items(), key=lambda kv: -kv[1])[:15]),
        "manifest": str(REPORT_DIR / "manifest_code.json"),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
