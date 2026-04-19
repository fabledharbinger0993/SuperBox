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
cd "$REPO_ROOT"

if ! command -v gh >/dev/null 2>&1; then
  echo "GitHub CLI (gh) is required. Install it first."
  exit 1
fi

if [[ -n "$(git status --porcelain --untracked-files=all)" ]]; then
  echo "Working tree is not clean. Commit or stash changes before releasing."
  exit 1
fi

git fetch --tags origin main

if git rev-parse "$TAG" >/dev/null 2>&1; then
  echo "Tag already exists: $TAG"
  exit 1
fi

if [[ -n "$NOTES_FILE" && ! -f "$NOTES_FILE" ]]; then
  echo "Notes file does not exist: $NOTES_FILE"
  exit 1
fi

if [[ -n "$NOTES_FILE" ]]; then
  gh release create "$TAG" --target main --title "$TAG" --notes-file "$NOTES_FILE"
else
  gh release create "$TAG" --target main --title "$TAG" --generate-notes
fi

echo "Release $TAG created."
echo "RekitBox.zip will be attached automatically by .github/workflows/release-zip.yml."
