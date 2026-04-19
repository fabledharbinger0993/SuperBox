#!/bin/bash
# RekitBox Agent — first-run dependency installer
# Creates and provisions a dedicated venv-agent runtime.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV="$SCRIPT_DIR/../../venv-agent"
SENTINEL="$SCRIPT_DIR/../../.rekitbox_agent_ready"

clear
echo ""
echo "  ╔════════════════════════════════════════════════════════╗"
echo "  ║         RekitBox Agent — First-Run Setup               ║"
echo "  ║   Isolated agent environment will be created now.      ║"
echo "  ╚════════════════════════════════════════════════════════╝"
echo ""

step() { echo ""; echo "  ── $1"; }
ok()   { echo "  ✓  $1"; }
info() { echo "     $1"; }

step "Homebrew"
BREW=""
for p in /opt/homebrew/bin/brew /usr/local/bin/brew; do
  [ -f "$p" ] && BREW="$p" && break
done

if [ -z "$BREW" ]; then
  info "Not found — installing Homebrew."
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  for p in /opt/homebrew/bin/brew /usr/local/bin/brew; do
    [ -f "$p" ] && BREW="$p" && break
  done
  if [ -z "$BREW" ]; then
    echo ""
    echo "  ✗  Homebrew installation failed."
    read -rp "     Press Return to close this window." _
    exit 1
  fi
  ok "Homebrew installed"
else
  info "Found at $BREW — updating..."
  "$BREW" update --quiet
  ok "Homebrew up to date"
fi

eval "$("$BREW" shellenv)"

step "Homebrew formulas  (ffmpeg, chromaprint)"
for formula in ffmpeg chromaprint; do
  if "$BREW" list --formula "$formula" &>/dev/null; then
    info "Upgrading $formula..."
    "$BREW" upgrade "$formula" 2>/dev/null || true
    ok "$formula ready"
  else
    info "Installing $formula..."
    "$BREW" install "$formula"
    ok "$formula installed"
  fi
done

step "Python 3"
if ! command -v python3 &>/dev/null; then
  info "python3 not found — installing via Homebrew..."
  "$BREW" install python
fi
ok "Python $(python3 --version 2>&1 | awk '{print $2}')"

step "Agent virtual environment"
if [ ! -d "$VENV" ]; then
  info "Creating venv at $VENV ..."
  python3 -m venv "$VENV"
  ok "Agent virtual environment created"
else
  ok "Agent virtual environment already exists"
fi

source "$VENV/bin/activate"

step "Python packages"
info "Upgrading pip..."
pip install --upgrade pip --quiet
info "Installing UI packages..."
pip install -r "$SCRIPT_DIR/requirements_ui.txt" --quiet
info "Installing library packages..."
pip install -r "$SCRIPT_DIR/requirements.txt" --quiet
ok "All Python packages installed"

step "Creating RekitBox Agent.app launcher"
APP_DEST="$HOME/Applications/RekitBox Agent.app"
LAUNCH_PATH="$SCRIPT_DIR/launch_agent.sh"
mkdir -p "$HOME/Applications"
osacompile -o "$APP_DEST" - 2>/dev/null <<APPLESCRIPT
do shell script "bash '$LAUNCH_PATH'"
APPLESCRIPT

if [ -d "$APP_DEST" ]; then
  ok "RekitBox Agent.app created at ~/Applications/RekitBox Agent.app"
else
  info "Could not create RekitBox Agent.app — run launch_agent.sh directly."
fi

touch "$SENTINEL"

echo ""
echo "  ╔════════════════════════════════════════════════════════╗"
echo "  ║  ✓  Agent setup complete. RekitBox Agent can launch.   ║"
echo "  ╚════════════════════════════════════════════════════════╝"
echo ""
sleep 4
