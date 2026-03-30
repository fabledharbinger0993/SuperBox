"""
SuperBox / pruner.py

Loads a duplicate_report.csv, enriches entries with live file metadata,
and executes confirmed prune operations:

  1. Removes selected tracks from DjmdContent (with DB backup via write_db)
  2. Moves selected files to ~/Trash/SuperBox_Pruned_[timestamp]/
     — NOT a permanent delete. The folder stays in Trash until the user
       empties it on their own schedule.

Called from app.py. Never called directly by the user.

Public interface:
  load_report(csv_path, db=None) -> list[DupeGroup]
  prune_files(file_paths, db, log=None) -> dict
"""

import csv
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

# ── Quality ranking ───────────────────────────────────────────────────────────
# Higher tier = higher quality. Used to re-rank within each duplicate group.

FORMAT_TIER: dict[str, int] = {
    ".aiff": 6, ".aif": 6,
    ".wav":  5,
    ".flac": 4,
    ".m4a":  3,
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

    @property
    def quality_score(self) -> tuple:
        """Higher = better. Used to sort within a group."""
        return (
            self.format_tier,
            self.file_size_mb,
            RARP_SCORE.get(self.rank, 0),
        )


@dataclass
class DupeGroup:
    group_id: str
    entries:  list[DupeEntry] = field(default_factory=list)

    @property
    def keep(self) -> Optional[DupeEntry]:
        return next((e for e in self.entries if e.action == "KEEP"), None)

    @property
    def remove_candidates(self) -> list[DupeEntry]:
        return [e for e in self.entries if e.action == "REVIEW_REMOVE"]


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

            gid = entry.group_id
            if gid not in groups:
                groups[gid] = DupeGroup(gid)
            groups[gid].entries.append(entry)

    # Re-rank: sort each group by quality descending, reassign KEEP to #1
    for group in groups.values():
        group.entries.sort(key=lambda e: e.quality_score, reverse=True)
        for i, entry in enumerate(group.entries):
            entry.action = "KEEP" if i == 0 else "REVIEW_REMOVE"

    # Drop groups with only one entry (nothing to prune)
    return [g for g in groups.values() if len(g.entries) > 1]


# ── Public: prune files ───────────────────────────────────────────────────────

def prune_files(
    file_paths: list[str],
    db,
    log=None,
) -> dict:
    """
    Remove file_paths from DjmdContent and move them to a timestamped
    recovery folder inside ~/Trash/.

    Order of operations:
      1. Create recovery folder in Trash.
      2. Remove DB entries (with the backup already created by write_db).
      3. Move files to recovery folder.

    Returns a summary dict:
      { db_removed, files_moved, skipped, errors, trash_dir }
    """

    def emit(msg: str) -> None:
        if log:
            log(msg)

    stamp     = datetime.now().strftime("%Y%m%d_%H%M%S")
    trash_dir = Path.home() / ".Trash" / f"SuperBox_Pruned_{stamp}"
    trash_dir.mkdir(parents=True, exist_ok=True)
    emit(f"Recovery folder → {trash_dir}")
    emit("")

    db_removed = 0
    files_moved = 0
    skipped    = 0
    errors: list[str] = []

    # ── Step 1: Remove from database ──────────────────────────────────────
    emit("  Removing from RekordBox database…")
    for path in file_paths:
        try:
            rows = db.get_content(FolderPath=path)
            if rows:
                for row in rows:
                    db.session.delete(row)
                db_removed += 1
                emit(f"    DB ✓  {Path(path).name}")
            else:
                emit(f"    DB —  {Path(path).name}  (not in database — file only)")
        except Exception as exc:
            msg = f"DB error for {Path(path).name}: {exc}"
            errors.append(msg)
            emit(f"    DB ✗  {msg}")

    emit("")

    # ── Step 2: Move files to recovery folder ──────────────────────────────
    emit("  Moving files to recovery folder…")
    for path in file_paths:
        p = Path(path)
        if not p.exists():
            emit(f"    Skip — not found on disk: {p.name}")
            skipped += 1
            continue
        try:
            dest = trash_dir / p.name
            # Handle name collisions within the recovery folder
            if dest.exists():
                dest = trash_dir / f"{p.stem}__{p.parent.name}{p.suffix}"
            shutil.move(str(p), str(dest))
            files_moved += 1
            emit(f"    Moved ✓  {p.name}")
        except Exception as exc:
            msg = f"Could not move {p.name}: {exc}"
            errors.append(msg)
            emit(f"    Move ✗  {msg}")

    emit("")
    emit("═══ PRUNE SUMMARY ═══")
    emit(f"  Database entries removed : {db_removed}")
    emit(f"  Files moved to recovery  : {files_moved}")
    if skipped:
        emit(f"  Skipped (not on disk)    : {skipped}")
    if errors:
        emit(f"  Errors                   : {len(errors)}")
        for err in errors:
            emit(f"    ⚠  {err}")
    emit(f"  Recovery folder          : {trash_dir}")
    emit("═════════════════════")

    return {
        "db_removed":  db_removed,
        "files_moved": files_moved,
        "skipped":     skipped,
        "errors":      errors,
        "trash_dir":   str(trash_dir),
    }
