# RekitBox Playlist/Track API Plan

## Endpoints (all local, Flask)

- `GET /api/playlists` — List all playlists (id, name, track_count)
- `GET /api/playlists/<playlist_id>` — Get playlist details and tracks
- `POST /api/playlists` — Create new playlist (name)
- `PUT /api/playlists/<playlist_id>` — Rename playlist
- `DELETE /api/playlists/<playlist_id>` — Delete playlist
- `POST /api/playlists/<playlist_id>/tracks` — Add track to playlist
- `DELETE /api/playlists/<playlist_id>/tracks/<track_id>` — Remove track from playlist

- `GET /api/tracks` — List all tracks (id, title, artist, bpm, key, file_path, etc.)
- `GET /api/tracks/<track_id>` — Get track details
- `POST /api/tracks` — Import/add new track (file_path)

- `GET /api/library/tree` — Get playlist tree (folders, playlists, tracks)

- `GET /audio/<path>` — Serve audio file for playback (Flask static route)

## Notes
- All endpoints return JSON
- All write actions require Rekordbox to be closed (enforced in backend)
- pyrekordbox handles DB and playlist XML export
- File/folder structure matches Rekordbox One Library for Pioneer compatibility

---

This plan covers the backend API for playlist/track browsing, editing, and export, plus audio serving for the media player.