# RekitBox-Native Sync/Repair Interface

## Purpose
- Keep RekitBox library in sync with Rekordbox DB for compatibility and recovery
- Allow re-import/repair if Rekordbox DB changes or is updated externally

## Features
- One-click re-import from Rekordbox DB
- Visual diff/merge for playlists, tracks, cues
- Schema drift detection: warn if Rekordbox DB version changes
- Backup before any destructive sync/repair
- Error/warning reporting for conflicts or failed imports

## Workflow
1. User triggers sync/repair
2. RekitBox reads current Rekordbox DB (via pyrekordbox)
3. Compares with internal library state
4. Presents diff/merge UI for user approval
5. Applies changes, updates internal DB, backs up as needed

## Safety
- Never overwrite user data without explicit confirmation
- Always create a backup before applying changes
- Warn user of any compatibility or schema issues

---

This interface ensures RekitBox remains robust and compatible, with safe recovery from DB changes or errors.