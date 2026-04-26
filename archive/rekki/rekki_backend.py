# ARCHIVED Rekki backend code from app.py
# Restore by re-inserting at the appropriate locations.

# == Comment at line 86 ==
# ── Rekki brain: Congress deliberation + HologrA.I.m memory ──────────────────

# == _rekki_enabled() function ==

def _rekki_enabled() -> bool:
    return _current_rekitbox_mode() == "suburban"

# == DB health + scripted module + routes ==


def _rekki_sqlite_health() -> dict:
    """Read-only SQLite snapshot for Rekordbox DB sanity checks."""
    try:
        from config import DJMT_DB  # noqa: PLC0415
    except Exception as exc:
        return {"ok": False, "error": f"config unavailable: {exc}"}

    db_path = Path(DJMT_DB)
    info = {
        "ok": True,
        "db_path": str(db_path),
        "exists": db_path.exists(),
        "size_bytes": None,
        "mtime": None,
        "integrity": None,
        "quick_check": None,
        "tables": {},
        "errors": [],
    }

    if not db_path.exists():
        info["ok"] = False
        info["error"] = "rekordbox database not found"
        return info

    try:
        st = db_path.stat()
        info["size_bytes"] = st.st_size
        info["mtime"] = datetime.datetime.fromtimestamp(st.st_mtime).isoformat()
    except Exception as exc:
        info["errors"].append(f"stat failed: {exc}")

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        try:
            integrity = conn.execute("PRAGMA integrity_check;").fetchone()
            quick = conn.execute("PRAGMA quick_check;").fetchone()
            info["integrity"] = integrity[0] if integrity else None
            info["quick_check"] = quick[0] if quick else None

            table_queries = {
                "djmdContent": "SELECT COUNT(*) FROM djmdContent",
                "djmdPlaylist": "SELECT COUNT(*) FROM djmdPlaylist",
                "djmdCue": "SELECT COUNT(*) FROM djmdCue",
                "content_missing_folder": (
                    "SELECT COUNT(*) FROM djmdContent "
                    "WHERE FolderPath IS NULL OR TRIM(FolderPath) = ''"
                ),
                "content_missing_bpm": (
                    "SELECT COUNT(*) FROM djmdContent "
                    "WHERE BPM IS NULL OR CAST(BPM AS INTEGER) = 0"
                ),
                "content_missing_key": (
                    "SELECT COUNT(*) FROM djmdContent "
                    "WHERE KeyID IS NULL OR CAST(KeyID AS INTEGER) = 0"
                ),
            }
            for key, sql in table_queries.items():
                try:
                    row = conn.execute(sql).fetchone()
                    info["tables"][key] = int(row[0]) if row else 0
                except Exception as exc:
                    info["tables"][key] = None
                    info["errors"].append(f"{key} query failed: {exc}")
        finally:
            conn.close()
    except Exception as exc:
        info["ok"] = False
        info["error"] = f"sqlite check failed: {exc}"

    return info


def _rekki_pyrekordbox_health() -> dict:
    """Read-only pyrekordbox snapshot for high-level record counts."""
    out = {
        "ok": True,
        "tracks": None,
        "playlists": None,
        "errors": [],
    }
    try:
        from db_connection import read_db  # noqa: PLC0415

        with read_db() as db:
            try:
                out["tracks"] = int(db.get_content().count())
            except Exception as exc:
                out["errors"].append(f"tracks count failed: {exc}")
            try:
                out["playlists"] = int(db.get_playlist().count())
            except Exception as exc:
                out["errors"].append(f"playlists count failed: {exc}")
    except Exception as exc:
        out["ok"] = False
        out["error"] = f"pyrekordbox read failed: {exc}"

    return out


def _rekki_db_health_snapshot() -> dict:
    sqlite_health = _rekki_sqlite_health()
    pyrekordbox_health = _rekki_pyrekordbox_health()
    return {
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "sqlite": sqlite_health,
        "pyrekordbox": pyrekordbox_health,
    }


# ── Rekki (local scripted assistant panel) ─────────────────────────────────

_REKKI_DEFAULT_MODEL = os.environ.get("REKIT_AGENT_MODEL", "rekki-scripted-v1")
_REKKI_PROFILE = os.environ.get("REKIT_AGENT_PROFILE", "default")
_REKKI_AUTOMATION_SCRIPT = REPO_ROOT / "scripts" / "agent_workflow.sh"
_REKKI_SCRIPTED_MODEL = "rekki-scripted-v1"


def _rekki_chat_url() -> str:
    return os.environ.get("REKIT_OLLAMA_URL", "http://127.0.0.1:11434/api/chat")


def _rekki_base_url() -> str:
    chat_url = _rekki_chat_url()
    parsed = urllib.parse.urlparse(chat_url)
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return "http://127.0.0.1:11434"


def _rekki_http_post_json(url: str, payload: dict, timeout: int = 30) -> dict:
    raise RuntimeError("External AI calls are disabled for Rekki scripted mode")


def _rekki_http_get_json(url: str, timeout: int = 10) -> dict:
    raise RuntimeError("External AI calls are disabled for Rekki scripted mode")


def _rekki_list_models() -> list[str]:
    return [_REKKI_SCRIPTED_MODEL]


def _rekki_resolve_model(requested_model: str) -> tuple[str, bool, str | None]:
    return _REKKI_SCRIPTED_MODEL, True, None


def _rekki_automation_env(model: str, profile: str) -> dict:
    env = os.environ.copy()
    env["REKIT_AGENT_PROVIDER"] = "scripted-local"
    env["REKIT_AGENT_MODEL"] = _REKKI_SCRIPTED_MODEL
    env["REKIT_AGENT_PROFILE"] = profile or _REKKI_PROFILE
    return env


def _rekki_tool_from_text(text: str) -> str | None:
    checks = [
        ("duplicate", "duplicate_detector"),
        ("dedupe", "duplicate_detector"),
        ("normalize", "normalizer"),
        ("bpm", "audio_processor"),
        ("key", "audio_processor"),
        ("tag", "audio_processor"),
        ("organize", "library_organizer"),
        ("relocate", "relocator"),
        ("missing file", "relocator"),
        ("import", "importer"),
        ("playlist", "library"),
        ("library", "library"),
        ("export", "export"),
        ("usb", "export"),
        ("pioneer", "export"),
        ("rekitgo", "mobile"),
        ("mobile", "mobile"),
        ("audit", "audit"),
        ("rename", "renamer"),
        ("novelty", "novelty_scanner"),
    ]
    for needle, tool in checks:
        if needle in text:
            return tool
    return None


def _rekki_action_plan(tool: str, context: dict) -> list[str]:
    db_ok = bool((((context or {}).get("db_health") or {}).get("sqlite") or {}).get("ok"))
    rb_running = bool((context or {}).get("rb_running"))
    backup_exists = bool((((context or {}).get("backup") or {}).get("exists")))

    preflight = []
    if not db_ok:
        preflight.append("Run Audit Library first and review DB health before any write action.")
    if rb_running:
        preflight.append("Close Rekordbox before write actions to avoid lock conflicts.")
    if not backup_exists:
        preflight.append("Create a backup first. No backup means no safe rollback.")

    steps_by_tool = {
        "audio_processor": [
            "Run Tag Tracks on the target folders.",
            "Review skipped files and rerun only failed paths.",
            "Open Library Editor and sort by BPM/Key to verify coverage.",
        ],
        "duplicate_detector": [
            "Run Duplicate Tracks scan first (read-only).",
            "Review confidence groups before any prune action.",
            "Keep one canonical copy per recording and preserve folder paths.",
        ],
        "relocator": [
            "Set old path prefix and new path prefix in Relocate Paths.",
            "Run relocate and validate random tracks in Library Editor stream preview.",
            "If unresolved tracks remain, rerun with narrower path prefixes.",
        ],
        "library_organizer": [
            "Run in dry-style review mode first if available.",
            "Apply organize only after backup confirmation.",
            "Re-check playlist links and relocate any moved paths if needed.",
        ],
        "importer": [
            "Use Import Tracks for new source folders.",
            "Verify imported rows in Library Editor and play-test a sample.",
            "Link imported tracks into playlists after import completes.",
        ],
        "library": [
            "Load Library, select/curate playlists, and use Add/Remove Selected.",
            "Rename or delete playlists as needed.",
            "Patch track titles only when metadata is confirmed.",
        ],
        "export": [
            "Insert Pioneer USB with existing PIONEER/Master/master.db.",
            "Open Export to USB, select target drive and playlists.",
            "Run export and wait for completion before unplugging the drive.",
        ],
        "mobile": [
            "Ensure Tailscale path is reachable and /api/mobile/ping is healthy.",
            "Use RekitGo for remote playlist edits and export control.",
            "Keep token auth enabled for all mobile routes.",
        ],
        "audit": [
            "Run Audit Library and inspect missing files, BPM/key gaps, and path drift.",
            "Fix high-risk issues first: missing paths and DB integrity warnings.",
            "Use findings to drive relocate/tag/import follow-up actions.",
        ],
        "renamer": [
            "Preview rename results first.",
            "Apply rename only on confirmed selections.",
            "Re-audit paths to ensure no broken links were introduced.",
        ],
        "novelty_scanner": [
            "Scan source drive for unknown tracks.",
            "Copy selected additions into library root.",
            "Import copied tracks into DB and then playlist them.",
        ],
    }

    return [*preflight, *(steps_by_tool.get(tool, [
        "Run Audit Library for current state.",
        "Choose the matching tool card and execute one step at a time.",
        "Re-check status and logs before the next write operation.",
    ]))]


def _rekki_scripted_reply(user_message: str, source: str, context: dict) -> str:
    msg = (user_message or "").strip()
    lower = msg.lower()
    tool = _rekki_tool_from_text(lower) or _rekki_tool_from_text((source or "").lower())

    if any(k in lower for k in ["hi", "hello", "hey", "yo"]) and len(lower) < 30:
        return (
            "I am in scripted local mode. No outside calls, no model inference. "
            "Tell me the exact task (paths, playlist goal, or export target) and I will give you a step-by-step runbook."
        )

    if any(k in lower for k in ["error", "failed", "not working", "broken", "stuck", "can\'t", "cannot"]):
        plan = _rekki_action_plan(tool or "audit", context)
        return "Issue triage:\n- " + "\n- ".join(plan[:4])

    if "search" in lower or "find" in lower:
        return (
            "Fast search workflow:\n"
            "- Load Library and search by title/artist/album first.\n"
            "- Use playlist narrowing to reduce candidate set.\n"
            "- Keep naming consistent (artist/title) to improve hit quality.\n"
            "- Next upgrade path: BPM/key/date filters in mobile + desktop endpoints."
        )

    if "rekitgo" in lower or "mobile" in lower or "tailscale" in lower:
        return (
            "RekitGo remote control checklist:\n"
            "- Confirm /api/mobile/ping responds over Tailscale.\n"
            "- Keep bearer token auth enabled.\n"
            "- Use mobile playlist CRUD and export routes for remote operations.\n"
            "- If export stalls, poll /api/mobile/export/<job_id> until complete/failed."
        )

    if tool:
        plan = _rekki_action_plan(tool, context)
        return f"{tool} runbook:\n- " + "\n- ".join(plan)

    return (
        "General RekitBox runbook:\n"
        "- Start with Audit Library to establish current health.\n"
        "- Fix path issues with Relocate Paths.\n"
        "- Fill metadata gaps with Tag Tracks (BPM/Key).\n"
        "- Curate playlists in Library Editor.\n"
        "- Export selected playlists to Pioneer USB after backup confirmation."
    )


def _rekki_infer_context_local(scrape: dict) -> dict:
    scrape = scrape or {}
    element_text = str(scrape.get("elementText", "")).strip()
    section = str(scrape.get("sectionHeading", "")).strip()
    tool_panel = str(scrape.get("toolPanel", "")).strip()
    attrs = scrape.get("existingAttributes", {}) or {}
    page_state = scrape.get("pageState", {}) or {}

    blob = " ".join([
        element_text.lower(),
        section.lower(),
        tool_panel.lower(),
        str(attrs).lower(),
        str(page_state.get("activeTool", "")).lower(),
        str(page_state.get("lastRunStatus", "")).lower(),
    ])

    tool = _rekki_tool_from_text(blob)
    severity = "info"
    if any(k in blob for k in ["error", "failed", "exception", "missing"]):
        severity = "error"
    elif any(k in blob for k in ["warn", "caution", "duplicate", "delete", "prune"]):
        severity = "warn"
    elif any(k in blob for k in ["success", "complete", "healthy", "ok"]):
        severity = "safe"

    inferred_type = str(attrs.get("type") or "").strip() or "generic"
    if inferred_type == "generic":
        if "playlist" in blob:
            inferred_type = "playlist"
        elif "track" in blob:
            inferred_type = "track-row"
        elif "status" in blob:
            inferred_type = "status-pill"
        elif "button" in blob:
            inferred_type = "button"
        elif "log" in blob:
            inferred_type = "log-entry"

    label = str(attrs.get("label") or "").strip()[:60]
    if not label:
        label = (section or tool_panel or element_text or "RekitBox context")[:60]

    if tool:
        description = f"This area belongs to {tool}. I can guide the safest next step and what to verify before writing to DB."
    else:
        description = "I can explain this UI area and provide the next safe operation sequence."

    return {
        "type": inferred_type,
        "label": label,
        "description": description,
        "tool": tool,
        "severity": severity,
    }


def _rekki_automation_status(model: str, profile: str) -> tuple[bool, str, int]:
    if not _REKKI_AUTOMATION_SCRIPT.exists():
        return False, "agent workflow script not found", 404
    proc = subprocess.run(
        ["bash", str(_REKKI_AUTOMATION_SCRIPT), "status"],
        cwd=str(REPO_ROOT),
        env=_rekki_automation_env(model, profile),
        capture_output=True,
        text=True,
        timeout=15,
    )
    output = (proc.stdout or "").strip() or (proc.stderr or "").strip()
    return proc.returncode == 0, output, proc.returncode


def _rekki_automation_action(action: str, model: str, profile: str) -> tuple[bool, str, int]:
    if action not in {"start", "stop", "once"}:
        return False, "unsupported action", 400
    if not _REKKI_AUTOMATION_SCRIPT.exists():
        return False, "agent workflow script not found", 404
    proc = subprocess.run(
        ["bash", str(_REKKI_AUTOMATION_SCRIPT), action],
        cwd=str(REPO_ROOT),
        env=_rekki_automation_env(model, profile),
        capture_output=True,
        text=True,
        timeout=120 if action == "once" else 20,
    )
    output = (proc.stdout or "").strip() or (proc.stderr or "").strip()
    return proc.returncode == 0, output, proc.returncode


def _rekki_context_snapshot() -> dict:
    with _proc_lock:
        # Check if any SSE streams are active
        active = any(proc.poll() is None for proc in _active_procs.values())

    last_response = None
    state_dir = REPO_ROOT / ".git" / "agent-workflow"
    response_file = state_dir / "response.json"
    try:
        if response_file.exists():
            last_response = json.loads(response_file.read_text(encoding="utf-8"))
    except Exception:
        last_response = None

    return {
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "rb_running": _rb_is_running(),
        "scan_running": active,
        "backup": _backup_info(),
        "release": _release_info(),
        "db_health": _rekki_db_health_snapshot(),
        "agent_last_response": last_response,
    }


@app.route("/api/rekki/status")
def api_rekki_status():
    model = _REKKI_SCRIPTED_MODEL
    resolved_model, model_ok, model_error = _rekki_resolve_model(model)
    result = {
        "ok": True,
        "name": "Rekki",
        "provider": "scripted-local",
        "model": model,
        "resolved_model": resolved_model,
        "model_resolved": model != resolved_model,
        "profile": os.environ.get("REKIT_AGENT_PROFILE", _REKKI_PROFILE),
        "ollama_base": None,
        "ollama_reachable": True,
        "model_available": True,
        "external_calls_blocked": True,
        "error": None,
    }
    if not model_ok:
        result["error"] = model_error

    ok, status_text, status_code = _rekki_automation_status(
        resolved_model,
        os.environ.get("REKIT_AGENT_PROFILE", _REKKI_PROFILE),
    )
    result["automation_ok"] = ok
    result["automation_status"] = status_text
    result["automation_status_code"] = status_code
    return jsonify(result)


@app.route("/api/rekki/context")
def api_rekki_context():
    if not _rekki_enabled():
        return jsonify({"error": "Rekki is disabled in Rural mode."}), 403
    return jsonify(_rekki_context_snapshot())


@app.route("/api/rekki/db-health")
def api_rekki_db_health():
    return jsonify(_rekki_db_health_snapshot())


@app.route("/api/rekki/history")
def api_rekki_history():
    """Return recent chat history for client hydration on page load.

    The JS side calls this once on DOMContentLoaded to repopulate _rekkiHistory
    and render past messages so every surface (main panel, wizard, card buttons)
    shares a single continuous thread across sessions.
    """
    limit = min(int(request.args.get("limit", 30)), 100)
    if not _REKKI_MEMORY_ENABLED:
        return jsonify({"ok": True, "messages": [], "memory_enabled": False})
    try:
        db = get_memory_db()
        # get_recent_chat_messages already returns oldest-first, typing excluded
        rows = db.get_recent_chat_messages(limit)
        messages = [
            {
                "role": r["role"],
                "content": r["content"],
                "source": r.get("source", "main"),
                "timestamp": r.get("timestamp", ""),
            }
            for r in rows
        ]
        return jsonify({"ok": True, "messages": messages, "memory_enabled": True})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc), "messages": []}), 500


@app.route("/api/rekki/discover-music")
def api_rekki_discover_music():
    """Walk a directory and return audio files not already in the scan index.

    GET /api/rekki/discover-music?path=<dir>&limit=200

    Returns:
        {ok, discovered: [{path, size_mb, ext}], total, library_source}
    """
    _AUDIO_EXTS = {".mp3", ".wav", ".aif", ".aiff", ".flac", ".m4a", ".ogg", ".opus"}
    raw_path = request.args.get("path", "").strip()
    try:
        limit = min(int(request.args.get("limit", 200)), 500)
    except (ValueError, TypeError):
        limit = 200

    if not raw_path:
        return jsonify({"ok": False, "error": "path parameter is required"}), 400

    search_dir = os.path.realpath(raw_path)
    if not os.path.isdir(search_dir):
        return jsonify({"ok": False, "error": f"Not a directory: {search_dir}"}), 400

    # Load known paths from scan_index.json if it exists
    known_paths: set = set()
    library_source = "none"
    scan_index_path = os.path.join(os.path.dirname(__file__), "data", "scan_index.json")
    if os.path.isfile(scan_index_path):
        try:
            with open(scan_index_path, encoding="utf-8") as _f:
                _idx = json.load(_f)
            if isinstance(_idx, dict):
                known_paths = {os.path.realpath(p) for p in _idx.keys()}
            elif isinstance(_idx, list):
                known_paths = {os.path.realpath(str(p)) for p in _idx}
            library_source = "scan_index.json"
        except Exception:
            pass

    # Also load known paths from the Rekordbox DB (FolderPath column in djmdContent).
    # This catches tracks that are in the library but haven't been scanned by RekitBox yet.
    # Read-only connection — no write risk.
    from config import DJMT_DB  # noqa: PLC0415
    _rb_db = Path(DJMT_DB)
    if _rb_db.exists():
        try:
            _conn = sqlite3.connect(f"file:{_rb_db}?mode=ro", uri=True, timeout=3)
            try:
                for (fp,) in _conn.execute(
                    "SELECT FolderPath FROM djmdContent WHERE FolderPath IS NOT NULL"
                ):
                    known_paths.add(os.path.realpath(fp))
            finally:
                _conn.close()
            library_source = "rekordbox + scan_index" if library_source != "none" else "rekordbox"
        except Exception:
            pass  # DB locked or unavailable — scan_index result is still valid

    discovered = []
    try:
        for dirpath, _dirs, files in os.walk(search_dir):
            for fname in files:
                ext = os.path.splitext(fname)[1].lower()
                if ext not in _AUDIO_EXTS:
                    continue
                full = os.path.realpath(os.path.join(dirpath, fname))
                if full in known_paths:
                    continue
                try:
                    size_bytes = os.path.getsize(full)
                except OSError:
                    size_bytes = 0
                discovered.append({
                    "path": full,
                    "size_mb": round(size_bytes / (1024 * 1024), 2),
                    "ext": ext,
                })
                if len(discovered) >= limit:
                    break
            if len(discovered) >= limit:
                break
    except PermissionError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 403

    return jsonify({
        "ok": True,
        "discovered": discovered,
        "total": len(discovered),
        "library_source": library_source,
    })
def _sse_response(