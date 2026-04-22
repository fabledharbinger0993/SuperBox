#!/bin/bash
# Build RekitBox .app wrapper from AppleScript and set custom icon
set -e

APP_NAME="RekitBox"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SRC_APPLESCRIPT="$SCRIPT_DIR/RekitBoxLauncher.applescript"
ICON_PNG="$SCRIPT_DIR/rekitbox-app-icon.png"
ICON_ICNS="$SCRIPT_DIR/rekitbox-app-icon.icns"
APP_BUNDLE="$SCRIPT_DIR/${APP_NAME}.app"

# Convert PNG to ICNS (requires sips and iconutil)
if [ ! -f "$ICON_ICNS" ]; then
  mkdir -p "$SCRIPT_DIR/icon.iconset"
  sips -z 512 512     "$ICON_PNG" --out "$SCRIPT_DIR/icon.iconset/icon_512x512.png"
  sips -z 256 256     "$ICON_PNG" --out "$SCRIPT_DIR/icon.iconset/icon_256x256.png"
  sips -z 128 128     "$ICON_PNG" --out "$SCRIPT_DIR/icon.iconset/icon_128x128.png"
  sips -z 64 64       "$ICON_PNG" --out "$SCRIPT_DIR/icon.iconset/icon_64x64.png"
  sips -z 32 32       "$ICON_PNG" --out "$SCRIPT_DIR/icon.iconset/icon_32x32.png"
  sips -z 16 16       "$ICON_PNG" --out "$SCRIPT_DIR/icon.iconset/icon_16x16.png"
  iconutil -c icns "$SCRIPT_DIR/icon.iconset" -o "$ICON_ICNS"
  rm -rf "$SCRIPT_DIR/icon.iconset"
fi

# Compile AppleScript to .app
osacompile -o "$APP_BUNDLE" "$SRC_APPLESCRIPT"

# Set custom icon
cp "$ICON_ICNS" "$APP_BUNDLE/Contents/Resources/applet.icns"

# Touch the bundle so Finder picks up the new icon
if command -v SetFile >/dev/null 2>&1; then
  SetFile -a C "$APP_BUNDLE"
fi

echo "Built $APP_BUNDLE with custom icon."
