# Git branch guard

Two machines share this repo. Before this convention existed they committed to
the same branch and raced each other. Each machine now has its own permanent
lane, and `main` is reached only through a pull request.

| Machine | Lane | Topic branches |
| --- | --- | --- |
| PC | `pc` | `pc-<topic>` |
| Mac | `mac` | `mac-<topic>` |

`main` is protected on GitHub: no direct pushes from either machine, including
the repo owner. Changes land via PR only.

## Install

Run once per clone, on each machine:

```bash
./hooks/git-branch-guard/install.sh mac    # on the Mac
./hooks/git-branch-guard/install.sh pc     # on the PC
```

This copies `pre-commit` and `pre-push` into `.git/hooks/`, substituting the
lane name. Any existing hook is backed up alongside it first.

Re-run after cloning. `.git/hooks/` is not tracked by git, so the guard does
not travel with the repository — that is why these templates are versioned
here and the install step is manual.

## Topic branches use a hyphen, not a slash

`mac/some-fix` cannot be created:

```
fatal: cannot lock ref 'refs/heads/mac/some-fix':
'refs/heads/mac' exists; cannot create 'refs/heads/mac/some-fix'
```

Git stores `refs/heads/mac` as a file. A nested ref would require it to be a
directory, and it cannot be both. Use `mac-some-fix`.

## Everyday flow

```bash
git checkout mac
# work, commit
git push origin mac
```

To reach `main`:

```bash
gh pr create --repo mkant18/obsidian-second-brain --base main --head mac \
  --title "..." --body "..."
```

Do not self-merge without sign-off.

Pull the other machine's merged work back in periodically, especially right
after any PR lands:

```bash
git fetch origin && git merge origin/main
```

## What the guard does not cover

`pre-commit` and `pre-push` are local and bypassable with `--no-verify`. They
catch the accident, not a determined override. GitHub branch protection is the
real enforcement for `main`; these hooks exist to stop a wrong-branch commit
before it becomes a push, and to stop either machine pushing into the other's
lane — something branch protection does not cover.

## The vault is not in this repo

The Obsidian vault itself (the notes, synced at `~/Desktop/OBSIDIAN`) is kept
in sync between machines by **Syncthing only**. It must never be `git init`-ed,
committed, or pushed. That was attempted once and deliberately reversed. If a
`.git` directory appears inside the vault, treat it as a problem to raise
rather than something intentional.
