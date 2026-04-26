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
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
import datetime
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from flask import Flask, Response, jsonify, redirect, render_template, render_template_string, request, send_file, send_from_directory, g
from flask_sock import Sock
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from mutagen import File as MutagenFile

import mimetypes

_playback_import_errors = []
try:
    import sounddevice as _sounddevice
except Exception as exc:  # pragma: no cover
    _sounddevice = None
    _playback_import_errors.append(f"sounddevice unavailable: {exc}")

try:
    import soundfile as _soundfile
except Exception as exc:  # pragma: no cover
    _soundfile = None
    _playback_import_errors.append(f"soundfile unavailable: {exc}")

_PLAYBACK_AVAILABLE = _sounddevice is not None and _soundfile is not None
_PLAYBACK_IMPORT_ERROR = "; ".join(_playback_import_errors) or None

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

# Rate limiting for mobile API authentication
limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    default_limits=["1000 per hour"],
    storage_uri="memory://",
)

# ── Rekki brain: Congress deliberation + HologrA.I.m memory ──────────────────
# ── RekitBox Native Playlist/Track API ───────────────────────────────────────

def _library_track_payload(track, *, track_no=None):
    import datetime  # noqa: PLC0415

    date_added = None
    stock_date = getattr(track, "StockDate", None)
    if stock_date and isinstance(stock_date, (datetime.date, datetime.datetime)):
        try:
            date_added = stock_date.isoformat()
        except Exception:
            date_added = None

    raw_rating = int(getattr(track, "Rating", 0) or 0)
    stars = 0 if raw_rating == 0 else max(1, min(5, round(raw_rating / 51)))

    color_id = int(getattr(track, "ColorID", 0) or 0)

    genre_name = ""
    try:
        genre_name = track.Genre.Name if track.Genre else ""
    except Exception:
        pass

    label_name = ""
    try:
        label_name = track.Label.Name if track.Label else ""
    except Exception:
        pass

    comment = str(getattr(track, "Commnt", "") or "").strip()
    play_count = int(getattr(track, "DJPlayCount", 0) or 0)

    return {
        "id":         str(track.ID),
        "title":      track.Title or "",
        "artist":     track.Artist.Name if track.Artist else "",
        "album":      track.Album.Name if getattr(track, "Album", None) else "",
        "genre":      genre_name,
        "label":      label_name,
        "bpm":        round(track.BPM / 100, 1) if track.BPM else None,
        "key":        track.Key.Name if track.Key else None,
        "key_id":     int(track.KeyID) if track.KeyID else None,
        "duration":   track.Length if track.Length else None,
        "date_added": date_added,
        "file_path":  track.FolderPath or "",
        "rating":     stars,
        "color":      color_id,
        "play_count": play_count,
        "comment":    comment,
        "track_no":   track_no,
    }


def _norm_text(value):
    return " ".join(str(value or "").strip().lower().split())


def _track_identity_signature(track):
    """Logical identity used to avoid duplicate playlist entries across alt content rows."""
    title = _norm_text(getattr(track, "Title", ""))
    artist_name = ""
    try:
        artist_name = _norm_text(track.Artist.Name if track.Artist else "")
    except Exception:
        artist_name = ""

    duration = int(getattr(track, "Length", 0) or 0)
    file_name = _norm_text(Path(str(getattr(track, "FolderPath", "") or "")).name)
    # Prefer artist/title/duration; use filename fallback when tags are weak.
    return (artist_name, title, duration, file_name)


def _playlist_tree_payload(db):
    rows = db.get_playlist().all()
    songs_by_playlist = {}
    nodes_by_id = {}
    roots = []

    for song in db.get_playlist_songs().all():
        playlist_id = str(song.PlaylistID)
        songs_by_playlist[playlist_id] = songs_by_playlist.get(playlist_id, 0) + 1

    for playlist in rows:
        attribute = int(getattr(playlist, "Attribute", 0) or 0)
        node = {
            "id": str(playlist.ID),
            "name": playlist.Name or "",
            "type": "folder" if attribute == 1 else "playlist",
            "track_count": songs_by_playlist.get(str(playlist.ID), 0),
            "children": [],
            "parent_id": str(getattr(playlist, "ParentID", "") or ""),
            "seq": int(getattr(playlist, "Seq", 0) or 0),
        }
        nodes_by_id[node["id"]] = node

    def _sort_key(node):
        return (node.get("seq", 0), node["name"].lower())

    for node in nodes_by_id.values():
        parent_id = node.pop("parent_id")
        if parent_id and parent_id in nodes_by_id:
            nodes_by_id[parent_id]["children"].append(node)
        else:
            roots.append(node)

    def _finalize(nodes):
        ordered = sorted(nodes, key=_sort_key)
        for node in ordered:
            node["children"] = _finalize(node["children"])
            node.pop("seq", None)
        return ordered

    return _finalize(roots)


def _library_canonical_path_conflicts(db):
    """Return (tracks_scanned, conflict_groups) for canonical-path integrity checks."""

    def _norm(value):
        return " ".join(str(value or "").strip().lower().split())

    tracks = db.get_content().all()
    grouped = {}
    for track in tracks:
        title = _norm(getattr(track, "Title", ""))
        artist_name = ""
        try:
            artist_name = _norm(track.Artist.Name if track.Artist else "")
        except Exception:
            artist_name = ""

        duration = int(getattr(track, "Length", 0) or 0)
        path = str(getattr(track, "FolderPath", "") or "").strip()

        # Skip weak signatures that cannot be trusted for canonical checks.
        if not title or not path:
            continue

        signature = (artist_name, title, duration)
        grouped.setdefault(signature, []).append(track)

    conflicts = []
    for signature, rows in grouped.items():
        distinct_paths = {
            str(getattr(row, "FolderPath", "") or "").strip()
            for row in rows
            if str(getattr(row, "FolderPath", "") or "").strip()
        }
        if len(distinct_paths) <= 1:
            continue

        items = []
        for row in rows:
            row_path = str(getattr(row, "FolderPath", "") or "").strip()
            if not row_path:
                continue
            playlist_refs = db.get_playlist_songs(ContentID=row.ID).all()
            items.append({
                "content_id": str(row.ID),
                "path": row_path,
                "exists_on_disk": os.path.isfile(row_path),
                "playlist_ref_count": len(playlist_refs),
            })

        artist_name, title, duration = signature
        conflicts.append({
            "signature": {
                "artist": artist_name,
                "title": title,
                "duration": duration,
            },
            "path_count": len(distinct_paths),
            "entries": items,
        })

    conflicts.sort(key=lambda g: (g["path_count"], len(g["entries"])), reverse=True)
    return len(tracks), conflicts


@app.route("/api/library/tracks")
def api_library_tracks():
    from db_connection import read_db  # noqa: PLC0415
    from config import DJMT_DB as _DB  # noqa: PLC0415

    try:
        with read_db(_DB) as db:
            tracks = [_library_track_payload(track) for track in db.get_content().all()]
            return jsonify(tracks)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/library/integrity/canonical-paths")
def api_library_integrity_canonical_paths():
    """
    Detect likely duplicate logical tracks that point at multiple physical paths.
    This is read-only and intended to support canonical-path cleanup workflows.
    """
    from db_connection import read_db  # noqa: PLC0415
    from config import DJMT_DB as _DB  # noqa: PLC0415

    try:
        with read_db(_DB) as db:
            tracks_scanned, conflicts = _library_canonical_path_conflicts(db)

            return jsonify({
                "ok": True,
                "total_tracks_scanned": tracks_scanned,
                "conflict_group_count": len(conflicts),
                "groups": conflicts,
            })
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/library/integrity/canonical-paths/plan")
def api_library_integrity_canonical_paths_plan():
    """
    Build a read-only consolidation plan for canonical path cleanup.
    No DB writes are performed.
    """
    from db_connection import read_db  # noqa: PLC0415
    from config import DJMT_DB as _DB  # noqa: PLC0415

    try:
        try:
            max_groups = int(request.args.get("max_groups", 50))
        except (TypeError, ValueError):
            max_groups = 50
        max_groups = max(1, min(500, max_groups))

        with read_db(_DB) as db:
            tracks_scanned, conflicts = _library_canonical_path_conflicts(db)

        plans = []
        for group in conflicts[:max_groups]:
            entries = group.get("entries") or []
            if len(entries) < 2:
                continue

            # Prefer keeper with on-disk presence and strongest playlist usage.
            keeper = max(
                entries,
                key=lambda e: (
                    1 if e.get("exists_on_disk") else 0,
                    int(e.get("playlist_ref_count", 0) or 0),
                    -len(str(e.get("path") or "")),
                    str(e.get("path") or "").lower(),
                ),
            )
            remove_candidates = [
                e for e in entries if str(e.get("content_id")) != str(keeper.get("content_id"))
            ]
            estimated_rethread = sum(
                int(e.get("playlist_ref_count", 0) or 0) for e in remove_candidates
            )

            plans.append({
                "signature": group.get("signature") or {},
                "keeper": keeper,
                "remove_candidates": remove_candidates,
                "estimated_playlist_slots_to_rethread": estimated_rethread,
            })

        return jsonify({
            "ok": True,
            "read_only": True,
            "total_tracks_scanned": tracks_scanned,
            "total_conflict_groups": len(conflicts),
            "planned_groups": len(plans),
            "plans": plans,
        })
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/library/tracks/<track_id>/stream")
def api_library_track_stream(track_id):
    from db_connection import read_db  # noqa: PLC0415
    from config import DJMT_DB as _DB  # noqa: PLC0415

    try:
        with read_db(_DB) as db:
            track = db.get_content(ID=track_id).one_or_none()
            if track is None:
                return jsonify({"error": "Track not found"}), 404
            file_path = str(getattr(track, "FolderPath", "") or "").strip()

        if not file_path:
            return jsonify({"error": "Track file path missing"}), 404
        if not os.path.isfile(file_path):
            return jsonify({"error": "Track file not found"}), 404

        mime, _ = mimetypes.guess_type(file_path)
        return send_file(file_path, mimetype=mime or "audio/mpeg", conditional=True)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/library/playlists", methods=["GET"])
def api_library_playlists():
    from db_connection import read_db  # noqa: PLC0415
    from config import DJMT_DB as _DB  # noqa: PLC0415

    try:
        with read_db(_DB) as db:
            return jsonify(_playlist_tree_payload(db))
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/library/playlists", methods=["POST"])
def api_library_create_playlist():
    from db_connection import write_db  # noqa: PLC0415
    from config import DJMT_DB as _DB  # noqa: PLC0415

    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).strip()
    node_type = str(data.get("type", "playlist")).strip().lower() or "playlist"
    parent_id = str(data.get("parent_id", "")).strip()

    if not name:
        return jsonify({"error": "name required"}), 400
    if node_type not in {"playlist", "folder"}:
        return jsonify({"error": "type must be playlist or folder"}), 400

    try:
        with write_db(_DB) as db:
            parent = None
            if parent_id:
                parent = db.get_playlist(ID=parent_id).one_or_none()
                if parent is None:
                    return jsonify({"error": "parent playlist not found"}), 404

            if node_type == "folder":
                playlist = db.create_playlist_folder(name, parent=parent)
            else:
                playlist = db.create_playlist(name, parent=parent)
            db.commit()
            return jsonify({
                "ok": True,
                "id": str(playlist.ID),
                "name": playlist.Name or name,
                "type": node_type,
            }), 201
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 503
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/library/playlists/<playlist_id>/tracks")
def api_library_playlist_tracks(playlist_id):
    from db_connection import read_db  # noqa: PLC0415
    from config import DJMT_DB as _DB  # noqa: PLC0415

    try:
        with read_db(_DB) as db:
            playlist = db.get_playlist(ID=playlist_id).one_or_none()
            if playlist is None:
                return jsonify({"error": "Playlist not found"}), 404
            if int(getattr(playlist, "Attribute", 0) or 0) == 1:
                return jsonify([])

            songs = db.get_playlist_songs(PlaylistID=playlist.ID).order_by("TrackNo").all()
            tracks = []
            for song in songs:
                track = song.Content
                if track is None:
                    continue
                tracks.append(_library_track_payload(track, track_no=getattr(song, "TrackNo", None)))
            return jsonify(tracks)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/library/playlists/<playlist_id>/tracks", methods=["POST"])
def api_library_add_tracks_to_playlist(playlist_id):
    from db_connection import write_db  # noqa: PLC0415
    from config import DJMT_DB as _DB  # noqa: PLC0415

    data = request.get_json(silent=True) or {}
    track_ids = data.get("track_ids")
    if not isinstance(track_ids, list):
        single_track_id = str(data.get("track_id", "")).strip()
        track_ids = [single_track_id] if single_track_id else []

    track_ids = [str(track_id).strip() for track_id in track_ids if str(track_id).strip()]
    if not track_ids:
        return jsonify({"error": "track_ids required"}), 400

    try:
        with write_db(_DB) as db:
            playlist = db.get_playlist(ID=playlist_id).one_or_none()
            if playlist is None:
                return jsonify({"error": "Playlist not found"}), 404
            if int(getattr(playlist, "Attribute", 0) or 0) == 1:
                return jsonify({"error": "Cannot add tracks to a folder"}), 400

            existing_ids = set()
            existing_signatures = set()
            for song in db.get_playlist_songs(PlaylistID=playlist.ID).all():
                existing_ids.add(str(getattr(song, "ContentID", "")))
                song_track = getattr(song, "Content", None)
                if song_track is not None:
                    existing_signatures.add(_track_identity_signature(song_track))

            added = 0
            skipped = []
            for track_id in track_ids:
                track = db.get_content(ID=track_id).one_or_none()
                if track is None:
                    skipped.append(track_id)
                    continue

                signature = _track_identity_signature(track)
                # Keep one canonical track reference per playlist entry.
                if str(track.ID) in existing_ids or signature in existing_signatures:
                    skipped.append(track_id)
                    continue
                try:
                    db.add_to_playlist(playlist, track, track_no=None)
                    existing_ids.add(str(track.ID))
                    existing_signatures.add(signature)
                    added += 1
                except Exception:
                    skipped.append(track_id)
            db.commit()
            return jsonify({"ok": True, "added": added, "skipped": skipped}), 201
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 503
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/library/playlists/<playlist_id>", methods=["PUT"])
def api_library_rename_playlist(playlist_id):
    from db_connection import write_db  # noqa: PLC0415
    from config import DJMT_DB as _DB  # noqa: PLC0415

    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).strip()
    if not name:
        return jsonify({"error": "name required"}), 400

    try:
        with write_db(_DB) as db:
            playlist = db.get_playlist(ID=playlist_id).one_or_none()
            if playlist is None:
                return jsonify({"error": "Playlist not found"}), 404
            db.rename_playlist(playlist, name)
            db.commit()
            return jsonify({"ok": True, "id": str(playlist.ID), "name": name})
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 503
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/library/playlists/<playlist_id>", methods=["DELETE"])
def api_library_delete_playlist(playlist_id):
    from db_connection import write_db  # noqa: PLC0415
    from config import DJMT_DB as _DB  # noqa: PLC0415

    try:
        with write_db(_DB) as db:
            playlist = db.get_playlist(ID=playlist_id).one_or_none()
            if playlist is None:
                return jsonify({"error": "Playlist not found"}), 404
            db.delete_playlist(playlist)
            db.commit()
            return jsonify({"ok": True, "id": str(playlist_id), "status": "deleted"})
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 503
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/library/playlists/<playlist_id>/tracks/<track_id>", methods=["DELETE"])
def api_library_remove_track_from_playlist(playlist_id, track_id):
    from db_connection import write_db  # noqa: PLC0415
    from config import DJMT_DB as _DB  # noqa: PLC0415

    try:
        with write_db(_DB) as db:
            songs = db.get_playlist_songs(PlaylistID=playlist_id, ContentID=track_id).all()
            if not songs:
                return jsonify({"error": "Track not in playlist"}), 404
            removed = 0
            for song in songs:
                db.remove_from_playlist(playlist_id, song.ID)
                removed += 1
            db.commit()
            return jsonify({"ok": True, "status": "removed", "removed": removed})
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 503
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/library/playlists/<playlist_id>/tracks", methods=["DELETE"])
def api_library_remove_tracks_from_playlist(playlist_id):
    from db_connection import write_db  # noqa: PLC0415
    from config import DJMT_DB as _DB  # noqa: PLC0415

    data = request.get_json(silent=True) or {}
    track_ids = data.get("track_ids")
    if not isinstance(track_ids, list):
        return jsonify({"error": "track_ids required"}), 400

    track_ids = [str(track_id).strip() for track_id in track_ids if str(track_id).strip()]
    if not track_ids:
        return jsonify({"error": "track_ids required"}), 400

    try:
        with write_db(_DB) as db:
            playlist = db.get_playlist(ID=playlist_id).one_or_none()
            if playlist is None:
                return jsonify({"error": "Playlist not found"}), 404
            if int(getattr(playlist, "Attribute", 0) or 0) == 1:
                return jsonify({"error": "Cannot remove tracks from a folder"}), 400

            removed = 0
            missing = []
            for track_id in track_ids:
                songs = db.get_playlist_songs(PlaylistID=playlist.ID, ContentID=track_id).all()
                if not songs:
                    missing.append(track_id)
                    continue
                for song in songs:
                    db.remove_from_playlist(playlist.ID, song.ID)
                    removed += 1

            db.commit()
            return jsonify({"ok": True, "removed": removed, "missing": missing})
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 503
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/library/tracks/<track_id>", methods=["PATCH"])
def api_library_patch_track(track_id):
    from db_connection import write_db  # noqa: PLC0415
    from config import DJMT_DB as _DB  # noqa: PLC0415

    data = request.get_json(silent=True) or {}
    if "title" not in data:
        return jsonify({"error": "title field required"}), 400

    new_title = str(data.get("title", "")).strip()
    if not new_title:
        return jsonify({"error": "title cannot be empty"}), 400

    try:
        with write_db(_DB) as db:
            track = db.get_content(ID=track_id).one_or_none()
            if track is None:
                return jsonify({"error": "Track not found"}), 404
            track.Title = new_title
            db.commit()
            return jsonify({"ok": True, "track": _library_track_payload(track)})
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 503
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/library/export/drives")
def api_library_export_drives():
    try:
        import psutil  # noqa: PLC0415
        drives = []
        for part in psutil.disk_partitions():
            mountpoint = part.mountpoint
            if not mountpoint.startswith("/Volumes"):
                continue
            try:
                usage = psutil.disk_usage(mountpoint)
                pioneer_db = Path(mountpoint) / "PIONEER" / "Master" / "master.db"
                drives.append({
                    "path": mountpoint,
                    "name": Path(mountpoint).name,
                    "free_bytes": usage.free,
                    "total_bytes": usage.total,
                    "pioneer": pioneer_db.exists(),
                })
            except (PermissionError, OSError):
                continue
        return jsonify(drives)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/library/export", methods=["POST"])
def api_library_export_start():
    data = request.get_json(silent=True) or {}
    playlist_ids = data.get("playlist_ids") or []
    drive_path = str(data.get("drive_path") or "").strip()

    if not playlist_ids:
        return jsonify({"error": "playlist_ids required"}), 400
    if not drive_path:
        return jsonify({"error": "drive_path required"}), 400

    usb_db = Path(drive_path) / "PIONEER" / "Master" / "master.db"
    if not usb_db.exists():
        return jsonify({"error": f"No PIONEER/Master/master.db on {drive_path}"}), 400

    job_id = str(uuid.uuid4())
    with _EXPORT_LOCK:
        _evict_old_jobs(_EXPORT_JOBS, _MAX_EXPORT_JOBS)
        _EXPORT_JOBS[job_id] = {
            "job_id": job_id,
            "status": "running",
            "tracks_total": 0,
            "tracks_done": 0,
            "current_track": "",
            "errors": [],
        }

    threading.Thread(
        target=_run_export,
        args=(job_id, [str(pid) for pid in playlist_ids], drive_path),
        daemon=True,
        name=f"library-export-{job_id[:8]}",
    ).start()

    return jsonify({"job_id": job_id}), 202


@app.route("/api/library/export/<job_id>")
def api_library_export_status(job_id):
    with _EXPORT_LOCK:
        job = _EXPORT_JOBS.get(job_id)
    if job is None:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(job)

# List all playlists
@app.route("/api/playlists")
def api_playlists():
    from db_connection import read_db
    from config import DJMT_DB as _DB
    try:
        with read_db(_DB) as db:
            rows = db.get_playlist().all()
            result = []
            for pl in rows:
                if getattr(pl, "Attribute", 0) != 0:
                    continue
                songs = db.get_playlist_songs(PlaylistID=pl.ID).all()
                result.append({
                    "id": str(pl.ID),
                    "name": pl.Name or "",
                    "track_count": len(songs),
                })
            result.sort(key=lambda p: p["name"].lower())
            return jsonify(result)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

# Get playlist details and tracks
@app.route("/api/playlists/<playlist_id>")
def api_playlist(playlist_id):
    from db_connection import read_db
    from config import DJMT_DB as _DB
    try:
        with read_db(_DB) as db:
            pl = db.get_playlist(ID=playlist_id).one_or_none()
            if pl is None:
                return jsonify({"error": "Playlist not found"}), 404
            songs = db.get_playlist_songs(PlaylistID=pl.ID).order_by("TrackNo").all()
            tracks = []
            for song in songs:
                t = song.Content
                if t is None:
                    continue
                tracks.append({
                    "id": str(t.ID),
                    "title": t.Title or "",
                    "artist": t.Artist.Name if t.Artist else "",
                    "bpm": round(t.BPM / 100, 1) if t.BPM else None,
                    "key": t.Key.Name if t.Key else None,
                    "file_path": t.FolderPath or "",
                })
            return jsonify({
                "id": str(pl.ID),
                "name": pl.Name or "",
                "track_count": len(tracks),
                "tracks": tracks,
            })
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

# List all tracks
@app.route("/api/tracks")
def api_tracks():
    from db_connection import read_db
    from config import DJMT_DB as _DB
    try:
        with read_db(_DB) as db:
            rows = db.get_content().all()
            result = []
            for t in rows:
                result.append({
                    "id": str(t.ID),
                    "title": t.Title or "",
                    "artist": t.Artist.Name if t.Artist else "",
                    "bpm": round(t.BPM / 100, 1) if t.BPM else None,
                    "key": t.Key.Name if t.Key else None,
                    "file_path": t.FolderPath or "",
                })
            return jsonify(result)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

# Get track details
@app.route("/api/tracks/<track_id>")
def api_track(track_id):
    from db_connection import read_db
    from config import DJMT_DB as _DB
    try:
        with read_db(_DB) as db:
            t = db.get_content(ID=track_id).one_or_none()
            if t is None:
                return jsonify({"error": "Track not found"}), 404
            return jsonify({
                "id": str(t.ID),
                "title": t.Title or "",
                "artist": t.Artist.Name if t.Artist else "",
                "bpm": round(t.BPM / 100, 1) if t.BPM else None,
                "key": t.Key.Name if t.Key else None,
                "file_path": t.FolderPath or "",
            })
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

# Serve audio file for playback
@app.route("/audio/<path:audio_path>")
def serve_audio(audio_path):
    from config import MUSIC_ROOT
    abs_path = os.path.join(str(MUSIC_ROOT), audio_path)
    if not os.path.isfile(abs_path):
        return jsonify({"error": "File not found"}), 404
    mime, _ = mimetypes.guess_type(abs_path)
    return send_file(abs_path, mimetype=mime or "audio/mpeg")
try:
    from rekki.recall import recall_memory, create_memory, format_recalled_memory
    from rekki.db import get_memory_db
    from rekki.review import run_tribunal
    _REKKI_MEMORY_ENABLED = True
except Exception as _rekki_import_err:  # pragma: no cover
    _REKKI_MEMORY_ENABLED = False
    run_tribunal = None  # type: ignore[assignment]
    print(f"[rekki] memory disabled — import failed: {_rekki_import_err}")

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
# CONCURRENCY FIX: Changed from single global to dict + lock to prevent race
# conditions when multiple SSE streams run concurrently.
_proc_lock: threading.Lock = threading.Lock()
_active_procs: dict[str, subprocess.Popen] = {}

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


def _current_rekitbox_mode() -> str:
    try:
        from config import REKITBOX_MODE  # noqa: PLC0415
        return str(REKITBOX_MODE).strip() or "rural"
    except Exception:
        return "rural"


def _rekki_enabled() -> bool:
    return _current_rekitbox_mode() == "suburban"


_playback_lock = threading.Lock()
_playback_thread = None
_playback_stop_event = threading.Event()
_playback_current_path = None


def _stop_playback():
    global _playback_thread, _playback_current_path
    with _playback_lock:
        if _playback_thread and _playback_thread.is_alive():
            _playback_stop_event.set()
            _playback_thread.join(timeout=2)
        _playback_thread = None
        _playback_current_path = None
        _playback_stop_event.clear()


def _play_audio_file(path):
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


def _release_info() -> dict:
    """Return a short git-based release blurb for the UI status row."""
    env_version = os.environ.get("REKITBOX_VERSION", "").strip()
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


@app.route("/api/rekki/chat", methods=["POST"])
def api_rekki_chat():
    if not _rekki_enabled():
        return jsonify({"ok": False, "error": "Rekki is disabled in Rural mode."}), 403

    data = request.get_json(silent=True) or {}
    user_message = str(data.get("message", "")).strip()
    source = str(data.get("source", "main")).strip() or "main"

    if not user_message:
        return jsonify({"ok": False, "error": "message is required"}), 400

    context = _rekki_context_snapshot()

    try:
        reply = _rekki_scripted_reply(user_message, source, context)

        # ── HologrA.I.m: persist chat + create memory after every response ───
        if _REKKI_MEMORY_ENABLED:
            try:
                db = get_memory_db()
                db.insert_chat_message(role="user", content=user_message, source=source)
                db.insert_chat_message(role="assistant", content=reply, source=source)
                create_memory(
                    core_insight=reply[:200],
                    confidence_score=0.7,
                    tags=["chat"],
                    congress_engaged=False,
                )
            except Exception as _persist_err:
                print(f"[rekki] memory persistence failed: {_persist_err}")

        return jsonify({
            "ok": True,
            "name": "Rekki",
            "provider": "scripted-local",
            "model": _REKKI_SCRIPTED_MODEL,
            "requested_model": _REKKI_SCRIPTED_MODEL,
            "reply": reply,
            "context": context,
            "memory_enabled": _REKKI_MEMORY_ENABLED,
            "external_calls_blocked": True,
        })
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502


@app.route("/api/rekki/infer-context", methods=["POST"])
def api_rekki_infer_context():
    data = request.get_json(silent=True) or {}
    scrape = data.get("scrape") or {}

    element_text = str(scrape.get("elementText", "")).strip()[:500]
    parent_chain = scrape.get("parentChain", [])
    siblings = scrape.get("siblings", [])
    section = str(scrape.get("sectionHeading", "")).strip()[:120]
    tool_panel = str(scrape.get("toolPanel", "")).strip()[:80]
    existing_attrs = scrape.get("existingAttributes", {})
    page_state = scrape.get("pageState", {})

    _ = (element_text, parent_chain, siblings, section, tool_panel, existing_attrs, page_state)

    try:
        inferred = _rekki_infer_context_local(scrape)
        return jsonify({"ok": True, "context": inferred})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502


@app.route("/api/rekki/automation", methods=["POST"])
def api_rekki_automation():
    data = request.get_json(silent=True) or {}
    action = str(data.get("action", "")).strip().lower()
    model = _REKKI_SCRIPTED_MODEL
    profile = str(data.get("profile") or os.environ.get("REKIT_AGENT_PROFILE", _REKKI_PROFILE)).strip()

    ok, output, code = _rekki_automation_action(action, model, profile)
    status_ok, status_text, status_code = _rekki_automation_status(model, profile)
    http_code = 200 if ok else (code if code in {400, 404} else 502)
    return jsonify({
        "ok": ok,
        "action": action,
        "provider": "scripted-local",
        "requested_model": model,
        "model": model,
        "profile": profile,
        "output": output,
        "code": code,
        "status_ok": status_ok,
        "status_text": status_text,
        "status_code": status_code,
        "external_calls_blocked": True,
    }), http_code


@app.route("/api/rekki/congress/review", methods=["POST"])
def api_rekki_congress_review():
    """Background Congress review — called fire-and-forget by the JS after every tool run.
    Validates the payload and starts run_tribunal() in a daemon thread.
    Always returns {ok: true} immediately so the client never waits.
    """
    if not _REKKI_MEMORY_ENABLED or run_tribunal is None:
        return jsonify({"ok": True, "skipped": "memory disabled"})

    data = request.get_json(silent=True) or {}
    tool_name  = str(data.get("tool") or "unknown").strip()[:80]
    exit_code  = int(data.get("exit_code") if data.get("exit_code") is not None else 0)
    log_lines  = [str(l)[:300] for l in (data.get("log_lines") or []) if str(l).strip()]
    report_text = str(data.get("report") or "")[:2000]

    import threading
    t = threading.Thread(
        target=run_tribunal,
        args=(tool_name, exit_code, log_lines, report_text),
        daemon=True,
    )
    t.start()
    return jsonify({"ok": True})


def _stream(
    cmd: list[str],
    library_root: str = "",
    step_name: str = "",
    prelude_lines: list[str] | None = None,
    cleanup_paths: list[Path] | None = None,
):
    """
    Generator that yields SSE-formatted lines from a subprocess.
    Each event is a JSON object:
      {"line": "..."}          — a line of output
      {"done": true, "exit_code": N}  — command finished

    Registers the process in _active_procs dict with a unique request ID
    so /api/cancel endpoints can send signals to it mid-run. Uses thread-safe
    dictionary to support concurrent SSE streams without race conditions.
    """
    request_id = str(uuid.uuid4())
    _library_root = library_root
    _step_name    = step_name
    try:
        for line in prelude_lines or []:
            yield f"data: {json.dumps({'line': line})}\n\n"

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
            if _step_name and _library_root:
                mark_step_complete(_library_root, _step_name, process.returncode)
            yield f"data: {json.dumps({'done': True, 'exit_code': process.returncode})}\n\n"
        finally:
            with _proc_lock:
                _active_procs.pop(request_id, None)
    except Exception as exc:
        with _proc_lock:
            _active_procs.pop(request_id, None)
        yield f"data: {json.dumps({'line': f'[SERVER ERROR] {exc}', 'done': True, 'exit_code': 1})}\n\n"
    finally:
        for path in cleanup_paths or []:
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                app.logger.warning("SSE cleanup failed for %s: %s", path, exc)
                # Attempt to move to a quarantine location instead of leaving in place
                try:
                    from config import REPORTS_DIR  # noqa: PLC0415
                    quarantine_dir = REPORTS_DIR.parent / "quarantine"
                    quarantine_dir.mkdir(exist_ok=True)
                    dest = quarantine_dir / f"cleanup_failed_{path.name}"
                    path.rename(dest)
                    app.logger.info("Moved uncleanable temp file to quarantine: %s", dest)
                except Exception:
                    pass  # Give up gracefully


def _sse_response(
    cmd: list[str],
    library_root: str = "",
    step_name: str = "",
    prelude_lines: list[str] | None = None,
    cleanup_paths: list[Path] | None = None,
) -> Response:
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
        # If tags cannot be read, keep file in pending list so normal processing can handle/report it.
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
    """Build process candidate list that excludes tracks already complete for requested tag ops."""
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
    request_id = str(uuid.uuid4())
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
                _active_procs[request_id] = process
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
                    _active_procs.pop(request_id, None)
        except Exception as exc:
            with _proc_lock:
                _active_procs.pop(request_id, None)
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


# ── Splash screen ─────────────────────────────────────────────────────────────
_SPLASH_SENTINEL = Path.home() / ".rekordbox-toolkit" / "splash_played"

_SPLASH_HTML = """\
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>RekitBox</title>
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
    <source src="/static/rekitbox-splash.mp4" type="video/mp4">
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


@app.route("/splash")
def splash():
    if _SPLASH_SENTINEL.exists():
        return redirect("/")
    try:
        _SPLASH_SENTINEL.parent.mkdir(parents=True, exist_ok=True)
        _SPLASH_SENTINEL.touch()
    except OSError:
        pass
    return render_template_string(_SPLASH_HTML)


@app.route("/api/status")
def api_status():
    return jsonify({
        "rb_running": _rb_is_running(),
        "backup": _backup_info(),
        "release": _release_info(),
    })


@app.route("/api/export/rekordbox", methods=["POST"])
def api_export_rekordbox():
    """Export playlists and tracks to a Rekordbox One Library structure."""
    data = request.get_json(silent=True) or {}
    target = data.get("target", "").strip()
    if not target or not os.path.isdir(target):
        return jsonify({"error": "Valid target folder required"}), 400

    try:
        import shutil
        import xml.etree.ElementTree as ET

        from config import DJMT_DB  # noqa: PLC0415
        from pyrekordbox import Rekordbox6Database  # noqa: PLC0415

        db_src = str(DJMT_DB)
        db_dst = os.path.join(target, "PIONEER", "Master", "master.db")
        xml_path = os.path.join(target, "PIONEER", "playlists.xml")
        contents_dir = os.path.join(target, "PIONEER", "Contents")

        os.makedirs(os.path.dirname(db_dst), exist_ok=True)
        os.makedirs(contents_dir, exist_ok=True)
        shutil.copy2(db_src, db_dst)

        root = ET.Element("DJ_PLAYLISTS")
        file_paths = set()
        with Rekordbox6Database(db_src) as db:
            playlists = db.get_playlist().all()
            for pl in playlists:
                pl_el = ET.SubElement(root, "PLAYLIST", Name=pl.Name or "", Id=str(pl.ID))
                songs = db.get_playlist_songs(PlaylistID=pl.ID).order_by("TrackNo").all()
                for song in songs:
                    track = song.Content
                    if track is None:
                        continue
                    file_path = track.FolderPath or ""
                    if file_path and os.path.isfile(file_path):
                        file_paths.add(file_path)
                    ET.SubElement(
                        pl_el,
                        "TRACK",
                        Id=str(track.ID),
                        Title=track.Title or "",
                        FilePath=file_path,
                    )

        tree = ET.ElementTree(root)
        tree.write(xml_path, encoding="utf-8", xml_declaration=True)

        for file_path in sorted(file_paths):
            dest = os.path.join(contents_dir, os.path.basename(file_path))
            if not os.path.exists(dest):
                shutil.copy2(file_path, dest)

        return jsonify({"ok": True, "db": db_dst, "xml": xml_path, "contents": contents_dir})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/playback/start", methods=["POST"])
def api_playback_start():
    global _playback_thread, _playback_current_path
    if not _PLAYBACK_AVAILABLE:
        detail = _PLAYBACK_IMPORT_ERROR or "audio playback backend is unavailable"
        return jsonify({"error": f"Playback unavailable: {detail}"}), 503

    data = request.get_json(silent=True) or {}
    file_path = data.get("file_path", "").strip()
    if not file_path or not os.path.isfile(file_path):
        return jsonify({"error": "File not found"}), 404

    _stop_playback()

    def _thread():
        _play_audio_file(file_path)

    with _playback_lock:
        _playback_stop_event.clear()
        _playback_thread = threading.Thread(target=_thread, daemon=True)
        _playback_thread.start()
        _playback_current_path = file_path
    return jsonify({"status": "playing", "file_path": file_path})


@app.route("/api/playback/stop", methods=["POST"])
def api_playback_stop():
    _stop_playback()
    return jsonify({"status": "stopped"})


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
        current_mode = _current_rekitbox_mode()
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
            "rekki_model":      os.environ.get("REKIT_AGENT_MODEL", "") if current_mode == "suburban" else "",
            "configured":       True,
        })
    except Exception:
        current_mode = _current_rekitbox_mode()
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
            "rekki_model":     os.environ.get("REKIT_AGENT_MODEL", "") if current_mode == "suburban" else "",
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

    # Smart skip mode: pre-filter tracks that already have requested tags so
    # heavy processing is focused only on incomplete files.
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
        # In --paths-file mode the positional path is a placeholder only.
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


@app.route("/api/run/process-retry", methods=["POST"])
def api_process_retry():
    """
    Re-run Tag Tracks with --force on a specific list of file paths only.
    Body: {"paths": ["/abs/path/to/file.mp3", ...], "no_bpm": bool, "no_key": bool}
    Uses a temp file so the CLI path-list can be arbitrarily long.
    """
    import tempfile
    body = request.get_json(force=True, silent=True) or {}
    paths = [p.strip() for p in (body.get("paths") or []) if p.strip()]
    if not paths:
        return jsonify({"error": "paths list is required"}), 400

    # Write paths to a temp file; CLI reads it with --paths-file
    tf = tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", prefix="rekitbox_retry_",
        delete=False, encoding="utf-8",
    )
    tf.write("\n".join(paths))
    tf.close()

    # PATH positional arg is required by argparse but unused in --paths-file mode;
    # pass the directory of the first file as a harmless placeholder.
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

    # Pre-flight: refuse if Rekordbox is open and any step writes to the DB.
    _WRITE_STEP_TYPES = {"import", "link", "relocate", "prune"}
    if not dry_run and any(s.get("type") in _WRITE_STEP_TYPES for s in raw_steps):
        err = _require_rb_closed()
        if err:
            return err

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


@app.route("/api/run/rename")
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


@app.route("/api/rename/probe")
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


@app.route("/api/rename/learn", methods=["POST"])
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
        if library_root:
            from renamer import quarantine_track  # noqa: PLC0415
            moved = quarantine_track(Path(source_path), Path(library_root))
        else:
            moved = None
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


@app.route("/api/rename/preflight/apply", methods=["POST"])
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


@app.route("/api/run/duplicates")
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
        csv_mtime = csv_path.stat().st_mtime
        cached = _report_cache.get(cache_key)
        if cached is None or cached.get("_mtime") != csv_mtime:
            # Cache is cold or the CSV file has been updated — (re)load it.
            from pruner import load_report          # noqa: PLC0415
            groups = None
            db_warning = None

            try:
                from db_connection import read_db       # noqa: PLC0415
                from config import DJMT_DB as _DB      # noqa: PLC0415

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
                "_mtime":           csv_mtime,
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
                "db_warning":       db_warning,
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
            "db_warning":       cached.get("db_warning"),
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
            # HIGH-07 FIX: Close file descriptors to prevent leaks
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
            # HIGH-07 FIX: Close file descriptors to prevent leaks
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
    staged = _prune_token_store.pop(token, {})
    _PRUNE_TOKEN_TTL = 1800  # 30 minutes
    if staged and (time.time() - staged.get("_issued_at", 0)) > _PRUNE_TOKEN_TTL:
        staged = {}           # treat expired token same as unknown
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
        if "mode" in data and data["mode"] in ("rural", "suburban"):
            cfg["mode"] = data["mode"]
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
    """Send SIGTERM to all active subprocesses (graceful interrupt / checkpoint)."""
    count = 0
    with _proc_lock:
        for proc in list(_active_procs.values()):
            try:
                if proc.poll() is None:  # Still running
                    proc.terminate()
                    count += 1
            except Exception as exc:
                app.logger.warning("Failed to terminate process: %s", exc)
        _active_procs.clear()
    
    if count > 0:
        return jsonify({"ok": True, "terminated": count})
    return jsonify({"ok": False, "error": "No active scan"}), 404


@app.route("/api/cancel/force", methods=["POST"])
def api_cancel_force():
    """Send SIGKILL to all active subprocesses (emergency stop — server stays running)."""
    count = 0
    with _proc_lock:
        for proc in list(_active_procs.values()):
            try:
                if proc.poll() is None:  # Still running
                    proc.kill()
                    count += 1
            except Exception as exc:
                app.logger.warning("Failed to kill process: %s", exc)
        _active_procs.clear()
    
    if count > 0:
        return jsonify({"ok": True, "killed": count})
    return jsonify({"ok": False, "error": "No active scan"}), 404


# ── Normalize preview ─────────────────────────────────────────────────────────
# Scans a folder for loudest/quietest tracks, extracts 10-second clips at the
# 50 % mark, normalises copies to -8 LUFS, and stores them in a temp dir so
# the browser can play them via /api/normalize/preview/clip/<id>.

import random as _random
import re as _re

_PREVIEW_TMP  = Path.home() / ".rekordbox-toolkit" / "previews"
_PREVIEW_TMP.mkdir(parents=True, exist_ok=True)

_PREVIEW_JOBS: dict[str, dict] = {}
_PREVIEW_LOCK  = threading.Lock()

_PREVIEW_AUDIO_EXTS = {'.aiff', '.aif', '.aifc', '.wav', '.flac', '.mp3', '.m4a', '.m4p', '.mp4', '.m4v', '.alac', '.ogg', '.opus'}
_PREVIEW_MIN_DUR    = 120    # track must be ≥ 2 min
_PREVIEW_MAX_SCAN   = 40     # cap random sample for large folders
_PREVIEW_WINDOW     = 20     # seconds of audio measured for LUFS


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
            return val if val > -70 else None   # treat near-silence as invalid
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

        all_audio = [f for f in sorted(folder.iterdir())
                     if f.suffix.lower() in _PREVIEW_AUDIO_EXTS
                     and not f.name.startswith('.')]

        # Duration filter — only tracks ≥ 2 min
        qualified: list[tuple[Path, float]] = []
        for f in all_audio:
            d = _preview_duration(f)
            if d and d >= _PREVIEW_MIN_DUR:
                qualified.append((f, d))

        if len(qualified) < 2:
            _preview_set(job_id, status="error",
                         msg=f"Need at least 2 tracks ≥ 2 min (found {len(qualified)}).")
            return

        # Random-sample large folders
        sample = (qualified if len(qualified) <= _PREVIEW_MAX_SCAN
                  else _random.sample(qualified, _PREVIEW_MAX_SCAN))

        _preview_set(job_id, status="measuring",
                     msg=f"Measuring loudness of {len(sample)} tracks…",
                     total=len(sample))

        measured: list[tuple[Path, float, float]] = []   # path, duration, lufs
        for i, (f, dur) in enumerate(sample):
            start = max(0, dur / 2 - _PREVIEW_WINDOW / 2)
            lufs  = _preview_lufs(f, start)
            if lufs is not None:
                measured.append((f, dur, lufs))
            _preview_set(job_id, progress=i + 1)

        if len(measured) < 2:
            _preview_set(job_id, status="error",
                         msg="Could not measure loudness for enough tracks.")
            return

        measured.sort(key=lambda x: x[2])
        quietest = measured[0]
        loudest  = measured[-1]

        _preview_set(job_id, status="extracting", msg="Extracting preview clips…")

        clips = []
        for tag, (f, dur, lufs) in [("q", quietest), ("l", loudest)]:
            clip_start = max(0, dur / 2 - 5)   # centre 10 s clip on midpoint

            orig_id   = f"{job_id}_{tag}_orig"
            norm_id   = f"{job_id}_{tag}_norm"
            orig_path = _PREVIEW_TMP / f"{orig_id}.mp3"
            norm_path = _PREVIEW_TMP / f"{norm_id}.mp3"

            ok_orig = _preview_extract(f, clip_start, orig_path)
            ok_norm = ok_orig and _preview_normalize(orig_path, norm_path)

            clips.append({
                "clip_id":  orig_id if ok_orig else None,
                "track":    f.name,
                "lufs":     round(lufs, 1),
                "label":    "Original",
                "kind":     "quietest" if tag == "q" else "loudest",
            })
            clips.append({
                "clip_id":  norm_id if ok_norm else None,
                "track":    f.name,
                "lufs":     -8.0,
                "label":    "Normalized  −8 LUFS",
                "kind":     "quietest" if tag == "q" else "loudest",
            })

        _preview_set(job_id, status="done", msg="", clips=clips)

    except Exception as exc:
        _preview_set(job_id, status="error", msg=str(exc))


@app.route("/api/normalize/preview", methods=["POST"])
def api_normalize_preview():
    data   = request.get_json(silent=True) or {}
    path   = data.get("path") or request.form.get("path", "")
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


@app.route("/api/normalize/preview/<job_id>")
def api_normalize_preview_status(job_id):
    if not _re.match(r'^[0-9a-f]{8}$', job_id):
        return jsonify({"error": "invalid"}), 400
    with _PREVIEW_LOCK:
        job = _PREVIEW_JOBS.get(job_id)
    if job is None:
        return jsonify({"error": "not found"}), 404
    return jsonify(job)


@app.route("/api/normalize/preview/clip/<clip_id>")
def api_normalize_preview_clip(clip_id):
    if not _re.match(r'^[0-9a-f]{8}_[ql]_(orig|norm)$', clip_id):
        return jsonify({"error": "invalid"}), 400
    clip_path = _PREVIEW_TMP / f"{clip_id}.mp3"
    if not clip_path.exists():
        return jsonify({"error": "not found"}), 404
    return send_file(str(clip_path), mimetype="audio/mpeg",
                     conditional=True)


# ── RekitBox update route ─────────────────────────────────────────────────────

@app.route("/api/update/status")
def api_update_status():
    """Return the cached GitHub release check result (never blocks)."""
    return jsonify(_update_get_status())


@app.route("/api/update/apply", methods=["POST"])
def api_update_apply():
    """
    Pull the latest release in-place, then relaunch RekitBox.

    Flow:
      1. Refuse if a scan/subprocess is running or Rekordbox is open.
      2. Run ``git pull --ff-only`` in the repo root.
      3. On success, spawn a detached helper that waits for the port to free,
         then re-runs launch.sh. Finally SIGTERM self so the helper can bind.
      4. Frontend polls /api/update/status until it responds, then reloads.
    """
    # Refuse mid-scan — interrupt would leave the DB in an ambiguous state.
    with _proc_lock:
        # Check if any SSE streams are active
        active = any(proc.poll() is None for proc in _active_procs.values())
    if active:
        return jsonify({
            "ok": False,
            "error": "A scan is still running — cancel or finish it before updating.",
        }), 409

    # Only git installs can pull in place.
    launch_sh = _REPO_ROOT / "launch.sh"
    if not (_REPO_ROOT / ".git").exists() or not launch_sh.exists():
        return jsonify({
            "ok": False,
            "error": "Not a git install — download the new release manually.",
        }), 400

    # Do the pull.
    try:
        # Refuse if the working tree has local modifications — pull would fail or clobber changes.
        status_check = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(_REPO_ROOT),
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
            cwd=str(_REPO_ROOT),
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

    # Schedule the relaunch: detached helper sleeps 2s (letting this process
    # exit and the port free) then execs launch.sh. start_new_session detaches
    # it from our process group so SIGTERM to us doesn't reach it.
    def _relaunch() -> None:
        import time
        time.sleep(0.7)  # let the JSON response reach the browser
        try:
            subprocess.Popen(
                ["bash", "-c", 'sleep 2 && exec bash "$0"', str(launch_sh)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                close_fds=True,
                cwd=str(_REPO_ROOT),
            )
        finally:
            os.kill(os.getpid(), signal.SIGTERM)

    threading.Thread(target=_relaunch, daemon=True).start()
    return jsonify({"ok": True, "output": pull.stdout.strip()})


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
        app.logger.debug("[finder-selection] rc=%d stdout=%r stderr=%r",
                         r.returncode, r.stdout, r.stderr)
        if r.returncode == 0 and r.stdout.strip():
            return jsonify({"path": r.stdout.strip().rstrip("/")})
    except Exception as exc:
        app.logger.debug("[finder-selection] exception: %s", exc)

    # When called from a drag-drop event, pywebview may focus before osascript
    # runs and Finder clears its selection — return null silently rather than
    # opening a picker dialog the user didn't ask for.
    if source == "drop":
        app.logger.debug("[finder-selection] source=drop, returning null")
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

    Security: intentionally unauthenticated because Waitress binds exclusively
    to 127.0.0.1 — this endpoint is unreachable from the network.  The
    localhost guard below is a defense-in-depth check in case the bind address
    changes in the future.
    """
    if request.remote_addr not in ("127.0.0.1", "::1"):
        return jsonify({"error": "Forbidden"}), 403
    AUDIO_EXTS = {'.aiff', '.aif', '.aifc', '.wav', '.flac', '.mp3', '.m4a', '.m4p', '.mp4', '.m4v', '.alac', '.ogg', '.opus'}
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


@app.route("/api/migrate-pioneer-db", methods=["POST"])
def api_migrate_pioneer_db():
    """Stream progress of migrating ~/Library/Pioneer/rekordbox/ to the target drive."""
    data = request.get_json(silent=True) or {}
    target = str(data.get("target", "")).strip()
    if not target:
        return jsonify({"error": "target is required"}), 400
    from db_migrator import migrate  # noqa: PLC0415
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


_MAX_ANALYSIS_JOBS = 100
_MAX_EXPORT_JOBS   = 50
_MAX_PREVIEW_JOBS  = 100
_MAX_PRUNE_TOKENS  = 200


def _evict_old_jobs(store: dict, max_size: int) -> None:
    """Trim a job dict to *max_size* by removing the oldest entries.
    Must be called while holding the relevant lock (or on stores that are
    only written from a single thread)."""
    if len(store) > max_size:
        excess = len(store) - max_size
        for key in list(store.keys())[:excess]:
            del store[key]



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


def _read_mobile_token() -> str:
    """
    Read (but never generate) the current mobile token from config.

    Called on every authenticated mobile request so that a token set up
    after the server starts is accepted without a restart.
    Falls back to the module-level MOBILE_TOKEN if config can't be read.
    """
    try:
        from user_config import load_user_config, config_exists  # noqa: PLC0415
        if not config_exists():
            return ""
        cfg = load_user_config()
        return cfg.get("mobile_token", "") or ""
    except Exception:
        return MOBILE_TOKEN  # safe fallback — the startup value


@app.before_request
@limiter.limit("10 per minute", exempt_when=lambda: not request.path.startswith('/api/mobile/'))
def _check_mobile_auth():
    """
    Require Bearer token for all /api/mobile/* routes except /api/mobile/ping.
    Desktop routes (/, /api/status, /api/run/*, etc.) are unaffected — they are
    already only reachable on localhost so no auth is needed there.
    
    SECURITY: Rate limited to 10 attempts per minute to prevent brute-force
    attacks on the Bearer token.
    """
    if not request.path.startswith("/api/mobile/"):
        return
    if request.path == "/api/mobile/ping":
        return
    current_token = _read_mobile_token()
    if not current_token:
        return jsonify({
            "error": "server_not_configured",
            "message": "Run: python3 cli.py setup",
        }), 503
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer ") or auth[7:] != current_token:
        app.logger.warning(
            "Mobile API auth failed from %s for %s",
            request.remote_addr,
            request.path,
        )
        return jsonify({"error": "unauthorized"}), 401

# ── Mobile API routes ────────────────────────────────────────────────────────

@app.route("/api/mobile/ping")
def mobile_ping():
    """
    Health check for RekitGo. No auth required.
    Used by the app on startup to confirm network reachability before attempting
    authenticated calls.
    """
    try:
        from update_checker import _local_version  # noqa: PLC0415
        _ver, _ = _local_version()
    except Exception:
        _ver = None
    ver = _ver or "unknown"
    return jsonify({"status": "ok", "version": ver, "rekitbox_version": ver})


@app.route("/api/connectivity")
def api_connectivity():
    """
    Connection info for the RekitGo pairing panel in the RekitBox UI.
    No auth required — this is served to the local desktop page only.

    Returns:
      local_ip      — LAN IP (reachable on same WiFi)
      tailscale_ip  — Tailscale IP if connected, else null
      port          — always 5001
      remote_ready  — true when Tailscale is up
      token         — mobile_token from config (for QR pairing)
      qr_svg        — SVG QR code encoding rekitgo://<best_ip>:5001?token=<token>
    """
    import socket, subprocess as _sp  # noqa: E401

    # Best local IP (non-loopback)
    local_ip: str | None = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
    except Exception:
        local_ip = "127.0.0.1"

    # Tailscale IP — fast check, 2 s timeout
    tailscale_ip: str | None = None
    try:
        ts = _sp.run(["tailscale", "ip", "-4"],
                     capture_output=True, text=True, timeout=2)
        if ts.returncode == 0:
            tailscale_ip = ts.stdout.strip() or None
    except Exception:
        pass

    # Mobile auth token
    token: str | None = None
    try:
        import json as _json, pathlib as _pl  # noqa: E401
        cfg_path = _pl.Path.home() / ".rekordbox-toolkit" / "config.json"
        if cfg_path.exists():
            token = _json.loads(cfg_path.read_text()).get("mobile_token")
    except Exception:
        pass

    best_ip = tailscale_ip or local_ip
    remote_ready = tailscale_ip is not None

    # ── QR generation helper ─────────────────────────────────────────────────
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
            # Strip white background rect (various quote / colour spellings)
            svg = re.sub(r'<rect[^>]+fill=["\']#fff(?:fff)?["\'][^/]*/>', '', svg)
            svg = re.sub(r'<rect[^>]+fill=["\']white["\'][^/]*/>', '', svg)
            # Recolour the data path
            svg = re.sub(r"fill=['\"]#000(?:000)?['\"]", f'fill="{fill}"', svg)
            return svg
        except Exception:
            return None

    # Pairing QR — orange (primary action)
    qr_svg: str | None = None
    if token and best_ip:
        qr_svg = _make_styled_qr(
            f"rekitgo://{best_ip}:5001?token={token}",
            fill="#ff6600",
        )

    # PWA install QR — orange, encodes the web-app URL so iPhone Safari can
    # open it directly and the user can Add to Home Screen
    qr_pwa_url: str | None = None
    if best_ip:
        qr_pwa_url = _make_styled_qr(
            f"http://{best_ip}:5001",
            fill="#ff6600",
        )

    # Setup QRs — green (safe / informational)
    _green = "#34d399"
    qr_tailscale_mac = _make_styled_qr("https://tailscale.com/download/macos", fill=_green)
    qr_tailscale_ios = _make_styled_qr(
        "https://apps.apple.com/app/tailscale/id1470499037", fill=_green
    )
    qr_rekitgo_ios   = _make_styled_qr(
        "https://github.com/fabledharbinger0993/RekitBox", fill=_green
    )

    return jsonify({
        "local_ip":          local_ip,
        "tailscale_ip":      tailscale_ip,
        "port":              5001,
        "remote_ready":      remote_ready,
        "token":             token,
        "qr_svg":            qr_svg,
        "qr_pwa_url":        qr_pwa_url,
        "qr_tailscale_mac":  qr_tailscale_mac,
        "qr_tailscale_ios":  qr_tailscale_ios,
        "qr_rekitgo_ios":    qr_rekitgo_ios,
    })


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
    
    SECURITY: Validates that folder_path stays within allowed music roots
    to prevent path traversal attacks (e.g., ../../etc/passwd).
    """
    import datetime  # noqa: PLC0415
    from config import MUSIC_ROOT  # noqa: PLC0415
    
    p = Path("/" + folder_path) if not folder_path.startswith("/") else Path(folder_path)
    
    # Resolve to absolute path and check for path traversal
    try:
        p_resolved = p.resolve()
    except (OSError, RuntimeError):
        return jsonify({"error": "invalid_path"}), 400
    
    # Validate against MUSIC_ROOT
    music_root_resolved = MUSIC_ROOT.resolve()
    if not str(p_resolved).startswith(str(music_root_resolved)):
        app.logger.warning(
            "Path traversal attempt blocked: %s (outside %s)",
            p_resolved,
            music_root_resolved,
        )
        return jsonify({"error": "forbidden"}), 403

    if not p_resolved.is_dir():
        return jsonify({"error": "folder_not_found"}), 404

    audio_extensions = {
        ".mp3", ".wav", ".aiff", ".aif", ".aifc", ".flac", ".m4a", ".m4p", ".mp4", ".m4v", ".ogg", ".opus",
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


@app.route("/api/mobile/download", methods=["POST"])
def mobile_download():
    """
    Enqueue a download job.

    Body: {
      "url":         "https://bandcamp.com/...",
      "destination": "/Volumes/DJMT/New Drops/",
      "format":      "aiff" | "flac" | "wav" | "mp3"  (optional, default "aiff")
      "filename":    "Artist - Title"                  (optional, derived from tags)
    }
    Response: { "job_id": "uuid" }

    The download runs asynchronously. Progress and completion are pushed to all
    connected WebSocket clients via /api/mobile/events as download_update events.
    Supported sources: Bandcamp, Beatport, Soundcloud, and any URL yt-dlp handles.
    """
    import downloader  # noqa: PLC0415
    body = request.get_json(force=True, silent=True) or {}
    url         = (body.get("url")         or "").strip()
    destination = (body.get("destination") or "").strip()
    filename    = (body.get("filename")    or "").strip() or None
    fmt         = (body.get("format")      or downloader.DEFAULT_FORMAT).strip().lower()

    if not url:
        return jsonify({"error": "url is required"}), 400
    if not destination:
        return jsonify({"error": "destination is required"}), 400
    if fmt not in downloader.FORMATS:
        return jsonify({"error": f"format must be one of: {', '.join(sorted(downloader.FORMATS))}"}), 400

    job_id = downloader.enqueue(url, destination, filename, fmt)
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
    try:
        limit  = int(request.args.get("limit",  200))
        offset = int(request.args.get("offset", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "limit and offset must be integers"}), 400

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

    AUDIO_EXTS = {".mp3", ".wav", ".aiff", ".aif", ".aifc", ".flac", ".m4a", ".m4p", ".mp4", ".m4v", ".ogg", ".opus"}
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

            status = "complete_partial" if db_note else "complete"

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
        job_results = _ANALYSIS_JOBS[job_id].get("results", {})
        had_partial = any(r.get("status") == "complete_partial" for r in job_results.values())
        _ANALYSIS_JOBS[job_id]["status"] = "complete_partial" if had_partial else "complete"


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
        _evict_old_jobs(_ANALYSIS_JOBS, _MAX_ANALYSIS_JOBS)
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
    from db_connection import read_db, rekordbox_is_running  # noqa: PLC0415
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

        # ── 1b. Refuse if Rekordbox is running ───────────────────────────────
        if rekordbox_is_running():
            _update({"status": "failed",
                      "errors": ["Rekordbox is running — close it before exporting to USB"]})
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


@app.after_request
def disable_cache_on_static_files(response):
    """Disable caching for static files to ensure fresh assets are always served.
    
    This is critical for icon files and CSS/JS updates to be reflected immediately
    in the embedded webview without requiring a full app restart.
    """
    if request.path.startswith('/static/'):
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
    
    # INFO-01 FIX: Add Content-Security-Policy header for defense-in-depth
    # against XSS attacks. This is a local-only app but CSP is still good practice.
    # 'unsafe-inline' is required for inline scripts in index.html.
    # 'unsafe-eval' is required for dynamic code execution in some JS libraries.
    response.headers['Content-Security-Policy'] = (
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


if __name__ == "__main__":
    print()
    print("  ┌─────────────────────────────────────┐")
    print("  │  RekitBox  ·  rekordbox-toolkit UI  │")
    print("  │  http://localhost:5001              │")
    print("  └─────────────────────────────────────┘")
    print()
    app.run(host="127.0.0.1", port=5001, debug=False, threaded=True)
