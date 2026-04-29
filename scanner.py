"""
fablegear / scanner.py

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
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from mutagen import File as MutagenFile
from mutagen.id3 import ID3, ID3NoHeaderError

from config import AUDIO_EXTENSIONS, BPM_MAX, BPM_MIN, MIN_FILE_BYTES, SKIP_DIRS, SKIP_PREFIXES

# Formats where having no tags at all is normal and shouldn't be a WARNING.
# WAV and AIFF files frequently have no ID3/metadata and are still valid audio.
_TAG_OPTIONAL_EXTS = {".wav", ".aif", ".aiff", ".aifc"}

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


# ─── Filename-based fallback ─────────────────────────────────────────────────

def _parse_filename_metadata(path: Path, info: "TrackInfo") -> None:
    """
    Last-resort metadata extraction from the filename when tags are absent.
    Handles "Artist - Title.mp3" convention and strips Pioneer _PN suffixes.
    """
    stem = path.stem
    # Strip Pioneer duplicate markers: _PN, _PN2, _PN 3, etc.
    stem = re.sub(r'_PN\s*\d*$', '', stem, flags=re.IGNORECASE).strip()
    # Strip leading track number: "02 ", "02. ", "02 - "
    stem = re.sub(r'^\d+[\s.\-]+', '', stem).strip()
    if ' - ' in stem:
        artist_part, title_part = stem.split(' - ', 1)
        if artist_part.strip():
            info.artist = artist_part.strip()
        if title_part.strip():
            info.title = title_part.strip()
    else:
        info.title = info.title or stem.strip() or None


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


def _get_mp4_text(tags, key: str) -> str | None:
    """
    Pull a text value from MP4/M4A atom tags.
    MP4 atom values are typically lists; we take the first element.
    """
    val = tags.get(key)
    if val:
        if isinstance(val, list) and val:
            return str(val[0]).strip() or None
        return str(val).strip() or None
    return None


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
        if path.suffix.lower() == '.mp3':
            # MPEG frame sync can fail while ID3 tags are still intact at the
            # start of the file. Try reading just the ID3 block.
            try:
                _id3 = ID3(path)
                class _ID3Only:
                    tags = _id3
                    info = None
                audio = _ID3Only()
                info.errors.append(f"MPEG sync warning (ID3 tags recovered): {e}")
            except Exception:
                info.errors.append(f"mutagen open failed: {e}")
                _parse_filename_metadata(path, info)
                return info
        else:
            info.errors.append(f"mutagen open failed: {e}")
            _parse_filename_metadata(path, info)
            return info

    if audio is None:
        info.errors.append("mutagen returned None (unrecognized format)")
        _parse_filename_metadata(path, info)
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
        _parse_filename_metadata(path, info)
        return info

    # Detect tag format by type name — Vorbis comments and MP4 atoms have a
    # different key/value structure than ID3 frames.
    tag_type = type(tags).__name__
    is_vorbis = "VCFLACDict" in tag_type or "VComment" in tag_type or "OggVorbis" in tag_type
    is_mp4    = "MP4Tags" in tag_type or "MP4" in tag_type

    try:
        if is_mp4:
            info.title  = _get_mp4_text(tags, "©nam")
            info.artist = _get_mp4_text(tags, "©ART")
            info.album  = _get_mp4_text(tags, "©alb")
            info.genre  = _get_mp4_text(tags, "©gen")
            info.year   = _parse_year(_get_mp4_text(tags, "©day"))
            # Track number in M4A is stored as (track, total) tuple
            trkn = tags.get("trkn")
            if trkn:
                try:
                    info.track_number = int(trkn[0][0])
                except (TypeError, IndexError, ValueError):
                    pass
            # BPM stored as integer list in tmpo atom
            tmpo = tags.get("tmpo")
            if tmpo:
                try:
                    bpm_val = float(tmpo[0])
                    if BPM_MIN <= bpm_val <= BPM_MAX:
                        info.bpm = bpm_val
                except (TypeError, ValueError, IndexError):
                    pass
            # Key may be in a custom iTunes freeform atom
            key_atom = tags.get("----:com.apple.iTunes:initialkey")
            if key_atom:
                try:
                    raw = key_atom[0]
                    info.key = (raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)).strip() or None
                except Exception:
                    pass
        elif is_vorbis:
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
    try:
        if path.stat().st_size < MIN_FILE_BYTES:
            log.debug("Skipping %s — file too small (%d bytes)", name, path.stat().st_size)
            return True
    except OSError:
        pass
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
                # "no tags found" on WAV/AIFF is normal — log at DEBUG, not WARNING.
                no_tag_only = track.errors == ["no tags found"]
                if no_tag_only and file_path.suffix.lower() in _TAG_OPTIONAL_EXTS:
                    log.debug("No tags in %s (expected for %s)", file_path.name, file_path.suffix)
                else:
                    log.warning("Errors reading %s: %s", file_path.name, track.errors)

            yield track

