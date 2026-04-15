"""
RekitBox / app.py

Local web dashboard for rekordbox-toolkit.
Run:  python3 app.py
Open: http://localhost:5001

The UI runs all CLI commands via subprocess and streams output live.
Rekordbox must be closed for any write operation — the server checks this
before spawning write commands and refuses if the process is found running.

All modules (config, db_connection, pruner, etc.) are imported directly
from this file's parent directory — no PYTHONPATH manipulation required.
"""

import json
import os
import platform
import queue
import signal
import subprocess
import sys
import threading
import uuid
import datetime
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request
from flask_sock import Sock

# ── Resource root — handles both dev and PyInstaller bundle ──────────────────
# When PyInstaller runs, sys._MEIPASS is the temp dir where everything lives.
# REKITBOX_ROOT can also be set by main.py before importing this module.
_REPO_ROOT = Path(
    os.environ.get('REKITBOX_ROOT')
    or getattr(sys, '_MEIPASS', None)
    or Path(__file__).parent.resolve()
)
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

app = Flask(
    __name__,
    template_folder=str(_REPO_ROOT / 'templates'),
    static_folder=str(_REPO_ROOT / 'static'),
)
sock = Sock(app)

# ── Homebrew update checker (background, weekly) ──────────────────────────────
from brew_updater import start_background_checker as _start_brew_checker, \
                         check_now as _brew_check_now, \
                         get_status as _brew_get_status, \
                         BREW_DEPS as _BREW_DEPS
_start_brew_checker()

from update_checker import start_background_checker as _start_update_checker, \
                           get_status as _update_get_status
_start_update_checker()

# ── Step state tracker ───────────────────────────────────────────────────────
try:
    from state_tracker import mark_step_complete, get_step_status  # noqa: PLC0415
    _STATE_TRACKER_AVAILABLE = True
except ImportError:
    _STATE_TRACKER_AVAILABLE = False
    def mark_step_complete(*a, **kw): pass   # no-op fallback
    def get_step_status(*a, **kw): return {}  # no-op fallback

# ── Active-process tracker (interrupt / emergency-stop) ───────────────────────
_proc_lock: threading.Lock = threading.Lock()
_active_proc: "subprocess.Popen | None" = None

# ── Paths ──────────────────────────────────────────────────────────────────────

REPO_ROOT = _REPO_ROOT
CLI_PATH  = REPO_ROOT / "cli.py"


def _backup_dir() -> Path:
    """Return the configured backup directory, with a sensible fallback."""
    try:
        from config import BACKUP_DIR  # noqa: PLC0415
        return BACKUP_DIR
    except Exception:
        return Path.home() / "rekordbox-toolkit" / "backups"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _rb_is_running() -> bool:
    """
    Return True if a Rekordbox process is currently active.
    Delegates to db_connection.rekordbox_is_running() so the logic stays
    in one place (platform-aware, FileNotFoundError-safe).
    """
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


def _subprocess_env() -> dict:
    """Return an environment dict for subprocesses running cli.py."""
    return os.environ.copy()


def _stream(cmd: list[str], library_root: str = "", step_name: str = ""):
    """
    Generator that yields SSE-formatted lines from a subprocess.
    Each event is a JSON object:
      {"line": "..."}          — a line of output
      {"done": true, "exit_code": N}  — command finished

    Registers the process in _active_proc so /api/cancel endpoints can
    send signals to it mid-run.
    """
    global _active_proc
    _library_root = library_root
    _step_name    = step_name
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
            _active_proc = process
        try:
            for line in iter(process.stdout.readline, ""):
                yield f"data: {json.dumps({'line': line.rstrip()})}\n\n"
            process.wait()
            if _step_name and _library_root:
                mark_step_complete(_library_root, _step_name, process.returncode)
            yield f"data: {json.dumps({'done': True, 'exit_code': process.returncode})}\n\n"
        finally:
            with _proc_lock:
                _active_proc = None
    except Exception as exc:
        with _proc_lock:
            _active_proc = None
        yield f"data: {json.dumps({'line': f'[SERVER ERROR] {exc}', 'done': True, 'exit_code': 1})}\n\n"


def _sse_response(cmd: list[str], library_root: str = "", step_name: str = "") -> Response:
    return Response(
        _stream(cmd, library_root=library_root, step_name=step_name),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _stream_pipeline(steps: list[dict]):
    """
    Generator that runs a list of pipeline steps sequentially.
    Each step dict has: {"name": str, "cmd": list[str]}
    Some steps produce a REKITBOX_REPORT_PATH that the next step may consume
    (e.g. duplicates → prune). This is captured and injected as needed.

    SSE events emitted beyond the normal {"line": "..."} stream:
      {"step_start": N, "step_name": "...", "total_steps": N}
      {"step_end": N, "step_name": "...", "exit_code": N}
      {"done": true, "exit_code": 0}   — all steps complete
      {"done": true, "exit_code": N, "failed_step": "..."} — step failed
    """
    global _active_proc
    total = len(steps)
    last_report_path: str | None = None   # passed from duplicates → prune

    for idx, step in enumerate(steps, 1):
        name = step["name"]
        cmd  = list(step["cmd"])

        # Inject the CSV from a previous duplicates step into prune
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
                _active_proc = process
            try:
                for line in iter(process.stdout.readline, ""):
                    stripped = line.rstrip()
                    # Capture CSV path for downstream steps
                    if stripped.startswith("REKITBOX_REPORT_PATH: "):
                        last_report_path = stripped[len("REKITBOX_REPORT_PATH: "):]
                    yield f"data: {json.dumps({'line': stripped})}\n\n"
                process.wait()
                exit_code = process.returncode
            finally:
                with _proc_lock:
                    _active_proc = None
        except Exception as exc:
            with _proc_lock:
                _active_proc = None
            yield f"data: {json.dumps({'line': f'[SERVER ERROR] {exc}'})}\n\n"
            exit_code = 1

        # Journal step completion — derive library root from config for pipeline runs
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
    """Return an error response if Rekordbox is running, else None."""
    if _rb_is_running():
        return jsonify({
            "error": "Rekordbox is running. Close it before running write operations."
        }), 409
    return None


def _get_library_root(req, primary_field: str) -> str:
    """
    Best-effort extraction of the library root from the current request.
    Checks ?library_root= first, then falls back to the primary path param,
    then to config.MUSIC_ROOT. Returns empty string if nothing is available.
    """
    root = req.args.get("library_root", "").strip()
    if root:
        return root
    path = req.args.get(primary_field, "").strip()
    if path:
        from pathlib import Path as _Path
        return str(_Path(path))
    try:
        from config import MUSIC_ROOT  # noqa: PLC0415
        return str(MUSIC_ROOT)
    except Exception:
        return ""


# ── Startup ───────────────────────────────────────────────────────────────────

try:
    from config import ensure_archive_structure  # noqa: PLC0415
    ensure_archive_structure()
except Exception:
    pass  # Drive not mounted yet — non-fatal

# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def api_status():
    return jsonify({
        "rb_running": _rb_is_running(),
        "backup": _backup_info(),
    })


@app.route("/api/config")
def api_config():
    """Expose the configured default paths so the UI can pre-fill forms."""
    try:
        from config import (  # noqa: PLC0415
            DJMT_DB, MUSIC_ROOT, SKIP_DIRS,
            ARCHIVE_ROOT, SAVEPOINTS_DIR, QUARANTINE_DIR, REPORTS_DIR,
            ARCHIVE_ENABLED, _archive_mode, _custom_archive,
        )
        from user_config import load_user_config as _luc  # noqa: PLC0415
        _ucfg = _luc()
        return jsonify({
            "music_root":       str(MUSIC_ROOT),
            "djmt_db":          str(DJMT_DB),
            "backup_dir":       str(SAVEPOINTS_DIR),
            "archive_root":     str(ARCHIVE_ROOT),
            "quarantine":       str(QUARANTINE_DIR),
            "reports":          str(REPORTS_DIR),
            "archive_mode":     _archive_mode,
            "custom_archive":   _custom_archive,
            "archive_enabled":  ARCHIVE_ENABLED,
            "excluded_dirs":    _ucfg.get("excluded_dirs", []),
            "configured":       True,
        })
    except Exception:
        return jsonify({
            "music_root":      "",
            "djmt_db":         "",
            "backup_dir":      str(_backup_dir()),
            "archive_root":    "",
            "quarantine":      "",
            "reports":         "",
            "archive_mode":    "auto",
            "custom_archive":  "",
            "archive_enabled": True,
            "configured":      False,
        })


# ── Command routes (all return SSE streams) ───────────────────────────────────

@app.route("/api/run/audit")
def api_audit():
    cmd = [sys.executable, str(CLI_PATH), "audit"]
    # Accept either 'root'/'also_scan' (legacy) or 'paths' (pill-zone UI sends this).
    # When 'paths' is used, the first is treated as --root, the rest as --also-scan
    # so that MUSIC_ROOT is never silently substituted for user-supplied paths.
    paths = [p.strip() for p in request.args.getlist("paths") if p.strip()]
    if paths:
        cmd += ["--root", paths[0]]
        for extra in paths[1:]:
            cmd += ["--also-scan", extra]
    else:
        root = request.args.get("root", "").strip()
        if root:
            cmd += ["--root", root]
        for extra in request.args.getlist("also_scan"):
            extra = extra.strip()
            if extra:
                cmd += ["--also-scan", extra]
    library_root = paths[0] if paths else _get_library_root(request, "root")
    return _sse_response(cmd, library_root=library_root, step_name="audit")


@app.route("/api/run/process")
def api_process():
    # Accept repeated 'path' params (pill-zone UI) or a single 'path'.
    paths = [p.strip() for p in request.args.getlist("path") if p.strip()]
    if not paths:
        return jsonify({"error": "path is required"}), 400

    cmd = [sys.executable, str(CLI_PATH), "process", paths[0]]
    for extra in paths[1:]:
        cmd += ["--also-scan", extra]
    if request.args.get("no_bpm") == "1":
        cmd.append("--no-bpm")
    if request.args.get("no_key") == "1":
        cmd.append("--no-key")
    if request.args.get("no_normalize") == "1":
        cmd.append("--no-normalize")
    if request.args.get("force") == "1":
        cmd.append("--force")
    if request.args.get("dry_run") == "1":
        cmd.append("--dry-run")
    workers = request.args.get("workers", "").strip()
    if workers and workers.isdigit() and int(workers) > 1:
        cmd += ["--workers", workers]
    pause = request.args.get("pause", "").strip()
    if pause:
        try:
            if float(pause) > 0:
                cmd += ["--pause", pause]
        except ValueError:
            pass
    library_root = _get_library_root(request, "path")
    return _sse_response(cmd, library_root=library_root, step_name="process")


@app.route("/api/run/pipeline", methods=["POST"])
def api_pipeline():
    """
    Execute a user-defined sequence of steps.
    Body: {"dry_run": bool, "steps": [{"type": str, "config": {...}}, ...]}

    Supported step types and their config keys:
      organize   — source, target, mix_threshold, workers
      process    — path, workers, no_bpm, no_key, force
      normalize  — path, workers
      duplicates — path, workers, output
      prune      — (csv injected automatically from previous duplicates step)
      convert    — path, format, workers
      relocate   — old_root, new_root
    """
    body     = request.get_json(force=True, silent=True) or {}
    dry_run  = bool(body.get("dry_run", True))
    raw_steps = body.get("steps", [])

    if not raw_steps:
        return jsonify({"error": "steps list is required"}), 400

    built: list[dict] = []

    for s in raw_steps:
        stype  = s.get("type", "")
        cfg    = s.get("config", {})
        name   = s.get("name", stype)

        if stype == "organize":
            src_list = cfg.get("sources") or [cfg.get("source", "")]
            if isinstance(src_list, str):
                src_list = [src_list]
            cmd = [sys.executable, str(CLI_PATH), "organize",
                   src_list[0], cfg.get("target", "")]
            for extra in src_list[1:]:
                if extra:
                    cmd += ["--also-scan", extra]
            if not dry_run:
                cmd.append("--no-dry-run")
            org_mode = cfg.get("mode", "assimilate")
            if org_mode == "integrate":
                cmd += ["--mode", "integrate"]
            if cfg.get("mix_threshold"):
                cmd += ["--mix-threshold", str(cfg["mix_threshold"])]
            if cfg.get("workers", 1) > 1:
                cmd += ["--workers", str(cfg["workers"])]

        elif stype == "process":
            paths = cfg.get("paths") or [cfg.get("path", "")]
            if isinstance(paths, str):
                paths = [paths]
            cmd = [sys.executable, str(CLI_PATH), "process", paths[0]]
            for extra in paths[1:]:
                if extra:
                    cmd += ["--also-scan", extra]
            if cfg.get("no_bpm"):   cmd.append("--no-bpm")
            if cfg.get("no_key"):   cmd.append("--no-key")
            if cfg.get("no_normalize"): cmd.append("--no-normalize")
            if cfg.get("force"):    cmd.append("--force")
            if cfg.get("workers", 1) > 1:
                cmd += ["--workers", str(cfg["workers"])]
            if dry_run:             cmd.append("--dry-run")

        elif stype == "normalize":
            paths = cfg.get("paths") or [cfg.get("path", "")]
            if isinstance(paths, str):
                paths = [paths]
            cmd = [sys.executable, str(CLI_PATH), "process", paths[0],
                   "--no-bpm", "--no-key"]
            for extra in paths[1:]:
                if extra:
                    cmd += ["--also-scan", extra]
            if cfg.get("workers", 1) > 1:
                cmd += ["--workers", str(cfg["workers"])]
            if dry_run: cmd.append("--dry-run")

        elif stype == "duplicates":
            paths = cfg.get("paths") or [cfg.get("path", "")]
            if isinstance(paths, str):
                paths = [paths]
            cmd = [sys.executable, str(CLI_PATH), "duplicates"] + [p for p in paths if p]
            if cfg.get("workers", 1) > 1:
                cmd += ["--workers", str(cfg["workers"])]
            if cfg.get("output"):
                cmd += ["--output", cfg["output"]]

        elif stype == "prune":
            # CSV path injected at runtime from previous duplicates step output
            cmd = [sys.executable, str(CLI_PATH), "prune"]
            if dry_run: cmd.append("--dry-run")
            built.append({"name": name, "cmd": cmd, "needs_csv": True})
            continue

        elif stype == "convert":
            paths = cfg.get("paths") or [cfg.get("path", "")]
            if isinstance(paths, str):
                paths = [paths]
            cmd = [sys.executable, str(CLI_PATH), "convert",
                   paths[0], cfg.get("format", "aiff")]
            for extra in paths[1:]:
                if extra:
                    cmd += ["--also-scan", extra]
            if cfg.get("workers", 1) > 1:
                cmd += ["--workers", str(cfg["workers"])]

        elif stype == "relocate":
            cmd = [sys.executable, str(CLI_PATH), "relocate",
                   cfg.get("old_root", ""), cfg.get("new_root", "")]

        elif stype == "audit":
            cmd = [sys.executable, str(CLI_PATH), "audit"]
            _audit_paths = cfg.get("paths") or ([cfg["root"]] if cfg.get("root") else [])
            if isinstance(_audit_paths, str):
                _audit_paths = [_audit_paths]
            _audit_paths = [p for p in _audit_paths if p]
            if _audit_paths:
                cmd += ["--root", _audit_paths[0]]
                for _ap in _audit_paths[1:]:
                    cmd += ["--also-scan", _ap]

        elif stype == "import":
            paths = cfg.get("paths") or [cfg.get("path", "")]
            if isinstance(paths, str):
                paths = [paths]
            cmd = [sys.executable, str(CLI_PATH), "import", paths[0]]
            for extra in paths[1:]:
                if extra:
                    cmd += ["--also-scan", extra]
            if dry_run:
                cmd.append("--dry-run")

        elif stype == "link":
            paths = cfg.get("paths") or [cfg.get("path", "")]
            if isinstance(paths, str):
                paths = [paths]
            cmd = [sys.executable, str(CLI_PATH), "link", paths[0]]
            for extra in paths[1:]:
                if extra:
                    cmd += ["--also-scan", extra]

        elif stype == "novelty":
            sources = cfg.get("sources") or [cfg.get("source", "")]
            if isinstance(sources, str):
                sources = [sources]
            cmd = [sys.executable, str(CLI_PATH), "novelty",
                   sources[0], cfg.get("dest", "")]
            for extra in sources[1:]:
                if extra:
                    cmd += ["--also-scan", extra]
            if not dry_run:
                cmd.append("--no-dry-run")
            if cfg.get("workers", 1) > 1:
                cmd += ["--workers", str(cfg["workers"])]

        else:
            return jsonify({"error": f"Unknown step type: {stype}"}), 400

        built.append({"name": name, "cmd": cmd})

    return Response(
        _stream_pipeline(built),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/api/run/organize")
def api_organize():
    sources = [s.strip() for s in request.args.getlist("source") if s.strip()]
    target  = request.args.get("target", "").strip()
    if not sources or not target:
        return jsonify({"error": "at least one source and a target are required"}), 400

    cmd = [sys.executable, str(CLI_PATH), "organize", sources[0], target]
    for extra in sources[1:]:
        cmd += ["--also-scan", extra]

    if request.args.get("no_dry_run") == "1":
        cmd.append("--no-dry-run")

    org_mode = request.args.get("mode", "assimilate").strip()
    if org_mode == "integrate":
        cmd += ["--mode", "integrate"]

    workers = request.args.get("workers", "1").strip()
    if workers.isdigit() and int(workers) > 1:
        cmd += ["--workers", workers]

    threshold = request.args.get("mix_threshold", "").strip()
    if threshold:
        try:
            if float(threshold) > 0:
                cmd += ["--mix-threshold", threshold]
        except ValueError:
            pass

    library_root = _get_library_root(request, "target")
    return _sse_response(cmd, library_root=library_root, step_name="organize")


@app.route("/api/run/convert")
def api_convert():
    paths = [p.strip() for p in request.args.getlist("path") if p.strip()]
    format_target = request.args.get("format", "").strip()
    if not paths or not format_target:
        return jsonify({"error": "path and format are required"}), 400

    cmd = [sys.executable, str(CLI_PATH), "convert", paths[0], format_target]
    for extra in paths[1:]:
        cmd += ["--also-scan", extra]
    workers = request.args.get("workers", "1").strip()
    if workers.isdigit() and int(workers) > 1:
        cmd += ["--workers", workers]
    library_root = paths[0]
    return _sse_response(cmd, library_root=library_root, step_name="convert")


@app.route("/api/run/import")
def api_import():
    dry_run = request.args.get("dry_run") == "1"
    if not dry_run:
        err = _require_rb_closed()
        if err:
            return err

    paths = [p.strip() for p in request.args.getlist("path") if p.strip()]
    if not paths:
        return jsonify({"error": "path is required"}), 400

    cmd = [sys.executable, str(CLI_PATH), "import", paths[0]]
    for extra in paths[1:]:
        cmd += ["--also-scan", extra]
    if dry_run:
        cmd.append("--dry-run")
    if request.args.get("resume") == "1" and not dry_run:
        cmd.append("--resume")
    library_root = paths[0]
    return _sse_response(cmd, library_root=library_root, step_name="import")


@app.route("/api/run/link")
def api_link():
    err = _require_rb_closed()
    if err:
        return err

    paths = [p.strip() for p in request.args.getlist("path") if p.strip()]
    if not paths:
        return jsonify({"error": "path is required"}), 400

    dry_run_link = request.args.get("dry_run") == "1"
    cmd = [sys.executable, str(CLI_PATH), "link", paths[0]]
    for extra in paths[1:]:
        cmd += ["--also-scan", extra]
    if dry_run_link:
        cmd.append("--dry-run")
    library_root = paths[0]
    return _sse_response(cmd, library_root=library_root, step_name="link")


@app.route("/api/run/relocate")
def api_relocate():
    err = _require_rb_closed()
    if err:
        return err

    old_roots   = [r.strip() for r in request.args.getlist("old_root") if r.strip()]
    new         = request.args.get("new_root", "").strip()
    if not old_roots or not new:
        return jsonify({"error": "old_root and new_root are required"}), 400

    library_root = _get_library_root(request, "new_root")

    if len(old_roots) == 1:
        cmd = [sys.executable, str(CLI_PATH), "relocate", old_roots[0], new]
        return _sse_response(cmd, library_root=library_root, step_name="relocate")

    # Multiple old roots — chain each CLI run, only emit done after the last one
    def _multi():
        overall = 0
        for i, old in enumerate(old_roots):
            if i > 0:
                yield f"data: {json.dumps({'line': ''})}\n\n"
                yield f"data: {json.dumps({'line': f'── {i+1}/{len(old_roots)}: relocating {old}'})}\n\n"
            cmd = [sys.executable, str(CLI_PATH), "relocate", old, new]
            try:
                proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, bufsize=1, cwd=str(REPO_ROOT), env=_subprocess_env(),
                )
                with _proc_lock:
                    globals()['_active_proc'] = proc
                try:
                    for line in iter(proc.stdout.readline, ""):
                        yield f"data: {json.dumps({'line': line.rstrip()})}\n\n"
                    proc.wait()
                    if proc.returncode != 0:
                        overall = proc.returncode
                finally:
                    with _proc_lock:
                        globals()['_active_proc'] = None
            except Exception as exc:
                with _proc_lock:
                    globals()['_active_proc'] = None
                yield f"data: {json.dumps({'line': f'[SERVER ERROR] {exc}'})}\n\n"
                overall = 1
        if library_root:
            mark_step_complete(library_root, "relocate", overall)
        yield f"data: {json.dumps({'done': True, 'exit_code': overall})}\n\n"

    return Response(
        _multi(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/api/run/novelty")
def api_novelty():
    sources = [s.strip() for s in request.args.getlist("source") if s.strip()]
    dest    = request.args.get("dest", "").strip()
    if not sources or not dest:
        return jsonify({"error": "at least one source and a dest are required"}), 400

    cmd = [sys.executable, str(CLI_PATH), "novelty", sources[0], dest]
    for extra in sources[1:]:
        cmd += ["--also-scan", extra]

    if request.args.get("no_dry_run") == "1":
        cmd.append("--no-dry-run")

    workers = request.args.get("workers", "1").strip()
    if workers.isdigit() and int(workers) > 1:
        cmd += ["--workers", workers]

    library_root = _get_library_root(request, "dest")
    return _sse_response(cmd, library_root=library_root, step_name="novelty")


@app.route("/api/run/duplicates")
def api_duplicates():
    paths = [p.strip() for p in request.args.getlist("path") if p.strip()]
    if not paths:
        return jsonify({"error": "at least one path is required"}), 400

    cmd = [sys.executable, str(CLI_PATH), "duplicates"] + paths
    workers = request.args.get("workers", "").strip()
    if workers and workers.isdigit() and int(workers) > 1:
        cmd += ["--workers", workers]
    pause = request.args.get("pause", "").strip()
    if pause:
        try:
            if float(pause) > 0:
                cmd += ["--pause", pause]
        except ValueError:
            pass
    library_root = paths[0] if paths else ""
    return _sse_response(cmd, library_root=library_root, step_name="duplicates")


# ── Duplicate prune routes ────────────────────────────────────────────────────

# Short-lived server-side store for prune path lists.
# Avoids passing potentially thousands of paths as a query-string (which
# exceeds waitress's 256 KB header limit on large libraries).
_prune_token_store: dict[str, dict] = {}   # token → {paths, permanent}

# Report cache: csv_path_str → {"groups": [...], "remove_paths": [...], "keep_paths": [...]}
# Populated on first load, reused for pagination and Select All requests.
_report_cache: dict[str, dict] = {}


@app.route("/api/prune/stage", methods=["POST"])
def api_prune_stage():
    """
    Accept a JSON body {"paths": [...], "permanent": false, "csv_path": "..."} and
    return a single-use token consumed by GET /api/run/prune?token=<uuid>.

    Builds a keeper_map {remove_path → keep_path} from the cached report so that
    prune_files() can re-thread playlist references before deleting content rows.
    """
    try:
        data = request.get_json(force=True, silent=True) or {}
        paths = data.get("paths", [])
        if not isinstance(paths, list):
            return jsonify({"error": "paths must be a list"}), 400

        # Build keeper_map from the cached report (populated by /api/duplicates/load)
        keeper_map: dict[str, str] = {}
        csv_path_str = data.get("csv_path", "").strip()
        csv_path = (
            Path(csv_path_str)
            if csv_path_str
            else Path.home() / "rekordbox-toolkit" / "duplicate_report.csv"
        )
        cache_key = str(csv_path.resolve())
        cached = _report_cache.get(cache_key)
        if cached:
            for g in cached["groups"]:
                keep_entry = next((e for e in g["entries"] if e["action"] == "KEEP"), None)
                if keep_entry:
                    for e in g["entries"]:
                        if e["action"] == "REVIEW_REMOVE":
                            keeper_map[e["file_path"]] = keep_entry["file_path"]

        token = str(uuid.uuid4())
        _prune_token_store[token] = {
            "paths":      paths,
            "permanent":  bool(data.get("permanent", False)),
            "keeper_map": keeper_map,
        }
        return jsonify({"token": token, "keeper_map_size": len(keeper_map)})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/duplicates/load")
def api_duplicates_load():
    """
    Load a duplicate_report.csv, enrich with live disk + DB data,
    and return a paginated slice of groups for the prune UI.
    Falls back to the default report path if no csv_path is given.

    Query params:
      csv_path  — path to the CSV (optional)
      page      — 0-based page index (default 0)
      per_page  — groups per page (default 200)
    """
    csv_path_str = request.args.get("csv_path", "").strip()
    try:
        page     = max(0, int(request.args.get("page",     0)))
        per_page = max(1, int(request.args.get("per_page", 200)))
    except (ValueError, TypeError):
        return jsonify({"error": "page and per_page must be integers"}), 400

    csv_path = (
        Path(csv_path_str)
        if csv_path_str
        else Path.home() / "rekordbox-toolkit" / "duplicate_report.csv"
    )

    if not csv_path.exists():
        return jsonify({"error": f"Report not found: {csv_path}"}), 404

    cache_key = str(csv_path.resolve())

    try:
        if cache_key not in _report_cache:
            from pruner import load_report          # noqa: PLC0415
            from db_connection import read_db       # noqa: PLC0415
            from config import DJMT_DB as _DB      # noqa: PLC0415

            with read_db(_DB) as db:
                groups = load_report(csv_path, db)

            all_groups = [
                {
                    "group_id": g.group_id,
                    "entries": [
                        {
                            "action":         e.action,
                            "rank":           e.rank,
                            "file_path":      e.file_path,
                            "filename":       e.filename,
                            "file_size_mb":   round(e.file_size_mb, 2),
                            "bpm":            e.bpm,
                            "key":            e.key,
                            "format_ext":     e.format_ext,
                            "format_tier":    e.format_tier,
                            "exists_on_disk": e.exists_on_disk,
                            "in_db":          e.in_db,
                        }
                        for e in g.entries
                    ],
                }
                for g in groups
            ]
            remove_entries = [
                e
                for g in all_groups
                for e in g["entries"]
                if e["action"] == "REVIEW_REMOVE"
            ]
            _report_cache[cache_key] = {
                "groups":           all_groups,
                "remove_paths":     [e["file_path"] for e in remove_entries],
                "keep_paths": [
                    e["file_path"]
                    for g in all_groups
                    for e in g["entries"]
                    if e["action"] == "KEEP"
                ],
                "total_remove_mb":  round(
                    sum(e["file_size_mb"] for e in remove_entries), 1
                ),
            }

        cached      = _report_cache[cache_key]
        all_groups  = cached["groups"]
        total       = len(all_groups)
        start       = page * per_page
        page_groups = all_groups[start : start + per_page]

        return jsonify({
            "groups":           page_groups,
            "total_groups":     total,
            "total_remove":     len(cached["remove_paths"]),
            "total_remove_mb":  cached.get("total_remove_mb", 0),
            "page":             page,
            "per_page":         per_page,
            "csv_path":         str(csv_path),
        })

    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/duplicates/remove-paths")
def api_duplicates_remove_paths():
    """
    Return the full remove_paths and keep_paths lists for Select All operations.
    The report must have been loaded via /api/duplicates/load first.
    """
    csv_path_str = request.args.get("csv_path", "").strip()
    csv_path = (
        Path(csv_path_str)
        if csv_path_str
        else Path.home() / "rekordbox-toolkit" / "duplicate_report.csv"
    )
    cache_key = str(csv_path.resolve())
    if cache_key not in _report_cache:
        return jsonify({"error": "Report not loaded — call /api/duplicates/load first"}), 400
    cached = _report_cache[cache_key]
    return jsonify({
        "remove_paths": cached["remove_paths"],
        "keep_paths":   cached["keep_paths"],
    })


@app.route("/api/open-file")
def api_open_file():
    """Open a file in the system's default application (native media player)."""
    path = request.args.get("path", "").strip()
    if not path:
        return jsonify({"error": "path is required"}), 400
    p = Path(path)
    if not p.exists():
        return jsonify({"error": f"File not found: {path}"}), 404
    try:
        _sys = platform.system()
        if _sys == "Darwin":
            subprocess.Popen(["open", str(p)])
        elif _sys == "Windows":
            os.startfile(str(p))  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", str(p)])
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/run/prune")
def api_run_prune():
    """
    Execute a confirmed prune: remove DB entries + move files to Trash.
    Expects a ?token=<uuid> issued by POST /api/prune/stage.
    Returns an SSE stream of progress lines.

    All pre-flight checks run inside the worker so this endpoint always
    returns a valid SSE stream — never a bare JSON 4xx response that would
    confuse EventSource and surface only as a silent "Connection error".
    """
    token     = request.args.get("token", "")
    staged    = _prune_token_store.pop(token, {})
    paths: list[str]      = staged.get("paths", [])
    permanent: bool       = staged.get("permanent", False)
    keeper_map: dict      = staged.get("keeper_map", {})

    log_q: queue.Queue = queue.Queue()

    def _worker() -> None:
        try:
            # Pre-flight checks emitted as log lines so the browser always
            # receives a well-formed SSE stream (no bare JSON 4xx response).
            if _rb_is_running():
                log_q.put(("line", "[ERROR] Rekordbox is open — close it before pruning."))
                log_q.put(("done", 1))
                return

            if not paths:
                log_q.put(("line", "[ERROR] No files were passed to the prune endpoint."))
                log_q.put(("done", 1))
                return

            from pruner import prune_files          # noqa: PLC0415
            from db_connection import write_db      # noqa: PLC0415
            from config import DJMT_DB as _DB      # noqa: PLC0415

            with write_db(_DB) as db:
                prune_files(paths, db, log=lambda m: log_q.put(("line", m)),
                            permanent=permanent, keeper_map=keeper_map)

            _prune_root = staged.get("library_root", "")
            if not _prune_root:
                try:
                    from config import MUSIC_ROOT as _MR  # noqa: PLC0415
                    _prune_root = str(_MR)
                except Exception:
                    pass
            if _prune_root:
                mark_step_complete(_prune_root, "prune", 0)

            log_q.put(("done", 0))
        except Exception as exc:
            log_q.put(("line", f"[ERROR] {exc}"))
            log_q.put(("done", 1))

    threading.Thread(target=_worker, daemon=True).start()

    def _generate():
        while True:
            kind, val = log_q.get()
            if kind == "line":
                yield f"data: {json.dumps({'line': val})}\n\n"
            elif kind == "done":
                yield f"data: {json.dumps({'done': True, 'exit_code': val})}\n\n"
                break

    return Response(
        _generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Step state endpoint ──────────────────────────────────────────────────────

@app.route("/api/state", methods=["POST"])
def api_state():
    """Return the steps_completed dict for a given library root."""
    data = request.get_json(force=True, silent=True) or {}
    library_root = data.get("library_root", "").strip()
    if not library_root:
        return jsonify({}), 200
    return jsonify(get_step_status(library_root))


# ── Archive setup ─────────────────────────────────────────────────────────────

@app.route("/api/setup-archive", methods=["POST"])
def api_setup_archive():
    """Create the RekitBox Archive folder structure on the DJ drive."""
    try:
        from config import ensure_archive_structure  # noqa: PLC0415
        ensure_archive_structure()
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/settings", methods=["POST"])
def api_settings():
    """Save archive mode and custom path to user config."""
    try:
        from user_config import load_user_config, CONFIG_PATH  # noqa: PLC0415
        import json as _json
        data = request.get_json(force=True) or {}
        cfg  = load_user_config()
        if "archive_mode" in data:
            cfg["archive_mode"] = data["archive_mode"]
        if "custom_archive_dir" in data:
            cfg["custom_archive_dir"] = data["custom_archive_dir"]
        if "excluded_dirs" in data:
            cfg["excluded_dirs"] = [d for d in data["excluded_dirs"] if isinstance(d, str) and d.strip()]
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            _json.dump(cfg, f, indent=2)
        return jsonify({"ok": True, "note": "Restart RekitBox for changes to take effect."})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/audit/path-roots")
def api_audit_path_roots():
    """
    Quick read-only scan of all track paths in the DB.
    Returns live and dead roots with track counts.
    Used by the UI to pre-fill the Relocate form.
    """
    try:
        from audit import find_dead_roots          # noqa: PLC0415
        from db_connection import read_db          # noqa: PLC0415
        from config import DJMT_DB as _DB          # noqa: PLC0415
        with read_db(_DB) as db:
            report = find_dead_roots(db)
        return jsonify({
            "dead_roots": report.dead_roots,
            "live_roots": report.live_roots,
            "has_dead_roots": report.has_dead_roots,
        })
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ── Scan cancellation ─────────────────────────────────────────────────────────

@app.route("/api/cancel", methods=["POST"])
def api_cancel():
    """Send SIGTERM to the active subprocess (graceful interrupt / checkpoint)."""
    with _proc_lock:
        proc = _active_proc
    if proc is None:
        return jsonify({"ok": False, "error": "No active scan"}), 404
    proc.terminate()
    return jsonify({"ok": True})


@app.route("/api/cancel/force", methods=["POST"])
def api_cancel_force():
    """Send SIGKILL to the active subprocess (emergency stop — server stays running)."""
    with _proc_lock:
        proc = _active_proc
    if proc is None:
        return jsonify({"ok": False, "error": "No active scan"}), 404
    proc.kill()
    return jsonify({"ok": True})


# ── RekitBox update route ─────────────────────────────────────────────────────

@app.route("/api/update/status")
def api_update_status():
    """Return the cached GitHub release check result (never blocks)."""
    return jsonify(_update_get_status())


# ── Homebrew update routes ────────────────────────────────────────────────────

@app.route("/api/brew/status")
def api_brew_status():
    """Return the cached brew-outdated status (never blocks)."""
    return jsonify(_brew_get_status())


@app.route("/api/brew/check", methods=["POST"])
def api_brew_check():
    """Trigger an immediate brew-outdated check and return the result."""
    status = _brew_check_now()
    return jsonify(status)


@app.route("/api/run/brew-upgrade")
def api_brew_upgrade():
    """
    SSE stream of ``brew upgrade <packages>`` for the packages RekitBox uses.
    Only upgrades known-outdated packages reported by the last cached check.
    """
    outdated = _brew_get_status().get("outdated", [])
    names    = [p["name"] for p in outdated if p.get("name")]
    if not names:
        # Nothing to do — return an immediate SSE end event
        def _nothing():
            yield "data: No outdated RekitBox packages found.\n\n"
            yield "data: [DONE]\n\n"
        return Response(_nothing(), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    cmd = ["brew", "upgrade"] + names
    return _sse_response(cmd)


# ── Folder path resolution ────────────────────────────────────────────────────

@app.route("/api/finder-selection")
def api_finder_selection():
    """Return the path of the currently selected item in Finder.

    When a user drags a folder from Finder and drops it in RekitBox, Finder
    keeps the dragged item selected after the drop.  Querying that selection
    immediately gives us the exact path the WebView security model withholds —
    no dialog, no double navigation.

    Falls back to the native folder picker if Finder has nothing selected
    (e.g. the drag came from a non-Finder source).
    Returns {"path": null} if the picker is cancelled.
    """
    from flask import request as _req
    source = _req.args.get("source", "")  # "drop" → skip picker fallback

    _finder_script = """\
tell application "Finder"
    set sel to selection
    if (count of sel) > 0 then
        return POSIX path of (item 1 of sel as alias)
    end if
end tell"""
    # 60 s — long enough to survive a macOS "Allow access to external drives"
    # permission dialog without timing out before the user can respond.
    try:
        r = subprocess.run(
            ["osascript", "-e", _finder_script],
            capture_output=True, text=True, timeout=60,
        )
        print(f"[finder-selection] rc={r.returncode} stdout={repr(r.stdout)} stderr={repr(r.stderr)}", flush=True)
        if r.returncode == 0 and r.stdout.strip():
            return jsonify({"path": r.stdout.strip().rstrip("/")})
    except Exception as exc:
        print(f"[finder-selection] exception: {exc}", flush=True)

    # When called from a drag-drop event, pywebview may focus before osascript
    # runs and Finder clears its selection — return null silently rather than
    # opening a picker dialog the user didn't ask for.
    if source == "drop":
        print(f"[finder-selection] source=drop, returning null", flush=True)
        return jsonify({"path": None})

    # Nothing selected in Finder — open the native picker as fallback
    try:
        r = subprocess.run(
            ["osascript", "-e", "POSIX path of (choose folder)"],
            capture_output=True, text=True, timeout=120,
        )
        if r.returncode == 0:
            return jsonify({"path": r.stdout.strip().rstrip("/")})
    except Exception:
        pass

    return jsonify({"path": None})


@app.route("/api/pick-folder")
def api_pick_folder():
    """Open the native macOS folder-chooser dialog (Browse button).

    Used by the Browse buttons in each folder zone.  Always shows the dialog —
    does not attempt to read Finder's selection.
    Returns {"path": null} if cancelled.
    """
    try:
        result = subprocess.run(
            ["osascript", "-e", "POSIX path of (choose folder)"],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode == 0:
            return jsonify({"path": result.stdout.strip().rstrip("/")})
    except Exception:
        pass
    return jsonify({"path": None})


@app.route("/api/fs/list")
def api_fs_list():
    """Lightweight directory listing for the in-app file browser panel.

    Returns folders first (sorted), then audio files, then everything else.
    Hidden files (dot-prefixed) are always omitted.
    """
    AUDIO_EXTS = {'.aiff', '.aif', '.wav', '.flac', '.mp3', '.m4a', '.alac', '.ogg', '.opus', '.mp4'}
    path_str = request.args.get("path", "/Volumes")
    p = Path(path_str)
    if not p.exists() or not p.is_dir():
        return jsonify({"error": f"Not a directory: {path_str}"}), 400
    try:
        entries = []
        for item in sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
            if item.name.startswith('.'):
                continue
            is_dir = item.is_dir()
            entries.append({
                "name":     item.name,
                "path":     str(item),
                "is_dir":   is_dir,
                "is_audio": not is_dir and item.suffix.lower() in AUDIO_EXTS,
            })
    except PermissionError:
        return jsonify({"error": "Permission denied"}), 403
    return jsonify({
        "path":    str(p),
        "parent":  str(p.parent) if str(p) != str(p.parent) else None,
        "entries": entries,
    })


# ── Quit ──────────────────────────────────────────────────────────────────────

_REKITBOX_STATE = Path.home() / ".rekordbox-toolkit" / "rekitbox-state.json"


@app.route("/api/setup-status")
def api_setup_status():
    """Return whether the welcome wizard has been completed and saved permissions.

    Backed by ~/.rekordbox-toolkit/rekitbox-state.json so state survives across
    pywebview sessions regardless of WKWebView localStorage behaviour.
    """
    try:
        if _REKITBOX_STATE.exists():
            state = json.loads(_REKITBOX_STATE.read_text())
            return jsonify({
                "setup_complete": bool(state.get("setup_complete")),
                "db_read":        state.get("db_read"),
                "db_write":       state.get("db_write"),
            })
    except Exception:
        pass
    return jsonify({"setup_complete": False, "db_read": None, "db_write": None})


@app.route("/api/setup-complete", methods=["POST"])
def api_setup_complete():
    """Persist welcome-wizard completion and permission choices server-side."""
    try:
        data  = request.get_json(silent=True) or {}
        state = {
            "setup_complete": True,
            "db_read":  data.get("db_read"),
            "db_write": data.get("db_write"),
        }
        _REKITBOX_STATE.parent.mkdir(parents=True, exist_ok=True)
        _REKITBOX_STATE.write_text(json.dumps(state, indent=2) + "\n")
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/config/set-music-root", methods=["POST"])
def api_set_music_root():
    """Update music_root in ~/.rekordbox-toolkit/config.json and return the new value."""
    try:
        from user_config import load_user_config, save_user_config  # noqa: PLC0415
        data = request.get_json(silent=True) or {}
        path = str(data.get("path", "")).strip()
        if not path:
            return jsonify({"error": "path is required"}), 400
        cfg = load_user_config()
        cfg["music_root"] = path
        save_user_config(cfg)
        return jsonify({"ok": True, "music_root": path})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/migrate-pioneer-db")
def api_migrate_pioneer_db():
    """Stream progress of migrating ~/Library/Pioneer/rekordbox/ to the target drive."""
    from flask import request as _req
    target = _req.args.get("target", "").strip()
    if not target:
        return jsonify({"error": "target parameter required"}), 400
    from db_migrator import migrate
    return Response(migrate(target), mimetype="text/event-stream")


@app.route("/api/quit", methods=["POST"])
def api_quit():
    """Shut the server down cleanly after sending the response."""
    def _shutdown():
        import time
        time.sleep(0.4)          # let the response reach the browser
        os.kill(os.getpid(), signal.SIGTERM)
    threading.Thread(target=_shutdown, daemon=True).start()
    return jsonify({"ok": True})


# ── Entry point ───────────────────────────────────────────────────────────────



# ── Analysis job state ────────────────────────────────────────────────────────
# In-memory only — analysis jobs don't need to survive restarts.
# Each entry: { job_id, track_ids, status, results: { track_id: {...} } }
_ANALYSIS_JOBS: dict[str, dict] = {}
_ANALYSIS_LOCK: threading.Lock = threading.Lock()


_EXPORT_JOBS: dict[str, dict] = {}
_EXPORT_LOCK: threading.Lock = threading.Lock()



# ── Mobile auth ───────────────────────────────────────────────────────────────

def _get_mobile_token() -> str:
    """
    Return the RekitGo Bearer token, generating it if absent.

    Token is persisted in ~/.rekordbox-toolkit/config.json under "mobile_token".
    Printed to console once on first generation so the user can copy it to the app.
    Returns empty string if RekitBox hasn't been configured yet (safe — auth
    middleware returns 503 in that case rather than silently accepting requests).
    """
    try:
        from user_config import load_user_config, save_user_config, config_exists  # noqa: PLC0415
        if not config_exists():
            return ""
        cfg = load_user_config()
        if not cfg.get("mobile_token"):
            cfg["mobile_token"] = str(uuid.uuid4())
            save_user_config(cfg)
            print()
            print("  ┌──────────────────────────────────────────────────────────┐")
            print(f"  │  REKITGO TOKEN: {cfg['mobile_token']}  │")
            print("  │  Copy this into RekitGo → Settings → Auth Token        │")
            print("  └──────────────────────────────────────────────────────────┘")
            print()
        return cfg["mobile_token"]
    except Exception:
        return ""


MOBILE_TOKEN: str = _get_mobile_token()


@app.before_request
def _check_mobile_auth():
    """
    Require Bearer token for all /api/mobile/* routes except /api/mobile/ping.
    Desktop routes (/, /api/status, /api/run/*, etc.) are unaffected — they are
    already only reachable on localhost so no auth is needed there.
    """
    if not request.path.startswith("/api/mobile/"):
        return
    if request.path == "/api/mobile/ping":
        return
    if not MOBILE_TOKEN:
        return jsonify({
            "error": "server_not_configured",
            "message": "Run: python3 cli.py setup",
        }), 503
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer ") or auth[7:] != MOBILE_TOKEN:
        return jsonify({"error": "unauthorized"}), 401

# ── Mobile API routes ────────────────────────────────────────────────────────

@app.route("/api/mobile/ping")
def mobile_ping():
    """
    Health check for RekitGo. No auth required.
    Used by the app on startup to confirm network reachability before attempting
    authenticated calls.
    """
    return jsonify({"status": "ok", "version": "1.0.0", "rekitbox_version": "1.0.9"})


@app.route("/api/mobile/folders")
def mobile_folders():
    """
    List configured download folders with file counts.

    Reads "download_folders" from ~/.rekordbox-toolkit/config.json.
    Returns an empty list if the key is absent — the user adds folders
    via the Settings tab.
    """
    try:
        from user_config import load_user_config  # noqa: PLC0415
        cfg = load_user_config()
        folders = cfg.get("download_folders", [])
    except Exception:
        folders = []

    result = []
    for folder_path in folders:
        p = Path(folder_path)
        if not p.is_dir():
            continue
        try:
            file_count = sum(
                1 for f in p.iterdir()
                if f.is_file() and not f.name.startswith(".")
            )
        except PermissionError:
            file_count = 0
        result.append({
            "name":       p.name,
            "path":       str(p),
            "file_count": file_count,
        })
    return jsonify(result)


@app.route("/api/mobile/folders/<path:folder_path>/files")
def mobile_folder_files(folder_path: str):
    """
    List audio files in a specific folder.

    folder_path is URL-encoded by the client and decoded by Flask's <path:>
    converter. We resolve it to an absolute path and validate it exists.
    """
    import datetime  # noqa: PLC0415
    p = Path("/" + folder_path) if not folder_path.startswith("/") else Path(folder_path)

    if not p.is_dir():
        return jsonify({"error": "folder_not_found"}), 404

    audio_extensions = {
        ".mp3", ".wav", ".aiff", ".aif", ".flac", ".m4a", ".ogg", ".opus",
    }

    files = []
    try:
        for f in sorted(p.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
            if not f.is_file():
                continue
            if f.suffix.lower() not in audio_extensions:
                continue
            if f.name.startswith("."):
                continue
            stat = f.stat()
            files.append({
                "name":       f.name,
                "path":       str(f),
                "size_bytes": stat.st_size,
                "modified":   datetime.datetime.fromtimestamp(
                    stat.st_mtime, tz=datetime.timezone.utc
                ).isoformat(),
            })
    except PermissionError:
        return jsonify({"error": "permission_denied"}), 403

    return jsonify(files)


@app.route("/api/mobile/download", methods=["POST"])
def mobile_download():
    """
    Enqueue a download job.

    Body: { "url": "...", "destination": "/Music/New Drops/", "filename": "optional" }
    Response: { "job_id": "uuid" }

    The download runs asynchronously. Progress and completion are pushed to all
    connected WebSocket clients via /api/mobile/events.
    """
    import downloader  # noqa: PLC0415
    body = request.get_json(force=True, silent=True) or {}
    url = (body.get("url") or "").strip()
    destination = (body.get("destination") or "").strip()
    filename = (body.get("filename") or "").strip() or None

    if not url:
        return jsonify({"error": "url is required"}), 400
    if not destination:
        return jsonify({"error": "destination is required"}), 400

    job_id = downloader.enqueue(url, destination, filename)
    return jsonify({"job_id": job_id}), 202


@app.route("/api/mobile/jobs")
def mobile_jobs():
    """Return all download jobs, newest first (capped at 200)."""
    import downloader  # noqa: PLC0415
    return jsonify(downloader.get_all_jobs())


@app.route("/api/mobile/jobs/<job_id>")
def mobile_job(job_id: str):
    """Return a single job by ID."""
    import downloader  # noqa: PLC0415
    job = downloader.get_job(job_id)
    if job is None:
        return jsonify({"error": "not_found"}), 404
    return jsonify(job)


@app.route("/api/mobile/rekordbox/tracks")
def mobile_rekordbox_tracks():
    """
    List tracks in the Rekordbox database.

    Query params:
      search  — filter by title or artist (case-insensitive, optional)
      sort    — "date_added" (default) | "title" | "artist" | "bpm"
      limit   — max results (default 200)
      offset  — pagination offset (default 0)

    Returns a JSON array of track objects.
    """
    import datetime  # noqa: PLC0415
    from db_connection import read_db  # noqa: PLC0415
    from config import DJMT_DB as _DB  # noqa: PLC0415

    search = request.args.get("search", "").strip().lower()
    sort   = request.args.get("sort", "date_added")
    limit  = int(request.args.get("limit", 200))
    offset = int(request.args.get("offset", 0))

    try:
        with read_db(_DB) as db:
            rows = list(db.get_content())

        results = []
        for t in rows:
            title  = t.Title or ""
            artist = t.Artist.Name if t.Artist else ""
            path   = t.FolderPath or ""

            # Skip streaming / non-local tracks (no real file path)
            if not path or not path.startswith("/"):
                continue

            if search and search not in title.lower() and search not in artist.lower():
                continue

            bpm = round(t.BPM / 100, 1) if t.BPM else None
            key = t.Key.Name if t.Key else None

            # StockDate is a date object; DateCreated is often 1969 (epoch artefact)
            date_added = None
            sd = t.StockDate
            if sd and isinstance(sd, (datetime.date, datetime.datetime)):
                try:
                    date_added = sd.isoformat()
                except Exception:
                    pass

            results.append({
                "id":         str(t.ID),
                "title":      title,
                "artist":     artist,
                "bpm":        bpm,
                "key":        key,
                "duration_ms": (t.Length * 1000) if t.Length else None,
                "file_path":  path,
                "date_added": date_added,
            })

        # Sort
        if sort == "title":
            results.sort(key=lambda r: r["title"].lower())
        elif sort == "artist":
            results.sort(key=lambda r: r["artist"].lower())
        elif sort == "bpm":
            results.sort(key=lambda r: r["bpm"] or 0, reverse=True)
        else:  # date_added (most recent first)
            results.sort(key=lambda r: r["date_added"] or "", reverse=True)

        return jsonify(results[offset: offset + limit])

    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/mobile/rekordbox/tracks", methods=["POST"])
def mobile_rekordbox_add_track():
    """
    Add a local audio file to the Rekordbox database.

    Body: { "file_path": "/absolute/path/to/track.mp3" }
    Response: { "track_id": "123456", "status": "added" }
              or { "track_id": "123456", "status": "already_exists" } (409)

    Requires Rekordbox to be closed (write_db enforces this).
    """
    import datetime  # noqa: PLC0415
    from pathlib import Path as _Path  # noqa: PLC0415
    from db_connection import write_db  # noqa: PLC0415
    from config import DJMT_DB as _DB  # noqa: PLC0415

    data = request.get_json(silent=True) or {}
    file_path = data.get("file_path", "").strip()

    if not file_path:
        return jsonify({"error": "file_path required"}), 400

    p = _Path(file_path)
    if not p.exists():
        return jsonify({"error": f"File not found: {file_path}"}), 404

    AUDIO_EXTS = {".mp3", ".wav", ".aiff", ".aif", ".flac", ".m4a", ".ogg", ".opus"}
    if p.suffix.lower() not in AUDIO_EXTS:
        return jsonify({"error": f"Unsupported file type: {p.suffix}"}), 400

    try:
        with write_db(_DB) as db:
            track = db.add_content(file_path)
            db.flush()
            track_id = str(track.ID)
        return jsonify({"track_id": track_id, "status": "added"}), 201

    except ValueError as exc:
        # "already exists in database"
        if "already exists" in str(exc):
            # Look up the existing ID to return it
            try:
                from db_connection import read_db as _read  # noqa: PLC0415
                with _read(_DB) as db:
                    existing = list(db.get_content())
                    match = next((t for t in existing if t.FolderPath == file_path), None)
                    existing_id = str(match.ID) if match else "unknown"
                return jsonify({"track_id": existing_id, "status": "already_exists"}), 409
            except Exception:
                return jsonify({"track_id": "unknown", "status": "already_exists"}), 409
        return jsonify({"error": str(exc)}), 400

    except RuntimeError as exc:
        # Rekordbox is running
        return jsonify({"error": str(exc)}), 503

    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ── Export job state ─────────────────────────────────────────────────────────
# In-memory export jobs. Each entry:
# { job_id, status, tracks_total, tracks_done, current_track, errors: [] }
_EXPORT_JOBS: dict[str, dict] = {}
_EXPORT_LOCK: threading.Lock = threading.Lock()

# ── Track analysis ────────────────────────────────────────────────────────────

def _push_analysis_event(
    job_id: str,
    track_id: str,
    status: str,
    bpm: "float | None" = None,
    key: "str | None" = None,
    error: "str | None" = None,
) -> None:
    """Push a WebSocket analysis_update event to all connected RekitGo clients."""
    try:
        import ws_bus  # noqa: PLC0415
        ws_bus.broadcast(json.dumps({
            "type":     "analysis_update",
            "job_id":   job_id,
            "track_id": track_id,
            "status":   status,
            "bpm":      bpm,
            "key":      key,
            "error":    error,
        }))
    except Exception:
        pass  # WS push is best-effort; never block analysis thread


def _run_analysis(job_id: str, track_ids: list) -> None:
    """
    Background thread: detect BPM and key for each track, write results
    to file tags and to the Rekordbox DB.

    For each track:
      1. Fetch file path from DB (read-only).
      2. Run process_file() — writes BPM/key to file tags via mutagen.
      3. Try write_db() to update DjmdContent.BPM and .KeyID.
         If Rekordbox is running, the DB update is skipped (tags still written).
    """
    from pathlib import Path as _Path  # noqa: PLC0415
    from audio_processor import process_file  # noqa: PLC0415
    from db_connection import read_db, write_db  # noqa: PLC0415
    from key_mapper import resolve_key_id  # noqa: PLC0415
    from config import DJMT_DB as _DB  # noqa: PLC0415

    for track_id in track_ids:
        # ── mark as analyzing ──────────────────────────────────────────────────
        with _ANALYSIS_LOCK:
            _ANALYSIS_JOBS[job_id]["results"][track_id]["status"] = "analyzing"
        _push_analysis_event(job_id, track_id, "analyzing")

        bpm: "float | None" = None
        key: "str | None" = None
        db_note: "str | None" = None

        try:
            # 1. Resolve file path
            with read_db(_DB) as db:
                row = db.get_content(ID=track_id).one_or_none()
                if row is None:
                    raise ValueError(f"Track {track_id} not found in DB")
                file_path = row.FolderPath or ""
                if not file_path or not file_path.startswith("/"):
                    raise ValueError(f"Track {track_id} has no local file path")

            # 2. Run analysis — BPM + key detection; loudness normalisation OFF
            p = _Path(file_path)
            result = process_file(
                p,
                detect_bpm=True,
                detect_key=True,
                normalise=False,
                force=False,   # skip if tags already exist
            )
            bpm = result.bpm_detected
            key = result.key_detected   # Camelot notation or None

            # 3. Update Rekordbox DB
            try:
                with write_db(_DB) as db:
                    row = db.get_content(ID=track_id).one_or_none()
                    if row:
                        if bpm is not None:
                            row.BPM = int(round(bpm * 100))
                        if key is not None:
                            kid = resolve_key_id(key, db)
                            if kid:
                                row.KeyID = kid
                    db.commit()
            except RuntimeError:
                # Rekordbox is running — tags were written to file, DB update deferred
                db_note = "DB not updated (Rekordbox is open); file tags written."

            status = "complete"

        except Exception as exc:
            status = "failed"
            db_note = str(exc)

        # ── record result ──────────────────────────────────────────────────────
        with _ANALYSIS_LOCK:
            _ANALYSIS_JOBS[job_id]["results"][track_id].update({
                "status": status,
                "bpm":    bpm,
                "key":    key,
                "error":  db_note,
            })
        _push_analysis_event(job_id, track_id, status, bpm=bpm, key=key, error=db_note)

    # Job complete
    with _ANALYSIS_LOCK:
        _ANALYSIS_JOBS[job_id]["status"] = "complete"


@app.route("/api/mobile/rekordbox/analyze", methods=["POST"])
def mobile_rekordbox_analyze():
    """
    Queue BPM + key analysis for one or more Rekordbox tracks.

    Body: { "track_ids": ["123456", "789012"] }
    Response: { "job_id": "uuid" }  (202 Accepted)

    Analysis runs in a background thread.  Poll GET /analyze/<job_id> for status,
    or listen for "analysis_update" WebSocket events.
    """
    data      = request.get_json(silent=True) or {}
    track_ids = data.get("track_ids") or []

    if not isinstance(track_ids, list) or not track_ids:
        return jsonify({"error": "track_ids must be a non-empty list"}), 400

    # Enforce reasonable batch limit
    track_ids = [str(t) for t in track_ids[:50]]

    job_id = str(uuid.uuid4())

    with _ANALYSIS_LOCK:
        _ANALYSIS_JOBS[job_id] = {
            "job_id":    job_id,
            "track_ids": track_ids,
            "status":    "running",
            "results":   {tid: {"status": "queued", "bpm": None, "key": None, "error": None}
                          for tid in track_ids},
        }

    t = threading.Thread(
        target=_run_analysis,
        args=(job_id, track_ids),
        daemon=True,
        name=f"analysis-{job_id[:8]}",
    )
    t.start()

    return jsonify({"job_id": job_id}), 202


@app.route("/api/mobile/rekordbox/analyze/<job_id>")
def mobile_rekordbox_analyze_status(job_id: str):
    """
    Poll analysis job status.

    Response: { job_id, status, results: { track_id: { status, bpm, key, error } } }
    """
    with _ANALYSIS_LOCK:
        job = _ANALYSIS_JOBS.get(job_id)

    if job is None:
        return jsonify({"error": "Job not found"}), 404

    return jsonify(job)


# ── Rekordbox playlists ────────────────────────────────────────────────────────

@app.route("/api/mobile/rekordbox/playlists")
def mobile_rekordbox_playlists():
    """
    List all non-folder playlists with track count.
    Returns: [ { id, name, track_count }, ... ] sorted alphabetically.
    """
    from db_connection import read_db  # noqa: PLC0415
    from config import DJMT_DB as _DB  # noqa: PLC0415

    try:
        with read_db(_DB) as db:
            rows = db.get_playlist().all()
            result = []
            for pl in rows:
                if pl.Attribute != 0:   # 0 = regular playlist; 1 = folder; 4 = smart
                    continue
                songs = db.get_playlist_songs(PlaylistID=pl.ID).all()
                result.append({
                    "id":          str(pl.ID),
                    "name":        pl.Name or "",
                    "track_count": len(songs),
                })
            result.sort(key=lambda p: p["name"].lower())
            return jsonify(result)

    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/mobile/rekordbox/playlists", methods=["POST"])
def mobile_rekordbox_create_playlist():
    """
    Create a new playlist at the root level.
    Body: { "name": "My Playlist" }
    Response: { "playlist_id": "123456" }  (201)
    """
    from db_connection import write_db  # noqa: PLC0415
    from config import DJMT_DB as _DB   # noqa: PLC0415

    data = request.get_json(silent=True) or {}
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400

    try:
        with write_db(_DB) as db:
            pl = db.create_playlist(name)
            db.commit()
            return jsonify({"playlist_id": str(pl.ID)}), 201

    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 503

    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/mobile/rekordbox/playlists/<playlist_id>")
def mobile_rekordbox_playlist(playlist_id: str):
    """
    Get a single playlist with its ordered track list.
    Response: { id, name, track_count, tracks: [ Track, ... ] }
    """
    import datetime  # noqa: PLC0415
    from db_connection import read_db  # noqa: PLC0415
    from config import DJMT_DB as _DB  # noqa: PLC0415

    try:
        with read_db(_DB) as db:
            pl = db.get_playlist(ID=playlist_id).one_or_none()
            if pl is None:
                return jsonify({"error": "Playlist not found"}), 404

            songs = (
                db.get_playlist_songs(PlaylistID=pl.ID)
                  .order_by("TrackNo")
                  .all()
            )

            tracks = []
            for song in songs:
                t = song.Content
                if t is None:
                    continue

                date_added = None
                sd = t.StockDate
                if sd and isinstance(sd, (datetime.date, datetime.datetime)):
                    try:
                        date_added = sd.isoformat()
                    except Exception:
                        pass

                tracks.append({
                    "id":          str(t.ID),
                    "title":       t.Title or "",
                    "artist":      t.Artist.Name if t.Artist else "",
                    "bpm":         round(t.BPM / 100, 1) if t.BPM else None,
                    "key":         t.Key.Name if t.Key else None,
                    "duration_ms": (t.Length * 1000) if t.Length else None,
                    "file_path":   t.FolderPath or "",
                    "date_added":  date_added,
                    "track_no":    song.TrackNo,
                })

            return jsonify({
                "id":          str(pl.ID),
                "name":        pl.Name or "",
                "track_count": len(tracks),
                "tracks":      tracks,
            })

    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/mobile/rekordbox/playlists/<playlist_id>", methods=["PUT"])
def mobile_rekordbox_rename_playlist(playlist_id: str):
    """
    Rename a playlist.
    Body: { "name": "New Name" }
    """
    from db_connection import write_db  # noqa: PLC0415
    from config import DJMT_DB as _DB   # noqa: PLC0415

    data = request.get_json(silent=True) or {}
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400

    try:
        with write_db(_DB) as db:
            pl = db.get_playlist(ID=playlist_id).one_or_none()
            if pl is None:
                return jsonify({"error": "Playlist not found"}), 404

            db.rename_playlist(pl, name)
            db.commit()
            return jsonify({"status": "ok"})

    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 503

    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/mobile/rekordbox/playlists/<playlist_id>", methods=["DELETE"])
def mobile_rekordbox_delete_playlist(playlist_id: str):
    """
    Delete a playlist (does not delete the tracks themselves).
    """
    from db_connection import write_db  # noqa: PLC0415
    from config import DJMT_DB as _DB   # noqa: PLC0415

    try:
        with write_db(_DB) as db:
            pl = db.get_playlist(ID=playlist_id).one_or_none()
            if pl is None:
                return jsonify({"error": "Playlist not found"}), 404

            db.delete_playlist(pl)
            db.commit()
            return jsonify({"status": "deleted"})

    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 503

    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/mobile/rekordbox/playlists/<playlist_id>/tracks", methods=["POST"])
def mobile_rekordbox_add_to_playlist(playlist_id: str):
    """
    Append a track to a playlist.
    Body: { "track_id": "123456" }
    """
    from db_connection import write_db  # noqa: PLC0415
    from config import DJMT_DB as _DB   # noqa: PLC0415

    data    = request.get_json(silent=True) or {}
    track_id = str(data.get("track_id", "")).strip()
    if not track_id:
        return jsonify({"error": "track_id required"}), 400

    try:
        with write_db(_DB) as db:
            pl = db.get_playlist(ID=playlist_id).one_or_none()
            if pl is None:
                return jsonify({"error": "Playlist not found"}), 404

            track = db.get_content(ID=track_id).one_or_none()
            if track is None:
                return jsonify({"error": "Track not found"}), 404

            db.add_to_playlist(pl, track, track_no=None)   # appends to end
            db.commit()
            return jsonify({"status": "added"}), 201

    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 503

    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route(
    "/api/mobile/rekordbox/playlists/<playlist_id>/tracks/<track_id>",
    methods=["DELETE"],
)
def mobile_rekordbox_remove_from_playlist(playlist_id: str, track_id: str):
    """
    Remove a track from a playlist.
    Does not delete the track from the library.
    """
    from db_connection import write_db  # noqa: PLC0415
    from config import DJMT_DB as _DB   # noqa: PLC0415

    try:
        with write_db(_DB) as db:
            song = db.get_playlist_songs(
                PlaylistID=playlist_id, ContentID=track_id
            ).one_or_none()
            if song is None:
                return jsonify({"error": "Track not in playlist"}), 404

            db.remove_from_playlist(playlist_id, song.ID)
            db.commit()
            return jsonify({"status": "removed"})

    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 503

    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ── Drive detection & USB export ─────────────────────────────────────────────

@app.route("/api/mobile/drives")
def mobile_drives():
    """
    List mounted Pioneer-compatible drives.

    A drive qualifies if it has a PIONEER/Master/master.db file — same format
    as the main Rekordbox library (Rekordbox6Database / SQLite).

    Returns: [ { path, name, free_bytes, total_bytes, pioneer } ]
    """
    try:
        import psutil  # noqa: PLC0415
        drives = []
        for part in psutil.disk_partitions():
            mp = part.mountpoint
            if not mp.startswith("/Volumes"):
                continue
            try:
                usage      = psutil.disk_usage(mp)
                name       = Path(mp).name
                pioneer_db = Path(mp) / "PIONEER" / "Master" / "master.db"
                drives.append({
                    "path":       mp,
                    "name":       name,
                    "free_bytes":  usage.free,
                    "total_bytes": usage.total,
                    "pioneer":     pioneer_db.exists(),
                })
            except (PermissionError, OSError):
                continue
        return jsonify(drives)

    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


def _run_export(job_id: str, playlist_ids: list, drive_path: str) -> None:
    """
    Background thread: export selected playlists from the main Rekordbox DB
    to the Pioneer USB drive's master.db.

    For each track in the selected playlists:
      1. If the track already exists in the USB DB (by FolderPath), skip it.
      2. Otherwise add it via db.add_content(file_path).
    Then create/update playlists in the USB DB and link all tracks.

    Both databases use the Rekordbox6Database (SQLite) format — no .pdb writes.
    The USB master.db is backed up before any writes.
    """
    import shutil as _shutil  # noqa: PLC0415
    from pathlib import Path as _Path  # noqa: PLC0415
    from db_connection import read_db  # noqa: PLC0415
    from config import DJMT_DB as _DB  # noqa: PLC0415
    from pyrekordbox import Rekordbox6Database  # noqa: PLC0415
    import ws_bus as _ws  # noqa: PLC0415

    def _push(update: dict) -> None:
        try:
            _ws.broadcast(json.dumps({"type": "export_update", "job_id": job_id, **update}))
        except Exception:
            pass

    def _update(patch: dict) -> None:
        with _EXPORT_LOCK:
            _EXPORT_JOBS[job_id].update(patch)
        _push(patch)

    usb_db_path = _Path(drive_path) / "PIONEER" / "Master" / "master.db"

    try:
        # ── 1. Validate USB DB ─────────────────────────────────────────────────
        if not usb_db_path.exists():
            _update({"status": "failed", "errors": ["PIONEER/Master/master.db not found on drive"]})
            return

        # ── 2. Backup USB DB ──────────────────────────────────────────────────
        backup_path = usb_db_path.with_suffix(".export_backup.db")
        _shutil.copy2(str(usb_db_path), str(backup_path))

        # ── 3. Load source playlists + tracks ────────────────────────────────
        tracks_by_playlist: dict[str, list] = {}   # playlist_name → [content_row]
        all_tracks: dict[str, object] = {}          # track_id → content_row (deduplicated)

        with read_db(_DB) as src:
            for pl_id in playlist_ids:
                pl = src.get_playlist(ID=pl_id).one_or_none()
                if pl is None:
                    continue
                songs = src.get_playlist_songs(PlaylistID=pl.ID).order_by("TrackNo").all()
                tracks_in_playlist = []
                for song in songs:
                    t = song.Content
                    if t is None:
                        continue
                    path = t.FolderPath or ""
                    if not path or not path.startswith("/"):
                        continue  # skip cloud/streaming tracks
                    if not _Path(path).exists():
                        continue  # skip tracks whose files aren't accessible
                    all_tracks[str(t.ID)] = t
                    tracks_in_playlist.append(t)
                tracks_by_playlist[pl.Name or f"Playlist {pl_id}"] = tracks_in_playlist

        total = sum(len(v) for v in tracks_by_playlist.values())
        _update({"tracks_total": total, "tracks_done": 0, "status": "running"})

        if total == 0:
            _update({"status": "complete", "tracks_done": 0})
            return

        # ── 4. Open USB DB for writing ────────────────────────────────────────
        usb = Rekordbox6Database(str(usb_db_path))

        try:
            # Build index of existing paths in USB DB for fast dedup check
            existing_paths: set[str] = set()
            for row in usb.get_content().all():
                if row.FolderPath:
                    existing_paths.add(row.FolderPath)

            # path → USB content row (populated as we add tracks)
            path_to_usb_row: dict[str, object] = {}
            for row in usb.get_content().all():
                if row.FolderPath:
                    path_to_usb_row[row.FolderPath] = row

            done = 0
            errors = []

            # ── 5. Add missing tracks ────────────────────────────────────────
            # Process deduplicated set first so each file is added at most once
            for src_row in all_tracks.values():
                fp = src_row.FolderPath
                _update({"current_track": src_row.Title or fp})

                if fp not in existing_paths:
                    try:
                        usb_row = usb.add_content(fp)
                        usb.flush()
                        path_to_usb_row[fp] = usb_row
                        existing_paths.add(fp)
                    except Exception as exc:
                        errors.append(f"{Path(fp).name}: {exc}")

                done += 1
                _update({"tracks_done": done})

            # Commit all new content rows in one go
            usb.commit()

            # ── 6. Create / update playlists in USB DB ───────────────────────
            for pl_name, src_tracks in tracks_by_playlist.items():
                # Find or create the playlist
                existing_pl = usb.get_playlist(Name=pl_name).one_or_none()
                if existing_pl is None:
                    usb_pl = usb.create_playlist(pl_name)
                    usb.flush()
                else:
                    usb_pl = existing_pl

                # Build set of content IDs already in this USB playlist
                already_linked: set[str] = {
                    str(s.ContentID)
                    for s in usb.get_playlist_songs(PlaylistID=usb_pl.ID).all()
                }

                for src_row in src_tracks:
                    usb_row = path_to_usb_row.get(src_row.FolderPath)
                    if usb_row is None:
                        continue
                    if str(usb_row.ID) not in already_linked:
                        try:
                            usb.add_to_playlist(usb_pl, usb_row, track_no=None)
                        except Exception as exc:
                            errors.append(f"Link {Path(src_row.FolderPath).name}: {exc}")

            usb.commit()

        finally:
            usb.close()

        _update({"status": "complete", "errors": errors, "current_track": ""})

    except Exception as exc:
        _update({"status": "failed", "errors": [str(exc)], "current_track": ""})


@app.route("/api/mobile/export", methods=["POST"])
def mobile_export_start():
    """
    Start a USB export job.

    Body: { "playlist_ids": ["123", "456"], "drive_path": "/Volumes/DJMT" }
    Response: { "job_id": "uuid" }  (202)
    """
    data         = request.get_json(silent=True) or {}
    playlist_ids = data.get("playlist_ids") or []
    drive_path   = (data.get("drive_path") or "").strip()

    if not playlist_ids:
        return jsonify({"error": "playlist_ids required"}), 400
    if not drive_path:
        return jsonify({"error": "drive_path required"}), 400

    usb_db = Path(drive_path) / "PIONEER" / "Master" / "master.db"
    if not usb_db.exists():
        return jsonify({"error": f"No PIONEER/Master/master.db on {drive_path}"}), 400

    job_id = str(uuid.uuid4())

    with _EXPORT_LOCK:
        _EXPORT_JOBS[job_id] = {
            "job_id":        job_id,
            "status":        "running",
            "tracks_total":  0,
            "tracks_done":   0,
            "current_track": "",
            "errors":        [],
        }

    threading.Thread(
        target=_run_export,
        args=(job_id, [str(p) for p in playlist_ids], drive_path),
        daemon=True,
        name=f"export-{job_id[:8]}",
    ).start()

    return jsonify({"job_id": job_id}), 202


@app.route("/api/mobile/export/<job_id>")
def mobile_export_status(job_id: str):
    """Poll export job status."""
    with _EXPORT_LOCK:
        job = _EXPORT_JOBS.get(job_id)
    if job is None:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(job)


@sock.route("/api/mobile/events")
def mobile_events(ws):
    """
    WebSocket event bus for RekitGo.

    The mobile app connects here on startup and holds the connection open.
    The server pushes JSON events as they occur (download progress, file added,
    drive connected, analysis complete, export progress, etc.).

    Auth: checked via the before_request hook — flask-sock routes run through
    the same request pipeline, so _check_mobile_auth fires before this handler.

    Keep-alive: the client should send any message (e.g. "ping") every ~25s.
    We block on ws.receive(timeout=30) so the loop stays alive without spinning.
    If the client disconnects or the timeout fires with no message, the exception
    exits the loop and unregister() cleans up.
    """
    import ws_bus  # noqa: PLC0415
    ws_bus.register(ws)
    try:
        while True:
            ws.receive(timeout=30)
    except Exception:
        pass
    finally:
        ws_bus.unregister(ws)


if __name__ == "__main__":
    print()
    print("  ┌─────────────────────────────────────┐")
    print("  │  RekitBox  ·  rekordbox-toolkit UI  │")
    print("  │  http://localhost:5001              │")
    print("  └─────────────────────────────────────┘")
    print()
    app.run(host="127.0.0.1", port=5001, debug=False, threaded=True)
