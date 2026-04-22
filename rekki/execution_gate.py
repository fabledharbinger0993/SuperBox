"""
rekki/execution_gate.py — Execution Gate + Calm-Mode Policy

Combines Congress deliberation with environment-driven policy to gate
every operation before it runs.  The three blocking criteria are:

  1. Congress confidence below CONFIDENCE_THRESHOLD (0.55)
  2. Calm-mode policy: rekordbox_db writes blocked when
     REKKI_CALM_MODE_ENABLED=true
  3. Write to rekordbox_db with no rollback mechanism available

Feature flags (env vars, default off):
  REKKI_POLICY_ENABLED      — master switch; when off, fast-path allows all
  REKKI_CALM_MODE_ENABLED   — enables rekordbox_db write blocking

Consolidated from RekkiClaw/src/rekki/execution-gate.ts + calm-mode.ts.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Literal, Optional

from rekki.congress import Congress, CongressOption

CONFIDENCE_THRESHOLD = 0.55

DenialSource = Literal["low-confidence", "no-rollback", "calm-mode-policy"]


# ─── Data Classes ─────────────────────────────────────────────────────────────

@dataclass
class ExecutionRequest:
    operation_type: str
    description: str
    is_write: bool
    target: Optional[str] = None
    constraints: list = field(default_factory=list)
    rollback_available: bool = False
    force_confidence: Optional[float] = None  # test hook only


@dataclass
class ExecutionDenial:
    reason: str
    next_steps: list
    source: DenialSource


@dataclass
class ExecutionResult:
    allowed: bool
    decision: dict
    denial: Optional[ExecutionDenial] = None


# ─── Feature Flags ────────────────────────────────────────────────────────────

def _policy_enabled() -> bool:
    return os.environ.get("REKKI_POLICY_ENABLED", "").lower() == "true"


def _calm_mode_enabled() -> bool:
    return os.environ.get("REKKI_CALM_MODE_ENABLED", "").lower() == "true"


# ─── Next-Step Guidance ────────────────────────────────────────────────────────

_NEXT_STEPS: dict = {
    "low-confidence": [
        "Break the operation into smaller, independently reversible steps.",
        "Add a dry-run/preview mode and confirm output before committing.",
        "Provide an explicit backup of the target resource before proceeding.",
        "Request Congress deliberation with additional context to raise confidence.",
    ],
    "no-rollback": [
        "Create a full backup of the target resource before writing.",
        "Implement a staging area — write to a copy, verify, then swap.",
        "Add a rollback script and confirm it works before running the write.",
    ],
    "calm-mode-policy": [
        "Set REKKI_CALM_MODE_ENABLED=false to bypass calm-mode (admin only).",
        "Stage the operation against a test database first.",
        "Provide explicit approval via the Rekki approval workflow.",
    ],
}


# ─── Main Gate ────────────────────────────────────────────────────────────────

def assess_execution(request: ExecutionRequest) -> ExecutionResult:
    """
    Decide whether an operation should be allowed to proceed.

    Fast path: both policy flags off → allow everything (backward-compat).
    Otherwise:
      read-only ops  → pass through after Congress deliberation
      write ops      → check confidence, calm-mode, and rollback availability
    """
    policy_on = _policy_enabled()
    calm_on = _calm_mode_enabled()

    # Fast path: both flags off
    if not policy_on and not calm_on:
        return ExecutionResult(
            allowed=True,
            decision={
                "mode": "fast-path",
                "rationale": "Rekki policy disabled — using default behavior",
                "confidence": 1.0,
                "dissent": [],
            },
        )

    # Run Congress deliberation
    decision = _run_congress(request)

    # Test hook: override confidence
    if request.force_confidence is not None:
        decision = {**decision, "confidence": request.force_confidence}

    confidence = float(decision.get("confidence") or 0.0)

    # Read-only → pass through after deliberation
    if not request.is_write:
        return ExecutionResult(allowed=True, decision=decision)

    # Low-confidence block
    if confidence < CONFIDENCE_THRESHOLD:
        return ExecutionResult(
            allowed=False,
            decision=decision,
            denial=ExecutionDenial(
                reason=(
                    f"Congress confidence too low to proceed "
                    f"({confidence * 100:.0f}% < {CONFIDENCE_THRESHOLD * 100:.0f}% threshold). "
                    f"Operation: \"{request.description}\""
                ),
                next_steps=_NEXT_STEPS["low-confidence"],
                source="low-confidence",
            ),
        )

    # Calm-mode policy check
    if calm_on and request.target == "rekordbox_db":
        return ExecutionResult(
            allowed=False,
            decision=decision,
            denial=ExecutionDenial(
                reason=(
                    f"Calm-mode policy blocked \"{request.operation_type}\": "
                    "direct RekordBox DB write requires staging and approval."
                ),
                next_steps=_NEXT_STEPS["calm-mode-policy"],
                source="calm-mode-policy",
            ),
        )

    # No-rollback block for rekordbox_db
    if not request.rollback_available and request.target == "rekordbox_db":
        return ExecutionResult(
            allowed=False,
            decision=decision,
            denial=ExecutionDenial(
                reason=(
                    f"Write to \"{request.target}\" blocked: "
                    "no rollback mechanism available. Data loss cannot be reversed."
                ),
                next_steps=_NEXT_STEPS["no-rollback"],
                source="no-rollback",
            ),
        )

    return ExecutionResult(allowed=True, decision=decision)


# ─── Congress Helper ──────────────────────────────────────────────────────────

def _run_congress(request: ExecutionRequest) -> dict:
    """Build Congress options from the execution request and deliberate."""
    congress = Congress()

    if request.is_write:
        options = [
            CongressOption(
                f"{request.operation_type}-proceed",
                f"{request.operation_type}: proceed directly",
                request.description,
            ),
            CongressOption(
                f"{request.operation_type}-stage",
                f"{request.operation_type}: stage and preview first",
                "reversible",
            ),
            CongressOption(
                f"{request.operation_type}-abort",
                f"{request.operation_type}: abort for review",
            ),
        ]
    else:
        options = [
            CongressOption(
                f"{request.operation_type}-proceed",
                f"{request.operation_type}: proceed",
                "read-only",
            ),
            CongressOption(
                f"{request.operation_type}-abort",
                f"{request.operation_type}: abort",
            ),
        ]

    result = congress.deliberate(
        context=request.description,
        options=options,
        constraints=request.constraints,
    )
    return result.to_dict()
