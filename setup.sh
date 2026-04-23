#!/bin/bash
# RekitBox — first-run dependency installer
# Opened automatically by launch.sh when Homebrew formulas or the Python
# venv are missing. Runs in a visible Terminal window so the user can see
# progress and respond to any password prompts.
#
# When complete it touches ../.rekitbox_ready so launch.sh knows to proceed.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV="$SCRIPT_DIR/../../venv"
SENTINEL="$SCRIPT_DIR/../../.rekitbox_ready"

# ── Banner ────────────────────────────────────────────────────────────────
clear
echo ""
echo "  ╔════════════════════════════════════════════════════════╗"
echo "  ║            RekitBox — First-Run Setup                  ║"
echo "  ║  This runs once. RekitBox will launch when it's done.  ║"
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
    echo "     Fix the issue, then double-click RekitBox again."
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

if ! command -v python3 &>/dev/null; then
  info "python3 not found — installing via Homebrew..."
  "$BREW" install python
fi
ok "Python $(python3 --version 2>&1 | awk '{print $2}')"

# ── Python virtual environment ────────────────────────────────────────────
step "Python virtual environment"

if [ ! -d "$VENV" ]; then
  info "Creating venv at $VENV ..."
  python3 -m venv "$VENV"
  ok "Virtual environment created"
else
  ok "Virtual environment already exists"
fi

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

# ── Ollama (local AI — required for Rekki) ────────────────────────────────
step "Ollama (local AI)"

if ! command -v ollama &>/dev/null; then
  info "Installing Ollama via Homebrew..."
  "$BREW" install --cask ollama
  ok "Ollama installed"
else
  ok "Ollama already installed ($(ollama --version 2>&1 | head -1))"
fi

# Start Ollama server if not already running
if ! pgrep -x ollama &>/dev/null; then
  info "Starting Ollama server..."
  nohup ollama serve > /dev/null 2>&1 &
  # Give it a moment to bind
  sleep 3
fi

# Pull default Rekki model
REKKI_MODEL="${REKIT_AGENT_MODEL:-qwen2.5-coder:7b}"
step "Rekki AI model  ($REKKI_MODEL)"

if ollama list 2>/dev/null | grep -q "^${REKKI_MODEL}"; then
  ok "$REKKI_MODEL already present"
else
  info "Pulling $REKKI_MODEL — this downloads ~4 GB on first run."
  info "Grab a coffee. This only happens once."
  echo ""
  ollama pull "$REKKI_MODEL"
  ok "$REKKI_MODEL ready"
fi

# ── Create launcher .app ─────────────────────────────────────────────────
step "Creating RekitBox.app launcher"

APP_DEST="$HOME/Applications/RekitBox.app"
PACKAGED_APP="$SCRIPT_DIR/packaging/RekitBox.app"

mkdir -p "$HOME/Applications"

# Prefer the bundled shell-script launcher because Finder surfaces generic
# "/bin/bash" alerts when the AppleScript do-shell-script wrapper fails.
# The packaged .app already knows how to hand off to ~/RekitBox/RekitBox.
rm -rf "$APP_DEST"
if [ -d "$PACKAGED_APP" ]; then
  cp -R "$PACKAGED_APP" "$APP_DEST"
  chmod +x "$APP_DEST/Contents/MacOS/RekitBox" 2>/dev/null || true
else
  LAUNCH_PATH="$SCRIPT_DIR/launch.sh"
  osacompile -o "$APP_DEST" - 2>/dev/null <<APPLESCRIPT
do shell script "bash '$LAUNCH_PATH'"
APPLESCRIPT
fi

if [ -d "$APP_DEST" ]; then
  ok "RekitBox.app created at ~/Applications/RekitBox.app"
  info "Drag it to your Dock for one-click access."
  info "Or double-click it from ~/Applications."
else
  info "Could not create RekitBox.app — run launch.sh directly from Terminal."
fi

# ── Done ──────────────────────────────────────────────────────────────────
touch "$SENTINEL"

echo ""
echo "  ╔════════════════════════════════════════════════════════╗"
echo "  ║  ✓  Setup complete. RekitBox is launching now.         ║"
echo "  ║     This window will close in 4 seconds.               ║"
echo "  ╚════════════════════════════════════════════════════════╝"
echo ""
sleep 4
