"""
routes_player.py — ── The Media Pit ──

Flask Blueprint: library, playlist, tracks, audio serving, and playback.
Handles all read/write operations on the Rekordbox library tree, plus
in-process audio playback for the desktop UI.
"""

import mimetypes
import os
import threading
import uuid
from pathlib import Path

from flask import Blueprint, jsonify, request, send_file

from helpers import (
    REPO_ROOT,
    CLI_PATH,
    _EXPORT_JOBS,
    _EXPORT_LOCK,
    _MAX_EXPORT_JOBS,
    _detect_pioneer_drive_layout,
    _PLAYBACK_AVAILABLE,
    _PLAYBACK_IMPORT_ERROR,
    _evict_old_jobs,
    _play_audio_file,
    _playback,
    _playback_lock,
    _playback_stop_event,
    _run_export,
    _stop_playback,
)

bp = Blueprint("player", __name__)


# ── Library payload helpers ───────────────────────────────────────────────────

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

    comment = str(getattr(track, "Comment", "") or "").strip()
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


# ── Filesystem track helper ───────────────────────────────────────────────────

_FS_AUDIO_EXTS = frozenset({
    ".aiff", ".aif", ".aifc", ".wav", ".flac", ".mp3",
    ".m4a", ".m4p", ".alac", ".ogg", ".opus",
})
_FS_TAG_LIMIT = 500   # stop reading mutagen beyond this many tracks in one folder
_FS_RECURSIVE_LIMIT = 5000  # hard cap for recursive scans


def _fs_track_payload(path: Path) -> dict:
    """Minimal metadata for a filesystem audio file (no rekordbox required).
    Reads ID3/Vorbis/FLAC tags via mutagen (fast for local files).
    Falls back to filename-only on any read error.
    """
    payload: dict = {
        "source":      "filesystem",
        "path":        str(path),
        "filename":    path.name,
        "title":       path.stem,
        "artist":      "",
        "album":       "",
        "genre":       "",
        "bpm":         None,
        "key":         None,
        "duration_s":  None,
    }
    try:
        import mutagen  # noqa: PLC0415
        f = mutagen.File(str(path), easy=True)
        if f:
            payload["title"]  = (f.get("title")  or [path.stem])[0]
            payload["artist"] = (f.get("artist") or [""])[0]
            payload["album"]  = (f.get("album")  or [""])[0]
            payload["genre"]  = (f.get("genre")  or [""])[0]
            # BPM: easy=True maps TBPM→"bpm" for MP3, and vorbis/flac use "bpm" directly
            raw_bpm = (f.get("bpm") or [""])[0]
            if raw_bpm:
                try:
                    payload["bpm"] = str(int(round(float(raw_bpm))))
                except (ValueError, TypeError):
                    payload["bpm"] = raw_bpm
            # Key: easy=True maps TKEY→"initialkey"
            raw_key = (f.get("initialkey") or [""])[0]
            if raw_key:
                payload["key"] = raw_key
            if hasattr(f, "info") and hasattr(f.info, "length"):
                payload["duration_s"] = round(f.info.length)
    except Exception:
        pass
    return payload


# ── Library track routes ──────────────────────────────────────────────────────

def _resolve_db(db_param):
    """Return the DB path for a ?db= query param.  'device' → DJMT_DB, else LOCAL_DB."""
    from config import LOCAL_DB, DJMT_DB  # noqa: PLC0415
    if db_param and str(db_param).lower() in ("device", "djmt"):
        return DJMT_DB
    return LOCAL_DB


@bp.route("/api/library/volumes")
def api_library_volumes():
    """Return all mounted volumes under /Volumes with audio-file estimates."""
    import shutil  # noqa: PLC0415
    volumes_root = Path("/Volumes")
    _AUDIO = {".mp3", ".flac", ".aac", ".wav", ".aiff", ".aif", ".m4a", ".ogg", ".opus", ".wv", ".alac"}

    results = []
    try:
        for vol in sorted(volumes_root.iterdir()):
            if not vol.is_dir() or vol.name.startswith("."):
                continue
            # Fast depth-1 audio estimate (not recursive — keeps it instant)
            audio_estimate = 0
            try:
                for entry in os.scandir(vol):
                    if entry.is_file() and Path(entry.name).suffix.lower() in _AUDIO:
                        audio_estimate += 1
            except PermissionError:
                pass

            # Disk usage
            total_gb = free_gb = None
            try:
                usage = shutil.disk_usage(vol)
                total_gb = round(usage.total / 1e9, 1)
                free_gb = round(usage.free / 1e9, 1)
            except Exception:
                pass

            # Pioneer DB present?
            has_pioneer_db = (vol / "PIONEER" / "rekordbox" / "master.db").exists()

            results.append({
                "name":          vol.name,
                "mountpoint":    str(vol),
                "audio_estimate": audio_estimate,
                "total_gb":      total_gb,
                "free_gb":       free_gb,
                "has_pioneer_db": has_pioneer_db,
            })
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    return jsonify(results)


@bp.route("/api/library/tracks")
def api_library_tracks():
    from db_connection import read_db  # noqa: PLC0415
    _DB = _resolve_db(request.args.get("db"))

    try:
        with read_db(_DB) as db:
            tracks = [_library_track_payload(track) for track in db.get_content().all()]
            return jsonify(tracks)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@bp.route("/api/library/fs-browse")
def api_library_fs_browse():
    """Browse a directory for audio files — filesystem-first, no rekordbox needed.
    Returns subdirectories + audio tracks in the requested folder.
    Falls back to /Volumes when no path is given.

    Query params:
      path      – directory to browse (default: /Volumes → volume picker)
      recursive – if "1" or "true", walk all subdirectories and return every
                  audio file at any depth.  Subdirs list is omitted in this mode.
                  Capped at _FS_RECURSIVE_LIMIT tracks; truncated=true when hit.
    """
    from config import MUSIC_ROOT as _MR  # noqa: PLC0415
    import shutil  # noqa: PLC0415

    path_str = request.args.get("path", "")
    recursive = request.args.get("recursive", "0").lower() in ("1", "true", "yes")

    # ── /Volumes sentinel — return volume picker payload ────────────────────
    volumes_root = Path("/Volumes")
    if not path_str or Path(path_str).resolve() == volumes_root:
        _AUDIO = {".mp3", ".flac", ".aac", ".wav", ".aiff", ".aif", ".m4a", ".ogg", ".opus", ".wv", ".alac"}
        volumes = []
        try:
            for vol in sorted(volumes_root.iterdir()):
                if not vol.is_dir() or vol.name.startswith("."):
                    continue
                audio_estimate = 0
                try:
                    for entry in os.scandir(vol):
                        if entry.is_file() and Path(entry.name).suffix.lower() in _AUDIO:
                            audio_estimate += 1
                except PermissionError:
                    pass
                total_gb = free_gb = None
                try:
                    usage = shutil.disk_usage(vol)
                    total_gb = round(usage.total / 1e9, 1)
                    free_gb = round(usage.free / 1e9, 1)
                except Exception:
                    pass
                has_pioneer_db = (vol / "PIONEER" / "rekordbox" / "master.db").exists()
                volumes.append({
                    "name": vol.name,
                    "path": str(vol),
                    "audio_estimate": audio_estimate,
                    "total_gb": total_gb,
                    "free_gb": free_gb,
                    "has_pioneer_db": has_pioneer_db,
                })
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500
        return jsonify({
            "path":           str(volumes_root),
            "is_volumes_root": True,
            "music_root":     str(_MR),
            "parent":         None,
            "volumes":        volumes,
            "subdirs":        [],
            "tracks":         [],
        })

    # ── Normal path browse ───────────────────────────────────────────────────
    try:
        p = Path(path_str).resolve()
    except Exception:
        return jsonify({"error": "Invalid path"}), 400
    if not p.exists() or not p.is_dir():
        return jsonify({"error": f"Not a directory: {path_str}"}), 400

    music_root = str(_MR)
    parent = str(p.parent) if str(p) != str(p.anchor) else None

    if recursive:
        # Walk the whole tree, collecting audio files up to the hard cap.
        tracks: list[Path] = []
        try:
            for item in sorted(p.rglob("*"), key=lambda x: x.name.lower()):
                if item.name.startswith("."):
                    continue
                if item.is_file() and item.suffix.lower() in _FS_AUDIO_EXTS:
                    tracks.append(item)
                    if len(tracks) >= _FS_RECURSIVE_LIMIT:
                        break
        except PermissionError:
            return jsonify({"error": "Permission denied"}), 403

        # Count remaining tracks for the truncation message (cheap: just keep
        # scanning filenames without reading tags).
        total_tracks = len(tracks)
        truncated = total_tracks >= _FS_RECURSIVE_LIMIT
        if truncated:
            try:
                total_tracks = sum(
                    1 for item in p.rglob("*")
                    if not item.name.startswith(".")
                    and item.is_file()
                    and item.suffix.lower() in _FS_AUDIO_EXTS
                )
            except Exception:
                total_tracks = _FS_RECURSIVE_LIMIT  # best-effort

        tag_limit = _FS_TAG_LIMIT if not truncated else min(_FS_TAG_LIMIT, len(tracks))
        track_payloads = [_fs_track_payload(t) for t in tracks[:tag_limit]]
        return jsonify({
            "path":           str(p),
            "music_root":     music_root,
            "in_music_root":  str(p).startswith(music_root),
            "parent":         parent,
            "subdirs":        [],   # omitted in recursive mode — sidebar stays navigable
            "tracks":         track_payloads,
            "track_count":    total_tracks,
            "truncated":      truncated,
            "recursive":      True,
        })

    # ── Non-recursive (default) ───────────────────────────────────────────────
    subdirs = []
    tracks_flat: list[Path] = []
    try:
        items = sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
        for item in items:
            if item.name.startswith("."):
                continue
            if item.is_dir():
                try:
                    audio_count = sum(1 for f in item.iterdir()
                                      if not f.name.startswith(".") and f.suffix.lower() in _FS_AUDIO_EXTS)
                except PermissionError:
                    audio_count = 0
                subdirs.append({"name": item.name, "path": str(item), "audio_count": audio_count})
            elif item.suffix.lower() in _FS_AUDIO_EXTS:
                tracks_flat.append(item)
    except PermissionError:
        return jsonify({"error": "Permission denied"}), 403

    total_tracks = len(tracks_flat)
    truncated = total_tracks > _FS_TAG_LIMIT
    track_payloads = [_fs_track_payload(t) for t in tracks_flat[:_FS_TAG_LIMIT]]

    return jsonify({
        "path":           str(p),
        "music_root":     music_root,
        "in_music_root":  str(p).startswith(music_root),
        "parent":         parent,
        "subdirs":        subdirs,
        "tracks":         track_payloads,
        "track_count":    total_tracks,
        "truncated":      truncated,
        "recursive":      False,
    })


@bp.route("/api/library/split-data")
def api_library_split_data():
    """Three-way library split:
    • in_library  — rekordbox tracks whose path is inside MUSIC_ROOT (canonical)
    • scattered   — rekordbox tracks whose path is outside MUSIC_ROOT, grouped by folder
    • unimported  — filesystem audio files in fs_path not tracked by rekordbox DB

    fs_path query param (optional): directory to scan for unimported files.
    """
    from db_connection import read_db  # noqa: PLC0415
    from config import LOCAL_DB as _DB, MUSIC_ROOT as _MR  # noqa: PLC0415

    music_root = str(_MR)

    # ── Load rekordbox DB ────────────────────────────────────────────────────
    try:
        with read_db(_DB) as db:
            all_db_tracks = [_library_track_payload(t) for t in db.get_content().all()]
    except Exception as exc:
        return jsonify({"error": f"rekordbox DB unavailable: {exc}"}), 500

    # ── Classify by path ─────────────────────────────────────────────────────
    in_library: list = []
    scattered_map: dict = {}
    db_path_set: set = set()

    for track in all_db_tracks:
        fp = (track.get("file_path") or "").strip()
        db_path_set.add(fp)
        if fp.startswith(music_root):
            in_library.append(track)
        else:
            folder = str(Path(fp).parent) if fp else "Unknown location"
            scattered_map.setdefault(folder, []).append(track)

    # Flatten scattered into folder-header + track rows
    scattered: list = []
    for folder, folder_tracks in sorted(scattered_map.items()):
        scattered.append({"type": "folder_header", "path": folder, "count": len(folder_tracks)})
        scattered.extend(folder_tracks)

    # ── Filesystem unimported scan ────────────────────────────────────────────
    unimported: list = []
    fs_path_str = request.args.get("fs_path", "")
    if fs_path_str:
        try:
            fp_dir = Path(fs_path_str).resolve()
            if fp_dir.is_dir():
                for item in sorted(fp_dir.iterdir(), key=lambda x: x.name.lower()):
                    if item.name.startswith(".") or not item.is_file():
                        continue
                    if item.suffix.lower() not in _FS_AUDIO_EXTS:
                        continue
                    if str(item) not in db_path_set:
                        unimported.append({"path": str(item), "filename": item.name, "title": item.stem})
        except Exception:
            pass

    return jsonify({
        "music_root":        music_root,
        "in_library":        in_library,
        "in_library_count":  len(in_library),
        "scattered":         scattered,
        "scattered_count":   sum(1 for r in scattered if r.get("type") != "folder_header"),
        "unimported":        unimported,
        "unimported_count":  len(unimported),
    })


@bp.route("/api/library/integrity/canonical-paths")
def api_library_integrity_canonical_paths():
    """
    Detect likely duplicate logical tracks that point at multiple physical paths.
    This is read-only and intended to support canonical-path cleanup workflows.
    """
    from db_connection import read_db  # noqa: PLC0415
    from config import LOCAL_DB as _DB  # noqa: PLC0415

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


@bp.route("/api/library/integrity/canonical-paths/plan")
def api_library_integrity_canonical_paths_plan():
    """Build a read-only consolidation plan for canonical path cleanup."""
    from db_connection import read_db  # noqa: PLC0415
    from config import LOCAL_DB as _DB  # noqa: PLC0415

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


@bp.route("/api/library/tracks/<track_id>/stream")
def api_library_track_stream(track_id):
    from db_connection import read_db  # noqa: PLC0415
    from config import LOCAL_DB as _DB  # noqa: PLC0415

    try:
        with read_db(_DB) as db:
            track = db.get_content(ID=track_id).one_or_none()
            if track is None:
                return jsonify({"error": f"Track {track_id!r} not found in DB"}), 404
            file_path = str(track.FolderPath or "").strip()

        if not file_path:
            return jsonify({"error": f"Track {track_id!r} has no file path in DB"}), 404
        if not os.path.isfile(file_path):
            return jsonify({"error": f"Audio file not found on disk: {file_path}"}), 404

        mime, _ = mimetypes.guess_type(file_path)
        return send_file(file_path, mimetype=mime or "audio/mpeg", conditional=True)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@bp.route("/api/library/playlists", methods=["GET"])
def api_library_playlists():
    from db_connection import read_db  # noqa: PLC0415
    _DB = _resolve_db(request.args.get("db"))

    try:
        with read_db(_DB) as db:
            return jsonify(_playlist_tree_payload(db))
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@bp.route("/api/library/playlists", methods=["POST"])
def api_library_create_playlist():
    from db_connection import write_db  # noqa: PLC0415
    from config import LOCAL_DB as _DB  # noqa: PLC0415

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


@bp.route("/api/library/playlists/<playlist_id>/tracks")
def api_library_playlist_tracks(playlist_id):
    from db_connection import read_db  # noqa: PLC0415
    from config import LOCAL_DB as _DB  # noqa: PLC0415

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


@bp.route("/api/library/playlists/<playlist_id>/tracks", methods=["POST"])
def api_library_add_tracks_to_playlist(playlist_id):
    from db_connection import write_db  # noqa: PLC0415
    from config import LOCAL_DB as _DB  # noqa: PLC0415

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


@bp.route("/api/library/playlists/<playlist_id>", methods=["PUT"])
def api_library_rename_playlist(playlist_id):
    from db_connection import write_db  # noqa: PLC0415
    from config import LOCAL_DB as _DB  # noqa: PLC0415

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


@bp.route("/api/library/playlists/<playlist_id>", methods=["DELETE"])
def api_library_delete_playlist(playlist_id):
    from db_connection import write_db  # noqa: PLC0415
    from config import LOCAL_DB as _DB  # noqa: PLC0415

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


@bp.route("/api/library/playlists/<playlist_id>/tracks/<track_id>", methods=["DELETE"])
def api_library_remove_track_from_playlist(playlist_id, track_id):
    from db_connection import write_db  # noqa: PLC0415
    from config import LOCAL_DB as _DB  # noqa: PLC0415

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


@bp.route("/api/library/playlists/<playlist_id>/tracks", methods=["DELETE"])
def api_library_remove_tracks_from_playlist(playlist_id):
    from db_connection import write_db  # noqa: PLC0415
    from config import LOCAL_DB as _DB  # noqa: PLC0415

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


@bp.route("/api/library/tracks/<track_id>", methods=["PATCH"])
def api_library_patch_track(track_id):
    from db_connection import write_db  # noqa: PLC0415
    from config import LOCAL_DB as _DB  # noqa: PLC0415

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


# ── Library USB export ────────────────────────────────────────────────────────

@bp.route("/api/library/export/drives")
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
                drive_info = _detect_pioneer_drive_layout(mountpoint)
                drives.append({
                    "path": mountpoint,
                    "name": Path(mountpoint).name,
                    "free_bytes": usage.free,
                    "total_bytes": usage.total,
                    **drive_info,
                })
            except (PermissionError, OSError):
                continue
        return jsonify(drives)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@bp.route("/api/library/export", methods=["POST"])
def api_library_export_start():
    data = request.get_json(silent=True) or {}
    playlist_ids = data.get("playlist_ids") or []
    drive_path = str(data.get("drive_path") or "").strip()

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


@bp.route("/api/library/export/<job_id>")
def api_library_export_status(job_id):
    with _EXPORT_LOCK:
        job = _EXPORT_JOBS.get(job_id)
    if job is None:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(job)


# ── Legacy playlist / track endpoints (non-library prefix) ────────────────────

@bp.route("/api/playlists")
def api_playlists():
    from db_connection import read_db  # noqa: PLC0415
    from config import LOCAL_DB as _DB  # noqa: PLC0415
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


@bp.route("/api/playlists/<playlist_id>")
def api_playlist(playlist_id):
    from db_connection import read_db  # noqa: PLC0415
    from config import LOCAL_DB as _DB  # noqa: PLC0415
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


@bp.route("/api/tracks")
def api_tracks():
    from db_connection import read_db  # noqa: PLC0415
    from config import LOCAL_DB as _DB  # noqa: PLC0415
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


@bp.route("/api/tracks/<track_id>")
def api_track(track_id):
    from db_connection import read_db  # noqa: PLC0415
    from config import LOCAL_DB as _DB  # noqa: PLC0415
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


@bp.route("/audio/<path:audio_path>")
def serve_audio(audio_path):
    from config import MUSIC_ROOT  # noqa: PLC0415
    abs_path = os.path.join(str(MUSIC_ROOT), audio_path)
    if not os.path.isfile(abs_path):
        return jsonify({"error": "File not found"}), 404
    mime, _ = mimetypes.guess_type(abs_path)
    return send_file(abs_path, mimetype=mime or "audio/mpeg")


# ── In-process audio playback ─────────────────────────────────────────────────

@bp.route("/api/playback/start", methods=["POST"])
def api_playback_start():
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
        _playback["thread"] = threading.Thread(target=_thread, daemon=True)
        _playback["thread"].start()
        _playback["current_path"] = file_path
    return jsonify({"status": "playing", "file_path": file_path})


@bp.route("/api/playback/stop", methods=["POST"])
def api_playback_stop():
    _stop_playback()
    return jsonify({"status": "stopped"})
