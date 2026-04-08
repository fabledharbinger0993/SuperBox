#!/bin/bash
# SuperBox build script
#
# Produces dist/SuperBox.app — a self-contained macOS application bundle.
# Run this from the SuperBox/ directory (where this file lives).
#
# Requirements:
#   - The venv must exist (run launch.sh once to create it, or: python3 -m venv ../venv)
#   - pywebview and pyinstaller must be installed (pip install -r requirements.txt)
#
# Output:
#   dist/SuperBox.app     ← drag this to /Applications or zip for distribution
#
# After building:
#   cd dist && zip -r SuperBox.zip SuperBox.app

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV="$SCRIPT_DIR/../venv"
PYTHON="$VENV/bin/python"

cd "$SCRIPT_DIR"

echo "── SuperBox build ───────────────────────────────────────"

# Ensure venv exists
if [ ! -f "$PYTHON" ]; then
  echo "ERROR: venv not found at $VENV"
  echo "       Run launch.sh once first to create the venv, then re-run build.sh."
  exit 1
fi

# Install/upgrade build tools inside the venv
echo "→ Installing pywebview + pyinstaller…"
"$PYTHON" -m pip install --quiet --upgrade pywebview pyinstaller

# Clean previous build artefacts
echo "→ Cleaning previous build…"
rm -rf build dist

# Run PyInstaller
echo "→ Building SuperBox.app…"
"$VENV/bin/pyinstaller" SuperBox.spec --noconfirm

echo ""
echo "✓ Done: dist/SuperBox.app"
echo ""
echo "  To distribute:"
echo "    cd dist && zip -r SuperBox.zip SuperBox.app"
echo ""
echo "  NOTE: macOS Gatekeeper will show a security warning for unsigned apps."
echo "        Users right-click → Open to bypass it (same as the current zip)."
echo "        To remove the warning permanently, code-sign with an Apple Developer"
echo "        account: set codesign_identity in SuperBox.spec."
