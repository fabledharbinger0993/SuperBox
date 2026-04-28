"""
RekitBox / state_tracker.py
Per-library persistent step tracking.
Stored as <library_root>/.rekitbox_state.json
Created lazily on first use.
"""

import json
import logging
import os
from pathlib import Path
from datetime import datetime, timezone

STATE_FILENAME = ".rekitbox_state.json"
log = logging.getLogger(__name__)

def _state_path(library_root: str) -> Path:
    """Return the state file path inside the library root."""
    return Path(library_root).resolve() / STATE_FILENAME

def load_state(library_root: str) -> dict:
    """Load or return fresh default state."""
    path = _state_path(library_root)
    if not path.exists():
        return {
            "library_root": str(library_root),
            "rekitbox_version": "1.4.0",
            "steps_completed": {},
            "last_updated": datetime.now(timezone.utc).isoformat()
        }
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        # corrupt file → graceful fallback
        return {"library_root": str(library_root), "steps_completed": {}}




def save_state(library_root: str, state: dict):
    """Save with timestamp and ensure parent dir exists."""
    path = _state_path(library_root)
    # Guard: never write to filesystem roots or non-writable system dirs
    if not os.access(path.parent, os.W_OK):
        log.warning("state_tracker: %s is not writable — skipping state save", path.parent)
        return
    state["last_updated"] = datetime.now(timezone.utc).isoformat()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    except OSError as exc:
        log.warning("state_tracker: could not write state file %s — %s", path, exc)

def mark_step_complete(library_root: str, step: str, exit_code: int):
    """Journal success/failure for a step. Safe no-op if no root."""
    if not library_root or not str(library_root).strip():
        return
    state = load_state(library_root)
    if "steps_completed" not in state:
        state["steps_completed"] = {}
    state["steps_completed"][step] = {
        "last_run": datetime.now(timezone.utc).isoformat(),
        "exit_code": exit_code
    }
    save_state(library_root, state)

def get_step_status(library_root: str) -> dict:
    """Return only the steps_completed dict (UI-friendly)."""
    if not library_root:
        return {}
    state = load_state(library_root)
    return state.get("steps_completed", {})
