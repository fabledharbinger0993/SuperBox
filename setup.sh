#!/bin/bash
# FableGear — first-run dependency installer
# Opened automatically by launch.sh when Homebrew formulas or the Python
# venv are missing. Runs in a visible Terminal window so the user can see
# progress and respond to any password prompts.
#
# When complete it touches .fablegear_ready so launch.sh knows to proceed.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV="$SCRIPT_DIR/venv"
SENTINEL="$SCRIPT_DIR/.fablegear_ready"

# ── Banner ────────────────────────────────────────────────────────────────
clear
echo ""
echo "  ╔════════════════════════════════════════════════════════╗"
echo "  ║            FableGear — First-Run Setup                  ║"
echo "  ║  This runs once. FableGear will launch when it's done.  ║"
echo "  ╚════════════════════════════════════════════════════════╝"
echo ""

# ── Helper: print a step header ───────────────────────────────────────────
step() { echo ""; echo "  ── $1"; }
ok()   { echo "  ✓  $1"; }
info() { echo "     $1"; }

# ── Homebrew ──────────────────────────────────────────────────────────────
step "Homebrew"

BREW=""
for p in /opt/homebrew/bin/brew /usr/local/bin/brew; do
  [ -f "$p" ] && BREW="$p" && break
done

if [ -z "$BREW" ]; then
  info "Not found — installing Homebrew."
  info "You may be prompted for your Mac password."
  echo ""
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  # Re-locate brew after install
  for p in /opt/homebrew/bin/brew /usr/local/bin/brew; do
    [ -f "$p" ] && BREW="$p" && break
  done
  if [ -z "$BREW" ]; then
    echo ""
    echo "  ✗  Homebrew installation failed. Check the output above."
    echo "     Fix the issue, then double-click FableGear again."
    read -rp "     Press Return to close this window." _
    exit 1
  fi
  ok "Homebrew installed"
else
  info "Found at $BREW — updating..."
  "$BREW" update --quiet
  ok "Homebrew up to date"
fi

# Ensure brew is on PATH for the rest of this session
eval "$("$BREW" shellenv)"

# ── Required Homebrew formulas ────────────────────────────────────────────
step "Homebrew formulas  (ffmpeg, chromaprint)"

FORMULAS=(ffmpeg chromaprint)
for formula in "${FORMULAS[@]}"; do
  if "$BREW" list --formula "$formula" &>/dev/null; then
    info "Upgrading $formula..."
    # shellcheck disable=SC2015
    "$BREW" upgrade "$formula" 2>/dev/null \
      && ok "$formula upgraded" \
      || ok "$formula already at latest"
  else
    info "Installing $formula..."
    "$BREW" install "$formula"
    ok "$formula installed"
  fi
done

# ── Python 3 ─────────────────────────────────────────────────────────────
step "Python 3"

# Prefer Python 3.13 — 3.14 ships without ensurepip on some macOS setups,
# which produces a broken venv (no pip, no activate). Fall back through
# known-good versions before using whatever python3 resolves to.
PYTHON3=""
for candidate in python3.13 python3.12 python3.11 python3; do
  if command -v "$candidate" &>/dev/null; then
    ver=$("$candidate" -c 'import sys; print(sys.version_info[:2])' 2>/dev/null)
    # Skip 3.14+ (ensurepip issues on macOS)
    major=$("$candidate" -c 'import sys; print(sys.version_info[1])' 2>/dev/null)
    if [ "${major:-99}" -le 13 ]; then
      PYTHON3="$candidate"
      break
    fi
  fi
done

if [ -z "$PYTHON3" ]; then
  info "No suitable Python found — installing python@3.13 via Homebrew..."
  "$BREW" install python@3.13
  PYTHON3="python3.13"
fi
ok "Python $("$PYTHON3" --version 2>&1 | awk '{print $2}')"

# ── Python virtual environment ────────────────────────────────────────────
step "Python virtual environment"

if [ ! -d "$VENV" ]; then
  info "Creating venv at $VENV ..."
  "$PYTHON3" -m venv "$VENV"
  ok "Virtual environment created"
else
  ok "Virtual environment already exists"
fi

# shellcheck disable=SC1091
source "$VENV/bin/activate"

# ── Python packages ───────────────────────────────────────────────────────
step "Python packages"

info "Upgrading pip..."
pip install --upgrade pip --quiet

info "Installing UI packages (Flask, Waitress)..."
pip install -r "$SCRIPT_DIR/requirements_ui.txt" --quiet

info "Installing library packages..."
pip install -r "$SCRIPT_DIR/requirements.txt" --quiet

ok "All Python packages installed"

pip install "mcp[cli]"
# ── Create launcher .app ─────────────────────────────────────────────────
step "Creating FableGear.app launcher"

APP_DEST="$HOME/Applications/FableGear.app"
PACKAGED_APP="$SCRIPT_DIR/packaging/FableGear.app"

mkdir -p "$HOME/Applications"

# Prefer the bundled shell-script launcher because Finder surfaces generic
# "/bin/bash" alerts when the AppleScript do-shell-script wrapper fails.
# The packaged .app already knows how to hand off to ~/FableGear/FableGear.
rm -rf "$APP_DEST"
if [ -d "$PACKAGED_APP" ]; then
  cp -R "$PACKAGED_APP" "$APP_DEST"
  chmod +x "$APP_DEST/Contents/MacOS/FableGear" 2>/dev/null || true
else
  LAUNCH_PATH="$SCRIPT_DIR/launch.sh"
  osacompile -o "$APP_DEST" - 2>/dev/null <<APPLESCRIPT
do shell script "bash '$LAUNCH_PATH'"
APPLESCRIPT
fi

if [ -d "$APP_DEST" ]; then
  ok "FableGear.app created at ~/Applications/FableGear.app"

  # ── Apply icon (sips + iconutil) ────────────────────────────────────────
  ICON_SRC="$SCRIPT_DIR/static/icon-logo-fablegear.png"
  if [ -f "$ICON_SRC" ]; then
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
      -o "$APP_DEST/Contents/Resources/applet.icns" 2>/dev/null && \
      ok "Icon applied" || info "Icon apply skipped (iconutil unavailable)"
    rm -rf "$(dirname "$ICONSET")"
  fi

  info "Drag it to your Dock for one-click access."
  info "Or double-click it from ~/Applications."
else
  info "Could not create FableGear.app — run launch.sh directly from Terminal."
fi

# ── Done ──────────────────────────────────────────────────────────────────
touch "$SENTINEL"

echo ""
echo "  ╔════════════════════════════════════════════════════════╗"
echo "  ║  ✓  Setup complete. FableGear is launching now.         ║"
echo "  ║     This window will close in 4 seconds.               ║"
echo "  ╚════════════════════════════════════════════════════════╝"
echo ""
sleep 4
