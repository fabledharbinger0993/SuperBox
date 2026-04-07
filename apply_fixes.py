"""apply_fixes.py — Run once to patch index.html and app.py in place."""
import re, sys
from pathlib import Path

REPO  = Path(__file__).parent
INDEX = REPO / "templates" / "index.html"
APP   = REPO / "app.py"

idx = INDEX.read_text(encoding="utf-8")
app = APP.read_text(encoding="utf-8")
results = []

# ══════════════════════════════════════════════════════════════
# INDEX.HTML FIXES
# ══════════════════════════════════════════════════════════════

# FIX 1 — _savePipeCfg declaration (original crash bug)
if 'function _savePipeCfg' not in idx:
    idx = idx.replace(
        '\n\n  try { localStorage.setItem(`sb_pipe_cfg_${type}`',
        '\n\nfunction _savePipeCfg(type, cfg) {\n  try { localStorage.setItem(`sb_pipe_cfg_${type}`'
    )
    results.append("FIX 1 (_savePipeCfg declaration): OK")
else:
    results.append("FIX 1: already present")

# FIX 2 — _addOrUpdateSummaryPill dedup fix
OLD2 = (
    "function _addOrUpdateSummaryPill(title) {\n"
    "  const label     = _pillLabel(title);\n"
    "  const container = document.getElementById('session-pills-container');\n"
    "  if (!container) return;\n"
    "  // If a pill for this title already exists, its click handler already reads\n"
    "  // from sessionReports[title] which was just updated — nothing else to do.\n"
    "  if (container.querySelector(`[data-pill-title]`) &&\n"
    "      [...container.querySelectorAll('[data-pill-title]')]\n"
    "        .some(el => el.dataset.pillTitle === title)) return;"
)
NEW2 = (
    "function _addOrUpdateSummaryPill(title, animate) {\n"
    "  const label     = _pillLabel(title);\n"
    "  const container = document.getElementById('session-pills-container');\n"
    "  if (!container) return;\n"
    "  const existing = [...container.querySelectorAll('[data-pill-title]')]\n"
    "    .find(el => el.dataset.pillTitle === title);\n"
    "  if (existing) return;"
)
if OLD2 in idx:
    idx = idx.replace(OLD2, NEW2)
    # Also add animate support at the end of the function
    idx = idx.replace(
        "  container.appendChild(pill);\n}\n\nfunction openReportModal",
        "  container.appendChild(pill);\n"
        "  if (animate) {\n"
        "    pill.style.animation = 'sb-pill-appear .3s cubic-bezier(.16,1,.3,1) forwards';\n"
        "    pill.addEventListener('animationend', () => { pill.style.animation=''; }, {once:true});\n"
        "  }\n"
        "}\n\nfunction openReportModal"
    )
    results.append("FIX 2 (pill dedup + animate): OK")
else:
    results.append("FIX 2: pattern not found — may already be fixed")

# FIX 3 — quit button: replace window.close() with shutdown page
OLD3 = "  // Server is gone — close the tab\n  setTimeout(() => window.close(), 500);\n}"
NEW3 = (
    "  // window.close() only works on script-opened tabs — replace the page instead\n"
    "  setTimeout(() => {\n"
    "    document.open();\n"
    "    document.write(\n"
    "      '<!DOCTYPE html><html><head><meta charset=\"UTF-8\"><title>SuperBox — Stopped</title>'\n"
    "      + '<style>body{background:#0a0a0a;color:#555;font-family:ui-monospace,monospace;'\n"
    "      + 'display:flex;align-items:center;justify-content:center;height:100vh;margin:0;'\n"
    "      + 'flex-direction:column;gap:16px;}p{margin:0;font-size:.9rem;letter-spacing:.04em;}'\n"
    "      + 'strong{color:#888;}</style></head><body>'\n"
    "      + '<p><strong>SuperBox has shut down.</strong></p>'\n"
    "      + '<p>Close this tab or restart the server to continue.</p>'\n"
    "      + '</body></html>'\n"
    "    );\n"
    "    document.close();\n"
    "  }, 500);\n"
    "}"
)
if OLD3 in idx:
    idx = idx.replace(OLD3, NEW3)
    results.append("FIX 3 (quit shutdown page): OK")
else:
    results.append("FIX 3: not found")

# FIX 4 — Animation CSS keyframes in <head>
ANIM_CSS = (
    "<style>\n"
    "/* SuperBox modal animation system */\n"
    "@keyframes sb-modal-in    {from{opacity:0;transform:scale(.92) translateY(10px)} to{opacity:1;transform:scale(1) translateY(0)}}\n"
    "@keyframes sb-modal-out   {from{opacity:1;transform:scale(1) translateY(0)} to{opacity:0;transform:scale(.95) translateY(6px)}}\n"
    "@keyframes sb-modal-shrink{0%{opacity:1;transform:scale(1) translate(0,0)} 100%{opacity:0;transform:scale(.06) translate(52vw,46vh)}}\n"
    "@keyframes sb-pill-appear {from{opacity:0;transform:scale(.55) translateY(10px)} to{opacity:1;transform:scale(1) translateY(0)}}\n"
    "@keyframes sb-backdrop-in {from{opacity:0} to{opacity:1}}\n"
    "@keyframes sb-backdrop-out{from{opacity:1} to{opacity:0}}\n"
    "</style>\n"
)
if 'sb-modal-in' not in idx:
    idx = idx.replace('</head>', ANIM_CSS + '</head>', 1)
    results.append("FIX 4a (animation keyframes): OK")
else:
    results.append("FIX 4a: already present")

# FIX 4b — Replace pulseModal + openReportModal + closeReportModal
OLD_PULSE = (
    "/* ── Modal open pulse ──────────────────────────────────────────────────────\n"
    "   Call pulseModal(innerBoxEl) immediately after making any backdrop visible.\n"
    "   Removes the class on animationend so re-opening replays the animation.   */\n"
    "function pulseModal(el) {\n"
    "  if (!el) return;\n"
    "  el.classList.remove('modal-pulse-once');\n"
    "  void el.offsetWidth; // force reflow so animation restarts on re-open\n"
    "  el.classList.add('modal-pulse-once');\n"
    "  el.addEventListener('animationend', () => el.classList.remove('modal-pulse-once'), { once: true });\n"
    "}\n"
)
NEW_PULSE = (
    "/* ── Modal animation helpers ───────────────────────────────────────────────\n"
    "   _sbAnim(el, keyframe, dur, cb) — runs keyframe then fires cb.\n"
    "   _sbFadeBd(id, show, cb) — fades backdrop in/out.\n"
    "   pulseModal kept as no-op so existing call-sites don't break.            */\n"
    "function _sbAnim(el, kf, dur, cb) {\n"
    "  if (!el) { if (cb) cb(); return; }\n"
    "  el.style.animation = kf + ' ' + dur + ' cubic-bezier(.16,1,.3,1) forwards';\n"
    "  el.addEventListener('animationend', () => { el.style.animation=''; if (cb) cb(); }, {once:true});\n"
    "}\n"
    "function _sbFadeBd(id, show, cb) {\n"
    "  const bd = document.getElementById(id);\n"
    "  if (!bd) { if (cb) cb(); return; }\n"
    "  if (show) { bd.classList.remove('hidden'); _sbAnim(bd, 'sb-backdrop-in', '.2s', cb); }\n"
    "  else      { _sbAnim(bd, 'sb-backdrop-out', '.18s', () => { bd.classList.add('hidden'); if (cb) cb(); }); }\n"
    "}\n"
    "function pulseModal(el) { /* no-op — animations now use _sbAnim */ }\n"
    "\n"
)
if OLD_PULSE in idx:
    idx = idx.replace(OLD_PULSE, NEW_PULSE)
    results.append("FIX 4b (pulseModal replaced): OK")
else:
    results.append("FIX 4b: pattern not found")

# FIX 4c — openReportModal: animate in + store
OLD_OPEN = (
    "function openReportModal(title, text, reportPath) {\n"
    "  document.getElementById('rmod-title').textContent = title + ' — Complete';\n"
    "  const pathEl = document.getElementById('rmod-save-path');\n"
    "  if (reportPath) {\n"
    "    pathEl.textContent = '▸ Report saved to:  ' + reportPath;\n"
    "    pathEl.style.display = '';\n"
    "  } else {\n"
    "    // No saved file — just note it's session-only\n"
    "    pathEl.textContent = '▸ Session summary only — no file saved for this step.';\n"
    "    pathEl.style.display = '';\n"
    "  }\n"
    "  document.getElementById('rmod-pre').textContent = text;\n"
    "  document.getElementById('report-modal-backdrop').classList.remove('hidden');\n"
    "  pulseModal(document.getElementById('report-modal'));\n"
    "  // Keep in session memory and surface as a persistent pill button\n"
    "  sessionReports[title] = { text, reportPath, ts: Date.now() };\n"
    "  _addOrUpdateSummaryPill(title);\n"
    "}\n"
    "\n"
    "function closeReportModal() {\n"
    "  document.getElementById('report-modal-backdrop').classList.add('hidden');\n"
    "}"
)
NEW_OPEN = (
    "function openReportModal(title, text, reportPath) {\n"
    "  document.getElementById('rmod-title').textContent = title + ' — Complete';\n"
    "  const pathEl = document.getElementById('rmod-save-path');\n"
    "  if (reportPath) {\n"
    "    pathEl.textContent = '▸ Report saved to:  ' + reportPath;\n"
    "    pathEl.style.display = '';\n"
    "  } else {\n"
    "    pathEl.textContent = '▸ Session summary only — no file saved for this step.';\n"
    "    pathEl.style.display = '';\n"
    "  }\n"
    "  document.getElementById('rmod-pre').textContent = text;\n"
    "  sessionReports[title] = { text, reportPath, ts: Date.now() };\n"
    "  _sbFadeBd('report-modal-backdrop', true);\n"
    "  const box = document.getElementById('report-modal');\n"
    "  void box.offsetWidth;\n"
    "  _sbAnim(box, 'sb-modal-in', '.28s');\n"
    "}\n"
    "\n"
    "function closeReportModal(shrinkToPill) {\n"
    "  const box   = document.getElementById('report-modal');\n"
    "  const title = (document.getElementById('rmod-title')?.textContent||'').replace(' — Complete','');\n"
    "  if (shrinkToPill) {\n"
    "    _sbAnim(box, 'sb-modal-shrink', '.32s', () => {\n"
    "      _sbFadeBd('report-modal-backdrop', false);\n"
    "      _addOrUpdateSummaryPill(title, true);\n"
    "    });\n"
    "  } else {\n"
    "    _sbAnim(box, 'sb-modal-out', '.18s', () => {\n"
    "      _sbFadeBd('report-modal-backdrop', false);\n"
    "      _addOrUpdateSummaryPill(title);\n"
    "    });\n"
    "  }\n"
    "}"
)
if OLD_OPEN in idx:
    idx = idx.replace(OLD_OPEN, NEW_OPEN)
    results.append("FIX 4c (openReportModal animated): OK")
else:
    results.append("FIX 4c: not found")

# FIX 4d — Got it button → shrink to pill
idx = idx.replace(
    'onclick="closeReportModal()">✓ Got it</button>',
    'onclick="closeReportModal(true)">✓ Got it</button>'
)

# FIX 4e — Auto-open report modal on successful runCommand
OLD_DONE = (
    "      // Store the report as a bottom-right pill — click to open as popup.\n"
    "      if (reportBuffer.length > 0) {\n"
    "        const reportText = reportBuffer.join('\\n');\n"
    "        sessionReports[logTitle] = { text: reportText, reportPath: capturedReportPath };\n"
    "        _addOrUpdateSummaryPill(logTitle);\n"
    "      }\n"
    "      if (onDone) onDone(data.exit_code);"
)
NEW_DONE = (
    "      // On success: auto-open report modal (user dismisses to pill).\n"
    "      // On failure: store silently as pill.\n"
    "      if (reportBuffer.length > 0) {\n"
    "        const reportText = reportBuffer.join('\\n');\n"
    "        if (data.exit_code === 0) {\n"
    "          openReportModal(logTitle, reportText, capturedReportPath);\n"
    "        } else {\n"
    "          sessionReports[logTitle] = { text: reportText, reportPath: capturedReportPath };\n"
    "          _addOrUpdateSummaryPill(logTitle);\n"
    "        }\n"
    "      }\n"
    "      if (onDone) onDone(data.exit_code);"
)
if OLD_DONE in idx:
    idx = idx.replace(OLD_DONE, NEW_DONE)
    results.append("FIX 4e (auto-open on done): OK")
else:
    results.append("FIX 4e: not found")

# FIX 5 — openSettings animation
idx = idx.replace(
    "  document.getElementById('settings-backdrop').classList.remove('hidden');\n"
    "  pulseModal(document.getElementById('settings-modal'));",
    "  _sbFadeBd('settings-backdrop', true);\n"
    "  const _smb = document.getElementById('settings-modal');\n"
    "  void _smb.offsetWidth; _sbAnim(_smb, 'sb-modal-in', '.28s');"
)
idx = idx.replace(
    "function closeSettings() {\n  document.getElementById('settings-backdrop').classList.add('hidden');\n}",
    "function closeSettings() {\n"
    "  _sbAnim(document.getElementById('settings-modal'), 'sb-modal-out', '.18s', () => {\n"
    "    _sbFadeBd('settings-backdrop', false);\n"
    "  });\n"
    "}"
)

# FIX 6 — openWelcome animation
idx = idx.replace(
    "  document.getElementById('welcome-backdrop').classList.remove('hidden');\n"
    "  pulseModal(document.getElementById('welcome-modal'));",
    "  _sbFadeBd('welcome-backdrop', true);\n"
    "  const _wmb = document.getElementById('welcome-modal');\n"
    "  void _wmb.offsetWidth; _sbAnim(_wmb, 'sb-modal-in', '.28s');"
)
idx = idx.replace(
    "function closeWelcome() {\n  document.getElementById('welcome-backdrop').classList.add('hidden');",
    "function closeWelcome() {\n"
    "  _sbAnim(document.getElementById('welcome-modal'), 'sb-modal-out', '.18s', () => {\n"
    "    _sbFadeBd('welcome-backdrop', false);"
)

# FIX 7 — path picker animation
idx = idx.replace(
    "  document.getElementById('path-backdrop').classList.remove('hidden');\n"
    "  pulseModal(document.getElementById('path-modal-box'));",
    "  _sbFadeBd('path-backdrop', true);\n"
    "  const _pmb = document.getElementById('path-modal-box');\n"
    "  void _pmb.offsetWidth; _sbAnim(_pmb, 'sb-modal-in', '.28s');"
)
idx = idx.replace(
    "  document.getElementById('path-backdrop').classList.add('hidden');",
    "  _sbAnim(document.getElementById('path-modal-box'), 'sb-modal-out', '.18s', () => {\n"
    "    _sbFadeBd('path-backdrop', false);\n"
    "  });"
)

# FIX 8 — pipeline wizard animation
idx = idx.replace(
    "  pulseModal(document.getElementById('pipeline-wizard'));",
    "  const _pwb = document.getElementById('pipeline-wizard');\n"
    "  void _pwb.offsetWidth; _sbAnim(_pwb, 'sb-modal-in', '.28s');"
)

results.append("FIX 5-8 (all modal animations): applied")

# FIX 9 — State tracker JS
if 'loadState' not in idx:
    STATE_JS = (
        "\n/* ── State tracker — per-library step completion ─────────────────────────\n"
        "   Calls /api/state on load and after every successful command.\n"
        "   Cards get .step-complete or .step-error CSS classes.              */\n"
        "const STATE_STEP_MAP = {\n"
        "  audit:'step-audit', process:'step-process', duplicates:'step-duplicates',\n"
        "  prune:'step-prune', relocate:'step-relocate', import:'step-import',\n"
        "  link:'step-link', normalize:'step-normalize', convert:'step-convert',\n"
        "  organize:'step-organize', novelty:'step-novelty',\n"
        "};\n"
        "async function loadState(libraryRoot) {\n"
        "  if (!libraryRoot) return;\n"
        "  try {\n"
        "    const res = await fetch('/api/state', {\n"
        "      method:'POST', headers:{'Content-Type':'application/json'},\n"
        "      body: JSON.stringify({ library_root: libraryRoot }),\n"
        "    });\n"
        "    if (res.ok) applyStateToUI(await res.json());\n"
        "  } catch (_) {}\n"
        "}\n"
        "function applyStateToUI(state) {\n"
        "  Object.entries(STATE_STEP_MAP).forEach(([step, cardId]) => {\n"
        "    const card = document.getElementById(cardId);\n"
        "    if (!card) return;\n"
        "    card.classList.remove('step-complete', 'step-error');\n"
        "    const info = state[step];\n"
        "    if (!info) return;\n"
        "    card.classList.add(info.exit_code === 0 ? 'step-complete' : 'step-error');\n"
        "  });\n"
        "}\n"
        "async function _initStateOverlay() {\n"
        "  try {\n"
        "    const cfg = await fetch('/api/config').then(r => r.json());\n"
        "    if (cfg.music_root) loadState(cfg.music_root);\n"
        "  } catch (_) {}\n"
        "}\n"
        "_initStateOverlay();\n"
        "['process-path','import-path','link-path','audit-root','normalize-path',\n"
        " 'organize-target','novelty-dest'].forEach(id => {\n"
        "  const el = document.getElementById(id);\n"
        "  if (el) el.addEventListener('change', () => { if (el.value.trim()) loadState(el.value.trim()); });\n"
        "});\n"
    )
    idx = idx.replace('setupAllDropZones();\n</script>', 'setupAllDropZones();\n' + STATE_JS + '</script>')
    results.append("FIX 9 (state tracker JS): OK")
else:
    results.append("FIX 9: already present")

# ══════════════════════════════════════════════════════════════
# APP.PY FIXES
# ══════════════════════════════════════════════════════════════

if 'state_tracker' not in app:
    app = app.replace(
        '# ── Active-process tracker',
        '# ── Step state tracker ───────────────────────────────────────────────────────\n'
        'try:\n'
        '    from state_tracker import mark_step_complete, get_step_status  # noqa: PLC0415\n'
        'except ImportError:\n'
        '    def mark_step_complete(*a, **kw): pass\n'
        '    def get_step_status(*a, **kw): return {}\n\n'
        '# ── Active-process tracker'
    )
    results.append("APP FIX 1 (state_tracker import): OK")

if '_get_library_root' not in app:
    app = app.replace(
        '# ── Startup ────',
        'def _get_library_root(req, primary_field: str) -> str:\n'
        '    root = req.args.get("library_root", "").strip()\n'
        '    if root: return root\n'
        '    path = req.args.get(primary_field, "").strip()\n'
        '    if path: return str(Path(path))\n'
        '    try:\n'
        '        from config import MUSIC_ROOT  # noqa: PLC0415\n'
        '        return str(MUSIC_ROOT)\n'
        '    except Exception: return ""\n\n\n'
        '# ── Startup ────'
    )
    results.append("APP FIX 2 (_get_library_root): OK")

if 'def _stream(cmd: list[str]):' in app:
    app = app.replace('def _stream(cmd: list[str]):', 'def _stream(cmd: list[str], library_root: str = "", step_name: str = ""):')
    app = app.replace(
        '    global _active_proc\n    try:\n        process = subprocess.Popen(',
        '    global _active_proc\n    _library_root = library_root\n    _step_name    = step_name\n    try:\n        process = subprocess.Popen('
    )
    # inject mark_step_complete before the done yield
    app = app.replace(
        '            process.wait()\n'
        '            yield f"data: {json.dumps({\'done\': True, \'exit_code\': process.returncode})}\\n\\n"\n'
        '        finally:\n'
        '            with _proc_lock:\n'
        '                _active_proc = None\n'
        '    except Exception as exc:\n'
        '        with _proc_lock:\n'
        '            _active_proc = None\n'
        '        yield f"data: {json.dumps({\'line\': f\'[SERVER ERROR] {exc}\', \'done\': True, \'exit_code\': 1})}\\n\\n"',
        '            process.wait()\n'
        '            if _step_name and _library_root:\n'
        '                mark_step_complete(_library_root, _step_name, process.returncode)\n'
        '            yield f"data: {json.dumps({\'done\': True, \'exit_code\': process.returncode})}\\n\\n"\n'
        '        finally:\n'
        '            with _proc_lock:\n'
        '                _active_proc = None\n'
        '    except Exception as exc:\n'
        '        with _proc_lock:\n'
        '            _active_proc = None\n'
        '        yield f"data: {json.dumps({\'line\': f\'[SERVER ERROR] {exc}\', \'done\': True, \'exit_code\': 1})}\\n\\n"'
    )
    results.append("APP FIX 3 (_stream + mark_step_complete): OK")

app = app.replace(
    'def _sse_response(cmd: list[str]) -> Response:\n    return Response(\n        _stream(cmd),',
    'def _sse_response(cmd: list[str], library_root: str = "", step_name: str = "") -> Response:\n    return Response(\n        _stream(cmd, library_root=library_root, step_name=step_name),'
)

# Wire routes
route_patches = [
    ('return _sse_response(cmd)\n\n\n@app.route("/api/run/process")',
     'return _sse_response(cmd, library_root=_get_library_root(request,"root"), step_name="audit")\n\n\n@app.route("/api/run/process")'),
    ('    return _sse_response(cmd)\n\n\n@app.route("/api/run/pipeline"',
     '    library_root=_get_library_root(request,"path")\n    return _sse_response(cmd, library_root=library_root, step_name="process")\n\n\n@app.route("/api/run/pipeline"'),
    ('    return _sse_response(cmd)\n\n\n@app.route("/api/run/convert")',
     '    library_root=_get_library_root(request,"target")\n    return _sse_response(cmd, library_root=library_root, step_name="organize")\n\n\n@app.route("/api/run/convert")'),
    ('    return _sse_response(cmd)\n\n\n@app.route("/api/run/import")',
     '    library_root=_get_library_root(request,"path")\n    return _sse_response(cmd, library_root=library_root, step_name="convert")\n\n\n@app.route("/api/run/import")'),
    ('    return _sse_response(cmd)\n\n\n@app.route("/api/run/link")',
     '    library_root=_get_library_root(request,"path")\n    return _sse_response(cmd, library_root=library_root, step_name="import")\n\n\n@app.route("/api/run/link")'),
    ('    return _sse_response(cmd)\n\n\n@app.route("/api/run/relocate")',
     '    library_root=_get_library_root(request,"path")\n    return _sse_response(cmd, library_root=library_root, step_name="link")\n\n\n@app.route("/api/run/relocate")'),
    ('    return _sse_response(cmd)\n\n\n@app.route("/api/run/novelty")',
     '    library_root=_get_library_root(request,"new_root")\n    return _sse_response(cmd, library_root=library_root, step_name="relocate")\n\n\n@app.route("/api/run/novelty")'),
    ('    return _sse_response(cmd)\n\n\n@app.route("/api/run/duplicates")',
     '    library_root=_get_library_root(request,"dest")\n    return _sse_response(cmd, library_root=library_root, step_name="novelty")\n\n\n@app.route("/api/run/duplicates")'),
    ('    return _sse_response(cmd)\n\n\n# ── Duplicate prune routes',
     '    library_root=_get_library_root(request,"path")\n    return _sse_response(cmd, library_root=library_root, step_name="duplicates")\n\n\n# ── Duplicate prune routes'),
]
for old, new in route_patches:
    if old in app: app = app.replace(old, new)

results.append("APP FIX 4 (route wiring): applied")

if '/api/state' not in app:
    app = app.replace(
        '# ── Archive setup ────────────────────────────────────────────────────────────',
        '@app.route("/api/state", methods=["POST"])\n'
        'def api_state():\n'
        '    """Return steps_completed dict for a given library root."""\n'
        '    data = request.get_json(force=True, silent=True) or {}\n'
        '    root = data.get("library_root", "").strip()\n'
        '    if not root: return jsonify({}), 200\n'
        '    return jsonify(get_step_status(root))\n\n\n'
        '# ── Archive setup ────────────────────────────────────────────────────────────'
    )
    results.append("APP FIX 5 (/api/state): OK")

# ── WRITE FILES ──────────────────────────────────────────────
INDEX.write_text(idx, encoding="utf-8")
APP.write_text(app, encoding="utf-8")

print("=== RESULTS ===")
for r in results:
    print(r)
print("\nDONE — both files written.")
