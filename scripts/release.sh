#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "Usage: ./scripts/release.sh <tag> [notes-file]"
  echo "Example: ./scripts/release.sh v2.0.13 .github/release-notes.md"
  exit 2
fi

TAG="$1"
NOTES_FILE="${2:-}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RELEASE_BRANCH="${RELEASE_BRANCH:-main}"
RELEASE_REMOTE="${RELEASE_REMOTE:-origin}"
RELEASE_WAIT_SECONDS="${RELEASE_WAIT_SECONDS:-300}"
RELEASE_WAIT_INTERVAL="${RELEASE_WAIT_INTERVAL:-5}"
ZIP_ASSET_NAME="${RELEASE_ZIP_ASSET:-FableGear.zip}"
LAUNCHER_ZIP_ASSET_NAME="${RELEASE_LAUNCHER_ZIP_ASSET:-FableGearLauncher.zip}"

# VS Code tasks or non-interactive shells may not export SHELL, which causes
# noisy GitHub CLI tip messages about unknown shell targets.
if [[ -z "${SHELL:-}" ]]; then
  export SHELL="/bin/zsh"
fi

cd "$REPO_ROOT"

log() {
  echo "[release] $*"
}

fail() {
  log "$*"
  exit 1
}

if ! command -v gh >/dev/null 2>&1; then
  fail "GitHub CLI (gh) is required. Install it first."
fi

if [[ -n "$(git status --porcelain --untracked-files=all)" ]]; then
  fail "Working tree is not clean. Commit or stash changes before releasing."
fi

if ! [[ "$TAG" =~ ^v[0-9]+\.[0-9]+\.[0-9]+([-.][0-9A-Za-z.-]+)?$ ]]; then
  fail "Tag '$TAG' must look like vX.Y.Z (optional suffix allowed)."
fi

if ! gh auth status >/dev/null 2>&1; then
  fail "GitHub CLI is not authenticated. Run: SHELL=/bin/zsh gh auth login --hostname github.com --git-protocol https --web"
fi

if [[ ! -f ".github/workflows/release-zip.yml" ]]; then
  fail "Missing .github/workflows/release-zip.yml (required for automatic $ZIP_ASSET_NAME attachment)."
fi

current_branch="$(git rev-parse --abbrev-ref HEAD)"
if [[ "$current_branch" != "$RELEASE_BRANCH" ]]; then
  fail "Release must be run from '$RELEASE_BRANCH' (current: '$current_branch')."
fi

git fetch --tags "$RELEASE_REMOTE" "$RELEASE_BRANCH"

local_head="$(git rev-parse HEAD)"
remote_head="$(git rev-parse "$RELEASE_REMOTE/$RELEASE_BRANCH")"
if [[ "$local_head" != "$remote_head" ]]; then
  fail "Local $RELEASE_BRANCH is not in sync with $RELEASE_REMOTE/$RELEASE_BRANCH. Pull/rebase/push first."
fi

if git rev-parse "$TAG" >/dev/null 2>&1; then
  fail "Tag already exists locally: $TAG"
fi

if git ls-remote --tags "$RELEASE_REMOTE" "refs/tags/$TAG" | grep -q .; then
  fail "Tag already exists on remote: $TAG"
fi

if [[ -n "$NOTES_FILE" && ! -f "$NOTES_FILE" ]]; then
  fail "Notes file does not exist: $NOTES_FILE"
fi

if [[ -n "$NOTES_FILE" ]]; then
  gh release create "$TAG" --target "$RELEASE_BRANCH" --title "$TAG" --notes-file "$NOTES_FILE"
else
  gh release create "$TAG" --target "$RELEASE_BRANCH" --title "$TAG" --generate-notes
fi

log "Release $TAG created."
log "Waiting for $ZIP_ASSET_NAME attachment from workflow (timeout ${RELEASE_WAIT_SECONDS}s)..."

start_ts="$(date +%s)"
while true; do
  if gh release view "$TAG" --json assets --jq '.assets[].name' | grep -Fxq "$ZIP_ASSET_NAME"; then
    log "$ZIP_ASSET_NAME is attached."
    break
  fi

  now_ts="$(date +%s)"
  elapsed="$((now_ts - start_ts))"
  if [[ "$elapsed" -ge "$RELEASE_WAIT_SECONDS" ]]; then
    fail "Timed out waiting for $ZIP_ASSET_NAME. Check workflow: .github/workflows/release-zip.yml"
  fi

  sleep "$RELEASE_WAIT_INTERVAL"
done

log "Waiting for $LAUNCHER_ZIP_ASSET_NAME attachment from workflow (timeout ${RELEASE_WAIT_SECONDS}s)..."

start_ts="$(date +%s)"
while true; do
  if gh release view "$TAG" --json assets --jq '.assets[].name' | grep -Fxq "$LAUNCHER_ZIP_ASSET_NAME"; then
    log "$LAUNCHER_ZIP_ASSET_NAME is attached."
    break
  fi

  now_ts="$(date +%s)"
  elapsed="$((now_ts - start_ts))"
  if [[ "$elapsed" -ge "$RELEASE_WAIT_SECONDS" ]]; then
    fail "Timed out waiting for $LAUNCHER_ZIP_ASSET_NAME. Check workflow: .github/workflows/release-launcher-zip.yml"
  fi

  sleep "$RELEASE_WAIT_INTERVAL"
done

log "Release is fully ready: $TAG"
