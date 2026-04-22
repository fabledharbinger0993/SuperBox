"""
rekki/ego.py — Ego Output Shaping & Coherence

The Ego layer shapes outward messages with attention to relational context,
ongoing history, and internal coherence.  It exercises the Skeptic's Veto
when output would contradict a stated obligation or degrade into appeasement.

"Never sacrifice coherence to produce a more comfortable response."
— Mojo-Dojo non-negotiable

Ported from RekkiClaw/src/rekki/ego.ts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

from rekki.paradigm import ParadigmContext, compute_trust_level

CoherenceVerdict = Literal["coherent", "appeasement-risk", "contradicts-prior"]

_APPEASEMENT_PHRASES = (
    "absolutely",
    "of course",
    "sure thing",
    "no problem",
    "certainly",
    "totally",
    "sounds great",
    "great idea",
    "perfect",
    "right away",
)


# ─── Data Class ───────────────────────────────────────────────────────────────

@dataclass
class EgoOutput:
    message: str
    coherence_verdict: CoherenceVerdict
    skeptics_veto: bool = False
    relational_note: Optional[str] = None


# ─── Coherence Check ─────────────────────────────────────────────────────────

def check_coherence(draft: str, obligations: list) -> CoherenceVerdict:
    """
    Evaluate a draft message for internal coherence.

    contradicts-prior — draft keywords overlap with a stated obligation
    appeasement-risk  — draft contains 2+ approval-seeking phrases
    coherent          — message looks clear and substantive
    """
    lower = draft.lower()

    for obligation in obligations:
        keywords = [w for w in obligation.lower().split() if len(w) > 3]
        if any(kw in lower for kw in keywords):
            return "contradicts-prior"

    hits = sum(1 for p in _APPEASEMENT_PHRASES if p in lower)
    if hits >= 2:
        return "appeasement-risk"

    return "coherent"


# ─── Skeptic's Veto ───────────────────────────────────────────────────────────

def apply_skeptics_veto(output: EgoOutput) -> EgoOutput:
    """
    Prepend a visible warning and mark the output vetoed.
    Idempotent — no-op if already vetoed.
    """
    if output.skeptics_veto:
        return output
    return EgoOutput(
        message=(
            "[Skeptic's Veto] This response may conflict with a stated obligation or "
            "rely on appeasement rather than substance. Review before proceeding.\n\n"
            + output.message
        ),
        coherence_verdict=output.coherence_verdict,
        skeptics_veto=True,
        relational_note=output.relational_note,
    )


# ─── Output Shaping ───────────────────────────────────────────────────────────

def shape_output(
    draft: str,
    paradigm: ParadigmContext,
    congress_confidence: float,
) -> EgoOutput:
    """
    Shape a draft through the Ego layer.

    - Checks coherence against Paradigm obligations
    - Adds a relational note for persistent low-trust patterns
    - Auto-applies Skeptic's Veto for contradictions or weak appeasement
    """
    verdict = check_coherence(draft, paradigm.obligations)
    trust = compute_trust_level(paradigm.arc)

    relational_note: Optional[str] = None
    if trust == "low" and paradigm.arc.override_count > 3:
        relational_note = (
            f"Note: You have made {paradigm.arc.override_count} override decisions "
            "this session without requesting critique. Engaging with agent feedback "
            "before each override tends to improve outcome quality."
        )

    output = EgoOutput(
        message=draft,
        coherence_verdict=verdict,
        skeptics_veto=False,
        relational_note=relational_note,
    )

    if verdict == "contradicts-prior":
        output = apply_skeptics_veto(output)

    if verdict == "appeasement-risk" and congress_confidence <= 0.5:
        output = apply_skeptics_veto(output)

    return output
