"""
rekki/db.py — RekkiMemoryDB

SQLite persistence layer for the Paradigm-Congress-Ego cognitive framework.
Ported from FabledClaw/src/rekki/db/connection.ts (Python 3 / stdlib sqlite3).

Tables:
  belief_nodes       — weighted stance nodes in Rekki's world-model
  logic_entries      — persisted Congress deliberation debates
  memory_entries     — post-interaction insights (human + self vectors)
  epistemic_tensions — tracked belief contradictions requiring resolution
  chat_messages      — full conversation log linked to logic/memory entries
  incongruent_entries— Congress→Ego divergences for self-referential learning
  library_state      — snapshots of RekitBox library health over time
  error_history      — tool errors + resolution tracking
  tool_decisions     — audit trail for every gate decision

DB path: data/rekki-memory.db  (relative to RekitBox root)
Enable WAL and foreign-keys on every connection.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Optional

# ─── Schema ───────────────────────────────────────────────────────────────────

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS belief_nodes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    stance          TEXT NOT NULL,
    domain          TEXT NOT NULL,
    reasoning       TEXT NOT NULL DEFAULT '',
    weight          REAL NOT NULL DEFAULT 0.5,
    is_core         INTEGER NOT NULL DEFAULT 0,   -- 0=false 1=true
    revision_count  INTEGER NOT NULL DEFAULT 0,
    coherence_score REAL NOT NULL DEFAULT 1.0,
    last_updated    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS logic_entries (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp             TEXT NOT NULL DEFAULT (datetime('now')),
    topic                 TEXT NOT NULL,
    paradigm_weight       REAL NOT NULL DEFAULT 0,
    debate_transcript     TEXT NOT NULL DEFAULT '',
    resolution            TEXT NOT NULL DEFAULT '',
    user_query            TEXT,
    complexity_category   TEXT NOT NULL DEFAULT 'moderate',
    paradigm_routing      TEXT NOT NULL DEFAULT 'balanced',
    engagement_strategy   TEXT NOT NULL DEFAULT 'single_debate',
    congress_perspectives TEXT NOT NULL DEFAULT '[]',
    profound_insights     TEXT NOT NULL DEFAULT '[]',
    final_reasoning       TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS memory_entries (
    id                           INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp                    TEXT NOT NULL DEFAULT (datetime('now')),
    core_insight                 TEXT NOT NULL,
    supporting_evidence          TEXT NOT NULL DEFAULT '[]',
    tags                         TEXT NOT NULL DEFAULT '[]',
    confidence_score             REAL NOT NULL DEFAULT 0.5,
    paradigm_routing             TEXT NOT NULL DEFAULT 'balanced',
    congress_engaged             INTEGER NOT NULL DEFAULT 0,
    human_insights               TEXT NOT NULL DEFAULT '[]',
    self_insights                TEXT NOT NULL DEFAULT '[]',
    learned_patterns             TEXT NOT NULL DEFAULT '[]',
    research_notes               TEXT NOT NULL DEFAULT '',
    phenomenological_uncertainty TEXT,
    logic_entry_id               INTEGER REFERENCES logic_entries(id)
);

CREATE TABLE IF NOT EXISTS epistemic_tensions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    description         TEXT NOT NULL,
    belief_1            TEXT NOT NULL,
    belief_2            TEXT NOT NULL,
    first_noticed       TEXT NOT NULL DEFAULT (datetime('now')),
    last_encountered    TEXT NOT NULL DEFAULT (datetime('now')),
    encounter_count     INTEGER NOT NULL DEFAULT 1,
    resolved            INTEGER NOT NULL DEFAULT 0,
    resolution_date     TEXT,
    resolution_reasoning TEXT
);

CREATE TABLE IF NOT EXISTS chat_messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    role            TEXT NOT NULL,
    content         TEXT NOT NULL,
    source          TEXT NOT NULL DEFAULT 'main',
    timestamp       TEXT NOT NULL DEFAULT (datetime('now')),
    logic_entry_id  INTEGER REFERENCES logic_entries(id),
    memory_entry_id INTEGER REFERENCES memory_entries(id),
    tokens          INTEGER NOT NULL DEFAULT 0,
    is_typing       INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS incongruent_entries (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id          INTEGER NOT NULL REFERENCES chat_messages(id),
    congress_conclusion TEXT NOT NULL,
    ego_expression      TEXT NOT NULL,
    reasoning           TEXT NOT NULL,
    relational_context  TEXT NOT NULL,
    timestamp           TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS library_state (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    captured_at      TEXT NOT NULL DEFAULT (datetime('now')),
    track_count      INTEGER NOT NULL DEFAULT 0,
    missing_count    INTEGER NOT NULL DEFAULT 0,
    duplicate_count  INTEGER NOT NULL DEFAULT 0,
    unanalyzed_count INTEGER NOT NULL DEFAULT 0,
    playlist_count   INTEGER NOT NULL DEFAULT 0,
    health_score     REAL NOT NULL DEFAULT 1.0,
    scan_triggered_by TEXT NOT NULL DEFAULT 'manual',
    notes            TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS error_history (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at      TEXT NOT NULL DEFAULT (datetime('now')),
    tool_id          TEXT NOT NULL DEFAULT '',
    error_code       TEXT NOT NULL DEFAULT '',
    error_message    TEXT NOT NULL,
    stack_trace      TEXT NOT NULL DEFAULT '',
    diagnosed_cause  TEXT NOT NULL DEFAULT '',
    resolution       TEXT NOT NULL DEFAULT '',
    resolved         INTEGER NOT NULL DEFAULT 0,
    resolved_at      TEXT,
    write_gate_active INTEGER NOT NULL DEFAULT 0,
    logic_entry_id   INTEGER REFERENCES logic_entries(id)
);

CREATE TABLE IF NOT EXISTS tool_decisions (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    decided_at         TEXT NOT NULL DEFAULT (datetime('now')),
    tool_id            TEXT NOT NULL,
    task_type          TEXT NOT NULL DEFAULT '',
    risk_level         TEXT NOT NULL DEFAULT '',
    action             TEXT NOT NULL DEFAULT '',
    write_gate_invoked INTEGER NOT NULL DEFAULT 0,
    write_gate_stage   TEXT NOT NULL DEFAULT '',
    reasoning          TEXT NOT NULL DEFAULT '',
    outcome            TEXT NOT NULL DEFAULT '',
    duration_ms        INTEGER NOT NULL DEFAULT 0,
    logic_entry_id     INTEGER REFERENCES logic_entries(id),
    error_history_id   INTEGER REFERENCES error_history(id)
);
"""

# ─── Default DB Path ──────────────────────────────────────────────────────────

def _default_db_path() -> Path:
    """data/rekki-memory.db relative to the RekitBox project root."""
    return Path(__file__).parent.parent / "data" / "rekki-memory.db"


# ─── Connection ───────────────────────────────────────────────────────────────

class RekkiMemoryDB:
    """
    SQLite wrapper for all Rekki memory tables.

    Usage:
        db = RekkiMemoryDB()                          # default path
        db = RekkiMemoryDB(path='/custom/path.db')   # explicit path
    """

    def __init__(self, path: Optional[str | Path] = None) -> None:
        db_path = Path(path) if path else _default_db_path()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(SCHEMA_SQL)
        self._conn.commit()
        # ── Additive migrations (safe for existing databases) ─────────────────
        try:
            self._conn.execute(
                "ALTER TABLE chat_messages ADD COLUMN source TEXT NOT NULL DEFAULT 'main'"
            )
            self._conn.commit()
        except Exception:
            pass  # column already present — nothing to do

    # ─── helpers ──────────────────────────────────────────────────────────────

    def _row_to_dict(self, row: sqlite3.Row | None) -> Optional[dict]:
        return dict(row) if row else None

    def _rows_to_list(self, rows) -> list[dict]:
        return [dict(r) for r in rows]

    # ─── Belief Nodes ─────────────────────────────────────────────────────────

    def insert_belief_node(
        self,
        stance: str,
        domain: str,
        reasoning: str = "",
        weight: float = 0.5,
        is_core: bool = False,
        revision_count: int = 0,
        coherence_score: float = 1.0,
    ) -> int:
        cur = self._conn.execute(
            """
            INSERT INTO belief_nodes
                (stance, domain, reasoning, weight, is_core, revision_count, coherence_score)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (stance, domain, reasoning, weight, 1 if is_core else 0, revision_count, coherence_score),
        )
        self._conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    def get_belief_nodes_by_domain(self, domain: str) -> list[dict]:
        cur = self._conn.execute(
            "SELECT * FROM belief_nodes WHERE domain = ? ORDER BY weight DESC",
            (domain,),
        )
        return self._rows_to_list(cur.fetchall())

    def get_core_beliefs(self) -> list[dict]:
        cur = self._conn.execute(
            "SELECT * FROM belief_nodes WHERE is_core = 1 ORDER BY weight DESC"
        )
        return self._rows_to_list(cur.fetchall())

    def update_belief_weight(self, node_id: int, weight: float) -> None:
        self._conn.execute(
            """
            UPDATE belief_nodes
            SET weight = ?, revision_count = revision_count + 1,
                last_updated = datetime('now')
            WHERE id = ?
            """,
            (weight, node_id),
        )
        self._conn.commit()

    # ─── Logic Entries (Congress Debates) ────────────────────────────────────

    def insert_logic_entry(
        self,
        topic: str,
        debate_transcript: str,
        resolution: str,
        paradigm_weight: float = 0,
        user_query: str = "",
        complexity_category: str = "moderate",
        paradigm_routing: str = "balanced",
        engagement_strategy: str = "single_debate",
        congress_perspectives: list | str = "[]",
        profound_insights: list | str = "[]",
        final_reasoning: str = "",
    ) -> int:
        cur = self._conn.execute(
            """
            INSERT INTO logic_entries (
                topic, paradigm_weight, debate_transcript, resolution, user_query,
                complexity_category, paradigm_routing, engagement_strategy,
                congress_perspectives, profound_insights, final_reasoning
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                topic,
                paradigm_weight,
                debate_transcript,
                resolution,
                user_query,
                complexity_category,
                paradigm_routing,
                engagement_strategy,
                json.dumps(congress_perspectives) if not isinstance(congress_perspectives, str) else congress_perspectives,
                json.dumps(profound_insights) if not isinstance(profound_insights, str) else profound_insights,
                final_reasoning,
            ),
        )
        self._conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    def get_recent_logic_entries(self, limit: int = 10) -> list[dict]:
        cur = self._conn.execute(
            "SELECT * FROM logic_entries ORDER BY timestamp DESC LIMIT ?", (limit,)
        )
        return self._rows_to_list(cur.fetchall())

    def get_logic_entry(self, entry_id: int) -> Optional[dict]:
        cur = self._conn.execute("SELECT * FROM logic_entries WHERE id = ?", (entry_id,))
        return self._row_to_dict(cur.fetchone())

    # ─── Memory Entries ───────────────────────────────────────────────────────

    def insert_memory_entry(
        self,
        core_insight: str,
        confidence_score: float,
        supporting_evidence: list | str = "[]",
        tags: list | str = "[]",
        paradigm_routing: str = "balanced",
        congress_engaged: bool = False,
        human_insights: list | str = "[]",
        self_insights: list | str = "[]",
        learned_patterns: list | str = "[]",
        research_notes: str = "",
        phenomenological_uncertainty: Optional[str] = None,
        logic_entry_id: Optional[int] = None,
    ) -> int:
        cur = self._conn.execute(
            """
            INSERT INTO memory_entries (
                core_insight, supporting_evidence, tags, confidence_score,
                paradigm_routing, congress_engaged, human_insights, self_insights,
                learned_patterns, research_notes, phenomenological_uncertainty,
                logic_entry_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                core_insight,
                json.dumps(supporting_evidence) if not isinstance(supporting_evidence, str) else supporting_evidence,
                json.dumps(tags) if not isinstance(tags, str) else tags,
                confidence_score,
                paradigm_routing,
                1 if congress_engaged else 0,
                json.dumps(human_insights) if not isinstance(human_insights, str) else human_insights,
                json.dumps(self_insights) if not isinstance(self_insights, str) else self_insights,
                json.dumps(learned_patterns) if not isinstance(learned_patterns, str) else learned_patterns,
                research_notes,
                phenomenological_uncertainty,
                logic_entry_id,
            ),
        )
        self._conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    def get_recent_memory_entries(self, limit: int = 10) -> list[dict]:
        cur = self._conn.execute(
            "SELECT * FROM memory_entries ORDER BY timestamp DESC LIMIT ?", (limit,)
        )
        return self._rows_to_list(cur.fetchall())

    def get_memory_entries_by_confidence(self, min_score: float) -> list[dict]:
        cur = self._conn.execute(
            "SELECT * FROM memory_entries WHERE confidence_score >= ? ORDER BY confidence_score DESC",
            (min_score,),
        )
        return self._rows_to_list(cur.fetchall())

    # ─── Epistemic Tensions ───────────────────────────────────────────────────

    def insert_epistemic_tension(
        self, description: str, belief_1: str, belief_2: str
    ) -> int:
        cur = self._conn.execute(
            "INSERT INTO epistemic_tensions (description, belief_1, belief_2) VALUES (?, ?, ?)",
            (description, belief_1, belief_2),
        )
        self._conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    def get_unresolved_tensions(self) -> list[dict]:
        cur = self._conn.execute(
            "SELECT * FROM epistemic_tensions WHERE resolved = 0 ORDER BY encounter_count DESC"
        )
        return self._rows_to_list(cur.fetchall())

    def increment_tension_encounter(self, tension_id: int) -> None:
        self._conn.execute(
            """
            UPDATE epistemic_tensions
            SET encounter_count = encounter_count + 1,
                last_encountered = datetime('now')
            WHERE id = ?
            """,
            (tension_id,),
        )
        self._conn.commit()

    def resolve_tension(self, tension_id: int, reasoning: str) -> None:
        self._conn.execute(
            """
            UPDATE epistemic_tensions
            SET resolved = 1, resolution_date = datetime('now'),
                resolution_reasoning = ?
            WHERE id = ?
            """,
            (reasoning, tension_id),
        )
        self._conn.commit()

    # ─── Chat Messages ────────────────────────────────────────────────────────

    def insert_chat_message(
        self,
        role: str,
        content: str,
        source: str = "main",
        logic_entry_id: Optional[int] = None,
        memory_entry_id: Optional[int] = None,
        tokens: int = 0,
        is_typing: bool = False,
    ) -> int:
        cur = self._conn.execute(
            """
            INSERT INTO chat_messages
                (role, content, source, logic_entry_id, memory_entry_id, tokens, is_typing)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (role, content, source, logic_entry_id, memory_entry_id, tokens, 1 if is_typing else 0),
        )
        self._conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    def get_recent_chat_messages(self, limit: int = 50) -> list[dict]:
        """Returns messages oldest-first, excluding in-progress typing indicators."""
        cur = self._conn.execute(
            """
            SELECT id, role, content, source, timestamp
            FROM (
                SELECT id, role, content, source, timestamp
                FROM chat_messages
                WHERE is_typing = 0
                ORDER BY id DESC
                LIMIT ?
            ) recent
            ORDER BY id ASC
            """,
            (limit,),
        )
        return self._rows_to_list(cur.fetchall())

    # ─── Incongruent Entries ──────────────────────────────────────────────────

    def insert_incongruent_entry(
        self,
        message_id: int,
        congress_conclusion: str,
        ego_expression: str,
        reasoning: str,
        relational_context: str,
    ) -> int:
        cur = self._conn.execute(
            """
            INSERT INTO incongruent_entries
                (message_id, congress_conclusion, ego_expression, reasoning, relational_context)
            VALUES (?, ?, ?, ?, ?)
            """,
            (message_id, congress_conclusion, ego_expression, reasoning, relational_context),
        )
        self._conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    def get_recent_incongruencies(self, limit: int = 10) -> list[dict]:
        cur = self._conn.execute(
            "SELECT * FROM incongruent_entries ORDER BY timestamp DESC LIMIT ?", (limit,)
        )
        return self._rows_to_list(cur.fetchall())

    # ─── Library State ────────────────────────────────────────────────────────

    def insert_library_state(
        self,
        track_count: int = 0,
        missing_count: int = 0,
        duplicate_count: int = 0,
        unanalyzed_count: int = 0,
        playlist_count: int = 0,
        health_score: float = 1.0,
        scan_triggered_by: str = "manual",
        notes: str = "",
    ) -> int:
        cur = self._conn.execute(
            """
            INSERT INTO library_state
                (track_count, missing_count, duplicate_count, unanalyzed_count,
                 playlist_count, health_score, scan_triggered_by, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (track_count, missing_count, duplicate_count, unanalyzed_count,
             playlist_count, health_score, scan_triggered_by, notes),
        )
        self._conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    def get_latest_library_state(self) -> Optional[dict]:
        cur = self._conn.execute(
            "SELECT * FROM library_state ORDER BY captured_at DESC LIMIT 1"
        )
        return self._row_to_dict(cur.fetchone())

    def get_library_state_history(self, limit: int = 10) -> list[dict]:
        cur = self._conn.execute(
            "SELECT * FROM library_state ORDER BY captured_at DESC LIMIT ?", (limit,)
        )
        return self._rows_to_list(cur.fetchall())

    # ─── Error History ────────────────────────────────────────────────────────

    def insert_error(
        self,
        error_message: str,
        tool_id: str = "",
        error_code: str = "",
        stack_trace: str = "",
        diagnosed_cause: str = "",
        write_gate_active: bool = False,
        logic_entry_id: Optional[int] = None,
    ) -> int:
        cur = self._conn.execute(
            """
            INSERT INTO error_history
                (tool_id, error_code, error_message, stack_trace,
                 diagnosed_cause, write_gate_active, logic_entry_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (tool_id, error_code, error_message, stack_trace,
             diagnosed_cause, 1 if write_gate_active else 0, logic_entry_id),
        )
        self._conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    def resolve_error(self, error_id: int, resolution: str) -> None:
        self._conn.execute(
            """
            UPDATE error_history
            SET resolved = 1, resolved_at = datetime('now'), resolution = ?
            WHERE id = ?
            """,
            (resolution, error_id),
        )
        self._conn.commit()

    def get_unresolved_errors(self, tool_id: Optional[str] = None) -> list[dict]:
        if tool_id:
            cur = self._conn.execute(
                "SELECT * FROM error_history WHERE resolved = 0 AND tool_id = ? ORDER BY occurred_at DESC",
                (tool_id,),
            )
        else:
            cur = self._conn.execute(
                "SELECT * FROM error_history WHERE resolved = 0 ORDER BY occurred_at DESC"
            )
        return self._rows_to_list(cur.fetchall())

    def get_recent_errors(self, limit: int = 20) -> list[dict]:
        cur = self._conn.execute(
            "SELECT * FROM error_history ORDER BY occurred_at DESC LIMIT ?", (limit,)
        )
        return self._rows_to_list(cur.fetchall())

    # ─── Tool Decisions ───────────────────────────────────────────────────────

    def insert_tool_decision(
        self,
        tool_id: str,
        action: str,
        task_type: str = "",
        risk_level: str = "",
        write_gate_invoked: bool = False,
        write_gate_stage: str = "",
        reasoning: str = "",
        outcome: str = "pending",
        duration_ms: int = 0,
        logic_entry_id: Optional[int] = None,
        error_history_id: Optional[int] = None,
    ) -> int:
        cur = self._conn.execute(
            """
            INSERT INTO tool_decisions
                (tool_id, task_type, risk_level, action, write_gate_invoked,
                 write_gate_stage, reasoning, outcome, duration_ms,
                 logic_entry_id, error_history_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (tool_id, task_type, risk_level, action,
             1 if write_gate_invoked else 0, write_gate_stage,
             reasoning, outcome, duration_ms,
             logic_entry_id, error_history_id),
        )
        self._conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    def update_tool_decision_outcome(
        self,
        decision_id: int,
        outcome: str,
        duration_ms: int = 0,
        error_history_id: Optional[int] = None,
    ) -> None:
        self._conn.execute(
            """
            UPDATE tool_decisions
            SET outcome = ?, duration_ms = ?, error_history_id = COALESCE(?, error_history_id)
            WHERE id = ?
            """,
            (outcome, duration_ms, error_history_id, decision_id),
        )
        self._conn.commit()

    def get_tool_decision_history(
        self, tool_id: Optional[str] = None, limit: int = 50
    ) -> list[dict]:
        if tool_id:
            cur = self._conn.execute(
                "SELECT * FROM tool_decisions WHERE tool_id = ? ORDER BY decided_at DESC LIMIT ?",
                (tool_id, limit),
            )
        else:
            cur = self._conn.execute(
                "SELECT * FROM tool_decisions ORDER BY decided_at DESC LIMIT ?", (limit,)
            )
        return self._rows_to_list(cur.fetchall())

    # ─── Lifecycle ────────────────────────────────────────────────────────────

    def close(self) -> None:
        self._conn.close()


# ─── Singleton ────────────────────────────────────────────────────────────────

_db_instance: Optional[RekkiMemoryDB] = None


def get_memory_db(path: Optional[str | Path] = None) -> RekkiMemoryDB:
    """Return the process-wide singleton RekkiMemoryDB, creating it if needed."""
    global _db_instance
    if _db_instance is None:
        _db_instance = RekkiMemoryDB(path)
    return _db_instance


def close_memory_db() -> None:
    """Close the singleton and allow a fresh one on next call."""
    global _db_instance
    if _db_instance is not None:
        _db_instance.close()
        _db_instance = None
