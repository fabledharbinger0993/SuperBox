#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC_ROOT="${1:-/Users/cameronkelly/FabledHarbinger/Git Repos/Harbinger/FabledClaw-main}"
DEST_ROOT="${REPO_ROOT}/agent_assets/fabledclaw_snapshot"

IMPORT_DIRS=(
  ".pi"
  ".vscode"
  "auto-reply"
  "context-engine"
  "bindings"
  "assets/chrome-extension"
  "channels"
  "commands"
  "copilot-proxy"
  "cron"
  "diffs"
  "flows"
  "helpers"
  "ollama"
  "media"
  "interactive"
  "process"
  "routing"
  "sglang"
  "signal"
  "skills"
  "slack"
  "zai"
)

IMPORT_FILES=(
  "apps/macos/README.md"
  "apps/macos/Package.swift"
  "apps/macos/Package.resolved"
  ".env.example"
  "entry.respawn.test.ts"
  "entry.version-fast-path.test.ts"
  "library.test.ts"
  "logging.ts"
  "openclaw.podman.env"
  "pnpm-workspace.yaml"
  "channel-web.ts"
)

log() {
  echo "[fabledclaw-import] $*"
}

require_cmd() {
  local cmd="$1"
  if ! command -v "$cmd" >/dev/null 2>&1; then
    log "missing required command: $cmd"
    exit 1
  fi
}

copy_tree() {
  local rel="$1"
  if [[ -d "$SRC_ROOT/$rel" ]]; then
    mkdir -p "$DEST_ROOT/$rel"
    rsync -a --delete "$SRC_ROOT/$rel/" "$DEST_ROOT/$rel/"
    log "copied $rel"
  else
    log "skip missing $rel"
  fi
}

copy_file() {
  local rel="$1"
  if [[ -f "$SRC_ROOT/$rel" ]]; then
    mkdir -p "$(dirname "$DEST_ROOT/$rel")"
    cp "$SRC_ROOT/$rel" "$DEST_ROOT/$rel"
    log "copied $rel"
  else
    log "skip missing $rel"
  fi
}

write_manifest() {
  local manifest
  manifest="$DEST_ROOT/MANIFEST.txt"

  {
    echo "FabledClaw Snapshot Manifest"
    echo "source=$SRC_ROOT"
    echo "generated_at=$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
    echo
    echo "Top-level directories imported:"
    for d in "${IMPORT_DIRS[@]}"; do
      if [[ -e "$DEST_ROOT/$d" ]]; then
        echo "- $d"
      fi
    done
    if [[ -e "$DEST_ROOT/apps/macos" ]]; then
      echo "- apps/macos"
    fi
    echo
    echo "File count by imported directory:"
    for d in "${IMPORT_DIRS[@]}"; do
      if [[ -d "$DEST_ROOT/$d" ]]; then
        count="$(find "$DEST_ROOT/$d" -type f | wc -l | tr -d ' ')"
        echo "- $d: $count"
      fi
    done
    if [[ -d "$DEST_ROOT/apps/macos" ]]; then
      count="$(find "$DEST_ROOT/apps/macos" -type f | wc -l | tr -d ' ')"
      echo "- apps/macos: $count"
    fi
    echo
    echo "Standalone files imported:"
    for f in "${IMPORT_FILES[@]}"; do
      if [[ -f "$DEST_ROOT/$f" ]]; then
        echo "- $f"
      fi
    done
  } > "$manifest"

  log "manifest: $manifest"
}

main() {
  require_cmd rsync

  if [[ ! -d "$SRC_ROOT" ]]; then
    log "source repo not found: $SRC_ROOT"
    exit 1
  fi

  mkdir -p "$DEST_ROOT"

  for dir in "${IMPORT_DIRS[@]}"; do
    copy_tree "$dir"
  done

  # macOS app is very large; start with metadata and primary package entry points.
  for file in "${IMPORT_FILES[@]}"; do
    copy_file "$file"
  done

  write_manifest

  log "snapshot ready: $DEST_ROOT"
}

main "$@"
