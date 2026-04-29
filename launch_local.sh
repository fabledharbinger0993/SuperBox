#!/bin/bash
# launch_local.sh — Local-dev launcher for FableGear
#
# Identical to launch.sh but:
#   - Hardwired to the canonical local repo (no clone, no git pull)
#   - Skips the GitHub git pull so local uncommitted work is never clobbered
#
# Use this as the Automator app target during active development.
# Switch back to launch.sh (or the bootstrap script) for public releases.

SCRIPT_DIR="/Users/cameronkelly/FABLEDHARBINGER/GIT_REPOS/FableGear"
VENV="$SCRIPT_DIR/.venv"
SENTINEL="$SCRIPT_DIR/.fablegear_ready"
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
  _brew --version &>/dev/null || return 0
  for formula in ffmpeg chromaprint; do
    _brew list --formula "$formula" &>/dev/null || return 0
  done
  [ ! -d "$VENV" ] && return 0
  [ ! -f "$SENTINEL" ] && return 0
  return 1
}

# ── First-run setup (opens visible Terminal window for password prompts) ──
if _setup_needed; then
  rm -f "$SENTINEL"
  osascript -e "tell application \"Terminal\" to do script \"bash '${SCRIPT_DIR}/setup.sh'; exit\""
  osascript -e "tell application \"Terminal\" to activate"
  until [ -f "$SENTINEL" ]; do sleep 2; done
fi

# ── Silence all output — Automator treats any stdout as an error ──────────
exec > /dev/null 2>&1

# ── Homebrew update/upgrade (silent, non-blocking) ───────────────────────
if _brew --version &>/dev/null; then
  (_brew update >/dev/null 2>&1 && _brew upgrade --formula >/dev/null 2>&1) &
fi

# ── Python / pip — use explicit venv paths (Automator runs a non-login shell
# that may not honour `source activate`, so bare `pip` can resolve to the
# wrong interpreter).
PYTHON="$VENV/bin/python"
PIP="$VENV/bin/pip"

# NOTE: git pull intentionally omitted — running from local repo.
#       Commit and push when ready for an official release, then
#       switch Automator back to launch.sh / the bootstrap script.

# ── Update Python dependencies ────────────────────────────────────────────
"$PIP" install --upgrade --quiet -r "$SCRIPT_DIR/requirements_ui.txt" >> "$LOG" 2>&1
"$PIP" install --upgrade --quiet -r "$SCRIPT_DIR/requirements.txt"    >> "$LOG" 2>&1

# ── Bring up Tailscale for FableGo remote access (best-effort) ───────────
if command -v tailscale &>/dev/null; then
  tailscale up --accept-routes >> "$LOG" 2>&1 &
fi

# ── Launch FableGear ───────────────────────────────────────────────────────
nohup "$PYTHON" "$SCRIPT_DIR/main.py" >> "$LOG" 2>&1 &

# ── Close Terminal window if launched interactively ───────────────────────
if [ -t 0 ]; then
  osascript -e 'tell application "Terminal" to close front window' > /dev/null 2>&1 &
fi
