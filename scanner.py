"""
rekordbox-toolkit / scanner.py

Filesystem walker and metadata extractor.
No database interaction. Returns structured TrackInfo dataclasses.

Flow:
  scan_directory(root) -> Iterator[TrackInfo]

Each TrackInfo contains everything the importer needs to write a
DjmdContent row: path, title, artist, album, genre, BPM, key,
duration, bitrate, sample rate, file size, track number, year.

Missing tag fields are None — the importer decides what to do with them.
BPM is returned as a float (e.g. 126.0) — the importer applies ×100.
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from mutagen import File as MutagenFile
from mutagen.id3 import ID3NoHeaderError

from config import AUDIO_EXTENSIONS, BPM_MAX, BPM_MIN, SKIP_DIRS, SKIP_PREFIXES

log = logging.getLogger(__name__)


# ─── Data structure ───────────────────────────────────────────────────────────

@dataclass
class TrackInfo:
    """Metadata extracted from a single audio file."""
    path: Path

    # Core tags
    title: str | None = None
    artist: str | None = None
    album: str | None = None
    genre: str | None = None
    year: int | None = None
    track_number: int | None = None

    # DJ-relevant
    bpm: float | None = None        # Raw float — NOT yet ×100
    key: str | None = None          # Raw string from tag (Camelot, Open Key, or standard)

    # Audio properties
    duration_seconds: float | None = None
    bitrate: int | None = None      # bits per second
    sample_rate: int | None = None  # Hz
    bit_depth: int | None = None    # 16, 24, 32 — not always available
    file_size: int | None = None    # bytes
    file_type: str | None = None    # "MP3", "AIFF", "WAV", "FLAC", etc.

    # Derived
    errors: list[str] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        """True if the file has at minimum a readable path and duration."""
        return self.duration_seconds is not None and self.duration_seconds > 0


# ─── Tag extraction helpers ───────────────────────────────────────────────────

def _get_id3_text(tags, frame_id: str) -> str | None:
    """
    Pull a text value from an ID3 tag frame.
    Returns the first string value or None if absent/empty.
    Works for both MP3 (ID3) and AIFF (which also uses ID3 tags).
    """
    frame = tags.get(frame_id)
    if frame is None:
        return None
    # ID3 frames expose their value via .text (a list of strings)
    text = getattr(frame, "text", None)
    if text and str(text[0]).strip():
        return str(text[0]).strip()
    # Fallback: str() of the frame itself
    val = str(frame).strip()
    return val if val else None


def _get_vorbis_text(tags, *keys: str) -> str | None:
    """
    Pull a text value from Vorbis comment tags (FLAC, OGG, Opus).
    Tries each key in order, returns first non-empty match.
    Vorbis comment values are lists of strings — we take the first element.
    Keys are case-insensitive in the spec but mutagen lowercases them.
    """
    for key in keys:
        val = tags.get(key.lower())
        if val:
            text = str(val[0]).strip() if isinstance(val, list) else str(val).strip()
            if text:
                return text
    return None


def _parse_bpm(raw: str | None) -> float | None:
    """
    Parse a BPM string to float.
    Handles '126', '126.00', '126.5', and garbage gracefully.
    """
    if raw is None:
        return None
    try:
        val = float(raw.strip())
        # Sanity check: BPM outside range is almost certainly corrupt
        if BPM_MIN <= val <= BPM_MAX:
            return val
        log.debug("BPM value %s outside expected range (%s–%s) — discarding", val, BPM_MIN, BPM_MAX)
        return None
    except (ValueError, AttributeError):
        return None


def _parse_year(raw: str | None) -> int | None:
    """Parse year from TDRC or TYER tag (may be 'YYYY', 'YYYY-MM-DD', etc.)."""
    if raw is None:
        return None
    try:
        return int(str(raw).strip()[:4])
    except (ValueError, TypeError):
        return None


def _parse_track_number(raw: str | None) -> int | None:
    """Parse track number from TRCK tag (may be '2' or '2/12')."""
    if raw is None:
        return None
    try:
        return int(str(raw).split("/")[0].strip())
    except (ValueError, TypeError):
        return None


# ─── Core extraction ─────────────────────────────────────────────────────────

def extract_metadata(path: Path) -> TrackInfo:
    """
    Extract all available metadata from a single audio file.
    Never raises — errors are captured in TrackInfo.errors.
    """
    info = TrackInfo(path=path)

    try:
        info.file_size = path.stat().st_size
        info.file_type = path.suffix.lstrip(".").upper()
    except OSError as e:
        info.errors.append(f"stat failed: {e}")
        return info

    try:
        audio = MutagenFile(path, easy=False)
    except Exception as e:
        info.errors.append(f"mutagen open failed: {e}")
        return info

    if audio is None:
        info.errors.append("mutagen returned None (unrecognized format)")
        return info

    # ── Audio properties (from info object, always present if file opened) ──
    try:
        ai = audio.info
        info.duration_seconds = getattr(ai, "length", None)
        info.bitrate = getattr(ai, "bitrate", None)
        info.sample_rate = getattr(ai, "sample_rate", None)
        info.bit_depth = getattr(ai, "bits_per_sample", None)  # AIFF/WAV/FLAC
    except Exception as e:
        info.errors.append(f"audio info read failed: {e}")

    # ── Tags ────────────────────────────────────────────────────────────────
    tags = audio.tags
    if tags is None:
        info.errors.append("no tags found")
        return info

    # Detect tag format by type name — Vorbis comments have a different
    # key/value structure than ID3 frames.
    tag_type = type(tags).__name__
    is_vorbis = "VCFLACDict" in tag_type or "VComment" in tag_type or "OggVorbis" in tag_type

    try:
        if is_vorbis:
            info.title  = _get_vorbis_text(tags, "title")
            info.artist = _get_vorbis_text(tags, "artist")
            info.album  = _get_vorbis_text(tags, "album")
            info.genre  = _get_vorbis_text(tags, "genre")
            info.key    = _get_vorbis_text(tags, "initialkey", "key")
            info.bpm    = _parse_bpm(_get_vorbis_text(tags, "bpm"))
            info.year   = _parse_year(_get_vorbis_text(tags, "date", "year"))
            info.track_number = _parse_track_number(
                _get_vorbis_text(tags, "tracknumber")
            )
        else:
            info.title  = _get_id3_text(tags, "TIT2")
            info.artist = _get_id3_text(tags, "TPE1")
            info.album  = _get_id3_text(tags, "TALB")
            info.genre  = _get_id3_text(tags, "TCON")
            info.key    = _get_id3_text(tags, "TKEY")
            info.bpm    = _parse_bpm(_get_id3_text(tags, "TBPM"))
            info.year   = _parse_year(
                _get_id3_text(tags, "TDRC") or _get_id3_text(tags, "TYER")
            )
            info.track_number = _parse_track_number(_get_id3_text(tags, "TRCK"))
    except Exception as e:
        info.errors.append(f"tag extraction failed: {e}")

    return info


# ─── Filesystem walker ────────────────────────────────────────────────────────

def _should_skip_file(path: Path) -> bool:
    """Return True if the file should be excluded from scanning."""
    name = path.name
    if any(name.startswith(p) for p in SKIP_PREFIXES):
        return True
    if path.suffix.lower() not in AUDIO_EXTENSIONS:
        return True
    return False


def _should_skip_dir(path: Path) -> bool:
    """Return True if this directory should not be descended into."""
    return path.name in SKIP_DIRS or path.name.startswith(".")


def scan_directory(
    root: Path,
    *,
    skip_errors: bool = True,
) -> Iterator[TrackInfo]:
    """
    Recursively walk root and yield TrackInfo for every audio file found.

    Parameters
    ----------
    root : Path
        Directory to scan. Must exist.
    skip_errors : bool
        If True (default), files with read errors are yielded with
        TrackInfo.errors populated. If False, they are silently skipped.

    Yields
    ------
    TrackInfo
        One per audio file encountered.
    """
    if not root.is_dir():
        raise ValueError(f"scan_directory: {root} is not a directory")

    for dirpath, dirnames, filenames in root.walk():
        # Prune skip dirs in-place so os.walk doesn't descend into them
        dirnames[:] = [
            d for d in dirnames
            if not _should_skip_dir(dirpath / d)
        ]

        for filename in filenames:
            file_path = dirpath / filename

            if _should_skip_file(file_path):
                continue

            track = extract_metadata(file_path)

            if track.errors and not skip_errors:
                log.debug("Skipping %s — errors: %s", file_path, track.errors)
                continue

            if track.errors:
                log.warning("Errors reading %s: %s", file_path.name, track.errors)

            yield track


# ─── Smoke test ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

    # Scan a small artist folder so the test is fast but real
    test_dirs = [
        Path("/Volumes/DJMT/DJMT PRIMARY/Kerri Chandler"),
        Path("/Volumes/DJMT/DJMT PRIMARY/Blaze"),
        Path("/Volumes/DJMT/DJMT PRIMARY/Moodymann"),
    ]

    total = valid = missing_bpm = missing_key = errored = 0

    for test_root in test_dirs:
        if not test_root.exists():
            print(f"SKIP (not found): {test_root}")
            continue
        print(f"\n=== {test_root.name} ===")
        for track in scan_directory(test_root):
            total += 1
            if track.is_valid:
                valid += 1
            if track.bpm is None:
                missing_bpm += 1
            if track.key is None:
                missing_key += 1
            if track.errors:
                errored += 1
            print(
                f"  [{track.file_type:4}] "
                f"{track.artist or '?':25} | "
                f"{(track.title or track.path.name)[:40]:40} | "
                f"BPM:{str(track.bpm or '?'):>6} | "
                f"KEY:{track.key or '?':>4} | "
                f"{track.duration_seconds:.0f}s"
                if track.duration_seconds is not None else f"  ERROR: {track.path.name}"
            )

    print(f"\n{'─'*60}")
    print(f"Total files : {total}")
    print(f"Valid       : {valid}")
    print(f"Missing BPM : {missing_bpm}")
    print(f"Missing KEY : {missing_key}")
    print(f"With errors : {errored}")
