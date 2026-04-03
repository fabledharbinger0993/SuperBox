"""
rekordbox-toolkit / library_organizer.py

Consolidates a music library into a canonical folder structure:

  <target>/
  ├── <Artist>/
  │   └── <Album>/
  │       └── track.mp3           (Artist / Album / Track)
  │   └── track.mp3               (Artist / Track — when no album tag)
  ├── Orphaned Tracks/
  │   └── <YYYY>/
  │       └── track.mp3           (no artist tag)
  └── Live Sets & Mixes/
      └── <YYYY>/
          └── mix.mp3             (duration >= threshold, default 15 min)

Rules
-----
- Joint releases keep their combined artist string as the folder name
  (e.g. "Daft Punk & Basement Jaxx" → one folder).
- For artist folder naming, TPE2 (album artist) is preferred over
  TPE1 (track artist) to avoid "Artist feat. X" folder proliferation.
- Files without an album tag sit directly in the artist folder.
- If a destination file already exists with the same size → skip (duplicate).
- If it exists with a different size → rename with _1, _2, … suffix.
- After all moves, empty directories are pruned bottom-up from source.
"""

import json
import logging
import os
import re
import shutil
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

# ─── Constants ────────────────────────────────────────────────────────────────

MIX_FOLDER        = "Live Sets & Mixes"
ORPHAN_FOLDER     = "Orphaned Tracks"
MIX_THRESHOLD_SEC = 900.0   # 15 minutes

_UNSAFE_CHARS = re.compile(r'[\\/:*?"<>|]')
_MULTI_SPACE  = re.compile(r' {2,}')

# Camelot / Open-Key prefix  e.g. "10A - ", "10A 9A - ", "2B 3B - "
# Matches one or more  <1-2 digits><A|B>  groups followed by a dash separator.
_KEY_PREFIX = re.compile(r'^(?:\d{1,2}[ABab]\s+)*\d{1,2}[ABab]\s*[-–]\s*')


# ─── Result type ──────────────────────────────────────────────────────────────

@dataclass
class MoveResult:
    src:    Path
    dest:   Path | None
    action: str    # "moved" | "conflict_renamed" | "skipped" | "error" | "dry_run"
    reason: str = ""


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _sanitize_folder(name: str, max_len: int = 100) -> str:
    """Strip filesystem-unsafe characters and return a clean folder name."""
    name = _UNSAFE_CHARS.sub(" ", name)
    name = _MULTI_SPACE.sub(" ", name).strip().strip(".")
    return (name[:max_len] if name else "Unknown")


def _normalize_artist(name: str) -> str:
    """
    Strip RekordBox / Camelot key prefixes that sometimes get written into
    artist tags, e.g. "10A 9A - Kenny Dope" → "Kenny Dope".

    Applies the strip in a loop to handle doubled prefixes like
    "12A 11A - 12A 11A - Brother 2 Brother".
    """
    while True:
        stripped = _KEY_PREFIX.sub("", name).strip()
        if stripped == name:
            break
        name = stripped
    return name or name  # never return empty


def _folder_artist(path: Path) -> str | None:
    """
    Return the best artist string for folder naming.
    Prefers TPE2 (album artist) over TPE1 (track artist) so that
    'Artist feat. Guest' tracks land in the primary artist's folder.
    Falls back gracefully if tags can't be read.
    """
    try:
        from mutagen import File as MutagenFile
        audio = MutagenFile(str(path), easy=False)
        if audio is None or audio.tags is None:
            return None
        tags = audio.tags

        # ID3-style (MP3, AIFF, WAV)
        for frame_id in ("TPE2", "TPE1"):
            frame = tags.get(frame_id)
            if frame is not None:
                text = getattr(frame, "text", None)
                val = str(text[0]).strip() if text else str(frame).strip()
                if val:
                    return _normalize_artist(val)

        # Vorbis-style (FLAC, OGG)
        for key in ("albumartist", "album_artist", "artist"):
            val = tags.get(key)
            if val:
                s = str(val[0]).strip() if isinstance(val, list) else str(val).strip()
                if s:
                    return _normalize_artist(s)
    except Exception:
        pass
    return None


def _year_str(path: Path, tagged_year: int | None) -> str:
    """Return a 4-digit year string from tag or fall back to file mtime year."""
    if tagged_year:
        return str(tagged_year)
    try:
        return str(time.localtime(path.stat().st_mtime).tm_year)
    except OSError:
        return "Unknown Year"


def _canonical_dest(
    src: Path,
    target: Path,
    track,
    threshold: float,
) -> Path:
    """Compute the canonical destination path for a track (no I/O performed)."""
    year  = _year_str(src, track.year)
    fname = src.name

    # Long-form content (mixes, live sets, radio shows)
    if track.duration_seconds is not None and track.duration_seconds >= threshold:
        return target / MIX_FOLDER / year / fname

    # Resolve artist for folder naming (normalize away any key prefixes)
    raw_artist = _folder_artist(src) or track.artist
    artist = _normalize_artist(raw_artist) if raw_artist else None

    # No artist — orphaned
    if not artist or not artist.strip():
        return target / ORPHAN_FOLDER / year / fname

    # Normal: Artist / Album / Track  or  Artist / Track
    artist_dir = _sanitize_folder(artist)
    if track.album and track.album.strip():
        return target / artist_dir / _sanitize_folder(track.album) / fname
    return target / artist_dir / fname


def _resolve_dest(src: Path, dest: Path) -> tuple[Path | None, str]:
    """
    Returns (final_dest, action).

    action is one of:
      "moved"            — destination is free
      "skipped"          — same-size file exists (likely duplicate)
      "conflict_renamed" — different file exists; numbered suffix applied
      "error"            — could not find a free slot (extremely unlikely)
    """
    if not dest.exists():
        return dest, "moved"

    # Same size → treat as duplicate, skip
    try:
        if dest.stat().st_size == src.stat().st_size:
            return None, "skipped"
    except OSError:
        pass

    # Different file — find a numbered rename slot
    stem, suffix = dest.stem, dest.suffix
    for i in range(1, 100):
        candidate = dest.with_name(f"{stem}_{i}{suffix}")
        if not candidate.exists():
            return candidate, "conflict_renamed"

    return None, "error"


# ─── Main organizer ───────────────────────────────────────────────────────────

def organize_library(
    sources: "Path | list[Path]",
    target: Path,
    *,
    mode: str = "assimilate",
    dry_run: bool = True,
    max_workers: int = 1,
    mix_threshold_sec: float = MIX_THRESHOLD_SEC,
) -> list[MoveResult]:
    """
    Scan one or more source directories, compute the canonical destination for
    every audio file, and move or copy files into the Artist / Album / Track
    hierarchy under *target*.

    Parameters
    ----------
    sources : Path | list[Path]
        One directory or a list of directories to scan.  All are scanned in a
        single pass and their files are merged before processing begins.
    target : Path
        Root of the organised library (e.g. /Volumes/DJMT/DJMT PRIMARY).
    mode : str
        ``"assimilate"`` (default) — **move** files to target, delete confirmed
        source duplicates, and prune empty source directories afterwards.
        Use this to fully consolidate a library in place.

        ``"integrate"`` — **copy** files to target without touching the source
        at all.  Nothing is deleted or pruned from any source directory.  Use
        this when you want to pull music off a second drive without altering it.
    dry_run : bool
        If True (default), compute and report planned changes without touching
        the filesystem.  Run with dry_run=True first to preview.
    max_workers : int
        Parallel I/O workers for the move/copy phase (default 1 = sequential).
    mix_threshold_sec : float
        Tracks at or above this duration (seconds) are routed to
        Live Sets & Mixes instead of the normal Artist / Album tree.
        Default 900 = 15 minutes.
    """
    from scanner import scan_directory

    source_list: list[Path] = [sources] if isinstance(sources, Path) else list(sources)

    tracks: list = []
    for s in source_list:
        tracks.extend(list(scan_directory(s)))
    total  = len(tracks)
    results: list[MoveResult] = []

    if total == 0:
        log.info("No audio files found under %s", source_list)
        return results

    log.info(
        "Organizing %d files  sources=%s  target=%s  mode=%s  dry_run=%s  workers=%d",
        total, [str(s) for s in source_list], target, mode, dry_run, max_workers,
    )

    done = moved = skipped = conflicts = errors = 0

    def _emit() -> None:
        print(
            "SUPERBOX_PROGRESS: " + json.dumps({
                "done":      done,
                "total":     total,
                "remaining": total - done,
                "moved":     moved,
                "skipped":   skipped,
                "conflicts": conflicts,
                "errors":    errors,
            }),
            flush=True,
        )

    def _process(track) -> MoveResult:
        dest = _canonical_dest(track.path, target, track, mix_threshold_sec)

        # Already in the right place (in-place reorganisation with correct structure)
        if track.path.resolve() == dest.resolve():
            return MoveResult(src=track.path, dest=dest,
                              action="skipped", reason="already in place")

        if dry_run:
            rel = dest.relative_to(target) if dest.is_relative_to(target) else dest
            return MoveResult(src=track.path, dest=dest,
                              action="dry_run", reason=str(rel))

        # Ensure destination directory exists
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return MoveResult(src=track.path, dest=dest,
                              action="error", reason=f"mkdir failed: {e}")

        final, action = _resolve_dest(track.path, dest)

        if final is None:
            if action == "skipped":
                if mode == "assimilate":
                    # Identical copy confirmed at canonical destination.
                    # Remove the source so the source tree can be pruned cleanly.
                    try:
                        track.path.unlink()
                        return MoveResult(src=track.path, dest=dest,
                                          action="skipped", reason="duplicate removed from source")
                    except Exception as e:
                        return MoveResult(src=track.path, dest=dest,
                                          action="error", reason=f"unlink failed: {e}")
                else:
                    # integrate mode — source is never touched
                    return MoveResult(src=track.path, dest=dest,
                                      action="skipped", reason="duplicate at destination — source kept")
            return MoveResult(src=track.path, dest=dest,
                              action="error", reason="no rename slot found")

        try:
            if mode == "integrate":
                shutil.copy2(str(track.path), str(final))
            else:
                shutil.move(str(track.path), str(final))
            return MoveResult(src=track.path, dest=final, action=action)
        except Exception as e:
            return MoveResult(src=track.path, dest=final,
                              action="error", reason=str(e))

    def _tally(r: MoveResult) -> None:
        nonlocal done, moved, skipped, conflicts, errors
        done += 1
        if r.action in ("moved", "dry_run"):
            moved += 1
        elif r.action == "conflict_renamed":
            moved += 1
            conflicts += 1
        elif r.action == "skipped":
            skipped += 1
        elif r.action == "error":
            errors += 1

    _emit()

    if max_workers > 1:
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = {ex.submit(_process, track): track for track in tracks}
            for future in as_completed(futures):
                try:
                    r = future.result()
                except Exception as exc:
                    r = MoveResult(src=futures[future].path, dest=None,
                                   action="error", reason=str(exc))
                results.append(r)
                _tally(r)
                _emit()
    else:
        for i, track in enumerate(tracks):
            r = _process(track)
            log.info("[%d/%d] %-16s %s", i + 1, total, r.action.upper(), track.path.name)
            results.append(r)
            _tally(r)
            _emit()

    # Prune empty directories from all source roots — assimilate mode only.
    # integrate mode never modifies the source.
    if not dry_run and mode == "assimilate":
        for s in source_list:
            _prune_empty_dirs(s)

    return results


def _prune_empty_dirs(root: Path) -> None:
    """Remove empty leaf directories bottom-up; never removes root itself."""
    for dirpath, _dirs, _files in os.walk(root, topdown=False):
        p = Path(dirpath)
        if p == root:
            continue
        try:
            if not any(p.iterdir()):
                p.rmdir()
                log.info("Pruned empty dir: %s", p)
        except OSError:
            pass
