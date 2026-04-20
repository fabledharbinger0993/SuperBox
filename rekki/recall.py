"""
rekki/recall.py — HologrA.I.m Pre-Response Hook

recall_memory() MUST be called before every Rekki response.
Ported from FabledClaw/src/rekki/memory/recall.ts.

Core Principle:
  Memory separates learning into two vectors:
    - What Rekki discovered about THE HUMAN (human_insights)
    - What Rekki discovered about ITSELF (self_insights)
  This self-referential loop drives belief evolution.

Typical usage inside /api/rekki/chat:

    from rekki.recall import recall_memory, create_memory, format_recalled_memory

    # 1. Pre-response: inject memory context into the system prompt
    memory_ctx = format_recalled_memory(recall_memory())

    # 2. Build Ollama payload with memory_ctx prepended to system message

    # 3. Post-response: persist what was learned
    create_memory(
        core_insight="User prefers dry-run before batch ops",
        confidence_score=0.85,
        tags=["preference", "safety"],
        human_insights=["prefers confirmation before large batches"],
    )
"""

from __future__ import annotations

import json
from typing import Optional
from rekki.db import get_memory_db


# ─── Types ────────────────────────────────────────────────────────────────────

class RecalledMemory:
    __slots__ = (
        "recent",
        "confident",
        "core_beliefs",
        "recent_debates",
        "human_patterns",
        "self_patterns",
        "tensions",
    )

    def __init__(
        self,
        recent: list[dict],
        confident: list[dict],
        core_beliefs: list[dict],
        recent_debates: list[dict],
        human_patterns: list[str],
        self_patterns: list[str],
        tensions: list[dict],
    ) -> None:
        self.recent = recent
        self.confident = confident
        self.core_beliefs = core_beliefs
        self.recent_debates = recent_debates
        self.human_patterns = human_patterns
        self.self_patterns = self_patterns
        self.tensions = tensions


# ─── Pattern Extraction ───────────────────────────────────────────────────────

def _extract_human_patterns(memories: list[dict]) -> list[str]:
    seen: set[str] = set()
    for mem in memories:
        try:
            for insight in json.loads(mem.get("human_insights") or "[]"):
                if isinstance(insight, str):
                    seen.add(insight)
        except (json.JSONDecodeError, TypeError):
            pass
    return list(seen)


def _extract_self_patterns(memories: list[dict]) -> list[str]:
    seen: set[str] = set()
    for mem in memories:
        for key in ("self_insights", "learned_patterns"):
            try:
                for item in json.loads(mem.get(key) or "[]"):
                    if isinstance(item, str):
                        seen.add(item)
            except (json.JSONDecodeError, TypeError):
                pass
    return list(seen)


# ─── Core Recall ──────────────────────────────────────────────────────────────

def recall_memory(
    recent_limit: int = 10,
    min_confidence: float = 70.0,
    debate_limit: int = 5,
    include_tensions: bool = True,
) -> RecalledMemory:
    """
    Mandatory pre-response memory retrieval.

    MUST be called before every Rekki response to maintain:
    - Relational continuity (what we know about the human)
    - Self-awareness (what we know about our own reasoning)
    - Belief coherence (weights and tensions)
    - Congress history (past deliberation outcomes)
    """
    db = get_memory_db()

    recent = db.get_recent_memory_entries(recent_limit)
    confident = db.get_memory_entries_by_confidence(min_confidence)
    core_beliefs = db.get_core_beliefs()
    recent_debates = db.get_recent_logic_entries(debate_limit)
    human_patterns = _extract_human_patterns(recent)
    self_patterns = _extract_self_patterns(recent)

    tensions: list[dict] = []
    if include_tensions:
        tensions = [
            {
                "description": t["description"],
                "belief_1": t["belief_1"],
                "belief_2": t["belief_2"],
            }
            for t in db.get_unresolved_tensions()
        ]

    return RecalledMemory(
        recent=recent,
        confident=confident,
        core_beliefs=core_beliefs,
        recent_debates=recent_debates,
        human_patterns=human_patterns,
        self_patterns=self_patterns,
        tensions=tensions,
    )


# ─── Memory Creation ──────────────────────────────────────────────────────────

def create_memory(
    core_insight: str,
    confidence_score: float,
    supporting_evidence: Optional[list[str]] = None,
    tags: Optional[list[str]] = None,
    human_insights: Optional[list[str]] = None,
    self_insights: Optional[list[str]] = None,
    learned_patterns: Optional[list[str]] = None,
    research_notes: str = "",
    phenomenological_uncertainty: Optional[str] = None,
    logic_entry_id: Optional[int] = None,
    paradigm_routing: str = "balanced",
    congress_engaged: bool = False,
) -> int:
    """
    Persist a new memory entry after an interaction.

    The self-referential loop:
        1. Congress deliberates → Logic Entry written
        2. Rekki analyses debate → Memory Entry written here
        3. Memory holds human_insights + self_insights
        4. Those insights influence future belief weights
    """
    db = get_memory_db()
    return db.insert_memory_entry(
        core_insight=core_insight,
        confidence_score=confidence_score,
        supporting_evidence=supporting_evidence or [],
        tags=tags or [],
        paradigm_routing=paradigm_routing,
        congress_engaged=congress_engaged,
        human_insights=human_insights or [],
        self_insights=self_insights or [],
        learned_patterns=learned_patterns or [],
        research_notes=research_notes,
        phenomenological_uncertainty=phenomenological_uncertainty,
        logic_entry_id=logic_entry_id,
    )


# ─── Formatting ───────────────────────────────────────────────────────────────

def format_recalled_memory(recalled: RecalledMemory) -> str:
    """
    Render recalled memory as a markdown context block ready to inject
    into a system prompt before every Rekki response.
    """
    sections: list[str] = []

    if recalled.core_beliefs:
        sections.append("## Core Beliefs")
        for b in recalled.core_beliefs[:5]:
            sections.append(
                f"- [{b['domain']}] {b['stance']} (weight: {b['weight']})"
            )

    if recalled.recent:
        sections.append("\n## Recent Insights")
        for m in recalled.recent[:5]:
            sections.append(
                f"- {m['core_insight']} (confidence: {m['confidence_score']}%)"
            )

    if recalled.human_patterns:
        sections.append("\n## Learned About Human")
        for p in recalled.human_patterns[:5]:
            sections.append(f"- {p}")

    if recalled.self_patterns:
        sections.append("\n## Learned About Self")
        for p in recalled.self_patterns[:5]:
            sections.append(f"- {p}")

    if recalled.tensions:
        sections.append("\n## Active Tensions")
        for t in recalled.tensions[:3]:
            sections.append(f"- {t['description']}")
            sections.append(f'  "{t["belief_1"]}" ↔ "{t["belief_2"]}"')

    return "\n".join(sections)


def get_memory_context(**kwargs) -> str:
    """One-shot recall + format for immediate system-prompt injection."""
    return format_recalled_memory(recall_memory(**kwargs))
