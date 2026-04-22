"""
rekki/paradigm.py — Paradigm Context Tracking

The Paradigm layer tracks the user's research arc, trust level, obligations,
architectural assumptions, and active knowledge domains.  It is the "state
of the world" that anchors every Congress deliberation.

Trust rules:
  high   — requested_critique_count >= 3 AND override_count <= 1
  low    — override_count > requested_critique_count * 2
  medium — everything else

All mutations return new instances (pure/immutable pattern).
Ported from RekkiClaw/src/rekki/paradigm.ts.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Literal

TrustLevel = Literal["low", "medium", "high"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ─── Data Classes ─────────────────────────────────────────────────────────────

@dataclass
class UserArc:
    session_id: str
    intent_history: list = field(default_factory=list)
    trust_level: TrustLevel = "medium"
    override_count: int = 0
    requested_critique_count: int = 0


@dataclass
class ParadigmContext:
    session_id: str
    arc: UserArc
    obligations: list = field(default_factory=list)
    architectural_assumptions: list = field(default_factory=list)
    knowledge_context: list = field(default_factory=list)
    updated_at: str = field(default_factory=_now)


# ─── Trust Computation ────────────────────────────────────────────────────────

def compute_trust_level(arc: UserArc) -> TrustLevel:
    """Derive trust level from the user arc."""
    if arc.requested_critique_count >= 3 and arc.override_count <= 1:
        return "high"
    if arc.override_count > arc.requested_critique_count * 2:
        return "low"
    return "medium"


# ─── Factory ──────────────────────────────────────────────────────────────────

def create_paradigm(session_id: str) -> ParadigmContext:
    """Create a fresh Paradigm context for a new session."""
    return ParadigmContext(session_id=session_id, arc=UserArc(session_id=session_id))


# ─── Arc Mutations (immutable) ────────────────────────────────────────────────

def record_intent(ctx: ParadigmContext, intent: str) -> ParadigmContext:
    """Append intent to history and re-evaluate trust level."""
    new_arc = replace(ctx.arc, intent_history=ctx.arc.intent_history + [intent])
    new_arc = replace(new_arc, trust_level=compute_trust_level(new_arc))
    return replace(ctx, arc=new_arc, updated_at=_now())


def record_override(ctx: ParadigmContext) -> ParadigmContext:
    """User overrode agent dissent — increment counter and re-evaluate trust."""
    new_arc = replace(ctx.arc, override_count=ctx.arc.override_count + 1)
    new_arc = replace(new_arc, trust_level=compute_trust_level(new_arc))
    return replace(ctx, arc=new_arc, updated_at=_now())


def record_critique_request(ctx: ParadigmContext) -> ParadigmContext:
    """User explicitly invited critique — increment counter and re-evaluate trust."""
    new_arc = replace(
        ctx.arc, requested_critique_count=ctx.arc.requested_critique_count + 1
    )
    new_arc = replace(new_arc, trust_level=compute_trust_level(new_arc))
    return replace(ctx, arc=new_arc, updated_at=_now())


# ─── Context Mutations (immutable) ───────────────────────────────────────────

def add_architectural_assumption(ctx: ParadigmContext, assumption: str) -> ParadigmContext:
    return replace(
        ctx,
        architectural_assumptions=ctx.architectural_assumptions + [assumption],
        updated_at=_now(),
    )


def add_obligation(ctx: ParadigmContext, obligation: str) -> ParadigmContext:
    return replace(ctx, obligations=ctx.obligations + [obligation], updated_at=_now())


def add_knowledge_context(ctx: ParadigmContext, topic: str) -> ParadigmContext:
    return replace(
        ctx, knowledge_context=ctx.knowledge_context + [topic], updated_at=_now()
    )
