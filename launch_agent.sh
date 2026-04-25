#!/bin/bash
# RekitBox Agent launcher
# Uses a dedicated venv so agent dependencies/config stay isolated
# from the regular RekitBox runtime.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV="$SCRIPT_DIR/../venv-agent"
SENTINEL="$SCRIPT_DIR/../.rekitbox_agent_ready"
LOG="$SCRIPT_DIR/../rekitbox-agent.log"
AGENT_ENV_FILE="$SCRIPT_DIR/.rekitbox-agent.env"

_brew() {
  for p in /opt/homebrew/bin/brew /usr/local/bin/brew; do
    [ -f "$p" ] && { "$p" "$@"; return; }
  done
  return 1
}

_setup_needed() {
  _brew --version &>/dev/null || return 0
  for formula in ffmpeg chromaprint; do
    _brew list --formula "$formula" &>/dev/null || return 0
  done
  # Ollama required for Rekki inference
  command -v ollama &>/dev/null || return 0
  local _model="${REKIT_AGENT_MODEL:-qwen2.5-coder:7b}"
  ollama list 2>/dev/null | grep -q "^${_model}" || return 0
  [ ! -d "$VENV" ] && return 0
  [ ! -f "$SENTINEL" ] && return 0
  return 1
}

if _setup_needed; then
  rm -f "$SENTINEL"
  osascript -e "tell application \"Terminal\" to do script \"bash '${SCRIPT_DIR}/setup_agent.sh'; exit\""
  osascript -e "tell application \"Terminal\" to activate"
  until [ -f "$SENTINEL" ]; do sleep 2; done
fi

exec > /dev/null 2>&1

# ── Ensure Ollama is running before starting Rekki ───────────────────────
if command -v ollama &>/dev/null && ! pgrep -x ollama &>/dev/null; then
  nohup ollama serve >> "$LOG" 2>&1 &
fi

source "$VENV/bin/activate"

# Optional private agent env file (kept local and ignored)
if [ -f "$AGENT_ENV_FILE" ]; then
  # shellcheck source=/dev/null
  source "$AGENT_ENV_FILE"
fi

# Agent mode defaults
export REKITBOX_AGENT_MODE="1"
export REKIT_AGENT_PROVIDER="${REKIT_AGENT_PROVIDER:-ollama}"
export REKIT_AGENT_PROFILE="${REKIT_AGENT_PROFILE:-cl}"

cd "$SCRIPT_DIR" || exit 1
git pull origin main --ff-only >> "$LOG" 2>&1

if command -v tailscale &>/dev/null; then
  tailscale up --accept-routes >> "$LOG" 2>&1 &
fi

nohup "$VENV/bin/python" "$SCRIPT_DIR/main.py" >> "$LOG" 2>&1 &

if [ -t 0 ]; then
  osascript -e 'tell application "Terminal" to close front window' > /dev/null 2>&1 &
fi
