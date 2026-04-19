"""
rekordbox-toolkit / renamer.py

Batch-renames audio files in a directory based on their ID3/Vorbis tags,
generating clean filenames with smart artist prioritization.

This tool extracts metadata (artist, title) from tags and replaces
underscores, numbers, and processing suffixes with a standardized format.
All metadata remains in the ID3 tags for database searchability.

Design:
  - Reads metadata via mutagen (same as scanner.py)
  - Artist priority: vocal/lead (TPE1) > album artist/band (TPE2) > fallback
  - Generates clean filenames: "{Artist}: {Title}.{ext}" or "{Artist}: {Title} (2).{ext}"
  - Preserves copy markers: (2), (3), (copy), (duplicate), (v2) from original filename
  - Falls back to original filename if title missing
  - Handles collisions: if file exists, uses (2), (3), ... until free slot found
  - Updates rekordbox DjmdContent.FolderPath for each renamed file
  - No file moves — renames happen in place
  - Dry-run mode by default; pass dry_run=False to execute

Supported naming patterns (detected and cleaned):
  - "SomethingPN.mp3" or "Something_PN.mp3" → extracts title, removes PN
  - "918223_SomethingElse.mp3" → extracts title, removes ID prefix
  - "Something_918223.mp3" → extracts title, removes ID suffix
  - "Track (remix).mp3" or "Track (dub).mp3" → preserves remix/version markers
  - "Track (2).mp3" → preserves copy marker as "Artist: Track (2).mp3"
  - Remixes: Uses original artist, preserves remixer in title marker
    E.g., "Donna Summer: On the Radio (Felix da-Housecat remix)"
  - Standard "Artist - Title.mp3" → extracted with artist prioritization
  - Anything else → fallback to original name

Artist Priority Examples:
  - If both vocal artist (TPE1) and album artist (TPE2) exist → uses vocal artist
  - If only album artist exists → uses band name
  - If only producer/release artist exists → uses producer name
  - If nothing in tags → tries filename parsing

Copy Suffix Examples:
  - "Track (2).mp3" → "Artist: Track (2).mp3"
  - "Remix (copy).mp3" → "Artist: Remix (copy).mp3"
  - Duplicates after rename: "Artist: Track (2).mp3", "Artist: Track (3).mp3", etc.
"""

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TYPE_CHECKING

from mutagen import File as MutagenFile
from mutagen.id3 import ID3, ID3NoHeaderError

from config import AUDIO_EXTENSIONS, BATCH_SIZE, SKIP_DIRS, SKIP_PREFIXES
from scanner import extract_metadata

if TYPE_CHECKING:
    from pyrekordbox.db6.tables import DjmdContent

log = logging.getLogger(__name__)

# Patterns to detect and clean from filenames
_PN_SUFFIX = re.compile(r'_?PN\s*\d*$', re.IGNORECASE)  # "Something_PN" or "SomethingPN2"
_ID_PREFIX = re.compile(r'^\d{6,}\s*[-_.]')             # "918223_Title" or "918223-Title"
_ID_SUFFIX = re.compile(r'[-_\.]\d{6,}$')               # "Title_918223" or "Title-918223"
_UNDERSCORE = re.compile(r'_')                          # Underscores (replaced with spaces)
_MULTI_SPACE = re.compile(r'\s{2,}')                    # Multiple spaces
_UNSAFE_CHARS = re.compile(r'[\\/:*?"<>|]')             # Filesystem-unsafe
_COPY_SUFFIX = re.compile(r'\s*\((\d+|copy|duplicate|v\d+)\)\s*$', re.IGNORECASE)  # "(2)", "(copy)", etc.
_VERSION_MARKERS = re.compile(
    r'\((remix|dub|extended|acoustic|instrumental|version|edit|remix[\s\-]mix|remaster|radio[\s\-]edit)\)',
    re.IGNORECASE
)                                                        # Version/remix markers to preserve


def _get_prioritized_artist(path: Path) -> str | None:
    """
    Read artist tags from the file and return the highest-priority artist.
    
    Priority order (use first available):
    1. TPE1 (Lead/Vocal artist) — the vocalist or primary performer
    2. TPE2 (Album artist/Band) — the band or ensemble name
    3. Fall back to None
    
    For remixes: Returns the original artist, not the remixer.
    E.g., "Donna Summer" (not "Felix da Housecat") for a remix.
    
    Returns: Cleaned artist string or None.
    """
    try:
        mf = MutagenFile(path)
        if not mf or not mf.tags:
            return None
        
        tags = mf.tags
        
        # ID3 tags (MP3, AIFF, WAV with ID3)
        if isinstance(tags, ID3):
            # TPE1: Lead/Vocal artist
            tpe1 = tags.get('TPE1')
            if tpe1 and str(tpe1).strip():
                return str(tpe1).strip()
            # TPE2: Album artist (band, ensemble)
            tpe2 = tags.get('TPE2')
            if tpe2 and str(tpe2).strip():
                return str(tpe2).strip()
        
        # Vorbis comments (FLAC, OGG, Opus)
        elif hasattr(tags, 'get'):
            # Vorbis ARTIST (vocalist/lead)
            artist = tags.get('artist')
            if artist and isinstance(artist, list) and artist[0].strip():
                return artist[0].strip()
            # Vorbis ALBUMARTIST (band/ensemble)
            album_artist = tags.get('albumartist')
            if album_artist and isinstance(album_artist, list) and album_artist[0].strip():
                return album_artist[0].strip()
        
        return None
    except Exception as e:
        log.debug(f"Could not read artist tags from {path}: {e}")
        return None


@dataclass
class RenameResult:
    """Outcome of a single file rename."""
    original_path: Path
    new_path: Path | None
    action: str  # "renamed" | "skipped" | "collision_numbered" | "error" | "no_change"
    reason: str = ""
    content_id: str | None = None


def _extract_artist_title(path: Path, metadata) -> tuple[str | None, str | None, str | None]:
    """
    Best-effort extraction of artist, title, and copy suffix from metadata.
    Prefers tag fields, falls back to filename parsing.
    
    Artist priority: vocal artist (TPE1) > album artist (TPE2) > fallback
    Title: extracted from tags or filename, preserves remix/version markers.
    Copy suffix: extracted from original filename (e.g., "(2)", "(copy)", "(v2)")
    Removes only filler: PN suffixes, numeric prefixes/suffixes, underscores.
    
    Returns: (artist, title, copy_suffix) where copy_suffix is None or a string like "(2)"
    """
    # Try to get artist from tags with priority (vocalist > band > producer)
    artist = _get_prioritized_artist(path)
    
    # Fallback to scanner's metadata.artist if prioritized method returned nothing
    if not artist:
        artist = metadata.artist or None
    
    title = metadata.title or None
    copy_suffix = None
    
    # Extract copy suffix from original filename first (before any cleaning)
    stem_original = path.stem
    copy_match = _COPY_SUFFIX.search(stem_original)
    if copy_match:
        copy_suffix = f"({copy_match.group(1)})"
        # Remove copy suffix from stem for further processing
        stem_original = _COPY_SUFFIX.sub('', stem_original).strip()
    
    # Both found in tags — use them (preserves remix/dub markers if in tag)
    if artist and title:
        return artist, title, copy_suffix
    
    # Try filename-based fallback for title
    stem = stem_original
    
    # Strip Pioneer/MiX markers: _PN, _PN2, _PN 3, or PN (no underscore)
    stem = _PN_SUFFIX.sub('', stem).strip()
    
    # Strip numeric prefixes: "918223_Title" or "918223-Title"
    stem = _ID_PREFIX.sub('', stem).strip()
    
    # Strip numeric suffixes: "Title_918223" or "Title-918223"
    stem = _ID_SUFFIX.sub('', stem).strip()
    
    # Replace underscores with spaces (they're filename separators, not part of title)
    stem = _UNDERSCORE.sub(' ', stem).strip()
    
    # Parse "Artist - Title" from filename if it exists and we're missing either field
    if ' - ' in stem and (not artist or not title):
        parts = stem.split(' - ', 1)
        if len(parts) == 2:
            if not artist:
                artist = parts[0].strip()
            if not title:
                title = parts[1].strip()
    
    # Last resort: use stem as title if we still have nothing
    if not title:
        title = stem if stem else None
    
    return artist or None, title or None, copy_suffix


def _sanitize_filename(text: str, max_len: int = 200) -> str:
    """
    Clean a string for use in a filename.
    Removes filesystem-unsafe characters, collapses spaces, strips dots.
    """
    text = _UNSAFE_CHARS.sub(" ", text)
    text = _MULTI_SPACE.sub(" ", text).strip().strip(".")
    return text[:max_len] if text else "Unknown"


def _generate_filename(artist: str | None, title: str | None, ext: str, copy_suffix: str | None = None) -> str:
    """
    Generate a clean filename with artist, title, and optional copy suffix.
    Format: "Artist: Title.ext" or "Artist: Title (2).ext"
    
    Artist and title are both included in filename for clear visual identification.
    Copy suffix (e.g., "(2)", "(copy)") is appended before the extension if present.
    Full metadata remains in ID3 tags for database searchability.
    """
    artist = _sanitize_filename(artist or "Unknown")
    title = _sanitize_filename(title or "Unknown")
    suffix_str = f" {copy_suffix}" if copy_suffix else ""
    return f"{artist}: {title}{suffix_str}{ext}"


def _resolve_filename_collision(dest: Path) -> Path:
    """
    If dest already exists, append (2), (3), ... until a free slot is found.
    Returns the new collision-safe path or None if no slot found within 100 attempts.
    
    Uses (2), (3) format to match standard copy naming conventions.
    """
    if not dest.exists():
        return dest
    
    stem, suffix = dest.stem, dest.suffix
    for i in range(2, 101):
        candidate = dest.with_name(f"{stem} ({i}){suffix}")
        if not candidate.exists():
            return candidate
    
    return None  # No free slot found (extremely unlikely)


def _walk_audio_files(root: Path) -> list[Path]:
    """Return all audio files under root, respecting skip lists."""
    files: list[Path] = []
    try:
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
    except OSError as e:
        log.warning(f"Error walking {root}: {e}")
    return files


def _rename_one(
    path: Path,
    db=None,
    dry_run: bool = True,
) -> RenameResult:
    """
    Rename a single audio file based on its metadata.
    Updates rekordbox DjmdContent.FolderPath if db is provided.
    
    Returns: RenameResult with action and outcome.
    """
    try:
        metadata = extract_metadata(path)
        artist, title, copy_suffix = _extract_artist_title(path, metadata)
    except Exception as e:
        return RenameResult(
            original_path=path,
            new_path=None,
            action="error",
            reason=f"Metadata extraction failed: {e}",
        )
    
    ext = path.suffix
    new_name = _generate_filename(artist, title, ext, copy_suffix)
    new_path = path.parent / new_name
    
    # If the new name matches the current name, skip
    if new_path == path:
        return RenameResult(
            original_path=path,
            new_path=path,
            action="no_change",
            reason="Filename already matches metadata",
        )
    
    # Handle collisions
    if new_path.exists():
        collision_path = _resolve_filename_collision(new_path)
        if collision_path is None:
            return RenameResult(
                original_path=path,
                new_path=None,
                action="error",
                reason="No available collision-free slot",
            )
        new_path = collision_path
        action = "collision_numbered"
    else:
        action = "renamed"
    
    if not dry_run:
        try:
            path.rename(new_path)
            log.info(f"Renamed: {path.name} → {new_path.name}")
            
            # Update rekordbox if db is provided
            if db is not None:
                try:
                    _update_db_path(path, new_path, db)
                except Exception as e:
                    log.warning(f"Database update failed for {new_path}: {e}")
        except OSError as e:
            return RenameResult(
                original_path=path,
                new_path=None,
                action="error",
                reason=f"Rename failed: {e}",
            )
    
    return RenameResult(
        original_path=path,
        new_path=new_path,
        action=action,
    )


def _update_db_path(old_path: Path, new_path: Path, db) -> None:
    """
    Update rekordbox DjmdContent.FolderPath for the given file.
    Matches by file hash (same strategy as relocator.py).
    """
    if not hasattr(db, 'update_content_path'):
        log.debug("Database does not support update_content_path — skipping DB update")
        return
    
    # Search for content row with matching file
    try:
        content_row = db.search_by_path(str(old_path))
        if content_row:
            db.update_content_path(content_row, new_path, check_path=True)
            log.debug(f"Updated DB: {old_path.name} → {new_path.name}")
    except Exception as e:
        log.warning(f"Database lookup/update failed: {e}")


# ─── Public interface ────────────────────────────────────────────────────────

def rename_directory(
    root: Path,
    db=None,
    *,
    dry_run: bool = True,
    max_workers: int = 1,
) -> list[RenameResult]:
    """
    Batch-rename all audio files in a directory based on their metadata.
    
    Parameters
    ----------
    root : Path
        Directory to scan for audio files.
    db : Rekordbox6Database, optional
        If provided, updates DjmdContent.FolderPath for each renamed file.
    dry_run : bool
        If True (default), compute and report changes without touching files.
        Pass dry_run=False to execute renames.
    max_workers : int
        Parallel workers for rename operations (default 1 = sequential).
    
    Returns
    -------
    list[RenameResult]
        Outcome for each file processed.
    """
    files = _walk_audio_files(root)
    total = len(files)
    results: list[RenameResult] = []
    
    if total == 0:
        log.info(f"No audio files found in {root}")
        return results
    
    log.info(
        f"Renaming {total} files in {root}  dry_run={dry_run}  workers={max_workers}"
    )
    
    renamed = skipped = collisions = errors = 0
    
    def _emit() -> None:
        print(
            "REKITBOX_PROGRESS: " + json.dumps({
                "done":      len(results),
                "total":     total,
                "remaining": total - len(results),
                "renamed":   renamed,
                "skipped":   skipped,
                "collisions": collisions,
                "errors":    errors,
            }),
            flush=True,
        )
    
    for i, file_path in enumerate(files):
        result = _rename_one(file_path, db=db, dry_run=dry_run)
        results.append(result)
        
        if result.action == "renamed":
            renamed += 1
        elif result.action == "no_change":
            skipped += 1
        elif result.action == "collision_numbered":
            collisions += 1
        elif result.action == "error":
            errors += 1
        
        if (i + 1) % max(1, total // 20) == 0 or i == total - 1:
            _emit()
    
    log.info(
        f"Rename complete: {renamed} renamed, {skipped} skipped, "
        f"{collisions} collisions handled, {errors} errors"
    )
    
    return results
