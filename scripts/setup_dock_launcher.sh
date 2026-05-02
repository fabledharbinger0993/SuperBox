#!/bin/bash
# shellcheck shell=bash
# setup_dock_launcher.sh
#
# Builds a native FableGear.app in ~/Applications/ from source (local osacompile),
# applies the FableGear icon, and optionally pins it to the Dock.
#
# Run by launch.sh on first boot (silent skip if already offered).
# Run standalone anytime to rebuild or re-add:
#   bash scripts/setup_dock_launcher.sh
#
# Exits 0 in all cases — failures are non-fatal.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

APP_NAME="FableGear"
INSTALL_DIR="$HOME/Applications"
APP_PATH="$INSTALL_DIR/$APP_NAME.app"
LAUNCH_SH="$REPO_ROOT/launch.sh"
ICON_SRC="$REPO_ROOT/static/icon-logo-fablegear.png"

# ── Ask the user ──────────────────────────────────────────────────────────
DIALOG_MSG="Add FableGear to your Dock?

This builds a native launcher app on your Mac (~/Applications/FableGear.app) and pins it to your Dock — just like any other app.

If you say Not Now, you can still right-click (or two-finger click) the downloaded launcher icon to open it, or run this again any time from Terminal."

RESPONSE=$(osascript \
    -e "button returned of (display dialog \"$DIALOG_MSG\" buttons {\"Not Now\", \"Add to Dock\"} default button \"Add to Dock\" with icon note with title \"FableGear Setup\")" \
    2>/dev/null || echo "Not Now")

if [[ "$RESPONSE" != "Add to Dock" ]]; then
    cat >&2 <<'MSG'
Dock setup skipped.

To add later:  bash ~/FableGear/FableGear/scripts/setup_dock_launcher.sh
Right-click the downloaded FableGear Launcher icon → Open for a one-time launch.
MSG
    exit 0
fi

# ── Build the .app locally via osacompile ────────────────────────────────
mkdir -p "$INSTALL_DIR"

APPLESCRIPT=$(cat <<ASCRIPT
-- FableGear native launcher (built locally by setup_dock_launcher.sh)
-- Runs launch.sh silently in the background.
do shell script "bash '${LAUNCH_SH}' > /dev/null 2>&1 &"
ASCRIPT
)

echo "$APPLESCRIPT" > /tmp/FableGearLocal.applescript
osacompile -o "$APP_PATH" /tmp/FableGearLocal.applescript
rm -f /tmp/FableGearLocal.applescript

# ── Apply icon ────────────────────────────────────────────────────────────
if [[ -f "$ICON_SRC" ]]; then
    ICONSET="$(mktemp -d)/fg.iconset"
    mkdir -p "$ICONSET"
    for size in 16 32 64 128 256 512; do
        sips -z "$size" "$size" "$ICON_SRC" \
            --out "$ICONSET/icon_${size}x${size}.png" >/dev/null 2>&1
        double=$((size * 2))
        sips -z "$double" "$double" "$ICON_SRC" \
            --out "$ICONSET/icon_${size}x${size}@2x.png" >/dev/null 2>&1
    done
    iconutil -c icns "$ICONSET" \
        -o "$APP_PATH/Contents/Resources/applet.icns" 2>/dev/null || true
fi

# ── Pin to Dock via Python plistlib (no extra tools needed) ──────────────
python3 - "$APP_PATH" <<'PYPIN'
import sys, os, plistlib, subprocess

app_path = sys.argv[1]
dock_plist = os.path.expanduser("~/Library/Preferences/com.apple.dock.plist")

try:
    with open(dock_plist, "rb") as f:
        dock = plistlib.load(f)
except Exception as e:
    print(f"Could not read Dock plist: {e}", file=sys.stderr)
    sys.exit(1)

# Check if already in Dock
for item in dock.get("persistent-apps", []):
    url = item.get("tile-data", {}).get("file-data", {}).get("_CFURLString", "")
    if url == app_path:
        print("FableGear already in Dock — done.")
        subprocess.run(["killall", "Dock"], capture_output=True)
        sys.exit(0)

# Append new Dock entry
new_entry = {
    "tile-data": {
        "file-data": {
            "_CFURLString": app_path,
            "_CFURLStringType": 0,
        },
        "file-label": "FableGear",
    },
    "tile-type": "file-tile",
}
dock.setdefault("persistent-apps", []).append(new_entry)

with open(dock_plist, "wb") as f:
    plistlib.dump(dock, f)

subprocess.run(["killall", "Dock"], capture_output=True)
print("FableGear pinned to Dock.")
PYPIN

# ── Build companion Uninstall app ─────────────────────────────────────────
UNINSTALL_SH="$REPO_ROOT/scripts/uninstall.sh"
UNINSTALL_APP="$INSTALL_DIR/FableGear Uninstall.app"

UNINSTALL_SCRIPT=$(cat <<ASCRIPT
-- FableGear Uninstall companion (built locally by setup_dock_launcher.sh)
do shell script "bash '${UNINSTALL_SH}' > /dev/null 2>&1"
ASCRIPT
)
echo "$UNINSTALL_SCRIPT" > /tmp/FableGearUninstall.applescript
osacompile -o "$UNINSTALL_APP" /tmp/FableGearUninstall.applescript
rm -f /tmp/FableGearUninstall.applescript

# Apply a tinted icon to the uninstaller so it's visually distinct
if [[ -f "$ICON_SRC" ]]; then
    ICONSET2="$(mktemp -d)/fgu.iconset"
    mkdir -p "$ICONSET2"
    for size in 16 32 64 128 256 512; do
        sips -z "$size" "$size" "$ICON_SRC" \
            --out "$ICONSET2/icon_${size}x${size}.png" >/dev/null 2>&1
        double=$((size * 2))
        sips -z "$double" "$double" "$ICON_SRC" \
            --out "$ICONSET2/icon_${size}x${size}@2x.png" >/dev/null 2>&1
    done
    iconutil -c icns "$ICONSET2" \
        -o "$UNINSTALL_APP/Contents/Resources/applet.icns" 2>/dev/null || true
fi

# ── Done ──────────────────────────────────────────────────────────────────
osascript -e 'display notification "FableGear added to Dock. Find '\''FableGear Uninstall'\'' in ~/Applications to remove it later." with title "FableGear Setup"' 2>/dev/null || true
echo "Launcher:   $APP_PATH"
echo "Uninstall:  $UNINSTALL_APP"
