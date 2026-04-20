"""
rekki/congress.py — Congress Internal Deliberation Engine

Multi-perspective scoring, dissent capture, and winner selection.
Ported from FabledClaw/src/rekki/congress.ts.

Congress gates every destructive RekitBox operation:
  - File relocations / path rewrites
  - Batch renames  (large-batch)
  - Duplicate deletions (irreversible + user-data-loss)
  - Rekordbox DB writes (db-write)
  - Schema migrations (schema-change)

Usage:
    from rekki.congress import Congress, CongressOption

    c = Congress()
    decision = c.deliberate(
        context="Relocate 450 tracks to new folder structure",
        options=[
            CongressOption("proceed", "Relocate all 450 tracks now"),
            CongressOption("dry_run", "Preview relocations without moving files"),
            CongressOption("abort",   "Cancel operation"),
        ],
        constraints=["large-batch", "irreversible"],
    )
    if decision["confidence"] >= 0.7 and decision["winner"] == "dry_run":
        ...
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Optional
from rekki.db import get_memory_db

# ─── Risk Vocabulary ──────────────────────────────────────────────────────────

RISKY_CONSTRAINTS: frozenset[str] = frozenset(
    {
        "irreversible",
        "large-batch",
        "no-backup",
        "user-data-loss",
        "db-write",
        "schema-change",
    }
)

DANGER_KEYWORDS: tuple[str, ...] = (
    "delete",
    "drop",
    "truncate",
    "destroy",
    "overwrite",
    "irreversible",
)

SAFE_KEYWORDS: tuple[str, ...] = (
    "read",
    "query",
    "list",
    "view",
    "dry-run",
    "dry_run",
    "stage",
    "preview",
    "archive",
    "backup",
    "export",
    "reversible",
    "cancel",
    "abort",
)

# ─── Data Types ───────────────────────────────────────────────────────────────

@dataclass
class CongressOption:
    id: str
    label: str
    description: str = ""


@dataclass
class ScoredOption:
    id: str
    label: str
    score: float
    rationale: str


@dataclass
class DissentNote:
    option_id: str
    concern: str
    severity: str  # "low" | "medium" | "high"


@dataclass
class CongressDecision:
    winner: Optional[str]
    confidence: float
    rationale: str
    options: list[ScoredOption] = field(default_factory=list)
    dissent: list[DissentNote] = field(default_factory=list)
    logic_entry_id: Optional[int] = None

    def to_dict(self) -> dict:
        return {
            "winner": self.winner,
            "confidence": self.confidence,
            "rationale": self.rationale,
            "options": [
                {"id": o.id, "label": o.label, "score": o.score, "rationale": o.rationale}
                for o in self.options
            ],
            "dissent": [
                {"option_id": d.option_id, "concern": d.concern, "severity": d.severity}
                for d in self.dissent
            ],
            "logic_entry_id": self.logic_entry_id,
        }

    @property
    def is_safe(self) -> bool:
        """True when confidence is high and no high-severity dissent was raised."""
        high_dissent = any(d.severity == "high" for d in self.dissent)
        return self.confidence >= 0.7 and not high_dissent

    @property
    def should_block(self) -> bool:
        """True when Congress recommends NOT proceeding (low confidence or high dissent)."""
        return not self.is_safe


# ─── Scoring ─────────────────────────────────────────────────────────────────

def _score_option(
    option: CongressOption,
    position: int,
    total_options: int,
    constraints: list[str],
) -> float:
    """
    Score an option's viability.  Higher = more viable.

    Factors
    -------
    - Position: gentle decay (first option is not always best)
    - Safe-keyword bonus
    - Danger-keyword penalty
    - Risky-constraint multiplier: each active risky constraint
      further penalises dangerous options and slightly rewards safe ones
    """
    text = f"{option.label} {option.description}".lower()

    # Base: gentle position decay
    score = 0.7 - (position / max(total_options - 1, 1)) * 0.1

    has_safe = any(kw in text for kw in SAFE_KEYWORDS)
    has_danger = any(kw in text for kw in DANGER_KEYWORDS)

    if has_safe:
        score += 0.12
    if has_danger:
        score -= 0.20

    active_risky = sum(1 for c in constraints if c in RISKY_CONSTRAINTS)
    if active_risky > 0:
        if has_danger:
            score -= active_risky * 0.10
        else:
            score += active_risky * 0.05

    return max(0.0, min(1.0, score))


def _build_rationale(option: CongressOption, constraints: list[str]) -> str:
    text = f"{option.label} {option.description}".lower()
    parts: list[str] = []

    for kw in SAFE_KEYWORDS:
        if kw in text:
            parts.append(f'contains safety signal "{kw}"')
            break
    for kw in DANGER_KEYWORDS:
        if kw in text:
            parts.append(f'contains risk signal "{kw}"')
            break

    active_risky = [c for c in constraints if c in RISKY_CONSTRAINTS]
    if active_risky:
        parts.append(f"active constraints: {', '.join(active_risky)}")

    return "; ".join(parts) if parts else "no notable risk signals"


def _build_dissent(
    winner: ScoredOption,
    all_scored: list[ScoredOption],
    constraints: list[str],
) -> list[DissentNote]:
    dissent: list[DissentNote] = []
    active_risky = [c for c in constraints if c in RISKY_CONSTRAINTS]
    if not active_risky:
        return dissent

    winner_text = winner.label.lower()
    has_danger = any(kw in winner_text for kw in DANGER_KEYWORDS)

    if has_danger:
        severity = "high" if len(active_risky) >= 2 else "medium"
        dissent.append(
            DissentNote(
                option_id=winner.id,
                concern=(
                    f'Winner "{winner.label}" contains destructive signals '
                    f"under active constraints: {', '.join(active_risky)}"
                ),
                severity=severity,
            )
        )

    return dissent


# ─── Main Class ───────────────────────────────────────────────────────────────

class Congress:
    """
    Internal deliberation engine.  Creates a CongressDecision, optionally
    persists the debate to the logic_entries table, and returns the result.
    """

    def deliberate(
        self,
        context: str,
        options: list[CongressOption],
        constraints: Optional[list[str]] = None,
        persist: bool = True,
    ) -> CongressDecision:
        """
        Run deliberation and return a CongressDecision.

        Parameters
        ----------
        context     : Human-readable description of the operation
        options     : Available choices
        constraints : Active risk tokens (from RISKY_CONSTRAINTS vocabulary)
        persist     : Whether to write the debate to logic_entries table
        """
        constraints = constraints or []

        if not options:
            return CongressDecision(
                winner=None,
                confidence=0.0,
                rationale="No options provided — nothing to deliberate",
            )

        # Score all options
        scored: list[ScoredOption] = [
            ScoredOption(
                id=opt.id,
                label=opt.label,
                score=_score_option(opt, i, len(options), constraints),
                rationale=_build_rationale(opt, constraints),
            )
            for i, opt in enumerate(options)
        ]

        # Winner = highest score
        winner = max(scored, key=lambda o: o.score)

        # Confidence: how decisively winner beats runner-up
        others = [o for o in scored if o.id != winner.id]
        runner_up_score = max((o.score for o in others), default=0.0)
        gap = winner.score - runner_up_score
        confidence = min(1.0, 0.4 + gap * 1.2)

        dissent = _build_dissent(winner, scored, constraints)

        decision = CongressDecision(
            winner=winner.id,
            confidence=confidence,
            rationale=(
                f'Selected "{winner.label}" (score {winner.score:.3f}) '
                f"via Congress deliberation"
            ),
            options=scored,
            dissent=dissent,
        )

        if persist:
            decision.logic_entry_id = self._persist(decision, context, constraints)

        return decision

    # ─── Persistence ──────────────────────────────────────────────────────────

    def _persist(
        self,
        decision: CongressDecision,
        context: str,
        constraints: list[str],
    ) -> Optional[int]:
        """Write the debate to logic_entries.  Returns the new row id."""
        try:
            db = get_memory_db()

            transcript = self._build_transcript(decision, context, constraints)

            paradigm_weight = float(
                sum(1 for c in constraints if c in RISKY_CONSTRAINTS)
            )

            n_opts = len(decision.options)
            n_cons = len(constraints)
            complexity_val = n_opts + n_cons * 2
            if complexity_val <= 3:
                complexity = "trivial"
            elif complexity_val <= 6:
                complexity = "moderate"
            elif complexity_val <= 10:
                complexity = "complex"
            else:
                complexity = "profound"

            return db.insert_logic_entry(
                topic=context,
                debate_transcript=transcript,
                resolution=decision.rationale,
                paradigm_weight=paradigm_weight,
                user_query=context,
                complexity_category=complexity,
                congress_perspectives=[
                    {
                        "option_id": o.id,
                        "label": o.label,
                        "score": o.score,
                        "rationale": o.rationale,
                    }
                    for o in decision.options
                ],
                profound_insights=[
                    {"concern": d.concern, "severity": d.severity}
                    for d in decision.dissent
                ],
                final_reasoning=decision.rationale,
            )
        except Exception as exc:
            # Never let persistence failure crash the caller
            print(f"[Congress] persistence failed: {exc}")
            return None

    @staticmethod
    def _build_transcript(
        decision: CongressDecision,
        context: str,
        constraints: list[str],
    ) -> str:
        lines = [
            "=== Congress Deliberation ===",
            f"Context: {context}",
        ]
        if constraints:
            lines.append(f"Constraints: {', '.join(constraints)}")

        lines += ["", "=== Options Evaluated ==="]
        for opt in decision.options:
            marker = "✓ SELECTED" if opt.id == decision.winner else "         "
            lines.append(f"{marker} [{opt.id}] {opt.label}")
            lines.append(f"   Score: {opt.score:.3f}")
            if opt.rationale:
                lines.append(f"   Rationale: {opt.rationale}")

        if decision.dissent:
            lines += ["", "=== Dissent Raised ==="]
            for d in decision.dissent:
                lines.append(f"[{d.severity.upper()}] {d.concern}")

        lines += ["", "=== Resolution ===", decision.rationale]
        return "\n".join(lines)


# ─── Convenience ─────────────────────────────────────────────────────────────

_congress = Congress()


def deliberate(
    context: str,
    options: list[CongressOption],
    constraints: Optional[list[str]] = None,
    persist: bool = True,
) -> CongressDecision:
    """Module-level shortcut — uses the shared Congress singleton."""
    return _congress.deliberate(context, options, constraints, persist)
