"""
rekordbox-toolkit / importer.py

Writes tracks from the filesystem into the Rekordbox database.

Orchestrates: scanner → key_mapper → db_connection → DjmdContent rows.

Flow:
    import_directory(root, db) → ImportReport

Each track goes through:
  1. Metadata extraction (scanner.extract_metadata)
  2. Artist get-or-create (DjmdArtist)
  3. Key resolution (key_mapper.resolve_key_id)
  4. Content row creation (db.add_content)
  5. Batch commit every BATCH_SIZE tracks

Nothing is committed until a full batch is ready. On exception, the batch
is rolled back and the ImportReport counts are corrected — no silent partial imports.
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path

from pyrekordbox import Rekordbox6Database

from config import BATCH_SIZE
from key_mapper import clear_cache as clear_key_cache, resolve_key_id
from scanner import TrackInfo, scan_directory

log = logging.getLogger(__name__)

# ─── Result types ─────────────────────────────────────────────────────────────

@dataclass
class TrackImportResult:
    path: Path
    success: bool = False
    skipped: bool = False
    content_id: str | None = None
    error: str | None = None


@dataclass
class ImportReport:
    imported: int = 0
    skipped: int = 0
    failed: int = 0
    results: list[TrackImportResult] = field(default_factory=list)

    @property
    def total_attempted(self) -> int:
        return self.imported + self.skipped + self.failed

    def summary(self) -> str:
        lines = [
            "═══ IMPORT REPORT ═══",
            f"  Imported : {self.imported}",
            f"  Skipped  : {self.skipped}  (already in DB)",
            f"  Failed   : {self.failed}",
            f"  Total    : {self.total_attempted}",
        ]
        if self.failed > 0:
            lines.append("\n  Failed files:")
            for r in self.results:
                if not r.success and not r.skipped:
                    lines.append(f"    {r.path.name}: {r.error}")
        lines.append("═════════════════════")
        return "\n".join(lines)


# ─── Artist resolution ────────────────────────────────────────────────────────

_artist_cache: dict[str, str] = {}


def _get_or_create_artist(name: str, db: Rekordbox6Database) -> str | None:
    """Return DjmdArtist.ID for name, creating a row if absent. Never raises."""
    if not name or not name.strip():
        return None
    name = name.strip()
    if name in _artist_cache:
        return _artist_cache[name]
    existing = db.get_artist(Name=name).one_or_none()
    if existing is not None:
        _artist_cache[name] = str(existing.ID)
        return str(existing.ID)
    try:
        artist = db.add_artist(name=name)
        _artist_cache[name] = str(artist.ID)
        log.debug("Created artist: %r", name)
        return str(artist.ID)
    except ValueError:
        # Race condition — re-fetch
        existing = db.get_artist(Name=name).one_or_none()
        if existing:
            _artist_cache[name] = str(existing.ID)
            return str(existing.ID)
        log.error("Failed to get or create artist %r", name)
        return None


def clear_caches() -> None:
    """Clear all session-level caches. Useful between test runs."""
    _artist_cache.clear()
    clear_key_cache()


# ─── Single track import ──────────────────────────────────────────────────────

def _import_track(track: TrackInfo, db: Rekordbox6Database) -> TrackImportResult:
    """
    Write a single TrackInfo to the database.
    Does not commit — caller owns the batch commit.
    """
    result = TrackImportResult(path=track.path)

    if not track.is_valid:
        result.error = "invalid or unreadable file"
        return result

    # ── Build kwargs for add_content ──
    kwargs: dict = {}

    if track.title:
        kwargs["Title"] = track.title

    if track.artist:
        artist_id = _get_or_create_artist(track.artist, db)
        if artist_id:
            kwargs["ArtistID"] = artist_id

    if track.bpm is not None:
        kwargs["BPM"] = int(round(track.bpm * 100))  # DB stores BPM × 100

    if track.key:
        key_id = resolve_key_id(track.key, db)
        if key_id:
            kwargs["KeyID"] = key_id

    if track.duration_seconds is not None:
        kwargs["Length"] = int(track.duration_seconds)

    if track.bitrate is not None:
        kwargs["BitRate"] = track.bitrate

    if track.sample_rate is not None:
        kwargs["SampleRate"] = track.sample_rate

    if track.bit_depth is not None:
        kwargs["BitDepth"] = track.bit_depth

    if track.year is not None:
        kwargs["ReleaseYear"] = track.year

    if track.track_number is not None:
        kwargs["TrackNo"] = track.track_number

    # ── Handle .aif extension ──
    # pyrekordbox's FileType enum only has AIFF (for .aiff), not AIF.
    # add_content derives FileType from path.suffix — ".aif" would raise ValueError.
    # Solution: pass a Path with .aiff suffix so FileType resolves correctly,
    # then override FolderPath in kwargs to preserve the real on-disk path.
    #
    # FRAGILITY NOTE: This assumes that add_content applies kwargs AFTER it
    # auto-sets FolderPath from the path argument, so our kwarg wins. This is
    # an internals assumption about the installed pyrekordbox version. If it
    # ever breaks silently (DB gets .aiff paths for .aif files), the fix is to
    # call setattr(content_row, 'FolderPath', str(actual_path)) after add_content
    # returns. Verify by running: SELECT FolderPath FROM DjmdContent WHERE
    # FolderPath LIKE '%.aif' LIMIT 5; — all results should end in .aif not .aiff.
    actual_path = track.path
    import_path = actual_path
    if actual_path.suffix.lower() == ".aif":
        import_path = actual_path.with_suffix(".aiff")
        kwargs["FolderPath"] = str(actual_path)

    # ── Write to DB ──
    try:
        content_row = db.add_content(import_path, **kwargs)
        result.success = True
        result.content_id = str(content_row.ID)
        log.debug("Imported: %s (ID=%s)", actual_path.name, content_row.ID)
    except ValueError as e:
        err = str(e)
        if "already exists" in err:
            result.skipped = True
            log.debug("Already in DB: %s", actual_path.name)
        else:
            result.error = err
            log.error("Import failed for %s: %s", actual_path.name, err)
    except Exception as e:
        result.error = str(e)
        log.error("Import failed for %s: %s", actual_path.name, e)

    return result


# ─── Batch importer ───────────────────────────────────────────────────────────

def import_directory(
    root: Path,
    db: Rekordbox6Database,
    *,
    dry_run: bool = False,
) -> ImportReport:
    """
    Import all audio files under root into the database.

    Parameters
    ----------
    root : Path
        Directory to scan. Inherits all scanner skip rules.
    db : Rekordbox6Database
        Open write-session database. Use write_db() context. If dry_run=True
        a read_db() session is sufficient — no writes will occur.
    dry_run : bool
        Scan and report without writing. Useful for metadata preview.
    """
    report = ImportReport()
    batch_count = 0
    batch_start_index = 0

    for track in scan_directory(root):
        if not track.is_valid:
            r = TrackImportResult(path=track.path, error="invalid or unreadable file")
            report.results.append(r)
            report.failed += 1
            continue

        if dry_run:
            log.info("[DRY RUN] %s | BPM:%s | KEY:%s | Artist:%s",
                     track.path.name, track.bpm or "?",
                     track.key or "?", track.artist or "?")
            report.results.append(TrackImportResult(path=track.path, success=True))
            report.imported += 1
            continue

        result = _import_track(track, db)
        report.results.append(result)

        if result.success:
            report.imported += 1
            batch_count += 1
        elif result.skipped:
            report.skipped += 1
        else:
            report.failed += 1

        if batch_count >= BATCH_SIZE:
            try:
                db.commit()
                log.info("Committed batch of %d tracks", batch_count)
                batch_count = 0
                batch_start_index = len(report.results)
            except Exception:
                log.exception("Batch commit failed — rolling back")
                db.rollback()
                # Correct the report: mark all results in this batch as failed
                for r in report.results[batch_start_index:]:
                    if r.success:
                        r.success = False
                        r.error = "rolled back with batch"
                        report.imported -= 1
                        report.failed += 1
                raise

    if not dry_run and batch_count > 0:
        try:
            db.commit()
            log.info("Final commit: %d tracks", batch_count)
        except Exception:
            log.exception("Final commit failed — rolling back")
            db.rollback()
            raise

    return report


# ─── Smoke test ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    sys.path.insert(0, ".")

    from config import DJMT_DB
    from db_connection import read_db

    test_root = Path("/Volumes/DJMT/DJMT PRIMARY/Kerri Chandler")

    print("=== DRY RUN (no writes) ===")
    with read_db(DJMT_DB) as db:
        report = import_directory(test_root, db, dry_run=True)
    print(report.summary())

    print("\n=== METADATA PREVIEW ===")
    for track in scan_directory(test_root):
        bpm_db = int(round(track.bpm * 100)) if track.bpm else "None"
        dur_db = int(track.duration_seconds) if track.duration_seconds else "None"
        print(f"  {track.path.name}")
        print(f"    Title    : {track.title}")
        print(f"    Artist   : {track.artist}")
        print(f"    BPM      : {track.bpm} → DB: {bpm_db}")
        print(f"    Key      : {track.key}")
        print(f"    Duration : {track.duration_seconds:.0f}s → DB: {dur_db}" if track.duration_seconds else "    Duration : None")
        print(f"    BitRate  : {track.bitrate}  SampleRate: {track.sample_rate}")

    print("\nSmoke test complete — no writes performed.")
