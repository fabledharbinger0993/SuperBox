# Copilot Instructions For FableGear

## Runtime Context
- Project root: /Users/cameronkelly/FabledHarbinger/Git Repos/FableGear-private-main
- FableGear runs local-only on localhost:5001 via Flask/Waitress.
- Native window shell is in main.py (pywebview) and UI is templates/index.html + static/fablegear.js.
- Rekordbox DB operations use pyrekordbox wrappers in db_connection.py.

## Hard Safety Rules
- Never auto-run write actions against Rekordbox DB.
- Require explicit user action for any write route (import, relocate, prune, organize).
- Assume Rekordbox must be closed before any DB write.

## Domain Logic To Reuse
- Harmonic transitions (Camelot):
  - Same key: nA -> nA, nB -> nB
  - Energy up/down: nA -> (n+1)A or (n-1)A
  - Relative major/minor: nA <-> nB
- Use existing key mapping utilities in key_mapper.py when possible.
- BPM matching guidance: suggest windows around +/- 2 BPM and +/- 6 BPM for pitch/tempo workflows.

## Implementation Preferences
- Backend endpoints in app.py should:
  - validate payloads strictly
  - return JSON with ok/error keys
  - avoid blocking operations on UI thread
- Frontend changes should stay in static/fablegear.js and templates/index.html.
- Keep UI additions compact and non-invasive.
- Preserve local-first behavior (no CDN, no required internet services).

## Testing Expectations
- Python compile must pass for modified Python files.
- Shell scripts must pass bash -n when edited.
- Avoid introducing markdown lint violations in docs.
