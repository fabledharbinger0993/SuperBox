"""
fablegear / health.py

Proactive hazard scanner — runs at startup and on-demand.

Detects configurations and file states that cause silent corruption,
data loss, or hard-to-diagnose failures.  Never writes to the DB or
moves files.  The only write it performs is creating a missing backup
directory (see auto_heal_safe).

Public interface
----------------
run_health_checks() -> list[HealthFinding]
    Run all checks. Each check is isolated — an exception in one does
    not abort the others.

auto_heal_safe(findings) -> list[str]
    Apply only 100%-safe repairs (currently: create missing backup_dir).
    Returns a list of human-readable descriptions of what was fixed.

SEVERITY levels (stored as string)
    critical — data corruption or loss is possible right now
    warn     — operation will silently fail or setup is fragile
    info     — advisory, no immediate risk
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class HealthFinding:
    id: str                        # machine-readable identifier
    severity: str                  # 'critical' | 'warn' | 'info'
    title: str                     # short label shown in UI
    detail: str                    # one-sentence explanation
    fix_hint: str = ""             # what the user should do
    auto_fixable: bool = False     # safe to fix without user input
    auto_fix_fn: Callable | None = field(default=None, repr=False)

    def as_dict(self) -> dict:
        return {
            "id":           self.id,
            "severity":     self.severity,
            "title":        self.title,
            "detail":       self.detail,
            "fix_hint":     self.fix_hint,
            "auto_fixable": self.auto_fixable,
        }


# ── Internal helpers ──────────────────────────────────────────────────────────

def _disk_partitions() -> list:
    """Return psutil disk partitions; falls back to [] on import failure."""
    try:
        import psutil  # noqa: PLC0415
        return psutil.disk_partitions(all=True)
    except Exception:
        return []


def _partition_for(path: Path) -> object | None:
    """Return the psutil partition whose mount point best matches *path*."""
    parts = _disk_partitions()
    best = None
    best_len = 0
    path_str = str(path)
    for p in parts:
        mp = p.mountpoint
        if path_str.startswith(mp) and len(mp) > best_len:
            best = p
            best_len = len(mp)
    return best


def _is_readonly_mount(path: Path) -> bool:
    part = _partition_for(path)
    if part is None:
        return False
    # psutil opts is comma-separated; "ro" appears as a standalone token
    return "ro" in {o.strip() for o in part.opts.split(",")}


def _free_bytes(path: Path) -> int | None:
    """Return free bytes on the volume containing *path*, or None on error."""
    try:
        import psutil  # noqa: PLC0415
        return psutil.disk_usage(str(path)).free
    except Exception:
        return None


def _volume_name(path: Path) -> str:
    """Extract /Volumes/<name> component or return the path string."""
    parts = path.parts
    if len(parts) >= 3 and parts[1] == "Volumes":
        return parts[2]
    return str(path)


def _on_same_volume(a: Path, b: Path) -> bool:
    """Return True if both paths share the same mount point."""
    pa = _partition_for(a)
    pb = _partition_for(b)
    if pa is None or pb is None:
        return False
    return pa.mountpoint == pb.mountpoint


_CLOUD_SYNC_ROOTS: list[Path] = []

def _cloud_sync_roots() -> list[Path]:
    """Return known cloud-sync directory roots that are present on this machine."""
    global _CLOUD_SYNC_ROOTS
    if _CLOUD_SYNC_ROOTS:
        return _CLOUD_SYNC_ROOTS
    home = Path.home()
    candidates = [
        home / "Library" / "Mobile Documents",       # iCloud Drive (legacy)
        home / "Library" / "CloudStorage",           # iCloud Drive (modern, also Google Drive, OneDrive via CloudStorage)
        home / "Dropbox",
        home / "OneDrive",
        home / "OneDrive - Personal",
        home / "Google Drive",
        home / "Box",
        home / "Box Sync",
    ]
    _CLOUD_SYNC_ROOTS = [c for c in candidates if c.exists()]
    return _CLOUD_SYNC_ROOTS


def _is_cloud_synced(path: Path) -> str | None:
    """Return a human-readable cloud provider name if *path* is inside a sync folder, else None."""
    path_str = str(path)
    labels = {
        "Mobile Documents": "iCloud",
        "CloudStorage":     "iCloud / Cloud Storage",
        "Dropbox":          "Dropbox",
        "OneDrive":         "OneDrive",
        "Google Drive":     "Google Drive",
        "Box":              "Box",
    }
    for root in _cloud_sync_roots():
        if path_str.startswith(str(root)):
            for key, label in labels.items():
                if key in str(root):
                    return label
            return "a cloud sync folder"
    return None


def _most_recent_backup(backup_dir: Path) -> Path | None:
    if not backup_dir.exists():
        return None
    candidates = sorted(backup_dir.glob("master.backup_*.db"), reverse=True)
    return candidates[0] if candidates else None


# ── Individual checks ─────────────────────────────────────────────────────────

def _check_rekordbox_running() -> HealthFinding | None:
    try:
        from db_connection import rekordbox_is_running  # noqa: PLC0415
        if not rekordbox_is_running():
            return None
        return HealthFinding(
            id="rb_running",
            severity="warn",
            title="Rekordbox is open",
            detail=(
                "Rekordbox has an exclusive lock on master.db. "
                "Any write operation from FableGear will fail or corrupt the database."
            ),
            fix_hint="Close Rekordbox before running import, relocate, or organise tools.",
        )
    except Exception:
        return None


def _check_cloud_sync() -> list[HealthFinding]:
    findings = []
    try:
        from user_config import get_drive_status  # noqa: PLC0415
        status = get_drive_status()
        if not status["configured"]:
            return findings

        db_paths = {
            "local Rekordbox DB":  status.get("local_db_path"),
            "device DB":           status.get("device_db_path"),
        }
        music_path = status.get("music_root_path")

        for label, path_str in db_paths.items():
            if not path_str:
                continue
            provider = _is_cloud_synced(Path(path_str))
            if provider:
                findings.append(HealthFinding(
                    id=f"cloud_sync_{label.replace(' ', '_')}",
                    severity="critical",
                    title=f"{label} is inside {provider}",
                    detail=(
                        f"{label} lives under a {provider}-managed folder. "
                        "Cloud sync agents modify files in place and can silently corrupt "
                        "a SQLite database while it is open."
                    ),
                    fix_hint=(
                        f"Move {label} out of the {provider} folder, or exclude it "
                        f"from {provider} sync, then update FableGear config."
                    ),
                ))

        if music_path:
            provider = _is_cloud_synced(Path(music_path))
            if provider:
                findings.append(HealthFinding(
                    id="cloud_sync_music_root",
                    severity="warn",
                    title=f"Music library is inside {provider}",
                    detail=(
                        f"The music root ({music_path}) is under a {provider}-managed "
                        "folder. Cloud sync can rename, move, or lock audio files "
                        "mid-operation, breaking scanner and organiser runs."
                    ),
                    fix_hint=f"Exclude the music folder from {provider} sync or move it to a local volume.",
                ))
    except Exception:
        pass
    return findings


def _check_db_size_regression() -> HealthFinding | None:
    """Warn if the current DB is >30 % smaller than the most recent backup."""
    try:
        from config import BACKUP_DIR, LOCAL_DB  # noqa: PLC0415
        db_path = Path(LOCAL_DB)
        if not db_path.exists():
            return None
        current_size = db_path.stat().st_size
        if current_size == 0:
            return HealthFinding(
                id="db_zero_bytes",
                severity="critical",
                title="master.db is 0 bytes",
                detail=(
                    f"{db_path} exists but is empty — this indicates database truncation "
                    "or a failed write. Do NOT run any write operations."
                ),
                fix_hint=(
                    "Restore from a backup in FableGear Archive/Savepoints, "
                    "or re-install Rekordbox and let it rebuild the database."
                ),
            )
        backup = _most_recent_backup(Path(BACKUP_DIR))
        if backup is None:
            return None
        backup_size = backup.stat().st_size
        if backup_size > 0 and current_size < backup_size * 0.70:
            shrink_pct = round(100 * (1 - current_size / backup_size))
            return HealthFinding(
                id="db_size_regression",
                severity="critical",
                title=f"master.db is {shrink_pct}% smaller than last backup",
                detail=(
                    f"The live database ({current_size:,} bytes) is significantly smaller "
                    f"than the most recent backup ({backup_size:,} bytes). "
                    "This may indicate partial corruption or accidental truncation."
                ),
                fix_hint=(
                    f"Compare {db_path.name} against {backup.name}. "
                    "If data is missing, restore from backup before running any tools."
                ),
            )
    except Exception:
        pass
    return None


def _check_readonly_mounts() -> list[HealthFinding]:
    findings = []
    try:
        from user_config import get_drive_status  # noqa: PLC0415
        from config import BACKUP_DIR  # noqa: PLC0415
        status = get_drive_status()
        if not status["configured"]:
            return findings

        targets = {
            "Music library":    status.get("music_root_path"),
            "Backup directory": str(BACKUP_DIR),
        }
        for label, path_str in targets.items():
            if not path_str:
                continue
            p = Path(path_str)
            # Walk up to find an existing ancestor to check the volume
            check = p
            while not check.exists() and check != check.parent:
                check = check.parent
            if not check.exists():
                continue
            if _is_readonly_mount(check):
                findings.append(HealthFinding(
                    id=f"readonly_mount_{label.lower().replace(' ', '_')}",
                    severity="warn",
                    title=f"{label} is on a read-only volume ({_volume_name(p)})",
                    detail=(
                        f"The volume containing {label.lower()} is mounted read-only. "
                        "Writes will silently fail or raise an error at runtime."
                    ),
                    fix_hint=(
                        "For NTFS drives on macOS: install a third-party NTFS driver "
                        "(e.g. Paragon NTFS, Tuxera), or reformat the drive as ExFAT. "
                        "For other volumes: check Disk Utility → First Aid."
                    ),
                ))
    except Exception:
        pass
    return findings


def _check_backup_same_volume() -> HealthFinding | None:
    try:
        from config import BACKUP_DIR, LOCAL_DB  # noqa: PLC0415
        db_path = Path(LOCAL_DB)
        backup_path = Path(BACKUP_DIR)
        if not db_path.exists():
            return None
        # Walk up backup path to an existing ancestor
        check_backup = backup_path
        while not check_backup.exists() and check_backup != check_backup.parent:
            check_backup = check_backup.parent
        if not check_backup.exists():
            return None
        if _on_same_volume(db_path, check_backup):
            return HealthFinding(
                id="backup_same_volume",
                severity="warn",
                title="Backups stored on same volume as source DB",
                detail=(
                    f"master.db and its backups are both on {_volume_name(db_path)}. "
                    "If that volume fails, you lose both the live database and all backups."
                ),
                fix_hint=(
                    "Move the backup directory to a separate drive. "
                    "Update backup_dir in ~/.fablegear/config.json."
                ),
            )
    except Exception:
        pass
    return None


def _check_free_space() -> HealthFinding | None:
    """Warn if less than 500 MB is free on the music root volume."""
    try:
        from user_config import get_drive_status  # noqa: PLC0415
        status = get_drive_status()
        if not status.get("music_root_ok"):
            return None
        music_root = Path(status["music_root_path"])
        free = _free_bytes(music_root)
        if free is None:
            return None
        threshold = 500 * 1024 * 1024  # 500 MB
        if free < threshold:
            free_mb = free // (1024 * 1024)
            return HealthFinding(
                id="free_space_low",
                severity="warn",
                title=f"Low disk space on {_volume_name(music_root)} ({free_mb} MB free)",
                detail=(
                    f"Less than 500 MB remains on the music library volume. "
                    "Import, organise, and download operations may fail mid-run."
                ),
                fix_hint="Free up space on the drive before running any tool that copies or moves files.",
            )
    except Exception:
        pass
    return None


def _check_backup_dir_missing() -> HealthFinding | None:
    try:
        from config import BACKUP_DIR  # noqa: PLC0415
        backup_path = Path(BACKUP_DIR)
        if backup_path.exists():
            return None

        def _create_backup_dir():
            backup_path.mkdir(parents=True, exist_ok=True)

        return HealthFinding(
            id="backup_dir_missing",
            severity="info",
            title="Backup directory does not exist yet",
            detail=f"{backup_path} has not been created. Backups will fail until it exists.",
            fix_hint="This will be created automatically when a backup is first needed.",
            auto_fixable=True,
            auto_fix_fn=_create_backup_dir,
        )
    except Exception:
        pass
    return None


def _check_db_symlink() -> HealthFinding | None:
    try:
        from user_config import get_drive_status  # noqa: PLC0415
        status = get_drive_status()
        for key in ("local_db_path", "device_db_path"):
            p_str = status.get(key)
            if p_str and Path(p_str).is_symlink():
                return HealthFinding(
                    id="db_is_symlink",
                    severity="warn",
                    title=f"Configured DB path is a symlink ({Path(p_str).name})",
                    detail=(
                        f"{p_str} is a symbolic link. Writes resolve through the symlink, "
                        "which can point to unexpected locations after a drive remount."
                    ),
                    fix_hint="Point config directly at the real file path to avoid surprises.",
                )
    except Exception:
        pass
    return None


# ── Public interface ──────────────────────────────────────────────────────────

def run_health_checks() -> list[HealthFinding]:
    """
    Run all hazard checks in isolation.  An unhandled exception in any
    individual check is caught and logged — it never aborts the others.
    """
    findings: list[HealthFinding] = []

    single_checks = [
        _check_rekordbox_running,
        _check_db_size_regression,
        _check_backup_same_volume,
        _check_free_space,
        _check_backup_dir_missing,
        _check_db_symlink,
    ]
    multi_checks = [
        _check_cloud_sync,
        _check_readonly_mounts,
    ]

    for fn in single_checks:
        try:
            result = fn()
            if result:
                findings.append(result)
        except Exception as exc:
            log.warning("health check %s raised: %s", fn.__name__, exc)

    for fn in multi_checks:
        try:
            findings.extend(fn())
        except Exception as exc:
            log.warning("health check %s raised: %s", fn.__name__, exc)

    # Sort: critical first, then warn, then info
    order = {"critical": 0, "warn": 1, "info": 2}
    findings.sort(key=lambda f: order.get(f.severity, 9))
    return findings


def auto_heal_safe(findings: list[HealthFinding]) -> list[str]:
    """
    Apply all auto_fixable repairs from *findings*.
    Returns a list of human-readable descriptions of what was done.
    Only repairs that are 100% non-destructive (create dirs, etc.) are
    ever marked auto_fixable.
    """
    applied = []
    for f in findings:
        if f.auto_fixable and f.auto_fix_fn is not None:
            try:
                f.auto_fix_fn()
                applied.append(f.title)
                log.info("health auto-heal applied: %s", f.id)
            except Exception as exc:
                log.warning("health auto-heal failed for %s: %s", f.id, exc)
    return applied
