"""
rekordbox-toolkit / db_connection.py

Safe database connection wrapper. Enforces:
  - Timestamped backup before any write operation
  - Rekordbox process detection (refuses writes if RB is running)
  - Automatic rollback on unhandled exceptions
  - Context manager interface for clean open/close

Nothing in this toolkit writes to the database without going through open_db().
"""

import logging
import platform
import shutil
import subprocess
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Generator

from pyrekordbox import Rekordbox6Database

from config import BACKUP_DIR, DJMT_DB, LOCAL_DB

log = logging.getLogger(__name__)

# ─── Process detection ────────────────────────────────────────────────────────

REKORDBOX_PROCESS_NAMES = ("rekordbox", "Rekordbox")

_PLATFORM = platform.system()  # "Darwin", "Windows", or "Linux"

if _PLATFORM not in ("Darwin", "Windows"):
    log.warning(
        "rekordbox-toolkit is only tested on macOS and Windows. "
        "Rekordbox does not have a Linux release. Some features "
        "(process detection, path casing) may behave differently on Linux."
    )


def rekordbox_is_running() -> bool:
    """
    Return True if any Rekordbox process is currently active.

    Platform-aware:
      macOS   — pgrep -x rekordbox  (exact name match)
      Windows — tasklist /FI "IMAGENAME eq rekordbox.exe" /NH
      Linux   — pgrep -x rekordbox  (Rekordbox has no Linux version, but
                the check is harmless; always returns False in practice)
    """
    try:
        if _PLATFORM == "Windows":
            result = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq rekordbox.exe", "/NH"],
                capture_output=True,
                text=True,
            )
            # tasklist exits 0 even when no match; check stdout for the name
            return "rekordbox.exe" in result.stdout.lower()
        else:
            # macOS and Linux both have pgrep
            # -x matches the exact process name (not the full command line).
            # Do NOT combine with -f: -x -f together require the full command
            # string to equal the pattern exactly, which never matches in practice.
            result = subprocess.run(
                ["pgrep", "-x", "rekordbox"],
                capture_output=True,
                text=True,
            )
            return result.returncode == 0
    except FileNotFoundError:
        # Neither pgrep nor tasklist found — log and assume not running
        log.warning(
            "Could not check for running Rekordbox processes "
            "(pgrep/tasklist not found). Proceeding without process check."
        )
        return False


# ─── Backup ───────────────────────────────────────────────────────────────────

def _backup_db(db_path: Path) -> Path:
    """
    Copy db_path to BACKUP_DIR with a timestamp suffix.
    Creates BACKUP_DIR if it doesn't exist.
    Returns the path of the backup file.
    Raises RuntimeError if the source file doesn't exist.
    """
    if not db_path.exists():
        raise RuntimeError(f"Database not found at {db_path}")

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    stem = db_path.stem  # "master"
    backup_name = f"{stem}.backup_{timestamp}.db"
    backup_path = BACKUP_DIR / backup_name

    shutil.copy2(db_path, backup_path)
    log.info("Backup created: %s", backup_path)
    return backup_path


# ─── Context manager ──────────────────────────────────────────────────────────

@contextmanager
def open_db(
    db_path: Path | None = None,
    *,
    write: bool = False,
) -> Generator[Rekordbox6Database, None, None]:
    """
    Open a Rekordbox6Database safely.

    Parameters
    ----------
    db_path : Path, optional
        Path to the database file. Defaults to DJMT_DB.
    write : bool
        If True, creates a backup and checks that Rekordbox is not running
        before yielding. Set False for read-only operations.

    Yields
    ------
    Rekordbox6Database
        Open database instance. Caller should not call .close() manually.

    Raises
    ------
    RuntimeError
        If write=True and Rekordbox is currently running.
    RuntimeError
        If write=True and backup creation fails.
    """
    target = db_path or DJMT_DB

    if write:
        if rekordbox_is_running():
            raise RuntimeError(
                "Rekordbox is currently running. "
                "Close it before making any changes to the database."
            )
        backup_path = _backup_db(target)
        log.info("Write session opening on %s (backup: %s)", target, backup_path)
    else:
        log.debug("Read-only session opening on %s", target)

    db: Rekordbox6Database | None = None
    try:
        db = Rekordbox6Database(target)
        yield db
    except Exception:
        if write and db is not None:
            log.exception("Exception during write session — rolling back")
            try:
                db.rollback()
            except Exception:
                log.exception("Rollback also failed")
        raise
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:
                log.exception("Error closing database")


# ─── Convenience wrappers ─────────────────────────────────────────────────────

@contextmanager
def read_db(db_path: Path | None = None) -> Generator[Rekordbox6Database, None, None]:
    """Shorthand for open_db(write=False). No backup, no process check."""
    with open_db(db_path, write=False) as db:
        yield db


@contextmanager
def write_db(db_path: Path | None = None) -> Generator[Rekordbox6Database, None, None]:
    """Shorthand for open_db(write=True). Backup + process check enforced."""
    with open_db(db_path, write=True) as db:
        yield db


# ─── Smoke test (run this file directly to verify) ────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(message)s")

    print(f"Rekordbox running: {rekordbox_is_running()}")

    print("\n--- Read-only test ---")
    with read_db() as db:
        n_playlists = db.get_playlist().count()
        n_tracks = db.get_content().count()
        print(f"  Playlists : {n_playlists}")
        print(f"  Tracks    : {n_tracks}")

    print("\n--- Backup test (no actual write) ---")
    backup = _backup_db(DJMT_DB)
    print(f"  Backup written to: {backup}")
    print("\nAll checks passed.")
