"""
rekordbox-toolkit / audit.py

Read-only database snapshot and filesystem validation.
No writes of any kind. Safe to run at any time, even with Rekordbox open.

Public interface:
    snapshot(db)                          -> LibrarySnapshot
    validate_paths(db)                    -> PathReport
    scan_physical_library(roots)          -> PhysicalScanReport
    write_physical_scan_report(report, p) -> Path
    full_audit(db, root)                  -> AuditReport

Typical usage:
    with read_db() as db:
        report = full_audit(db, MUSIC_ROOT)
        print(report.summary())
"""

import json
import logging
import os
import platform
from dataclasses import dataclass, field
from pathlib import Path

from pyrekordbox import Rekordbox6Database

from config import AUDIO_EXTENSIONS, MUSIC_ROOT, SKIP_DIRS, SKIP_PREFIXES

# macOS (APFS/HFS+) and Windows (NTFS) are case-insensitive filesystems.
# Linux is case-sensitive. Path comparisons must match the filesystem behaviour
# so orphan detection doesn't produce false positives or miss real orphans.
_FS_CASE_INSENSITIVE: bool = platform.system() in ("Darwin", "Windows")


def _normalise_path(p: str) -> str:
    """Normalise a path string for case-insensitive or case-sensitive comparison."""
    return p.lower() if _FS_CASE_INSENSITIVE else p

log = logging.getLogger(__name__)

# ─── Trash-folder detection ───────────────────────────────────────────────────
# Folder names that suggest their contents are marked for deletion.
# Mirrors (but does not import) the same logic in duplicate_detector.py to
# avoid a circular import — kept intentionally lean.

_TRASH_WORDS: frozenset[str] = frozenset({
    "trash", "trashed", "junk", "toss", "tossed", "delete", "deleted",
    "remove", "removed", "old stuff", "graveyard", "dead", "deprecated",
    "obsolete", "discard", "discarded", "recycle", "recycled", "recycling",
    "bin", "waste", "wasted", "dump", "dumped", "scrap", "scraps",
})
_TRASH_SUBSTRINGS: tuple[str, ...] = (
    "trash", "junk", "delete", "recycle", "graveyard", "toss",
)


def _is_trash_folder(name: str) -> bool:
    """Return True if a folder name suggests it contains discarded files."""
    n = name.lower().strip()
    if n in _TRASH_WORDS:
        return True
    return any(sub in n for sub in _TRASH_SUBSTRINGS)


# ─── Data structures ──────────────────────────────────────────────────────────

@dataclass
class LibrarySnapshot:
    """Counts of every significant entity in the database."""
    total_tracks: int = 0
    total_playlists: int = 0
    total_playlist_links: int = 0   # djmdSongPlaylist rows
    total_cue_points: int = 0
    total_history_sessions: int = 0
    total_artists: int = 0
    total_albums: int = 0
    total_keys: int = 0

    # Tag coverage
    tracks_with_bpm: int = 0
    tracks_with_key: int = 0
    tracks_with_artist: int = 0
    tracks_with_album: int = 0
    tracks_with_genre: int = 0

    # File type breakdown
    by_file_type: dict[str, int] = field(default_factory=dict)

    def coverage_pct(self, count: int) -> str:
        if self.total_tracks == 0:
            return "N/A"
        return f"{100 * count / self.total_tracks:.1f}%"


@dataclass
class PathReport:
    """Results of validating every FolderPath in DjmdContent against the filesystem."""
    total_checked: int = 0
    missing: list[tuple[str, str]] = field(default_factory=list)   # (ContentID, FolderPath)
    found: int = 0
    streaming_only: int = 0     # SoundCloud/Tidal tracks with no local path

    @property
    def missing_count(self) -> int:
        return len(self.missing)

    @property
    def integrity_pct(self) -> str:
        if self.total_checked == 0:
            return "N/A"
        local = self.total_checked - self.streaming_only
        if local == 0:
            return "N/A"
        return f"{100 * self.found / local:.1f}%"


@dataclass
class OrphanReport:
    """
    Files on disk that have no corresponding DjmdContent row.
    Only populated when full_audit() is called with a music root.
    """
    total_scanned: int = 0
    orphaned_paths: list[Path] = field(default_factory=list)

    @property
    def orphan_count(self) -> int:
        return len(self.orphaned_paths)


@dataclass
class DeadRootReport:
    """Path roots in the DB that don't exist on disk — likely disconnected/renamed drives."""
    dead_roots: dict[str, int] = field(default_factory=dict)   # prefix -> track count
    live_roots: dict[str, int] = field(default_factory=dict)   # prefix -> track count

    @property
    def has_dead_roots(self) -> bool:
        return bool(self.dead_roots)


@dataclass
class PhysicalScanReport:
    """
    A complete inventory of every audio file found under one or more roots.
    Pure filesystem data — no database involvement.

    Produced by scan_physical_library() and included in AuditReport when
    a root directory is available.  The all_files list is only populated when
    collect_all_files=True; omitting it keeps memory usage low for very large
    libraries while still giving the summary statistics.
    """
    roots_scanned: list[str] = field(default_factory=list)
    total_files: int = 0
    total_size_bytes: int = 0
    by_extension: dict[str, int] = field(default_factory=dict)
    by_extension_size: dict[str, int] = field(default_factory=dict)
    trash_adjacent_count: int = 0
    trash_adjacent_paths: list[str] = field(default_factory=list)
    all_files: list[dict] = field(default_factory=list)   # populated only on request

    @property
    def total_size_gb(self) -> float:
        return self.total_size_bytes / (1024 ** 3)

    @property
    def total_size_human(self) -> str:
        b = self.total_size_bytes
        if b >= 1024 ** 3:
            return f"{b / 1024 ** 3:.1f} GB"
        if b >= 1024 ** 2:
            return f"{b / 1024 ** 2:.1f} MB"
        return f"{b / 1024:.1f} KB"

    def summary_lines(self) -> list[str]:
        lines = [
            "  ── Physical Library Scan ──",
            f"  Roots scanned   : {len(self.roots_scanned)}",
        ]
        for r in self.roots_scanned:
            lines.append(f"    {r}")
        lines += [
            f"  Total files     : {self.total_files:,}",
            f"  Total size      : {self.total_size_human}",
            "  By format:",
        ]
        for ext, count in sorted(self.by_extension.items()):
            sz = self.by_extension_size.get(ext, 0)
            sz_human = f"{sz / 1024**3:.1f} GB" if sz >= 1024**3 else f"{sz / 1024**2:.1f} MB"
            lines.append(f"    {ext:6} : {count:,} files  ({sz_human})")
        if self.trash_adjacent_count:
            lines.append(f"  In trash-adjacent folders: {self.trash_adjacent_count:,}")
        return lines


@dataclass
class AuditReport:
    """Combined result of a full audit pass."""
    snapshot: LibrarySnapshot
    paths: PathReport
    orphans: OrphanReport
    dead_roots: DeadRootReport = field(default_factory=DeadRootReport)
    physical: "PhysicalScanReport | None" = None

    def summary(self, list_cap: int = 10) -> str:
        s = self.snapshot
        p = self.paths
        o = self.orphans
        d = self.dead_roots
        lines = [
            "═══ REKORDBOX LIBRARY AUDIT ═══",
            f"  Tracks          : {s.total_tracks}",
            f"  Playlists       : {s.total_playlists}",
            f"  Playlist links  : {s.total_playlist_links}",
            f"  Cue points      : {s.total_cue_points}",
            f"  Artists         : {s.total_artists}",
            f"  Albums          : {s.total_albums}",
            "",
            "  Tag coverage:",
            f"    BPM           : {s.tracks_with_bpm} ({s.coverage_pct(s.tracks_with_bpm)})",
            f"    Key           : {s.tracks_with_key} ({s.coverage_pct(s.tracks_with_key)})",
            f"    Artist        : {s.tracks_with_artist} ({s.coverage_pct(s.tracks_with_artist)})",
            f"    Album         : {s.tracks_with_album} ({s.coverage_pct(s.tracks_with_album)})",
            f"    Genre         : {s.tracks_with_genre} ({s.coverage_pct(s.tracks_with_genre)})",
            "",
            "  File types:",
        ]
        for ext, count in sorted(s.by_file_type.items()):
            lines.append(f"    {ext:6} : {count}")
        lines += [
            "",
            f"  Path integrity  : {p.integrity_pct} ({p.found}/{p.total_checked - p.streaming_only} local files found)",
            f"  Missing files   : {p.missing_count}",
            f"  Streaming tracks: {p.streaming_only}",
            f"  Orphaned files  : {o.orphan_count} (on disk, not in DB)",
            f"  Dead drive roots: {len(d.dead_roots)}",
        ]
        for prefix, count in sorted(d.dead_roots.items(), key=lambda x: -x[1]):
            lines.append(f"    {prefix}  →  {count:,} tracks unreachable")

        def _capped_list(items, cap, label):
            if not items:
                return []
            out = [f"", f"  ── {label} ──"]
            if len(items) > cap:
                out.append(f"  ({len(items)} total — showing first {cap})")
            for item in items[:cap]:
                out.append(f"    {item}")
            if len(items) > cap:
                out.append(f"    … and {len(items) - cap} more")
            return out

        if p.missing:
            lines += _capped_list(
                [fp for _, fp in p.missing], list_cap, f"Missing from disk ({p.missing_count})"
            )
        if o.orphaned_paths:
            lines += _capped_list(
                [path.name for path in o.orphaned_paths], list_cap,
                f"Orphaned — on disk but not in DB ({o.orphan_count})"
            )

        if self.physical:
            lines.append("")
            lines += self.physical.summary_lines()

        lines.append("═══════════════════════════════")
        return "\n".join(lines)


# ─── Snapshot ─────────────────────────────────────────────────────────────────

def snapshot(db: Rekordbox6Database) -> LibrarySnapshot:
    """
    Count every significant entity in the database.
    Pure reads — safe with Rekordbox open.
    """
    s = LibrarySnapshot()

    tracks = db.get_content().all()
    s.total_tracks = len(tracks)
    s.total_playlists = db.get_playlist().count()
    s.total_playlist_links = db.get_playlist_songs().count()
    s.total_cue_points = db.get_cue().count()
    s.total_history_sessions = db.get_history().count()
    s.total_artists = db.get_artist().count()
    s.total_albums = db.get_album().count()
    s.total_keys = db.get_key().count()

    for track in tracks:
        # BPM: stored as int×100, so 0 means unset
        if track.BPM is not None and track.BPM > 0:
            s.tracks_with_bpm += 1
        if track.KeyID is not None:
            s.tracks_with_key += 1
        if track.ArtistID is not None:
            s.tracks_with_artist += 1
        if track.AlbumID is not None:
            s.tracks_with_album += 1
        if track.GenreID is not None:
            s.tracks_with_genre += 1

        # File type from FolderPath extension — skip streaming URIs
        if track.FolderPath and not _is_streaming(track.FolderPath):
            ext = Path(track.FolderPath).suffix.upper().lstrip(".") or "UNKNOWN"
            s.by_file_type[ext] = s.by_file_type.get(ext, 0) + 1

    return s


# ─── Path validation ──────────────────────────────────────────────────────────

_STREAMING_PREFIXES = ("soundcloud:", "tidal:", "beatport-streaming:")


def _is_streaming(folder_path: str) -> bool:
    """Return True if the FolderPath is a streaming URI rather than a local file."""
    return any(folder_path.startswith(p) for p in _STREAMING_PREFIXES)


def validate_paths(db: Rekordbox6Database) -> PathReport:
    """
    Check every FolderPath in DjmdContent against the filesystem.
    Returns PathReport with lists of missing files.
    """
    report = PathReport()
    tracks = db.get_content().all()
    report.total_checked = len(tracks)

    for track in tracks:
        path_str = track.FolderPath
        if not path_str:
            report.missing.append((str(track.ID), "<empty path>"))
            continue

        if _is_streaming(path_str):
            report.streaming_only += 1
            continue

        if Path(path_str).exists():
            report.found += 1
        else:
            report.missing.append((str(track.ID), path_str))
            log.debug("Missing: %s", path_str)

    return report


# ─── Orphan detection ─────────────────────────────────────────────────────────

def find_orphans(db: Rekordbox6Database, root: Path) -> OrphanReport:
    """
    Walk root and find audio files that have no DjmdContent row.
    Builds an in-memory set of all known DB paths for O(1) lookup.
    """
    report = OrphanReport()

    # Build set of all known paths from DB, normalised for the host filesystem.
    # macOS (APFS/HFS+) and Windows (NTFS) are case-insensitive — we lowercase.
    # Linux is case-sensitive — we preserve case. See _normalise_path().
    known_paths: set[str] = set()
    for track in db.get_content().all():
        if track.FolderPath and not _is_streaming(track.FolderPath):
            known_paths.add(_normalise_path(track.FolderPath))

    if not root.is_dir():
        log.warning("Orphan scan root does not exist: %s", root)
        return report

    for dirpath, dirnames, filenames in root.walk():
        # Prune skip dirs in-place
        dirnames[:] = [
            d for d in dirnames
            if d not in SKIP_DIRS and not d.startswith(".")
        ]
        for filename in filenames:
            if any(filename.startswith(p) for p in SKIP_PREFIXES):
                continue
            file_path = dirpath / filename
            if file_path.suffix.lower() not in AUDIO_EXTENSIONS:
                continue
            report.total_scanned += 1
            if _normalise_path(str(file_path)) not in known_paths:
                report.orphaned_paths.append(file_path)

    return report


# ─── Dead root detection ──────────────────────────────────────────────────────

def find_dead_roots(db: Rekordbox6Database) -> DeadRootReport:
    """
    Group all local track paths in the DB by their volume root, then check
    which roots exist on disk. Dead roots are drives that are disconnected
    or have been renamed/remounted.

    On macOS paths like /Volumes/DRIVE/folder/..., the root is /Volumes/DRIVE.
    For other paths, uses the first two components.
    """
    from collections import defaultdict
    root_counts: dict[str, int] = defaultdict(int)

    for track in db.get_content().all():
        path_str = track.FolderPath
        if not path_str or _is_streaming(path_str):
            continue
        parts = Path(path_str).parts
        # macOS: /Volumes/DRIVE_NAME/...
        if len(parts) >= 3 and parts[1] == "Volumes":
            prefix = "/" + parts[1] + "/" + parts[2]
        elif len(parts) >= 2:
            prefix = parts[0] + parts[1]
        else:
            prefix = path_str
        root_counts[prefix] += 1

    report = DeadRootReport()
    for prefix, count in root_counts.items():
        if Path(prefix).exists():
            report.live_roots[prefix] = count
        else:
            report.dead_roots[prefix] = count
    return report


# ─── Physical library scan ────────────────────────────────────────────────────

def scan_physical_library(
    roots: "list[Path] | Path",
    *,
    collect_all_files: bool = True,
) -> PhysicalScanReport:
    """
    Walk one or more music roots and produce a complete inventory of every
    audio file found on disk.  This is independent of the database.

    Parameters
    ----------
    roots : Path | list[Path]
        One or more directories to scan.
    collect_all_files : bool
        If True (default), populate PhysicalScanReport.all_files with a dict
        per file: {path, size, ext, trash_adjacent}.  For 500 K-track libraries
        this is ~100 MB in memory — pass False for summary-only mode.
    """
    if isinstance(roots, Path):
        roots = [roots]

    report = PhysicalScanReport(roots_scanned=[str(r) for r in roots])

    for root in roots:
        if not root.is_dir():
            log.warning("Physical scan root not found, skipping: %s", root)
            continue

        for dirpath, dirnames, filenames in os.walk(root):
            # Prune skip dirs in-place
            dirnames[:] = [
                d for d in dirnames
                if d not in SKIP_DIRS
                and not any(d.startswith(px) for px in SKIP_PREFIXES)
            ]

            # Check whether this directory is trash-adjacent
            path_parts = Path(dirpath).parts
            in_trash = any(_is_trash_folder(part) for part in path_parts)

            for filename in filenames:
                if any(filename.startswith(px) for px in SKIP_PREFIXES):
                    continue
                fp = Path(dirpath) / filename
                ext = fp.suffix.lower()
                if ext not in AUDIO_EXTENSIONS:
                    continue

                try:
                    size = fp.stat().st_size
                except OSError:
                    size = 0

                ext_upper = fp.suffix.upper().lstrip(".") or "UNKNOWN"
                report.total_files += 1
                report.total_size_bytes += size
                report.by_extension[ext_upper] = report.by_extension.get(ext_upper, 0) + 1
                report.by_extension_size[ext_upper] = (
                    report.by_extension_size.get(ext_upper, 0) + size
                )

                if in_trash:
                    report.trash_adjacent_count += 1
                    report.trash_adjacent_paths.append(str(fp))

                if collect_all_files:
                    report.all_files.append({
                        "path":           str(fp),
                        "size":           size,
                        "ext":            ext_upper,
                        "trash_adjacent": in_trash,
                    })

    return report


def write_physical_scan_report(
    report: PhysicalScanReport,
    output_path: Path,
) -> Path:
    """
    Serialise a PhysicalScanReport to JSON at output_path.

    The JSON includes all_files (the full file list) if it was collected.
    Other tools (Organizer, Novelty Scanner, Duplicate Detector) can read
    this file to skip their own filesystem walk.

    Returns the output path on success.
    """
    payload = {
        "roots_scanned":         report.roots_scanned,
        "total_files":           report.total_files,
        "total_size_bytes":      report.total_size_bytes,
        "total_size_human":      report.total_size_human,
        "by_extension":          report.by_extension,
        "by_extension_size":     report.by_extension_size,
        "trash_adjacent_count":  report.trash_adjacent_count,
        "trash_adjacent_paths":  report.trash_adjacent_paths,
        "all_files":             report.all_files,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    log.info("Physical scan report written: %s  (%d files)", output_path, report.total_files)
    return output_path


# ─── Full audit ───────────────────────────────────────────────────────────────

def full_audit(
    db: Rekordbox6Database,
    root: Path | None = None,
    *,
    collect_all_files: bool = True,
) -> AuditReport:
    """
    Run all audit passes (database + filesystem) and return a combined AuditReport.

    Parameters
    ----------
    db : Rekordbox6Database
        Open database (read_db is sufficient).
    root : Path, optional
        Music root for the orphan scan and physical library inventory.
        Defaults to MUSIC_ROOT from config.
        Both scans are skipped if the root does not exist (drive not mounted).
    collect_all_files : bool
        If True (default), PhysicalScanReport.all_files is populated with a
        dict per file.  Pass False for large libraries where only summary
        statistics are needed.
    """
    log.info("Running library snapshot...")
    snap = snapshot(db)

    log.info("Validating file paths...")
    paths = validate_paths(db)

    log.info("Detecting dead drive roots...")
    dead_roots = find_dead_roots(db)

    scan_root = root if root is not None else MUSIC_ROOT
    physical: PhysicalScanReport | None = None

    if scan_root.exists():
        # ── Single combined filesystem walk ───────────────────────────────────
        # We walk the root once and simultaneously:
        #   1. Collect the full physical inventory (PhysicalScanReport)
        #   2. Identify orphans (files not referenced in the DB)
        # This avoids walking the same potentially-huge tree twice.
        log.info("Running combined physical scan + orphan detection under %s...", scan_root)

        # Build DB known-path set (normalised for case-insensitivity)
        known_paths: set[str] = set()
        for track in db.get_content().all():
            if track.FolderPath and not _is_streaming(track.FolderPath):
                known_paths.add(_normalise_path(track.FolderPath))

        physical = PhysicalScanReport(roots_scanned=[str(scan_root)])
        orphans  = OrphanReport()

        for dirpath, dirnames, filenames in os.walk(scan_root):
            dirnames[:] = [
                d for d in dirnames
                if d not in SKIP_DIRS
                and not any(d.startswith(px) for px in SKIP_PREFIXES)
            ]
            path_parts = Path(dirpath).parts
            in_trash   = any(_is_trash_folder(part) for part in path_parts)

            for filename in filenames:
                if any(filename.startswith(px) for px in SKIP_PREFIXES):
                    continue
                fp  = Path(dirpath) / filename
                ext = fp.suffix.lower()
                if ext not in AUDIO_EXTENSIONS:
                    continue

                try:
                    size = fp.stat().st_size
                except OSError:
                    size = 0

                ext_upper = fp.suffix.upper().lstrip(".") or "UNKNOWN"

                # ── Physical inventory ────────────────────────────────────────
                physical.total_files      += 1
                physical.total_size_bytes += size
                physical.by_extension[ext_upper] = (
                    physical.by_extension.get(ext_upper, 0) + 1
                )
                physical.by_extension_size[ext_upper] = (
                    physical.by_extension_size.get(ext_upper, 0) + size
                )
                if in_trash:
                    physical.trash_adjacent_count += 1
                    physical.trash_adjacent_paths.append(str(fp))
                if collect_all_files:
                    physical.all_files.append({
                        "path":           str(fp),
                        "size":           size,
                        "ext":            ext_upper,
                        "trash_adjacent": in_trash,
                    })

                # ── Orphan detection ──────────────────────────────────────────
                orphans.total_scanned += 1
                if _normalise_path(str(fp)) not in known_paths:
                    orphans.orphaned_paths.append(fp)

        log.info(
            "Filesystem scan complete: %d files total, %d orphans, %d in trash-adjacent folders",
            physical.total_files, orphans.orphan_count, physical.trash_adjacent_count,
        )
    else:
        log.warning("Music root not found, skipping filesystem scan: %s", scan_root)
        orphans = OrphanReport()

    return AuditReport(
        snapshot=snap,
        paths=paths,
        orphans=orphans,
        dead_roots=dead_roots,
        physical=physical,
    )


# ─── Smoke test ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    sys.path.insert(0, ".")

    from db_connection import read_db
    from config import DJMT_DB, MUSIC_ROOT

    print("Opening DB (read-only)...")
    with read_db(DJMT_DB) as db:

        # ── Snapshot ──
        print("\n--- Snapshot ---")
        snap = snapshot(db)
        print(f"  Tracks          : {snap.total_tracks}")
        print(f"  Playlists       : {snap.total_playlists}")
        print(f"  Playlist links  : {snap.total_playlist_links}")
        print(f"  Cue points      : {snap.total_cue_points}")
        print(f"  Artists         : {snap.total_artists}")
        print(f"  Albums          : {snap.total_albums}")
        print(f"  File types      : {snap.by_file_type}")
        print(f"  BPM coverage    : {snap.tracks_with_bpm} ({snap.coverage_pct(snap.tracks_with_bpm)})")
        print(f"  Key coverage    : {snap.tracks_with_key} ({snap.coverage_pct(snap.tracks_with_key)})")

        # ── Path validation ──
        print("\n--- Path validation ---")
        paths = validate_paths(db)
        print(f"  Checked         : {paths.total_checked}")
        print(f"  Found           : {paths.found}")
        print(f"  Streaming       : {paths.streaming_only}")
        print(f"  Missing         : {paths.missing_count}")
        if paths.missing:
            for cid, fp in paths.missing[:5]:
                print(f"    ContentID={cid}: {fp}")
            if paths.missing_count > 5:
                print(f"    ... and {paths.missing_count - 5} more")

        # ── Orphan scan (small subdirectory only for speed) ──
        print("\n--- Orphan scan (Kerri Chandler only) ---")
        test_root = MUSIC_ROOT / "Kerri Chandler"
        orphans = find_orphans(db, test_root)
        print(f"  Scanned         : {orphans.total_scanned}")
        print(f"  Orphaned        : {orphans.orphan_count}")
        for p in orphans.orphaned_paths[:5]:
            print(f"    {p.name}")

        # ── Full summary ──
        print("\n--- Full audit summary ---")
        report = AuditReport(snapshot=snap, paths=paths, orphans=orphans)
        print(report.summary())
