#!/bin/bash
# make_automator_app.sh
# Creates a new Automator .app wrapper for RekitBox
# Usage: bash make_automator_app.sh

set -e

APP_NAME="RekitBox"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_PATH="$SCRIPT_DIR/${APP_NAME}.app"
ICON_PNG="$SCRIPT_DIR/rekitbox-app-icon.png"
ICON_ICNS="$SCRIPT_DIR/rekitbox-app-icon.icns"

# 1. Create temporary Automator workflow
TMP_WF="$SCRIPT_DIR/${APP_NAME}_workflow.workflow"
cat > "$TMP_WF" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>AMApplicationBuild</key>
    <string>549.2</string>
    <key>AMApplicationVersion</key>
    <string>2.10</string>
    <key>AMDocumentVersion</key>
    <string>2.0</string>
    <key>actions</key>
    <array>
        <dict>
            <key>action</key>
            <dict>
                <key>AMAccepts</key>
                <dict>
                    <key>Container</key>
                    <string>None</string>
                    <key>Optional</key>
                    <true/>
                    <key>Types</key>
                    <array/>
                </dict>
                <key>AMActionVersion</key>
                <string>2.3.2</string>
                <key>AMParameterProperties</key>
                <dict/>
                <key>AMProvides</key>
                <dict/>
                    <key>Container</key>
                    <string>None</string>
                    <key>Types</key>
                    <array/>
                </dict>
                <key>ActionName</key>
                <string>Run Shell Script</string>
                <key>ActionParameters</key>
                <dict>
                    <key>command</key>
                    <string>bash \"$SCRIPT_DIR/../launch.sh\"</string>
                    <key>inputMethod</key>
                    <string>arguments</string>
                </dict>
                <key>BundleIdentifier</key>
                <string>com.apple.Automator.RunShellScript</string>
                <key>Class Name</key>
                <string>RunShellScriptAction</string>
                <key>UUID</key>
                <string>F2C7B6A2-7B6B-4B6B-8B6B-7B6B7B6B7B6B</string>
            </dict>
        </dict>
    </array>
    <key>connectors</key>
    <array/>
    <key>workflowMetaData</key>
    <dict/>
</dict>
</plist>
EOF

# 2. Build the .app using Automator
/usr/bin/automator -i "$TMP_WF" -o "$APP_PATH"

# 3. Convert PNG icon to ICNS if needed
if [ -f "$ICON_PNG" ] && [ ! -f "$ICON_ICNS" ]; then
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

# 4. Set the custom icon
if [ -f "$ICON_ICNS" ]; then
  cp "$ICON_ICNS" "$APP_PATH/Contents/Resources/AutomatorApplet.icns"
  if command -v SetFile >/dev/null 2>&1; then
    SetFile -a C "$APP_PATH"
  fi
fi

# 5. Clean up
echo "Created $APP_PATH with custom icon."
rm -f "$TMP_WF"
