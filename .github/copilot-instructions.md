# Copilot Instructions For RekitBox + Rekki

## Runtime Context
- Project root: /Users/cameronkelly/FabledHarbinger/Git Repos/RekitBox-private-main
- RekitBox runs local-only on localhost:5001 via Flask/Waitress.
- Native window shell is in main.py (pywebview) and UI is templates/index.html + static/rekitbox.js.
- Rekordbox DB operations use pyrekordbox wrappers in db_connection.py.

## Hard Safety Rules
- Never auto-run write actions against Rekordbox DB.
- Require explicit user action for any write route (import, relocate, prune, organize).
- Assume Rekordbox must be closed before any DB write.
- Default Rekki assistant behavior is read-only guidance.
- Keep apply/commit/push automation flags disabled by default.

## Rekki Agent Loop (Reason -> Act -> Observe)
1. Reason:
   - Parse user intent (example: set-building, prep auditing, duplicates review).
   - Build minimal context from /api/rekki/context and current app status.
2. Act:
   - Prefer read-only operations first (status, health, summaries, dry-run suggestions).
   - If proposing write actions, return a checklist and required confirmations.
3. Observe:
   - Re-check status and report deltas.
   - Surface errors in plain language with next safe step.

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
- Frontend changes should stay in static/rekitbox.js and templates/index.html.
- Keep UI additions compact and non-invasive.
- Preserve local-first behavior (no CDN, no required internet services).

## Suggested Next Feature Slice
- Add read-only Rekki helper tools:
  - "Find compatible tracks" by BPM/key window
  - "Prep gap audit" (missing BPM/key/hot-cue indicators from available metadata)
  - "Set builder seed" (genre + tempo + harmonic compatibility summary)

## Testing Expectations
- Python compile must pass for modified Python files.
- Shell scripts must pass bash -n when edited.
- Avoid introducing markdown lint violations in docs.
