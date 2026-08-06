# Branch audit - 2026-08-06

Snapshot of every remote branch, what was decided, and why.

## Base branches - untouched

`main`, `pc`, `mac` are the working baselines and were explicitly excluded from
this audit.

## Open work

| branch | ahead of main | behind | PR | state |
|---|---|---|---|---|
| `fixes/audit-backlog-2026-08-05` | 16 | 0 | #1 | `MERGEABLE` / `UNSTABLE` |
| `hermes-memory-provider` | 1 | 227 | #2 | `MERGEABLE` / `CLEAN` |

Nothing here was stale in the sense of "already merged" - both branches carry
commits not reachable from `main`, so neither was deleted.

### #1 - `fixes/audit-backlog-2026-08-05`

16 commits ahead, **0 behind**. Merges clean; no rebase needed. The blocker is
not the diff, it is CI:

- Smoke tests (ubuntu-latest) - **pass**
- Smoke tests (windows-latest) - **fail**

Since the branch's own subject is "Windows compat, MCP data-safety, and
token-cost efficiency", a failing Windows smoke test is the one signal that
should not be waved through. **Decision: do not merge until that job is green.**
Merging a Windows-compatibility branch over a red Windows check would defeat the
point of having the check.

Recommended merge once green: **merge commit**, not squash. The 16 commits are
individually scoped fixes across 49 files, and collapsing them loses the ability
to bisect a specific fix later.

### #2 - `hermes-memory-provider`

1 commit ahead, 227 behind, from 2026-06-06. Reported `CLEAN` despite the age
because the change is **purely additive** - three new files under a new
directory, 470 insertions, zero modifications to existing code:

- `integrations/hermes-memory-provider/__init__.py`
- `integrations/hermes-memory-provider/plugin.yaml`

Being 227 commits behind does not matter when nothing it touches has moved.
**Decision: squash merge.** It is a single v0 scaffold (Issue #60); one commit
on `main` is the honest representation, and there is no per-commit history worth
preserving.

Caveat carried into the merge: the scaffold has not been exercised against
current `main`. It is being merged as reviewable scaffolding, not as shipped
functionality.

## Method

Staleness was determined by commit reachability, not by date:

```bash
git rev-list --count origin/main..origin/<branch>   # 0 = fully merged, safe to delete
git rev-list --count origin/<branch>..origin/main   # how far behind
```

A branch that is far behind but 0 ahead is deletable. A branch that is far
behind but ahead by even 1 commit is not - it holds work that exists nowhere
else.
