# Claude harvest

Recovers files Claude authored across Claude Code, Cowork, Desktop and
claude.ai, and renders session transcripts, into Obsidian-ready markdown.

**Only the tooling lives here. Harvested output must never be committed.**
It contains real financial records, attorney-consultation material, personal
names and email addresses, and at least one live service token. Output stages
to `~/Desktop/claude-harvest-staging/`, deliberately outside both this repo and
the Syncthing vault root.

## Modules

| File | Does |
| --- | --- |
| `harvest_core.py` | Parses session JSONL, resolves each file's final content, scans for credentials, sanitizes filenames for Windows |
| `harvest_all.py` | Batch: every session to staged markdown notes plus a manifest |
| `render_transcripts.py` | Renders sessions to readable transcripts — turns verbatim, tool calls collapsed to one line each |

```bash
python3 harvest_all.py          # artifacts
python3 render_transcripts.py   # transcripts
```

## Why content is wrapped in a fenced block

Every artifact becomes a `.md` note: YAML frontmatter, then the original body
inside a fence tagged with its language. Frontmatter cannot live inside a `.py`
or `.ts` file without breaking it, and Obsidian only parses frontmatter in
markdown. The fence is sized longer than the longest backtick run in the body,
or the block would close early and leak the rest of the file into the note.

## Content confidence is not decoration

Each note records how its body was obtained:

- `full` — a `Write` payload from the transcript, or a verified snapshot
- `reconstructed` — edits replayed onto a write, or the file read from disk as
  it exists now, which may have changed since the session
- `fragments-only` — the path was only ever `Edit`ed and no full body survives

Fragments-only notes carry **no content**. Concatenating `new_string` values
into something file-shaped produces a plausible file that never existed.

## Two failures worth not repeating

**file-history cannot be trusted by position.** It is content-addressed with no
path index. An earlier version assumed a session's lone snapshot family
belonged to whatever file was being resolved; it served a research document as
the body of `INSTALL.md`, labelled `full`. `_snapshot_matches_edits` now
requires an edit's `old_string` to actually appear in the snapshot before it is
accepted.

**Scan transcripts, not just artifacts.** A token pasted into conversation
never becomes a file, so an artifact-only scanner cannot see it. That is
exactly how a live Doppler token reached the staging set.

## Machine-generated sessions

Two classes of session are a program prompting itself, and are excluded or
flagged rather than imported as conversations: `claude-mem` observer sessions
(~517, two turns each, zero artifacts) and research-harness eval probes (~533,
97% under three turns, 13 artifacts between them).

## Windows safety

The vault syncs to a Windows PC. Filenames are stripped of `<>:"/\|?*`, trailing
dots and spaces; reserved basenames (`CON`, `AUX`, `COM1`…) are suffixed; and
case-insensitive collisions are resolved, because `README.md` beside
`Readme.md` is fine on APFS and a silent sync failure on Windows. A leading dot
becomes `dot-`, so `.gitignore` stays distinct from `gitignore` and remains
visible in Obsidian.
