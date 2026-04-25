#!/bin/bash
# build_agent_release.sh — builds a dedicated RekitBox Agent.app zip.
# Output: RekitBox-Agent.zip

set -euo pipefail

DEFAULT_REPO_URL="https://github.com/fabledharbinger0993/RekitBox.git"
ORIGIN_URL="$(git config --get remote.origin.url 2>/dev/null || true)"
REPO_URL="${AGENT_REPO_URL:-${ORIGIN_URL:-$DEFAULT_REPO_URL}}"
APP_NAME="RekitBox Agent.app"
EXECUTABLE_NAME="RekitBoxAgent"
ZIP_NAME="RekitBox-Agent.zip"
BUILD_DIR="$(mktemp -d)"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOCAL_DEV_REPO="$SCRIPT_DIR"

VERSION="$(git describe --tags --abbrev=0 2>/dev/null || echo "v0.0.0")"
echo "Building $APP_NAME version $VERSION"
echo "Agent source repo: $REPO_URL"

APP_PATH="$BUILD_DIR/$APP_NAME"
mkdir -p "$APP_PATH/Contents/MacOS"
mkdir -p "$APP_PATH/Contents/Resources"

cat > "$APP_PATH/Contents/MacOS/$EXECUTABLE_NAME" << LAUNCHER
#!/bin/bash
# RekitBox Agent bootstrap launcher

PARENT_DIR="\$HOME/RekitBox"
INSTALL_DIR="\$PARENT_DIR/RekitBox"
REPO_URL="$REPO_URL"
LOCAL_DEV_REPO="$LOCAL_DEV_REPO"

clone_source="\$REPO_URL"
if [ -d "\$LOCAL_DEV_REPO/.git" ] && [ -f "\$LOCAL_DEV_REPO/scripts/rekit_agent.py" ]; then
  clone_source="\$LOCAL_DEV_REPO"
fi

if [ -d "\$INSTALL_DIR/.git" ]; then
  if [ -f "\$INSTALL_DIR/launch_agent.sh" ]; then
    bash "\$INSTALL_DIR/launch_agent.sh"
  else
    STALE_BACKUP="\${INSTALL_DIR}.stale-\$(date +%Y%m%d-%H%M%S)"
    mv "\$INSTALL_DIR" "\$STALE_BACKUP"
    CLONE_CMD="mkdir -p '\$PARENT_DIR' && git clone '\$clone_source' '\$INSTALL_DIR' && bash '\$INSTALL_DIR/launch_agent.sh'"
    osascript -e "tell application \"Terminal\" to do script \"\$CLONE_CMD\""
    osascript -e "tell application \"Terminal\" to activate"
  fi
else
  CLONE_CMD="mkdir -p '\$PARENT_DIR' && git clone '\$clone_source' '\$INSTALL_DIR' && bash '\$INSTALL_DIR/launch_agent.sh'"
  osascript -e "tell application \"Terminal\" to do script \"\$CLONE_CMD\""
  osascript -e "tell application \"Terminal\" to activate"
fi
LAUNCHER
chmod +x "$APP_PATH/Contents/MacOS/$EXECUTABLE_NAME"
echo "  ✓ Launcher script written"

cat > "$APP_PATH/Contents/Info.plist" << PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleExecutable</key>
  <string>$EXECUTABLE_NAME</string>
  <key>CFBundleIconFile</key>
  <string>applet</string>
  <key>CFBundleIdentifier</key>
  <string>com.fabledharbinger.rekitbox.agent</string>
  <key>CFBundleName</key>
  <string>RekitBox Agent</string>
  <key>CFBundleDisplayName</key>
  <string>RekitBox Agent</string>
  <key>CFBundlePackageType</key>
  <string>APPL</string>
  <key>CFBundleShortVersionString</key>
  <string>1.0.0</string>
  <key>CFBundleVersion</key>
  <string>1</string>
  <key>LSMinimumSystemVersion</key>
  <string>12.0</string>
  <key>NSHighResolutionCapable</key>
  <true/>
  <key>NSAppleEventsUsageDescription</key>
  <string>RekitBox Agent uses Terminal to install dependencies and launch the server.</string>
</dict>
</plist>
PLIST
echo "  ✓ Info.plist written"

# Prefer new branded icon, fall back to legacy logo
ICON_SRC="$SCRIPT_DIR/static/icon-rekitbox-app.png"
[[ ! -f "$ICON_SRC" ]] && ICON_SRC="$SCRIPT_DIR/static/RB_LOGO.png"

if [[ -f "$ICON_SRC" ]]; then
  ICONSET_DIR="$BUILD_DIR/rekitbox_agent.iconset"
  mkdir -p "$ICONSET_DIR"
  for size in 16 32 64 128 256 512; do
    sips -z $size $size "$ICON_SRC" --out "$ICONSET_DIR/icon_${size}x${size}.png" &>/dev/null
    double=$((size * 2))
    sips -z $double $double "$ICON_SRC" --out "$ICONSET_DIR/icon_${size}x${size}@2x.png" &>/dev/null
  done
  iconutil -c icns "$ICONSET_DIR" -o "$APP_PATH/Contents/Resources/applet.icns" 2>/dev/null || true
fi

ZIP_PATH="$(pwd)/$ZIP_NAME"
(cd "$BUILD_DIR" && zip -qr "$ZIP_PATH" "$APP_NAME")
echo "  ✓ Packaged -> $ZIP_PATH"

rm -rf "$BUILD_DIR"
echo "Done. $ZIP_NAME is ready."
