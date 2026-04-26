# Rekki Archive — Restore Notes

All Rekki code was surgically removed from the RekitBox codebase.
This directory contains the extracted code for future re-integration.

## Files
- `rekki_backend.py` — Python code from `app.py`
  - Comment block (line 86)
  - `_rekki_enabled()` helper function
  - DB health functions (`_rekki_sqlite_health`, `_rekki_pyrekordbox_health`, `_rekki_db_health_snapshot`)
  - Full Rekki scripted module with all 5 routes
- `rekki_frontend.js` — JS code from `static/rekitbox.js`
  - Opening Rekki setup block (lines 1–73)
  - Main Rekki section (lines 5815–6357)
  - Congress background review IIFE
  - Scattered `_applyRekkiModeUI` calls
- `rekki_markup.html` — HTML fragments from `templates/index.html`
  - `#rekki-home` avatar block
  - `#rekki-stage` chat overlay panel
  - `#rekki-ctx-menu` right-click menu
  - `rekki-wiz-strip` elements in pipeline wizard
  - `rekki-card-btn` buttons on tool cards
  - `data-rekki-context` and `data-rekki-droppable` attributes
- `rekki_styles.css` — CSS from `static/rekitbox.css`
  - `.rekki-card-btn` and `.rekki-card-btn:hover` rules

## Re-integration steps
1. Add Python code back to `app.py` before `def _sse_response`
2. Add JS setup block back at top of `rekitbox.js`
3. Add Rekki section back before `// ── Toolkit Modal` in `rekitbox.js`
4. Restore HTML fragments and attributes in `index.html`
5. Restore CSS rules in `rekitbox.css` at line ~314
6. Remove `archive/rekki/` from `.gitignore` if needed
