"""
helpers.py — Shared infrastructure for FableGear Flask blueprints.

Contains shared globals, utilities, and SSE streaming primitives that all
blueprint modules and app.py import.  Does NOT import from app.py or any
blueprint — this is the clean layer that prevents circular imports.

The _stream() generator defined here is the canonical fix for the bug where
_sse_response() called an undefined _stream local.
"""
from __future__ import annotations

import datetime
import json
import os
import subprocess
import sys
import threading
import uuid
from pathlib import Path

from flask import Response, jsonify, request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_sock import Sock
from mutagen import File as MutagenFile

from pioneer_export_validator import validate_export_paths, build_export_metadata, validate_copied_file_exists


# ── Playback backend (optional) ───────────────────────────────────────────────

_playback_import_errors: list = []

try:
    import sounddevice as _sounddevice  # type: ignore[import-untyped]
except Exception as _exc:
    _sounddevice = None
    _playback_import_errors.append(f"sounddevice unavailable: {_exc}")

try:
    import soundfile as _soundfile  # type: ignore[import-untyped]
except Exception as _exc:
    _soundfile = None
    _playback_import_errors.append(f"soundfile unavailable: {_exc}")

_PLAYBACK_AVAILABLE: bool = _sounddevice is not None and _soundfile is not None
_PLAYBACK_IMPORT_ERROR: str | None = "; ".join(_playback_import_errors) or None


# ── Resource root — handles both dev and PyInstaller bundle ──────────────────

_REPO_ROOT = Path(
    os.environ.get("FABLEGEAR_ROOT")
    or getattr(sys, "_MEIPASS", None)
    or Path(__file__).parent.resolve()
)
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

REPO_ROOT: Path = _REPO_ROOT
CLI_PATH: Path = REPO_ROOT / "cli.py"


# ── Flask extensions (lazy-init; app.py calls .init_app(app)) ────────────────

limiter: Limiter = Limiter(
    key_func=get_remote_address,
    default_limits=["1000 per hour"],
    storage_uri="memory://",
)

sock: Sock = Sock()


# ── Active-process tracker (interrupt / emergency-stop) ───────────────────────

_proc_lock: threading.Lock = threading.Lock()
_active_procs: dict[str, subprocess.Popen] = {}


# ── Step state tracker ────────────────────────────────────────────────────────

try:
    from state_tracker import mark_step_complete, get_step_status  # noqa: PLC0415
    _STATE_TRACKER_AVAILABLE = True
except ImportError:
    _STATE_TRACKER_AVAILABLE = False

    def mark_step_complete(*a, **kw):  # type: ignore[misc]
        pass

    def get_step_status(*a, **kw) -> dict:  # type: ignore[misc]
        return {}


# ── Playback state (dict so mutations are visible across blueprint modules) ───

_playback_lock: threading.Lock = threading.Lock()
_playback_stop_event: threading.Event = threading.Event()
_playback: dict = {
    "thread": None,
    "current_path": None,
}


# ── Shared export job state ───────────────────────────────────────────────────
# Used by both routes_player (library export) and routes_mobile (USB export).

_EXPORT_JOBS: dict[str, dict] = {}
_EXPORT_LOCK: threading.Lock = threading.Lock()


# ── Job / token size limits ───────────────────────────────────────────────────

_MAX_ANALYSIS_JOBS: int = 100
_MAX_EXPORT_JOBS: int = 50
_MAX_PREVIEW_JOBS: int = 100
_MAX_PRUNE_TOKENS: int = 200


def _evict_old_jobs(store: dict, max_size: int) -> None:
    """Trim a job dict to *max_size* by removing the oldest entries."""
    if len(store) > max_size:
        excess = len(store) - max_size
        for key in list(store.keys())[:excess]:
            del store[key]


# ── Path / config helpers ─────────────────────────────────────────────────────

def _backup_dir() -> Path:
    """Return the configured backup directory, with a sensible fallback."""
    try:
        from config import BACKUP_DIR  # noqa: PLC0415
        return BACKUP_DIR
    except Exception:
        return Path.home() / "rekordbox-toolkit" / "backups"


def _drives_guard(*needs: str):
    """
    Verify that required drive-backed resources are accessible before a route
    proceeds.  Call at the top of any handler that uses the DJ drive or music
    library, *before* any lazy imports that depend on config.py.

    Parameters
    ----------
    *needs : str
        Any subset of 'local_db', 'device_db', 'music_root'.
        Omit a key if that resource is not required for the operation.

    Returns
    -------
    None
        All required resources are accessible — safe to continue.
    (Response, int)
        A JSON error response with HTTP 503 and ``drives_offline: true``.
        Return this immediately from the route handler.

    Example
    -------
    guard = _drives_guard('device_db', 'music_root')
    if guard:
        return guard
    """
    from user_config import get_drive_status  # noqa: PLC0415
    status = get_drive_status()

    if not status["configured"]:
        return jsonify({
            "ok": False,
            "error": "FableGear is not configured yet.",
            "hint": "Run: python3 cli.py setup",
            "drives_offline": True,
        }), 503

    def _vol_name(path_str):
        if not path_str:
            return "drive"
        parts = Path(path_str).parts
        return parts[2] if len(parts) >= 3 and parts[1] == "Volumes" else path_str

    missing = []
    if "local_db" in needs and not status["local_db_ok"]:
        missing.append("local Rekordbox database not found — is Rekordbox installed?")
    if "device_db" in needs and not status["device_db_ok"]:
        missing.append(f"DJ drive database offline — connect \"{_vol_name(status['device_db_path'])}\" and try again")
    if "music_root" in needs and not status["music_root_ok"]:
        missing.append(f"music library offline — connect \"{_vol_name(status['music_root_path'])}\" and try again")

    if missing:
        return jsonify({
            "ok": False,
            "error": "; ".join(missing),
            "hint": "Reconnect the drive and try again.",
            "drives_offline": True,
        }), 503

    return None


def _current_fablegear_mode() -> str:
    try:
        from config import FABLEGEAR_MODE  # noqa: PLC0415
        return str(FABLEGEAR_MODE).strip() or "rural"
    except Exception:
        return "rural"


def _detect_pioneer_drive_layout(drive_path: str | Path) -> dict:
    """Classify a mounted drive by the Pioneer export layout it contains."""
    drive = Path(drive_path)
    master_db = drive / "PIONEER" / "Master" / "master.db"
    rekordbox_dir = drive / "PIONEER" / "rekordbox"
    export_library = rekordbox_dir / "exportLibrary.db"
    export_pdb = rekordbox_dir / "export.pdb"
    export_ext = rekordbox_dir / "exportExt.pdb"

    if master_db.exists():
        return {
            "pioneer": True,
            "layout": "master-db",
            "export_supported": True,
            "export_error": None,
            "db_path": str(master_db),
        }

    if export_library.exists() or export_pdb.exists() or export_ext.exists():
        db_path = export_library if export_library.exists() else export_pdb if export_pdb.exists() else export_ext
        return {
            "pioneer": True,
            "layout": "rekordbox-usb-export",
            "export_supported": False,
            "export_error": (
                "This drive uses Rekordbox USB export files "
                "(PIONEER/rekordbox/exportLibrary.db, export.pdb, exportExt.pdb). "
                "FableGear can detect it, but it cannot safely write that Pioneer format yet."
            ),
            "db_path": str(db_path),
        }

    return {
        "pioneer": False,
        "layout": None,
        "export_supported": False,
        "export_error": None,
        "db_path": None,
    }


# ── Playback helpers ──────────────────────────────────────────────────────────

def _stop_playback() -> None:
    with _playback_lock:
        thread = _playback["thread"]
        if thread and thread.is_alive():
            _playback_stop_event.set()
            thread.join(timeout=2)
        _playback["thread"] = None
        _playback["current_path"] = None
        _playback_stop_event.clear()


def _play_audio_file(path: str) -> None:
    if not _PLAYBACK_AVAILABLE or _sounddevice is None or _soundfile is None:
        return
    try:
        with _soundfile.SoundFile(path) as audio_file:
            for block in audio_file.blocks(blocksize=1024, dtype="float32"):
                if _playback_stop_event.is_set():
                    break
                _sounddevice.play(block, audio_file.samplerate, blocking=True)
    except Exception as exc:
        print(f"Playback error: {exc}")


# ── Rekordbox status helpers ──────────────────────────────────────────────────

def _rb_is_running() -> bool:
    """Return True if a Rekordbox process is currently active."""
    try:
        from db_connection import rekordbox_is_running  # noqa: PLC0415
        return rekordbox_is_running()
    except Exception:
        return False


def _backup_info() -> dict:
    """Return the age of the most recent timestamped backup, if any."""
    backup_dir = _backup_dir()
    if not backup_dir.exists():
        return {"exists": False, "name": None, "age": None}
    backups = sorted(backup_dir.glob("master.backup_*.db"), reverse=True)
    if not backups:
        return {"exists": False, "name": None, "age": None}
    latest = backups[0]
    age = datetime.datetime.now() - datetime.datetime.fromtimestamp(latest.stat().st_mtime)
    h = int(age.total_seconds() // 3600)
    m = int((age.total_seconds() % 3600) // 60)
    age_str = f"{h}h {m}m ago" if h else f"{m}m ago"
    return {"exists": True, "name": latest.name, "age": age_str}


def _release_info() -> dict:
    """Return a short git-based release blurb for the UI status row."""
    env_version = os.environ.get("FABLEGEAR_VERSION", "").strip()
    if env_version:
        return {
            "exists": True,
            "label": f"Version: {env_version}",
            "tag": env_version,
            "commit": None,
            "source": "env",
        }

    try:
        commit = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(REPO_ROOT),
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=1.0,
        ).strip()
    except Exception:
        return {
            "exists": False,
            "label": "Version: unknown",
            "tag": None,
            "commit": None,
            "source": "none",
        }

    try:
        tag = subprocess.check_output(
            ["git", "describe", "--tags", "--exact-match", "HEAD"],
            cwd=str(REPO_ROOT),
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=1.0,
        ).strip()
    except Exception:
        tag = None

    if tag:
        label = f"Release: {tag} ({commit})"
        source = "git-tag"
    else:
        label = f"Build: {commit} (unreleased)"
        source = "git-head"

    return {
        "exists": True,
        "label": label,
        "tag": tag,
        "commit": commit,
        "source": source,
    }


def _subprocess_env() -> dict:
    """Return an environment dict for subprocesses running cli.py."""
    return os.environ.copy()


# ── SSE streaming — _stream is the canonical subprocess SSE generator ─────────

def _stream(
    cmd: list[str],
    library_root: str = "",
    step_name: str = "",
    prelude_lines: list[str] | None = None,
    cleanup_paths: list[Path] | None = None,
):
    """
    Generator: spawn *cmd* as a subprocess, stream stdout as SSE events.

    Yields:
      data: {"line": "..."}\\n\\n   — for each stdout line
      data: {"done": true, "exit_code": N}\\n\\n  — terminal event

    This is the fix for the _sse_response bug where _stream was called but
    never defined in the original monolithic app.py.
    """
    request_id = str(uuid.uuid4())
    exit_code = 0

    if prelude_lines:
        for line in prelude_lines:
            yield f"data: {json.dumps({'line': line})}\n\n"

    try:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            cwd=str(REPO_ROOT),
            env=_subprocess_env(),
        )
        with _proc_lock:
            _active_procs[request_id] = process
        try:
            for line in iter(process.stdout.readline, ""):
                yield f"data: {json.dumps({'line': line.rstrip()})}\n\n"
            process.wait()
            exit_code = process.returncode
        finally:
            with _proc_lock:
                _active_procs.pop(request_id, None)
    except Exception as exc:
        with _proc_lock:
            _active_procs.pop(request_id, None)
        yield f"data: {json.dumps({'line': f'[SERVER ERROR] {exc}'})}\n\n"
        exit_code = 1

    if library_root and step_name:
        mark_step_complete(library_root, step_name, exit_code)

    if cleanup_paths:
        for p in cleanup_paths:
            try:
                p.unlink(missing_ok=True)
            except Exception:
                pass

    yield f"data: {json.dumps({'done': True, 'exit_code': exit_code})}\n\n"


def _sse_response(
    cmd: list[str],
    library_root: str = "",
    step_name: str = "",
    prelude_lines: list[str] | None = None,
    cleanup_paths: list[Path] | None = None,
) -> Response:
    """Wrap _stream() in a Flask SSE Response."""
    return Response(
        _stream(
            cmd,
            library_root=library_root,
            step_name=step_name,
            prelude_lines=prelude_lines,
            cleanup_paths=cleanup_paths,
        ),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Tag-presence helpers (used by smart-skip logic) ───────────────────────────

def _tag_value_present(value) -> bool:
    if value is None:
        return False
    if isinstance(value, (list, tuple)):
        if not value:
            return False
        value = value[0]
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8", errors="ignore")
        except Exception:
            return False
    text = str(value).strip()
    return text not in {"", "0", "0.0"}


def _track_needs_tag_work(path: Path, detect_bpm: bool, detect_key: bool) -> tuple[bool, bool]:
    """Return (needs_bpm, needs_key) using fast tag presence checks only."""
    try:
        audio = MutagenFile(str(path), easy=False)
        tags = audio.tags if audio else None
    except Exception:
        return detect_bpm, detect_key

    if tags is None:
        return detect_bpm, detect_key

    tag_type = type(tags).__name__
    is_vorbis = "VCFLACDict" in tag_type or "VComment" in tag_type
    is_mp4 = "MP4Tags" in tag_type or "MP4" in tag_type

    has_bpm = False
    has_key = False

    if is_vorbis:
        has_bpm = _tag_value_present(tags.get("bpm"))
        has_key = _tag_value_present(tags.get("initialkey"))
    elif is_mp4:
        has_bpm = _tag_value_present(tags.get("tmpo"))
        has_key = _tag_value_present(tags.get("----:com.apple.iTunes:initialkey"))
    else:
        has_bpm = _tag_value_present(tags.get("TBPM"))
        has_key = _tag_value_present(tags.get("TKEY"))

    needs_bpm = detect_bpm and not has_bpm
    needs_key = detect_key and not has_key
    return needs_bpm, needs_key


def _smart_skip_candidates(roots: list[Path], detect_bpm: bool, detect_key: bool) -> dict:
    """Build process candidate list excluding tracks already complete for requested tag ops."""
    from scanner import scan_directory  # noqa: PLC0415
    from config import AUDIO_EXTENSIONS  # noqa: PLC0415

    pending: list[str] = []
    total = 0
    skipped_complete = 0
    unreadable = 0
    invalid_paths = 0

    for root in roots:
        if root.is_file():
            total += 1
            if root.suffix.lower() not in AUDIO_EXTENSIONS:
                invalid_paths += 1
                continue
            needs_bpm, needs_key = _track_needs_tag_work(root, detect_bpm, detect_key)
            if needs_bpm or needs_key:
                pending.append(str(root))
            else:
                skipped_complete += 1
            continue

        if not root.is_dir():
            invalid_paths += 1
            continue

        for track in scan_directory(root):
            total += 1
            path = track.path
            needs_bpm, needs_key = _track_needs_tag_work(path, detect_bpm, detect_key)
            if needs_bpm or needs_key:
                pending.append(str(path))
            else:
                skipped_complete += 1
            if track.errors:
                unreadable += 1

    return {
        "total": total,
        "pending": pending,
        "pending_count": len(pending),
        "skipped_complete": skipped_complete,
        "unreadable": unreadable,
        "invalid_paths": invalid_paths,
    }


def _sse_done(lines: list[str], exit_code: int = 0) -> Response:
    """Return an SSE Response containing a static list of lines then done."""
    def _gen():
        for line in lines:
            yield f"data: {json.dumps({'line': line})}\n\n"
        yield f"data: {json.dumps({'done': True, 'exit_code': exit_code})}\n\n"

    return Response(
        _gen(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _stream_pipeline(steps: list[dict]):
    """
    Generator: run a list of pipeline steps sequentially.

    Each step dict: {"name": str, "cmd": list[str], "type": str (optional),
                     "needs_csv": bool (optional), "library_root": str (optional)}

    SSE events beyond normal {"line": "..."} stream:
      {"step_start": N, "step_name": "...", "total_steps": N}
      {"step_end": N, "step_name": "...", "exit_code": N}
      {"done": true, "exit_code": 0}
      {"done": true, "exit_code": N, "failed_step": "..."}
    """
    request_id = str(uuid.uuid4())
    total = len(steps)
    last_report_path: str | None = None

    for idx, step in enumerate(steps, 1):
        name = step["name"]
        cmd = list(step["cmd"])

        if step.get("needs_csv") and last_report_path:
            cmd.append(last_report_path)

        yield f"data: {json.dumps({'step_start': idx, 'step_name': name, 'total_steps': total})}\n\n"

        exit_code = 0
        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                cwd=str(REPO_ROOT),
                env=_subprocess_env(),
            )
            with _proc_lock:
                _active_procs[request_id] = process
            try:
                for line in iter(process.stdout.readline, ""):
                    stripped = line.rstrip()
                    if stripped.startswith("FABLEGEAR_REPORT_PATH: "):
                        last_report_path = stripped[len("FABLEGEAR_REPORT_PATH: "):]
                    yield f"data: {json.dumps({'line': stripped})}\n\n"
                process.wait()
                exit_code = process.returncode
            finally:
                with _proc_lock:
                    _active_procs.pop(request_id, None)
        except Exception as exc:
            with _proc_lock:
                _active_procs.pop(request_id, None)
            yield f"data: {json.dumps({'line': f'[SERVER ERROR] {exc}'})}\n\n"
            exit_code = 1

        try:
            _pipe_root = step.get("library_root", "")
            if not _pipe_root:
                from config import MUSIC_ROOT as _MR  # noqa: PLC0415
                _pipe_root = str(_MR)
        except Exception:
            _pipe_root = ""
        if _pipe_root:
            mark_step_complete(_pipe_root, step.get("type", name), exit_code)

        yield f"data: {json.dumps({'step_end': idx, 'step_name': name, 'exit_code': exit_code})}\n\n"

        if exit_code != 0:
            yield f"data: {json.dumps({'done': True, 'exit_code': exit_code, 'failed_step': name})}\n\n"
            return

    yield f"data: {json.dumps({'done': True, 'exit_code': 0})}\n\n"


def _require_rb_closed():
    """Return an error response tuple if Rekordbox is running, else None."""
    if _rb_is_running():
        return jsonify({"error": "Rekordbox is running. Close it before running write operations."}), 409
    return None


def _get_library_root(req, primary_field: str) -> str:
    """
    Best-effort extraction of the library root from the current request.
    Checks ?library_root= first, then the primary path param, then config.MUSIC_ROOT.
    """
    root = req.args.get("library_root", "").strip()
    if root:
        return root
    path = req.args.get(primary_field, "").strip()
    if path:
        return str(Path(path))
    try:
        from config import MUSIC_ROOT  # noqa: PLC0415
        return str(MUSIC_ROOT)
    except Exception:
        return ""


# ── USB / library export job runner (shared between player + mobile) ──────────

def _run_export(job_id: str, playlist_ids: list, drive_path: str) -> None:
    """
    Background thread: export selected playlists from the main Rekordbox DB
    to a supported Pioneer-style target database.

    Supported target layout:
      - PIONEER/Master/master.db
      - /Contents/... media copied onto the target drive

    Unsupported Pioneer USB exports such as exportLibrary.db/export.pdb are
    detected and rejected up front rather than being mis-written.
    """
    import shutil as _shutil  # noqa: PLC0415
    import json as _json  # noqa: PLC0415
    from pathlib import Path as _Path  # noqa: PLC0415
    from db_connection import read_db, rekordbox_is_running  # noqa: PLC0415
    from config import LOCAL_DB as _DB, MUSIC_ROOT as _MUSIC_ROOT  # noqa: PLC0415
    from pyrekordbox import Rekordbox6Database  # noqa: PLC0415
    import ws_bus as _ws  # noqa: PLC0415

    def _safe_segment(value: str, fallback: str) -> str:
        text = str(value or "").strip().replace("/", "_").replace("\\", "_")
        return text or fallback

    def _export_relative_path(src_path: _Path, track) -> _Path:
        """
        Compute path relative to Contents directory on USB drive.
        
        For files under MUSIC_ROOT: use relative path from root (Artist/Album/track.mp3)
        For files outside MUSIC_ROOT: use hash-based name to avoid collisions
        
        All paths are validated by pioneer_export_validator before DB insertion.
        """
        try:
            # If file is under MUSIC_ROOT, use its relative path structure
            return src_path.resolve().relative_to(_MUSIC_ROOT.resolve())
        except (ValueError, OSError):
            # File is outside MUSIC_ROOT (e.g., external drive)
            # Use a deterministic path based on filename + hash to avoid collisions
            import hashlib  # noqa: PLC0415
            
            file_stem = src_path.stem or "track"
            file_ext = src_path.suffix or ""
            
            # Hash the full source path to create a unique identifier
            path_hash = hashlib.md5(str(src_path.resolve()).encode()).hexdigest()[:8]
            
            # Return: External/<filename>-<hash>.<ext>
            # This ensures deterministic, collision-free names
            safe_name = _safe_segment(file_stem, "track")
            return _Path("External") / f"{safe_name}-{path_hash}{file_ext}"

    def _push(update: dict) -> None:
        try:
            _ws.broadcast(_json.dumps({"type": "export_update", "job_id": job_id, **update}))
        except Exception:
            pass

    def _update(patch: dict) -> None:
        with _EXPORT_LOCK:
            _EXPORT_JOBS[job_id].update(patch)
        _push(patch)

    try:
        drive_info = _detect_pioneer_drive_layout(drive_path)
        if not drive_info.get("pioneer"):
            _update({"status": "failed", "errors": [f"No Pioneer export structure detected on {drive_path}"]})
            return
        if not drive_info.get("export_supported"):
            _update({"status": "failed", "errors": [drive_info.get("export_error") or "Unsupported Pioneer drive layout"]})
            return

        usb_db_path = _Path(str(drive_info["db_path"]))
        contents_root = _Path(drive_path) / "Contents"

        if rekordbox_is_running():
            _update({"status": "failed",
                      "errors": ["Rekordbox is running — close it before exporting to USB"]})
            return

        backup_path = usb_db_path.with_suffix(".export_backup.db")
        _shutil.copy2(str(usb_db_path), str(backup_path))

        source_playlists: dict[str, object] = {}
        selected_playlists: list[object] = []
        playlist_tracks: dict[str, list[dict]] = {}
        export_tracks: dict[str, dict] = {}
        errors: list[str] = []

        with read_db(_DB) as src:
            for playlist in src.get_playlist().all():
                source_playlists[str(playlist.ID)] = playlist

            for pl_id in playlist_ids:
                pl = source_playlists.get(str(pl_id))
                if pl is None:
                    errors.append(f"Playlist {pl_id} not found in source library")
                    continue
                if int(getattr(pl, "Attribute", 0) or 0) == 1:
                    errors.append(f"Playlist {pl.Name or pl_id} is a folder and cannot be exported directly")
                    continue

                selected_playlists.append(pl)
                songs = src.get_playlist_songs(PlaylistID=pl.ID).order_by("TrackNo").all()
                playlist_entries: list[dict] = []
                for song in songs:
                    t = song.Content
                    if t is None:
                        continue
                    path = t.FolderPath or ""
                    if not path or not path.startswith("/"):
                        errors.append(f"{t.Title or 'Track'} has no absolute source path")
                        continue
                    source_path = _Path(path)
                    if not source_path.exists():
                        errors.append(f"{source_path.name}: source file is missing")
                        continue
                    dest_path = contents_root / _export_relative_path(source_path, t)
                    dest_key = str(dest_path)
                    export_tracks.setdefault(dest_key, {
                        "source_path": source_path,
                        "dest_path": dest_path,
                        "track": t,
                        "title": t.Title or source_path.name,
                        "usb_row": None,
                    })
                    playlist_entries.append({
                        "dest_key": dest_key,
                        "track_no": int(getattr(song, "TrackNo", 0) or 0),
                    })
                playlist_tracks[str(pl.ID)] = playlist_entries

        # ── Validate all export paths before opening database ────────────────
        try:
            export_entries = list(export_tracks.values())
            validated_entries = validate_export_paths(export_entries)
            
            # Update export_tracks with validated metadata
            for entry in validated_entries:
                dest_key = str(entry["dest_path"])
                export_tracks[dest_key] = entry
        except Exception as exc:
            from pioneer_export_validator import PioneerExportError  # noqa: PLC0415
            
            if isinstance(exc, PioneerExportError):
                error_msg = str(exc)
            else:
                error_msg = f"Path validation failed: {exc}"
            
            _update({"status": "failed", "errors": [error_msg], "current_track": ""})
            return

        total = len(export_tracks)
        _update({"tracks_total": total, "tracks_done": 0, "status": "running"})

        if total == 0:
            final_status = "complete_with_errors" if errors else "complete"
            _update({"status": final_status, "tracks_done": 0, "errors": errors, "current_track": ""})
            return

        usb = Rekordbox6Database(str(usb_db_path))

        try:
            path_to_usb_row: dict[str, object] = {}
            for row in usb.get_content().all():
                if row.FolderPath:
                    path_to_usb_row[row.FolderPath] = row

            done = 0
            for entry in export_tracks.values():
                source_path = entry["source_path"]
                dest_path = entry["dest_path"]
                dest_key = str(dest_path)
                track = entry["track"]
                _update({"current_track": entry["title"]})

                try:
                    dest_path.parent.mkdir(parents=True, exist_ok=True)
                    if (not dest_path.exists()) or dest_path.stat().st_size != source_path.stat().st_size:
                        _shutil.copy2(str(source_path), str(dest_path))
                    
                    # Validate copy succeeded (post-copy check)
                    validate_copied_file_exists(str(dest_path))
                except Exception as exc:
                    errors.append(f"Copy {source_path.name}: {exc}")
                    done += 1
                    _update({"tracks_done": done})
                    continue

                usb_row = path_to_usb_row.get(dest_key)
                if usb_row is None:
                    legacy_row = path_to_usb_row.get(str(source_path))
                    if legacy_row is not None:
                        try:
                            setattr(legacy_row, "FolderPath", dest_key)
                            usb.flush()
                            usb_row = legacy_row
                            path_to_usb_row[dest_key] = usb_row
                        except Exception as exc:
                            errors.append(f"Migrate {source_path.name}: {exc}")

                if usb_row is None:
                    try:
                        # Pass validated metadata fields to add_content
                        usb_row = usb.add_content(
                            dest_key,
                            OrgFolderPath=entry.get("OrgFolderPath"),
                            rb_LocalFolderPath=entry.get("rb_LocalFolderPath"),
                        )
                        usb.flush()
                        path_to_usb_row[dest_key] = usb_row
                    except Exception as exc:
                        errors.append(f"{source_path.name}: {exc}")

                entry["usb_row"] = usb_row

                done += 1
                _update({"tracks_done": done})

            usb.commit()

            usb_playlist_index: dict[tuple[str, str, int], object] = {}
            for row in usb.get_playlist().all():
                key = (
                    row.Name or "",
                    str(getattr(row, "ParentID", "") or ""),
                    int(getattr(row, "Attribute", 0) or 0),
                )
                usb_playlist_index[key] = row

            usb_playlists_by_source_id: dict[str, object] = {}

            def _ensure_usb_playlist(src_playlist):
                src_id = str(src_playlist.ID)
                if src_id in usb_playlists_by_source_id:
                    return usb_playlists_by_source_id[src_id]

                parent_usb = None
                parent_id = str(getattr(src_playlist, "ParentID", "") or "")
                if parent_id:
                    parent_src = source_playlists.get(parent_id)
                    if parent_src is not None:
                        parent_usb = _ensure_usb_playlist(parent_src)

                parent_key = str(getattr(parent_usb, "ID", "") or "") if parent_usb is not None else ""
                attribute = int(getattr(src_playlist, "Attribute", 0) or 0)
                playlist_key = (src_playlist.Name or "", parent_key, attribute)
                usb_playlist = usb_playlist_index.get(playlist_key)

                if usb_playlist is None:
                    if attribute == 1:
                        usb_playlist = usb.create_playlist_folder(src_playlist.Name or "Folder", parent=parent_usb)
                    else:
                        usb_playlist = usb.create_playlist(src_playlist.Name or "Playlist", parent=parent_usb)
                    usb.flush()
                    usb_playlist_index[playlist_key] = usb_playlist

                usb_playlists_by_source_id[src_id] = usb_playlist
                return usb_playlist

            for src_playlist in selected_playlists:
                usb_pl = _ensure_usb_playlist(src_playlist)

                already_linked: set[str] = {
                    str(s.ContentID)
                    for s in usb.get_playlist_songs(PlaylistID=usb_pl.ID).all()
                }

                for playlist_entry in playlist_tracks.get(str(src_playlist.ID), []):
                    export_entry = export_tracks.get(playlist_entry["dest_key"])
                    usb_row = export_entry.get("usb_row") if export_entry else None
                    if usb_row is None:
                        continue
                    if str(usb_row.ID) not in already_linked:
                        try:
                            usb.add_to_playlist(usb_pl, usb_row, track_no=None)
                            already_linked.add(str(usb_row.ID))
                        except Exception as exc:
                            track_name = export_entry["source_path"].name if export_entry else "track"
                            errors.append(f"Link {track_name}: {exc}")

            usb.commit()

        finally:
            usb.close()

        final_status = "complete_with_errors" if errors else "complete"
        _update({"status": final_status, "errors": errors, "current_track": ""})

    except Exception as exc:
        _update({"status": "failed", "errors": [str(exc)], "current_track": ""})
