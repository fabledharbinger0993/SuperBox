"""
db_migrator.py — Move Rekordbox library data to an external drive.

Copies ~/Library/Pioneer/rekordbox/ to <drive_root>/Pioneer/rekordbox/,
then replaces the original with a symlink so Rekordbox continues to work
normally while the actual data lives on the external drive.

This makes the library self-contained and portable: disconnect the drive,
reconnect on any Mac (or create the same symlink), and Rekordbox works.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path


# ── Pioneer app-data location ─────────────────────────────────────────────────

def _pioneer_rekordbox_dir() -> Path:
    """Return ~/Library/Pioneer/rekordbox on macOS."""
    p = Path.home() / "Library" / "Pioneer" / "rekordbox"
    if not p.exists():
        raise FileNotFoundError(
            f"Rekordbox data directory not found: {p}\n"
            "Is rekordbox installed on this Mac?"
        )
    return p


def _drive_root_from_path(target: str) -> Path:
    """
    Derive the drive root from a path on an external volume.

    /Volumes/DJMT/DJMT PRIMARY  →  /Volumes/DJMT
    /Users/…                    →  / (fallback)
    """
    p = Path(target).resolve()
    parts = p.parts
    if len(parts) >= 3 and parts[1] == "Volumes":
        return Path("/") / parts[1] / parts[2]
    # Fallback: use provided path as root
    return p


def _rb_is_running() -> bool:
    try:
        r = subprocess.run(
            ["pgrep", "-f", "rekordbox"],
            capture_output=True, text=True, timeout=5,
        )
        return r.returncode == 0
    except Exception:
        return False


# ── Main migration ─────────────────────────────────────────────────────────────

def migrate(target_path: str):
    """
    Generator — yields SSE-formatted data lines, then a done event.

    target_path: the organise target (e.g. /Volumes/DJMT/DJMT PRIMARY).
    Drive root is derived automatically from the volume name.
    """

    def _line(msg: str):
        return f"data: {json.dumps({'line': msg})}\n\n"

    def _done(code: int):
        return f"data: {json.dumps({'done': True, 'exit_code': code})}\n\n"

    yield _line("── Rekordbox Library Migration ──────────────────────────────")

    # 1. Safety check
    if _rb_is_running():
        yield _line("✗ Rekordbox is running. Close it before migrating.")
        yield _done(1)
        return

    # 2. Locate source
    try:
        src = _pioneer_rekordbox_dir()
    except FileNotFoundError as exc:
        yield _line(f"✗ {exc}")
        yield _done(1)
        return

    # 3. Derive drive root and destination
    drive_root = _drive_root_from_path(target_path)
    if not drive_root.exists():
        yield _line(f"✗ Drive root not found: {drive_root}")
        yield _line("   Make sure the target drive is mounted.")
        yield _done(1)
        return

    dst = drive_root / "Pioneer" / "rekordbox"

    yield _line(f"  Source : {src}")
    yield _line(f"  Drive  : {drive_root}")
    yield _line(f"  Target : {dst}")
    yield _line("")

    # 4. Already migrated?
    if src.is_symlink():
        resolved = src.resolve()
        if resolved == dst.resolve():
            yield _line("✓ Already migrated — source is a symlink to the target.")
            yield _line("  Nothing to do.")
            yield _done(0)
            return
        yield _line(f"⚠  Source is a symlink to {resolved}, not the expected target.")
        yield _line("   Aborting — resolve manually before re-running.")
        yield _done(1)
        return

    # 5. Copy source → destination
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        yield _line(f"  Destination already exists — removing stale copy…")
        shutil.rmtree(dst)

    yield _line(f"  Copying {src.name}/…")
    try:
        shutil.copytree(str(src), str(dst))
    except Exception as exc:
        yield _line(f"✗ Copy failed: {exc}")
        yield _done(1)
        return
    yield _line("  Copy complete.")

    # 6. Verify master.db arrived
    if not (dst / "master.db").exists():
        yield _line("✗ master.db not found at destination — aborting before removing source.")
        yield _done(1)
        return

    # 7. Remove original and replace with symlink
    try:
        shutil.rmtree(str(src))
        src.symlink_to(dst)
    except Exception as exc:
        yield _line(f"✗ Symlink creation failed: {exc}")
        yield _line(f"  Your data is safe at {dst} — recreate the symlink manually:")
        yield _line(f"  ln -s '{dst}' '{src}'")
        yield _done(1)
        return

    yield _line(f"  Symlink : {src} → {dst}")
    yield _line("")
    yield _line("✓ Migration complete.")
    yield _line("  Rekordbox will continue to work normally.")
    yield _line("  Your library data now lives on the external drive.")
    yield _line(f"  To use on another Mac: ln -s '{dst}' '{src}'")
    yield _done(0)
