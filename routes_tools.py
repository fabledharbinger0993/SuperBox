"""
routes_tools.py — ── The Butcher Shop ──

Flask Blueprint: all analysis, processing, and library-management tool routes
(process, pipeline, organize, convert, novelty, rename, duplicates, prune,
normalize preview, and scan cancel).
"""

import json
import os
import platform
import queue
import random as _random
import re as _re
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

from flask import Blueprint, Response, jsonify, request, send_file

from helpers import (
    REPO_ROOT,
    CLI_PATH,
    _sse_response,
    _sse_done,
    _stream_pipeline,
    _smart_skip_candidates,
    _require_rb_closed,
    _get_library_root,
    _rb_is_running,
    _proc_lock,
    _active_procs,
    _evict_old_jobs,
    _MAX_PRUNE_TOKENS,
    _MAX_PREVIEW_JOBS,
    mark_step_complete,
)

bp = Blueprint("tools", __name__)


# ── Normalize preview state ───────────────────────────────────────────────────

_PREVIEW_TMP: Path = Path.home() / ".rekordbox-toolkit" / "previews"
_PREVIEW_TMP.mkdir(parents=True, exist_ok=True)

_PREVIEW_JOBS: dict[str, dict] = {}
_PREVIEW_LOCK: threading.Lock = threading.Lock()

_PREVIEW_AUDIO_EXTS = {
    ".aiff", ".aif", ".aifc", ".wav", ".flac", ".mp3",
    ".m4a", ".m4p", ".mp4", ".m4v", ".alac", ".ogg", ".opus",
}
_PREVIEW_MIN_DUR: int = 120     # track must be ≥ 2 min
_PREVIEW_MAX_SCAN: int = 40     # cap random sample for large folders
_PREVIEW_WINDOW: int = 20       # seconds of audio measured for LUFS


# ── Prune / duplicate report state ───────────────────────────────────────────

_prune_token_store: dict[str, dict] = {}
_report_cache: dict[str, dict] = {}


# ── Process (Tag Tracks) ──────────────────────────────────────────────────────

@bp.route("/api/run/process")
def api_process():
    paths = [p.strip() for p in request.args.getlist("path") if p.strip()]
    if not paths:
        return jsonify({"error": "path is required"}), 400

    no_bpm = request.args.get("no_bpm") == "1"
    no_key = request.args.get("no_key") == "1"
    no_normalize = request.args.get("no_normalize") == "1"
    force = request.args.get("force") == "1"
    enrich_tags = request.args.get("enrich_tags") == "1"
    smart_skip = request.args.get("smart_skip", "1") == "1"

    detect_bpm = not no_bpm
    detect_key = not no_key

    cmd = [sys.executable, str(CLI_PATH), "process", paths[0]]
    for extra in paths[1:]:
        cmd += ["--also-scan", extra]

    if no_bpm:
        cmd.append("--no-bpm")
    if no_key:
        cmd.append("--no-key")
    if no_normalize:
        cmd.append("--no-normalize")
    if force:
        cmd.append("--force")
    if enrich_tags:
        cmd.append("--enrich-tags")
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

    if (
        smart_skip
        and not force
        and no_normalize
        and not enrich_tags
        and (detect_bpm or detect_key)
    ):
        roots = [Path(p) for p in paths]
        filter_result = _smart_skip_candidates(roots, detect_bpm=detect_bpm, detect_key=detect_key)
        pending = filter_result["pending"]

        prelude = [
            (
                "Smart Skip: "
                f"{filter_result['pending_count']}/{filter_result['total']} file(s) need work; "
                f"{filter_result['skipped_complete']} already complete and skipped upfront."
            )
        ]
        if filter_result["unreadable"]:
            prelude.append(
                f"Smart Skip note: {filter_result['unreadable']} file(s) had read warnings and remain included for safe handling."
            )
        if filter_result["invalid_paths"]:
            prelude.append(
                f"Smart Skip note: {filter_result['invalid_paths']} path(s) were invalid or unsupported and ignored."
            )

        if not pending:
            return _sse_done([
                prelude[0],
                "No files require BPM/key updates for this run.",
                "Finished successfully.",
            ])

        tf = tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".txt",
            prefix="rekitbox_smart_skip_",
            delete=False,
            encoding="utf-8",
        )
        tf.write("\n".join(pending))
        tf.close()
        cmd += ["--paths-file", tf.name]
        cmd = [sys.executable, str(CLI_PATH), "process", paths[0], *cmd[4:]]
        library_root = _get_library_root(request, "path")
        return _sse_response(
            cmd,
            library_root=library_root,
            step_name="process",
            prelude_lines=prelude,
            cleanup_paths=[Path(tf.name)],
        )

    library_root = _get_library_root(request, "path")
    return _sse_response(cmd, library_root=library_root, step_name="process")


# ── Process retry (force re-tag a specific file list) ────────────────────────

@bp.route("/api/run/process-retry", methods=["POST"])
def api_process_retry():
    body = request.get_json(force=True, silent=True) or {}
    paths = [p.strip() for p in (body.get("paths") or []) if p.strip()]
    if not paths:
        return jsonify({"error": "paths list is required"}), 400

    tf = tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", prefix="rekitbox_retry_",
        delete=False, encoding="utf-8",
    )
    tf.write("\n".join(paths))
    tf.close()

    placeholder_root = str(Path(paths[0]).parent)
    cmd = [
        sys.executable, str(CLI_PATH),
        "process", placeholder_root,
        "--no-normalize",
        "--force",
        "--paths-file", tf.name,
    ]
    if body.get("no_bpm"):
        cmd.append("--no-bpm")
    if body.get("no_key"):
        cmd.append("--no-key")

    library_root = str(Path(paths[0]).parent)
    return _sse_response(cmd, library_root=library_root, step_name="process")


# ── Pipeline ──────────────────────────────────────────────────────────────────

@bp.route("/api/run/pipeline", methods=["POST"])
def api_pipeline():
    """
    Execute a user-defined sequence of steps.
    Body: {"dry_run": bool, "steps": [{"type": str, "config": {...}}, ...]}
    """
    body = request.get_json(force=True, silent=True) or {}
    dry_run = bool(body.get("dry_run", True))
    raw_steps = body.get("steps", [])

    if not raw_steps:
        return jsonify({"error": "steps list is required"}), 400

    _WRITE_STEP_TYPES = {"import", "link", "relocate", "prune"}
    if not dry_run and any(s.get("type") in _WRITE_STEP_TYPES for s in raw_steps):
        err = _require_rb_closed()
        if err:
            return err

    built: list[dict] = []

    for s in raw_steps:
        stype = s.get("type", "")
        cfg = s.get("config", {})
        name = s.get("name", stype)

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
            if cfg.get("no_bpm"):       cmd.append("--no-bpm")
            if cfg.get("no_key"):       cmd.append("--no-key")
            if cfg.get("no_normalize"): cmd.append("--no-normalize")
            if cfg.get("force"):        cmd.append("--force")
            if cfg.get("workers", 1) > 1:
                cmd += ["--workers", str(cfg["workers"])]
            if dry_run:                 cmd.append("--dry-run")

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
            if dry_run:
                cmd.append("--dry-run")

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
            cmd = [sys.executable, str(CLI_PATH), "prune"]
            if dry_run:
                cmd.append("--dry-run")
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


# ── Organize ──────────────────────────────────────────────────────────────────

@bp.route("/api/run/organize")
def api_organize():
    sources = [s.strip() for s in request.args.getlist("source") if s.strip()]
    target = request.args.get("target", "").strip()
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


# ── Convert ───────────────────────────────────────────────────────────────────

@bp.route("/api/run/convert")
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


# ── Novelty ───────────────────────────────────────────────────────────────────

@bp.route("/api/run/novelty")
def api_novelty():
    sources = [s.strip() for s in request.args.getlist("source") if s.strip()]
    dest = request.args.get("dest", "").strip()
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


# ── Rename ────────────────────────────────────────────────────────────────────

@bp.route("/api/run/rename")
def api_rename():
    path = request.args.get("path", "").strip()
    if not path:
        return jsonify({"error": "path is required"}), 400

    cmd = [sys.executable, str(CLI_PATH), "rename", path]

    if request.args.get("no_dry_run") == "1":
        cmd.append("--no-dry-run")

    workers = request.args.get("workers", "1").strip()
    if workers.isdigit() and int(workers) > 1:
        cmd += ["--workers", workers]

    library_root = path
    return _sse_response(cmd, library_root=library_root, step_name="rename")


@bp.route("/api/rename/probe")
def api_rename_probe():
    path = request.args.get("path", "").strip()
    if not path:
        return jsonify({"error": "path is required"}), 400

    root = Path(path)
    if not root.is_dir():
        return jsonify({"error": f"Not a directory: {path}"}), 400

    top_n = request.args.get("top_n", "5").strip()
    sample_size = request.args.get("sample_size", "100").strip()
    try:
        top_n_int = max(1, min(20, int(top_n)))
        sample_size_int = max(top_n_int, min(500, int(sample_size)))
    except ValueError:
        return jsonify({"error": "top_n and sample_size must be integers"}), 400

    from renamer import probe_ambiguous  # noqa: PLC0415

    candidates = probe_ambiguous(root, top_n=top_n_int, sample_size=sample_size_int)
    return jsonify({
        "path": str(root),
        "top_n": top_n_int,
        "sample_size": sample_size_int,
        "candidates": [candidate.to_dict() for candidate in candidates],
    })


@bp.route("/api/rename/learn", methods=["POST"])
def api_rename_learn():
    data = request.get_json(silent=True) or {}
    action = str(data.get("action", "")).strip()
    source_path = str(data.get("source_path", "")).strip()
    if not action or not source_path:
        return jsonify({"error": "action and source_path are required"}), 400

    import renamer_learned as _learned  # noqa: PLC0415

    rules = _learned.load()

    if action in ("confirm", "manual"):
        target_name = str(data.get("target_name", "")).strip()
        if not target_name:
            return jsonify({"error": "target_name is required for confirm/manual"}), 400
        rules.add_manual_rename(source_path, target_name)
        _learned.harvest_from_confirmation(rules, target_name)
    elif action == "producer_alias":
        lowered = str(data.get("token", "")).strip()
        canonical = str(data.get("canonical", "")).strip()
        if not lowered or not canonical:
            return jsonify({"error": "token and canonical are required for producer_alias"}), 400
        rules.add_producer_alias(lowered, canonical)
        rules.add_known_producer(canonical)
    elif action == "quarantine":
        rules.add_quarantine(source_path)
        library_root = str(data.get("library_root", "")).strip()
        moved = None
        if library_root:
            from renamer import quarantine_track  # noqa: PLC0415
            moved = quarantine_track(Path(source_path), Path(library_root))
    else:
        return jsonify({"error": f"Unsupported action: {action}"}), 400

    _learned.save(rules)
    response = {
        "ok": True,
        "action": action,
        "source_path": source_path,
        "history_count": len(rules.history),
    }
    if action == "quarantine":
        response["moved"] = moved
    return jsonify(response)


@bp.route("/api/rename/preflight/apply", methods=["POST"])
def api_rename_preflight_apply():
    data = request.get_json(silent=True) or {}
    root_str = str(data.get("path", "")).strip()
    entries = data.get("entries") or []
    if not root_str:
        return jsonify({"error": "path is required"}), 400
    if not isinstance(entries, list):
        return jsonify({"error": "entries must be a list"}), 400

    root = Path(root_str)
    if not root.is_dir():
        return jsonify({"error": f"Not a directory: {root}"}), 400

    import renamer_learned as _learned  # noqa: PLC0415
    from renamer import quarantine_track  # noqa: PLC0415

    rules = _learned.load()
    results = []

    for item in entries:
        if not isinstance(item, dict):
            return jsonify({"error": "Each entry must be an object"}), 400
        action = str(item.get("action", "")).strip()
        source_path = str(item.get("source_path", "")).strip()
        if not action or not source_path:
            return jsonify({"error": "Each entry requires action and source_path"}), 400

        if action == "manual":
            target_name = str(item.get("target_name", "")).strip()
            if not target_name:
                return jsonify({"error": f"target_name is required for {source_path}"}), 400
            rules.add_manual_rename(source_path, target_name)
            _learned.harvest_from_confirmation(rules, target_name)
            results.append({"action": action, "source_path": source_path, "target_name": target_name})
        elif action == "producer_alias":
            token = str(item.get("token", "")).strip()
            canonical = str(item.get("canonical", "")).strip()
            if not token or not canonical:
                return jsonify({"error": f"token and canonical are required for {source_path}"}), 400
            rules.add_producer_alias(token, canonical)
            rules.add_known_producer(canonical)
            results.append({"action": action, "source_path": source_path, "token": token, "canonical": canonical})
        elif action == "quarantine":
            moved = quarantine_track(Path(source_path), root)
            rules.add_quarantine(source_path)
            results.append({"action": action, "source_path": source_path, "moved": moved})
        elif action == "skip":
            results.append({"action": action, "source_path": source_path})
        else:
            return jsonify({"error": f"Unsupported action: {action}"}), 400

    _learned.save(rules)
    return jsonify({
        "ok": True,
        "path": str(root),
        "results": results,
        "history_count": len(rules.history),
    })


# ── Duplicates ────────────────────────────────────────────────────────────────

@bp.route("/api/run/duplicates")
def api_duplicates():
    paths = [p.strip() for p in request.args.getlist("path") if p.strip()]
    if not paths:
        return jsonify({"error": "at least one path is required"}), 400

    cmd = [sys.executable, str(CLI_PATH), "duplicates"] + paths
    workers = request.args.get("workers", "").strip()
    if workers and workers.isdigit() and int(workers) > 1:
        cmd += ["--workers", workers]
    match_mode = request.args.get("match_mode", "").strip()
    if match_mode in ("exact", "fuzzy", "tags", "all"):
        cmd += ["--match-mode", match_mode]
    fuzzy_threshold = request.args.get("fuzzy_threshold", "").strip()
    if fuzzy_threshold:
        try:
            ft = float(fuzzy_threshold)
            if 0.0 < ft < 1.0:
                cmd += ["--fuzzy-threshold", f"{ft:.2f}"]
        except ValueError:
            pass
    pause = request.args.get("pause", "").strip()
    if pause:
        try:
            if float(pause) > 0:
                cmd += ["--pause", pause]
        except ValueError:
            pass
    library_root = paths[0] if paths else ""
    return _sse_response(cmd, library_root=library_root, step_name="duplicates")


# ── Prune stage / load / remove-paths ────────────────────────────────────────

@bp.route("/api/prune/stage", methods=["POST"])
def api_prune_stage():
    """
    Accept a JSON body {"paths": [...], "permanent": false, "csv_path": "..."} and
    return a single-use token consumed by GET /api/run/prune?token=<uuid>.
    """
    try:
        data = request.get_json(force=True, silent=True) or {}
        paths = data.get("paths", [])
        if not isinstance(paths, list):
            return jsonify({"error": "paths must be a list"}), 400

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
        _evict_old_jobs(_prune_token_store, _MAX_PRUNE_TOKENS)
        _prune_token_store[token] = {
            "paths":      paths,
            "permanent":  bool(data.get("permanent", False)),
            "keeper_map": keeper_map,
            "_issued_at": time.time(),
        }
        return jsonify({"token": token, "keeper_map_size": len(keeper_map)})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@bp.route("/api/duplicates/load")
def api_duplicates_load():
    """
    Load a duplicate_report.csv, enrich with live disk + DB data,
    and return a paginated slice of groups for the prune UI.
    """
    csv_path_str = request.args.get("csv_path", "").strip()
    try:
        page = max(0, int(request.args.get("page", 0)))
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
        csv_mtime = csv_path.stat().st_mtime
        cached = _report_cache.get(cache_key)
        if cached is None or cached.get("_mtime") != csv_mtime:
            from pruner import load_report  # noqa: PLC0415
            groups = None
            db_warning = None

            try:
                from db_connection import read_db  # noqa: PLC0415
                from config import DJMT_DB as _DB  # noqa: PLC0415
                with read_db(_DB) as db:
                    groups = load_report(csv_path, db)
            except Exception as db_exc:
                groups = load_report(csv_path, None)
                db_warning = f"Rekordbox DB unavailable while loading duplicates: {db_exc}"

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
                "_mtime":         csv_mtime,
                "groups":         all_groups,
                "remove_paths":   [e["file_path"] for e in remove_entries],
                "keep_paths": [
                    e["file_path"]
                    for g in all_groups
                    for e in g["entries"]
                    if e["action"] == "KEEP"
                ],
                "total_remove_mb": round(
                    sum(e["file_size_mb"] for e in remove_entries), 1
                ),
                "db_warning": db_warning,
            }

        cached = _report_cache[cache_key]
        all_groups = cached["groups"]
        total = len(all_groups)
        start = page * per_page
        page_groups = all_groups[start : start + per_page]

        return jsonify({
            "groups":          page_groups,
            "total_groups":    total,
            "total_remove":    len(cached["remove_paths"]),
            "total_remove_mb": cached.get("total_remove_mb", 0),
            "db_warning":      cached.get("db_warning"),
            "page":            page,
            "per_page":        per_page,
            "csv_path":        str(csv_path),
        })

    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@bp.route("/api/duplicates/remove-paths")
def api_duplicates_remove_paths():
    """Return the full remove_paths and keep_paths lists for Select All operations."""
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


@bp.route("/api/open-file")
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
            subprocess.Popen(
                ["open", str(p)],
                close_fds=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        elif _sys == "Windows":
            os.startfile(str(p))  # type: ignore[attr-defined]
        else:
            subprocess.Popen(
                ["xdg-open", str(p)],
                close_fds=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@bp.route("/api/run/prune")
def api_run_prune():
    """
    Execute a confirmed prune. Expects ?token=<uuid> issued by POST /api/prune/stage.
    Returns an SSE stream of progress lines.
    """
    token = request.args.get("token", "")
    staged = _prune_token_store.pop(token, {})
    _PRUNE_TOKEN_TTL = 1800  # 30 minutes
    if staged and (time.time() - staged.get("_issued_at", 0)) > _PRUNE_TOKEN_TTL:
        staged = {}
    paths: list[str] = staged.get("paths", [])
    permanent: bool = staged.get("permanent", False)
    keeper_map: dict = staged.get("keeper_map", {})

    log_q: queue.Queue = queue.Queue()

    def _worker() -> None:
        try:
            if _rb_is_running():
                log_q.put(("line", "[ERROR] Rekordbox is open — close it before pruning."))
                log_q.put(("done", 1))
                return

            if not paths:
                log_q.put(("line", "[ERROR] No files were passed to the prune endpoint."))
                log_q.put(("done", 1))
                return

            from pruner import prune_files  # noqa: PLC0415
            from db_connection import write_db  # noqa: PLC0415
            from config import DJMT_DB as _DB  # noqa: PLC0415

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


# ── Scan cancel ───────────────────────────────────────────────────────────────

@bp.route("/api/cancel", methods=["POST"])
def api_cancel():
    """Send SIGTERM to all active subprocesses (graceful interrupt / checkpoint)."""
    count = 0
    with _proc_lock:
        for proc in list(_active_procs.values()):
            try:
                if proc.poll() is None:
                    proc.terminate()
                    count += 1
            except Exception:
                pass
        _active_procs.clear()

    if count > 0:
        return jsonify({"ok": True, "terminated": count})
    return jsonify({"ok": False, "error": "No active scan"}), 404


@bp.route("/api/cancel/force", methods=["POST"])
def api_cancel_force():
    """Send SIGKILL to all active subprocesses (emergency stop — server stays running)."""
    count = 0
    with _proc_lock:
        for proc in list(_active_procs.values()):
            try:
                if proc.poll() is None:
                    proc.kill()
                    count += 1
            except Exception:
                pass
        _active_procs.clear()

    if count > 0:
        return jsonify({"ok": True, "killed": count})
    return jsonify({"ok": False, "error": "No active scan"}), 404


# ── Normalize preview ─────────────────────────────────────────────────────────

def _preview_set(job_id: str, **kw) -> None:
    with _PREVIEW_LOCK:
        if job_id in _PREVIEW_JOBS:
            _PREVIEW_JOBS[job_id].update(kw)


def _preview_duration(path: Path) -> "float | None":
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=8,
        )
        v = float(r.stdout.strip())
        return v if v > 0 else None
    except Exception:
        return None


def _preview_lufs(path: Path, start: float) -> "float | None":
    """Measure integrated LUFS over _PREVIEW_WINDOW seconds starting at start."""
    try:
        r = subprocess.run(
            ["ffmpeg", "-ss", str(max(0, start)), "-t", str(_PREVIEW_WINDOW),
             "-i", str(path), "-af", "loudnorm=print_format=json",
             "-f", "null", "/dev/null"],
            capture_output=True, text=True, timeout=40,
        )
        m = _re.search(r'"input_i"\s*:\s*"(-?\d+\.?\d*)"', r.stderr)
        if m:
            val = float(m.group(1))
            return val if val > -70 else None
    except Exception:
        pass
    return None


def _preview_extract(src: Path, start: float, dest: Path) -> bool:
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-ss", str(max(0, start)), "-t", "10",
             "-i", str(src), "-acodec", "libmp3lame", "-q:a", "2",
             str(dest)],
            capture_output=True, timeout=30, check=True,
        )
        return True
    except Exception:
        return False


def _preview_normalize(src: Path, dest: Path) -> bool:
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(src),
             "-af", "loudnorm=I=-8:TP=-1.5:LRA=11",
             "-acodec", "libmp3lame", "-q:a", "2", str(dest)],
            capture_output=True, timeout=30, check=True,
        )
        return True
    except Exception:
        return False


def _run_preview_job(job_id: str, folder: Path) -> None:
    try:
        _preview_set(job_id, status="scanning", msg="Listing audio files…", progress=0, total=0)

        all_audio = [
            f for f in sorted(folder.iterdir())
            if f.suffix.lower() in _PREVIEW_AUDIO_EXTS and not f.name.startswith(".")
        ]

        qualified: list[tuple[Path, float]] = []
        for f in all_audio:
            d = _preview_duration(f)
            if d and d >= _PREVIEW_MIN_DUR:
                qualified.append((f, d))

        if len(qualified) < 2:
            _preview_set(job_id, status="error",
                         msg=f"Need at least 2 tracks ≥ 2 min (found {len(qualified)}).")
            return

        sample = (
            qualified
            if len(qualified) <= _PREVIEW_MAX_SCAN
            else _random.sample(qualified, _PREVIEW_MAX_SCAN)
        )

        _preview_set(job_id, status="measuring",
                     msg=f"Measuring loudness of {len(sample)} tracks…",
                     total=len(sample))

        measured: list[tuple[Path, float, float]] = []
        for i, (f, dur) in enumerate(sample):
            start = max(0, dur / 2 - _PREVIEW_WINDOW / 2)
            lufs = _preview_lufs(f, start)
            if lufs is not None:
                measured.append((f, dur, lufs))
            _preview_set(job_id, progress=i + 1)

        if len(measured) < 2:
            _preview_set(job_id, status="error",
                         msg="Could not measure loudness for enough tracks.")
            return

        measured.sort(key=lambda x: x[2])
        quietest = measured[0]
        loudest = measured[-1]

        _preview_set(job_id, status="extracting", msg="Extracting preview clips…")

        clips = []
        for tag, (f, dur, lufs) in [("q", quietest), ("l", loudest)]:
            clip_start = max(0, dur / 2 - 5)

            orig_id = f"{job_id}_{tag}_orig"
            norm_id = f"{job_id}_{tag}_norm"
            orig_path = _PREVIEW_TMP / f"{orig_id}.mp3"
            norm_path = _PREVIEW_TMP / f"{norm_id}.mp3"

            ok_orig = _preview_extract(f, clip_start, orig_path)
            ok_norm = ok_orig and _preview_normalize(orig_path, norm_path)

            clips.append({
                "clip_id": orig_id if ok_orig else None,
                "track":   f.name,
                "lufs":    round(lufs, 1),
                "label":   "Original",
                "kind":    "quietest" if tag == "q" else "loudest",
            })
            clips.append({
                "clip_id": norm_id if ok_norm else None,
                "track":   f.name,
                "lufs":    -8.0,
                "label":   "Normalized  −8 LUFS",
                "kind":    "quietest" if tag == "q" else "loudest",
            })

        _preview_set(job_id, status="done", msg="", clips=clips)

    except Exception as exc:
        _preview_set(job_id, status="error", msg=str(exc))


@bp.route("/api/normalize/preview", methods=["POST"])
def api_normalize_preview():
    data = request.get_json(silent=True) or {}
    path = data.get("path") or request.form.get("path", "")
    folder = Path(path)
    if not path or not folder.is_dir():
        return jsonify({"error": "valid folder path required"}), 400

    job_id = uuid.uuid4().hex[:8]
    with _PREVIEW_LOCK:
        _evict_old_jobs(_PREVIEW_JOBS, _MAX_PREVIEW_JOBS)
        _PREVIEW_JOBS[job_id] = {"status": "queued", "msg": "", "progress": 0,
                                 "total": 0, "clips": []}

    threading.Thread(target=_run_preview_job, args=(job_id, folder),
                     daemon=True, name=f"preview-{job_id}").start()
    return jsonify({"job_id": job_id})


@bp.route("/api/normalize/preview/<job_id>")
def api_normalize_preview_status(job_id):
    if not _re.match(r"^[0-9a-f]{8}$", job_id):
        return jsonify({"error": "invalid"}), 400
    with _PREVIEW_LOCK:
        job = _PREVIEW_JOBS.get(job_id)
    if job is None:
        return jsonify({"error": "not found"}), 404
    return jsonify(job)


@bp.route("/api/normalize/preview/clip/<clip_id>")
def api_normalize_preview_clip(clip_id):
    if not _re.match(r"^[0-9a-f]{8}_[ql]_(orig|norm)$", clip_id):
        return jsonify({"error": "invalid"}), 400
    clip_path = _PREVIEW_TMP / f"{clip_id}.mp3"
    if not clip_path.exists():
        return jsonify({"error": "not found"}), 404
    return send_file(str(clip_path), mimetype="audio/mpeg", conditional=True)
