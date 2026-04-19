"""
rekordbox-toolkit / audio_processor.py

Analyses and normalises audio files in-place. No database interaction.

Operations per file (each independently skippable):
1. BPM detection via librosa beat tracking, written to TBPM tag
2. Key detection via librosa chroma + Krumhansl-Schmuckler, written to TKEY (Camelot)
3. Loudness check via pyloudnorm (EBU R128 measurement)
4. Normalisation via ffmpeg volume filter if outside tolerance, in-place replacement

Design rules:
- Existing tags are NEVER overwritten unless force=True is passed
- Original files are never deleted until the replacement is verified
- All failures are logged and returned in ProcessResult; nothing crashes the batch
- MP3s are re-encoded at 320kbps CBR if normalisation is applied
- AIFFs are re-encoded losslessly (pcm_s16le or pcm_s24le, matching source bit depth)

Target loudness: -8.0 LUFS (DJ standard)
Tolerance: 0.5 LUFS (skip normalisation if within this window)
"""


import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

import librosa
import numpy as np
import soundfile as sf
from mutagen import File as MutagenFile
from mutagen.id3 import TBPM, TKEY

from config import AUDIO_EXTENSIONS, BPM_MAX, BPM_MIN, LUFS_TOLERANCE, TARGET_LUFS

log = logging.getLogger(__name__)

# Resolve ffmpeg once at import time — on macOS with Homebrew the server process
# may not inherit the shell PATH, so we fall back to common install locations.
def _find_ffmpeg() -> str:
    found = shutil.which("ffmpeg")
    if found:
        return found
    for candidate in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"):
        if Path(candidate).exists():
            return candidate
    return "ffmpeg"  # last resort — will surface a clear FileNotFoundError if absent

_FFMPEG = _find_ffmpeg()

ANALYSIS_DURATION: float = 90.0
LIBROSA_TO_CAMELOT: dict[str, str] = {
    "Amin": "8A", "Emin": "9A", "Bmin": "10A", "F#min": "11A", "C#min": "12A",
    "G#min": "1A", "D#min": "2A", "A#min": "3A", "Fmin": "4A", "Cmin": "5A",
    "Gmin": "6A", "Dmin": "7A",
    "Cmaj": "8B", "Gmaj": "9B", "Dmaj": "10B", "Amaj": "11B", "Emaj": "12B",
    "Bmaj": "1B", "F#maj": "2B", "C#maj": "3B", "G#maj": "4B", "D#maj": "5B",
    "A#maj": "6B", "Fmaj": "7B",
}

KS_MAJOR = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
KS_MINOR = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])
NOTES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

# ─── Result dataclass ─────────────────────────────────────────────────────────

@dataclass
class ProcessResult:
    path: Path
    bpm_detected: float | None = None
    bpm_written: bool = False
    key_detected: str | None = None
    key_written: bool = False
    loudness_before: float | None = None
    loudness_after: float | None = None
    normalised: bool = False
    skipped_bpm: bool = False
    skipped_key: bool = False
    skipped_loudness: bool = False
    enrich_written: bool = False
    mb_recording_id: str | None = None
    errors: list[str] = field(default_factory=list)
    quarantined: bool = False        # True if file was moved to the quarantine folder
    quarantine_dest: Path | None = None  # Where it was moved to

    @property
    def ok(self) -> bool:
        return len(self.errors) == 0


# Error substrings that indicate a file is unreadable/corrupt at the binary level.
# These are distinct from soft failures (tag write failed, no tags found, etc.)
# that don't mean the audio data is broken.
_CORRUPT_ERRORS: tuple[str, ...] = (
    "mutagen could not open file",
    "mutagen open failed",
    "Header size < 8",
    "No 'fmt' chunk found",
    "can't sync to MPEG frame",
    "unrecognized format",
    "could not read tags",
)


def is_corrupt(result: ProcessResult) -> bool:
    """Return True if this result represents a file that cannot be read at all."""
    return any(
        any(sig in err for sig in _CORRUPT_ERRORS)
        for err in result.errors
    )


def quarantine_file(result: ProcessResult, quarantine_dir: Path) -> bool:
    """
    Move result.path into quarantine_dir, preserving the filename.
    If a file with the same name already exists there, append a counter suffix.

    Returns True if the move succeeded; updates result.quarantined and
    result.quarantine_dest in place.
    """
    src = result.path
    if not src.exists():
        return False

    quarantine_dir.mkdir(parents=True, exist_ok=True)

    dest = quarantine_dir / src.name
    # Avoid silently overwriting a different file with the same name
    if dest.exists():
        stem, suffix = src.stem, src.suffix
        for n in range(1, 10_000):
            candidate = quarantine_dir / f"{stem}_{n}{suffix}"
            if not candidate.exists():
                dest = candidate
                break

    try:
        src.rename(dest)
        result.quarantined = True
        result.quarantine_dest = dest
        log.info("Quarantined %s → %s", src.name, dest)
        return True
    except OSError as exc:
        log.warning("Could not quarantine %s: %s", src.name, exc)
        return False


# ─── BPM detection ────────────────────────────────────────────────────────────

_ANALYSIS_SR: int = 22050  # sample rate used for BPM/key analysis


def _load_audio_ffmpeg(path: Path, duration: float = ANALYSIS_DURATION) -> "tuple[np.ndarray, int] | None":
    """
    Decode audio to mono float32 PCM via ffmpeg subprocess.

    Bypasses audioread / macOS Core Audio entirely — librosa.load() falls back
    to audioread for MP3s which can segfault via AudioToolbox on certain files.
    ffmpeg runs isolated; any crash or format error surfaces as a return of None.
    """
    cmd = [
        _FFMPEG, "-hide_banner", "-y",
        "-t", str(duration), "-i", str(path),
        "-ac", "1", "-ar", str(_ANALYSIS_SR), "-f", "f32le", "-",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=120)
        if result.returncode != 0:
            log.debug("ffmpeg decode failed for %s: %s", path.name, result.stderr[-200:])
            return None
        y = np.frombuffer(result.stdout, dtype=np.float32).copy()
        return (y, _ANALYSIS_SR) if y.size > 0 else None
    except Exception as exc:
        log.debug("ffmpeg audio decode error for %s: %s", path.name, exc)
        return None


def _detect_bpm(y: np.ndarray, sr: int, name: str) -> float | None:
    try:
        tempo, _ = librosa.beat.beat_track(y=y, sr=sr)
        bpm = float(np.squeeze(tempo))
        if BPM_MIN <= bpm <= BPM_MAX:
            return round(bpm, 2)
        log.warning("BPM %s out of range (%s–%s) for %s", bpm, BPM_MIN, BPM_MAX, name)
        return None
    except Exception as e:
        log.error("BPM detection failed for %s: %s", name, e)
        return None


# ─── Key detection ────────────────────────────────────────────────────────────

def _detect_key(y: np.ndarray, sr: int, name: str) -> str | None:
    try:
        chroma = librosa.feature.chroma_cqt(y=y, sr=sr).mean(axis=1)
        scores: dict[str, float] = {}
        for i, note in enumerate(NOTES):
            rolled = np.roll(chroma, -i)
            scores[note + "maj"] = float(np.corrcoef(rolled, KS_MAJOR)[0, 1])
            scores[note + "min"] = float(np.corrcoef(rolled, KS_MINOR)[0, 1])
        best = max(scores, key=scores.get)  # type: ignore[arg-type]
        camelot = LIBROSA_TO_CAMELOT.get(best)
        if camelot is None:
            log.warning("No Camelot mapping for detected key %r", best)
            return None
        log.debug("Key detected: %s → %s  (score %.3f)", best, camelot, scores[best])
        return camelot
    except Exception as e:
        log.error("Key detection failed for %s: %s", name, e)
        return None


# ─── Loudness measurement ─────────────────────────────────────────────────────

def _measure_lufs(path: Path) -> float | None:
    """
    Measure integrated loudness via ffmpeg's loudnorm filter (EBU R128).
    Uses a subprocess so memory use is bounded regardless of file size, and
    avoids the scipy circular-import problem on Python 3.12+.
    """
    try:
        cmd = [
            _FFMPEG, "-hide_banner",
            "-i", str(path),
            "-af", "loudnorm=print_format=json",
            "-f", "null", "-",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        # loudnorm prints its JSON summary to stderr after the last '{'
        idx = result.stderr.rfind("{")
        if idx == -1:
            log.warning("No loudnorm JSON in ffmpeg output for %s", path.name)
            return None
        end = result.stderr.rfind("}")
        if end == -1 or end < idx:
            return None
        data = json.loads(result.stderr[idx : end + 1])
        lufs = float(data["input_i"])
        if not np.isfinite(lufs):
            log.warning("Non-finite LUFS for %s (silent file?)", path.name)
            return None
        return round(lufs, 2)
    except Exception as e:
        log.error("Loudness measurement failed for %s: %s", path.name, e)
        return None


# ─── Normalisation ────────────────────────────────────────────────────────────

def _get_ffmpeg_codec_args(path: Path) -> list[str]:
    """Return ffmpeg codec args matching the file format and source bit depth."""
    ext = path.suffix.lower()
    if ext == ".mp3":
        return ["-codec:a", "libmp3lame", "-b:a", "320k"]
    elif ext in (".aiff", ".aif"):
        try:
            info = sf.info(str(path))
            codec = "pcm_s24le" if "24" in info.subtype else "pcm_s16le"
        except Exception:
            codec = "pcm_s16le"
        return ["-codec:a", codec]
    elif ext == ".wav":
        return ["-codec:a", "pcm_s16le"]
    elif ext == ".flac":
        return ["-codec:a", "flac", "-compression_level", "8"]
    else:
        return ["-codec:a", "copy"]


def _normalise_file(path: Path, gain_db: float) -> bool:
    """
    Apply gain_db to path using ffmpeg volume filter.
    Write → verify → move original to .bak → move temp to path → delete .bak.
    Restores from .bak if the final move fails. Logs CRITICAL if restore fails.
    """
    suffix = path.suffix
    tmp_fd, tmp_path_str = tempfile.mkstemp(suffix=suffix, dir=path.parent)
    tmp_path = Path(tmp_path_str)
    os.close(tmp_fd)

    bak = path.with_suffix(path.suffix + ".bak")
    original_moved = False

    try:
        codec_args = _get_ffmpeg_codec_args(path)
        cmd = [
            _FFMPEG, "-y", "-i", str(path),
            "-af", f"volume={gain_db:.4f}dB",
            *codec_args,
            "-map_metadata", "0",
            "-id3v2_version", "3",
            str(tmp_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if result.returncode != 0:
            log.error("ffmpeg failed for %s:\n%s", path.name, result.stderr[-500:])
            return False

        # Verify output. soundfile can't open MP3s, so use mutagen for those.
        try:
            if tmp_path.suffix.lower() == ".mp3":
                mf = MutagenFile(str(tmp_path))
                if mf is None or mf.info.length == 0:
                    raise ValueError("empty or unreadable MP3")
            else:
                verify_info = sf.info(str(tmp_path))
                if verify_info.frames == 0:
                    raise ValueError("zero frames in output")
        except Exception as verify_err:
            log.error("Could not verify ffmpeg output for %s: %s", path.name, verify_err)
            return False

        shutil.move(str(path), str(bak))
        original_moved = True
        shutil.move(str(tmp_path), str(path))
        bak.unlink()
        return True

    except Exception as e:
        log.error("Normalisation failed for %s: %s", path.name, e)
        if original_moved and not path.exists() and bak.exists():
            try:
                shutil.move(str(bak), str(path))
                log.warning("Restored original from .bak: %s", path.name)
            except Exception as restore_err:
                log.critical(
                    "RESTORE FAILED for %s — original is at %s: %s",
                    path.name, bak, restore_err,
                )
        return False
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


# ─── Format Conversion ────────────────────────────────────────────────────────────

def _convert_file(path: Path, target_format: str) -> tuple[bool, str]:
    """
    Convert path to target format (mp3, wav, aif, flac).
    Write → verify → move original to .bak → move new to path with target ext → delete .bak.
    Returns (success: bool, message: str).
    """
    target_format = target_format.lower().lstrip(".")
    if target_format not in ("mp3", "wav", "aif", "aiff", "flac"):
        return False, f"Unsupported format: {target_format}"

    # Normalize aif → aiff for consistency
    if target_format == "aif":
        target_format = "aiff"

    # Compute target extension. When converting *to* AIFF, normalise .aif → .aiff
    # so we always land on the canonical extension. For every other target format,
    # just use the format name as-is regardless of the input extension.
    if target_format == "aiff":
        target_ext = ".aiff"
    else:
        target_ext = f".{target_format}"

    # Normalise source extension for the skip check so .aif and .aiff both match
    src_ext = path.suffix.lower()
    if src_ext == ".aif":
        src_ext = ".aiff"

    # If already target format, skip
    if src_ext == target_ext.lower():
        return True, f"Already {target_format}"

    new_path = path.with_suffix(target_ext)
    if new_path.exists():
        return False, f"{new_path.name} already exists"

    tmp_fd, tmp_path_str = tempfile.mkstemp(suffix=target_ext, dir=path.parent)
    tmp_path = Path(tmp_path_str)
    os.close(tmp_fd)

    bak = path.with_suffix(path.suffix + ".bak")
    original_moved = False

    try:
        # Determine codec args for target format
        if target_format == "mp3":
            codec_args = ["-codec:a", "libmp3lame", "-b:a", "320k"]
        elif target_format == "aiff":
            try:
                info = sf.info(str(path))
                codec = "pcm_s24le" if "24" in info.subtype else "pcm_s16le"
            except Exception:
                codec = "pcm_s16le"
            codec_args = ["-codec:a", codec]
        elif target_format == "wav":
            codec_args = ["-codec:a", "pcm_s16le"]
        elif target_format == "flac":
            codec_args = ["-codec:a", "flac", "-compression_level", "8"]

        cmd = [
            _FFMPEG, "-y", "-i", str(path),
            *codec_args,
            "-map_metadata", "0",
            "-id3v2_version", "3",
            str(tmp_path),
        ]
        result = subprocess.run(cmd, capture_output=True, timeout=600)
        if result.returncode != 0:
            stderr_str = result.stderr.decode("utf-8", errors="replace")[-200:]
            return False, f"ffmpeg failed: {stderr_str}"

        # Verify output without loading into RAM
        try:
            verify_info = sf.info(str(tmp_path))
        except Exception as verify_err:
            return False, f"Could not verify ffmpeg output: {verify_err}"
        if verify_info.frames == 0:
            return False, "ffmpeg output is empty (zero frames)"

        # Move original to .bak, new to path
        shutil.move(str(path), str(bak))
        original_moved = True
        shutil.move(str(tmp_path), str(new_path))
        bak.unlink()
        return True, f"Converted to {target_format}"

    except Exception as e:
        if original_moved and not path.exists() and bak.exists():
            try:
                shutil.move(str(bak), str(path))
                log.warning("Restored original from .bak: %s", path.name)
                return False, f"Conversion failed (restored original): {e}"
            except Exception as restore_err:
                log.critical("RESTORE FAILED for %s — original is at %s: %s", path.name, bak, restore_err)
                return False, f"Conversion failed AND restore failed: {restore_err}"
        return False, f"Conversion failed: {e}"

    finally:
        if tmp_path.exists():
            tmp_path.unlink()


# ─── AcoustID enrichment ──────────────────────────────────────────────────────

def _enrich_from_acoustid(path: Path, *, force: bool = False) -> dict | None:
    """
    Fingerprint path with fpcalc (via pyacoustid) and query the AcoustID
    web service. Returns a dict with available metadata fields on success,
    or None if the API key is not configured, lookup fails, or score is low.

    Returned dict keys (all optional — only present when non-empty):
      recording_id, title, artist, album, year, genre

    Note: when enrich_tags=True is passed to process_directory(), expect
    ~1s additional time per file due to AcoustID rate limits (3 req/s).
    """
    try:
        from config import ACOUSTID_API_KEY   # noqa: PLC0415
    except ImportError:
        return None
    if not ACOUSTID_API_KEY:
        return None

    try:
        import acoustid  # noqa: PLC0415
        duration, fingerprint = acoustid.fingerprint_file(str(path))
        if not fingerprint:
            return None
        if isinstance(fingerprint, bytes):
            fingerprint = fingerprint.decode("utf-8", errors="replace")
    except Exception as e:
        log.debug("AcoustID fingerprint failed for %s: %s", path.name, e)
        return None

    try:
        import acoustid  # noqa: PLC0415
        response = acoustid.lookup(
            ACOUSTID_API_KEY, fingerprint, duration,
            meta=["recordings", "releasegroups", "compress"],
        )
        best_score = 0.0
        best_meta: dict = {}
        for score, rid, title, artist in acoustid.parse_lookup_result(response):
            if score > best_score:
                best_score = score
                best_meta = {
                    "recording_id": rid or "",
                    "title":        title or "",
                    "artist":       artist or "",
                }
        if best_score < 0.60 or not best_meta:
            log.debug("AcoustID: no confident match for %s (best=%.2f)", path.name, best_score)
            return None
        log.info("AcoustID match: %s → %s - %s (score=%.2f)",
                 path.name, best_meta.get("artist", "?"), best_meta.get("title", "?"), best_score)
        return best_meta
    except Exception as e:
        log.warning("AcoustID lookup failed for %s: %s", path.name, e)
        return None


def _write_enriched_tags(path: Path, meta: dict, *, force: bool = False) -> list[str]:
    """
    Write MusicBrainz metadata into file tags.
    Only writes fields that are:
      a) present and non-empty in meta, AND
      b) currently empty in the file (or force=True).
    Returns list of field names written.
    """
    from mutagen import File as MutagenFile  # noqa: PLC0415
    from mutagen.id3 import TIT2, TPE1, TALB  # noqa: PLC0415

    audio = MutagenFile(str(path), easy=False)
    if audio is None:
        return []
    if audio.tags is None:
        try:
            audio.add_tags()
        except Exception:
            return []

    tag_type = type(audio.tags).__name__
    is_vorbis = "VCFLACDict" in tag_type or "VComment" in tag_type
    is_mp4    = "MP4Tags" in tag_type or "MP4" in tag_type

    written = []

    def _field_empty(id3_key, vorbis_key, mp4_key=None) -> bool:
        try:
            if is_vorbis:
                v = audio.tags.get(vorbis_key.lower())
                return not (v and str(v[0] if isinstance(v, list) else v).strip())
            elif is_mp4 and mp4_key:
                v = audio.tags.get(mp4_key)
                return not (v and str(v[0] if isinstance(v, list) else v).strip())
            else:
                f = audio.tags.get(id3_key)
                return f is None or not str(f).strip()
        except Exception:
            return True

    def _write_field(label, value, id3_cls, id3_key, vorbis_key, mp4_key=None):
        if not value:
            return
        if not force and not _field_empty(id3_key, vorbis_key, mp4_key):
            return
        try:
            if is_vorbis:
                audio.tags[vorbis_key.lower()] = [value]
            elif is_mp4 and mp4_key:
                audio.tags[mp4_key] = [value]
            else:
                audio.tags.delall(id3_key)
                audio.tags[id3_key] = id3_cls(encoding=3, text=[value])
            written.append(label)
        except Exception as e:
            log.debug("Could not write %s tag to %s: %s", label, path.name, e)

    _write_field("title",  meta.get("title"),  TIT2, "TIT2", "title", "©nam")
    _write_field("artist", meta.get("artist"), TPE1, "TPE1", "artist", "©ART")
    _write_field("album",  meta.get("album"),  TALB, "TALB", "album", "©alb")

    if written:
        try:
            audio.save()
        except Exception as e:
            log.warning("Could not save enriched tags for %s: %s", path.name, e)
            return []

    return written


# ─── Tag writing ──────────────────────────────────────────────────────────────

def _write_tags(path: Path, bpm: float | None, key: str | None) -> None:
    """Write BPM and/or key to file tags via mutagen. Raises on failure."""
    audio = MutagenFile(str(path), easy=False)
    if audio is None:
        raise RuntimeError(f"mutagen could not open {path.name}")
    if audio.tags is None:
        try:
            audio.add_tags()
        except Exception as e:
            raise RuntimeError(f"Cannot create tag block for {path.name}: {e}")

    tag_type = type(audio.tags).__name__
    is_vorbis = "VCFLACDict" in tag_type or "VComment" in tag_type
    is_mp4 = "MP4Tags" in tag_type or "MP4" in tag_type

    if is_vorbis:
        if bpm is not None:
            audio.tags["bpm"] = [str(int(round(bpm)))]
        if key is not None:
            audio.tags["initialkey"] = [key]
    elif is_mp4:
        # MP4/M4A uses atom keys — tmpo for BPM (integer list), freeform atom for key
        if bpm is not None:
            audio.tags["tmpo"] = [int(round(bpm))]
        if key is not None:
            from mutagen.mp4 import MP4FreeForm
            audio.tags["----:com.apple.iTunes:initialkey"] = [
                MP4FreeForm(key.encode("utf-8"))
            ]
    else:
        # delall() before setting ensures a clean overwrite regardless of the
        # existing frame's encoding or format (handles WAV + force-overwrite).
        if bpm is not None:
            audio.tags.delall("TBPM")
            audio.tags["TBPM"] = TBPM(encoding=3, text=[str(int(round(bpm)))])
        if key is not None:
            audio.tags.delall("TKEY")
            audio.tags["TKEY"] = TKEY(encoding=3, text=[key])

    audio.save()


# ─── Main entry point ─────────────────────────────────────────────────────────

def process_file(
    path: Path,
    *,
    detect_bpm: bool = True,
    detect_key: bool = True,
    normalise: bool = True,
    force: bool = False,
    enrich_tags: bool = False,
) -> ProcessResult:
    """Run the full analysis + normalisation pipeline on a single file."""
    result = ProcessResult(path=path)

    if not path.exists():
        result.errors.append("file not found")
        return result
    if path.suffix.lower() not in AUDIO_EXTENSIONS:
        result.errors.append(f"unsupported extension: {path.suffix}")
        return result

    try:
        audio = MutagenFile(str(path), easy=False)
        if audio is None:
            result.errors.append("mutagen could not open file (unsupported format)")
            return result
        # If the file has no tag block yet, create one now so we can write to it.
        if audio.tags is None:
            try:
                audio.add_tags()
                log.info("Created new tag block for tagless file: %s", path.name)
            except Exception as e:
                # Some formats (e.g. WAV) may need special handling — log and continue
                log.warning("Could not add tags to %s (%s: %s) — will attempt write anyway", path.name, type(e).__name__, e)
        tags = audio.tags
    except Exception as e:
        result.errors.append(f"could not read tags: {e}")
        tags = None

    tag_type = type(tags).__name__ if tags else ""
    is_vorbis = "VCFLACDict" in tag_type or "VComment" in tag_type

    def _existing(id3_key: str, vorbis_key: str) -> bool:
        if tags is None:
            return False
        if is_vorbis:
            val = tags.get(vorbis_key.lower())
            # Treat empty string or "0" as absent so force=False still writes
            return bool(val) and str(val[0] if isinstance(val, list) else val).strip() not in ("", "0")
        frame = tags.get(id3_key)
        return frame is not None and str(frame).strip() not in ("", "0")

    # ── Load audio once for BPM + key (shared decode) ──
    needs_bpm = detect_bpm and not (_existing("TBPM", "bpm") and not force)
    needs_key = detect_key and not (_existing("TKEY", "initialkey") and not force)
    _audio: "tuple[np.ndarray, int] | None" = None
    if needs_bpm or needs_key:
        _audio = _load_audio_ffmpeg(path)
        if _audio is None:
            result.errors.append("audio decode failed — BPM/key analysis skipped")

    # ── BPM ──
    if detect_bpm:
        if not needs_bpm:
            result.skipped_bpm = True
        elif _audio is not None:
            bpm = _detect_bpm(*_audio, path.name)
            result.bpm_detected = bpm
            if bpm is not None:
                try:
                    _write_tags(path, bpm=bpm, key=None)
                    result.bpm_written = True
                    log.info("BPM written: %.1f → %s", bpm, path.name)
                except Exception as e:
                    result.errors.append(f"BPM tag write failed: {e}")

    # ── Key ──
    if detect_key:
        if not needs_key:
            result.skipped_key = True
        elif _audio is not None:
            key = _detect_key(*_audio, path.name)
            result.key_detected = key
            if key is not None:
                try:
                    _write_tags(path, bpm=None, key=key)
                    result.key_written = True
                    log.info("KEY written: %s → %s", key, path.name)
                except Exception as e:
                    result.errors.append(f"KEY tag write failed: {e}")

    # ── Loudness ──
    if normalise:
        lufs = _measure_lufs(path)
        result.loudness_before = lufs
        if lufs is None:
            result.errors.append("loudness measurement failed")
        elif abs(lufs - TARGET_LUFS) <= LUFS_TOLERANCE:
            result.skipped_loudness = True
        else:
            gain_db = TARGET_LUFS - lufs
            log.info("Normalising %s: %.1f LUFS → %.1f (gain: %+.1f dB)",
                     path.name, lufs, TARGET_LUFS, gain_db)
            if _normalise_file(path, gain_db):
                result.loudness_after = _measure_lufs(path)
                result.normalised = True
            else:
                result.errors.append("normalisation failed")

    # ── MusicBrainz enrichment ──
    if enrich_tags:
        meta = _enrich_from_acoustid(path, force=force)
        if meta:
            written_fields = _write_enriched_tags(path, meta, force=force)
            if written_fields:
                result.enrich_written = True
                result.mb_recording_id = meta.get("recording_id")
                log.info("Enriched %s: wrote %s", path.name, ", ".join(written_fields))

    return result


# ─── Batch runner ─────────────────────────────────────────────────────────────

def process_directory(
    root: Path,
    *,
    detect_bpm: bool = True,
    detect_key: bool = True,
    normalise: bool = True,
    force: bool = False,
    enrich_tags: bool = False,
    max_workers: int = 1,
    pause_seconds: float = 0.0,
    quarantine_dir: Path | None = None,
) -> list[ProcessResult]:
    """
    Process all audio files under root. Returns all ProcessResults.

    Parameters
    ----------
    root : Path
        Directory to scan recursively.
    detect_bpm, detect_key, normalise, force : bool
        Passed through to process_file().
    max_workers : int
        Number of files to process in parallel. Default 1 (sequential).
        Values > 1 use a ThreadPoolExecutor. Keep at 1 on systems where
        the BPM/key libraries are not thread-safe, or when normalisation
        is enabled (ffmpeg is subprocess-safe but concurrent re-encoding
        is very disk-intensive).
    pause_seconds : float
        Seconds to sleep between files (sequential mode only). Use this to
        keep CPU load below 100% on slower machines or when DJing on the
        same computer. Default 0.0 (no pause).
    quarantine_dir : Path | None
        If provided, any file whose result is corrupt (cannot be opened
        at the binary level) is moved here after processing. Pass the
        RekitBox Archive Quarantine path from config or a custom location.
    """
    import concurrent.futures
    from scanner import scan_directory

    tracks = list(scan_directory(root))
    total = len(tracks)
    results: list[ProcessResult] = []

    if total == 0:
        log.info("No audio files found under %s", root)
        return results

    log.info(
        "Processing %d files — workers=%d pause=%.1fs",
        total, max_workers, pause_seconds,
    )

    # Running counters for live progress ticker
    done = 0
    clean = 0
    errors = 0
    edited = 0
    tags_written = 0
    bpm_key_written = 0
    quarantined = 0
    enriched = 0

    def _emit_progress() -> None:
        print(
            "REKITBOX_PROGRESS: " + json.dumps({
                "done":          done,
                "total":         total,
                "remaining":     total - done,
                "clean":         clean,
                "errors":        errors,
                "edited":        edited,
                "tags_written":  tags_written,
                "bpm_key_written": bpm_key_written,
                "quarantined":   quarantined,
                "enriched":      enriched,
            }),
            flush=True,
        )

    def _tally(r: ProcessResult) -> None:
        nonlocal done, clean, errors, edited, tags_written, bpm_key_written, quarantined, enriched
        done += 1
        if r.errors:
            errors += 1
        if r.quarantined:
            quarantined += 1
        if r.enrich_written:
            enriched += 1
        any_edit = r.bpm_written or r.key_written or r.normalised
        if any_edit:
            edited += 1
            if r.bpm_written or r.key_written:
                bpm_key_written += 1
            tags_written += 1  # all writes: bpm, key, or normalisation
        elif r.ok:
            clean += 1
        # Quarantined files are gone from their original path — don't index them
        if r.quarantined:
            return
        # Build scan index entry — duration via soundfile header (fast, no decode)
        try:
            duration_sec = round(sf.info(str(r.path)).duration, 1)
        except Exception:
            duration_sec = None
        try:
            file_size = r.path.stat().st_size
        except OSError:
            file_size = 0
        # Read current BPM/key from tags (may have just been written)
        bpm_val = None
        key_val = None
        try:
            audio = MutagenFile(str(r.path), easy=False)
            if audio and audio.tags:
                tbpm = audio.tags.get("TBPM")
                if tbpm:
                    bpm_val = str(tbpm).strip()
                tkey = audio.tags.get("TKEY")
                if tkey:
                    key_val = str(tkey).strip()
        except Exception:
            pass
        scan_index.append({
            "path":         str(r.path),
            "bpm":          bpm_val,
            "key":          key_val,
            "duration_sec": duration_sec,
            "file_size":    file_size,
        })

    def _process_one(track, index: int) -> ProcessResult:
        r = process_file(
            track.path,
            detect_bpm=detect_bpm,
            detect_key=detect_key,
            normalise=normalise,
            force=force,
            enrich_tags=enrich_tags,
        )
        if r.errors:
            log.info("[%d/%d] %s  ✗ errors: %s",
                     index, total, track.path.name, ", ".join(r.errors))
        else:
            log.info("[%d/%d] %s", index, total, track.path.name)
        # Quarantine corrupt files immediately after processing
        if quarantine_dir and is_corrupt(r):
            quarantine_file(r, quarantine_dir)
            log.warning("QUARANTINED: %s → %s", track.path.name, quarantine_dir)
        return r

    scan_index: list[dict] = []   # accumulates entries for scan_index.json

    if max_workers > 1:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = {
                ex.submit(_process_one, track, i + 1): i
                for i, track in enumerate(tracks)
            }
            for future in concurrent.futures.as_completed(futures):
                try:
                    r = future.result()
                    results.append(r)
                    _tally(r)
                    _emit_progress()
                except Exception as exc:
                    idx = futures[future]
                    log.error("Unexpected error processing file %d: %s", idx + 1, exc)
                    done += 1
                    errors += 1
                    _emit_progress()
    else:
        for i, track in enumerate(tracks):
            r = _process_one(track, i + 1)
            results.append(r)
            _tally(r)
            _emit_progress()
            if pause_seconds > 0 and i < total - 1:
                time.sleep(pause_seconds)

    # Write scan index for duplicate pre-filter
    if scan_index:
        index_path = Path.home() / "rekordbox-toolkit" / "scan_index.json"
        index_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            existing: dict[str, dict] = {}
            if index_path.exists():
                with open(index_path, encoding="utf-8") as f:
                    for entry in json.load(f):
                        existing[entry["path"]] = entry
            for entry in scan_index:
                existing[entry["path"]] = entry
            with open(index_path, "w", encoding="utf-8") as f:
                json.dump(list(existing.values()), f, indent=2)
            log.info("Scan index written: %s (%d entries)", index_path, len(existing))
        except Exception as exc:
            log.warning("Could not write scan index: %s", exc)

    # Emit structured error summary so the UI can build actionable next steps.
    # Emitted as REKITBOX_ERROR_SUMMARY: {json} — parsed by the JS SSE handler.
    errored_results = [r for r in results if r.errors]
    if errored_results:
        def _short_err(r: ProcessResult) -> str:
            return r.errors[0] if r.errors else "unknown error"

        corrupt_list:  list[dict] = []
        decode_list:   list[dict] = []
        tag_list:      list[dict] = []
        other_list:    list[dict] = []

        for r in errored_results:
            entry = {"name": r.path.name, "path": str(r.path), "error": _short_err(r)}
            if r.quarantined:
                corrupt_list.append(entry)
            elif any("audio decode failed" in e for e in r.errors):
                decode_list.append(entry)
            elif any("tag write failed" in e or "normalisation failed" in e for e in r.errors):
                tag_list.append(entry)
            else:
                other_list.append(entry)

        print(
            "REKITBOX_ERROR_SUMMARY: " + json.dumps({
                "corrupt":       corrupt_list,
                "decode_failed": decode_list,
                "tag_failed":    tag_list,
                "other":         other_list,
                "quarantine_dir": str(quarantine_dir) if quarantine_dir else None,
            }),
            flush=True,
        )

    return results


# ─── Smoke test ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    test_files = [
        Path("/Volumes/DJMT/DJMT PRIMARY/Kerri Chandler/Sunset - So Let The Wind Come/02 Sunset - So Let The Wind Come.mp3"),
        Path("/Volumes/DJMT/DJMT PRIMARY/DJMT PRIMARY/The Salsoul Orchestra/The Salsoul Orchestra/01 - Salsoul Hustle .flac"),
    ]

    for f in test_files:
        if not f.exists():
            print(f"SKIP (not found): {f.name}")
            continue
        print(f"\n{'─'*60}")
        print(f"FILE: {f.name}")
        r = process_file(f, detect_bpm=True, detect_key=True, normalise=False, force=False)
        print(f"  BPM detected : {r.bpm_detected}  written={r.bpm_written}  skipped={r.skipped_bpm}")
        print(f"  KEY detected : {r.key_detected}  written={r.key_written}  skipped={r.skipped_key}")
        print(f"  Errors       : {r.errors or 'none'}")
        print(f"  Status       : {'OK' if r.ok else 'ERRORS'}")
