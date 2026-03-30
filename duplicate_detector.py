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
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import acoustid
from mutagen import File as MutagenFile

from config import AUDIO_EXTENSIONS, MUSIC_ROOT, SKIP_DIRS, SKIP_PREFIXES

log = logging.getLogger(__name__)

_LOG_EVERY: int = 100

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

def fingerprint_file(path: Path) -> str | None:
    """
    Compute the Chromaprint acoustic fingerprint for an audio file.
    Uses fpcalc via pyacoustid. Returns the fingerprint as a str, or None
    on failure.

    pyacoustid may return the fingerprint as bytes in some versions — this
    function always decodes to str so fp_map keys are consistent.

    fpcalc default: analyses first 120 seconds of audio — sufficient for DJ use.
    """
    try:
        duration, fingerprint = acoustid.fingerprint_file(str(path))
        if not fingerprint:
            log.warning("Empty fingerprint for %s", path.name)
            return None
        # Normalise to str — some pyacoustid versions return bytes
        if isinstance(fingerprint, bytes):
            fingerprint = fingerprint.decode("utf-8", errors="replace")
        return fingerprint
    except acoustid.FingerprintGenerationError as e:
        log.error("Fingerprint failed for %s: %s", path.name, e)
        return None
    except Exception as e:
        log.error("Unexpected error fingerprinting %s: %s", path.name, e)
        return None


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
    files = _walk_audio_files(root)
    total = len(files)
    log.info(
        "Found %d audio files — beginning fingerprint pass "
        "(workers=%d pause=%.1fs)",
        total, max_workers, pause_seconds,
    )

    fp_map: dict[str, list[Path]] = {}
    failed = 0
    completed = 0

    def _fingerprint_one(path: Path) -> tuple[Path, str | None]:
        return path, fingerprint_file(path)

    if max_workers > 1:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = {ex.submit(_fingerprint_one, p): p for p in files}
            for future in concurrent.futures.as_completed(futures):
                completed += 1
                try:
                    path, fp = future.result()
                except Exception as exc:
                    log.error("Unexpected fingerprint error: %s", exc)
                    failed += 1
                    continue
                if fp is None:
                    failed += 1
                else:
                    fp_map.setdefault(fp, []).append(path)
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
            fp = fingerprint_file(path)
            if fp is None:
                failed += 1
            else:
                fp_map.setdefault(fp, []).append(path)
            if pause_seconds > 0 and i < total - 1:
                time.sleep(pause_seconds)

    log.info(
        "Fingerprint pass complete — %d unique prints, %d failures",
        len(fp_map), failed,
    )

    groups: list[DuplicateGroup] = []

    for fp, paths in fp_map.items():
        if len(paths) < 2:
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
        ))

    groups.sort(key=lambda g: len(g.files), reverse=True)

    log.info(
        "Duplicate scan complete — %d groups, %d duplicate files",
        len(groups),
        sum(len(g.recommended_remove) for g in groups),
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
