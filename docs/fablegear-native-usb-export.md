# FableGear-Native USB Export & File Structure

## Export Workflow

1. User selects target USB drive
2. FableGear validates drive (format, free space, folder structure)
3. Pyrekordbox writes Rekordbox-compatible SQLite DB to drive
4. FableGear generates all required folders/files (PIONEER, contents, playlists, cues, etc.)
5. Optionally, custom folder structure for advanced users
6. Final validation: check for all required files, report errors

## File/Folder Structure

- /PIONEER/
  - /DJM/
  - /CDJ/
  - /Contents/
  - /Playlists/
  - /Analysis/
  - /Artwork/
  - /HotCues/
  - /Waveforms/
- /Contents/ (audio files, organized by artist/album or flat)
- /Playlists/ (XML or DB references)
- /rekordbox/ (hidden, for DB and settings)

## Compatibility

- All files/folders must match what Pioneer hardware expects
- Use Pyrekordbox for DB schema and export logic
- Test on real hardware (CDJ, Omnis, XDJ, etc.)

## Safety

- Never overwrite existing DB/files without backup
- Validate before/after export, show user errors/warnings

---

This plan ensures FableGear can export fully compatible USBs for Pioneer hardware, with robust validation and user safety.
