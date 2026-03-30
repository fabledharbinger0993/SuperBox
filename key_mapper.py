"""
rekordbox-toolkit / key_mapper.py

Resolves any key notation (Camelot, Open Key, standard) to a DjmdKey.ID.

Rekordbox builds the DjmdKey table dynamically as tracks are analyzed —
it does not pre-populate all 24 keys. This module uses a get-or-create
pattern: look up the ScaleName row, create it if absent, return its ID.

Public interface:
    resolve_key_id(scale_name, db) -> str | None
    notation_to_scale_name(raw_key_string) -> str | None
"""

import logging
from uuid import uuid4

from pyrekordbox import Rekordbox6Database
from pyrekordbox.db6 import tables

from config import CAMELOT_TO_RB, OPENKEY_TO_RB, STANDARD_KEY_ALIASES

log = logging.getLogger(__name__)

# ─── Canonical key definitions ────────────────────────────────────────────────
#
# The 24 ScaleNames Rekordbox uses for standard keys.
#
# DjmdKey.Seq investigation (live DB dump, 2026-03-30):
#
#   Seq is NOT a canonical fixed ordering — it is simply an auto-incrementing
#   counter assigned when Rekordbox first creates a DjmdKey row during track
#   analysis. The values reflect the order keys were encountered in THIS
#   user's library, not any musical scheme:
#
#     Seq 1 → Am      Seq 2 → Gm     Seq 3 → Abm    Seq 4 → Dm
#     Seq 5 → Fm      Seq 6 → Eb     Seq 7 → C      Seq 8 → Ebm
#
#   Rows created by third-party tools have Seq = None in the observed DB,
#   confirming there is no fixed canonical mapping.
#
# Resolution: _get_or_create_key_row() now computes Seq dynamically as
# max(existing Seq values) + 1 — exactly what Rekordbox does natively.
# This produces correct, non-colliding Seq values for any user's database
# regardless of which keys they already have. The CANONICAL_SCALE_NAMES
# frozenset below now only serves as the validity guard — Seq is gone.

CANONICAL_SCALE_NAMES: frozenset[str] = frozenset({
    # Minor (Camelot xA)
    "Am",  "Em",  "Bm",  "F#m",
    "C#m", "Abm", "Ebm", "Bbm",
    "Fm",  "Cm",  "Gm",  "Dm",
    # Major (Camelot xB)
    "C",   "G",   "D",   "A",
    "E",   "B",   "F#",  "Db",
    "Ab",  "Eb",  "Bb",  "F",
})


# ─── Notation normalisation ───────────────────────────────────────────────────

def notation_to_scale_name(raw: str | None) -> str | None:
    """
    Convert any key notation to a canonical Rekordbox ScaleName.

    Handles:
      - Camelot wheel  : "5A", "8B", "12A"
      - Open Key       : "5m", "8d"
      - Standard       : "C", "Am", "F#m", "Dbm", "Ebm", etc.
      - Aliases        : "Cmaj", "CM", "A minor", enharmonic equivalents

    Returns None if the string is unrecognised — caller should log and skip.
    """
    if not raw:
        return None

    key = raw.strip()

    # Camelot: digit(s) + A/B — normalise to upper so "5a" → "5A"
    upper = key.upper()
    if upper in CAMELOT_TO_RB:
        return CAMELOT_TO_RB[upper]

    # Open Key: digit(s) + m/d (case-insensitive)
    ok_candidate = key[:-1] + key[-1].lower() if key else key
    if ok_candidate in OPENKEY_TO_RB:
        return OPENKEY_TO_RB[ok_candidate]

    # Standard notation and aliases
    if key in STANDARD_KEY_ALIASES:
        return STANDARD_KEY_ALIASES[key]

    # Last attempt: direct match against canonical key names
    if key in CANONICAL_SCALE_NAMES:
        return key

    log.warning("Unrecognised key notation: %r — will import without key", raw)
    return None


# ─── Get-or-create ────────────────────────────────────────────────────────────

# Module-level cache: ScaleName → DjmdKey.ID (always str)
_key_id_cache: dict[str, str] = {}


def _next_seq(db: Rekordbox6Database) -> int:
    """
    Return the next available DjmdKey.Seq value.

    Rekordbox assigns Seq as a simple incrementing counter — there is no
    canonical musical ordering. We replicate that behaviour: find the current
    maximum Seq and add 1. Rows with Seq = None (created by third-party tools)
    are ignored. Returns 1 if no rows with a Seq value exist yet.
    """
    rows = db.get_key().all()
    seqs = [r.Seq for r in rows if r.Seq is not None]
    return (max(seqs) + 1) if seqs else 1


def _get_or_create_key_row(scale_name: str, db: Rekordbox6Database) -> str:
    """
    Return the DjmdKey.ID for scale_name, creating the row if it doesn't exist.

    Returns str. Raises ValueError if scale_name is not in CANONICAL_SCALE_NAMES.
    """
    if scale_name in _key_id_cache:
        return _key_id_cache[scale_name]

    existing = db.get_key(ScaleName=scale_name).first()
    if existing is not None:
        _key_id_cache[scale_name] = str(existing.ID)
        return str(existing.ID)

    if scale_name not in CANONICAL_SCALE_NAMES:
        raise ValueError(
            f"Cannot create DjmdKey row for unknown scale name: {scale_name!r}"
        )

    seq = _next_seq(db)
    new_id = db.generate_unused_id(tables.DjmdKey)
    key_row = tables.DjmdKey.create(
        ID=new_id,
        ScaleName=scale_name,
        Seq=seq,
        UUID=str(uuid4()),
    )
    db.add(key_row)
    db.flush()

    log.info(
        "Created DjmdKey row: ScaleName=%r ID=%s Seq=%s",
        scale_name, new_id, seq,
    )
    _key_id_cache[scale_name] = str(new_id)
    return str(new_id)


# ─── Public interface ─────────────────────────────────────────────────────────

def resolve_key_id(
    raw_key: str | None,
    db: Rekordbox6Database,
) -> str | None:
    """Full pipeline: raw tag string → DjmdKey.ID (str) or None."""
    scale_name = notation_to_scale_name(raw_key)
    if scale_name is None:
        return None
    try:
        return _get_or_create_key_row(scale_name, db)
    except Exception:
        log.exception("Failed to resolve/create key row for %r", scale_name)
        return None


def clear_cache() -> None:
    """Clear the in-memory key ID cache. Useful between test runs."""
    _key_id_cache.clear()


# ─── Smoke test ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(message)s")
    sys.path.insert(0, ".")

    from db_connection import read_db
    from config import DJMT_DB

    # ── Part 1: notation_to_scale_name (no DB needed) ──
    print("=== notation_to_scale_name ===")
    test_cases = [
        ("5A",  "C#m"),  ("8B",  "Db"),  ("1B",  "C"),   ("12A", "Dm"),
        ("5a",  "C#m"),  ("8b",  "Db"),
        ("5m",  "C#m"),  ("8d",  "Db"),  ("1d",  "C"),
        ("Am",  "Am"),   ("F#m", "F#m"), ("Gm",  "Gm"),
        ("Dbm", "C#m"),  ("C#m", "C#m"),
        ("Eb",  "Eb"),   ("F#",  "F#"),  ("Bb",  "Bb"),
        ("Cmaj","C"),    ("DM",  "D"),   ("Amin","Am"),
        ("XYZ", None),   ("",    None),  (None,  None),
    ]

    all_ok = True
    for raw, expected in test_cases:
        result = notation_to_scale_name(raw)
        status = "✓" if result == expected else "✗"
        if result != expected:
            all_ok = False
        print(f"  {status}  {str(raw):8} → {str(result):8}  (expected {str(expected)})")

    print(f"\nNotation tests: {'ALL PASSED' if all_ok else 'FAILURES ABOVE'}")

    # ── Part 2: resolve_key_id + Seq audit ──
    # These 4 keys exist in the live DB — no creation will occur.
    # NOTE: resolve_key_id may call db.add()/flush() if a row is missing.
    # For creation testing use write_db instead.
    print("\n=== resolve_key_id (existing rows — expect no creation) ===")
    with read_db(DJMT_DB) as db:
        for scale_name in ["Ebm", "C", "Fm", "Eb"]:
            kid = resolve_key_id(scale_name, db)
            print(f"  {scale_name:6} → KeyID: {kid}  (type: {type(kid).__name__})")

        result = resolve_key_id("XYZ", db)
        print(f"  {'XYZ':6} → KeyID: {result}  (expected None)")

        # ── Seq audit — the TODO from the review ──
        print("\n=== DjmdKey Seq audit (SELECT ScaleName, Seq FROM DjmdKey ORDER BY Seq) ===")
        rows = db.get_key().all()
        camelot_notes = {"Ebm": "7A", "Fm": "9A", "C": "1B", "Eb": "8B"}
        expected_seq  = {"Ebm": 7,    "Fm": 9,    "C": 1,    "Eb": 10}
        for r in sorted(rows, key=lambda x: x.Seq):
            cn  = camelot_notes.get(r.ScaleName, "?")
            exp = expected_seq.get(r.ScaleName, "?")
            print(f"  ScaleName={r.ScaleName:6} Seq={r.Seq}  "
                  f"(Camelot: {cn}, expected Camelot#: {exp})")

    print("\nSmoke test complete.")
