"""
routes_rekordbox.py — ── The Zombie Machine ──

Flask Blueprint: Rekordbox-specific DB operations (audit, import, link, relocate,
path-root inspection, and Pioneer DB migration). All write operations enforce
the Rekordbox-must-be-closed guard.
"""

import json
import sys
import uuid
from pathlib import Path

from flask import Blueprint, Response, jsonify, request

from helpers import (
    REPO_ROOT,
    CLI_PATH,
    _proc_lock,
    _active_procs,
    _sse_response,
    _sse_done,
    _require_rb_closed,
    _get_library_root,
    _subprocess_env,
    mark_step_complete,
)

bp = Blueprint("rekordbox", __name__)


# ── Audit ─────────────────────────────────────────────────────────────────────

@bp.route("/api/run/audit")
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


# ── Import ────────────────────────────────────────────────────────────────────

@bp.route("/api/run/import")
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


# ── Link ──────────────────────────────────────────────────────────────────────

@bp.route("/api/run/link")
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


# ── Relocate ──────────────────────────────────────────────────────────────────

@bp.route("/api/run/relocate")
def api_relocate():
    import subprocess  # noqa: PLC0415

    err = _require_rb_closed()
    if err:
        return err

    old_roots = [r.strip() for r in request.args.getlist("old_root") if r.strip()]
    new = request.args.get("new_root", "").strip()
    if not old_roots or not new:
        return jsonify({"error": "old_root and new_root are required"}), 400

    library_root = _get_library_root(request, "new_root")

    if len(old_roots) == 1:
        cmd = [sys.executable, str(CLI_PATH), "relocate", old_roots[0], new]
        return _sse_response(cmd, library_root=library_root, step_name="relocate")

    # Multiple old roots — chain each CLI run, only emit done after the last one.
    def _multi():
        request_id = str(uuid.uuid4())
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
                    _active_procs[request_id] = proc
                try:
                    for line in iter(proc.stdout.readline, ""):
                        yield f"data: {json.dumps({'line': line.rstrip()})}\n\n"
                    proc.wait()
                    if proc.returncode != 0:
                        overall = proc.returncode
                finally:
                    with _proc_lock:
                        _active_procs.pop(request_id, None)
            except Exception as exc:
                with _proc_lock:
                    _active_procs.pop(request_id, None)
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


# ── Audit path-roots ──────────────────────────────────────────────────────────

@bp.route("/api/audit/path-roots")
def api_audit_path_roots():
    """
    Quick read-only scan of all track paths in the DB.
    Returns live and dead roots with track counts.
    Used by the UI to pre-fill the Relocate form.
    """
    try:
        from audit import find_dead_roots  # noqa: PLC0415
        from db_connection import read_db  # noqa: PLC0415
        from config import DJMT_DB as _DB  # noqa: PLC0415
        with read_db(_DB) as db:
            report = find_dead_roots(db)
        return jsonify({
            "dead_roots": report.dead_roots,
            "live_roots": report.live_roots,
            "has_dead_roots": report.has_dead_roots,
        })
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ── Pioneer DB migration ──────────────────────────────────────────────────────

@bp.route("/api/migrate-pioneer-db", methods=["POST"])
def api_migrate_pioneer_db():
    """Stream progress of migrating ~/Library/Pioneer/rekordbox/ to the target drive."""
    data = request.get_json(silent=True) or {}
    target = str(data.get("target", "")).strip()
    if not target:
        return jsonify({"error": "target is required"}), 400
    from db_migrator import migrate  # noqa: PLC0415
    return Response(migrate(target), mimetype="text/event-stream")
