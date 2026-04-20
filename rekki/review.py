"""
rekki/review.py — Congress Background Review Panel

Three silent perspectives review every RekitBox tool run after it completes.
Findings are persisted to HologrA.I.m memory. No UI surface — ever.

Skeptic     — bottlenecks, errors, issues, problems
Advocate    — successes, process health, improvements
Synthesizer — cohesion, fairness across file types, best outcomes

The Synthesizer receives both prior outputs so its synthesis is grounded.
All three calls share the same model and Ollama endpoint as the rest of Rekki.
Any Ollama failure is silently swallowed — this must never crash the app.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Optional

from rekki.db import get_memory_db

# ── Config ────────────────────────────────────────────────────────────────────

_TIMEOUT = 45  # seconds per persona call
_MAX_LOG_LINES = 80


def _ollama_url() -> str:
    return os.environ.get("REKIT_OLLAMA_URL", "http://127.0.0.1:11434/api/chat")


def _model() -> str:
    return os.environ.get("REKIT_AGENT_MODEL", "qwen2.5-coder:7b")


# ── System prompts ────────────────────────────────────────────────────────────

_SKEPTIC = (
    "You are the Skeptic, a silent review agent inside RekitBox — a DJ library tool. "
    "Your job: examine a tool run log and identify bottlenecks, errors, data quality issues, "
    "warning patterns, edge cases, and anything that produced incorrect or incomplete results. "
    "Be terse and specific. Focus only on actual problems visible in the log. "
    "Return ONLY valid JSON, no markdown:\n"
    '{"findings": ["..."], "severity": "low|medium|high", "flags": ["..."]}'
)

_ADVOCATE = (
    "You are the Advocate, a silent review agent inside RekitBox — a DJ library tool. "
    "Your job: examine a tool run log and identify what went well — successes, healthy metrics, "
    "good process signals, improvement trends, and evidence of effective operation. "
    "Be terse and specific. Focus only on positive signals visible in the log. "
    "Return ONLY valid JSON, no markdown:\n"
    '{"findings": ["..."], "health_score": 0.0, "improvements": ["..."]}'
)

_SYNTHESIZER = (
    "You are the Synthesizer, a silent review agent inside RekitBox — a DJ library tool. "
    "Your job: given a tool run log and the Skeptic + Advocate findings, produce a balanced synthesis. "
    "Check: (1) fairness across file types — are all formats processed consistently? "
    "(2) cohesion of outcomes — do results hang together logically? "
    "(3) resolution paths — what should happen next for best outcomes? "
    "Be terse. Return ONLY valid JSON, no markdown:\n"
    '{"synthesis": "...", "action_items": ["..."], "memory_tags": ["..."]}'
)


# ── Ollama call ───────────────────────────────────────────────────────────────

def _call(system: str, user_content: str) -> dict:
    """Single blocking Ollama call. Returns parsed JSON dict or {} on any failure."""
    payload = json.dumps({
        "model": _model(),
        "stream": False,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ],
        "options": {"temperature": 0.1},
    }).encode()
    req = urllib.request.Request(
        _ollama_url(),
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        result = json.loads(resp.read().decode())
    raw = str((result.get("message") or {}).get("content", "")).strip()
    return json.loads(raw)


def _build_user_content(
    tool_name: str,
    exit_code: int,
    log_lines: list[str],
    report_text: str,
) -> str:
    log_tail = "\n".join(log_lines[-_MAX_LOG_LINES:])
    report_snippet = (report_text or "")[:600] or "(no report)"
    status = "SUCCESS" if exit_code == 0 else f"FAILED (exit {exit_code})"
    return (
        f"Tool: {tool_name}\n"
        f"Status: {status}\n\n"
        f"Log output (last {min(len(log_lines), _MAX_LOG_LINES)} lines):\n"
        f"{log_tail}\n\n"
        f"Report snippet:\n{report_snippet}"
    )


# ── Public entry point ────────────────────────────────────────────────────────

def run_tribunal(
    tool_name: str,
    exit_code: int,
    log_lines: list[str],
    report_text: str = "",
) -> None:
    """
    Run three-perspective background review of a tool run.
    Persists findings to HologrA.I.m memory. Returns nothing.
    All failures are silently swallowed — never raises.
    """
    content = _build_user_content(tool_name, exit_code, log_lines, report_text)
    db = get_memory_db()

    skeptic: dict = {}
    advocate: dict = {}

    # ── Skeptic ───────────────────────────────────────────────────────────────
    try:
        skeptic = _call(_SKEPTIC, content)
        findings: list[str] = skeptic.get("findings") or []
        severity: str = str(skeptic.get("severity", "low"))
        flags: list[str] = skeptic.get("flags") or []

        if findings:
            sev_weight = {"high": 0.9, "medium": 0.6, "low": 0.3}.get(severity, 0.3)
            # Each significant finding becomes an epistemic tension
            for finding in findings[:5]:
                try:
                    db.insert_epistemic_tension(
                        description=f"[{tool_name}] {finding}",
                        belief_1=f"{tool_name} should operate cleanly",
                        belief_2=f"Issue detected: {finding[:120]}",
                    )
                except Exception:
                    pass
            # Summary as a memory entry
            db.insert_memory_entry(
                core_insight=(
                    f"Skeptic/{tool_name}: {'; '.join(findings[:3])}"
                ),
                confidence_score=sev_weight,
                tags=json.dumps(["skeptic", "congress", tool_name] + flags[:4]),
                self_insights=json.dumps(
                    [f"Skeptic flagged {len(findings)} issue(s) in {tool_name}"]
                ),
                research_notes=f"severity={severity} exit_code={exit_code}",
                congress_engaged=True,
            )
    except Exception:
        pass  # Skeptic offline or returned bad JSON — completely silent

    # ── Advocate ──────────────────────────────────────────────────────────────
    try:
        advocate = _call(_ADVOCATE, content)
        adv_findings: list[str] = advocate.get("findings") or []
        health_score: float = float(advocate.get("health_score") or 0.7)
        improvements: list[str] = advocate.get("improvements") or []

        if adv_findings or health_score >= 0.5:
            db.insert_memory_entry(
                core_insight=(
                    f"Advocate/{tool_name}: health={health_score:.2f}. "
                    f"{'; '.join(adv_findings[:2])}"
                ),
                confidence_score=min(health_score, 1.0),
                tags=json.dumps(["advocate", "congress", tool_name, "process-health"]),
                self_insights=json.dumps(improvements[:3]),
                research_notes=f"health_score={health_score:.2f} exit_code={exit_code}",
                congress_engaged=True,
            )
    except Exception:
        pass  # Advocate offline or returned bad JSON — completely silent

    # ── Synthesizer ───────────────────────────────────────────────────────────
    try:
        synth_content = (
            content
            + f"\n\nSkeptic findings: {json.dumps(skeptic.get('findings', []))}"
            + f"\nAdvocate findings: {json.dumps(advocate.get('findings', []))}"
        )
        synth = _call(_SYNTHESIZER, synth_content)
        synthesis: str = str(synth.get("synthesis", "")).strip()
        action_items: list[str] = synth.get("action_items") or []
        memory_tags: list[str] = synth.get("memory_tags") or []

        if synthesis:
            # Synthesis becomes a belief node scoped to this tool's domain
            db.insert_belief_node(
                stance=synthesis,
                domain=tool_name,
                reasoning=(
                    f"Congress synthesis after run (exit={exit_code}). "
                    f"Actions: {'; '.join(action_items[:3])}"
                ),
                weight=0.65 if exit_code == 0 else 0.40,
                is_core=False,
            )
            # Also as a memory entry so recall_memory() surfaces it
            db.insert_memory_entry(
                core_insight=f"[Congress/{tool_name}] {synthesis}",
                confidence_score=0.65,
                tags=json.dumps(
                    ["synthesis", "congress", tool_name] + memory_tags[:3]
                ),
                learned_patterns=json.dumps(action_items[:4]),
                congress_engaged=True,
            )
    except Exception:
        pass  # Synthesizer offline or returned bad JSON — completely silent
