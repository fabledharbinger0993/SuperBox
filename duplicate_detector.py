"""
rekordbox-toolkit / duplicate_detector.py

Finds acoustically identical audio files using Chromaprint fingerprinting
via the fpcalc binary (brew install chromaprint) and the pyacoustid wrapper.

This module ONLY reports duplicates — it never deletes, moves, or modifies
any file. The output is a CSV report for human review.

Within each duplicate group, files are ranked by the RARP deduplication
hierarchy to suggest which to keep:
  1. PN   — Pioneer Numbered (filename contains pattern like "01 -" or "001 ")
  2. MIK  — Mixed In Key tagged (TKEY tag present and non-empty)
  3. RAW  — Neither of the above

PN pattern note: _PN_PATTERN is anchored to the start of the filename stem
and requires a separator character (space, dot, dash, en-dash) immediately
after the digit block. This correctly identifies Pioneer-numbered files
(e.g. "01 - Title", "001 Title") and rejects mid-stem digit sequences like
"2 Bad", "100% Track", or "1984". The optional "track " prefix handles stems
like "Track 01 Something". Verify against a real sample during smoke testing.

Fingerprint note: acoustid.fingerprint_file may return the fingerprint as
bytes in some pyacoustid versions. fingerprint_file() decodes bytes to str
automatically so fp_map keys are always str.

Trash-rescue logic:
  After fingerprinting, any file whose ONLY known copy lives inside a
  trash or trash-adjacent folder is captured in ScanResult.unique_in_trash.
  These files are NOT included in the pruning CSV — they require manual
  rescue. SuperBox does not offer an automated step for this. A separate
  plain-text rescue report is written via write_trash_rescue_report().

  Two cases are covered:
    1. Truly unique: single fingerprint match, file is in a trash folder.
    2. Trapped KEEP: duplicate group where the best copy is in a trash
       folder. These stay in the CSV (marked KEEP, safe from the pruner)
       but are also listed in the rescue report and flagged in the CSV
       with keep_in_trash=True.

Public interface:
    fingerprint_file(path) -> str | None
    scan_duplicates(root)  -> ScanResult
    write_csv_report(result, output_path)
    write_trash_rescue_report(result, output_path)
"""

import concurrent.futures
import csv
import difflib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import acoustid
from mutagen import File as MutagenFile

from config import ACOUSTID_API_KEY, AUDIO_EXTENSIONS, MUSIC_ROOT, SKIP_DIRS, SKIP_PREFIXES

log = logging.getLogger(__name__)

_LOG_EVERY: int = 100

# ─── Trash detection ──────────────────────────────────────────────────────────

# Canonical trash-intent words. Checked against every folder component in the
# full path (case-insensitive), not just the immediate parent — so
# /Volumes/Drive/trash/subfolder/file.mp3 is caught even though the immediate
# parent is "subfolder".
#
# Exact matches are checked first (fast path). If no exact match, fuzzy
# matching kicks in: a folder component with similarity ≥ _TRASH_FUZZY_CUTOFF
# to any canonical word is also flagged. This catches common typos like
# "trahs", "recylce", "jnuk", "delet", "tosss", etc.
#
# Deliberate non-inclusions:
#   "old", "archive", "remove" — too broad; would catch legitimate folder names
#   like "old school", "archived sets", "removed vocals".
_TRASH_CANONICAL: frozenset[str] = frozenset({
    # trash family
    "trash",
    ".trash",
    "thrash",       # common typo
    # recycle family
    "recycle",
    "recycled",
    "recycling",
    "recycles",
    "$recycle.bin",
    # delete family
    "delete",
    "deleted",
    "deletes",
    "deletion",
    "to delete",
    "to_delete",
    "to-delete",
    # toss family
    "toss",
    "tossed",
    "tosses",
    # junk family
    "junk",
    "junked",
    "junk bin",
    "junk_bin",
    # purge family
    "purge",
    "purged",
    "purges",
    # discard family
    "discard",
    "discarded",
    "discards",
    # dump family
    "dump",
    "dumped",
    "dumps",
    # other clear intent
    "waste",
    "wasted",
    "garbage",
    ".deleted",
})

# Similarity threshold for fuzzy matching (0–1). 0.82 allows roughly 1–2
# character errors on typical 5–9 character words while avoiding false
# positives on short unrelated words.
_TRASH_FUZZY_CUTOFF: float = 0.82

# Minimum length for fuzzy matching — very short folder names (≤3 chars)
# have too many accidental near-matches to fuzz safely.
_TRASH_FUZZY_MIN_LEN: int = 4


def _folder_is_trash(name: str) -> bool:
    """
    Return True if a single folder name matches a trash-intent word.
    Checks exact match first, then fuzzy similarity.
    Also tokenises names that contain separators (spaces, underscores, dashes)
    so "to_delete" matches even if the canonical form is "to delete".
    """
    normalised = name.lower().strip()

    # Exact match
    if normalised in _TRASH_CANONICAL:
        return True

    # Normalise separators and re-check (handles "to_delete" vs "to delete")
    sep_normalised = normalised.replace("_", " ").replace("-", " ")
    if sep_normalised in _TRASH_CANONICAL:
        return True

    # Fuzzy match against each canonical word (skip very short names)
    if len(normalised) >= _TRASH_FUZZY_MIN_LEN:
        matches = difflib.get_close_matches(
            normalised,
            _TRASH_CANONICAL,
            n=1,
            cutoff=_TRASH_FUZZY_CUTOFF,
        )
        if matches:
            log.debug(
                "Trash fuzzy match: %r ~ %r (cutoff=%.2f)",
                name, matches[0], _TRASH_FUZZY_CUTOFF,
            )
            return True

    return False


def _is_trash_adjacent(path: Path) -> bool:
    """
    Return True if any component of path's parent folders is a trash folder.
    Checks exact and fuzzy matches against _TRASH_CANONICAL.
    """
    return any(_folder_is_trash(part) for part in path.parts)

# Pre-filter tolerances
_BPM_TOLERANCE_PCT: float = 0.03   # ±3% — accounts for detection variance
_DURATION_TOLERANCE_SEC: float = 3.0  # ±3 seconds


# ─── Scan index pre-filter ────────────────────────────────────────────────────

def _load_scan_index() -> dict[str, dict]:
    """Load scan_index.json written by audio_processor. Returns {} if absent."""
    index_path = Path.home() / "rekordbox-toolkit" / "scan_index.json"
    if not index_path.exists():
        return {}
    try:
        with open(index_path, encoding="utf-8") as f:
            entries = json.load(f)
        return {e["path"]: e for e in entries if "path" in e}
    except Exception as exc:
        log.warning("Could not load scan index: %s", exc)
        return {}


def _bpm_bucket(bpm_str: str | None) -> str | None:
    """Round BPM to nearest 3% bucket for grouping. Returns None if unparseable."""
    if not bpm_str:
        return None
    try:
        bpm = float(bpm_str)
        if bpm <= 0:
            return None
        # Bucket by rounding to nearest integer — close enough for ±3% grouping
        return str(round(bpm))
    except (ValueError, TypeError):
        return None


def _candidate_pairs(files: list[Path], index: dict[str, dict]) -> list[Path]:
    """
    Filter files to only those that have at least one potential duplicate
    based on matching key + BPM (±3%) + duration (±3s) from the scan index.
    Files not in the index are always included (conservative — don't skip unknowns).
    Returns deduplicated list of candidate files to fingerprint.
    """
    if not index:
        return files

    # Group files by (key, bpm_bucket) — duration checked per-pair below
    from collections import defaultdict
    buckets: dict[tuple, list[Path]] = defaultdict(list)
    no_index: list[Path] = []

    for path in files:
        entry = index.get(str(path))
        if not entry:
            no_index.append(path)
            continue
        key = entry.get("key") or "UNKNOWN"
        bpm_b = _bpm_bucket(entry.get("bpm")) or "UNKNOWN"
        buckets[(key, bpm_b)].append(path)

    # Within each bucket, check duration proximity
    candidates: set[Path] = set()
    for group_files in buckets.values():
        if len(group_files) < 2:
            continue
        # Pairwise duration check within the bucket
        for i, a in enumerate(group_files):
            for b in group_files[i + 1:]:
                dur_a = index.get(str(a), {}).get("duration_sec")
                dur_b = index.get(str(b), {}).get("duration_sec")
                if dur_a is None or dur_b is None or abs(dur_a - dur_b) <= _DURATION_TOLERANCE_SEC:
                    candidates.add(a)
                    candidates.add(b)

    result = list(candidates) + no_index
    skipped = len(files) - len(result)
    if skipped > 0:
        log.info(
            "Pre-filter: %d / %d files are candidates (skipped %d — no matching key+BPM+duration)",
            len(result), len(files), skipped,
        )
    return result

# Regex for Pioneer-numbered filename stems.
# Anchored to the start of the stem. Two forms are accepted:
#   1. "track " prefix followed by any digit count: "Track 01 Title", "track 5 - name"
#   2. Two or more digits followed by a separator:  "01 - Title", "001 Title", "12 Step"
#
# Single-digit stems (e.g. "1. Title") are intentionally excluded — they are
# indistinguishable from titles like "2 Bad" or "1 Trick Pony". Pioneer CDJ
# exports consistently zero-pad single-digit track numbers (01, not 1), so
# the loss of genuine single-digit PN files is minimal in practice.
#
# The separator group [\s.\-\u2013]+ excludes stems like "1984" or "100k"
# (no separator immediately after digits) and "1-800" (digit follows the dash).
_PN_PATTERN = re.compile(
    r"^(?:(?:track\s+\d{1,3})|(?:\d{2,3}))[\s.\-\u2013]+",
    re.IGNORECASE,
)


# ─── Hierarchy ranking ────────────────────────────────────────────────────────

def _has_key_tag(path: Path) -> bool:
    """Return True if the file has a non-empty TKEY or initialkey tag."""
    try:
        audio = MutagenFile(str(path), easy=False)
        if audio is None or audio.tags is None:
            return False
        # ID3 (MP3/AIFF)
        frame = audio.tags.get("TKEY")
        if frame is not None and str(frame).strip():
            return True
        # Vorbis (FLAC/OGG)
        vc = audio.tags.get("initialkey")
        if vc and str(vc[0]).strip():
            return True
        return False
    except Exception:
        return False


def _rank_file(path: Path) -> int:
    """
    Return hierarchy rank for deduplication priority.
    Lower number = higher priority = recommended to keep.
      0 = PN  (Pioneer Numbered)
      1 = MIK (Mixed In Key tagged)
      2 = RAW
    """
    stem = path.stem
    if _PN_PATTERN.search(stem):
        return 0
    if _has_key_tag(path):
        return 1
    return 2


_RANK_LABELS = {0: "PN", 1: "MIK", 2: "RAW"}


# ─── Data structures ──────────────────────────────────────────────────────────

@dataclass
class DuplicateGroup:
    """A set of files that share an acoustic fingerprint."""
    fingerprint: str
    files: list[Path]
    recommended_keep: Path
    recommended_remove: list[Path]
    ranks: dict[str, str] = field(default_factory=dict)   # path str → rank label
    keep_in_trash: bool = False  # True when the recommended_keep lives in a trash folder
    # AcoustID / MusicBrainz enrichment (populated only when an API key is configured)
    recording_id: str | None = None
    mb_title:     str | None = None
    mb_artist:    str | None = None


@dataclass
class ScanResult:
    """
    Output of scan_duplicates().

    groups          — duplicate groups (2+ files sharing a fingerprint).
                      Safe to pass to write_csv_report().
    unique_in_trash — files with no duplicate anywhere in the scan that
                      live inside a trash or trash-adjacent folder.
                      These are NOT in the CSV. They require manual rescue.
                      SuperBox does not offer an automated step for these.
    """
    groups: list[DuplicateGroup]
    unique_in_trash: list[Path]


# ─── Fingerprinting ───────────────────────────────────────────────────────────

def fingerprint_file(path: Path) -> str | None:
    """
    Compute the Chromaprint acoustic fingerprint for an audio file.
    Uses fpcalc via pyacoustid. Returns the fingerprint as a str, or None
    on failure.

    pyacoustid may return the fingerprint as bytes in some versions — this
    function always decodes to str so fp_map keys are consistent.

    fpcalc default: analyses first 120 seconds of audio — sufficient for DJ use.
    """
    result = _fingerprint_with_duration(path)
    return result[1] if result is not None else None


def _fingerprint_with_duration(path: Path) -> tuple[float, str] | None:
    """
    Same as fingerprint_file() but also returns the track duration in seconds.
    Used internally by scan_duplicates() so the AcoustID lookup can reuse the
    duration without re-running fpcalc.
    """
    try:
        duration, fingerprint = acoustid.fingerprint_file(str(path))
        if not fingerprint:
            log.warning("Empty fingerprint for %s", path.name)
            return None
        if isinstance(fingerprint, bytes):
            fingerprint = fingerprint.decode("utf-8", errors="replace")
        return float(duration), fingerprint
    except acoustid.FingerprintGenerationError as e:
        log.error("Fingerprint failed for %s: %s", path.name, e)
        return None
    except Exception as e:
        log.error("Unexpected error fingerprinting %s: %s", path.name, e)
        return None


def _acoustid_lookup(
    api_key: str,
    fingerprint: str,
    duration: float,
) -> tuple[str | None, str | None, str | None]:
    """
    Submit a pre-computed fingerprint to the AcoustID web service.
    Returns (recording_id, title, artist) for the best match, or
    (None, None, None) on failure or no match.

    Uses acoustid.lookup() so fpcalc is NOT re-run — the fingerprint and
    duration captured during the local scan pass are reused.
    """
    try:
        response = acoustid.lookup(
            api_key,
            fingerprint,
            duration,
            meta=["recordings"],
        )
        best_score = 0.0
        best = (None, None, None)
        for score, rid, title, artist in acoustid.parse_lookup_result(response):
            if score > best_score:
                best_score = score
                best = (rid, title, artist)
        return best
    except acoustid.WebServiceError as e:
        log.warning("AcoustID lookup failed: %s", e)
        return None, None, None
    except Exception as e:
        log.warning("AcoustID lookup error: %s", e)
        return None, None, None


# ─── Filesystem walk ──────────────────────────────────────────────────────────

def _walk_audio_files(root: Path) -> list[Path]:
    """Walk root and return all audio files, respecting skip rules."""
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


# ─── Duplicate scanning ───────────────────────────────────────────────────────

def scan_duplicates(
    root: "Path | list[Path]",
    *,
    max_workers: int = 1,
    pause_seconds: float = 0.0,
) -> ScanResult:
    """
    Fingerprint all audio files under root (or multiple roots) and return
    groups of duplicates.

    CPU-intensive. For 50k files expect 10–30 minutes depending on hardware.
    Run as an explicit command, not as part of the import workflow.

    Parameters
    ----------
    root : Path | list[Path]
        Directory (or list of directories) to scan recursively.
        All paths are combined into a single fingerprint pool before comparison,
        so duplicates that span multiple source folders are detected correctly.
    max_workers : int
        Number of files to fingerprint in parallel using fpcalc subprocesses.
        Default 1 (sequential). fpcalc is subprocess-safe so workers > 1
        is safe, but each worker spawns its own fpcalc process — be cautious
        on machines with limited cores or when DJing simultaneously.
    pause_seconds : float
        Seconds to sleep between files in sequential mode. Default 0.0.

    Returns
    -------
    list[DuplicateGroup]
        Only groups with 2+ files are returned. Unique files are not included.
        Groups are sorted by size descending (largest first).
    """
    roots = [root] if isinstance(root, Path) else list(root)
    for r in roots:
        if not r.is_dir():
            raise ValueError(f"scan_duplicates: {r} is not a directory")

    log.info("Walking %d root(s) for audio files...", len(roots))
    all_files: list[Path] = []
    for r in roots:
        found = _walk_audio_files(r)
        log.info("  %s → %d files", r, len(found))
        all_files.extend(found)
    log.info("Found %d audio files total", len(all_files))

    # Apply pre-filter from scan index if available
    index = _load_scan_index()
    if index:
        files = _candidate_pairs(all_files, index)
        print(
            f"SUPERBOX_PREFILTER: "
            + json.dumps({
                "total": len(all_files),
                "candidates": len(files),
                "skipped": len(all_files) - len(files),
            }),
            flush=True,
        )
    else:
        files = all_files
        log.info("No scan index found — fingerprinting all files (run Tag Tracks first to speed this up)")

    total = len(files)
    log.info(
        "Beginning fingerprint pass on %d files "
        "(workers=%d pause=%.1fs)",
        total, max_workers, pause_seconds,
    )

    fp_map: dict[str, list[Path]] = {}
    dur_map: dict[str, float] = {}  # fingerprint → duration of first file seen
    failed = 0
    completed = 0

    def _fingerprint_one(path: Path) -> tuple[Path, float | None, str | None]:
        result = _fingerprint_with_duration(path)
        if result is None:
            return path, None, None
        return path, result[0], result[1]

    if max_workers > 1:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = {ex.submit(_fingerprint_one, p): p for p in files}
            for future in concurrent.futures.as_completed(futures):
                completed += 1
                try:
                    path, dur, fp = future.result()
                except Exception as exc:
                    log.error("Unexpected fingerprint error: %s", exc)
                    failed += 1
                    continue
                if fp is None:
                    failed += 1
                else:
                    fp_map.setdefault(fp, []).append(path)
                    dur_map.setdefault(fp, dur)
                if completed % _LOG_EVERY == 0:
                    log.info(
                        "Fingerprinting: %d / %d  (failures: %d)",
                        completed, total, failed,
                    )
    else:
        for i, path in enumerate(files):
            if i > 0 and i % _LOG_EVERY == 0:
                log.info(
                    "Fingerprinting: %d / %d  (failures so far: %d)",
                    i, total, failed,
                )
            result = _fingerprint_with_duration(path)
            if result is None:
                failed += 1
            else:
                dur, fp = result
                fp_map.setdefault(fp, []).append(path)
                dur_map.setdefault(fp, dur)
            if pause_seconds > 0 and i < total - 1:
                time.sleep(pause_seconds)

    log.info(
        "Fingerprint pass complete — %d unique prints, %d failures",
        len(fp_map), failed,
    )

    groups: list[DuplicateGroup] = []
    unique_in_trash: list[Path] = []

    for fp, paths in fp_map.items():
        if len(paths) < 2:
            # Single copy — if it's in trash, this is a rescue candidate
            if _is_trash_adjacent(paths[0]):
                unique_in_trash.append(paths[0])
            continue

        ranked = sorted(paths, key=_rank_file)
        keep = ranked[0]
        remove = ranked[1:]
        ranks = {str(p): _RANK_LABELS[_rank_file(p)] for p in paths}

        groups.append(DuplicateGroup(
            fingerprint=fp,
            files=paths,
            recommended_keep=keep,
            recommended_remove=remove,
            ranks=ranks,
            keep_in_trash=_is_trash_adjacent(keep),
        ))

    groups.sort(key=lambda g: len(g.files), reverse=True)

    # ── AcoustID enrichment ───────────────────────────────────────────────────
    # If an API key is configured, submit each group's fingerprint to the
    # AcoustID web service to get the canonical MusicBrainz recording ID,
    # title, and artist. Rate-limited to ≤3 req/s per AcoustID ToS.
    if ACOUSTID_API_KEY and groups:
        log.info(
            "AcoustID enrichment: looking up %d groups (≤3 req/s)…", len(groups)
        )
        _ACOUSTID_DELAY = 0.34  # seconds between requests (just under 3/s limit)
        for i, group in enumerate(groups):
            dur = dur_map.get(group.fingerprint)
            if dur is not None:
                rid, title, artist = _acoustid_lookup(
                    ACOUSTID_API_KEY, group.fingerprint, dur
                )
                group.recording_id = rid
                group.mb_title     = title
                group.mb_artist    = artist
            if i < len(groups) - 1:
                time.sleep(_ACOUSTID_DELAY)
        log.info("AcoustID enrichment complete.")

    trapped_keep_count = sum(1 for g in groups if g.keep_in_trash)

    log.info(
        "Duplicate scan complete — %d groups, %d duplicate files",
        len(groups),
        sum(len(g.recommended_remove) for g in groups),
    )
    if unique_in_trash:
        log.warning(
            "RESCUE REQUIRED: %d unique tracks exist ONLY inside trash folders — "
            "see rescue report. SuperBox will NOT include these in the pruning CSV.",
            len(unique_in_trash),
        )
    if trapped_keep_count:
        log.warning(
            "RESCUE REQUIRED: %d duplicate groups have their best copy inside a "
            "trash folder — these are marked keep_in_trash=True in the CSV.",
            trapped_keep_count,
        )

    return ScanResult(groups=groups, unique_in_trash=unique_in_trash)


# ─── CSV report ───────────────────────────────────────────────────────────────

def write_csv_report(
    result: ScanResult,
    output_path: Path,
) -> None:
    """
    Write duplicate groups to a CSV file for human review.

    Unique-in-trash files (result.unique_in_trash) are intentionally excluded
    from this CSV. They appear only in the rescue report. The pruning tool must
    never act on them.

    Columns:
        group_id        — integer, same for all files in a group
        action          — "KEEP" or "REVIEW_REMOVE"
        rank            — PN / MIK / RAW
        file_path       — absolute path
        file_size_mb    — file size in MB (2 decimal places)
        bpm             — from TBPM tag, or blank
        key             — from TKEY tag, or blank
        filename        — basename only (for quick scanning)
        keep_in_trash   — "YES" when this group's KEEP copy is inside a trash
                          folder (applies to all rows in that group). These
                          tracks need manual rescue before the trash is cleared.

    BPM and key are read live from file tags, not from the database. This
    means the report is valid before import and reflects raw tag values.

    Parameters
    ----------
    result : ScanResult
        Output of scan_duplicates().
    output_path : Path
        Where to write the CSV. Parent directory is created if absent.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)

    rows_written = 0
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "group_id", "action", "rank", "file_path",
            "file_size_mb", "bpm", "key", "filename", "keep_in_trash",
            "mb_recording_id", "mb_title", "mb_artist",
        ])
        writer.writeheader()

        for group_id, group in enumerate(result.groups, start=1):
            for path in group.files:
                action = "KEEP" if path == group.recommended_keep else "REVIEW_REMOVE"
                rank = group.ranks.get(str(path), "RAW")

                bpm_str = ""
                key_str = ""
                try:
                    audio = MutagenFile(str(path), easy=False)
                    if audio and audio.tags:
                        tbpm = audio.tags.get("TBPM")
                        if tbpm:
                            bpm_str = str(tbpm).strip()
                        tkey = audio.tags.get("TKEY")
                        if tkey:
                            key_str = str(tkey).strip()
                except Exception:
                    pass

                try:
                    size_mb = round(path.stat().st_size / (1024 * 1024), 2)
                except OSError:
                    size_mb = 0.0

                writer.writerow({
                    "group_id": group_id,
                    "action": action,
                    "rank": rank,
                    "file_path": str(path),
                    "file_size_mb": size_mb,
                    "bpm": bpm_str,
                    "key": key_str,
                    "filename": path.name,
                    "keep_in_trash": "YES" if group.keep_in_trash else "",
                    "mb_recording_id": group.recording_id or "",
                    "mb_title":        group.mb_title or "",
                    "mb_artist":       group.mb_artist or "",
                })
                rows_written += 1

    log.info(
        "CSV report written: %s (%d rows, %d groups)",
        output_path, rows_written, len(result.groups),
    )


# ─── Trash rescue report ─────────────────────────────────────────────────────

def write_trash_rescue_report(
    result: ScanResult,
    output_path: Path,
) -> None:
    """
    Write a plain-text rescue report for tracks that need manual intervention
    before any trash folder is cleared.

    Two categories are reported:

      SECTION 1 — Unique tracks in trash (result.unique_in_trash):
        These files have NO duplicate anywhere in the scan. Their only known
        copy is inside a trash or trash-adjacent folder. SuperBox does not
        offer an automated rescue step for these. The user must manually move
        them to a safe location. They are NOT in the pruning CSV.

      SECTION 2 — Trapped KEEP copies (groups where keep_in_trash=True):
        These are duplicate groups where the best surviving copy happens to
        live inside a trash folder (all better copies were already deleted or
        never existed elsewhere). The pruner will not delete them (they are
        marked KEEP), but if the trash folder is manually cleared they will
        be lost. Move them to a safe location before clearing any trash.

    Parameters
    ----------
    result : ScanResult
        Output of scan_duplicates().
    output_path : Path
        Where to write the report. Parent directory is created if absent.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)

    trapped_keeps = [g for g in result.groups if g.keep_in_trash]
    unique_count = len(result.unique_in_trash)
    trapped_count = len(trapped_keeps)
    total_at_risk = unique_count + trapped_count

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with open(output_path, "w", encoding="utf-8") as f:
        w = f.write

        w("╔══════════════════════════════════════════════════════════════════════╗\n")
        w("║          !!!  SUPERBOX TRASH RESCUE REPORT  !!!                     ║\n")
        w("║                                                                      ║\n")
        w("║  STOP. READ THIS BEFORE DELETING ANYTHING.                          ║\n")
        w("║                                                                      ║\n")
        w("║  The tracks in this report exist ONLY inside trash or trash-         ║\n")
        w("║  adjacent folders. If you clear those folders, these tracks are      ║\n")
        w("║  PERMANENTLY GONE. SuperBox does not offer an automated rescue       ║\n")
        w("║  step — you must move these files manually before proceeding.        ║\n")
        w("╚══════════════════════════════════════════════════════════════════════╝\n")
        w(f"\n  Generated : {now}\n")
        w(f"  Unique tracks with NO copy outside trash     : {unique_count}\n")
        w(f"  Duplicate groups whose best copy is in trash : {trapped_count}\n")
        w(f"  Total tracks at risk                         : {total_at_risk}\n")
        w("\n")

        # ── Section 1: Truly unique tracks in trash ───────────────────────────
        w("━" * 72 + "\n")
        w("  SECTION 1 OF 2 — UNIQUE TRACKS (no copy exists outside trash)\n")
        w("━" * 72 + "\n")
        w("\n")
        if not result.unique_in_trash:
            w("  None found.\n")
        else:
            w(f"  {unique_count} tracks have their ONLY known copy in a trash folder.\n")
            w("  These are NOT in the pruning CSV. SuperBox will never touch them.\n")
            w("  You must move them to a safe location manually.\n")
            w("\n")
            for path in sorted(result.unique_in_trash, key=lambda p: p.name.lower()):
                w(f"  {path}\n")
        w("\n")

        # ── Section 2: Trapped KEEP copies ────────────────────────────────────
        w("━" * 72 + "\n")
        w("  SECTION 2 OF 2 — TRAPPED KEEPS (best copy is in trash)\n")
        w("━" * 72 + "\n")
        w("\n")
        if not trapped_keeps:
            w("  None found.\n")
        else:
            w(f"  {trapped_count} duplicate groups have their recommended KEEP inside a trash\n")
            w("  folder. The pruner will NOT delete them, but clearing trash manually\n")
            w("  would lose them. Move the KEEP file to a safe location first.\n")
            w("\n")
            for group in sorted(trapped_keeps, key=lambda g: g.recommended_keep.name.lower()):
                w(f"  KEEP  → {group.recommended_keep}\n")
                for rem in group.recommended_remove:
                    w(f"  DUPE  → {rem}\n")
                w("\n")

        w("━" * 72 + "\n")
        w("  END OF REPORT\n")
        w("━" * 72 + "\n")

    log.warning(
        "Trash rescue report written: %s  (%d unique-in-trash, %d trapped keeps)",
        output_path, unique_count, trapped_count,
    )


# ─── Smoke test ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    sys.path.insert(0, ".")

    from config import MUSIC_ROOT

    # ── Part 1: single file fingerprint test ──
    print("=== Single file fingerprint test ===")
    test_file = MUSIC_ROOT / "Kerri Chandler" / "Sunset - So Let The Wind Come" / "02 Sunset - So Let The Wind Come.mp3"
    if test_file.exists():
        fp = fingerprint_file(test_file)
        if fp:
            print(f"  File    : {test_file.name}")
            print(f"  Rank    : {_RANK_LABELS[_rank_file(test_file)]}")
            print(f"  FP type : {type(fp).__name__}  (should be str)")
            print(f"  FP len  : {len(fp)} chars")
            print(f"  FP head : {fp[:60]}...")
        else:
            print("  Fingerprint failed — check fpcalc is installed (brew install chromaprint)")
    else:
        print(f"  SKIP: {test_file} not found")

    # ── Part 2: small directory scan ──
    print("\n=== Duplicate scan (Kerri Chandler only) ===")
    test_root = MUSIC_ROOT / "Kerri Chandler"
    if test_root.exists():
        result = scan_duplicates(test_root)
        print(f"  Duplicate groups found  : {len(result.groups)}")
        print(f"  Unique-in-trash         : {len(result.unique_in_trash)}")
        for g in result.groups[:3]:
            print(f"\n  Group ({len(g.files)} files) keep_in_trash={g.keep_in_trash}:")
            for p in g.files:
                action = "KEEP  " if p == g.recommended_keep else "REMOVE"
                print(f"    [{action}] [{g.ranks[str(p)]}] {p.name}")

        if result.groups or result.unique_in_trash:
            out = Path.home() / "rekordbox-toolkit" / "duplicate_report_test.csv"
            rescue_out = Path.home() / "rekordbox-toolkit" / "trash_rescue_report_test.txt"
            write_csv_report(result, out)
            write_trash_rescue_report(result, rescue_out)
            print(f"\n  CSV written to    : {out}")
            print(f"  Rescue report to  : {rescue_out}")
    else:
        print(f"  SKIP: {test_root} not found")

    print("\nSmoke test complete — no files modified.")
