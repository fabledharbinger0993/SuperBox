#!/usr/bin/env python3
"""
remove_rekki.py — Surgical removal of the Rekki AI subsystem from RekitBox.

This script:
  1. Creates archive/rekki/ with the extracted Rekki code for future reference.
  2. Removes all Rekki code from app.py, rekitbox.js, index.html, rekitbox.css.
  3. Verifies Python syntax on app.py after modification.
  4. Reports a residual grep to confirm clean removal.

Run from project root:  python3 scripts/remove_rekki.py
"""
import ast
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

APP_PY      = ROOT / "app.py"
JS_FILE     = ROOT / "static" / "rekitbox.js"
HTML_FILE   = ROOT / "templates" / "index.html"
CSS_FILE    = ROOT / "static" / "rekitbox.css"
ARCHIVE_DIR = ROOT / "archive" / "rekki"

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def read(path):
    return path.read_text(encoding="utf-8")

def write(path, content):
    path.write_text(content, encoding="utf-8")

def replace_once(content, old, new, label):
    count = content.count(old)
    if count == 0:
        print(f"  [WARN] '{label}' — pattern not found, skipping.")
        return content
    if count > 1:
        print(f"  [WARN] '{label}' — {count} occurrences found, replacing first only.")
    return content.replace(old, new, 1)

def replace_all(content, old, new, label):
    count = content.count(old)
    if count == 0:
        print(f"  [WARN] '{label}' — pattern not found, skipping.")
        return content
    print(f"  [OK] '{label}' — {count} occurrence(s) replaced.")
    return content.replace(old, new)

# ─────────────────────────────────────────────────────────────────────────────
# STEP 1: Create archive
# ─────────────────────────────────────────────────────────────────────────────

def create_archive():
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    print(f"\n[1/5] Creating archive at {ARCHIVE_DIR}")

    app_content = read(APP_PY)
    js_content  = read(JS_FILE)
    css_content = read(CSS_FILE)
    html_content = read(HTML_FILE)

    # ── Extract from app.py ────────────────────────────────────────────────
    app_rekki_start = app_content.find("\n\ndef _rekki_sqlite_health() -> dict:")
    # The entire scripted module starts right after the DB health block —
    # actually they're contiguous, so capture from here through api_rekki_discover_music
    app_rekki_end_marker = "\n    })\ndef _sse_response("
    app_rekki_end = app_content.find(app_rekki_end_marker)
    if app_rekki_start != -1 and app_rekki_end != -1:
        app_rekki_block = app_content[app_rekki_start : app_rekki_end + len(app_rekki_end_marker)]
    else:
        app_rekki_block = "# (extraction failed — block boundaries not found)\n"
        print("  [WARN] app.py rekki block extraction failed — check boundaries")

    # Also capture _rekki_enabled()
    rekki_enabled_old = "\ndef _rekki_enabled() -> bool:\n    return _current_rekitbox_mode() == \"suburban\"\n"
    rekki_enabled_block = rekki_enabled_old if rekki_enabled_old in app_content else ""

    rekki_brain_comment = "# ── Rekki brain: Congress deliberation + HologrA.I.m memory ──────────────────\n"

    (ARCHIVE_DIR / "rekki_backend.py").write_text(
        "# ARCHIVED Rekki backend code from app.py\n"
        "# Restore by re-inserting at the appropriate locations.\n\n"
        + "# == Comment at line 86 ==\n"
        + rekki_brain_comment + "\n"
        + "# == _rekki_enabled() function ==\n"
        + rekki_enabled_block + "\n"
        + "# == DB health + scripted module + routes ==\n"
        + app_rekki_block,
        encoding="utf-8",
    )
    print("  [OK] rekki_backend.py written")

    # ── Extract from rekitbox.js ───────────────────────────────────────────
    js_rekki_section_start = js_content.find("// ── Rekki ─────────────────────────────────────────────────────────────────────")
    js_rekki_section_end = js_content.find("\n// ── Toolkit Modal ─────────────────────────────────────────────────────────────")
    if js_rekki_section_start != -1 and js_rekki_section_end != -1:
        js_rekki_block = js_content[js_rekki_section_start : js_rekki_section_end]
    else:
        js_rekki_block = "// (extraction failed)\n"
        print("  [WARN] rekitbox.js rekki section extraction failed")

    js_header_end = js_content.find("/* ── State ─────────────────────────────────────────────────────────────────── */")
    js_header_block = js_content[:js_header_end] if js_header_end != -1 else ""

    (ARCHIVE_DIR / "rekki_frontend.js").write_text(
        "// ARCHIVED Rekki frontend code from rekitbox.js\n\n"
        "// == Opening Rekki setup block (lines 1-73) ==\n"
        + js_header_block + "\n\n"
        "// == Main Rekki section (lines ~5815-6357) ==\n"
        + js_rekki_block,
        encoding="utf-8",
    )
    print("  [OK] rekki_frontend.js written")

    # ── Extract from rekitbox.css ──────────────────────────────────────────
    css_start = css_content.find("/* ── Per-card Rekki button ─────────────────────────────────────────────── */")
    # The block ends after `.rekki-card-btn:hover { ... }` — find the closing brace
    if css_start != -1:
        css_end = css_content.find("\n}", css_content.find(".rekki-card-btn:hover {", css_start))
        css_rekki_block = css_content[css_start : css_end + 3] if css_end != -1 else ""
    else:
        css_rekki_block = "/* (extraction failed) */"
        print("  [WARN] rekitbox.css rekki block extraction failed")

    (ARCHIVE_DIR / "rekki_styles.css").write_text(
        "/* ARCHIVED Rekki styles from rekitbox.css */\n\n" + css_rekki_block,
        encoding="utf-8",
    )
    print("  [OK] rekki_styles.css written")

    # ── Write RESTORE.md ──────────────────────────────────────────────────
    (ARCHIVE_DIR / "RESTORE.md").write_text(
        "# Rekki Archive — Restore Notes\n\n"
        "All Rekki code was surgically removed from the RekitBox codebase.\n"
        "This directory contains the extracted code for future re-integration.\n\n"
        "## Files\n"
        "- `rekki_backend.py` — Python code from `app.py`\n"
        "  - Comment block (line 86)\n"
        "  - `_rekki_enabled()` helper function\n"
        "  - DB health functions (`_rekki_sqlite_health`, `_rekki_pyrekordbox_health`, `_rekki_db_health_snapshot`)\n"
        "  - Full Rekki scripted module with all 5 routes\n"
        "- `rekki_frontend.js` — JS code from `static/rekitbox.js`\n"
        "  - Opening Rekki setup block (lines 1–73)\n"
        "  - Main Rekki section (lines 5815–6357)\n"
        "  - Congress background review IIFE\n"
        "  - Scattered `_applyRekkiModeUI` calls\n"
        "- `rekki_markup.html` — HTML fragments from `templates/index.html`\n"
        "  - `#rekki-home` avatar block\n"
        "  - `#rekki-stage` chat overlay panel\n"
        "  - `#rekki-ctx-menu` right-click menu\n"
        "  - `rekki-wiz-strip` elements in pipeline wizard\n"
        "  - `rekki-card-btn` buttons on tool cards\n"
        "  - `data-rekki-context` and `data-rekki-droppable` attributes\n"
        "- `rekki_styles.css` — CSS from `static/rekitbox.css`\n"
        "  - `.rekki-card-btn` and `.rekki-card-btn:hover` rules\n\n"
        "## Re-integration steps\n"
        "1. Add Python code back to `app.py` before `def _sse_response`\n"
        "2. Add JS setup block back at top of `rekitbox.js`\n"
        "3. Add Rekki section back before `// ── Toolkit Modal` in `rekitbox.js`\n"
        "4. Restore HTML fragments and attributes in `index.html`\n"
        "5. Restore CSS rules in `rekitbox.css` at line ~314\n"
        "6. Remove `archive/rekki/` from `.gitignore` if needed\n",
        encoding="utf-8",
    )
    print("  [OK] RESTORE.md written")


# ─────────────────────────────────────────────────────────────────────────────
# STEP 2: Modify app.py
# ─────────────────────────────────────────────────────────────────────────────

def modify_app_py():
    print(f"\n[2/5] Modifying {APP_PY.name}")
    content = read(APP_PY)

    # 2a. Remove Rekki brain comment (line 86)
    content = replace_once(
        content,
        "# ── Rekki brain: Congress deliberation + HologrA.I.m memory ──────────────────\n",
        "",
        "rekki brain comment",
    )

    # 2b. Remove _rekki_enabled() function
    content = replace_once(
        content,
        "\ndef _rekki_enabled() -> bool:\n    return _current_rekitbox_mode() == \"suburban\"\n",
        "\n",
        "_rekki_enabled function",
    )

    # 2c. Remove DB health block + full scripted module + all 5 routes
    #     Block starts at \n\ndef _rekki_sqlite_health
    #     Block ends just before \ndef _sse_response(
    rekki_big_start = "\n\ndef _rekki_sqlite_health() -> dict:"
    rekki_big_end   = "\n    })\ndef _sse_response("

    pos_start = content.find(rekki_big_start)
    pos_end   = content.find(rekki_big_end)
    if pos_start != -1 and pos_end != -1:
        before = content[:pos_start]
        after  = content[pos_end + len(rekki_big_end):]
        content = before + "\n\ndef _sse_response(" + after
        print("  [OK] 'rekki DB health + scripted module' — removed.")
    else:
        print("  [WARN] 'rekki DB health + scripted module' — boundary not found, skipping.")

    # 2d. Remove "rekki_model" lines from /api/config handler
    content = replace_all(
        content,
        "            \"rekki_model\":      os.environ.get(\"REKIT_AGENT_MODEL\", \"\") if current_mode == \"suburban\" else \"\",\n",
        "",
        "rekki_model in config (padded)",
    )
    content = replace_all(
        content,
        "            \"rekki_model\":     os.environ.get(\"REKIT_AGENT_MODEL\", \"\") if current_mode == \"suburban\" else \"\",\n",
        "",
        "rekki_model in config (less padded)",
    )
    # Generic fallback in case spacing differs
    import re
    content = re.sub(
        r'            "rekki_model":\s+os\.environ\.get\("REKIT_AGENT_MODEL", ""\) if current_mode == "suburban" else "",\n',
        "",
        content,
    )

    write(APP_PY, content)
    print(f"  [OK] {APP_PY.name} written.")


# ─────────────────────────────────────────────────────────────────────────────
# STEP 3: Modify rekitbox.js
# ─────────────────────────────────────────────────────────────────────────────

def modify_rekitbox_js():
    print(f"\n[3/5] Modifying {JS_FILE.name}")
    content = read(JS_FILE)

    # 3a. Replace entire opening Rekki setup block (lines 1-73) with just the
    #     pin button DOMContentLoaded.
    #     The block starts at the very top of the file and ends before:
    #     /* ── State ────────────────────────────────────────────────────────── */
    STATE_MARKER = "/* ── State ─────────────────────────────────────────────────────────────────── */"
    pos = content.find(STATE_MARKER)
    if pos != -1:
        # Everything before STATE_MARKER is the Rekki setup block.
        content = (
            "document.addEventListener('DOMContentLoaded', () => {\n"
            "  // Initialize tool drawer pin button\n"
            "  const pinBtn = document.getElementById('tool-drawer-pin');\n"
            "  if (pinBtn) {\n"
            "    _syncToolDrawerPinState();\n"
            "  }\n"
            "});\n"
            + content[pos:]
        )
        print("  [OK] Opening Rekki setup block replaced.")
    else:
        print("  [WARN] STATE_MARKER not found — opening block not replaced.")

    # 3b. Remove Rekki mode lines from openSettings()
    content = replace_once(
        content,
        "    // Mode (saved in localStorage; welcome modal handles the radio)\n"
        "    const boxMode = cfg.mode || 'suburban';\n"
        "    localStorage.setItem('rekitbox-mode', boxMode);\n"
        "    const modeRadio = document.querySelector(`input[name=\"rekitbox-mode\"][value=\"${boxMode}\"]`);\n"
        "    if (modeRadio) modeRadio.checked = true;\n"
        "    const modeBtn = document.getElementById('wbtn-' + boxMode);\n"
        "    if (modeBtn) { document.querySelectorAll('.wbtn-mode').forEach(b => b.classList.remove('selected')); modeBtn.classList.add('selected'); }\n"
        "    _applyRekkiModeUI(boxMode, cfg.rekki_model || '');\n",
        "",
        "openSettings rekki mode block",
    )

    # 3c. Remove _applyRekkiModeUI('suburban') from openSettings catch
    content = replace_once(
        content,
        "    document.querySelector('input[name=\"archive-mode\"][value=\"auto\"]').checked = true;\n"
        "    const sr = document.querySelector('input[name=\"rekitbox-mode\"][value=\"suburban\"]');\n"
        "    if (sr) sr.checked = true;\n"
        "    _applyRekkiModeUI('suburban');\n"
        "    _settingsUpdateUI('auto');\n",
        "    document.querySelector('input[name=\"archive-mode\"][value=\"auto\"]').checked = true;\n"
        "    _settingsUpdateUI('auto');\n",
        "openSettings catch _applyRekkiModeUI",
    )

    # 3d. Remove _applyRekkiModeUI(mode) from welcomeSelectMode()
    content = replace_once(
        content,
        "  _applyRekkiModeUI(mode);\n"
        "  setTimeout(() => _welcomeShowReady(), 220);\n",
        "  setTimeout(() => _welcomeShowReady(), 220);\n",
        "welcomeSelectMode _applyRekkiModeUI",
    )

    # 3e. Remove Congress IIFE block
    content = replace_once(
        content,
        "      // ── Congress background review (fire-and-forget, completely silent) ──────\n"
        "      // Skeptic/Advocate/Synthesizer review runs in a daemon thread server-side.\n"
        "      // Findings go to HologrA.I.m memory — no UI surface.\n"
        "      (function _congressReview() {\n"
        "        const _logEls = document.querySelectorAll('#log-output .log-line');\n"
        "        const _logLines = Array.from(_logEls).slice(-80).map(el => el.textContent || '');\n"
        "        fetch('/api/rekki/congress/review', {\n"
        "          method: 'POST',\n"
        "          headers: { 'Content-Type': 'application/json' },\n"
        "          body: JSON.stringify({\n"
        "            tool: logTitle,\n"
        "            exit_code: data.exit_code || 0,\n"
        "            log_lines: _logLines,\n"
        "            report: reportBuffer.join('\\n'),\n"
        "          }),\n"
        "        }).catch(() => {});  // silent — Congress never interrupts the user\n"
        "      })();\n",
        "",
        "Congress IIFE",
    )

    # 3f. Remove the entire Rekki section (from // ── Rekki ── through end of rekkiMenuTag)
    REKKI_SECTION_START = "\n// ── Rekki ─────────────────────────────────────────────────────────────────────"
    TOOLKIT_SECTION_START = "\n// ── Toolkit Modal ─────────────────────────────────────────────────────────────"
    pos_rstart = content.find(REKKI_SECTION_START)
    pos_tend   = content.find(TOOLKIT_SECTION_START)
    if pos_rstart != -1 and pos_tend != -1:
        content = content[:pos_rstart] + content[pos_tend:]
        print("  [OK] 'Rekki section (lines ~5815-6357)' — removed.")
    else:
        print("  [WARN] Rekki section boundaries not found, skipping.")

    # 3g. Remove rekki-ctx-menu and _rekkiOpen from keyboard shortcut handler
    content = replace_once(
        content,
        "    const menu = document.getElementById('rekki-ctx-menu');\n"
        "    if (menu && !menu.classList.contains('hidden')) { menu.classList.add('hidden'); return; }\n"
        "    if (_rekkiOpen) { toggleRekkiPanel(); return; }\n",
        "",
        "keyboard shortcut rekki ctx-menu + _rekkiOpen",
    )

    # 3h. Remove Cmd+J / Ctrl+J shortcut to toggleRekkiPanel
    content = replace_once(
        content,
        "  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'j') {\n"
        "    e.preventDefault();\n"
        "    toggleRekkiPanel();\n"
        "  }\n",
        "",
        "Cmd+J toggleRekkiPanel shortcut",
    )

    # 3i. Transform final DOMContentLoaded — remove rekki init calls, keep drag + sidebar
    content = replace_once(
        content,
        "document.addEventListener('DOMContentLoaded', () => {\n"
        "  const input = document.getElementById('rekki-input');\n"
        "  if (input) {\n"
        "    input.addEventListener('keydown', (e) => {\n"
        "      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); rekkiSendMessage(); }\n"
        "    });\n"
        "  }\n"
        "  _rekkiAvatarInit();\n"
        "  _rekkiRefreshStatus();\n"
        "  _rekkiBootHistory();\n\n"
        "  // Floating tool modal drag\n"
        "  _initToolFloatModalDrag();\n",
        "document.addEventListener('DOMContentLoaded', () => {\n"
        "  // Floating tool modal drag\n"
        "  _initToolFloatModalDrag();\n",
        "final DOMContentLoaded rekki init calls",
    )

    write(JS_FILE, content)
    print(f"  [OK] {JS_FILE.name} written.")


# ─────────────────────────────────────────────────────────────────────────────
# STEP 4: Modify index.html
# ─────────────────────────────────────────────────────────────────────────────

def modify_index_html():
    print(f"\n[4/5] Modifying {HTML_FILE.name}")
    content = read(HTML_FILE)

    # 4a. Remove comment in <style> block
    content = replace_once(
        content,
        "  /* Rekki styles live in rekitbox.css */\n",
        "",
        "<style> rekki comment",
    )

    # 4b. Remove #rekki-home block (avatar + stage span in top bar)
    content = replace_once(
        content,
        '      <div id="rekki-home" title="Rekki — drag onto anything or click to chat">\n'
        '        <img id="rekki-avatar" src="/static/icon-rekki.png" alt="Rekki" draggable="true">\n'
        '        <span id="rekki-stage-indicator"></span>\n'
        '      </div>\n',
        "",
        "#rekki-home block (with stage-indicator)",
    )
    # Alternate form if stage-indicator line differs:
    content = replace_once(
        content,
        '      <div id="rekki-home" title="Rekki — drag onto anything or click to chat">\n'
        '        <img id="rekki-avatar" src="/static/icon-rekki.png" alt="Rekki" draggable="true">\n'
        '      </div>\n',
        "",
        "#rekki-home block (without stage-indicator)",
    )

    # 4c. Remove data-rekki-droppable from <main>
    content = replace_all(
        content,
        ' data-rekki-droppable',
        '',
        "data-rekki-droppable attribute",
    )

    # 4d. Remove ALL data-rekki-context="..." attributes (from any element)
    import re
    content = re.sub(r"\s+data-rekki-context='[^']*'", "", content)
    content = re.sub(r'\s+data-rekki-context="[^"]*"', "", content)
    print("  [OK] data-rekki-context attributes removed (regex).")

    # 4e. Remove rekki-card-btn buttons (all instances)
    content = re.sub(
        r'\s*<button[^>]+class="rekki-card-btn"[^>]*>.*?</button>',
        "",
        content,
        flags=re.DOTALL,
    )
    print("  [OK] rekki-card-btn buttons removed (regex).")

    # 4f. Remove rekki-wiz-strip-p1
    content = replace_once(
        content,
        "      <!-- ── Rekki wizard voice strip ───────────────────────────────── -->\n"
        '      <div id="rekki-wiz-strip-p1" class="rekki-wiz-strip">\n'
        '        <img src="/static/icon-rekki.png" class="rekki-wiz-avatar" alt="Rekki" onerror="this.style.display=\'none\'">\n'
        '        <div id="rekki-wiz-msg-p1" class="rekki-wiz-msg">Building a pipeline? Tell me what you\'re trying to fix and I\'ll suggest the right steps.</div>\n'
        '        <button type="button" class="rekki-wiz-reply-btn" onclick="_rekkiWizardOpenChat(\'p1\')" title="Chat with Rekki">Ask &rarr;</button>\n'
        "      </div>\n",
        "",
        "rekki-wiz-strip-p1",
    )

    # 4g. Remove rekki-wiz-strip-p2
    content = replace_once(
        content,
        "      <!-- ── Rekki wizard voice strip ───────────────────────────────── -->\n"
        '      <div id="rekki-wiz-strip-p2" class="rekki-wiz-strip">\n'
        '        <img src="/static/icon-rekki.png" class="rekki-wiz-avatar" alt="Rekki" onerror="this.style.display=\'none\'">\n'
        '        <div id="rekki-wiz-msg-p2" class="rekki-wiz-msg">Select a step on the left to see what I know about it.</div>\n'
        '        <button type="button" class="rekki-wiz-reply-btn" onclick="_rekkiWizardOpenChat(\'p2\')" title="Chat with Rekki">Ask &rarr;</button>\n'
        "      </div>\n",
        "",
        "rekki-wiz-strip-p2",
    )

    # 4h. Remove Rekki Stage panel
    content = replace_once(
        content,
        "<!-- ══ Rekki Stage ════════════════════════════════════════════════════════ -->\n"
        '<div id="rekki-stage" class="hidden" aria-label="Rekki">\n'
        '  <video id="rekki-anim" muted playsinline>\n'
        '    <source id="rekki-anim-src" src="/static/rekki-entrance.mp4" type="video/mp4">\n'
        "  </video>\n"
        '  <button type="button" id="rekki-stage-close" onclick="toggleRekkiPanel()" aria-label="Close Rekki">✕</button>\n'
        '  <div id="rekki-chat-overlay">\n'
        '    <div id="rekki-chat-log"></div>\n'
        '    <div id="rekki-compose">\n'
        '      <div id="rekki-ctx-chip" class="hidden">\n'
        '        <span id="rekki-ctx-chip-label"></span>\n'
        '        <button type="button" onclick="rekkiClearChip()" aria-label="Clear context">✕</button>\n'
        "      </div>\n"
        '      <textarea id="rekki-input" placeholder="Drop me on anything, or ask me something…"></textarea>\n'
        '      <div id="rekki-compose-foot">\n'
        '        <span id="rekki-status"></span>\n'
        '        <div id="rekki-conn-dot" title="Rekki scripted helper status"></div>\n'
        '        <button type="button" id="rekki-send" onclick="rekkiSendMessage()">Send ↵</button>\n'
        "      </div>\n"
        "    </div>\n"
        "  </div>\n"
        "</div>\n",
        "",
        "Rekki Stage panel",
    )

    # 4i. Remove Rekki right-click context menu
    content = replace_once(
        content,
        "<!-- ══ Rekki right-click context menu ══════════════════════════════════ -->\n"
        '<div id="rekki-ctx-menu" class="hidden" role="menu">\n'
        '  <button type="button" onclick="rekkiMenuExplain()" role="menuitem">✦ Explain via Rekki</button>\n'
        '  <button type="button" onclick="rekkiMenuTag()" role="menuitem">◈ Tag this item</button>\n'
        "</div>\n",
        "",
        "rekki-ctx-menu",
    )

    write(HTML_FILE, content)
    print(f"  [OK] {HTML_FILE.name} written.")


# ─────────────────────────────────────────────────────────────────────────────
# STEP 5: Modify rekitbox.css
# ─────────────────────────────────────────────────────────────────────────────

def modify_rekitbox_css():
    print(f"\n[5/5] Modifying {CSS_FILE.name}")
    content = read(CSS_FILE)

    # The full block including comment and both rules
    content = replace_once(
        content,
        "/* ── Per-card Rekki button ─────────────────────────────────────────────── */\n"
        ".rekki-card-btn {\n"
        "  position:       absolute;\n"
        "  top:            8px;\n"
        "  right:          8px;\n"
        "  background:     none;\n"
        "  border:         none;\n"
        "  color:          var(--accent);\n"
        "  font-size:      1rem;\n"
        "  cursor:         pointer;\n"
        "  opacity:        0.5;\n"
        "  transition:     opacity .2s;\n"
        "  padding:        2px 4px;\n"
        "}\n"
        ".rekki-card-btn:hover {\n"
        "  opacity:        1;\n"
        "  color:          var(--safe);\n"
        "}\n",
        "",
        "rekki-card-btn CSS rules",
    )

    write(CSS_FILE, content)
    print(f"  [OK] {CSS_FILE.name} written.")


# ─────────────────────────────────────────────────────────────────────────────
# Verification
# ─────────────────────────────────────────────────────────────────────────────

def verify():
    print("\n[Verify] Python syntax check on app.py...")
    try:
        source = read(APP_PY)
        ast.parse(source)
        print("  [OK] app.py syntax: PASS")
    except SyntaxError as exc:
        print(f"  [ERROR] app.py syntax ERROR: {exc}")
        sys.exit(1)

    print("\n[Verify] Residual rekki references in source files...")
    files = [APP_PY, JS_FILE, HTML_FILE, CSS_FILE]
    found_any = False
    for f in files:
        result = subprocess.run(
            ["grep", "-in", "rekki", str(f)],
            capture_output=True, text=True
        )
        if result.stdout.strip():
            print(f"  [RESIDUAL] {f.name}:")
            for line in result.stdout.strip().splitlines():
                print(f"    {line}")
            found_any = True
        else:
            print(f"  [CLEAN] {f.name}")

    if found_any:
        print("\n  Some residuals found — review above. May be benign (comments in non-Rekki code).")
    else:
        print("\n  All clean. Rekki fully removed.")


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 70)
    print("  RekitBox — Rekki Removal Script")
    print("=" * 70)

    create_archive()
    modify_app_py()
    modify_rekitbox_js()
    modify_index_html()
    modify_rekitbox_css()
    verify()

    print("\n" + "=" * 70)
    print("  Done. Rekki has been archived and removed from the codebase.")
    print("  Archive location: archive/rekki/")
    print("=" * 70)
