"""
rekki/sovern.py — Sovern P→C→E Facade

Top-level entry point for every Rekki response cycle.
Wires the three scaffold layers together in order:

  Paradigm  → tracks user arc, trust, context
  Congress  → deliberates over options, produces a decision with confidence
  Ego       → shapes the output, applies Skeptic's Veto when warranted

Usage:
    from rekki.sovern import SovernRequest, run_sovern

    result = run_sovern(SovernRequest(
        input="find compatible tracks for this set",
        options=["search by key+BPM", "search by genre", "search by energy"],
        session_id="abc123",
    ))
    # result.ego_output.message is the final shaped response

Ported from RekkiClaw/src/rekki/sovern.ts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from rekki.congress import Congress, CongressOption
from rekki.ego import EgoOutput, shape_output
from rekki.paradigm import (
    ParadigmContext,
    create_paradigm,
    record_intent,
)
from rekki.task_classifier import AGENT_LABEL, TaskType, classify_task


# ─── Data Classes ─────────────────────────────────────────────────────────────

@dataclass
class SovernRequest:
    """
    A single Sovern orchestration cycle request.

    input           — raw user text or system prompt
    options         — candidate options for Congress to deliberate over
                      (if empty, Congress deliberates with a default proceed/abort pair)
    constraints     — Congress constraint labels (e.g. "large-batch", "db-write")
    session_id      — used to build / inherit Paradigm state
    paradigm        — pass an existing ParadigmContext to continue a session;
                      when None a fresh one is created from session_id
    force_task_type — skip classifier and use this TaskType directly
    """
    input: str
    options: list = field(default_factory=list)       # list[str]
    constraints: list = field(default_factory=list)   # list[str]
    session_id: str = "default"
    paradigm: Optional[ParadigmContext] = None
    force_task_type: Optional[TaskType] = None


@dataclass
class SovernResult:
    """
    The complete result of one Sovern cycle.

    task_type        — classified task (or forced)
    congress_result  — raw CongressDecision serialised to dict
    ego_output       — shaped, veto-checked final message
    paradigm         — updated ParadigmContext after recording intent
    routed_to        — AGENT_LABEL for the task type (for display)
    """
    task_type: TaskType
    congress_result: dict
    ego_output: EgoOutput
    paradigm: ParadigmContext
    routed_to: str


# ─── Main Entry Point ─────────────────────────────────────────────────────────

def run_sovern(req: SovernRequest) -> SovernResult:
    """
    Execute one full Sovern P→C→E cycle.

    Steps
    -----
    1. Paradigm — inherit or create a ParadigmContext
    2. Classify  — determine TaskType via keyword classifier (or forced)
    3. Congress  — deliberate over the supplied options
    4. Intent    — record the input in the Paradigm arc
    5. Ego       — shape + veto-check the Congress draft into a final message
    6. Return    — SovernResult containing all layers for downstream wiring
    """

    # ── 1. Paradigm ──────────────────────────────────────────────────────────
    paradigm = req.paradigm if req.paradigm is not None else create_paradigm(req.session_id)

    # ── 2. Classification ─────────────────────────────────────────────────────
    task_type: TaskType = req.force_task_type or classify_task(req.input)
    routed_to = AGENT_LABEL[task_type]

    # ── 3. Congress ───────────────────────────────────────────────────────────
    congress = Congress()

    if req.options:
        congress_options = [
            CongressOption(f"option-{i}", opt)
            for i, opt in enumerate(req.options)
        ]
    else:
        congress_options = [
            CongressOption("proceed", f"{task_type}: proceed"),
            CongressOption("abort",   f"{task_type}: abort"),
        ]

    congress_decision = congress.deliberate(
        context=req.input,
        options=congress_options,
        constraints=req.constraints,
    )
    congress_result = congress_decision.to_dict()

    # ── 4. Record Intent ──────────────────────────────────────────────────────
    updated_paradigm = record_intent(paradigm, req.input)

    # ── 5. Ego ────────────────────────────────────────────────────────────────
    draft = congress_result.get("rationale") or congress_result.get("winner") or req.input
    confidence = float(congress_result.get("confidence") or 0.0)
    ego_output = shape_output(draft, updated_paradigm, confidence)

    # ── 6. Return ─────────────────────────────────────────────────────────────
    return SovernResult(
        task_type=task_type,
        congress_result=congress_result,
        ego_output=ego_output,
        paradigm=updated_paradigm,
        routed_to=routed_to,
    )
