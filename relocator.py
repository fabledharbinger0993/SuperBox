"""
rekordbox-toolkit / relocator.py

Batch-updates FolderPath values in DjmdContent when files have been
moved or renamed. The equivalent of Rekordbox's one-at-a-time "Relocate"
function, applied to entire directory trees at once.

Matching strategies (tried in priority order per track):
  1. Exact      — filename exists at same relative path under new_root
  2. Hash       — SHA256 of first 64KB matches a file in new_root
                  (catches renames where content is identical)
  3. Fuzzy      — difflib filename similarity ≥ 0.90, stem-only comparison
                  (catches minor filename edits, encoding fixes, format changes)
  4. Not found  — logged as warning, FolderPath left unchanged

Design rules:
  - Never deletes DjmdContent rows — only updates FolderPath
  - check_path=True passed to update_content_path — pyrekordbox verifies
    the target file exists before writing
  - Hash index is built once over new_root before processing begins
  - Fuzzy index is built once (filename list) — no per-track filesystem walk
  - Commits in BATCH_SIZE batches

Hash strategy note: _try_hash can only hash the original file if it still
exists at the old path. In mid-migration scenarios (both copies present),
hash matching is most reliable. Once the original is gone, the strategy
falls through to fuzzy automatically. An OSError on an existing file (e.g.
permissions) is logged and also falls through to fuzzy rather than crashing.

Fuzzy index note: stem-only keys mean track.mp3 and track.aiff share the
key "track". A format conversion that preserves the stem will match at 1.0
similarity. If two files in new_root share a stem, the last one encountered
during the walk wins in the fuzzy index — a collision warning is logged when
this occurs.

Public interface:
    relocate_directory(old_root, new_root, db) -> list[RelocationResult]
"""

import difflib
import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TYPE_CHECKING

from pyrekordbox import Rekordbox6Database

from config import AUDIO_EXTENSIONS, BATCH_SIZE, SKIP_DIRS, SKIP_PREFIXES

if TYPE_CHECKING:
    # DjmdContent is an ORM row type from pyrekordbox's SQLAlchemy models.
    # Not cleanly importable at runtime in all versions — referenced under
    # TYPE_CHECKING only.
    from pyrekordbox.db6.tables import DjmdContent

log = logging.getLogger(__name__)

_FUZZY_CUTOFF: float = 0.90
_HASH_READ_BYTES: int = 65536   # 64 KB — fast, sufficient for audio file identity


# ─── Result type ──────────────────────────────────────────────────────────────

@dataclass
class RelocationResult:
    content_id: str
    original_path: str
    new_path: str | None
    strategy: Literal["exact", "hash", "fuzzy", "not_found"]
    success: bool
    error: str | None = None


# ─── Filesystem index builders ────────────────────────────────────────────────

def _walk_audio_files(root: Path) -> list[Path]:
    """
    Return all audio files under root, respecting SKIP_DIRS and SKIP_PREFIXES.
    Used to build both the hash and fuzzy indexes from a single walk.
    """
    files: list[Path] = []
    for dirpath, dirnames, filenames in root.walk():
        dirnames[:] = [
            d for d in dirnames
            if d not in SKIP_DIRS and not d.startswith(".")
        ]
        for filename in filenames:
            if any(filename.startswith(p) for p in SKIP_PREFIXES):
                continue
            file_path = dirpath / filename
            if file_path.suffix.lower() in AUDIO_EXTENSIONS:
                files.append(file_path)
    return files


def _file_hash(path: Path) -> str | None:
    """
    SHA256 of the first 64KB of a file.
    Returns None on read failure — caller falls through to the next strategy.
    """
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            h.update(f.read(_HASH_READ_BYTES))
        return h.hexdigest()
    except OSError as e:
        log.warning("Hash failed for %s: %s", path.name, e)
        return None


def build_hash_index(files: list[Path]) -> dict[str, Path]:
    """
    Build a dict mapping SHA256-of-first-64KB → Path for all files.
    If two files share a hash (identical opening bytes), last one wins.
    Logs progress every 500 files — this is the slow step.
    """
    index: dict[str, Path] = {}
    total = len(files)
    for i, path in enumerate(files):
        if i > 0 and i % 500 == 0:
            log.info("Hashing: %d / %d files...", i, total)
        h = _file_hash(path)
        if h is not None:
            index[h] = path
    log.info("Hash index built: %d entries from %d files", len(index), total)
    return index


def build_fuzzy_index(files: list[Path]) -> dict[str, Path]:
    """
    Build a dict mapping lowercase filename stem → Path.
    Stem-only comparison avoids false mismatches from format conversions
    (e.g. track.mp3 → track.aiff shares stem "track" — will match at 1.0).

    If two files share a stem, the last one encountered wins and a warning
    is logged. This is intentional: the fuzzy index is a best-effort fallback,
    and stem collisions are rare in well-organized DJ libraries.
    """
    index: dict[str, Path] = {}
    for p in files:
        key = p.stem.lower()
        if key in index:
            log.warning(
                "Fuzzy index stem collision: %r — keeping %s, dropping %s",
                key, p.name, index[key].name,
            )
        index[key] = p
    return index


# ─── Match strategies ─────────────────────────────────────────────────────────

def _try_exact(
    original_path: str,
    old_root: Path,
    new_root: Path,
) -> Path | None:
    """
    Check if the file exists at the same relative path under new_root.
    E.g. old: /old/Artist/Album/track.mp3
         new: /new/Artist/Album/track.mp3
    """
    try:
        original = Path(original_path)
        relative = original.relative_to(old_root)
        candidate = new_root / relative
        if candidate.exists():
            return candidate
    except ValueError:
        log.debug("Path not relative to old_root: %s", original_path)
    return None


def _try_hash(
    original_path: str,
    hash_index: dict[str, Path],
) -> Path | None:
    """
    Hash the original file (if it still exists) and look it up in the index.

    If the original no longer exists at old_root (the common post-move case),
    returns None and the caller falls through to fuzzy. An OSError on an
    existing file (permissions, etc.) also returns None after logging.
    """
    original = Path(original_path)
    if not original.exists():
        return None
    h = _file_hash(original)
    if h is None:
        return None
    return hash_index.get(h)


def _try_fuzzy(
    original_path: str,
    fuzzy_index: dict[str, Path],
) -> Path | None:
    """
    Compare the original filename stem against all stems in new_root.
    Returns the best match above _FUZZY_CUTOFF, or None.
    """
    stem = Path(original_path).stem.lower()
    candidates = list(fuzzy_index.keys())
    matches = difflib.get_close_matches(stem, candidates, n=1, cutoff=_FUZZY_CUTOFF)
    if matches:
        return fuzzy_index[matches[0]]
    return None


# ─── Single track relocation ──────────────────────────────────────────────────

def _relocate_one(
    content_row: object,
    old_root: Path,
    new_root: Path,
    hash_index: dict[str, Path],
    fuzzy_index: dict[str, Path],
    db: Rekordbox6Database,
) -> RelocationResult:
    """
    Attempt to find and update the new path for a single DjmdContent row.
    Does not commit — caller owns batching.

    content_row type is annotated as object because pyrekordbox's ORM row
    types aren't cleanly importable in all versions. See TYPE_CHECKING import.
    """
    original_path = content_row.FolderPath
    content_id = str(content_row.ID)

    # Try strategies in priority order
    new_path: Path | None = None
    strategy: Literal["exact", "hash", "fuzzy", "not_found"] = "not_found"

    candidate = _try_exact(original_path, old_root, new_root)
    if candidate is not None:
        new_path = candidate
        strategy = "exact"
    else:
        candidate = _try_hash(original_path, hash_index)
        if candidate is not None:
            new_path = candidate
            strategy = "hash"
        else:
            candidate = _try_fuzzy(original_path, fuzzy_index)
            if candidate is not None:
                new_path = candidate
                strategy = "fuzzy"

    if new_path is None:
        log.warning("No match found for: %s", original_path)
        return RelocationResult(
            content_id=content_id,
            original_path=original_path,
            new_path=None,
            strategy="not_found",
            success=False,
        )

    # Update the path via pyrekordbox — check_path=True refuses to write if
    # the target file doesn't exist, guarding against fuzzy false positives.
    try:
        db.update_content_path(content_row, new_path, check_path=True)
        log.debug(
            "[%s] %s → %s",
            strategy,
            Path(original_path).name,
            new_path.name,
        )
        return RelocationResult(
            content_id=content_id,
            original_path=original_path,
            new_path=str(new_path),
            strategy=strategy,
            success=True,
        )
    except Exception as e:
        log.error(
            "update_content_path failed for %s: %s",
            Path(original_path).name, e,
        )
        return RelocationResult(
            content_id=content_id,
            original_path=original_path,
            new_path=str(new_path),
            strategy=strategy,
            success=False,
            error=str(e),
        )


# ─── Public interface ─────────────────────────────────────────────────────────

def relocate_directory(
    old_root: Path,
    new_root: Path,
    db: Rekordbox6Database,
) -> list[RelocationResult]:
    """
    Batch-update FolderPath for all DjmdContent rows under old_root.

    Parameters
    ----------
    old_root : Path
        The path prefix currently stored in the database. Does not need to
        exist on disk — it's a string prefix match against FolderPath values.
        A typo here will match zero rows and return an empty list with a
        warning; it will not corrupt the database.
    new_root : Path
        Where the files now live. Must exist.
    db : Rekordbox6Database
        Open write session (write_db()). Backup and process check are
        enforced by write_db() before this function is called.

    Returns
    -------
    list[RelocationResult]
        One entry per affected DjmdContent row.
    """
    if not new_root.is_dir():
        raise ValueError(f"new_root does not exist or is not a directory: {new_root}")

    old_root_str = str(old_root)
    try:
        all_content = db.get_content().all()
        affected = [
            c for c in all_content
            if c.FolderPath and c.FolderPath.startswith(old_root_str)
        ]
    except Exception as e:
        log.error("Failed to fetch content rows: %s", e)
        return []

    if not affected:
        log.warning(
            "No DB entries found with FolderPath under %s — "
            "check that old_root is the correct path prefix stored in the database",
            old_root,
        )
        return []

    log.info(
        "Relocating %d tracks: %s → %s",
        len(affected), old_root, new_root,
    )

    log.info("Scanning new_root for files...")
    new_files = _walk_audio_files(new_root)
    log.info("Building hash index (%d files)...", len(new_files))
    hash_index = build_hash_index(new_files)
    fuzzy_index = build_fuzzy_index(new_files)

    results: list[RelocationResult] = []
    batch_count = 0

    for content_row in affected:
        result = _relocate_one(
            content_row=content_row,
            old_root=old_root,
            new_root=new_root,
            hash_index=hash_index,
            fuzzy_index=fuzzy_index,
            db=db,
        )
        results.append(result)

        if result.success:
            batch_count += 1

        if batch_count >= BATCH_SIZE:
            try:
                db.commit()
                log.info("Committed batch of %d relocations", batch_count)
                batch_count = 0
            except Exception:
                log.exception("Batch commit failed — rolling back")
                db.rollback()
                raise

    # Final commit for remaining tail
    if batch_count > 0:
        try:
            db.commit()
            log.info("Final commit: %d relocations", batch_count)
        except Exception:
            log.exception("Final commit failed — rolling back")
            db.rollback()
            raise

    by_strategy: dict[str, int] = {}
    for r in results:
        by_strategy[r.strategy] = by_strategy.get(r.strategy, 0) + 1
    log.info(
        "Relocation complete — exact:%d hash:%d fuzzy:%d not_found:%d",
        by_strategy.get("exact", 0),
        by_strategy.get("hash", 0),
        by_strategy.get("fuzzy", 0),
        by_strategy.get("not_found", 0),
    )

    return results


# ─── Smoke test ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    sys.path.insert(0, ".")

    from config import DJMT_DB, MUSIC_ROOT
    from db_connection import read_db

    # ── Part 1: hash and fuzzy index build test (no DB needed) ──
    print("=== Hash index build test ===")
    test_dir = MUSIC_ROOT / "Kerri Chandler"
    if test_dir.exists():
        files = _walk_audio_files(test_dir)
        print(f"  Files found: {len(files)}")
        hash_idx = build_hash_index(files)
        fuzzy_idx = build_fuzzy_index(files)
        print(f"  Hash entries: {len(hash_idx)}")
        print(f"  Fuzzy entries: {len(fuzzy_idx)}")
        for stem, path in list(fuzzy_idx.items())[:5]:
            print(f"    {stem!r:40} → {path.name}")
    else:
        print(f"  SKIP: {test_dir} not found")

    # ── Part 2: dry-run match test (read-only DB) ──
    print("\n=== Match strategy dry-run (read-only) ===")
    with read_db(DJMT_DB) as db:
        all_content = db.get_content().all()
        sample = [
            c for c in all_content
            if c.FolderPath and str(MUSIC_ROOT) in c.FolderPath
        ][:20]

        if not sample:
            print("  No local tracks in DB yet — run importer first.")
        else:
            matched = 0
            for c in sample:
                result = _try_exact(c.FolderPath, MUSIC_ROOT, MUSIC_ROOT)
                if result:
                    matched += 1
            print(f"  Exact self-match: {matched}/{len(sample)} (should be {len(sample)})")

    print("\nSmoke test complete — no writes performed.")
    print("Real usage: relocate_directory(old_root, new_root, db) inside write_db() session.")
