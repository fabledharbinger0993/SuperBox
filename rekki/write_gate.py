"""
rekki/write_gate.py — Write Gate: Four-Stage RekitBox Write Approval

Implements the four-stage execution model for any RekordBox DB write:
  1. stage_plan   — dry-run validation, pre-integrity checks, rollback script
  2. approve_plan — explicit named approval checkpoint
  3. execute_plan — run write + post-op integrity checks + audit record
  4. (audit persisted to tool_decisions by the caller via rekki.db)

All write operations targeting rekordbox_db must pass through this gate.
The executor hook in execute_plan is where real DB writes are wired in —
the stage/approve/audit structure is fully operational without it.

Ported from RekkiClaw/src/rekki/write-gate.ts.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Literal, Optional


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ─── Data Classes ─────────────────────────────────────────────────────────────

@dataclass
class FieldChange:
    """A single field change within an operation."""
    field_name: str
    from_value: Any
    to_value: Any
    track_id: Optional[str] = None
    playlist_id: Optional[str] = None


@dataclass
class WriteGateRequest:
    operation_type: str
    description: str
    target: str
    changes: list = field(default_factory=list)  # list[FieldChange]
    force_check_failure: bool = False


@dataclass
class ApprovalRecord:
    approved_by: str
    timestamp: str
    reason: Optional[str] = None


@dataclass
class Validation:
    check: str
    result: bool
    message: str = ""


@dataclass
class CheckResult:
    timestamp: str
    passed: bool
    validations: list = field(default_factory=list)  # list[Validation]


WriteGatePlanStatus = Literal["pending-approval", "approved", "blocked", "executed", "failed"]


@dataclass
class WriteGatePlan:
    plan_id: str
    status: WriteGatePlanStatus
    description: str
    operation_type: str
    target: str
    changes: list  # list[FieldChange]
    precheck: CheckResult
    rollback_script: str
    created_at: str
    approval: Optional[ApprovalRecord] = None


@dataclass
class ExecuteResult:
    plan: WriteGatePlan
    postcheck: CheckResult
    audit: dict  # structured for rekki.db.insert_tool_decision()


# ─── Stage 1: Dry-run + Pre-integrity ────────────────────────────────────────

def stage_plan(req: WriteGateRequest) -> WriteGatePlan:
    """
    Validate inputs, run pre-integrity checks, build rollback script.
    Returns a plan in 'pending-approval' or 'blocked' status.
    """
    plan_id = str(uuid.uuid4())
    precheck = _run_precheck(req)
    status: WriteGatePlanStatus = "pending-approval" if precheck.passed else "blocked"

    return WriteGatePlan(
        plan_id=plan_id,
        status=status,
        description=req.description,
        operation_type=req.operation_type,
        target=req.target,
        changes=req.changes,
        precheck=precheck,
        rollback_script=_build_rollback_script(req),
        created_at=_now(),
    )


# ─── Stage 2: Approval Checkpoint ────────────────────────────────────────────

def approve_plan(
    plan: WriteGatePlan,
    approved_by: str,
    reason: Optional[str] = None,
) -> WriteGatePlan:
    """
    Grant explicit approval for a staged plan.
    Raises ValueError if the plan is not in 'pending-approval' status.
    """
    if plan.status != "pending-approval":
        raise ValueError(
            f"Cannot approve a '{plan.status}' plan (plan_id: {plan.plan_id}). "
            "Only 'pending-approval' plans can be approved."
        )
    from dataclasses import replace
    return replace(
        plan,
        status="approved",
        approval=ApprovalRecord(
            approved_by=approved_by,
            timestamp=_now(),
            reason=reason,
        ),
    )


# ─── Stage 3: Execute + Post-integrity ───────────────────────────────────────

def execute_plan(
    plan: WriteGatePlan,
    executor: Optional[Callable[[WriteGatePlan], None]] = None,
) -> ExecuteResult:
    """
    Execute an approved plan.

    executor: optional callable(plan) → None that performs the real DB write.
              When None, the gate structure runs fully but no write occurs
              (use for dry-run confirmation flows).

    Raises ValueError if the plan is not in 'approved' status.
    """
    if plan.status != "approved":
        raise ValueError(
            f"Plan must be approved before execution "
            f"(current status: '{plan.status}', plan_id: {plan.plan_id})."
        )

    exec_start = time.monotonic()
    exec_ts = _now()

    if executor is not None:
        executor(plan)

    duration_ms = int((time.monotonic() - exec_start) * 1000)
    postcheck = _run_postcheck(plan)

    from dataclasses import replace
    executed_plan = replace(plan, status="executed" if postcheck.passed else "failed")

    audit = {
        "tool_id": plan.operation_type,
        "task_type": "write",
        "risk_level": "high" if plan.target == "rekordbox_db" else "moderate",
        "action": "dispatched",
        "write_gate_invoked": 1,
        "write_gate_stage": "execute",
        "reasoning": plan.description,
        "outcome": "success" if postcheck.passed else "failure",
        "duration_ms": duration_ms,
    }

    return ExecuteResult(plan=executed_plan, postcheck=postcheck, audit=audit)


# ─── Pre-integrity Checks ─────────────────────────────────────────────────────

def _run_precheck(req: WriteGateRequest) -> CheckResult:
    ts = _now()
    validations = []

    if req.force_check_failure:
        validations.append(Validation(
            "forced-failure", False, "Test hook: force_check_failure=True"
        ))
        return CheckResult(ts, False, validations)

    has_changes = len(req.changes) > 0
    validations.append(Validation(
        "changes-present",
        has_changes,
        f"{len(req.changes)} change(s) staged"
        if has_changes else "No changes provided — nothing to execute",
    ))

    all_have_field = all(bool(c.field_name) for c in req.changes)
    validations.append(Validation(
        "changes-have-field",
        all_have_field,
        "All changes specify a field name"
        if all_have_field else "One or more changes are missing a field name",
    ))

    has_target = bool(req.target)
    validations.append(Validation(
        "target-specified",
        has_target,
        f"Target: {req.target}" if has_target else "No target resource specified",
    ))

    rollbackable = all(c.from_value is not None for c in req.changes)
    validations.append(Validation(
        "rollback-feasible",
        rollbackable,
        "All changes have a from_value — rollback script can be generated"
        if rollbackable else "Some changes are missing from_value — rollback not guaranteed",
    ))

    return CheckResult(ts, all(v.result for v in validations), validations)


# ─── Post-integrity Checks ────────────────────────────────────────────────────

def _run_postcheck(plan: WriteGatePlan) -> CheckResult:
    ts = _now()
    validations = [
        Validation(
            "approval-on-record",
            plan.approval is not None,
            f"Approved by {plan.approval.approved_by} at {plan.approval.timestamp}"
            if plan.approval else "No approval record — execution integrity in question",
        ),
        Validation(
            "change-count-consistent",
            len(plan.changes) > 0,
            f"{len(plan.changes)} change(s) executed against {plan.target}",
        ),
        Validation(
            "plan-id-present",
            bool(plan.plan_id),
            f"Plan ID: {plan.plan_id}" if plan.plan_id else "No plan ID — audit trail incomplete",
        ),
    ]
    return CheckResult(ts, all(v.result for v in validations), validations)


# ─── Rollback Script Generation ───────────────────────────────────────────────

def _build_rollback_script(req: WriteGateRequest) -> str:
    if not req.changes:
        return f"-- Rollback for: {req.description}\n-- No changes to reverse."

    lines = [
        f"-- Rollback script for: {req.description}",
        f"-- Target: {req.target}",
        f"-- Generated: {_now()}",
        f"-- Changes to reverse: {len(req.changes)}",
        "",
    ]

    for change in req.changes:
        if change.track_id:
            id_str = f"track {change.track_id}"
            where = f"WHERE id = '{change.track_id}'"
        elif change.playlist_id:
            id_str = f"playlist {change.playlist_id}"
            where = f"WHERE id = '{change.playlist_id}'"
        else:
            id_str = "record"
            where = ""

        lines.append(
            f"-- Revert {id_str}: "
            f"set {change.field_name} = {change.from_value!r} (was: {change.to_value!r})"
        )
        lines.append(
            f"UPDATE {req.target} SET {change.field_name} = {change.from_value!r} {where};".strip()
        )

    return "\n".join(lines)
