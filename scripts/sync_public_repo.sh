#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

PUBLIC_REPO_URL="${PUBLIC_REPO_URL:-https://github.com/fabledharbinger0993/RekitBox.git}"
PUBLIC_REPO_PATH="${PUBLIC_REPO_PATH:-$REPO_ROOT/../RekitBox}"
PUBLIC_REPO_BRANCH="${PUBLIC_REPO_BRANCH:-main}"
SYNC_MESSAGE_PREFIX="${PUBLIC_SYNC_COMMIT_PREFIX:-sync(private->public)}"
EXCLUDES_FILE="${PUBLIC_SYNC_EXCLUDES_FILE:-$REPO_ROOT/.public-sync-excludes}"

log() {
  echo "[public-sync] $*"
}

require_cmd() {
  local cmd="$1"
  if ! command -v "$cmd" >/dev/null 2>&1; then
    log "missing required command: $cmd"
    exit 1
  fi
}

ensure_public_repo() {
  if [[ -d "$PUBLIC_REPO_PATH/.git" ]]; then
    return 0
  fi
  log "cloning public repo to $PUBLIC_REPO_PATH"
  git clone "$PUBLIC_REPO_URL" "$PUBLIC_REPO_PATH"
}

sync_files() {
  local rsync_args
  rsync_args=(
    -a
    --delete
    --exclude ".git/"
    --exclude ".venv/"
    --exclude "venv/"
    --exclude "__pycache__/"
    --exclude "*.pyc"
  )

  if [[ -f "$EXCLUDES_FILE" ]]; then
    rsync_args+=(--exclude-from "$EXCLUDES_FILE")
  fi

  rsync "${rsync_args[@]}" "$REPO_ROOT/" "$PUBLIC_REPO_PATH/"
}

commit_and_push_public() {
  cd "$PUBLIC_REPO_PATH"

  git checkout "$PUBLIC_REPO_BRANCH"
  git pull --ff-only origin "$PUBLIC_REPO_BRANCH"

  if [[ -z "$(git status --porcelain --untracked-files=all)" ]]; then
    log "no public changes to commit"
    return 0
  fi

  local stamp msg
  stamp="$(date '+%Y-%m-%d %H:%M:%S')"
  msg="$SYNC_MESSAGE_PREFIX: $stamp"
  git add -A
  git commit -m "$msg"
  git push origin "$PUBLIC_REPO_BRANCH"
  log "pushed to public repo"
}

run_once() {
  require_cmd git
  require_cmd rsync
  ensure_public_repo
  sync_files
  commit_and_push_public
}

case "${1:-}" in
  init)
    ensure_public_repo
    ;;
  once)
    run_once
    ;;
  *)
    cat <<EOF
Usage: ./scripts/sync_public_repo.sh <init|once>

Environment:
  PUBLIC_REPO_PATH=<path>        (default: ../RekitBox)
  PUBLIC_REPO_URL=<url>          (default: GitHub public repo)
  PUBLIC_REPO_BRANCH=<branch>    (default: main)
  PUBLIC_SYNC_EXCLUDES_FILE=<path>
EOF
    exit 2
    ;;
esac
