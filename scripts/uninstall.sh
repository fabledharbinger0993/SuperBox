#!/bin/bash
# shellcheck shell=bash
# uninstall.sh
#
# Removes the FableGear native Dock launcher and optionally the full repo.
# Called by FableGear Uninstall.app (built by setup_dock_launcher.sh).
# Also safe to run standalone: bash ~/FableGear/FableGear/scripts/uninstall.sh
#
# Exits 0 in all cases — failures are non-fatal.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
APP_PATH="$HOME/Applications/FableGear.app"
UNINSTALL_APP_PATH="$HOME/Applications/FableGear Uninstall.app"
DOCK_SENTINEL="$REPO_ROOT/.dock_launcher_offered"

# ── Step 1: choose scope ──────────────────────────────────────────────────
CHOICE=$(osascript \
    -e 'button returned of (display dialog "What would you like to remove?

Dock Icon Only
  Removes FableGear from your Dock and deletes ~/Applications/FableGear.app.
  Your music library and the FableGear repo folder are untouched.

Full Uninstall
  Removes everything above AND deletes the FableGear repo folder (~'$HOME/FableGear/FableGear').
  Your Rekordbox library is never touched." buttons {"Cancel", "Dock Icon Only", "Full Uninstall"} default button "Dock Icon Only" with icon caution with title "FableGear Uninstall")' \
    2>/dev/null || echo "Cancel")

case "$CHOICE" in
    "Cancel"|"")
        exit 0
        ;;
    "Dock Icon Only")
        REMOVE_REPO=false
        ;;
    "Full Uninstall")
        # Second confirmation for destructive action
        CONFIRM=$(osascript \
            -e "button returned of (display dialog \"Delete the FableGear repo folder?\n\n$REPO_ROOT\n\nThis cannot be undone.\" buttons {\"Cancel\", \"Delete It\"} default button \"Cancel\" with icon stop with title \"Are you sure?\")" \
            2>/dev/null || echo "Cancel")
        if [[ "$CONFIRM" != "Delete It" ]]; then
            exit 0
        fi
        REMOVE_REPO=true
        ;;
esac

# ── Remove from Dock plist ────────────────────────────────────────────────
python3 - "$APP_PATH" <<'PYUNPIN'
import sys, os, plistlib, subprocess

app_path = sys.argv[1]
dock_plist = os.path.expanduser("~/Library/Preferences/com.apple.dock.plist")

try:
    with open(dock_plist, "rb") as f:
        dock = plistlib.load(f)
except Exception:
    sys.exit(0)

apps = dock.get("persistent-apps", [])
filtered = [
    item for item in apps
    if item.get("tile-data", {}).get("file-data", {}).get("_CFURLString", "") != app_path
]

if len(filtered) < len(apps):
    dock["persistent-apps"] = filtered
    with open(dock_plist, "wb") as f:
        plistlib.dump(dock, f)
    subprocess.run(["killall", "Dock"], capture_output=True)
    print("Removed from Dock.")
else:
    print("Not found in Dock — skipping.")
PYUNPIN

# ── Remove .app bundles from ~/Applications/ ──────────────────────────────
rm -rf "$APP_PATH" 2>/dev/null || true
rm -rf "$UNINSTALL_APP_PATH" 2>/dev/null || true
rm -f "$DOCK_SENTINEL" 2>/dev/null || true

# ── Remove repo (full uninstall only) ────────────────────────────────────
if [[ "$REMOVE_REPO" == true ]]; then
    rm -rf "$REPO_ROOT"
    osascript -e 'display notification "FableGear has been fully removed." with title "FableGear Uninstall"' 2>/dev/null || true
    # We can't show a terminal message since the script dir is gone — dialog only
    exit 0
fi

osascript -e 'display notification "FableGear Dock launcher removed. Your library is untouched." with title "FableGear Uninstall"' 2>/dev/null || true
echo "Done. Repo kept at: $REPO_ROOT"
echo "Re-run setup_dock_launcher.sh any time to restore the Dock icon."
