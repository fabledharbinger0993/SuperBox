#!/usr/bin/env bash
# Sync Rekki prompt profiles from the RekkiClaw repo into rekki/prompts/.
# Run this whenever prompt files change in RekkiClaw.
#
# Usage: ./scripts/import_fabledclaw_assets.sh [/path/to/RekkiClaw]
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC_ROOT="${1:-/Users/cameronkelly/FabledHarbinger/Git Repos/RekkiClaw}"
DEST_PROMPTS="${REPO_ROOT}/rekki/prompts"

log() { echo "[import-prompts] $*"; }

if [[ ! -d "$SRC_ROOT/.pi/prompts" ]]; then
  log "ERROR: prompt source not found: $SRC_ROOT/.pi/prompts"
  exit 1
fi

mkdir -p "$DEST_PROMPTS"
rsync -a --delete "$SRC_ROOT/.pi/prompts/" "$DEST_PROMPTS/"
log "synced $(ls "$DEST_PROMPTS"/*.md 2>/dev/null | wc -l | tr -d ' ') prompt files -> $DEST_PROMPTS"
