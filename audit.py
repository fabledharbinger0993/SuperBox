"""
rekordbox-toolkit / audit.py

Read-only database snapshot and filesystem validation.
No writes of any kind. Safe to run at any time, even with Rekordbox open.

Public interface:
    snapshot(db)           -> LibrarySnapshot
    validate_paths(db)     -> PathReport
    full_audit(db, root)   -> AuditReport

Typical usage:
    with read_db() as db:
        report = full_audit(db, MUSIC_ROOT)
        print(report.summary())
"""

import logging
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
class AuditReport:
    """Combined result of a full audit pass."""
    snapshot: LibrarySnapshot
    paths: PathReport
    orphans: OrphanReport
    dead_roots: DeadRootReport = field(default_factory=DeadRootReport)

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


# ─── Full audit ───────────────────────────────────────────────────────────────

def full_audit(
    db: Rekordbox6Database,
    root: Path | None = None,
    extra_roots: list[Path] | None = None,
) -> AuditReport:
    """
    Run all three audit passes and return a combined AuditReport.

    Parameters
    ----------
    db : Rekordbox6Database
        Open database (read_db is sufficient).
    root : Path, optional
        Primary music root for orphan detection. Defaults to MUSIC_ROOT from config.
    extra_roots : list[Path], optional
        Additional library roots to include in the orphan scan.
        All roots are scanned and their results merged.
    """
    log.info("Running library snapshot...")
    snap = snapshot(db)

    log.info("Validating file paths...")
    paths = validate_paths(db)

    # Build the combined list of all roots to scan
    all_roots: list[Path] = []
    primary = root if root is not None else MUSIC_ROOT
    all_roots.append(primary)
    for extra in (extra_roots or []):
        if extra not in all_roots:
            all_roots.append(extra)

    # Scan each root and merge OrphanReports
    merged_orphans = OrphanReport()
    for scan_root in all_roots:
        if scan_root.exists():
            log.info("Scanning for orphaned files under %s...", scan_root)
            o = find_orphans(db, scan_root)
            merged_orphans.orphaned_paths.extend(o.orphaned_paths)
            merged_orphans.total_scanned += o.total_scanned
        else:
            log.warning("Music root not found, skipping orphan scan: %s", scan_root)

    log.info("Detecting dead drive roots...")
    dead_roots = find_dead_roots(db)

    return AuditReport(snapshot=snap, paths=paths, orphans=merged_orphans, dead_roots=dead_roots)


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
