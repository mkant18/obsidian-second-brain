#!/usr/bin/env bash
# Install this machine's git branch guard.
#
#   ./hooks/git-branch-guard/install.sh mac    # on the Mac
#   ./hooks/git-branch-guard/install.sh pc     # on the PC
#
# Git hooks live in .git/hooks/, which is never tracked, so they do not
# survive a clone. This script re-installs them from the versioned templates.
set -euo pipefail

MACHINE_BRANCH="${1:-}"
if [[ -z "$MACHINE_BRANCH" ]]; then
  echo "usage: $0 <machine-branch>    e.g. $0 mac" >&2
  exit 2
fi

repo_root=$(git rev-parse --show-toplevel)
src="$repo_root/hooks/git-branch-guard"
dest="$repo_root/.git/hooks"

if [[ ! -d "$dest" ]]; then
  echo "error: $dest does not exist — is this a git repo?" >&2
  exit 1
fi

for hook in pre-commit pre-push; do
  if [[ -e "$dest/$hook" ]]; then
    cp "$dest/$hook" "$dest/$hook.backup-$(date +%Y%m%d%H%M%S)"
    echo "backed up existing $hook"
  fi
  sed "s/__MACHINE_BRANCH__/$MACHINE_BRANCH/g" "$src/$hook" > "$dest/$hook"
  chmod +x "$dest/$hook"
  echo "installed $hook (branch lane: $MACHINE_BRANCH)"
done

current=$(git symbolic-ref --short HEAD 2>/dev/null || echo "?")
if [[ "$current" != "$MACHINE_BRANCH" && "$current" != "$MACHINE_BRANCH"-* ]]; then
  echo
  echo "note: you are on '$current', which the guard now blocks."
  echo "      run: git checkout $MACHINE_BRANCH"
fi
