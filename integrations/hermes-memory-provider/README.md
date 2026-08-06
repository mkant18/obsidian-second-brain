# Obsidian vault as Hermes Agent memory

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) memory provider that uses an Obsidian vault of AI-first markdown notes as the agent's long-term memory, replacing the default vector store.

This is the "memory backend" half of [Issue #60](https://github.com/eugeniughelbur/obsidian-second-brain/issues/60): instead of an opaque embeddings store, Hermes remembers in human-readable, self-rewriting, wikilinked notes - the LLM-Wiki pattern this project is built on.

## Status

v0 scaffold. The core path is implemented in pure stdlib (no extra pip deps), but it has **not** yet been run inside a live Hermes runtime against a model. Treat it as a working starting point, not a finished provider. See "Testing checklist" below.

## What it does

It implements Hermes's `MemoryProvider` interface (`agent/memory_provider.py`). Each method maps to a vault operation:

| Hermes lifecycle method | What this provider does |
|---|---|
| `system_prompt_block()` | Injects the vault's standing memory (`MEMORY.md` / `CRITICAL_FACTS.md` / `SOUL.md`) at session start |
| `prefetch(query)` | Bounded keyword search across vault notes, returns the top matches as recall context before each turn |
| `sync_turn(user, asst)` | Appends the completed turn to a per-session transcript note under `Hermes/sessions/` |
| `get_tool_schemas()` / `handle_tool_call()` | Exposes three tools: `obsidian_save_note`, `obsidian_search`, `obsidian_read_note` |
| `on_session_end()` | Stamps the session note as closed |

Writes follow the AI-first rule (`references/ai-first-rules.md`): frontmatter, a `## For future Claude` preamble, `type`/`date`/`tags`/`ai-first: true`, and a `source: hermes-agent` marker so vault notes written by Hermes are distinguishable from those written by Claude.

## Install

1. Copy this folder into a Hermes checkout as `plugins/memory/obsidian/`:
   ```bash
   cp -R integrations/hermes-memory-provider /path/to/hermes-agent/plugins/memory/obsidian
   ```
2. Point it at your vault:
   ```bash
   export OBSIDIAN_VAULT_PATH="/path/to/your/vault"
   ```
3. Activate it:
   ```bash
   hermes config set memory.provider obsidian
   ```

Hermes discovers the plugin by scanning `plugins/memory/*/`, calls `register(ctx)`, and activates it when `is_available()` returns true (vault path set and the directory exists).

## Where it writes

- `Hermes/sessions/<session>.md` - one transcript note per session (turn-by-turn).
- `Hermes/notes/<date> - <title>.md` - durable notes saved via `obsidian_save_note`.

Both live under a top-level `Hermes/` folder so agent-written memory stays separate from your hand-authored vault, while still being fully searchable and human-readable.

## Testing checklist (before calling this done)

- [ ] Drop into a real Hermes checkout, run `hermes memory setup` / `hermes config set memory.provider obsidian`, confirm it activates.
- [ ] Confirm `system_prompt_block()` content appears in the assembled system prompt.
- [ ] Have the agent call `obsidian_save_note`, then `obsidian_search` for it in a later turn - verify recall.
- [ ] Confirm `Hermes/sessions/<id>.md` accumulates turns and is valid AI-first markdown.
- [ ] Measure `prefetch()` latency on a realistic vault - it runs before every turn. If a large vault is slow, add an index (e.g. a cached inverted index or SQLite FTS) rather than the current linear scan.
- [ ] Verify `register(ctx)` and `plugin.yaml` against the current Hermes plugin loader (the API can churn).

## Known limitations

- Search is a bounded linear scan (`_MAX_FILES_SCANNED`), good for small/medium vaults; large vaults need an index.
- `sync_turn` truncates each side of a turn to keep notes readable.
- No deduplication or reconciliation yet - that is where the full obsidian-second-brain command logic (`/obsidian-save`, `/obsidian-reconcile`) would eventually plug in.
