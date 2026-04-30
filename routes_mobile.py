"""
routes_mobile.py — ── The Overlord ──

Flask Blueprint: all /api/mobile/* REST + WebSocket endpoints for the
FableGo iOS companion app, plus the /api/connectivity pairing panel route.
"""

import datetime
import json
import threading
import uuid
from pathlib import Path

from flask import Blueprint, jsonify, request, current_app

from helpers import (
    limiter,
    sock,
    _EXPORT_JOBS,
    _EXPORT_LOCK,
    _MAX_EXPORT_JOBS,
    _MAX_ANALYSIS_JOBS,
    _evict_old_jobs,
    _detect_pioneer_drive_layout,
    _run_export,
    REPO_ROOT,
)

bp = Blueprint("mobile", __name__)


def _norm_text(value):
    return " ".join(str(value or "").strip().lower().split())


def _track_identity_signature(track):
    title = _norm_text(getattr(track, "Title", ""))
    artist_name = ""
    try:
        artist_name = _norm_text(track.Artist.Name if track.Artist else "")
    except Exception:
        artist_name = ""

    duration = int(getattr(track, "Length", 0) or 0)
    file_name = _norm_text(Path(str(getattr(track, "FolderPath", "") or "")).name)
    return (artist_name, title, duration, file_name)


# ── Analysis job state (mobile-only) ─────────────────────────────────────────

_ANALYSIS_JOBS: dict[str, dict] = {}
_ANALYSIS_LOCK: threading.Lock = threading.Lock()


# ── Mobile auth helpers ───────────────────────────────────────────────────────

def _get_mobile_token() -> str:
    """
    Return the FableGo Bearer token, generating it if absent.

    Token is persisted in ~/.fablegear/config.json under "mobile_token".
    Printed to console once on first generation so the user can copy it.
    Returns empty string if FableGear hasn't been configured yet.
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
            print("  │  Copy this into FableGo → Settings → Auth Token        │")
            print("  └──────────────────────────────────────────────────────────┘")
            print()
        return cfg["mobile_token"]
    except Exception:
        return ""


MOBILE_TOKEN: str = _get_mobile_token()


def _read_mobile_token() -> str:
    """
    Read (but never generate) the current mobile token from config.

    Called on every authenticated mobile request so tokens configured after
    server start are accepted without a restart.
    Falls back to the module-level MOBILE_TOKEN if config can't be read.
    """
    try:
        from user_config import load_user_config, config_exists  # noqa: PLC0415
        if not config_exists():
            return ""
        cfg = load_user_config()
        return cfg.get("mobile_token", "") or ""
    except Exception:
        return MOBILE_TOKEN


# ── Auth gate (fires before every blueprint route) ────────────────────────────

import hmac as _hmac  # noqa: E402

@bp.before_request
@limiter.limit("10 per minute")
def _check_mobile_auth():
    """
    Require Bearer token for all /api/mobile/* routes except /api/mobile/ping.
    /api/connectivity is served without auth (desktop-only, already on localhost).
    Rate limited to 10 attempts per minute to resist brute-force.
    """
    if not request.path.startswith("/api/mobile/"):
        return  # /api/connectivity and other non-mobile paths skip auth
    if request.path == "/api/mobile/ping":
        return

    current_token = _read_mobile_token()
    if not current_token:
        return jsonify({
            "error": "server_not_configured",
            "message": "Run: python3 cli.py setup",
        }), 503

    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer ") or not _hmac.compare_digest(auth[7:], current_token):
        current_app.logger.warning(
            "Mobile API auth failed from %s for %s",
            request.remote_addr,
            request.path,
        )
        return jsonify({"error": "unauthorized"}), 401


# ── Health check ──────────────────────────────────────────────────────────────

@bp.route("/api/mobile/ping")
def mobile_ping():
    """Health check for FableGo. No auth required."""
    try:
        from update_checker import _local_version  # noqa: PLC0415
        _ver, _ = _local_version()
    except Exception:
        _ver = None
    ver = _ver or "unknown"
    return jsonify({"status": "ok", "version": ver, "fablegear_version": ver})


# ── Connectivity / QR pairing ─────────────────────────────────────────────────

@bp.route("/api/connectivity")
def api_connectivity():
    """
    Connection info for the FableGo pairing panel in the FableGear desktop UI.
    No auth required — served to the local desktop page only.
    """
    import socket
    import subprocess as _sp

    # Best local IP (non-loopback)
    local_ip: "str | None" = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
    except Exception:
        local_ip = "127.0.0.1"

    # Tailscale IP — fast check, 2 s timeout
    tailscale_ip: "str | None" = None
    try:
        ts = _sp.run(["tailscale", "ip", "-4"],
                     capture_output=True, text=True, timeout=2)
        if ts.returncode == 0:
            tailscale_ip = ts.stdout.strip() or None
    except Exception:
        pass

    # Mobile auth token
    token: "str | None" = None
    try:
        import json as _json
        import pathlib as _pl
        cfg_path = _pl.Path.home() / ".fablegear" / "config.json"
        if cfg_path.exists():
            token = _json.loads(cfg_path.read_text()).get("mobile_token")
    except Exception:
        pass

    best_ip = tailscale_ip or local_ip
    remote_ready = tailscale_ip is not None

    def _make_styled_qr(payload: str, fill: str = "#ff6600") -> "str | None":
        """Transparent-background SVG QR with a custom fill colour."""
        try:
            import qrcode, qrcode.image.svg, io, re  # noqa: E401
            qr = qrcode.make(payload,
                             image_factory=qrcode.image.svg.SvgPathImage,
                             box_size=6, border=2)
            buf = io.BytesIO()
            qr.save(buf)
            svg = buf.getvalue().decode("utf-8")
            svg = re.sub(r'<rect[^>]+fill=["\']#fff(?:fff)?["\'][^/]*/>', '', svg)
            svg = re.sub(r'<rect[^>]+fill=["\']white["\'][^/]*/>', '', svg)
            svg = re.sub(r"fill=['\"]#000(?:000)?['\"]", f'fill="{fill}"', svg)
            return svg
        except Exception:
            return None

    qr_svg: "str | None" = None
    if token and best_ip:
        qr_svg = _make_styled_qr(
            f"fablego://{best_ip}:5001?token={token}",
            fill="#ff6600",
        )

    qr_pwa_url: "str | None" = None
    if best_ip:
        qr_pwa_url = _make_styled_qr(f"http://{best_ip}:5001", fill="#ff6600")

    _green = "#34d399"
    qr_tailscale_mac = _make_styled_qr("https://tailscale.com/download/macos", fill=_green)
    qr_tailscale_ios = _make_styled_qr(
        "https://apps.apple.com/app/tailscale/id1470499037", fill=_green
    )
    qr_fablego_ios = _make_styled_qr(
        "https://github.com/fabledharbinger0993/FableGear", fill=_green
    )

    return jsonify({
        "local_ip":         local_ip,
        "tailscale_ip":     tailscale_ip,
        "port":             5001,
        "remote_ready":     remote_ready,
        "token":            token,
        "qr_svg":           qr_svg,
        "qr_pwa_url":       qr_pwa_url,
        "qr_tailscale_mac": qr_tailscale_mac,
        "qr_tailscale_ios": qr_tailscale_ios,
        "qr_fablego_ios":   qr_fablego_ios,
    })


# ── Folder browsing ───────────────────────────────────────────────────────────

@bp.route("/api/mobile/folders")
def mobile_folders():
    """List configured download folders with file counts."""
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


@bp.route("/api/mobile/folders/<path:folder_path>/files")
def mobile_folder_files(folder_path: str):
    """
    List audio files in a specific folder.
    SECURITY: Validates that folder_path stays within MUSIC_ROOT to prevent
    path traversal attacks.
    """
    from config import MUSIC_ROOT  # noqa: PLC0415

    p = Path("/" + folder_path) if not folder_path.startswith("/") else Path(folder_path)

    try:
        p_resolved = p.resolve()
    except (OSError, RuntimeError):
        return jsonify({"error": "invalid_path"}), 400

    music_root_resolved = MUSIC_ROOT.resolve()
    try:
        p_resolved.relative_to(music_root_resolved)
    except ValueError:
        current_app.logger.warning(
            "Path traversal attempt blocked: %s (outside %s)",
            p_resolved,
            music_root_resolved,
        )
        return jsonify({"error": "forbidden"}), 403

    if not p_resolved.is_dir():
        return jsonify({"error": "folder_not_found"}), 404

    audio_extensions = {
        ".mp3", ".wav", ".aiff", ".aif", ".aifc", ".flac",
        ".m4a", ".m4p", ".mp4", ".m4v", ".ogg", ".opus",
    }

    files = []
    try:
        for f in sorted(p_resolved.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
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


# ── Download ──────────────────────────────────────────────────────────────────

@bp.route("/api/mobile/download", methods=["POST"])
def mobile_download():
    """
    Enqueue a download job.
    Body: { "url": "...", "destination": "/...", "format": "aiff", "filename": "..." }
    Response: { "job_id": "uuid" }
    """
    import downloader  # noqa: PLC0415
    body = request.get_json(force=True, silent=True) or {}
    url = (body.get("url") or "").strip()
    destination = (body.get("destination") or "").strip()
    filename = (body.get("filename") or "").strip() or None
    fmt = (body.get("format") or downloader.DEFAULT_FORMAT).strip().lower()

    if not url:
        return jsonify({"error": "url is required"}), 400
    if not destination:
        return jsonify({"error": "destination is required"}), 400
    if fmt not in downloader.FORMATS:
        return jsonify({"error": f"format must be one of: {', '.join(sorted(downloader.FORMATS))}"}), 400

    job_id = downloader.enqueue(url, destination, filename, fmt)
    return jsonify({"job_id": job_id}), 202


@bp.route("/api/mobile/jobs")
def mobile_jobs():
    """Return all download jobs, newest first (capped at 200)."""
    import downloader  # noqa: PLC0415
    return jsonify(downloader.get_all_jobs())


@bp.route("/api/mobile/jobs/<job_id>")
def mobile_job(job_id: str):
    """Return a single download job by ID."""
    import downloader  # noqa: PLC0415
    job = downloader.get_job(job_id)
    if job is None:
        return jsonify({"error": "not_found"}), 404
    return jsonify(job)


# ── Rekordbox track list + add ────────────────────────────────────────────────

@bp.route("/api/mobile/rekordbox/tracks")
def mobile_rekordbox_tracks():
    """
    List tracks in the Rekordbox database.
    Query params: search, sort (date_added|title|artist|bpm), limit, offset.
    """
    from db_connection import read_db  # noqa: PLC0415
    from config import LOCAL_DB as _DB  # noqa: PLC0415

    search = request.args.get("search", "").strip().lower()
    sort = request.args.get("sort", "date_added")
    try:
        limit = int(request.args.get("limit", 200))
        offset = int(request.args.get("offset", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "limit and offset must be integers"}), 400

    try:
        with read_db(_DB) as db:
            rows = list(db.get_content())

        results = []
        for t in rows:
            title = t.Title or ""
            artist = t.Artist.Name if t.Artist else ""
            path = t.FolderPath or ""

            if not path or not path.startswith("/"):
                continue

            if search and search not in title.lower() and search not in artist.lower():
                continue

            bpm = round(t.BPM / 100, 1) if t.BPM else None
            key = t.Key.Name if t.Key else None

            date_added = None
            sd = t.StockDate
            if sd and isinstance(sd, (datetime.date, datetime.datetime)):
                try:
                    date_added = sd.isoformat()
                except Exception:
                    pass

            results.append({
                "id":          str(t.ID),
                "title":       title,
                "artist":      artist,
                "bpm":         bpm,
                "key":         key,
                "duration_ms": (t.Length * 1000) if t.Length else None,
                "file_path":   path,
                "date_added":  date_added,
            })

        if sort == "title":
            results.sort(key=lambda r: r["title"].lower())
        elif sort == "artist":
            results.sort(key=lambda r: r["artist"].lower())
        elif sort == "bpm":
            results.sort(key=lambda r: r["bpm"] or 0, reverse=True)
        else:
            results.sort(key=lambda r: r["date_added"] or "", reverse=True)

        return jsonify(results[offset: offset + limit])

    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@bp.route("/api/mobile/rekordbox/tracks", methods=["POST"])
def mobile_rekordbox_add_track():
    """
    Add a local audio file to the Rekordbox database.
    Body: { "file_path": "/absolute/path/to/track.mp3" }
    Response: { "track_id": "123456", "status": "added" }
    """
    from db_connection import write_db  # noqa: PLC0415
    from config import LOCAL_DB as _DB  # noqa: PLC0415

    data = request.get_json(silent=True) or {}
    file_path = data.get("file_path", "").strip()

    if not file_path:
        return jsonify({"error": "file_path required"}), 400

    p = Path(file_path)
    if not p.exists():
        return jsonify({"error": f"File not found: {file_path}"}), 404

    AUDIO_EXTS = {
        ".mp3", ".wav", ".aiff", ".aif", ".aifc", ".flac",
        ".m4a", ".m4p", ".mp4", ".m4v", ".ogg", ".opus",
    }
    if p.suffix.lower() not in AUDIO_EXTS:
        return jsonify({"error": f"Unsupported file type: {p.suffix}"}), 400

    try:
        with write_db(_DB) as db:
            track = db.add_content(file_path)
            db.commit()
            track_id = str(track.ID)
        return jsonify({"track_id": track_id, "status": "added"}), 201

    except ValueError as exc:
        if "already exists" in str(exc):
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
        return jsonify({"error": str(exc)}), 503

    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ── Track analysis ────────────────────────────────────────────────────────────

def _push_analysis_event(
    job_id: str,
    track_id: str,
    status: str,
    bpm: "float | None" = None,
    key: "str | None" = None,
    error: "str | None" = None,
) -> None:
    """Push a WebSocket analysis_update event to all connected FableGo clients."""
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
        pass


def _run_analysis(job_id: str, track_ids: list) -> None:
    """
    Background thread: detect BPM and key for each track, write results
    to file tags and to the Rekordbox DB.
    """
    from pathlib import Path as _Path  # noqa: PLC0415
    from audio_processor import process_file  # noqa: PLC0415
    from db_connection import read_db, write_db  # noqa: PLC0415
    from key_mapper import resolve_key_id  # noqa: PLC0415
    from config import LOCAL_DB as _DB  # noqa: PLC0415

    for track_id in track_ids:
        with _ANALYSIS_LOCK:
            _ANALYSIS_JOBS[job_id]["results"][track_id]["status"] = "analyzing"
        _push_analysis_event(job_id, track_id, "analyzing")

        bpm: "float | None" = None
        key: "str | None" = None
        db_note: "str | None" = None

        try:
            with read_db(_DB) as db:
                row = db.get_content(ID=track_id).one_or_none()
                if row is None:
                    raise ValueError(f"Track {track_id} not found in DB")
                file_path = row.FolderPath or ""
                if not file_path or not file_path.startswith("/"):
                    raise ValueError(f"Track {track_id} has no local file path")

            p = _Path(file_path)
            result = process_file(
                p,
                detect_bpm=True,
                detect_key=True,
                normalise=False,
                force=False,
            )
            bpm = result.bpm_detected
            key = result.key_detected

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
                db_note = "DB not updated (Rekordbox is open); file tags written."

            status = "complete_partial" if db_note else "complete"

        except Exception as exc:
            status = "failed"
            db_note = str(exc)

        with _ANALYSIS_LOCK:
            _ANALYSIS_JOBS[job_id]["results"][track_id].update({
                "status": status,
                "bpm":    bpm,
                "key":    key,
                "error":  db_note,
            })
        _push_analysis_event(job_id, track_id, status, bpm=bpm, key=key, error=db_note)

    with _ANALYSIS_LOCK:
        job_results = _ANALYSIS_JOBS[job_id].get("results", {})
        had_partial = any(r.get("status") == "complete_partial" for r in job_results.values())
        _ANALYSIS_JOBS[job_id]["status"] = "complete_partial" if had_partial else "complete"


@bp.route("/api/mobile/rekordbox/analyze", methods=["POST"])
def mobile_rekordbox_analyze():
    """
    Queue BPM + key analysis for one or more Rekordbox tracks.
    Body: { "track_ids": ["123456", "789012"] }
    Response: { "job_id": "uuid" }  (202 Accepted)
    """
    data = request.get_json(silent=True) or {}
    track_ids = data.get("track_ids") or []

    if not isinstance(track_ids, list) or not track_ids:
        return jsonify({"error": "track_ids must be a non-empty list"}), 400

    track_ids = [str(t) for t in track_ids[:50]]

    job_id = str(uuid.uuid4())

    with _ANALYSIS_LOCK:
        _evict_old_jobs(_ANALYSIS_JOBS, _MAX_ANALYSIS_JOBS)
        _ANALYSIS_JOBS[job_id] = {
            "job_id":    job_id,
            "track_ids": track_ids,
            "status":    "running",
            "results":   {tid: {"status": "queued", "bpm": None, "key": None, "error": None}
                          for tid in track_ids},
        }

    threading.Thread(
        target=_run_analysis,
        args=(job_id, track_ids),
        daemon=True,
        name=f"analysis-{job_id[:8]}",
    ).start()

    return jsonify({"job_id": job_id}), 202


@bp.route("/api/mobile/rekordbox/analyze/<job_id>")
def mobile_rekordbox_analyze_status(job_id: str):
    """Poll analysis job status."""
    with _ANALYSIS_LOCK:
        job = _ANALYSIS_JOBS.get(job_id)
    if job is None:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(job)


# ── Playlists CRUD ────────────────────────────────────────────────────────────

@bp.route("/api/mobile/rekordbox/playlists")
def mobile_rekordbox_playlists():
    """List all non-folder playlists with track count."""
    from db_connection import read_db  # noqa: PLC0415
    from config import LOCAL_DB as _DB  # noqa: PLC0415

    try:
        with read_db(_DB) as db:
            rows = db.get_playlist().all()
            result = []
            for pl in rows:
                if pl.Attribute != 0:
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


@bp.route("/api/mobile/rekordbox/playlists", methods=["POST"])
def mobile_rekordbox_create_playlist():
    """Create a new playlist. Body: { "name": "My Playlist" }"""
    from db_connection import write_db  # noqa: PLC0415
    from config import LOCAL_DB as _DB  # noqa: PLC0415

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


@bp.route("/api/mobile/rekordbox/playlists/<playlist_id>")
def mobile_rekordbox_playlist(playlist_id: str):
    """Get a single playlist with its ordered track list."""
    from db_connection import read_db  # noqa: PLC0415
    from config import LOCAL_DB as _DB  # noqa: PLC0415

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


@bp.route("/api/mobile/rekordbox/playlists/<playlist_id>", methods=["PUT"])
def mobile_rekordbox_rename_playlist(playlist_id: str):
    """Rename a playlist. Body: { "name": "New Name" }"""
    from db_connection import write_db  # noqa: PLC0415
    from config import LOCAL_DB as _DB  # noqa: PLC0415

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


@bp.route("/api/mobile/rekordbox/playlists/<playlist_id>", methods=["DELETE"])
def mobile_rekordbox_delete_playlist(playlist_id: str):
    """Delete a playlist (does not delete the tracks themselves)."""
    from db_connection import write_db  # noqa: PLC0415
    from config import LOCAL_DB as _DB  # noqa: PLC0415

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


@bp.route("/api/mobile/rekordbox/playlists/<playlist_id>/tracks", methods=["POST"])
def mobile_rekordbox_add_to_playlist(playlist_id: str):
    """Append a track to a playlist. Body: { "track_id": "123456" }"""
    from db_connection import write_db  # noqa: PLC0415
    from config import LOCAL_DB as _DB  # noqa: PLC0415

    data = request.get_json(silent=True) or {}
    track_id = str(data.get("track_id", "")).strip()
    if not track_id:
        return jsonify({"error": "track_id required"}), 400

    try:
        with write_db(_DB) as db:
            pl = db.get_playlist(ID=playlist_id).one_or_none()
            if pl is None:
                return jsonify({"error": "Playlist not found"}), 404
            if int(getattr(pl, "Attribute", 0) or 0) == 1:
                return jsonify({"error": "Cannot add tracks to a folder"}), 400

            existing_ids = set()
            existing_signatures = set()
            for song in db.get_playlist_songs(PlaylistID=pl.ID).all():
                existing_ids.add(str(getattr(song, "ContentID", "")))
                song_track = getattr(song, "Content", None)
                if song_track is not None:
                    existing_signatures.add(_track_identity_signature(song_track))

            track = db.get_content(ID=track_id).one_or_none()
            if track is None:
                return jsonify({"error": "Track not found"}), 404

            signature = _track_identity_signature(track)
            if str(track.ID) in existing_ids or signature in existing_signatures:
                return jsonify({"status": "skipped", "reason": "already present"}), 200

            db.add_to_playlist(pl, track, track_no=None)
            db.commit()
            return jsonify({"status": "added"}), 201
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 503
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@bp.route(
    "/api/mobile/rekordbox/playlists/<playlist_id>/tracks/<track_id>",
    methods=["DELETE"],
)
def mobile_rekordbox_remove_from_playlist(playlist_id: str, track_id: str):
    """Remove a track from a playlist (does not delete the track from the library)."""
    from db_connection import write_db  # noqa: PLC0415
    from config import LOCAL_DB as _DB  # noqa: PLC0415

    try:
        with write_db(_DB) as db:
            songs = db.get_playlist_songs(
                PlaylistID=playlist_id, ContentID=track_id
            ).all()
            if not songs:
                return jsonify({"error": "Track not in playlist"}), 404

            removed = 0
            for song in songs:
                db.remove_from_playlist(playlist_id, song.ID)
                removed += 1
            db.commit()
            return jsonify({"status": "removed", "removed": removed})
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 503
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ── Drive detection ───────────────────────────────────────────────────────────

@bp.route("/api/mobile/drives")
def mobile_drives():
    """List mounted Pioneer-compatible drives."""
    try:
        import psutil  # noqa: PLC0415
        drives = []
        for part in psutil.disk_partitions():
            mp = part.mountpoint
            if not mp.startswith("/Volumes"):
                continue
            try:
                usage = psutil.disk_usage(mp)
                name = Path(mp).name
                drive_info = _detect_pioneer_drive_layout(mp)
                drives.append({
                    "path":        mp,
                    "name":        name,
                    "free_bytes":  usage.free,
                    "total_bytes": usage.total,
                    **drive_info,
                })
            except (PermissionError, OSError):
                continue
        return jsonify(drives)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ── USB export ────────────────────────────────────────────────────────────────

@bp.route("/api/mobile/export", methods=["POST"])
def mobile_export_start():
    """
    Start a USB export job.
    Body: { "playlist_ids": ["123", "456"], "drive_path": "/Volumes/DJMT" }
    Response: { "job_id": "uuid" }  (202)
    """
    data = request.get_json(silent=True) or {}
    playlist_ids = data.get("playlist_ids") or []
    drive_path = (data.get("drive_path") or "").strip()

    if not playlist_ids:
        return jsonify({"error": "playlist_ids required"}), 400
    if not drive_path:
        return jsonify({"error": "drive_path required"}), 400

    drive_info = _detect_pioneer_drive_layout(drive_path)
    if not drive_info.get("pioneer"):
        return jsonify({"error": f"No Pioneer export structure detected on {drive_path}"}), 400
    if not drive_info.get("export_supported"):
        return jsonify({"error": drive_info.get("export_error")}), 400

    job_id = str(uuid.uuid4())

    with _EXPORT_LOCK:
        _evict_old_jobs(_EXPORT_JOBS, _MAX_EXPORT_JOBS)
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


@bp.route("/api/mobile/export/<job_id>")
def mobile_export_status(job_id: str):
    """Poll export job status."""
    with _EXPORT_LOCK:
        job = _EXPORT_JOBS.get(job_id)
    if job is None:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(job)


# ── WebSocket event bus ───────────────────────────────────────────────────────

@sock.route("/api/mobile/events")
def mobile_events(ws):
    """
    WebSocket event bus for FableGo.

    The mobile app connects here on startup and holds the connection open.
    Auth checked via the before_request hook.
    Keep-alive: client should send any message (e.g. "ping") every ~25s.
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
