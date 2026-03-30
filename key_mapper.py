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
# 24 keys Rekordbox recognises, with placeholder Seq values.
#
# !! SEQ VALUES ARE UNVERIFIED — READ THIS BEFORE USING IN PRODUCTION !!
#
# Seq is used by Rekordbox for display ordering in the key column.
# The true Seq scheme must be derived from the live DjmdKey table.
# Four observed rows contradict every simple pattern:
#
#   ScaleName  Observed Seq   Camelot#   Camelot# (sequential) predicts
#   ---------  ------------   --------   ---------------------------------
#   Ebm (7A)        8            7            mismatch
#   Fm  (9A)        5            9            mismatch
#   C   (1B)        7            1            mismatch
#   Eb  (8B)        6           10            mismatch
#
# The placeholder values below (Camelot number) are WRONG for at least
# these 4 rows. Newly created DjmdKey rows will have incorrect Seq values
# until this is resolved.
#
# TO FIX BEFORE PRODUCTION USE:
#   1. Run the audit query in the smoke test below to dump all DjmdKey rows.
#   2. Cross-reference every row against the Camelot wheel.
#   3. Replace CANONICAL_KEYS values with the observed Seq integers.
#   4. Remove this warning comment once verified.
#
# Practical impact: Seq is cosmetic (key column sort order in Rekordbox).
# It does NOT affect playback, cue points, or playlist membership.
# Rekordbox overwrites Seq when it re-analyses a track, but it may NOT
# re-analyse tracks imported by a third-party tool. Treat the Seq values
# as wrong until verified.

CANONICAL_KEYS: dict[str, int] = {
    # Minor (xA) — placeholder Seq = Camelot number (UNVERIFIED — see above)
    "Am":  1,   "Em":  2,   "Bm":  3,   "F#m": 4,
    "C#m": 5,   "Abm": 6,   "Ebm": 7,   "Bbm": 8,
    "Fm":  9,   "Cm":  10,  "Gm":  11,  "Dm":  12,
    # Major (xB) — placeholder Seq = Camelot number (UNVERIFIED — see above)
    "C":   1,   "G":   2,   "D":   3,   "A":   4,
    "E":   5,   "B":   6,   "F#":  7,   "Db":  8,
    "Ab":  9,   "Eb":  10,  "Bb":  11,  "F":   12,
}


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
    if key in CANONICAL_KEYS:
        return key

    log.warning("Unrecognised key notation: %r — will import without key", raw)
    return None


# ─── Get-or-create ────────────────────────────────────────────────────────────

# Module-level cache: ScaleName → DjmdKey.ID (always str)
_key_id_cache: dict[str, str] = {}


def _get_or_create_key_row(scale_name: str, db: Rekordbox6Database) -> str:
    """
    Return the DjmdKey.ID for scale_name, creating the row if it doesn't exist.

    Returns str. Raises ValueError if scale_name is not in CANONICAL_KEYS.
    """
    if scale_name in _key_id_cache:
        return _key_id_cache[scale_name]

    existing = db.get_key(ScaleName=scale_name).first()
    if existing is not None:
        _key_id_cache[scale_name] = str(existing.ID)
        return str(existing.ID)

    if scale_name not in CANONICAL_KEYS:
        raise ValueError(
            f"Cannot create DjmdKey row for unknown scale name: {scale_name!r}"
        )

    seq = CANONICAL_KEYS[scale_name]
    new_id = db.generate_unused_id(tables.DjmdKey)
    key_row = tables.DjmdKey.create(
        ID=new_id,
        ScaleName=scale_name,
        Seq=seq,
        UUID=str(uuid4()),
    )
    db.add(key_row)
    db.flush()

    log.warning(
        "Created DjmdKey row: ScaleName=%r ID=%s Seq=%s "
        "(WARNING: Seq value is a placeholder — see CANONICAL_KEYS comment in key_mapper.py)",
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
