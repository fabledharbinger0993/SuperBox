#!/bin/bash
# FableGear launcher
# Run directly: bash launch.sh
# Or wrap in Automator > Application > Run Shell Script for a dock icon

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV="$SCRIPT_DIR/venv"
SENTINEL="$SCRIPT_DIR/.fablegear_ready"
DOCK_SENTINEL="$SCRIPT_DIR/.dock_launcher_offered"
LOG="$SCRIPT_DIR/fablegear.log"

# ── Locate Homebrew (works on both Apple Silicon and Intel) ───────────────
_brew() {
  for p in /opt/homebrew/bin/brew /usr/local/bin/brew; do
    [ -f "$p" ] && { "$p" "$@"; return; }
  done
  return 1
}

# ── Determine whether first-run setup is needed ───────────────────────────
_setup_needed() {
  # Homebrew missing?
  _brew --version &>/dev/null || return 0
  # Required formulas missing?
  for formula in ffmpeg chromaprint; do
    _brew list --formula "$formula" &>/dev/null || return 0
  done
 # Python venv missing?
  [ ! -d "$VENV" ] && return 0
  # Sentinel not yet written by setup.sh?
  [ ! -f "$SENTINEL" ] && return 0
  return 1
}

# ── First-run setup ───────────────────────────────────────────────────────
# Must happen before exec > /dev/null so Automator doesn't see it as an
# error, yet users still need a visible window for password prompts and
# progress. Solution: open a new Terminal window running setup.sh and poll
# for the sentinel file before proceeding.
if _setup_needed; then
  rm -f "$SENTINEL"   # clear any stale sentinel
  # open -a Terminal runs the script via Launch Services — no Automation
  # permission required (unlike osascript "tell application Terminal do script")
  open -a Terminal "$SCRIPT_DIR/setup.sh"
  # Wait for setup.sh to touch the sentinel (max 40 min, polls every 2 s)
  _waited=0
  until [ -f "$SENTINEL" ]; do
    sleep 2
    _waited=$((_waited + 2))
    if [ $_waited -ge 2400 ]; then
      echo "FableGear: setup timed out — check the setup window for errors" >&2
      exit 1
    fi
  done
  unset _waited
fi

# ── Offer native Dock launcher on first boot (one-time, before silence) ──
# Skipped automatically if: already offered, already installed, or launched
# from Automator/non-TTY (no point showing a dialog with no screen context).
if [ ! -f "$DOCK_SENTINEL" ] && [ ! -d "$HOME/Applications/FableGear.app" ]; then
  touch "$DOCK_SENTINEL"
  bash "$SCRIPT_DIR/scripts/setup_dock_launcher.sh" || true
fi

# ── Silence all output — Automator treats any stdout as an error ──────────
exec > /dev/null 2>&1


# ── Homebrew update/upgrade (silent, non-blocking) ───────────────────────
if _brew --version &>/dev/null; then
  (_brew update >/dev/null 2>&1 && _brew upgrade --formula >/dev/null 2>&1) &
fi

# ── Activate venv ─────────────────────────────────────────────────────────
source "$VENV/bin/activate"

# ── Pull latest from GitHub (skip in dev mode) ───────────────────────────
cd "$SCRIPT_DIR"
if [ ! -f "$SCRIPT_DIR/.dev" ]; then
  git pull origin main --ff-only >> "$LOG" 2>&1
  # After git pull, requirements may have changed — reinstall/upgrade quietly.
  pip install --upgrade --quiet -r "$SCRIPT_DIR/requirements_ui.txt" >> "$LOG" 2>&1
  pip install --upgrade --quiet -r "$SCRIPT_DIR/requirements.txt" >> "$LOG" 2>&1
fi

# ── Bring up Tailscale for FableGo remote access (best-effort, non-blocking) ─
# FableGear runs fully offline without this. Tailscale just enables the iOS app
# to connect remotely. Silent on failure — missing Tailscale is not an error.
if command -v tailscale &>/dev/null; then
  tailscale up --accept-routes >> "$LOG" 2>&1 &
fi

# ── Launch FableGear ──────────────────────────────────────────────────────
# main.py handles splash internally (with OS-level watchdog timeout)
nohup "$VENV/bin/python" "$SCRIPT_DIR/main.py" >> "$LOG" 2>&1 &

# ── Close Terminal window if launched interactively (not via Automator) ───
# Automator runs via do shell script (no TTY), so this block is skipped there.
# When run manually from Terminal, close the window so it doesn't linger.
if [ -t 0 ]; then
  osascript -e 'tell application "Terminal" to close front window' > /dev/null 2>&1 &
fi
