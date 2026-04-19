#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

if [[ ! -d .git ]]; then
  echo "[autosync] not a git repository: $REPO_ROOT" >&2
  exit 1
fi

INTERVAL="${AUTOSYNC_INTERVAL:-5}"
COMMIT_PREFIX="${AUTOSYNC_COMMIT_PREFIX:-autosync}"

is_git_busy() {
  [[ -f .git/MERGE_HEAD || -d .git/rebase-merge || -d .git/rebase-apply || -f .git/CHERRY_PICK_HEAD || -f .git/REVERT_HEAD || -f .git/BISECT_LOG ]]
}

push_with_upstream_if_needed() {
  local branch
  branch="$(git rev-parse --abbrev-ref HEAD)"

  if [[ "$branch" == "HEAD" ]]; then
    echo "[autosync] detached HEAD - skipping push"
    return 0
  fi

  if git rev-parse --abbrev-ref --symbolic-full-name "@{u}" >/dev/null 2>&1; then
    git push
  else
    git push -u origin "$branch"
  fi
}

commit_and_push_if_dirty() {
  local status_output
  status_output="$(git status --porcelain --untracked-files=all)"

  if [[ -z "$status_output" ]]; then
    return 0
  fi

  # Ignore transient files that should never trigger autosync commits.
  if [[ -n "$status_output" ]]; then
    status_output="$(printf '%s\n' "$status_output" | grep -Ev '(^.. \.DS_Store$|^.. .*__pycache__/|^.. .*\.pyc$)' || true)"
  fi

  if [[ -z "$status_output" ]]; then
    return 0
  fi

  git add -A
  if git diff --cached --quiet; then
    return 0
  fi

  local stamp msg
  stamp="$(date '+%Y-%m-%d %H:%M:%S')"
  msg="$COMMIT_PREFIX: $stamp"

  echo "[autosync] commit: $msg"
  git commit -m "$msg"
  push_with_upstream_if_needed
  echo "[autosync] pushed"
}

watch_loop() {
  echo "[autosync] watching $REPO_ROOT (interval=${INTERVAL}s)"
  while true; do
    if is_git_busy; then
      echo "[autosync] git busy (merge/rebase/cherry-pick) - skipping cycle"
    else
      if ! commit_and_push_if_dirty; then
        echo "[autosync] cycle failed - will retry"
      fi
    fi
    sleep "$INTERVAL"
  done
}

case "${1:-watch}" in
  watch)
    watch_loop
    ;;
  once)
    if is_git_busy; then
      echo "[autosync] git busy (merge/rebase/cherry-pick) - skipping"
      exit 0
    fi
    commit_and_push_if_dirty
    ;;
  *)
    echo "Usage: $0 [watch|once]"
    exit 2
    ;;
esac
