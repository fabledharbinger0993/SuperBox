"""
FableGear / app.py  —  thin factory

Registers blueprints, applies Flask extensions, runs startup side-effects,
and keeps the small set of core routes that do not belong in any blueprint.

Blueprint layout:
  routes_player.py    — The Media Pit    (/api/library/*, /api/playback/*, /audio/*)
  routes_tools.py     — The Butcher Shop (/api/run/process*, /api/run/organize,
                                          /api/run/duplicates, /api/normalize/*, etc.)
  routes_rekordbox.py — The Zombie Machine (/api/run/audit, /api/run/import,
                                            /api/run/link, /api/run/relocate,
                                            /api/migrate-pioneer-db)
  routes_mobile.py    — The Overlord    (/api/mobile/*, /api/connectivity)
"""

import json
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, render_template_string, request

# ── Shared helpers (base layer — no circular imports) ─────────────────────────
from helpers import (
    REPO_ROOT,
    CLI_PATH,
    limiter,
    sock,
    _rb_is_running,
    _backup_info,
    _release_info,
    _sse_response,
    _proc_lock,
    _active_procs,
    get_step_status,
)

_REPO_ROOT = REPO_ROOT   # local alias for legacy references below

# ── Flask app ─────────────────────────────────────────────────────────────────

app = Flask(
    __name__,
    template_folder=str(REPO_ROOT / "templates"),
    static_folder=str(REPO_ROOT / "static"),
)

# Attach lazy-init extensions to the app instance
limiter.init_app(app)
sock.init_app(app)

# ── Blueprints ────────────────────────────────────────────────────────────────

from routes_player     import bp as player_bp      # noqa: E402
from routes_tools      import bp as tools_bp        # noqa: E402
from routes_rekordbox  import bp as rekordbox_bp    # noqa: E402
from routes_mobile     import bp as mobile_bp       # noqa: E402

app.register_blueprint(player_bp)
app.register_blueprint(tools_bp)
app.register_blueprint(rekordbox_bp)
app.register_blueprint(mobile_bp)

# ── Startup side-effects ──────────────────────────────────────────────────────

from brew_updater import (                          # noqa: E402
    start_background_checker as _start_brew_checker,
    check_now as _brew_check_now,
    get_status as _brew_get_status,
)
_start_brew_checker()

from update_checker import (                        # noqa: E402
    start_background_checker as _start_update_checker,
    get_status as _update_get_status,
)
_start_update_checker()

try:
    from config import ensure_archive_structure     # noqa: PLC0415
    ensure_archive_structure()
except Exception:
    pass  # Drive not mounted yet — non-fatal


# ── Splash route ──────────────────────────────────────────────────────────────

_SPLASH_HTML = """\
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>FableGear</title>
  <style>
    * { margin: 0; padding: 0; box-sizing: border-box; }
    html, body {
      width: 100%; height: 100%;
      background: #07070f;
      overflow: hidden;
      display: flex;
      align-items: center;
      justify-content: center;
    }
    video {
      width: 100%; height: 100%;
      object-fit: contain;
      opacity: 1;
      transition: opacity 0.5s ease;
    }
    video.fade-out { opacity: 0; }
  </style>
</head>
<body>
  <video id="splash" autoplay playsinline>
    <source src="/static/fablegear-splash.mp4" type="video/mp4">
  </video>
  <script>
    var v = document.getElementById('splash');
    var done = false;
    function finish() {
      if (done) return;
      done = true;
      v.classList.add('fade-out');
      setTimeout(function() { window.location.replace('/'); }, 550);
    }
    v.addEventListener('ended', finish);
    v.addEventListener('error', function() { window.location.replace('/'); });
    setTimeout(finish, 35000);
  </script>
</body>
</html>
"""


# ── Core routes ───────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/splash")
def splash():
    return render_template_string(_SPLASH_HTML)


@app.route("/api/status")
def api_status():
    from user_config import get_drive_status  # noqa: PLC0415
    return jsonify({
        "rb_running": _rb_is_running(),
        "backup":     _backup_info(),
        "release":    _release_info(),
        "drives":     get_drive_status(),
    })


@app.route("/api/export/rekordbox", methods=["POST"])
def api_export_rekordbox():
    return jsonify({
        "error": (
            "Legacy Rekordbox XML export is disabled. The previous implementation "
            "did not produce a self-consistent Pioneer-compatible export."
        )
    }), 501


@app.route("/api/config")
def api_config():
    """Expose the configured default paths so the UI can pre-fill forms."""
    from helpers import _current_fablegear_mode, _backup_dir  # noqa: PLC0415
    try:
        from config import (  # noqa: PLC0415
            DJMT_DB, MUSIC_ROOT, SKIP_DIRS,
            ARCHIVE_ROOT, SAVEPOINTS_DIR, QUARANTINE_DIR, REPORTS_DIR,
            ARCHIVE_ENABLED, _archive_mode, _custom_archive,
        )
        from user_config import load_user_config as _luc  # noqa: PLC0415
        _ucfg = _luc()
        current_mode = _current_fablegear_mode()
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
            "mode":             current_mode,
            "configured":       True,
        })
    except Exception:
        current_mode = _current_fablegear_mode()
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
            "mode":            current_mode,
            "configured":      False,
        })


@app.route("/api/state", methods=["POST"])
def api_state():
    """Return the steps_completed dict for a given library root."""
    data = request.get_json(force=True, silent=True) or {}
    library_root = data.get("library_root", "").strip()
    if not library_root:
        return jsonify({}), 200
    return jsonify(get_step_status(library_root))


@app.route("/api/setup-archive", methods=["POST"])
def api_setup_archive():
    """Create the FableGear Archive folder structure on the DJ drive."""
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
        cfg = load_user_config()
        if "archive_mode" in data:
            cfg["archive_mode"] = data["archive_mode"]
        if "custom_archive_dir" in data:
            cfg["custom_archive_dir"] = data["custom_archive_dir"]
        if "excluded_dirs" in data:
            cfg["excluded_dirs"] = [d for d in data["excluded_dirs"] if isinstance(d, str) and d.strip()]
        if "mode" in data and data["mode"] in ("rural", "suburban"):
            cfg["mode"] = data["mode"]
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            _json.dump(cfg, f, indent=2)
        return jsonify({"ok": True, "note": "Restart FableGear for changes to take effect."})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


# ── Update routes ─────────────────────────────────────────────────────────────

@app.route("/api/update/status")
def api_update_status():
    """Return the cached GitHub release check result (never blocks)."""
    return jsonify(_update_get_status())


@app.route("/api/update/apply", methods=["POST"])
def api_update_apply():
    """
    Pull the latest release in-place, then relaunch FableGear.

    Flow:
      1. Refuse if a scan/subprocess is running or Rekordbox is open.
      2. Run ``git pull --ff-only`` in the repo root.
      3. On success, spawn a detached helper that waits for the port to free,
         then re-runs launch.sh. Finally SIGTERM self so the helper can bind.
      4. Frontend polls /api/update/status until it responds, then reloads.
    """
    with _proc_lock:
        active = any(proc.poll() is None for proc in _active_procs.values())
    if active:
        return jsonify({
            "ok": False,
            "error": "A scan is still running — cancel or finish it before updating.",
        }), 409

    launch_sh = REPO_ROOT / "launch.sh"
    if not (REPO_ROOT / ".git").exists() or not launch_sh.exists():
        return jsonify({
            "ok": False,
            "error": "Not a git install — download the new release manually.",
        }), 400

    try:
        status_check = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=15,
        )
        if status_check.returncode == 0 and status_check.stdout.strip():
            return jsonify({
                "ok": False,
                "error": "Working tree has uncommitted changes — commit or stash them before updating.",
            }), 409

        pull = subprocess.run(
            ["git", "pull", "origin", "main", "--ff-only"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=60,
        )
    except FileNotFoundError:
        return jsonify({"ok": False, "error": "git is not installed."}), 500
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": "git pull timed out — check your connection."}), 504
    except Exception as exc:
        return jsonify({"ok": False, "error": f"git pull failed: {exc}"}), 500

    if pull.returncode != 0:
        err = (pull.stderr or pull.stdout or "").strip() or "git pull failed"
        return jsonify({"ok": False, "error": err}), 500

    def _relaunch() -> None:
        import time
        time.sleep(0.7)
        try:
            subprocess.Popen(
                ["bash", "-c", 'sleep 2 && exec bash "$0"', str(launch_sh)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                close_fds=True,
                cwd=str(REPO_ROOT),
            )
        finally:
            os.kill(os.getpid(), signal.SIGTERM)

    threading.Thread(target=_relaunch, daemon=True).start()
    return jsonify({"ok": True, "output": pull.stdout.strip()})


# ── Homebrew routes ───────────────────────────────────────────────────────────

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
    """SSE stream of ``brew upgrade <packages>`` for known-outdated packages."""
    outdated = _brew_get_status().get("outdated", [])
    names = [p["name"] for p in outdated if p.get("name")]
    if not names:
        def _nothing():
            yield "data: No outdated FableGear packages found.\n\n"
            yield "data: [DONE]\n\n"
        return Response(
            _nothing(),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    cmd = ["brew", "upgrade"] + names
    return _sse_response(cmd)


# ── Folder path resolution ────────────────────────────────────────────────────

@app.route("/api/finder-selection")
def api_finder_selection():
    """Return the path of the currently selected item in Finder."""
    source = request.args.get("source", "")

    _finder_script = """\
tell application "Finder"
    set sel to selection
    if (count of sel) > 0 then
        return POSIX path of (item 1 of sel as alias)
    end if
end tell"""
    try:
        r = subprocess.run(
            ["osascript", "-e", _finder_script],
            capture_output=True, text=True, timeout=60,
        )
        app.logger.debug("[finder-selection] rc=%d stdout=%r stderr=%r",
                         r.returncode, r.stdout, r.stderr)
        if r.returncode == 0 and r.stdout.strip():
            return jsonify({"path": r.stdout.strip().rstrip("/")})
    except Exception as exc:
        app.logger.debug("[finder-selection] exception: %s", exc)

    if source == "drop":
        app.logger.debug("[finder-selection] source=drop, returning null")
        return jsonify({"path": None})

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
    """Open the native macOS folder-chooser dialog (Browse button)."""
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
    """Lightweight directory listing for the in-app file browser panel."""
    if request.remote_addr not in ("127.0.0.1", "::1"):
        return jsonify({"error": "Forbidden"}), 403
    AUDIO_EXTS = {
        ".aiff", ".aif", ".aifc", ".wav", ".flac", ".mp3",
        ".m4a", ".m4p", ".mp4", ".m4v", ".alac", ".ogg", ".opus",
    }
    path_str = request.args.get("path", "/Volumes")
    p = Path(path_str)
    if not p.exists() or not p.is_dir():
        return jsonify({"error": f"Not a directory: {path_str}"}), 400
    try:
        entries = []
        for item in sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
            if item.name.startswith("."):
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


# ── Setup / state persistence ─────────────────────────────────────────────────

_FABLEGEAR_STATE = Path.home() / ".fablegear" / "fablegear-state.json"


@app.route("/api/setup-status")
def api_setup_status():
    """Return whether the welcome wizard has been completed."""
    try:
        if _FABLEGEAR_STATE.exists():
            state = json.loads(_FABLEGEAR_STATE.read_text())
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
        data = request.get_json(silent=True) or {}
        state = {
            "setup_complete": True,
            "db_read":  data.get("db_read"),
            "db_write": data.get("db_write"),
        }
        _FABLEGEAR_STATE.parent.mkdir(parents=True, exist_ok=True)
        _FABLEGEAR_STATE.write_text(json.dumps(state, indent=2) + "\n")
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/config/set-music-root", methods=["POST"])
def api_set_music_root():
    """Update music_root in ~/.fablegear/config.json."""
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


@app.route("/api/quit", methods=["POST"])
def api_quit():
    """Shut the server down cleanly after sending the response."""
    def _shutdown():
        import time
        time.sleep(0.4)
        os.kill(os.getpid(), signal.SIGTERM)
    threading.Thread(target=_shutdown, daemon=True).start()
    return jsonify({"ok": True})


# ── After-request headers ─────────────────────────────────────────────────────

@app.after_request
def disable_cache_on_static_files(response):
    """Disable caching for static files; add CSP for defense-in-depth."""
    if request.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"

    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' 'unsafe-eval'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "connect-src 'self' ws://localhost:* wss://localhost:*; "
        "font-src 'self' data:; "
        "object-src 'none'; "
        "base-uri 'self';"
    )

    return response


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print()
    print("  ┌─────────────────────────────────────┐")
    print("  │  FableGear  ·  rekordbox-toolkit UI  │")
    print("  │  http://localhost:5001              │")
    print("  └─────────────────────────────────────┘")
    print()
    app.run(host="127.0.0.1", port=5001, debug=False)
