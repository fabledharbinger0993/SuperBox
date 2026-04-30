#!/usr/bin/env bash
# make_local_launcher.sh
#
# Creates FableGear.command at the repo root — a portable, double-clickable
# launcher that opens FableGear directly from the DJMT drive without any
# Automator app, security approval, or Gatekeeper block.
#
# Run this once after cloning/moving the repo, or any time the icon is lost
# (e.g. after drive reformatting). The .command file is gitignored and lives
# only on the local drive.
#
# Usage:
#   bash packaging/make_local_launcher.sh
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LAUNCHER="$REPO_ROOT/FableGear.command"
ICON_PNG="$REPO_ROOT/static/icon-logo-fablegear.png"

# ── 1. Write the launcher script ──────────────────────────────────────────
cat > "$LAUNCHER" << 'SCRIPT'
#!/bin/bash
# FableGear — portable local launcher
# Double-click in Finder to launch FableGear from this drive.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
bash "$SCRIPT_DIR/launch_local.sh"
SCRIPT

chmod +x "$LAUNCHER"
echo "✓ Created $LAUNCHER"

# ── 2. Apply custom icon via NSWorkspace (loads PNG directly, no icns needed)
if [ -f "$ICON_PNG" ]; then
  osascript << APPLESCRIPT
use framework "AppKit"
set iconImage to current application's NSImage's alloc()'s initWithContentsOfFile:"$ICON_PNG"
current application's NSWorkspace's sharedWorkspace()'s setIcon:iconImage forFile:"$LAUNCHER" options:0
APPLESCRIPT
  echo "✓ Custom icon applied"
else
  echo "⚠  Icon not found at $ICON_PNG — skipping"
fi

# ── 3. Hide the .command extension so Finder displays just "FableGear" ────
if osascript -e "tell application \"Finder\" to set extension hidden of (POSIX file \"$LAUNCHER\" as alias) to true" 2>/dev/null; then
  echo "✓ Extension hidden — Finder will show 'FableGear'"
else
  echo "  Note: couldn't hide extension via Finder. Right-click → Get Info → tick 'Hide extension' to do it manually."
fi

echo ""
echo "  Ready. Double-click FableGear in Finder (at the repo root) to launch."
