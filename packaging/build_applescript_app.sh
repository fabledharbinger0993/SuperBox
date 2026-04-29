#!/bin/bash
# Build FABLEGEAR .app wrappers from AppleScript and set custom icon
# Usage: bash build_applescript_app.sh [main|agent|both]
set -e

MODE="${1:-both}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ICON_PNG="$SCRIPT_DIR/FABLEGEAR-app-icon.png"
ICON_ICNS="$SCRIPT_DIR/FABLEGEAR-app-icon.icns"

# Convert PNG to ICNS (requires sips and iconutil)
if [ -f "$ICON_PNG" ] && [ ! -f "$ICON_ICNS" ]; then
  echo "Converting icon to ICNS format..."
  mkdir -p "$SCRIPT_DIR/icon.iconset"
  sips -z 512 512     "$ICON_PNG" --out "$SCRIPT_DIR/icon.iconset/icon_512x512.png" >/dev/null
  sips -z 256 256     "$ICON_PNG" --out "$SCRIPT_DIR/icon.iconset/icon_256x256.png" >/dev/null
  sips -z 128 128     "$ICON_PNG" --out "$SCRIPT_DIR/icon.iconset/icon_128x128.png" >/dev/null
  sips -z 64 64       "$ICON_PNG" --out "$SCRIPT_DIR/icon.iconset/icon_64x64.png" >/dev/null
  sips -z 32 32       "$ICON_PNG" --out "$SCRIPT_DIR/icon.iconset/icon_32x32.png" >/dev/null
  sips -z 16 16       "$ICON_PNG" --out "$SCRIPT_DIR/icon.iconset/icon_16x16.png" >/dev/null
  iconutil -c icns "$SCRIPT_DIR/icon.iconset" -o "$ICON_ICNS"
  rm -rf "$SCRIPT_DIR/icon.iconset"
fi

# Function to build a single app
build_app() {
  local APP_NAME="$1"
  local SRC_APPLESCRIPT="$2"
  local APP_BUNDLE="$SCRIPT_DIR/${APP_NAME}.app"
  
  echo "Building ${APP_NAME}.app..."
  
  # Compile AppleScript to .app
  osacompile -o "$APP_BUNDLE" "$SRC_APPLESCRIPT"
  
  # Set custom icon
  if [ -f "$ICON_ICNS" ]; then
    cp "$ICON_ICNS" "$APP_BUNDLE/Contents/Resources/applet.icns"
    # Touch the bundle so Finder picks up the new icon
    if command -v SetFile >/dev/null 2>&1; then
      SetFile -a C "$APP_BUNDLE"
    fi
  fi
  
  echo "✅ Built $APP_BUNDLE"
}

# Build requested apps
case "$MODE" in
  main)
    build_app "FABLEGEAR" "$SCRIPT_DIR/FABLEGEARLauncher.applescript"
    ;;
  agent)
    build_app "FABLEGEAR Agent" "$SCRIPT_DIR/FABLEGEARAgentLauncher.applescript"
    ;;
  both|*)
    build_app "FABLEGEAR" "$SCRIPT_DIR/FABLEGEARLauncher.applescript"
    build_app "FABLEGEAR Agent" "$SCRIPT_DIR/FABLEGEARAgentLauncher.applescript"
    ;;
esac

echo ""
echo "📦 Apps ready in: $SCRIPT_DIR"
echo "To install: cp -r \"$SCRIPT_DIR/FABLEGEAR\"*.app ~/Applications/"
