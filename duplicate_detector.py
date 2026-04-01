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

Public interface:
    fingerprint_file(path) -> str | None
    scan_duplicates(root)  -> list[DuplicateGroup]
    write_csv_report(groups, output_path)
"""

import concurrent.futures
import csv
import json
import logging
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

from mutagen import File as MutagenFile

from config import AUDIO_EXTENSIONS, MUSIC_ROOT, SKIP_DIRS, SKIP_PREFIXES

log = logging.getLogger(__name__)

_LOG_EVERY: int = 100

# Pre-filter tolerances
_BPM_TOLERANCE_PCT: float = 0.03   # ±3% — accounts for detection variance
_DURATION_TOLERANCE_SEC: float = 3.0  # ±3 seconds

# Fingerprint analysis window.
# DJ tracks often have 16–32 bar intros before harmonic content arrives.
# Skipping 30 s lands us in the body of most house/techno/dance tracks,
# giving Chromaprint the melodic material it needs for reliable matching.
# Changing either constant invalidates existing cache entries (they store
# fp_offset and fp_length so mismatches are detected automatically).
_FP_OFFSET_SEC: int = 45   # seconds to skip at the start
_FP_LENGTH_SEC: int = 45   # seconds to analyse after the offset

# Filename similarity gate.
# After fingerprints match, at least one pair in the group must have filenames
# this similar (0.0–1.0) or the group is rejected as a false positive.
# Catches shared sample loops and common breakdowns that hash identically.
# 0.4 = 40% character overlap after normalisation — loose enough for
# "Track_PN.mp3" vs "Track (Extended).aiff", tight enough to reject
# "Artist A - Song X.mp3" vs "Artist B - Song Y.mp3".
_FILENAME_SIMILARITY_MIN: float = 0.4

# ─── Version-tag constants ────────────────────────────────────────────────────
#
# After the acoustic fingerprint gate passes, a fingerprint group may still
# contain different *releases* of the same recording — e.g. the original and a
# dub mix share enough audio to match. These are not duplicate copies; they are
# distinct products that should be kept. The version-tag splitter separates
# them by extracting a normalised version phrase from each filename and
# bucketing: files with the same tag (or both with no tag) remain as
# duplicates; files with *different* tags become separate groups.

# Core keywords that mark a file as a specific version/mix.
# Matched case-insensitively within the extracted phrase.
_VERSION_CORE_KW = re.compile(
    r'\b(remix|rmx|dub|mix|instrumental|instru|edit|version|vip|flip|'
    r'rework|bootleg|mashup|remaster(?:ed)?|reprise|revision|reconstruction)\b',
    re.IGNORECASE,
)

# Version phrases that mean "original release" — treated the same as no tag so
# a bare "Track.aiff" and "Track - Original Mix.aiff" are still flagged as
# duplicates of each other (they almost always are).
_ORIGINAL_TAG_NORM: frozenset[str] = frozenset({
    'original', 'original mix', 'original version', 'original edit',
    'original club mix', 'main mix', 'main', 'album version', 'album mix',
})


def _normalise_stem(path: Path) -> str:
    """
    Strip noise from a filename stem for similarity comparison.
    Removes: leading track numbers, _PN/_MIK suffixes, parenthetical
    version tags, extra whitespace. Lowercases everything.
    """
    s = path.stem.lower()
    s = re.sub(r'_pn(_pn)*$', '', s)           # _PN, _PN_PN
    s = re.sub(r'_mik$', '', s)                 # _MIK
    s = re.sub(r'^\d{1,3}[\s.\-–]+', '', s)    # leading track numbers
    s = re.sub(r'\s*[\(\[].*(remix|edit|mix|dub|version|extended|radio|inst).*[\)\]]', '', s)
    s = re.sub(r'[-_]+', ' ', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s


def _filename_similarity(a: Path, b: Path) -> float:
    """Return 0.0–1.0 similarity between two track filenames after normalisation."""
    return SequenceMatcher(None, _normalise_stem(a), _normalise_stem(b)).ratio()


def _extract_version_tag(path: Path) -> str | None:
    """
    Extract and normalise a version tag from a filename stem.

    Scans the stem for a version phrase (remix, dub, mix, instrumental,
    edit, etc.) at the tail, inside parentheses/brackets, or after a dash
    separator — in that order of preference. Returns a lowercase,
    whitespace-normalised string, or None when:
      • no version keyword is found, or
      • the phrase is an "original" variant ("Original Mix", etc.), which is
        treated identically to having no version tag at all.

    Examples
    --------
    "Track - Club Mix.aiff"             → "club mix"
    "Artist - Track [Danny Dub].mp3"    → "danny dub"
    "Track (Deep Mix).aiff"             → "deep mix"
    "01 - Track Remix.mp3"              → "remix"
    "Track Instrumental.aiff"           → "instrumental"
    "Track - Original Mix.aiff"         → None   (original = base version)
    "Artist - Track Name.aiff"          → None   (no version)
    """
    stem = path.stem
    # Strip leading track numbers and PN/MIK suffixes so they don't
    # accidentally land inside the version-phrase window.
    stem = re.sub(r'^(?:track\s+)?\d{1,3}[\s.\-\u2013\u2014]+', '', stem, flags=re.IGNORECASE)
    stem = re.sub(r'_(?:pn|mik)(?:_pn)*$', '', stem, flags=re.IGNORECASE)

    candidate: str | None = None

    # ① Last parenthetical or bracketed suffix — most explicit form.
    m = re.search(r'[\(\[]\s*([^()\[\]]{2,60}?)\s*[\)\]]\s*$', stem)
    if m and _VERSION_CORE_KW.search(m.group(1)):
        candidate = m.group(1)

    # ② Dash-separated suffix (no bracketed version found).
    if candidate is None:
        m = re.search(r'[-\u2013\u2014]\s*(.{2,60}?)\s*$', stem)
        if m and _VERSION_CORE_KW.search(m.group(1)):
            candidate = m.group(1)

    # ③ Bare version keyword sitting at the very end of the stem.
    if candidate is None:
        m = re.search(
            r'(?:^|\s)((?:[\w]+\s+){0,3}'
            r'(?:remix|rmx|dub|mix|instrumental|instru|edit|vip|flip|rework|bootleg))'
            r'\s*$',
            stem,
            re.IGNORECASE,
        )
        if m:
            candidate = m.group(1)

    if candidate is None:
        return None

    tag = re.sub(r'\s+', ' ', candidate.lower()).strip()

    # "original mix" and equivalents → same tier as no version tag
    if tag in _ORIGINAL_TAG_NORM:
        return None

    return tag


def _split_group_by_version(paths: list[Path]) -> list[list[Path]]:
    """
    Split a fingerprint-matched group into sub-groups by version tag.

    Files with the same version tag (or both with None) stay in the same
    sub-group — they are duplicate copies of the same release. Files with
    *different* tags are separated — they are different releases of the same
    recording (e.g. "Club Mix" vs "Dub Mix") and should not be flagged as
    duplicates of each other.

    Sub-groups with fewer than 2 files are omitted (a single file has no
    duplicate within its version).

    Returns the original list unchanged (wrapped in a list) when no
    version differences are detected, keeping the common-case overhead to
    one set-of-tags comparison.
    """
    from collections import defaultdict  # noqa: PLC0415

    tags = {p: _extract_version_tag(p) for p in paths}
    unique_tags = set(tags.values())

    if len(unique_tags) == 1:
        # All files share the same version tag (or all None) — nothing to split.
        return [paths]

    buckets: dict[str | None, list[Path]] = defaultdict(list)
    for path, tag in tags.items():
        buckets[tag].append(path)

    singletons = [p for grp in buckets.values() if len(grp) == 1 for p in grp]
    if singletons:
        log.debug(
            "Version split: %d file(s) have no same-version duplicate and will be "
            "excluded from all groups: %s",
            len(singletons),
            [p.name for p in singletons],
        )

    return [grp for grp in buckets.values() if len(grp) >= 2]


# Build an extended environment for subprocess calls so fpcalc is found
# even when the server process has a minimal PATH (e.g. launched via Automator).
_FPCALC_ENV = os.environ.copy()
_FPCALC_ENV["PATH"] = "/opt/homebrew/bin:/usr/local/bin:" + _FPCALC_ENV.get("PATH", "")


def _find_fpcalc() -> str:
    """Locate fpcalc binary, checking PATH (extended) then common Homebrew paths."""
    found = shutil.which("fpcalc", path=_FPCALC_ENV["PATH"])
    if found:
        return found
    for candidate in ["/opt/homebrew/bin/fpcalc", "/usr/local/bin/fpcalc"]:
        if Path(candidate).is_file():
            return candidate
    return "fpcalc"   # will produce a clear FileNotFoundError at call time


def _check_fpcalc_offset_support(fpcalc: str) -> bool:
    """
    Return True if this fpcalc build supports the -offset flag.
    -offset was added in chromaprint 1.5.0 (2020). Older Homebrew installs
    may not have it. Upgrade with: brew upgrade chromaprint
    """
    try:
        # Pass a nonexistent file — we only care whether fpcalc rejects
        # -offset before it even tries to open the file.
        result = subprocess.run(
            [fpcalc, "-offset", "0", "-length", "1", "__superbox_offset_check__"],
            capture_output=True, text=True, timeout=5, env=_FPCALC_ENV,
        )
        return "Unknown option -offset" not in result.stderr
    except Exception:
        return False


_FPCALC = _find_fpcalc()
_FPCALC_OFFSET_OK = _check_fpcalc_offset_support(_FPCALC)

log_startup = logging.getLogger(__name__)
log_startup.debug("fpcalc resolved to: %s", _FPCALC)
if not _FPCALC_OFFSET_OK:
    log_startup.warning(
        "fpcalc does not support -offset (chromaprint < 1.5.0) — "
        "fingerprinting from the start of each file. "
        "Upgrade for intro-skipping: brew upgrade chromaprint"
    )


# ─── Scan index pre-filter ────────────────────────────────────────────────────

_STREAMING_PREFIXES = ("soundcloud:", "tidal:", "beatport-streaming:")


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


def _load_db_index() -> dict[str, dict]:
    """
    Load BPM, key, and duration for every imported track directly from the
    Rekordbox database (read-only — safe while Rekordbox is open).

    The DB is the authoritative source: it covers all 27k+ imported tracks,
    not just files that have been through audio_processor since the last run.

    BPM is stored in DjmdContent as int×100 (e.g. 128 BPM → 12800).
    KeyID is a FK to DjmdKey.ScaleName (e.g. "Am", "C#m").
    TotalTime is stored in milliseconds.

    Returns {} on any failure — pre-filter gracefully falls back to scan_index.
    """
    try:
        from db_connection import read_db   # noqa: PLC0415
        from config import DJMT_DB          # noqa: PLC0415

        with read_db(DJMT_DB) as db:
            # Build KeyID → ScaleName lookup
            key_map: dict[str, str] = {
                str(k.ID): k.ScaleName
                for k in db.get_key().all()
            }

            index: dict[str, dict] = {}
            for track in db.get_content().all():
                path_str = track.FolderPath
                if not path_str:
                    continue
                if any(path_str.startswith(p) for p in _STREAMING_PREFIXES):
                    continue

                # BPM: stored as int×100 — divide to get real BPM
                bpm: str | None = None
                try:
                    if track.BPM and int(track.BPM) > 0:
                        bpm = str(round(int(track.BPM) / 100, 2))
                except (TypeError, ValueError):
                    pass

                # Key: resolve via KeyID → DjmdKey.ScaleName
                key: str | None = None
                try:
                    if track.KeyID:
                        key = key_map.get(str(track.KeyID))
                except (TypeError, AttributeError):
                    pass

                # Duration: TotalTime is in milliseconds in Rekordbox 6
                duration_sec: float | None = None
                try:
                    tt = track.TotalTime
                    if tt and int(tt) > 0:
                        duration_sec = round(int(tt) / 1000, 1)
                except (TypeError, ValueError, AttributeError):
                    pass

                index[path_str] = {
                    "path":         path_str,
                    "bpm":          bpm,
                    "key":          key,
                    "duration_sec": duration_sec,
                }

        log.info("DB index loaded: %d imported tracks", len(index))
        return index

    except Exception as exc:
        log.warning("Could not load DB index (will use scan_index only): %s", exc)
        return {}


def _merge_indices(db_index: dict[str, dict], scan_index: dict[str, dict]) -> dict[str, dict]:
    """
    Merge DB index and scan index into a single pre-filter index.

    Strategy:
      - DB wins for bpm and key (authoritative — what Rekordbox has stored).
      - scan_index fills in duration_sec where DB has none (audio_processor
        measures actual audio duration; DB TotalTime may be 0 for some tracks).
      - Files only in scan_index (not yet imported) are included as-is.
    """
    merged: dict[str, dict] = {}
    for path in set(db_index) | set(scan_index):
        db  = db_index.get(path, {})
        si  = scan_index.get(path, {})
        merged[path] = {
            "path":         path,
            "bpm":          db.get("bpm")          or si.get("bpm"),
            "key":          db.get("key")          or si.get("key"),
            "duration_sec": db.get("duration_sec") or si.get("duration_sec"),
        }
    return merged


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


# ─── Fingerprinting ───────────────────────────────────────────────────────────

def fingerprint_file(
    path: Path,
    offset: int = _FP_OFFSET_SEC,
    length: int = _FP_LENGTH_SEC,
) -> str | None:
    """
    Compute the Chromaprint acoustic fingerprint for an audio file by calling
    fpcalc directly as a subprocess.

    Parameters
    ----------
    path : Path
        Audio file to fingerprint.
    offset : int
        Seconds to skip at the start of the file before analysis begins.
        Default _FP_OFFSET_SEC (30 s) — skips DJ intros so the fingerprint
        captures the harmonic body of the track, not the intro percussion.
    length : int
        Seconds of audio to analyse after the offset.
        Default _FP_LENGTH_SEC (60 s) — enough for reliable Chromaprint matching.

    Returns
    -------
    str or None
        Fingerprint string, or None on any failure.
    """
    try:
        cmd = [_FPCALC, "-json", "-length", str(length)]
        if offset > 0 and _FPCALC_OFFSET_OK:
            cmd += ["-offset", str(offset)]
        cmd.append(str(path))
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=90,
            env=_FPCALC_ENV,
        )
        if result.returncode != 0:
            log.error("fpcalc error for %s: %s", path.name, result.stderr.strip())
            return None
        data = json.loads(result.stdout)
        fp = data.get("fingerprint")
        if not fp:
            log.warning("Empty fingerprint for %s", path.name)
            return None
        return fp
    except subprocess.TimeoutExpired:
        log.error("fpcalc timed out for %s", path.name)
        return None
    except FileNotFoundError:
        log.error("fpcalc not found at %s — install with: brew install chromaprint", _FPCALC)
        return None
    except Exception as e:
        log.error("Unexpected error fingerprinting %s: %s", path.name, e)
        return None


# ─── Fingerprint cache ───────────────────────────────────────────────────────

_FP_CACHE_PATH = Path.home() / "rekordbox-toolkit" / "fingerprint_cache.json"


def _load_fp_cache() -> dict[str, dict]:
    """
    Load the persistent fingerprint cache from disk.
    Returns a dict keyed by absolute file path string.
    Each entry: {"path", "fingerprint", "mtime", "size"}.
    Returns {} if the file is absent or unreadable.
    """
    if not _FP_CACHE_PATH.exists():
        return {}
    try:
        with open(_FP_CACHE_PATH, encoding="utf-8") as f:
            entries = json.load(f)
        return {
            e["path"]: e
            for e in entries
            if "path" in e and "fingerprint" in e
        }
    except Exception as exc:
        log.warning("Could not load fingerprint cache: %s", exc)
        return {}


def _save_fp_cache(cache: dict[str, dict]) -> None:
    """Write the fingerprint cache to disk, merging with any existing entries."""
    _FP_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        # Load what's on disk first so we don't overwrite entries from a
        # concurrent or previous run that aren't in the current cache dict.
        existing: dict[str, dict] = {}
        if _FP_CACHE_PATH.exists():
            try:
                with open(_FP_CACHE_PATH, encoding="utf-8") as f:
                    for e in json.load(f):
                        if "path" in e:
                            existing[e["path"]] = e
            except Exception:
                pass
        existing.update(cache)  # new entries win
        with open(_FP_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(list(existing.values()), f)
        log.info(
            "Fingerprint cache saved: %d total entries (%d new this run)",
            len(existing), len(cache),
        )
    except Exception as exc:
        log.warning("Could not save fingerprint cache: %s", exc)


def _fp_cache_valid(entry: dict, path: Path) -> bool:
    """
    Return True if the cache entry is still valid for this path.
    Checks file mtime + size (file unchanged) and fp_offset + fp_length
    (analysis window unchanged). Any mismatch forces a fresh fpcalc run.
    """
    if not entry:
        return False
    if entry.get("fp_offset") != _FP_OFFSET_SEC:
        return False
    if entry.get("fp_length") != _FP_LENGTH_SEC:
        return False
    try:
        stat = path.stat()
        return (
            stat.st_mtime == entry.get("mtime")
            and stat.st_size == entry.get("size")
        )
    except OSError:
        return False


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
    root: Path,
    *,
    max_workers: int = 1,
    pause_seconds: float = 0.0,
) -> list[DuplicateGroup]:
    """
    Fingerprint all audio files under root and return groups of duplicates.

    CPU-intensive. For 50k files expect 10–30 minutes depending on hardware.
    Run as an explicit command, not as part of the import workflow.

    Parameters
    ----------
    root : Path
        Directory to scan recursively.
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
    if not root.is_dir():
        raise ValueError(f"scan_duplicates: {root} is not a directory")

    log.info("Walking %s for audio files...", root)
    all_files = _walk_audio_files(root)
    log.info("Found %d audio files total", len(all_files))

    # Load fingerprint cache early so we can report hits at prefilter time
    fp_cache = _load_fp_cache()
    log.info("Fingerprint cache: %d entries loaded", len(fp_cache))

    # Build pre-filter index — DB covers all imported tracks; scan_index fills
    # in files not yet imported and provides measured audio durations.
    db_index   = _load_db_index()
    scan_index = _load_scan_index()
    index      = _merge_indices(db_index, scan_index)

    if index:
        files = _candidate_pairs(all_files, index)
    else:
        files = all_files
        log.info("No index available — fingerprinting all files (run Audit + Tag Tracks first to speed this up)")

    total = len(files)

    # Count how many candidates already have a valid cached fingerprint
    cache_hits_pre = sum(1 for p in files if _fp_cache_valid(fp_cache.get(str(p), {}), p))
    cache_misses   = total - cache_hits_pre

    print(
        "SUPERBOX_PREFILTER: "
        + json.dumps({
            "total":        len(all_files),
            "candidates":   total,
            "skipped":      len(all_files) - total,
            "db_tracks":    len(db_index),
            "scan_tracks":  len(scan_index),
            "cached":       cache_hits_pre,
            "to_compute":   cache_misses,
        }),
        flush=True,
    )

    log.info(
        "Beginning fingerprint pass on %d files "
        "(workers=%d pause=%.1fs cached=%d to_compute=%d)",
        total, max_workers, pause_seconds, cache_hits_pre, cache_misses,
    )

    fp_map: dict[str, list[Path]] = {}
    failed    = 0
    completed = 0
    hits      = 0
    # Collect new cache entries — list.append is GIL-safe for concurrent use
    new_cache_entries: list[dict] = []

    def _fingerprint_one(path: Path) -> tuple[Path, str | None, bool]:
        """Return (path, fingerprint, was_cache_hit). Never raises."""
        entry = fp_cache.get(str(path))
        if entry and _fp_cache_valid(entry, path):
            return path, entry["fingerprint"], True
        fp = fingerprint_file(path)
        if fp is not None:
            try:
                stat = path.stat()
                new_cache_entries.append({
                    "path":        str(path),
                    "fingerprint": fp,
                    "mtime":       stat.st_mtime,
                    "size":        stat.st_size,
                    "fp_offset":   _FP_OFFSET_SEC,
                    "fp_length":   _FP_LENGTH_SEC,
                })
            except OSError:
                pass
        return path, fp, False

    if max_workers > 1:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = {ex.submit(_fingerprint_one, p): p for p in files}
            for future in concurrent.futures.as_completed(futures):
                completed += 1
                try:
                    path, fp, was_hit = future.result()
                except Exception as exc:
                    log.error("Unexpected fingerprint error: %s", exc)
                    failed += 1
                    continue
                if fp is None:
                    failed += 1
                else:
                    fp_map.setdefault(fp, []).append(path)
                    if was_hit:
                        hits += 1
                if completed % _LOG_EVERY == 0:
                    log.info(
                        "Fingerprinting: %d / %d  (cache hits: %d  failures: %d)",
                        completed, total, hits, failed,
                    )
    else:
        for i, path in enumerate(files):
            if i > 0 and i % _LOG_EVERY == 0:
                log.info(
                    "Fingerprinting: %d / %d  (cache hits: %d  failures so far: %d)",
                    i, total, hits, failed,
                )
            path, fp, was_hit = _fingerprint_one(path)
            if fp is None:
                failed += 1
            else:
                fp_map.setdefault(fp, []).append(path)
                if was_hit:
                    hits += 1
            if pause_seconds > 0 and i < total - 1:
                time.sleep(pause_seconds)

    # Persist new fingerprints to cache
    new_entries_map = {e["path"]: e for e in new_cache_entries}
    if new_entries_map:
        _save_fp_cache(new_entries_map)

    log.info(
        "Fingerprint pass complete — %d cache hits, %d computed, %d failures, %d unique prints",
        hits, len(new_entries_map), failed, len(fp_map),
    )

    groups: list[DuplicateGroup] = []
    fp_rejected   = 0
    ver_split_cnt = 0

    for fp, paths in fp_map.items():
        if len(paths) < 2:
            continue

        # Filename gate — reject groups where no pair has similar names.
        # Filters false positives caused by shared sample loops / breakdowns
        # that produce identical fingerprints for genuinely different tracks.
        has_similar_pair = any(
            _filename_similarity(paths[i], paths[j]) >= _FILENAME_SIMILARITY_MIN
            for i in range(len(paths))
            for j in range(i + 1, len(paths))
        )
        if not has_similar_pair:
            fp_rejected += 1
            log.debug(
                "Rejected fingerprint group (name mismatch): %s",
                [p.name for p in paths],
            )
            continue

        # Version-tag split — a fingerprint match can contain different
        # *releases* of the same recording (e.g. "Club Mix" vs "Dub Mix"
        # or "[Artist] Dub" vs another artist's dub). These share audio but
        # are distinct products and should NOT be marked as duplicates.
        # Split the group by version tag; files with the same tag (or both
        # with no tag) remain as duplicates. Each sub-group is ranked and
        # reported independently.
        sub_groups = _split_group_by_version(paths)
        if len(sub_groups) != 1 or len(sub_groups[0]) != len(paths):
            ver_split_cnt += 1
            log.debug(
                "Version split (%d file(s) → %d sub-group(s)): %s",
                len(paths),
                len(sub_groups),
                [p.name for p in paths],
            )

        for sub_paths in sub_groups:
            ranked = sorted(sub_paths, key=_rank_file)
            keep   = ranked[0]
            remove = ranked[1:]
            ranks  = {str(p): _RANK_LABELS[_rank_file(p)] for p in sub_paths}
            groups.append(DuplicateGroup(
                fingerprint=fp,
                files=sub_paths,
                recommended_keep=keep,
                recommended_remove=remove,
                ranks=ranks,
            ))

    groups.sort(key=lambda g: len(g.files), reverse=True)

    log.info(
        "Duplicate scan complete — %d groups, %d duplicate files "
        "(%d fingerprint groups rejected by name gate, %d split by version tag)",
        len(groups),
        sum(len(g.recommended_remove) for g in groups),
        fp_rejected,
        ver_split_cnt,
    )
    return groups


# ─── CSV report ───────────────────────────────────────────────────────────────

def write_csv_report(
    groups: list[DuplicateGroup],
    output_path: Path,
) -> None:
    """
    Write duplicate groups to a CSV file for human review.

    Columns:
        group_id        — integer, same for all files in a group
        action          — "KEEP" or "REVIEW_REMOVE"
        rank            — PN / MIK / RAW
        file_path       — absolute path
        file_size_mb    — file size in MB (2 decimal places)
        bpm             — from TBPM tag, or blank
        key             — from TKEY tag, or blank
        filename        — basename only (for quick scanning)

    BPM and key are read live from file tags, not from the database. This
    means the report is valid before import and reflects raw tag values.

    Parameters
    ----------
    groups : list[DuplicateGroup]
        Output of scan_duplicates().
    output_path : Path
        Where to write the CSV. Parent directory is created if absent.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)

    rows_written = 0
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "group_id", "action", "rank", "file_path",
            "file_size_mb", "bpm", "key", "filename",
        ])
        writer.writeheader()

        for group_id, group in enumerate(groups, start=1):
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
                })
                rows_written += 1

    log.info(
        "CSV report written: %s (%d rows, %d groups)",
        output_path, rows_written, len(groups),
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
        groups = scan_duplicates(test_root)
        print(f"  Duplicate groups found: {len(groups)}")
        for g in groups[:3]:
            print(f"\n  Group ({len(g.files)} files):")
            for p in g.files:
                action = "KEEP  " if p == g.recommended_keep else "REMOVE"
                print(f"    [{action}] [{g.ranks[str(p)]}] {p.name}")

        if groups:
            out = Path.home() / "rekordbox-toolkit" / "duplicate_report_test.csv"
            write_csv_report(groups, out)
            print(f"\n  CSV written to: {out}")
    else:
        print(f"  SKIP: {test_root} not found")

    print("\nSmoke test complete — no files modified.")
