#!/bin/bash
# build_release.sh — builds the distributable SuperBox.app and uploads a new GitHub release.
#
# What the generated .app does:
#   First launch  → opens Terminal, git-clones the repo to ~/SuperBox/SuperBox,
#                   then hands off to launch.sh (which runs setup.sh if needed)
#   Every launch  → runs launch.sh directly (which does git pull + starts server)
#
# Usage:
#   bash build_release.sh              # builds SuperBox.zip in the current dir
#   bash build_release.sh --release    # also creates a new GitHub release and uploads it

set -euo pipefail

REPO_URL="https://github.com/fabledharbinger0993/SuperBox.git"
APP_NAME="SuperBox.app"
ZIP_NAME="SuperBox.zip"
BUILD_DIR="$(mktemp -d)"

# ── Parse flags ───────────────────────────────────────────────────────────────
DO_RELEASE=false
for arg in "$@"; do
  [[ "$arg" == "--release" ]] && DO_RELEASE=true
done

# ── Version from latest git tag ───────────────────────────────────────────────
VERSION="$(git describe --tags --abbrev=0 2>/dev/null || echo "v0.0.0")"
echo "Building $APP_NAME  version $VERSION"

# ── Write the AppleScript ─────────────────────────────────────────────────────
APPLESCRIPT=$(cat <<'APPLEEOF'
-- SuperBox bootstrap launcher
-- First run: clones the repo. Every run: starts the server.

set homeDir to POSIX path of (path to home folder)
set parentDir to homeDir & "SuperBox"
set installDir to parentDir & "/SuperBox"
set launchScript to installDir & "/launch.sh"
set repoURL to "https://github.com/fabledharbinger0993/SuperBox.git"

-- Check whether SuperBox is already installed
set isInstalled to false
try
	do shell script "test -d " & quoted form of installDir & "/.git"
	set isInstalled to true
end try

if isInstalled then
	-- Already installed: hand off to launch.sh (handles git pull + server start)
	do shell script "bash " & quoted form of launchScript
else
	-- First install: open a Terminal window so the user can see git clone + setup progress
	set cloneCmd to "mkdir -p " & quoted form of parentDir & " && git clone " & quoted form of repoURL & " " & quoted form of installDir & " && bash " & quoted form of launchScript
	tell application "Terminal"
		do script cloneCmd
		activate
	end tell
end if
APPLEEOF
)

# ── Compile .app ──────────────────────────────────────────────────────────────
APP_PATH="$BUILD_DIR/$APP_NAME"
echo "$APPLESCRIPT" | osacompile -o "$APP_PATH" -
echo "  ✓ Compiled $APP_NAME"

# ── Copy SuperBox icon onto the .app ─────────────────────────────────────────
ICON_SRC="$(dirname "$0")/static/SRB_LOGO.png"
if [[ -f "$ICON_SRC" ]]; then
  ICON_DEST="$APP_PATH/Contents/Resources/applet.icns"
  # Convert PNG → ICNS using sips + iconutil
  ICONSET_DIR="$BUILD_DIR/superbox.iconset"
  mkdir -p "$ICONSET_DIR"
  for size in 16 32 64 128 256 512; do
    sips -z $size $size "$ICON_SRC" --out "$ICONSET_DIR/icon_${size}x${size}.png" &>/dev/null
    double=$((size * 2))
    sips -z $double $double "$ICON_SRC" --out "$ICONSET_DIR/icon_${size}x${size}@2x.png" &>/dev/null
  done
  iconutil -c icns "$ICONSET_DIR" -o "$ICON_DEST" 2>/dev/null && echo "  ✓ Icon applied" || echo "  ⚠ Icon conversion failed — app will use default icon"
fi

# ── Strip ad-hoc code signature ───────────────────────────────────────────────
# osacompile signs the app with an ad-hoc signature. On macOS Sequoia+,
# ad-hoc signed apps downloaded from the internet show "damaged and can't be
# opened" with no recourse. Stripping the signature downgrades this to
# "unidentified developer", which shows the Open Anyway button in
# System Settings → Privacy & Security.
codesign --remove-signature "$APP_PATH" 2>/dev/null && echo "  ✓ Ad-hoc signature stripped" || echo "  ⚠ Could not strip signature"

# ── Package into SuperBox.zip ─────────────────────────────────────────────────
ZIP_PATH="$(pwd)/$ZIP_NAME"
(cd "$BUILD_DIR" && zip -qr "$ZIP_PATH" "$APP_NAME")
echo "  ✓ Packaged → $ZIP_PATH"
rm -rf "$BUILD_DIR"

# ── Optionally create a GitHub release ───────────────────────────────────────
if [[ "$DO_RELEASE" == true ]]; then
  echo ""
  echo "Creating GitHub release $VERSION …"

  RELEASE_NOTES="## Install

1. Download **SuperBox.zip** below
2. Unzip — you get **SuperBox.app**
3. Move it to your Desktop or Applications folder
4. Double-click to launch

> **First launch** opens a Terminal window and clones SuperBox, then automatically installs everything needed — Homebrew, \`ffmpeg\`, \`chromaprint\`, and all Python packages. This runs once and takes a few minutes. SuperBox opens in your browser when it's done.

> **Future launches** update SuperBox automatically — no manual downloads needed.

## \"SuperBox is damaged\" or \"cannot be opened\"?

This is macOS Gatekeeper — it blocks apps that aren't signed with an Apple Developer certificate. To allow it:

1. Go to **System Settings → Privacy & Security**
2. Scroll down — you'll see *\"SuperBox was blocked from use\"*
3. Click **Open Anyway**

Alternatively, right-click the app → **Open** → **Open Anyway**."

  gh release create "$VERSION" "$ZIP_PATH" \
    --title "SuperBox $VERSION" \
    --notes "$RELEASE_NOTES" \
    --latest 2>/dev/null \
    || gh release upload "$VERSION" "$ZIP_PATH" --clobber

  echo "  ✓ Release $VERSION published"
  echo "  → https://github.com/fabledharbinger0993/SuperBox/releases/tag/$VERSION"
fi

echo ""
echo "Done. $ZIP_NAME is ready."
