"""
SuperBox / app.py

Local web dashboard for rekordbox-toolkit.
Run:  python3 app.py
Open: http://localhost:5001

The UI runs all CLI commands via subprocess and streams output live.
Rekordbox must be closed for any write operation — the server checks this
before spawning write commands and refuses if the process is found running.

Import note:
  cli.py uses `from SuperBox.config import ...`, which requires the *parent*
  of this repo to be on PYTHONPATH. The server sets this automatically in the
  subprocess environment. If you cloned the repo under a different name than
  "SuperBox", update REPO_NAME below.
"""

import json
import os
import platform
import queue
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request

app = Flask(__name__)

# ── Paths ──────────────────────────────────────────────────────────────────────

REPO_ROOT   = Path(__file__).parent.resolve()
REPO_PARENT = REPO_ROOT.parent          # parent dir must be in PYTHONPATH
CLI_PATH    = REPO_ROOT / "cli.py"
BACKUP_DIR  = Path.home() / "rekordbox-toolkit" / "backups"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _rb_is_running() -> bool:
    """Return True if a Rekordbox process is currently active."""
    if platform.system() == "Darwin":
        result = subprocess.run(
            ["pgrep", "-i", "rekordbox"],
            capture_output=True, text=True,
        )
        return result.returncode == 0
    # Windows fallback
    result = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq rekordbox.exe"],
        capture_output=True, text=True, shell=True,
    )
    return "rekordbox.exe" in result.stdout.lower()


def _backup_info() -> dict:
    """Return the age of the most recent timestamped backup, if any."""
    if not BACKUP_DIR.exists():
        return {"exists": False, "name": None, "age": None}
    backups = sorted(BACKUP_DIR.glob("master_*.db"), reverse=True)
    if not backups:
        return {"exists": False, "name": None, "age": None}
    latest = backups[0]
    age = datetime.now() - datetime.fromtimestamp(latest.stat().st_mtime)
    h = int(age.total_seconds() // 3600)
    m = int((age.total_seconds() % 3600) // 60)
    age_str = f"{h}h {m}m ago" if h else f"{m}m ago"
    return {"exists": True, "name": latest.name, "age": age_str}


def _subprocess_env() -> dict:
    """Build an environment dict that puts REPO_PARENT on PYTHONPATH."""
    env = os.environ.copy()
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        str(REPO_PARENT) + os.pathsep + existing if existing else str(REPO_PARENT)
    )
    return env


def _stream(cmd: list[str]):
    """
    Generator that yields SSE-formatted lines from a subprocess.
    Each event is a JSON object:
      {"line": "..."}          — a line of output
      {"done": true, "exit_code": N}  — command finished
    """
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
        for line in iter(process.stdout.readline, ""):
            yield f"data: {json.dumps({'line': line.rstrip()})}\n\n"
        process.wait()
        yield f"data: {json.dumps({'done': True, 'exit_code': process.returncode})}\n\n"
    except Exception as exc:
        yield f"data: {json.dumps({'line': f'[SERVER ERROR] {exc}', 'done': True, 'exit_code': 1})}\n\n"


def _sse_response(cmd: list[str]) -> Response:
    return Response(
        _stream(cmd),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _require_rb_closed():
    """Return an error response if Rekordbox is running, else None."""
    if _rb_is_running():
        return jsonify({
            "error": "Rekordbox is running. Close it before running write operations."
        }), 409
    return None


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
        sys.path.insert(0, str(REPO_ROOT))
        from config import DJMT_DB, MUSIC_ROOT  # noqa: PLC0415
        return jsonify({
            "music_root": str(MUSIC_ROOT),
            "djmt_db": str(DJMT_DB),
            "backup_dir": str(BACKUP_DIR),
        })
    except Exception:
        return jsonify({
            "music_root": "/Volumes/DJMT/DJMT PRIMARY",
            "djmt_db": "/Volumes/DJMT/PIONEER/Master/master.db",
            "backup_dir": str(BACKUP_DIR),
        })


# ── Command routes (all return SSE streams) ───────────────────────────────────

@app.route("/api/run/audit")
def api_audit():
    cmd = [sys.executable, str(CLI_PATH), "audit"]
    root = request.args.get("root", "").strip()
    if root:
        cmd += ["--root", root]
    return _sse_response(cmd)


@app.route("/api/run/process")
def api_process():
    path = request.args.get("path", "").strip()
    if not path:
        return jsonify({"error": "path is required"}), 400

    # process touches audio files — no Rekordbox check needed, but normalise
    # requires a note. The safety check is informational only here.

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
    return _sse_response(cmd)


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
    return _sse_response(cmd)


@app.route("/api/run/link")
def api_link():
    err = _require_rb_closed()
    if err:
        return err

    path = request.args.get("path", "").strip()
    if not path:
        return jsonify({"error": "path is required"}), 400

    cmd = [sys.executable, str(CLI_PATH), "link", path]
    return _sse_response(cmd)


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
    return _sse_response(cmd)


@app.route("/api/run/duplicates")
def api_duplicates():
    path = request.args.get("path", "").strip()
    if not path:
        return jsonify({"error": "path is required"}), 400

    cmd = [sys.executable, str(CLI_PATH), "duplicates", path]
    output = request.args.get("output", "").strip()
    if output:
        cmd += ["--output", output]
    return _sse_response(cmd)


# ── Duplicate prune routes ────────────────────────────────────────────────────

@app.route("/api/duplicates/load")
def api_duplicates_load():
    """
    Load a duplicate_report.csv, enrich with live disk + DB data,
    and return structured JSON for the prune UI.
    Falls back to the default report path if no csv_path is given.
    """
    csv_path_str = request.args.get("csv_path", "").strip()
    csv_path = (
        Path(csv_path_str)
        if csv_path_str
        else Path.home() / "rekordbox-toolkit" / "duplicate_report.csv"
    )

    if not csv_path.exists():
        return jsonify({"error": f"Report not found: {csv_path}"}), 404

    try:
        sys.path.insert(0, str(REPO_ROOT))
        from pruner import load_report          # noqa: PLC0415
        from db_connection import read_db       # noqa: PLC0415
        from config import DJMT_DB as _DB      # noqa: PLC0415

        with read_db(_DB) as db:
            groups = load_report(csv_path, db)

        payload = [
            {
                "group_id": g.group_id,
                "entries": [
                    {
                        "action":        e.action,
                        "rank":          e.rank,
                        "file_path":     e.file_path,
                        "filename":      e.filename,
                        "file_size_mb":  round(e.file_size_mb, 2),
                        "bpm":           e.bpm,
                        "key":           e.key,
                        "format_ext":    e.format_ext,
                        "format_tier":   e.format_tier,
                        "exists_on_disk":e.exists_on_disk,
                        "in_db":         e.in_db,
                    }
                    for e in g.entries
                ],
            }
            for g in groups
        ]
        return jsonify({"groups": payload, "csv_path": str(csv_path)})

    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


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
        # macOS: `open` uses the default app per file type
        subprocess.Popen(["open", str(p)])
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/run/prune")
def api_run_prune():
    """
    Execute a confirmed prune: remove DB entries + move files to Trash.
    Accepts a JSON-encoded list of file paths as the `paths` query param.
    Returns an SSE stream of progress lines.
    """
    err = _require_rb_closed()
    if err:
        return err

    try:
        paths: list[str] = json.loads(request.args.get("paths", "[]"))
    except (json.JSONDecodeError, ValueError):
        return jsonify({"error": "paths must be a JSON array"}), 400

    if not paths:
        return jsonify({"error": "no files selected"}), 400

    log_q: queue.Queue = queue.Queue()

    def _worker() -> None:
        try:
            sys.path.insert(0, str(REPO_ROOT))
            from pruner import prune_files          # noqa: PLC0415
            from db_connection import write_db      # noqa: PLC0415
            from config import DJMT_DB as _DB      # noqa: PLC0415

            with write_db(_DB) as db:
                prune_files(paths, db, log=lambda m: log_q.put(("line", m)))

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


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print()
    print("  ┌─────────────────────────────────────┐")
    print("  │  SuperBox  ·  rekordbox-toolkit UI  │")
    print("  │  http://localhost:5001              │")
    print("  └─────────────────────────────────────┘")
    print()
    app.run(host="127.0.0.1", port=5001, debug=False, threaded=True)
