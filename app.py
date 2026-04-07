"""
SuperBox / app.py

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
import Pathlib
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request

# Ensure the toolkit modules are importable when app.py is run directly
_REPO_ROOT = Path(__file__).parent.resolve()
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

app = Flask(__name__)

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
    age = datetime.now() - datetime.fromtimestamp(latest.stat().st_mtime)
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
    Some steps produce a SUPERBOX_REPORT_PATH that the next step may consume
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
                    if stripped.startswith("SUPERBOX_REPORT_PATH: "):
                        last_report_path = stripped[len("SUPERBOX_REPORT_PATH: "):]
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
    root = request.args.get("root", "").strip()
    if root:
        cmd += ["--root", root]
    return _sse_response(cmd, library_root=_get_library_root(request, "root"), step_name="audit")


@app.route("/api/run/process")
def api_process():
    path = request.args.get("path", "").strip()
    if not path:
        return jsonify({"error": "path is required"}), 400

    cmd = [sys.executable, str(CLI_PATH), "process", path]
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
            cmd = [sys.executable, str(CLI_PATH), "process", cfg.get("path", "")]
            if cfg.get("no_bpm"):   cmd.append("--no-bpm")
            if cfg.get("no_key"):   cmd.append("--no-key")
            if cfg.get("no_normalize"): cmd.append("--no-normalize")
            if cfg.get("force"):    cmd.append("--force")
            if cfg.get("workers", 1) > 1:
                cmd += ["--workers", str(cfg["workers"])]
            if dry_run:             cmd.append("--dry-run")

        elif stype == "normalize":
            cmd = [sys.executable, str(CLI_PATH), "process", cfg.get("path", ""),
                   "--no-bpm", "--no-key"]
            if cfg.get("workers", 1) > 1:
                cmd += ["--workers", str(cfg["workers"])]
            if dry_run: cmd.append("--dry-run")

        elif stype == "duplicates":
            cmd = [sys.executable, str(CLI_PATH), "duplicates", cfg.get("path", "")]
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
            cmd = [sys.executable, str(CLI_PATH), "convert",
                   cfg.get("path", ""), cfg.get("format", "aiff")]
            if cfg.get("workers", 1) > 1:
                cmd += ["--workers", str(cfg["workers"])]

        elif stype == "relocate":
            cmd = [sys.executable, str(CLI_PATH), "relocate",
                   cfg.get("old_root", ""), cfg.get("new_root", "")]

        elif stype == "audit":
            cmd = [sys.executable, str(CLI_PATH), "audit"]
            if cfg.get("root"):
                cmd += ["--root", cfg["root"]]

        elif stype == "import":
            cmd = [sys.executable, str(CLI_PATH), "import", cfg.get("path", "")]
            if dry_run:
                cmd.append("--dry-run")

        elif stype == "link":
            cmd = [sys.executable, str(CLI_PATH), "link", cfg.get("path", "")]

        elif stype == "novelty":
            cmd = [sys.executable, str(CLI_PATH), "novelty",
                   cfg.get("source", ""), cfg.get("dest", "")]
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
    path = request.args.get("path", "").strip()
    format_target = request.args.get("format", "").strip()
    if not path or not format_target:
        return jsonify({"error": "path and format are required"}), 400

    cmd = [sys.executable, str(CLI_PATH), "convert", path, format_target]
    workers = request.args.get("workers", "1").strip()
    if workers.isdigit() and int(workers) > 1:
        cmd += ["--workers", workers]
    library_root = _get_library_root(request, "path")
    return _sse_response(cmd, library_root=library_root, step_name="convert")


@app.route("/api/run/import")
def api_import():
    dry_run = request.args.get("dry_run") == "1"
    if not dry_run:
        err = _require_rb_closed()
        if err:
            return err

    path = request.args.get("path", "").strip()
    if not path:
        return jsonify({"error": "path is required"}), 400

    cmd = [sys.executable, str(CLI_PATH), "import", path]
    if dry_run:
        cmd.append("--dry-run")
    if request.args.get("resume") == "1" and not dry_run:
        cmd.append("--resume")
    library_root = _get_library_root(request, "path")
    return _sse_response(cmd, library_root=library_root, step_name="import")


@app.route("/api/run/link")
def api_link():
    err = _require_rb_closed()
    if err:
        return err

    path = request.args.get("path", "").strip()
    if not path:
        return jsonify({"error": "path is required"}), 400

    dry_run_link = request.args.get("dry_run") == "1"
    cmd = [sys.executable, str(CLI_PATH), "link", path]
    if dry_run_link:
        cmd.append("--dry-run")
    library_root = _get_library_root(request, "path")
    return _sse_response(cmd, library_root=library_root, step_name="link")


@app.route("/api/run/relocate")
def api_relocate():
    err = _require_rb_closed()
    if err:
        return err

    old = request.args.get("old_root", "").strip()
    new = request.args.get("new_root", "").strip()
    if not old or not new:
        return jsonify({"error": "old_root and new_root are required"}), 400

    cmd = [sys.executable, str(CLI_PATH), "relocate", old, new]
    library_root = _get_library_root(request, "new_root")
    return _sse_response(cmd, library_root=library_root, step_name="relocate")


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
    path = request.args.get("path", "").strip()
    if not path:
        return jsonify({"error": "path is required"}), 400

    cmd = [sys.executable, str(CLI_PATH), "duplicates", path]
    output = request.args.get("output", "").strip()
    if output:
        cmd += ["--output", output]
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
    Accept a JSON body {"paths": [...], "permanent": false} and return a single-use token.
    The token is consumed by GET /api/run/prune?token=<uuid>.
    """
    try:
        data = request.get_json(force=True, silent=True) or {}
        paths = data.get("paths", [])
        if not isinstance(paths, list):
            return jsonify({"error": "paths must be a list"}), 400
        token = str(uuid.uuid4())
        _prune_token_store[token] = {
            "paths":     paths,
            "permanent": bool(data.get("permanent", False)),
        }
        return jsonify({"token": token})
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
    page     = max(0, int(request.args.get("page",     0)))
    per_page = max(1, int(request.args.get("per_page", 200)))

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
            _report_cache[cache_key] = {
                "groups":       all_groups,
                "remove_paths": [
                    e["file_path"]
                    for g in all_groups
                    for e in g["entries"]
                    if e["action"] == "REVIEW_REMOVE"
                ],
                "keep_paths": [
                    e["file_path"]
                    for g in all_groups
                    for e in g["entries"]
                    if e["action"] == "KEEP"
                ],
            }

        cached      = _report_cache[cache_key]
        all_groups  = cached["groups"]
        total       = len(all_groups)
        start       = page * per_page
        page_groups = all_groups[start : start + per_page]

        return jsonify({
            "groups":        page_groups,
            "total_groups":  total,
            "total_remove":  len(cached["remove_paths"]),
            "page":          page,
            "per_page":      per_page,
            "csv_path":      str(csv_path),
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
    paths: list[str] = staged.get("paths", [])
    permanent: bool  = staged.get("permanent", False)

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
                            permanent=permanent)

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
    """Create the SuperBox Archive folder structure on the DJ drive."""
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
        return jsonify({"ok": True, "note": "Restart SuperBox for changes to take effect."})
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


# ── SuperBox update route ─────────────────────────────────────────────────────

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
    SSE stream of ``brew upgrade <packages>`` for the packages SuperBox uses.
    Only upgrades known-outdated packages reported by the last cached check.
    """
    outdated = _brew_get_status().get("outdated", [])
    names    = [p["name"] for p in outdated if p.get("name")]
    if not names:
        # Nothing to do — return an immediate SSE end event
        def _nothing():
            yield "data: No outdated SuperBox packages found.\n\n"
            yield "data: [DONE]\n\n"
        return Response(_nothing(), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    cmd = ["brew", "upgrade"] + names
    return _sse_response(cmd)


# ── Quit ──────────────────────────────────────────────────────────────────────

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

if __name__ == "__main__":
    print()
    print("  ┌─────────────────────────────────────┐")
    print("  │  SuperBox  ·  rekordbox-toolkit UI  │")
    print("  │  http://localhost:5001              │")
    print("  └─────────────────────────────────────┘")
    print()
    app.run(host="127.0.0.1", port=5001, debug=False, threaded=True)
