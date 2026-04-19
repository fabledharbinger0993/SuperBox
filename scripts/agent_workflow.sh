#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

HAS_GIT=1
if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  HAS_GIT=0
fi

VENV_DIR="${REKIT_AGENT_VENV:-$REPO_ROOT/../venv}"
PYTHON_BIN="${REKIT_AGENT_PYTHON:-$VENV_DIR/bin/python}"
INTERVAL="${REKIT_AGENT_INTERVAL:-180}"
STATE_DIR="${REKIT_AGENT_STATE_DIR:-$REPO_ROOT/.git/agent-workflow}"
PID_FILE="$STATE_DIR/watch.pid"
LOG_FILE="$STATE_DIR/watch.log"
CONTEXT_FILE="$STATE_DIR/context.txt"
RESPONSE_FILE="$STATE_DIR/response.json"
PATCH_FILE="$STATE_DIR/patch.diff"

AUTO_APPLY="${REKIT_AGENT_AUTO_APPLY:-0}"
AUTO_COMMIT="${REKIT_AGENT_AUTO_COMMIT:-0}"
AUTO_PUSH="${REKIT_AGENT_AUTO_PUSH:-0}"
SYNC_PUBLIC="${REKIT_AGENT_SYNC_PUBLIC:-0}"
PROMPT_PROFILE="${REKIT_AGENT_PROFILE:-default}"

mkdir -p "$STATE_DIR"

log() {
  echo "[agent] $*"
}

is_running() {
  if [[ ! -f "$PID_FILE" ]]; then
    return 1
  fi
  local pid
  pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [[ -z "$pid" ]]; then
    return 1
  fi
  if kill -0 "$pid" >/dev/null 2>&1; then
    return 0
  fi
  rm -f "$PID_FILE"
  return 1
}

ensure_python() {
  if [[ -x "$PYTHON_BIN" ]]; then
    return 0
  fi

  if command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v python3)"
    log "venv python missing, falling back to $PYTHON_BIN"
    return 0
  fi

  log "python not found at $PYTHON_BIN and python3 is unavailable"
  log "Set REKIT_AGENT_VENV or REKIT_AGENT_PYTHON, then retry."
  return 1
}

write_context() {
  local compile_log test_log
  compile_log="$STATE_DIR/compile.log"
  test_log="$STATE_DIR/tests.log"

  {
    echo "# RekitBox Agent Context"
    echo "generated_at=$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
    if [[ "$HAS_GIT" == "1" ]]; then
      echo "branch=$(git rev-parse --abbrev-ref HEAD)"
    else
      echo "branch=none (non-git workspace)"
    fi
    echo
    echo "## git status --porcelain"
    if [[ "$HAS_GIT" == "1" ]]; then
      git status --porcelain --untracked-files=all || true
    else
      echo "skipped (non-git workspace)"
    fi
    echo
    echo "## git diff --stat"
    if [[ "$HAS_GIT" == "1" ]]; then
      git diff --stat || true
    else
      echo "skipped (non-git workspace)"
    fi
    echo
    echo "## recent commits"
    if [[ "$HAS_GIT" == "1" ]]; then
      git --no-pager log --oneline -n 8 || true
    else
      echo "skipped (non-git workspace)"
    fi
    echo
    echo "## compile check"
  } > "$CONTEXT_FILE"

  "$PYTHON_BIN" -m compileall -q "$REPO_ROOT" >"$compile_log" 2>&1 || true
  cat "$compile_log" >> "$CONTEXT_FILE"

  {
    echo
    echo "## pytest check"
  } >> "$CONTEXT_FILE"

  if [[ -d "$REPO_ROOT/tests" ]] && "$PYTHON_BIN" -m pip show pytest >/dev/null 2>&1; then
    "$PYTHON_BIN" -m pytest -q >"$test_log" 2>&1 || true
    cat "$test_log" >> "$CONTEXT_FILE"
  else
    echo "pytest skipped (tests/ missing or pytest not installed)" >> "$CONTEXT_FILE"
  fi

  {
    echo
    echo "## unstaged diff (truncated)"
    if [[ "$HAS_GIT" == "1" ]]; then
      git --no-pager diff -- . ':(exclude).git' | head -n 500 || true
    else
      echo "skipped (non-git workspace)"
    fi
  } >> "$CONTEXT_FILE"
}

run_agent() {
  "$PYTHON_BIN" "$REPO_ROOT/scripts/rekit_agent.py" \
    --context "$CONTEXT_FILE" \
    --output "$RESPONSE_FILE" \
    --prompt-profile "$PROMPT_PROFILE"
}

extract_json_field() {
  local field="$1"
  "$PYTHON_BIN" - "$RESPONSE_FILE" "$field" <<'PY'
import json
import sys

path = sys.argv[1]
field = sys.argv[2]
with open(path, "r", encoding="utf-8") as fh:
    data = json.load(fh)
print(data.get(field, ""))
PY
}

apply_patch_if_any() {
  if [[ "$HAS_GIT" != "1" ]]; then
    log "patch apply requires git repository; skipping"
    return 1
  fi

  local patch_text
  patch_text="$(extract_json_field patch)"
  if [[ -z "$patch_text" ]]; then
    log "no patch proposed"
    return 0
  fi

  printf '%s\n' "$patch_text" > "$PATCH_FILE"
  if git apply --whitespace=nowarn "$PATCH_FILE"; then
    log "patch applied"
    return 0
  fi

  log "patch failed to apply"
  return 1
}

quick_verify() {
  local verify_log
  verify_log="$STATE_DIR/verify.log"
  "$PYTHON_BIN" -m compileall -q "$REPO_ROOT" >"$verify_log" 2>&1 || true
  if [[ -s "$verify_log" ]]; then
    log "verify found issues"
    cat "$verify_log"
    return 1
  fi
  log "verify passed"
  return 0
}

commit_and_push_if_enabled() {
  if [[ "$HAS_GIT" != "1" ]]; then
    log "commit/push skipped (non-git workspace)"
    return 0
  fi

  if [[ -z "$(git status --porcelain --untracked-files=all)" ]]; then
    log "no changes to commit"
    return 0
  fi

  if [[ "$AUTO_COMMIT" != "1" ]]; then
    log "changes present; auto-commit disabled"
    return 0
  fi

  local msg
  msg="$(extract_json_field commit_message)"
  if [[ -z "$msg" ]]; then
    msg="agent: subtle maintenance fix"
  fi

  git add -A
  git commit -m "$msg"
  log "committed: $msg"

  if [[ "$AUTO_PUSH" == "1" ]]; then
    git push
    log "pushed"
  else
    log "auto-push disabled"
  fi

  if [[ "$SYNC_PUBLIC" == "1" ]]; then
    "$REPO_ROOT/scripts/sync_public_repo.sh" once
  fi
}

run_once() {
  ensure_python || return 1
  log "collecting diagnostics"
  write_context
  log "querying provider=${REKIT_AGENT_PROVIDER:-ollama} model=${REKIT_AGENT_MODEL:-qwen2.5-coder:7b} profile=$PROMPT_PROFILE"
  run_agent

  local summary confidence notes
  summary="$(extract_json_field summary)"
  confidence="$(extract_json_field confidence)"
  notes="$(extract_json_field notes)"
  log "summary: ${summary:-none}"
  log "confidence: ${confidence:-unknown}"
  if [[ -n "$notes" ]]; then
    log "notes: $notes"
  fi

  if [[ "$AUTO_APPLY" == "1" ]]; then
    if apply_patch_if_any && quick_verify; then
      commit_and_push_if_enabled
    else
      log "apply/verify failed; leaving changes untouched"
      return 1
    fi
  else
    log "auto-apply disabled; response saved to $RESPONSE_FILE"
  fi
}

watch_loop() {
  trap 'log "stop requested"; exit 0' INT TERM
  log "watching (interval=${INTERVAL}s)"
  while true; do
    if ! run_once; then
      log "cycle failed; retrying later"
    fi
    sleep "$INTERVAL"
  done
}

start_daemon() {
  if is_running; then
    log "already running (pid $(cat "$PID_FILE"))"
    return 0
  fi
  touch "$LOG_FILE"
  nohup "$0" watch >>"$LOG_FILE" 2>&1 &
  echo "$!" > "$PID_FILE"
  log "started (pid $(cat "$PID_FILE"))"
  log "log: $LOG_FILE"
}

stop_daemon() {
  if ! is_running; then
    log "not running"
    rm -f "$PID_FILE"
    return 0
  fi
  local pid
  pid="$(cat "$PID_FILE")"
  kill "$pid" >/dev/null 2>&1 || true
  rm -f "$PID_FILE"
  log "stopped"
}

show_status() {
  if is_running; then
    log "running (pid $(cat "$PID_FILE"))"
  else
    log "not running"
  fi
  log "python: $PYTHON_BIN"
  log "provider: ${REKIT_AGENT_PROVIDER:-ollama}"
  log "model: ${REKIT_AGENT_MODEL:-qwen2.5-coder:7b}"
  log "profile: $PROMPT_PROFILE"
  log "auto_apply=$AUTO_APPLY auto_commit=$AUTO_COMMIT auto_push=$AUTO_PUSH sync_public=$SYNC_PUBLIC"
  log "state: $STATE_DIR"
  if [[ "$HAS_GIT" == "1" ]]; then
    log "git: enabled"
  else
    log "git: disabled (non-git workspace)"
  fi
}

case "${1:-}" in
  once)
    run_once
    ;;
  watch)
    watch_loop
    ;;
  start)
    start_daemon
    ;;
  stop)
    stop_daemon
    ;;
  status)
    show_status
    ;;
  *)
    cat <<EOF
Usage: ./scripts/agent_workflow.sh <once|watch|start|stop|status>

Environment flags:
  REKIT_AGENT_PROVIDER=ollama|openai
  REKIT_AGENT_MODEL=<model-name>
  REKIT_AGENT_PROFILE=default|cl|is|reviewpr
  REKIT_AGENT_AUTO_APPLY=0|1
  REKIT_AGENT_AUTO_COMMIT=0|1
  REKIT_AGENT_AUTO_PUSH=0|1
  REKIT_AGENT_SYNC_PUBLIC=0|1
EOF
    exit 2
    ;;
esac
