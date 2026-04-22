"""
rekki/ — Rekki brain: Sovern P→C→E scaffold + HologrA.I.m memory.

Ported from FabledClaw/src/rekki/ (TypeScript) to native Python.
Lives inside RekitBox; SQLite stored at data/rekki-memory.db.

Modules
-------
  db              — 9-table SQLite persistence (RekkiMemoryDB)
  congress        — multi-perspective deliberation engine (Congress)
  recall          — memory helpers (recall_memory, create_memory)
  review          — background tribunal (run_tribunal)
  paradigm        — user arc + trust tracking (ParadigmContext)
  ego             — output shaping + Skeptic's Veto (EgoOutput)
  task_classifier — keyword-based task routing (classify_task)
  write_gate      — four-stage write approval (stage_plan / approve_plan / execute_plan)
  execution_gate  — confidence + policy gate (assess_execution)
  sovern          — top-level P→C→E facade (run_sovern)
"""

# Core layers — imported for convenient package-level access
from rekki.db import get_memory_db  # noqa: F401
from rekki.congress import Congress, CongressOption  # noqa: F401
from rekki.paradigm import ParadigmContext, create_paradigm  # noqa: F401
from rekki.ego import EgoOutput, shape_output  # noqa: F401
from rekki.task_classifier import classify_task, TaskType  # noqa: F401
from rekki.write_gate import stage_plan, approve_plan, execute_plan  # noqa: F401
from rekki.execution_gate import assess_execution, ExecutionRequest  # noqa: F401
from rekki.sovern import run_sovern, SovernRequest, SovernResult  # noqa: F401
