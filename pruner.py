"""
RekitBox / pruner.py

Loads a duplicate_report.csv, enriches entries with live file metadata,
and executes confirmed prune operations:

  1. Removes selected tracks from DjmdContent (with DB backup via write_db)
  2. Moves selected files to ~/Trash/RekitBox_Pruned_[timestamp]/
     — NOT a permanent delete. The folder stays in Trash until the user
       empties it on their own schedule.

Called from app.py. Never called directly by the user.

Trash rescue gate:
  Before any prune can execute, trash_rescue_preflight(csv_path) must be
  called and return an empty issues list. If the companion rescue report
  has unresolved items, or the CSV contains keep_in_trash=YES rows, the
  preflight raises TrashRescueRequired. The prune will not proceed until
  the user has reviewed and cleared those items. RekitBox does not offer
  an automated rescue step — the user must act manually.

Public interface:
  trash_rescue_preflight(csv_path) -> None   (raises TrashRescueRequired)
  load_report(csv_path, db=None) -> list[DupeGroup]
  prune_files(file_paths, db, log=None) -> dict
"""

import csv
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

# ── Trash rescue gate ────────────────────────────────────────────────────────

class TrashRescueRequired(RuntimeError):
    """
    Raised by trash_rescue_preflight() when unresolved rescue items are found.
    The prune must not proceed until the user has acted on the rescue report.
    """
    def __init__(self, message: str, issues: list[str]):
        super().__init__(message)
        self.issues = issues


def trash_rescue_preflight(csv_path: Path) -> None:
    """
    Check whether it is safe to proceed with a prune run against csv_path.

    Raises TrashRescueRequired if either:
      1. A companion rescue report (.txt, same stem prefix) exists and
         contains file paths — meaning unique-in-trash tracks were found
         during the last scan and have not been cleared.
      2. The CSV itself contains rows with keep_in_trash=YES — meaning at
         least one duplicate group's best surviving copy is inside a trash
         folder. Pruning the REVIEW_REMOVE copies for that group while the
         KEEP is still in trash would leave no safe copy anywhere.

    RekitBox does not offer an automated rescue step. The user must manually
    move the flagged files before the prune can run.

    Parameters
    ----------
    csv_path : Path
        Path to the duplicate_report CSV that will be fed to load_report().
    """
    issues: list[str] = []

    # ── Check 1: companion rescue report ─────────────────────────────────────
    # The rescue report is written alongside the CSV with a parallel name:
    #   duplicate_report_20260403_013019.csv
    #   trash_rescue_report_20260403_013019.txt
    stem = csv_path.stem
    rescue_stem = stem.replace("duplicate_report", "trash_rescue_report")
    if rescue_stem == stem:
        rescue_stem = f"trash_rescue_{stem}"
    rescue_path = csv_path.with_name(rescue_stem).with_suffix(".txt")

    if rescue_path.exists():
        # Scan the rescue report for actual file paths (lines starting with /)
        with open(rescue_path, encoding="utf-8", errors="replace") as f:
            rescue_paths = [
                ln.strip() for ln in f
                if ln.strip().startswith("/") or ln.strip().startswith("\\")
            ]
        if rescue_paths:
            issues.append(
                f"Rescue report lists {len(rescue_paths)} track(s) that need manual "
                f"attention before pruning: {rescue_path}"
            )

    # ── Check 2: keep_in_trash rows in the CSV ────────────────────────────────
    try:
        with open(csv_path, newline="", encoding="utf-8", errors="replace") as f:
            reader = csv.DictReader(f)
            if "keep_in_trash" in (reader.fieldnames or []):
                trapped = [
                    row["file_path"]
                    for row in reader
                    if row.get("keep_in_trash", "").strip().upper() == "YES"
                    and row.get("action", "").strip() == "KEEP"
                ]
                if trapped:
                    issues.append(
                        f"{len(trapped)} group(s) have their best copy inside a trash folder "
                        f"(keep_in_trash=YES). Move those files to a safe location before pruning."
                    )
    except Exception:
        pass  # If the CSV can't be read here, load_report will surface the error

    if issues:
        lines = [
            "╔══════════════════════════════════════════════════════════════════╗",
            "║  !!! PRUNE BLOCKED — TRASH RESCUE REQUIRED !!!                  ║",
            "║                                                                  ║",
            "║  Unique or possibly-unique tracks were found inside trash or     ║",
            "║  trash-adjacent folders. RekitBox does not offer an automated   ║",
            "║  rescue step. You must review and act on these manually before  ║",
            "║  any pruning can proceed.                                        ║",
            "╠══════════════════════════════════════════════════════════════════╣",
        ]
        for issue in issues:
            # Word-wrap each issue to ~66 chars
            words = issue.split()
            line = ""
            for word in words:
                if len(line) + len(word) + 1 > 64:
                    lines.append(f"║  {line:<66}║")
                    line = word
                else:
                    line = (line + " " + word).strip()
            if line:
                lines.append(f"║  {line:<66}║")
        lines.append("╚══════════════════════════════════════════════════════════════════╝")
        raise TrashRescueRequired("\n".join(lines), issues)


# ── Quality ranking ───────────────────────────────────────────────────────────
# Higher tier = higher quality. Used to re-rank within each duplicate group.

FORMAT_TIER: dict[str, int] = {
    ".aiff": 6, ".aif": 6, ".aifc": 6,
    ".wav":  5,
    ".flac": 4,
    ".m4a":  3, ".m4p": 3, ".mp4": 3, ".m4v": 3,
    ".mp3":  2,
    ".ogg":  1, ".opus": 1,
}

RARP_SCORE: dict[str, int] = {
    "PN":  3,   # Pioneer Numbered
    "MIK": 2,   # Mixed In Key tagged
    "RAW": 1,   # Neither
}


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class DupeEntry:
    group_id:      str
    action:        str            # KEEP | REVIEW_REMOVE (re-assigned on load)
    rank:          str            # PN | MIK | RAW
    file_path:     str
    file_size_mb:  float
    bpm:           Optional[str]
    key:           Optional[str]
    filename:      str
    # enriched after load
    format_ext:    str  = ""
    format_tier:   int  = 0
    exists_on_disk:bool = True
    in_db:         bool = False
    tag_completeness: int = 0

    @property
    def quality_score(self) -> tuple:
        """Higher = better. Used to sort within a group."""
        return (
            self.format_tier,
            self.file_size_mb,
            RARP_SCORE.get(self.rank, 0),
            self.tag_completeness,  # higher = more complete tags
        )


@dataclass
class DupeGroup:
    group_id:     str
    entries:      list[DupeEntry] = field(default_factory=list)
    keep_in_trash: bool = False  # True when the KEEP file lives in a trash folder

    @property
    def keep(self) -> Optional[DupeEntry]:
        return next((e for e in self.entries if e.action == "KEEP"), None)

    @property
    def remove_candidates(self) -> list[DupeEntry]:
        # Safety lock: if the best surviving copy is in a trash folder, pruning
        # the REVIEW_REMOVE files would leave no safe copy once trash is cleared.
        # Return nothing so the pruner can never act on this group regardless of
        # how it was called or whether preflight was skipped.
        if self.keep_in_trash:
            return []
        return [e for e in self.entries if e.action == "REVIEW_REMOVE"]


# ── Tag completeness helper ───────────────────────────────────────────────────

def _count_tags(path: Path) -> int:
    """
    Count how many meaningful tags the file has.
    Used to prefer well-tagged copies during deduplication.
    Checks: title, artist, album, BPM, key, year, genre.
    Returns 0–7.
    """
    if not path.exists():
        return 0
    try:
        from mutagen import File as MutagenFile
        audio = MutagenFile(str(path), easy=False)
        if audio is None or audio.tags is None:
            return 0
        tags = audio.tags
        tag_type = type(tags).__name__
        is_vorbis = "VCFLACDict" in tag_type or "VComment" in tag_type
        is_mp4    = "MP4Tags" in tag_type or "MP4" in tag_type
        score = 0
        def _has(id3_key, vorbis_key, mp4_key=None):
            nonlocal score
            try:
                if is_vorbis:
                    v = tags.get(vorbis_key.lower())
                    if v and str(v[0] if isinstance(v, list) else v).strip():
                        score += 1
                elif is_mp4 and mp4_key:
                    v = tags.get(mp4_key)
                    if v and str(v[0] if isinstance(v, list) else v).strip():
                        score += 1
                else:
                    f = tags.get(id3_key)
                    if f and str(f).strip():
                        score += 1
            except Exception:
                pass
        _has("TIT2", "title",       "©nam")
        _has("TPE1", "artist",      "©ART")
        _has("TALB", "album",       "©alb")
        _has("TBPM", "bpm",         "tmpo")
        _has("TKEY", "initialkey",  "----:com.apple.iTunes:initialkey")
        _has("TDRC", "date",        "©day")
        _has("TCON", "genre",       "©gen")
        return score
    except Exception:
        return 0


# ── Public: load report ───────────────────────────────────────────────────────

def load_report(csv_path: Path, db=None) -> list[DupeGroup]:
    """
    Read a duplicate_report.csv, enrich each entry with live disk and DB data,
    re-rank within each group by quality, and return the structured groups.

    db is an optional read-only database connection used to flag which files
    are currently referenced in DjmdContent.
    """
    # Build a lookup of paths that exist in the database
    db_paths: set[str] = set()
    if db is not None:
        try:
            db_paths = {row.FolderPath for row in db.get_content()}
        except Exception:
            pass  # DB unavailable — just skip in_db flagging

    groups: dict[str, DupeGroup] = {}

    # Track which group IDs are flagged keep_in_trash from the CSV column.
    # Collected separately so we can set it after all entries are loaded.
    trash_flagged_groups: set[str] = set()

    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            fp = row.get("file_path", "").strip()
            if not fp:
                continue
            entry = DupeEntry(
                group_id     = row.get("group_id", "").strip(),
                action       = row.get("action", "").strip(),
                rank         = row.get("rank", "RAW").strip(),
                file_path    = fp,
                file_size_mb = float(row.get("file_size_mb") or 0),
                bpm          = row.get("bpm") or None,
                key          = row.get("key") or None,
                filename     = row.get("filename", Path(fp).name),
            )
            p = Path(fp)
            entry.format_ext     = p.suffix.lower()
            entry.format_tier    = FORMAT_TIER.get(entry.format_ext, 0)
            entry.exists_on_disk = p.exists()
            entry.in_db          = fp in db_paths
            # Compute tag completeness score from live file tags
            entry.tag_completeness = _count_tags(p)

            gid = entry.group_id
            if gid not in groups:
                groups[gid] = DupeGroup(gid)
            groups[gid].entries.append(entry)

            if row.get("keep_in_trash", "").strip().upper() == "YES":
                trash_flagged_groups.add(gid)

    # Re-rank: sort each group by quality descending, reassign KEEP to #1
    for group in groups.values():
        group.entries.sort(key=lambda e: e.quality_score, reverse=True)
        for i, entry in enumerate(group.entries):
            entry.action = "KEEP" if i == 0 else "REVIEW_REMOVE"
        # Apply trash lock — locked regardless of re-ranking outcome
        if group.group_id in trash_flagged_groups:
            group.keep_in_trash = True

    # Drop groups with only one entry (nothing to prune)
    return [g for g in groups.values() if len(g.entries) > 1]


# ── Public: prune files ───────────────────────────────────────────────────────

def _rethread_playlists(remove_content, keeper_path: str, db, emit) -> int:
    """
    Before a duplicate content row is deleted, re-point any DjmdSongPlaylist
    entries that reference it to the keeper track instead.

    For each playlist that contains the duplicate:
      - If the keeper is NOT already in that playlist → update ContentID in place.
      - If the keeper IS already in that playlist → delete the duplicate's entry
        (the keeper already represents the track there; adding another would
        create a redundant entry).

    Returns the number of playlist slots re-threaded (updated, not dropped).
    """
    keeper_rows = db.get_content(FolderPath=keeper_path)
    if not keeper_rows:
        emit(f"      ⚠  Keeper not in DB ({Path(keeper_path).name}) — playlist entries left intact")
        return 0

    keeper_content = keeper_rows[0]
    song_rows = db.get_playlist_songs(ContentID=remove_content.ID)
    if not song_rows:
        return 0

    rethreaded = 0
    for song_row in song_rows:
        playlist_id = song_row.PlaylistID
        # Check whether keeper is already in this playlist
        already_there = db.get_playlist_songs(
            ContentID=keeper_content.ID, PlaylistID=playlist_id
        )
        if already_there:
            # Keeper already present — just drop the duplicate's slot
            db.session.delete(song_row)
        else:
            # Re-point the slot to the keeper
            song_row.ContentID = keeper_content.ID
            rethreaded += 1

    return rethreaded


def prune_files(
    file_paths: list[str],
    db,
    log=None,
    permanent: bool = False,
    keeper_map: Optional[dict[str, str]] = None,
    should_cancel=None,
) -> dict:
    """
    Remove file_paths from DjmdContent and move them to a timestamped
    recovery folder inside ~/Trash/.

    keeper_map (optional): {remove_path → keeper_path}
      When provided, any DjmdSongPlaylist entries pointing at a duplicate are
      re-threaded to point at the keeper *before* the duplicate row is deleted.
      This preserves playlist membership — the track stays in every playlist
      it was in, now backed by the file that is being kept.

    Order of operations:
      1. Create recovery folder in Trash.
      2. Re-thread playlist references for each duplicate that has a keeper.
      3. Remove DB entries (with the backup already created by write_db).
      4. Move files to recovery folder.

    Returns a summary dict:
      { db_removed, files_moved, skipped, errors, trash_dir, playlists_rethreaded }
    """

    def emit(msg: str) -> None:
        if log:
            log(msg)

    def _cancel_requested() -> bool:
        if not should_cancel:
            return False
        try:
            return bool(should_cancel())
        except Exception:
            return False

    stamp     = datetime.now().strftime("%Y%m%d_%H%M%S")
    if permanent:
        trash_dir = None
        emit("⚠  Permanent delete mode — files will NOT be recoverable")
    else:
        trash_dir = Path.home() / ".Trash" / f"RekitBox_Pruned_{stamp}"
        trash_dir.mkdir(parents=True, exist_ok=True)
        emit(f"Recovery folder → {trash_dir}")
    emit("")

    db_removed = 0
    files_moved = 0
    skipped    = 0
    playlists_rethreaded = 0
    errors: list[str] = []

    # ── Step 1: Remove from database (with playlist re-threading) ─────────
    emit("  Removing from RekordBox database…")
    for path in file_paths:
        if _cancel_requested():
            emit("    ⚠  Cancel requested — rolling back pending database changes.")
            try:
                db.rollback()
            except Exception as exc:
                errors.append(f"Rollback failed after cancel request: {exc}")
            emit("  Prune cancelled before commit. No files were moved.")
            return {
                "db_removed":           0,
                "files_moved":          0,
                "skipped":              0,
                "errors":               errors,
                "trash_dir":            str(trash_dir) if trash_dir is not None else None,
                "playlists_rethreaded": 0,
                "cancelled":            True,
            }
        try:
            rows = db.get_content(FolderPath=path)
            if rows:
                for row in rows:
                    # Re-thread playlist slots before deleting the content row
                    keeper_path = (keeper_map or {}).get(path)
                    if keeper_path:
                        n = _rethread_playlists(row, keeper_path, db, emit)
                        if n:
                            playlists_rethreaded += n
                            emit(f"      ↪  {n} playlist slot(s) re-threaded to keeper")
                    db.session.delete(row)
                db_removed += 1
                emit(f"    DB ✓  {Path(path).name}")
            else:
                emit(f"    DB —  {Path(path).name}  (not in database — file only)")
        except Exception as exc:
            msg = f"DB error for {Path(path).name}: {exc}"
            errors.append(msg)
            emit(f"    DB ✗  {msg}")

    # Commit all session.delete() calls — session.close() does NOT commit.
    try:
        db.commit()
        emit(f"  Database commit OK ({db_removed} row(s) removed)")
    except Exception as exc:
        msg = f"Database commit failed — no files will be moved: {exc}"
        errors.append(msg)
        emit(f"  ✗ {msg}")
        return {
            "db_removed":           0,
            "files_moved":          0,
            "skipped":              skipped,
            "errors":               errors,
            "trash_dir":            str(trash_dir) if trash_dir is not None else None,
            "playlists_rethreaded": playlists_rethreaded,
            "cancelled":            False,
        }

    emit("")

    # ── Step 2: Move/delete files ──────────────────────────────────────────
    if _cancel_requested():
        emit("  ⚠  Cancel requested after database commit — finishing file operations to keep DB and filesystem in sync.")

    action_label = "Permanently deleting" if permanent else "Moving files to recovery folder"
    emit(f"  {action_label}…")
    for path in file_paths:
        p = Path(path)
        if not p.exists():
            emit(f"    Skip — not found on disk: {p.name}")
            skipped += 1
            continue
        try:
            if permanent:
                p.unlink()
                files_moved += 1
                emit(f"    Deleted ✓  {p.name}")
            else:
                dest = trash_dir / p.name
                # Handle name collisions within the recovery folder
                if dest.exists():
                    dest = trash_dir / f"{p.stem}__{p.parent.name}{p.suffix}"
                shutil.move(str(p), str(dest))
                files_moved += 1
                emit(f"    Moved ✓  {p.name}")
        except Exception as exc:
            msg = f"Could not {'delete' if permanent else 'move'} {p.name}: {exc}"
            errors.append(msg)
            emit(f"    {'Delete' if permanent else 'Move'} ✗  {msg}")

    emit("")
    emit("═══ PRUNE SUMMARY ═══")
    emit(f"  Database entries removed        : {db_removed}")
    if playlists_rethreaded:
        emit(f"  Playlist slots re-threaded     : {playlists_rethreaded}")
    emit(f"  Files {'permanently deleted' if permanent else 'moved to recovery'} : {files_moved}")
    if skipped:
        emit(f"  Skipped (not on disk)    : {skipped}")
    if errors:
        emit(f"  Errors                   : {len(errors)}")
        for err in errors:
            emit(f"    ⚠  {err}")
    emit(f"  Recovery folder          : {trash_dir}")
    emit("═════════════════════")

    return {
        "db_removed":           db_removed,
        "files_moved":          files_moved,
        "skipped":              skipped,
        "errors":               errors,
        "trash_dir":            str(trash_dir) if trash_dir is not None else None,
        "playlists_rethreaded": playlists_rethreaded,
        "cancelled":            False,
    }
