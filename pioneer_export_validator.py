"""
FableGear Pioneer Export Validator

Centralizes all Pioneer USB export constraints and validation logic:
  - FolderPath length validation (≤255 chars, pyrekordbox VARCHAR constraint)
  - Path collision detection (sanitized paths must be unique)
  - Metadata field population (OrgFolderPath, rb_LocalFolderPath)
  - Hardware compatibility checks

All export operations should call validate_export_paths() before committing
to the database. This ensures fail-fast behavior and clear error reporting.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Dict, List


# ─── Constants ────────────────────────────────────────────────────────────────

FOLDERPATH_MAX_LENGTH = 255
"""Maximum length for FolderPath in Rekordbox master.db (VARCHAR(255))."""


# ─── Validation Errors ────────────────────────────────────────────────────────

class PioneerExportError(Exception):
    """Base exception for Pioneer export validation failures."""
    pass


class FolderPathTooLongError(PioneerExportError):
    """FolderPath exceeds 255 character limit."""
    def __init__(self, path: str, length: int):
        super().__init__(
            f"FolderPath too long: {length} chars > {FOLDERPATH_MAX_LENGTH} limit. "
            f"Path: {path[:100]}..."
        )
        self.path = path
        self.length = length


class PathCollisionError(PioneerExportError):
    """Two different source paths would map to the same sanitized FolderPath."""
    def __init__(self, path1: str, path2: str, sanitized: str):
        super().__init__(
            f"Path collision detected: both {path1} and {path2} would "
            f"map to {sanitized}. Use hash-based naming or rename tracks."
        )
        self.path1 = path1
        self.path2 = path2
        self.sanitized = sanitized


class FileNotFoundError(PioneerExportError):
    """Copied file does not exist on target USB drive."""
    def __init__(self, path: str):
        super().__init__(
            f"File not found on USB drive: {path}. "
            f"Verify the file was copied before adding to database."
        )
        self.path = path


# ─── Validators ───────────────────────────────────────────────────────────────

def validate_folderpath_length(path: str) -> None:
    """Raise FolderPathTooLongError if path exceeds 255 chars."""
    if len(path) > FOLDERPATH_MAX_LENGTH:
        raise FolderPathTooLongError(path, len(path))


def validate_no_collisions(paths: List[str]) -> None:
    """
    Raise PathCollisionError if two paths would map to the same sanitized FolderPath.
    
    Example collision:
      - /Contents/Artist_Album.mp3  (original)
      - /Contents/Artist-Album.mp3  (would sanitize to same path if not careful)
    """
    seen: Dict[str, str] = {}
    
    for path in paths:
        # Use the actual path as-is; collisions only occur if two DIFFERENT
        # source paths would map to identical FolderPath values in the database.
        # Since FolderPath is the real location on USB after copying, collision
        # only happens if we try to add two tracks with the same destination path.
        if path in seen:
            # Same FolderPath proposed twice — this is a logic error in the export code
            raise PathCollisionError(seen[path], path, path)
        seen[path] = path


def validate_file_exists(path: str) -> None:
    """Raise FileNotFoundError if the file does not exist."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(str(p))


# ─── Export Path Builders ──────────────────────────────────────────────────────

def build_export_metadata(
    source_path: str,
    dest_path: str,
) -> Dict[str, Any]:
    """
    Build complete metadata for a track being exported to Pioneer drive.
    
    Parameters
    ----------
    source_path : str
        Original file path on local system (e.g., /Volumes/DJMT/Music/track.mp3)
    dest_path : str
        Actual file location on USB drive after copy (e.g., /Volumes/USB/Contents/Artist/track.mp3)
    
    Returns
    -------
    dict
        Metadata dict with FolderPath, OrgFolderPath, rb_LocalFolderPath, etc.
        Ready to pass to pyrekordbox DjmdContent creation.
    """
    validate_folderpath_length(dest_path)
    validate_file_exists(dest_path)
    
    return {
        "FolderPath": dest_path,
        # OrgFolderPath: original source path (for relocation tracking)
        "OrgFolderPath": source_path,
        # rb_LocalFolderPath: used by Rekordbox when moving files on the drive
        "rb_LocalFolderPath": str(Path(dest_path).parent),
    }


def validate_export_paths(export_entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Validate all paths in an export batch before committing to database.
    
    This is the PRE-COPY validation phase. Checks:
    - FolderPath length constraint (≤255 chars)
    - Path collision detection
    - Source files exist locally
    
    Post-copy existence checks happen in the copy loop after files are written to USB.
    
    Parameters
    ----------
    export_entries : list[dict]
        Each entry should have: dest_path (intended USB location), source_path (original).
    
    Returns
    -------
    list[dict]
        Same entries but with validated and populated metadata.
    
    Raises
    ------
    PioneerExportError
        If any path violates Pioneer export constraints (before copy).
    """
    validated = []
    dest_paths = []
    
    for entry in export_entries:
        dest_path = entry.get("dest_path", "")
        source_path = entry.get("source_path", "")
        
        if not dest_path or not source_path:
            raise PioneerExportError(
                f"Export entry missing dest_path or source_path: {entry}"
            )
        
        # Validate length (will be the FolderPath in DB)
        validate_folderpath_length(dest_path)
        
        # Validate SOURCE file exists (files haven't been copied to USB yet)
        # Post-copy existence checks happen later in the copy loop
        source_p = Path(source_path)
        if not source_p.exists():
            raise PioneerExportError(
                f"Source file not found locally: {source_path}"
            )
        
        # Track for collision detection
        dest_paths.append(dest_path)
        
        # Build metadata (OrgFolderPath, rb_LocalFolderPath)
        # Note: we don't call validate_file_exists() here—that happens post-copy
        metadata = {
            "FolderPath": dest_path,
            "OrgFolderPath": source_path,
            "rb_LocalFolderPath": str(Path(dest_path).parent),
        }
        entry.update(metadata)
        validated.append(entry)
    
    # Check for collisions after all individual validations pass
    validate_no_collisions(dest_paths)
    
    return validated


def validate_copied_file_exists(dest_path: str) -> None:
    """
    Validate that a file was successfully copied to the USB drive.
    
    This is the POST-COPY validation phase, called in the export loop after
    each file is copied.
    
    Parameters
    ----------
    dest_path : str
        Path on USB drive where file should have been copied.
    
    Raises
    ------
    FileNotFoundError
        If the file does not exist at dest_path.
    """
    validate_file_exists(dest_path)
