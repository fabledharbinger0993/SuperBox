"""
rekordbox-toolkit / audio_processor.py

Analyses and normalises audio files in-place. No database interaction.

Operations per file (each independently skippable):
  1. BPM detection    — aubio tempo, written to TBPM tag
  2. Key detection    — Krumhansl-Schmuckler via librosa chroma, written to TKEY (Camelot)
  3. Loudness check   — pyloudnorm EBU R128 measurement
  4. Normalisation    — ffmpeg volume filter if outside tolerance, in-place replacement

Design rules:
  - Existing tags are NEVER overwritten unless force=True is passed
  - Original files are never deleted until the replacement is verified
  - All failures are logged and returned in ProcessResult — nothing crashes the batch
  - MP3s are re-encoded at 320kbps CBR if normalisation is applied
  - AIFFs are re-encoded losslessly (pcm_s16le or pcm_s24le, matching source bit depth)

Target loudness: -8.0 LUFS (DJ standard)
Tolerance:        ±0.5 LUFS (skip normalisation if within this window)
"""

import logging
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

import aubio
import librosa
import numpy as np
import pyloudnorm as pyln
import soundfile as sf
from mutagen import File as MutagenFile
from mutagen.id3 import TBPM, TKEY

from config import AUDIO_EXTENSIONS, BPM_MAX, BPM_MIN, LUFS_TOLERANCE, TARGET_LUFS

log = logging.getLogger(__name__)

# ─── Constants ────────────────────────────────────────────────────────────────

# TARGET_LUFS and LUFS_TOLERANCE are loaded from user config via config.py.
# To change the target, run: python3 cli.py setup --update
ANALYSIS_DURATION: float = 90.0
BPM_HOP_SIZE: int = 512
BPM_WIN_SIZE: int = 1024

_LIBROSA_TO_CAMELOT: dict[str, str] = {
    "Amin": "8A",  "Emin": "9A",   "Bmin": "10A", "F#min": "11A",
    "C#min": "12A","G#min": "1A",  "D#min": "2A", "A#min": "3A",
    "Fmin": "4A",  "Cmin": "5A",   "Gmin": "6A",  "Dmin": "7A",
    "Cmaj": "8B",  "Gmaj": "9B",   "Dmaj": "10B", "Amaj": "11B",
    "Emaj": "12B", "Bmaj": "1B",   "F#maj": "2B", "C#maj": "3B",
    "G#maj": "4B", "D#maj": "5B",  "A#maj": "6B", "Fmaj": "7B",
}

_KS_MAJOR = np.array([6.35,2.23,3.48,2.33,4.38,4.09,2.52,5.19,2.39,3.66,2.29,2.88])
_KS_MINOR = np.array([6.33,2.68,3.52,5.38,2.60,3.53,2.54,4.75,3.98,2.69,3.34,3.17])
_NOTES = ["C","C#","D","D#","E","F","F#","G","G#","A","A#","B"]


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
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return len(self.errors) == 0


# ─── BPM detection ────────────────────────────────────────────────────────────

def _detect_bpm(path: Path) -> float | None:
    try:
        src = aubio.source(str(path), hop_size=BPM_HOP_SIZE)
        tempo = aubio.tempo("default", BPM_WIN_SIZE, BPM_HOP_SIZE, src.samplerate)
        beats: list[float] = []
        while True:
            samples, read = src()
            if tempo(samples):
                beats.append(tempo.get_bpm())
            if read < BPM_HOP_SIZE:
                break
        if not beats:
            return None
        bpm = float(np.median(beats))
        if BPM_MIN <= bpm <= BPM_MAX:
            return round(bpm, 2)
        log.warning("BPM %s out of range (%s–%s) for %s", bpm, BPM_MIN, BPM_MAX, path.name)
        return None
    except Exception as e:
        log.error("BPM detection failed for %s: %s", path.name, e)
        return None


# ─── Key detection ────────────────────────────────────────────────────────────

def _detect_key(path: Path) -> str | None:
    try:
        y, sr = librosa.load(str(path), duration=ANALYSIS_DURATION, mono=True)
        chroma = librosa.feature.chroma_cqt(y=y, sr=sr).mean(axis=1)
        scores: dict[str, float] = {}
        for i, note in enumerate(_NOTES):
            rolled = np.roll(chroma, -i)
            scores[note + "maj"] = float(np.corrcoef(rolled, _KS_MAJOR)[0, 1])
            scores[note + "min"] = float(np.corrcoef(rolled, _KS_MINOR)[0, 1])
        best = max(scores, key=scores.get)  # type: ignore[arg-type]
        camelot = _LIBROSA_TO_CAMELOT.get(best)
        if camelot is None:
            log.warning("No Camelot mapping for detected key %r", best)
            return None
        log.debug("Key detected: %s → %s  (score %.3f)", best, camelot, scores[best])
        return camelot
    except Exception as e:
        log.error("Key detection failed for %s: %s", path.name, e)
        return None


# ─── Loudness measurement ─────────────────────────────────────────────────────

def _measure_lufs(path: Path) -> float | None:
    try:
        data, rate = sf.read(str(path))
        meter = pyln.Meter(rate)
        lufs = meter.integrated_loudness(data)
        if not np.isfinite(lufs):
            log.warning("Non-finite LUFS for %s (silent file?)", path.name)
            return None
        return round(float(lufs), 2)
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
            "ffmpeg", "-y", "-i", str(path),
            "-af", f"volume={gain_db:.4f}dB",
            *codec_args,
            "-map_metadata", "0",
            "-id3v2_version", "3",
            str(tmp_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            log.error("ffmpeg failed for %s:\n%s", path.name, result.stderr[-500:])
            return False

        # sf.read raises SoundFileError on failure — it never returns None.
        # Check for an empty or implausibly short result instead.
        verify_data, verify_rate = sf.read(str(tmp_path))
        if len(verify_data) == 0:
            log.error("ffmpeg output is empty (zero samples) for %s", path.name)
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


# ─── Tag writing ──────────────────────────────────────────────────────────────

def _write_tags(path: Path, bpm: float | None, key: str | None) -> None:
    """Write BPM and/or key to file tags via mutagen. Raises on failure."""
    audio = MutagenFile(str(path), easy=False)
    if audio is None:
        raise RuntimeError(f"mutagen could not open {path.name}")
    if audio.tags is None:
        audio.add_tags()

    tag_type = type(audio.tags).__name__
    is_vorbis = "VCFLACDict" in tag_type or "VComment" in tag_type

    if is_vorbis:
        if bpm is not None:
            audio.tags["bpm"] = [str(int(round(bpm)))]
        if key is not None:
            audio.tags["initialkey"] = [key]
    else:
        if bpm is not None:
            audio.tags["TBPM"] = TBPM(encoding=3, text=[str(int(round(bpm)))])
        if key is not None:
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
        tags = audio.tags if audio else None
    except Exception as e:
        result.errors.append(f"could not read tags: {e}")
        tags = None

    tag_type = type(tags).__name__ if tags else ""
    is_vorbis = "VCFLACDict" in tag_type or "VComment" in tag_type

    def _existing(id3_key: str, vorbis_key: str) -> bool:
        if tags is None:
            return False
        if is_vorbis:
            return bool(tags.get(vorbis_key.lower()))
        frame = tags.get(id3_key)
        return frame is not None and str(frame).strip() not in ("", "0")

    # ── BPM ──
    if detect_bpm:
        if _existing("TBPM", "bpm") and not force:
            result.skipped_bpm = True
        else:
            bpm = _detect_bpm(path)
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
        if _existing("TKEY", "initialkey") and not force:
            result.skipped_key = True
        else:
            key = _detect_key(path)
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

    return result


# ─── Batch runner ─────────────────────────────────────────────────────────────

def process_directory(
    root: Path,
    *,
    detect_bpm: bool = True,
    detect_key: bool = True,
    normalise: bool = True,
    force: bool = False,
    max_workers: int = 1,
    pause_seconds: float = 0.0,
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

    def _process_one(track, index: int) -> ProcessResult:
        r = process_file(
            track.path,
            detect_bpm=detect_bpm,
            detect_key=detect_key,
            normalise=normalise,
            force=force,
        )
        log.info("[%d/%d] %s%s",
                 index, total, track.path.name,
                 "  ✗ errors: " + ", ".join(r.errors) if r.errors else "")
        return r

    if max_workers > 1:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = {
                ex.submit(_process_one, track, i + 1): i
                for i, track in enumerate(tracks)
            }
            for future in concurrent.futures.as_completed(futures):
                try:
                    results.append(future.result())
                except Exception as exc:
                    idx = futures[future]
                    log.error("Unexpected error processing file %d: %s", idx + 1, exc)
    else:
        for i, track in enumerate(tracks):
            results.append(_process_one(track, i + 1))
            if pause_seconds > 0 and i < total - 1:
                time.sleep(pause_seconds)

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
