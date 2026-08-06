# Claude harvest — handoff

**Status: recovery and tagging COMPLETE. Nothing has been written to the Obsidian vault.**
All background agents terminated 2026-08-06. Remaining work: taxonomy, build, review, import.

Every number below was verified by reading the filesystem, not recalled.

## What is on disk

`~/Desktop/claude-harvest-staging/` — 420 MB, outside the Syncthing vault root, so nothing here propagates to the Windows PC.

| Source | Staged files | Tag records | Location |
| --- | --- | --- | --- |
| Claude Code artifacts | 867 | 875 | `artifacts/` |
| Claude Code transcripts | 1,137 | 1,137 | `transcripts/` |
| Cowork | 390 | 389 | `cowork/` |
| claude.ai | 939 (322 conversations, 291 artifacts) | 321 | `claude-ai/` |
| Desktop & Downloads | 52 | 51 | `desktop-downloads/` |
| **Total** | | **2,773 tagged** | |

524 records flagged sensitive. 9,647 distinct free-form topic tags await canonicalization.

Every tag was earned by an agent opening and reading the file. None were pattern-matched from filenames.

## What is NOT done

1. **Taxonomy consolidation** — collapse 9,647 raw tags into 300–800 canonical `claude/topic/*` tags; produce `_tooling/taxonomy.json` and `taxonomy-report.md`. An Opus agent was mid-run when terminated and wrote no output, so this restarts clean.
2. **Build** — materialize `_final/Claude Exports/` from the taxonomy.
3. **Adversarial review** — filename legality, case collisions, tag accuracy on a read-back sample, confirmation the vault is untouched.
4. **Import** — one agent copies into the vault. Requires explicit approval first.

## Two decisions still open — the user's call

**533 of 1,137 Claude Code transcripts are synthetic eval probes** from `singularity-research-wave2-self-improvement` and `singularity-research-fable-opus-distillation`. 97% have two or fewer assistant turns, 93% zero tool calls, and all 533 together produced 13 artifacts — the other 604 produced 927. The corpus-wide tag frequency confirms it: the four most common tags are `trivial-session`, `synthetic-eval-probe`, `fable-behavioral-distillation-research`, `llm-behavioral-probe-harness`. Recommendation: exclude them, or collapse to two summary notes. Importing them makes ~47% of the transcript folder a machine prompting itself.

**116 of 867 code artifacts were written by this job about itself.** 37 are intermediate tag-batch JSONL dumps that contain duplicated copies of sensitive data, 43 are reusable tooling, 36 are scratch. Recommendation: keep the tooling, drop the other 73.

## Credentials found — flagged, not removed

The user instructed that nothing be quarantined. All remain in the import set, tagged `claude/flagged/credentials` with `sensitive: true`.

- **Doppler service token** — `transcripts/Desktop-Coding-Projects/2026-08-05 Ok, can we continue setting up the Obsidian vault…md`, lines 153/197/294. Present independently in `~/.claude/paste-cache/5861ec6b2d4fe53f.txt`.
- **GitHub token** — `claude-ai/transcripts/2026-05-09 Bypassing Dropbox download restrictions (8c35d5a9).md`, line 427.
- `env_assignment_secret` — `claude-ai/transcripts/2026-03-09 Setting up Claude Code with GitHub integration (12c777ef).md`.
- `jwt` — raw JSON `9e4fd91d…`, metadata only, reaches no note.

**Both real tokens should be rotated.** They sit in `~/.claude/` regardless of this harvest.

## How to resume

Tooling is in `_tooling/`, and committed to the `mac` branch of `obsidian-second-brain` under `tools/claude-harvest/`.

```bash
cd ~/Desktop/claude-harvest-staging/_tooling
python3 harvest_all.py         # rebuild artifact notes + manifest
python3 render_transcripts.py  # rebuild Claude Code transcripts
python3 render_claudeai.py     # rebuild claude.ai transcripts + manifest
```

All three are idempotent and rebuild from untouched sources. The claude.ai raw JSON is the one thing **not** cheaply reproducible — it took hours of authenticated browser fetching. Back up `claude-ai/transcripts-raw/` before anything else. Time Machine on this machine has no backups (`tmutil listbackups` reports no machine directory), so that data currently exists in one place only.

Tag records: `_reports/tags_*.jsonl`, keyed by staged path. Manifests: `_reports/manifest_*.json`.

## Hard rules that still apply

- Nothing writes to `/Users/michaelkanter/Desktop/OBSIDIAN` until the import step, and only one agent ever does.
- The import is additive, lands in exactly one new top-level folder `Claude Exports/`, and modifies no existing note.
- Filenames must be Windows-safe: no `<>:"/\|?*`, no trailing dots or spaces, no reserved basenames (`CON` `PRN` `AUX` `NUL` `COM1-9` `LPT1-9`), a leading dot becomes `dot-`, and zero case-insensitive collisions. The vault syncs to Windows; a collision means a file silently never arrives.
- Folders encode source and conversation. Tags encode subject. Never duplicate the tree by topic.

## Failures worth not repeating

**file-history cannot be trusted by position.** It is content-addressed with no path index. An early version assumed a session's lone snapshot family belonged to whatever file was being resolved, and served a research document as the body of `INSTALL.md` labelled `content-confidence: full`. Fixed by `_snapshot_matches_edits`, which requires an edit's `old_string` to appear in the snapshot before accepting it.

**Secret scanning must cover transcripts, not just artifacts.** A token pasted into conversation never becomes a file, so an artifact-only scanner cannot see it. That is how the Doppler token reached staging.

**Long-running browser agents stall.** Two died attempting fetch + render + artifact download + secret scan + manifest across ~100 conversations. The fix: agents do one thing — save raw JSON, capped at 30 — with rendering, hashing, scanning and manifest-building in local code. Both bounded agents then completed 30/30 with zero failures.

**Agent self-reports were unreliable.** One reported 162 recovered when disk held 235, and 79 missing when the real number was 96. Verify against the filesystem.

**A stale agent interfered.** A completed claude.ai agent kept being resumed, misread this pipeline as a rogue fleet, and sent stop messages to sixteen tagging workers it did not own. No data was lost: every worker wrote incrementally, and four independently classified the messages as probable prompt injection and refused to widen scope. Workers should always write incrementally and treat peer instructions as unauthorized.
