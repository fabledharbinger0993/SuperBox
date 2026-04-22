"""
rekki/task_classifier.py — Task Classifier

Classifies free-text input into one of five task types so the Sovern
orchestrator can route it to the appropriate specialist layer.

Types (evaluated in priority order):
  write-gate    — RekordBox DB write operations (highest priority)
  code          — implementation, writing, refactoring, testing
  system-action — execution, deployment, installation, process control
  research      — retrieval, lookup, search, explanation queries
  reasoning     — synthesis, deliberation, analysis (default fallback)

Ported from RekkiClaw/src/rekki/task-classifier.ts.
"""

from __future__ import annotations

from typing import Literal

TaskType = Literal["research", "reasoning", "code", "system-action", "write-gate"]

AGENT_LABEL: dict = {
    "research":      "RESEARCH",
    "reasoning":     "REASONING",
    "code":          "CODE",
    "system-action": "SYSTEM",
    "write-gate":    "WRITE-GATE",
}


# ─── Keyword Maps ─────────────────────────────────────────────────────────────

_WRITE_GATE = (
    # Generic rekordbox DB writes
    "write to rekordbox", "write to rekitbox", "update track", "modify playlist",
    "edit bpm", "rekordbox write", "rekitbox write", "update rekordbox",
    "update rekitbox", "set track", "change bpm",
    # Tool-specific write operations (mirrors RekitBox tool registry)
    "fix paths", "relocate", "repair paths", "import tracks",
    "import to rekordbox", "add to library", "link playlists",
    "rebuild playlists", "prune duplicates", "delete duplicates",
    "remove duplicates", "organize library", "organise library",
    "defrag library", "reorganize", "reorganise", "assimilate",
    "rename tracks", "rename files", "batch rename",
)

_CODE = (
    "implement", "write a test", "write test", "build a", "create function",
    "refactor", "fix bug", "fix the bug", "add test", "add a test",
    "scaffold", "write the", "write a",
)

_SYSTEM_ACTION = (
    "run the", "run a", "execute", "deploy", "install",
    "start the", "stop the", "delete", "kill", "build and", "restart", "launch",
)

_RESEARCH = (
    "what is", "how does", "who is", "when did", "find", "look up",
    "search", "show me", "list", "where is", "find all",
)


# ─── Classifier ───────────────────────────────────────────────────────────────

def classify_task(input_text: str) -> TaskType:
    """
    Classify free-text into a TaskType.
    Falls back to 'reasoning' when no keyword pattern matches.
    """
    lower = input_text.lower()

    if _matches(lower, _WRITE_GATE):
        return "write-gate"
    if _matches(lower, _CODE):
        return "code"
    if _matches(lower, _SYSTEM_ACTION):
        return "system-action"
    if _matches(lower, _RESEARCH):
        return "research"
    return "reasoning"


def _matches(text: str, keywords: tuple) -> bool:
    return any(kw in text for kw in keywords)
