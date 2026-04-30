# FableGear-Native Library/Playlist/USB Architecture

## 1. Core Data Model
- **Track**: { id, title, artist, album, path, bpm, key, cues, tags, date_added, ... }
- **Playlist**: { id, name, tracks: [track_id], parent_id, ... }
- **Library**: { tracks: {id: Track}, playlists: {id: Playlist}, ... }
- **Sync State**: { last_import, last_export, rekordbox_db_version, ... }

## 2. Storage
- Primary: SQLite (mirrors Rekordbox schema where possible, but can extend for new features)
- Export: Generates Rekordbox-compatible DB and file/folder structure for Pioneer hardware
- Import: Can re-sync from Rekordbox DB if needed (for updates/repairs)

## 3. UI/UX
- Library browser: Search, filter, sort, tag, edit
- Playlist builder: Drag/drop, nested playlists, smart folders
- Media player: Preview/cue local files
- USB export: One-click export to drive, with validation and error reporting
- Sync/repair: UI for re-importing or fixing DB from Rekordbox

## 4. USB Export Logic
- Use Pyrekordbox to write DB and generate all required files/folders
- Validate output on real Pioneer hardware (CDJ, Omnis, etc.)
- Optionally support custom folder structures for advanced users

## 5. Compatibility Layer
- Pyrekordbox as the bridge for DB read/write and schema updates
- FableGear never overwrites Rekordbox DB without explicit user action
- All destructive actions require confirmation and backup

## 6. Extensibility
- Modular: easy to add new tools (analysis, dedupe, etc.)
- Future: monitor for legal, user-authenticated cloud streaming options

---

This architecture enables FableGear to function as a standalone, Rekordbox-compatible DJ library manager, maximizing user freedom while maintaining legal and technical safety.