/* ── State ─────────────────────────────────────────────────────────────────── */
let activeSource = null;
let isRunning    = false;
let rbRunning    = false;

/* ── File Browser Panel ─────────────────────────────────────────────────────── */
let _fbCurrentPath = '/Volumes';

function toggleFileBrowser() {
  const panel = document.getElementById('fb-panel');
  const btn   = document.getElementById('fb-toggle-btn');
  const isOpen = panel.classList.toggle('fb-open');
  btn.classList.toggle('active', isOpen);
  if (isOpen) fbNavigateTo(_fbCurrentPath);
}

async function fbNavigateTo(path) {
  _fbCurrentPath = path;
  const list = document.getElementById('fb-list');
  list.innerHTML = '<div class="fb-empty">Loading…</div>';

  let data;
  try {
    const res = await fetch(`/api/fs/list?path=${encodeURIComponent(path)}`);
    if (!res.ok) throw new Error(await res.text());
    data = await res.json();
  } catch (_) {
    list.innerHTML = '<div class="fb-error">Could not read this folder</div>';
    return;
  }

  // Breadcrumb — show current path reversed so deepest segment stays visible
  const crumb = document.getElementById('fb-breadcrumb');
  const crumbSpan = document.createElement('span');
  crumbSpan.textContent = data.path || '/';
  crumb.innerHTML = '';
  crumb.appendChild(crumbSpan);

  // Up button
  const upBtn = document.getElementById('fb-up-btn');
  upBtn.disabled = !data.parent;
  upBtn._fbParent = data.parent || null;

  // Render entries
  list.innerHTML = '';
  if (!data.entries || data.entries.length === 0) {
    list.innerHTML = '<div class="fb-empty">Empty folder</div>';
    return;
  }

  data.entries.forEach(entry => {
    const cls  = entry.is_dir ? 'fb-dir' : entry.is_audio ? 'fb-audio' : 'fb-file';
    const item = document.createElement('div');
    item.className  = `fb-item ${cls}`;
    item.draggable  = true;
    item.dataset.path = entry.path;

    const img = document.createElement('img');
    img.alt = '';
    img.src = entry.is_dir   ? '/static/icon-rb-folder.png'
            : entry.is_audio ? '/static/icon-track.png'
            :                  '/static/icon-rb-file.png';
    img.onerror = () => { img.onerror = null; img.src = '/static/icon-rb-file.png'; };

    const nameEl = document.createElement('span');
    nameEl.className = 'fb-item-name';
    nameEl.textContent = entry.name;
    nameEl.title = entry.name;

    item.appendChild(img);
    item.appendChild(nameEl);

    // Navigate into folders on click
    if (entry.is_dir) {
      item.addEventListener('click', (e) => {
        e.stopPropagation();
        fbNavigateTo(entry.path);
      });
    }

    // Drag — Strategy 3 (text/plain) is picked up by all existing drop zones
    item.addEventListener('dragstart', e => {
      e.dataTransfer.effectAllowed = 'copy';
      e.dataTransfer.setData('text/plain', entry.path);
      item.classList.add('fb-dragging');
    });
    item.addEventListener('dragend', () => item.classList.remove('fb-dragging'));

    list.appendChild(item);
  });
}

function fbUp() {
  const btn = document.getElementById('fb-up-btn');
  if (btn._fbParent) fbNavigateTo(btn._fbParent);
}

function fbHome() { fbNavigateTo('/Volumes'); }

/* ── Step completion report modal ─────────────────────────────────────────── */
// Session-only storage — cleared when the page reloads / server stops.
const sessionReports = {};

const STEP_PILL_LABELS = {
  'Audit — Library Health Check':                 'Step 1 Summary',
  'Audit — Database + Physical Scan':             'Step 1 Summary',
  'Tag Tracks — BPM & Key Detection':             'Step 2 Summary',
  'Preview Import — Dry Run':                     'Step 3 Preview',
  'Import — Writing Tracks to Database':          'Step 3 Summary',
  'Link Playlists — Matching Tracks to Folders':  'Step 4 Summary',
  'Normalize — Loudness to −8.0 LUFS':            'Step 5 Summary',
  'Find Duplicates — Acoustic Fingerprinting':    'Duplicates Summary',
  'Relocate — Updating File Paths in Database':   'Relocate Summary',
};

function _pillLabel(title) {
  if (STEP_PILL_LABELS[title]) return STEP_PILL_LABELS[title];
  // Dynamic labels: Organize, Convert, Novelty, Pipeline
  if (title.startsWith('Organize —'))      return 'Organize Summary';
  if (title.startsWith('Converting '))     return 'Convert Summary';
  if (title.startsWith('Novelty Scan —'))  return 'Novelty Summary';
  if (title.startsWith('Prune —'))         return 'Prune Summary';
  if (title.startsWith('Running Pipeline') || title.startsWith('Pipeline —')) return 'Pipeline Summary';
  if (title.startsWith('Homebrew —'))      return 'Brew Update';
  // Generic fallback
  return title.split(' — ')[0] + ' Summary';
}

function _addOrUpdateSummaryPill(title, animate) {
  const label     = _pillLabel(title);
  const container = document.getElementById('session-pills-container');
  if (!container) return;
  const existing = [...container.querySelectorAll('[data-pill-title]')]
    .find(el => el.dataset.pillTitle === title);
  if (existing) return;
  const pill = document.createElement('button');
  pill.className        = 'summary-pill';
  pill.dataset.pillTitle = title;
  pill.title            = 'Re-open summary: ' + label;
  pill.innerHTML        = `<span class="summary-pill-icon">📋</span>${label}`;
  pill.addEventListener('click', () => {
    const r = sessionReports[title];
    if (r) openReportModal(title, r.text, r.reportPath);
  });
  container.appendChild(pill);
}

/* ── Modal animation helpers ───────────────────────────────────────────────
   _sbAnim(el, keyframe, dur, cb) — runs keyframe then fires cb.
   _sbFadeBd(id, show, cb) — fades backdrop in/out.
   pulseModal kept as no-op so existing call-sites don't break.            */
function _sbAnim(el, kf, dur, cb) {
  if (!el) { if (cb) cb(); return; }
  el.style.animation = kf + ' ' + dur + ' cubic-bezier(.16,1,.3,1) forwards';
  el.addEventListener('animationend', () => { el.style.animation=''; if (cb) cb(); }, {once:true});
}
function _sbFadeBd(id, show, cb) {
  const bd = document.getElementById(id);
  if (!bd) { if (cb) cb(); return; }
  if (show) { bd.classList.remove('hidden'); _sbAnim(bd, 'sb-backdrop-in', '.2s', cb); }
  else      { _sbAnim(bd, 'sb-backdrop-out', '.18s', () => { bd.classList.add('hidden'); if (cb) cb(); }); }
}
function pulseModal(el) { /* no-op — animations now use _sbAnim */ }


function openReportModal(title, text, reportPath) {
  document.getElementById('rmod-title').textContent = title + ' — Complete';
  const pathEl = document.getElementById('rmod-save-path');
  if (reportPath) {
    pathEl.textContent = '▸ Report saved to:  ' + reportPath;
    pathEl.style.display = '';
  } else {
    pathEl.textContent = '▸ Session summary only — no file saved for this step.';
    pathEl.style.display = '';
  }
  document.getElementById('rmod-pre').textContent = text;
  sessionReports[title] = { text, reportPath, ts: Date.now() };
  _sbFadeBd('report-modal-backdrop', true);
  const box = document.getElementById('report-modal');
  void box.offsetWidth;
  _sbAnim(box, 'sb-modal-in', '.28s');
}

function closeReportModal(shrinkToPill) {
  const box   = document.getElementById('report-modal');
  const title = (document.getElementById('rmod-title')?.textContent||'').replace(' — Complete','');
  if (shrinkToPill) {
    _sbAnim(box, 'sb-modal-shrink', '.32s', () => {
      _sbFadeBd('report-modal-backdrop', false);
      _addOrUpdateSummaryPill(title, true);
    });
  } else {
    _sbAnim(box, 'sb-modal-out', '.18s', () => {
      _sbFadeBd('report-modal-backdrop', false);
      _addOrUpdateSummaryPill(title);
    });
  }
}

// Escape key handled in the global keydown listener below

/* ── Status polling ────────────────────────────────────────────────────────── */
/* ── Settings modal ────────────────────────────────────────────────────────── */
function openSettings() {
  // Load current config into the form
  fetch('/api/config').then(r => r.json()).then(cfg => {
    const mode = cfg.archive_mode || 'auto';
    document.querySelector(`input[name="archive-mode"][value="${mode}"]`).checked = true;
    document.getElementById('settings-custom-input').value = cfg.custom_archive || '';
    const excluded = Array.isArray(cfg.excluded_dirs) ? cfg.excluded_dirs : [];
    document.getElementById('settings-excluded-dirs').value = excluded.join('\n');
    _settingsUpdateUI(mode);
  }).catch(() => {
    document.querySelector('input[name="archive-mode"][value="auto"]').checked = true;
    _settingsUpdateUI('auto');
  });
  _sbFadeBd('settings-backdrop', true);
  const _smb = document.getElementById('settings-modal');
  void _smb.offsetWidth; _sbAnim(_smb, 'sb-modal-in', '.28s');
}
function closeSettings() {
  _sbAnim(document.getElementById('settings-modal'), 'sb-modal-out', '.18s', () => {
    _sbFadeBd('settings-backdrop', false);
  });
}
function _settingsUpdateUI(mode) {
  document.getElementById('settings-custom-path').style.display  = mode === 'custom' ? 'block' : 'none';
  document.getElementById('settings-warnings').style.display     = mode === 'none'   ? 'block' : 'none';
}
document.addEventListener('change', e => {
  if (e.target.name === 'archive-mode') _settingsUpdateUI(e.target.value);
});
async function saveSettings() {
  const mode   = document.querySelector('input[name="archive-mode"]:checked')?.value || 'auto';
  const custom = document.getElementById('settings-custom-input').value.trim();
  if (mode === 'custom' && !custom) {
    alert('Please enter a folder path for the custom archive location.');
    return;
  }
  const btn = document.querySelector('.settings-save');
  btn.textContent = 'Saving…'; btn.disabled = true;
  try {
    const res  = await fetch('/api/settings', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        archive_mode: mode,
        custom_archive_dir: custom,
        excluded_dirs: document.getElementById('settings-excluded-dirs').value
          .split('\n').map(s => s.trim()).filter(Boolean),
      }),
    });
    const data = await res.json();
    if (data.ok) {
      closeSettings();
      // Restart the server to apply new config
      await fetch('/api/quit', { method: 'POST' }).catch(() => {});
      setTimeout(() => window.close(), 500);
    } else {
      alert('Save failed: ' + (data.error || 'unknown error'));
    }
  } catch(e) {
    alert('Could not save settings.');
  }
  btn.textContent = 'Save & Restart'; btn.disabled = false;
}

/* ── Welcome wizard ─────────────────────────────────────────────────────────
   Permission keys: rekitbox-db-read / rekitbox-db-write = 'granted'|'denied'
   Setup gate:      rekitbox-setup-complete = '1'                             */

let _wReadGranted  = false;
let _wWriteGranted = false;

function welcomeShowStep(id) {
  document.querySelectorAll('.welcome-step').forEach(s => s.classList.remove('active'));
  const el = document.getElementById('wstep-' + id);
  if (el) el.classList.add('active');
}

function openWelcome() {
  _wReadGranted  = localStorage.getItem('rekitbox-db-read')  === 'granted';
  _wWriteGranted = localStorage.getItem('rekitbox-db-write') === 'granted';
  // Returning users land on the read step so they can adjust permissions
  welcomeShowStep(localStorage.getItem('rekitbox-setup-complete') ? 'read' : 'intro');
  _sbFadeBd('welcome-backdrop', true);
  const modal = document.getElementById('welcome-modal');
  void modal.offsetWidth; _sbAnim(modal, 'sb-modal-in', '.28s');
}

function closeWelcome() {
  _sbAnim(document.getElementById('welcome-modal'), 'sb-modal-out', '.18s', () => {
    _sbFadeBd('welcome-backdrop', false);
  });
}

function welcomeGrantRead() {
  _wReadGranted = true;
  welcomeShowStep('write');
}
function welcomeDenyRead() {
  _wReadGranted  = false;
  _wWriteGranted = false;
  _welcomeShowReady();
}
function welcomeGrantWrite() {
  _wWriteGranted = true;
  _welcomeShowReady();
}
function welcomeDenyWrite() {
  _wWriteGranted = false;
  _welcomeShowReady();
}

function _welcomeShowReady() {
  const body = document.getElementById('wstep-ready-body');
  if (_wReadGranted && _wWriteGranted) {
    body.innerHTML =
      `<p class="welcome-step-title">You're all set.</p>
       <p class="welcome-step-sub">We'll kick off a quick library audit automatically — it's read-only and maps where Rekordbox thinks everything is. It runs silently in the background. When it's done you'll land on Tag Tracks, and the tools will have data to work with.</p>
       <p class="welcome-step-sub" style="color:var(--safe)">✓ Full access — all tools enabled.</p>`;
  } else if (_wReadGranted) {
    body.innerHTML =
      `<p class="welcome-step-title">Read-only mode.</p>
       <p class="welcome-step-sub">We'll run a quick library audit to map your library. Available: Library Audit, Tag Tracks, Find Duplicates, Normalize, Convert, Organize, Novelty Scanner, Pipeline Builder.</p>
       <p class="welcome-step-sub" style="color:var(--caution)">⚠ Write tools are locked: Fix Broken Paths, Import, Link Playlists, Prune. Enable them anytime via the lightbulb icon.</p>`;
  } else {
    body.innerHTML =
      `<p class="welcome-step-title">Limited mode.</p>
       <p class="welcome-step-sub">Database tools aren't available. These work without database access: Tag Tracks (file analysis), Find Duplicates (folder scan), Normalize, Convert, Organize, Novelty Scanner.</p>
       <p class="welcome-step-sub" style="color:var(--text-dim)">Enable database access anytime via the lightbulb icon in the bottom-right corner.</p>`;
  }
  welcomeShowStep('ready');
}

async function completeSetup() {
  const readVal  = _wReadGranted  ? 'granted' : 'denied';
  const writeVal = _wWriteGranted ? 'granted' : 'denied';
  // Mirror to localStorage as fast cache, but truth lives server-side
  localStorage.setItem('rekitbox-db-read',        readVal);
  localStorage.setItem('rekitbox-db-write',       writeVal);
  localStorage.setItem('rekitbox-setup-complete', '1');
  if (_wWriteGranted) {
    localStorage.setItem('rekitbox-archive-permission', 'granted');
    fetch('/api/setup-archive', { method: 'POST' }).catch(() => {});
  }
  // Persist to ~/.rekordbox-toolkit/rekitbox-state.json so it survives
  // across pywebview sessions even if WKWebView clears localStorage
  await fetch('/api/setup-complete', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ db_read: readVal, db_write: writeVal }),
  }).catch(() => {});
  applyPermissions();
  closeWelcome();
  if (_wReadGranted) {
    setTimeout(runSilentAudit, 700);
  } else {
    setTimeout(() => document.getElementById('step-process')
      ?.scrollIntoView({ behavior: 'smooth', block: 'start' }), 500);
  }
}

/* ── Permission application ──────────────────────────────────────────────── */
function applyPermissions() {
  const readOk  = localStorage.getItem('rekitbox-db-read')  === 'granted';
  const writeOk = localStorage.getItem('rekitbox-db-write') === 'granted';
  // Main cards that require write permission
  ['step-duplicates'].forEach(id =>
    document.getElementById(id)?.classList.toggle('permission-locked', !writeOk));
  // Rail buttons that require write permission
  ['rail-btn-relocate','rail-btn-import','rail-btn-link'].forEach(id => {
    const btn = document.getElementById(id);
    if (!btn) return;
    btn.classList.toggle('permission-locked', !writeOk);
    btn.disabled = !writeOk;
  });
  // Audit rail button requires read permission
  const auditBtn = document.getElementById('rail-btn-audit');
  if (auditBtn) {
    auditBtn.classList.toggle('permission-locked', !readOk);
    auditBtn.disabled = !readOk;
  }
}

/* Clicking a locked card reopens the wizard at the relevant step */
document.addEventListener('click', e => {
  const card = e.target.closest('.card.permission-locked');
  if (!card) return;
  e.stopPropagation();
  _wReadGranted  = localStorage.getItem('rekitbox-db-read')  === 'granted';
  _wWriteGranted = localStorage.getItem('rekitbox-db-write') === 'granted';
  const needsWrite = ['rail-btn-relocate','rail-btn-import','rail-btn-link','step-duplicates'].includes(card.id);
  openWelcome();
  welcomeShowStep(needsWrite ? 'write' : 'read');
}, true);

/* ── Silent background audit ─────────────────────────────────────────────── */
function runSilentAudit() {
  fetch('/api/run/audit')
    .then(r => {
      const reader  = r.body.getReader();
      const decoder = new TextDecoder();
      let buf = '';
      function pump() {
        return reader.read().then(({ done, value }) => {
          if (done) { _onSilentAuditDone(true); return; }
          buf += decoder.decode(value, { stream: true });
          if (buf.includes('[DONE]'))  { reader.cancel(); _onSilentAuditDone(true);  return; }
          if (buf.includes('[ERROR]')) { reader.cancel(); _onSilentAuditDone(false); return; }
          return pump();
        });
      }
      return pump();
    })
    .catch(() => _onSilentAuditDone(false));
}

function _onSilentAuditDone(ok) {
  showToast(ok ? 'Library audit complete ✓' : 'Audit skipped — check Settings for music drive', ok ? 'success' : 'neutral');
  refreshStatus();
  setTimeout(() => document.getElementById('step-process')
    ?.scrollIntoView({ behavior: 'smooth', block: 'start' }), 900);
}

/* ── Toast notification ──────────────────────────────────────────────────── */
function showToast(message, type = 'neutral') {
  const t = document.createElement('div');
  t.className = `sb-toast toast-${type}`;
  t.textContent = message;
  document.body.appendChild(t);
  t.addEventListener('animationend', e => { if (e.animationName === 'sb-toast-out') t.remove(); });
}

async function interruptScan() {
  if (!isRunning) return;
  if (!confirm('Ask the scan to stop gracefully?\n\nThe process will receive a stop signal and try to clean up. The server will remain running.')) return;
  const btn = document.getElementById('scan-bar-interrupt');
  btn.textContent = '⏸ Stopping…'; btn.disabled = true;
  try {
    await fetch('/api/cancel', { method: 'POST' });
    appendLog('⏸ Interrupt signal sent — waiting for process to exit…', 'warn');
  } catch(e) {
    appendLog('[ERROR] Could not send interrupt signal.', 'error');
    btn.textContent = '⏸ Interrupt'; btn.disabled = false;
  }
}

async function emergencyStop() {
  if (!isRunning) return;
  if (!confirm('Force-kill the scan immediately?\n\nThis will hard-terminate the process. Any in-progress writes may be incomplete.\n\nThe server will remain running.')) return;
  const btn = document.getElementById('scan-bar-emergency');
  btn.textContent = '⚡ Killing…'; btn.disabled = true;
  try {
    await fetch('/api/cancel/force', { method: 'POST' });
    appendLog('⚡ Emergency stop — process force-killed. Server is still running.', 'error');
  } catch(e) {
    appendLog('[ERROR] Could not send kill signal.', 'error');
    btn.textContent = '⚡ Emergency Stop'; btn.disabled = false;
  }
}

/* ── Homebrew update banner ─────────────────────────────────────────────────── */

let _brewDismissed = false;

async function brewCheckStatus() {
  try {
    const res = await fetch('/api/brew/status');
    if (!res.ok) return;
    const data = await res.json();
    _brewRender(data);
  } catch (_) {}
}

async function brewCheckNow() {
  document.getElementById('brew-msg').textContent = 'Checking for Homebrew updates…';
  document.getElementById('brew-banner').style.display = 'flex';
  _brewDismissed = false;
  try {
    const res = await fetch('/api/brew/check', { method: 'POST' });
    const data = await res.json();
    _brewRender(data);
  } catch (e) {
    document.getElementById('brew-msg').textContent = 'Could not reach brew — check manually.';
  }
}

function _brewRender(data) {
  const banner   = document.getElementById('brew-banner');
  const msgEl    = document.getElementById('brew-msg');
  if (_brewDismissed) return;
  const outdated = data.outdated || [];
  if (!outdated.length) {
    banner.style.display = 'none';
    return;
  }
  const list = outdated.map(p =>
    `<strong>${p.name}</strong> ${p.installed} → ${p.current}`
  ).join(' &nbsp;·&nbsp; ');
  msgEl.innerHTML = `Homebrew updates available for SuperBox packages: ${list}`;
  banner.style.display = 'flex';
}

function brewDismiss() {
  _brewDismissed = true;
  document.getElementById('brew-banner').style.display = 'none';
}

/* ── RekitBox update checker ────────────────────────────────────────────────── */
let _rkbUpdateData = null;   // populated when update found; used by modal buttons

async function rekitboxUpdateCheck() {
  try {
    const res = await fetch('/api/update/status');
    if (!res.ok) return;
    const data = await res.json();
    // Silently do nothing if no update or no connection
    if (!data.update_available) return;
    _rkbUpdateData = data;
    _rkbShowUpdateModal(data);
  } catch (_) {
    // No internet / server unreachable — silent, keep running current version
  }
}

function _rkbShowUpdateModal(data) {
  const latest  = data.latest_version || 'a newer version';
  const current = data.current_version;
  const overlay = document.getElementById('rkb-update-overlay');
  const title   = document.getElementById('rkb-update-title');
  const body    = document.getElementById('rkb-update-body');
  const goBtn   = document.getElementById('rkb-update-go');

  title.textContent = current
    ? `RekitBox ${latest} is available`
    : 'RekitBox update available';

  if (data.is_git_install) {
    body.textContent = current
      ? `You're running ${current}. RekitBox will pull ${latest} and restart itself — takes about 10 seconds. Your library and settings are untouched.`
      : `A newer version is available. RekitBox will pull it and restart itself — takes about 10 seconds.`;
    goBtn.textContent = 'Update now';
  } else {
    const dlUrl = data.download_url || data.release_url || '#';
    body.textContent = current
      ? `You're running ${current}. Download ${latest}, replace your current RekitBox.app, and relaunch.`
      : `A newer version is available. Download it, replace your current RekitBox.app, and relaunch.`;
    goBtn.textContent = 'Download RekitBox.zip';
    goBtn.dataset.dlUrl = dlUrl;
  }

  overlay.style.display = 'flex';
}

async function rkbUpdateGo() {
  const data = _rkbUpdateData;
  if (!data) return;

  if (!data.is_git_install) {
    // ZIP install — open download in new tab, hide modal, leave banner reminder
    document.getElementById('rkb-update-overlay').style.display = 'none';
    const url = document.getElementById('rkb-update-go').dataset.dlUrl;
    if (url && url !== '#') window.open(url, '_blank', 'noopener');
    _rkbShowBanner(data);
    return;
  }

  // Git install — pull + restart in place
  const body     = document.getElementById('rkb-update-body');
  const goBtn    = document.getElementById('rkb-update-go');
  const skipBtn  = document.getElementById('rkb-update-skip');
  const titleEl  = document.getElementById('rkb-update-title');

  goBtn.disabled   = true;
  skipBtn.disabled = true;
  goBtn.style.opacity   = '0.5';
  skipBtn.style.opacity = '0.5';
  goBtn.textContent = 'Updating…';
  body.textContent  = 'Pulling the latest release from GitHub…';

  let resp;
  try {
    resp = await fetch('/api/update/apply', { method: 'POST' });
  } catch (e) {
    _rkbShowUpdateError('Could not reach the server to start the update.');
    return;
  }

  let payload;
  try { payload = await resp.json(); } catch (_) { payload = null; }

  if (!resp.ok || !payload || !payload.ok) {
    const err = (payload && payload.error) || `Update failed (HTTP ${resp.status}).`;
    _rkbShowUpdateError(err);
    return;
  }

  // Server pulled successfully and is now shutting itself down.
  titleEl.textContent = 'Restarting RekitBox…';
  body.innerHTML =
    '<span style="display:inline-block;width:14px;height:14px;border:2px solid rgba(196,181,253,0.3);'
    + 'border-top-color:#c4b5fd;border-radius:50%;animation:spin .7s linear infinite;margin-right:10px;'
    + 'vertical-align:middle;"></span>'
    + 'Waiting for the server to come back online. The page will reload automatically.';
  goBtn.style.display = 'none';
  skipBtn.style.display = 'none';

  _rkbWaitForServerThenReload();
}

// Poll /api/update/status until the server responds again, then reload.
// Gap timeline: SIGTERM ~0.7s, port free ~0.2s, helper sleep 2s, Flask boot
// a few seconds — typical total 4-8s. Give up after ~60s.
async function _rkbWaitForServerThenReload() {
  // Initial grace so we don't race the old process that's still shutting down
  await new Promise(r => setTimeout(r, 1500));

  const started = Date.now();
  while (Date.now() - started < 60000) {
    try {
      const ctrl = new AbortController();
      const t    = setTimeout(() => ctrl.abort(), 1500);
      const res  = await fetch('/api/update/status', { signal: ctrl.signal, cache: 'no-store' });
      clearTimeout(t);
      if (res.ok) {
        // Small buffer so the server finishes initializing other routes too
        await new Promise(r => setTimeout(r, 400));
        window.location.reload();
        return;
      }
    } catch (_) { /* server still down — keep polling */ }
    await new Promise(r => setTimeout(r, 800));
  }

  _rkbShowUpdateError(
    'Server did not come back online after 60 seconds. '
    + 'Try launching RekitBox manually from your dock.'
  );
}

function _rkbShowUpdateError(msg) {
  const body    = document.getElementById('rkb-update-body');
  const goBtn   = document.getElementById('rkb-update-go');
  const skipBtn = document.getElementById('rkb-update-skip');
  const titleEl = document.getElementById('rkb-update-title');

  titleEl.textContent    = 'Update failed';
  body.textContent       = msg;
  goBtn.style.display    = '';
  skipBtn.style.display  = '';
  goBtn.disabled         = false;
  skipBtn.disabled       = false;
  goBtn.style.opacity    = '';
  skipBtn.style.opacity  = '';
  goBtn.textContent      = 'Retry';
  skipBtn.textContent    = 'Close';
}

function rkbUpdateSkip() {
  // Dismiss modal, show the smaller banner as a reminder
  document.getElementById('rkb-update-overlay').style.display = 'none';
  if (_rkbUpdateData) _rkbShowBanner(_rkbUpdateData);
}

function _rkbShowBanner(data) {
  const latest  = data.latest_version || 'a newer version';
  const current = data.current_version;
  const msgEl   = document.getElementById('rekitbox-update-msg');
  const linkEl  = document.getElementById('rekitbox-update-link');

  if (data.is_git_install) {
    msgEl.textContent = current
      ? `RekitBox ${latest} available — close and relaunch to update.`
      : `RekitBox update available — close and relaunch to update.`;
    linkEl.style.display = 'none';
  } else {
    msgEl.textContent = current
      ? `RekitBox ${latest} available (you have ${current}).`
      : `RekitBox update available.`;
    const dlUrl = data.download_url || data.release_url;
    if (dlUrl) {
      linkEl.href = dlUrl;
      linkEl.textContent = 'Download RekitBox.zip';
      linkEl.style.display = '';
    } else {
      linkEl.style.display = 'none';
    }
  }
  document.getElementById('rekitbox-update-banner').style.display = 'flex';
}

function rekitboxUpdateDismiss() {
  document.getElementById('rekitbox-update-banner').style.display = 'none';
}

function runBrewUpgrade() {
  brewDismiss();
  runCommand('/api/run/brew-upgrade', 'Homebrew — Upgrade SuperBox Packages');
}

// Check on page load (non-blocking — banners appear only if updates found)
brewCheckStatus();
// Delay the update check slightly so the brew check fires first
setTimeout(rekitboxUpdateCheck, 1000);

async function quitRekitBox() {
  const msg = isRunning
    ? '⚠️ A scan is still running.\n\nShutting down now will cancel it mid-process. Are you sure?'
    : 'Shut down RekitBox?\n\nThe server will stop and this window will close.';
  if (!confirm(msg)) return;
  const btn = document.getElementById('quit-btn');
  btn.textContent = 'Shutting down…';
  btn.disabled = true;
  try {
    await fetch('/api/quit', { method: 'POST' });
  } catch(_) {}
  // window.close() only works on script-opened tabs — replace the page instead
  setTimeout(() => {
    document.open();
    document.write(
      '<!DOCTYPE html><html><head><meta charset="UTF-8"><title>RekitBox — Stopped</title>'
      + '<style>body{background:#0a0a0a;color:#555;font-family:ui-monospace,monospace;'
      + 'display:flex;align-items:center;justify-content:center;height:100vh;margin:0;'
      + 'flex-direction:column;gap:16px;}p{margin:0;font-size:.9rem;letter-spacing:.04em;}'
      + 'strong{color:#888;}</style></head><body>'
      + '<p><strong>RekitBox has shut down.</strong></p>'
      + '<p>Close this tab or relaunch the app to continue.</p>'
      + '</body></html>'
    );
    document.close();
  }, 500);
}

async function refreshStatus() {
  try {
    const res  = await fetch('/api/status');
    const data = await res.json();
    rbRunning = data.rb_running;

    const dot   = document.getElementById('rb-dot');
    const label = document.getElementById('rb-label');
    if (rbRunning) {
      dot.className = 'dot danger pulse';
      label.textContent = 'RekordBox is OPEN — close before writing';
      label.style.color = 'var(--danger)';
    } else {
      dot.className = 'dot safe';
      label.textContent = 'RekordBox is closed — safe to write';
      label.style.color = 'var(--safe)';
    }

    if (data.backup?.exists) {
      const bp = document.getElementById('backup-pill');
      const bl = document.getElementById('backup-label');
      bp.style.display = 'flex';
      bl.textContent = `Last backup: ${data.backup.age}`;
    }
  } catch (_) {}
}
refreshStatus();
setInterval(refreshStatus, 6000);
// First launch: show permission wizard (mandatory, can't skip).
// Returning users: restore permissions from server-side state file, resume silently.
// Server-side state (/api/setup-status → rekitbox-state.json) is the source of
// truth; localStorage is used as a fast-path cache on top of it.
(async () => {
  try {
    const r = await fetch('/api/setup-status');
    const d = await r.json();
    if (d.setup_complete) {
      // Restore permission values from server into localStorage so applyPermissions works
      if (d.db_read)  localStorage.setItem('rekitbox-db-read',  d.db_read);
      if (d.db_write) localStorage.setItem('rekitbox-db-write', d.db_write);
      localStorage.setItem('rekitbox-setup-complete', '1');
      applyPermissions();
      if (d.db_write === 'granted') {
        localStorage.setItem('rekitbox-archive-permission', 'granted');
        fetch('/api/setup-archive', { method: 'POST' }).catch(() => {});
      }
      // Run silent audit on every launch for returning users
      if (d.db_read === 'granted') setTimeout(runSilentAudit, 700);
    } else {
      openWelcome();
    }
  } catch (_) {
    // Server not yet ready — fall back to localStorage cache
    if (!localStorage.getItem('rekitbox-setup-complete')) {
      openWelcome();
    } else {
      applyPermissions();
      if (localStorage.getItem('rekitbox-archive-permission') === 'granted') {
        fetch('/api/setup-archive', { method: 'POST' }).catch(() => {});
      }
    }
  }
})();

function choosePath(mode) {
  _sbAnim(document.getElementById('path-modal-box'), 'sb-modal-out', '.18s', () => {
    _sbFadeBd('path-backdrop', false);
  });
  if (mode === 'pipeline') {
    openPipelineWizard();
  }
}

/* ── Config prefill + localStorage persistence ─────────────────────────────── */
const LS_PREFIX = 'superbox_path_';

function lsSave(id) {
  const el = document.getElementById(id);
  if (el) localStorage.setItem(LS_PREFIX + id, el.value);
}

function lsLoad(id) {
  return localStorage.getItem(LS_PREFIX + id) || '';
}

async function prefillDefaults() {
  // No fields are auto-filled from the music root — leaving destination inputs
  // blank prevents accidental runs against an unconfigured path.
  // All fields restore from localStorage only (user's own previous entries).
  const rootFields = [];
  const freeFields = ['relocate-new', 'organize-target', 'novelty-dest', 'relocate-old'];

  // Restore any previously saved value for every tracked field first
  [...rootFields, ...freeFields].forEach(id => {
    const saved = lsLoad(id);
    const el = document.getElementById(id);
    if (el && saved) el.value = saved;
  });

  // Then fill blanks from server config
  try {
    const res    = await fetch('/api/config');
    const config = await res.json();
    const root   = config.music_root;
    if (root) {
      _libraryRoot = root;
    }
  } catch (_) {}

  // Save to localStorage whenever the user edits any remaining path field
  [...rootFields, ...freeFields].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.addEventListener('change', () => lsSave(id));
  });
}
prefillDefaults();

/* ── Dead root detection ────────────────────────────────────────────────────── */
async function checkDeadRoots() {
  try {
    const res = await fetch('/api/audit/path-roots');
    const data = await res.json();
    if (data.has_dead_roots) {
      showDeadRootsBanner(data.dead_roots);
      prefillRelocate(data.dead_roots);
    }
  } catch(e) { /* silent — non-critical */ }
}

function showDeadRootsBanner(deadRoots) {
  const banner = document.getElementById('dead-roots-banner');
  const detail = document.getElementById('dead-roots-detail');
  if (!banner || !detail) return;
  const lines = Object.entries(deadRoots)
    .map(([root, count]) => `<code style="color:var(--accent)">${root}</code> — ${count.toLocaleString()} tracks unreachable`);
  detail.innerHTML = lines.join('<br>');
  banner.style.display = 'block';
}

function prefillRelocate(deadRoots) {
  // Add each dead root as a pill in the relocate-old-pills zone
  const sorted = Object.entries(deadRoots).sort((a,b) => b[1]-a[1]);
  if (!sorted.length) return;
  const existing = getFolderPaths('relocate-old-pills');
  sorted.forEach(([oldRoot]) => {
    if (!existing.includes(oldRoot)) addFolderPill('relocate-old-pills', oldRoot);
  });
}

checkDeadRoots();

/* ── Workflow rail scroll ───────────────────────────────────────────────────── */
document.querySelectorAll('.step-tab').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.step-tab').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    const target = document.getElementById(btn.dataset.target);
    if (target) target.scrollIntoView({ behavior: 'smooth', block: 'start' });
  });
});

/* ── Scan bar ──────────────────────────────────────────────────────────────── */
let scanWarnings = 0;

function showScanBar(title) {
  scanWarnings = 0;
  // Reset interrupt/emergency buttons for fresh use
  const ib = document.getElementById('scan-bar-interrupt');
  ib.textContent = '⏸ Interrupt'; ib.disabled = false; ib.style.display = 'inline-block';
  const eb = document.getElementById('scan-bar-emergency');
  eb.textContent = '⚡ Emergency Stop'; eb.disabled = false; eb.style.display = 'inline-block';
  document.getElementById('sb-remaining').textContent    = '—';
  document.getElementById('sb-clean').textContent        = '0';
  document.getElementById('sb-edited').textContent       = '0';
  document.getElementById('sb-errors').textContent       = '0';
  document.getElementById('sb-warnings').textContent     = '0';
  document.getElementById('sb-quarantined').textContent  = '0';
  document.getElementById('sb-quarantine-wrap').style.display = 'none';
  document.getElementById('scan-bar-title').textContent = title;
  document.getElementById('scan-bar-spinner').classList.add('active');
  document.getElementById('scan-bar-dismiss').style.display = 'none';
  document.getElementById('scan-bar').classList.add('active');
  document.body.classList.add('scan-active');
}
function updateScanBar(p) {
  document.getElementById('sb-remaining').textContent = p.remaining.toLocaleString();
  document.getElementById('sb-clean').textContent     = p.clean.toLocaleString();
  document.getElementById('sb-edited').textContent    = p.edited.toLocaleString();
  document.getElementById('sb-errors').textContent    = p.errors.toLocaleString();
  document.getElementById('sb-warnings').textContent  = scanWarnings.toLocaleString();
  if (p.quarantined > 0) {
    document.getElementById('sb-quarantined').textContent = p.quarantined.toLocaleString();
    document.getElementById('sb-quarantine-wrap').style.display = '';
  }
}
function finishScanBar() {
  document.getElementById('scan-bar-spinner').classList.remove('active');
  document.getElementById('scan-bar-interrupt').style.display = 'none';
  document.getElementById('scan-bar-emergency').style.display = 'none';
  document.getElementById('scan-bar-dismiss').style.display = 'inline-block';
}
function dismissScanBar() {
  document.getElementById('scan-bar').classList.remove('active');
  document.body.classList.remove('scan-active');
}

/* ── Log panel ─────────────────────────────────────────────────────────────── */
function openLog(title) {
  document.getElementById('log-panel').classList.add('open');
  document.body.classList.add('log-open');
  document.getElementById('log-cmd-label').textContent = title;
  document.getElementById('log-output').innerHTML = '';
  document.getElementById('view-output-btn').style.display = 'none';
}
function closeLog() {
  document.getElementById('log-panel').classList.remove('open');
  document.body.classList.remove('log-open');
  if (document.getElementById('log-output').children.length > 0) {
    document.getElementById('view-output-btn').style.display = 'inline-block';
  }
}
function reopenLog() {
  document.getElementById('log-panel').classList.add('open');
  document.body.classList.add('log-open');
  document.getElementById('view-output-btn').style.display = 'none';
}
function setSpinner(on) {
  document.getElementById('log-spinner').classList.toggle('active', on);
}
const LOG_MAX_LINES = 800;
let _logScrollPending = false;
function appendLog(text, cls = '') {
  const out  = document.getElementById('log-output');
  const line = document.createElement('div');
  line.className = 'log-line ' + cls;
  line.textContent = text;
  out.appendChild(line);
  // Trim oldest lines to keep DOM size bounded (prevents browser freeze on large scans)
  while (out.children.length > LOG_MAX_LINES) {
    out.removeChild(out.firstChild);
  }
  // Debounce scroll via rAF — avoids forced reflow on every line
  if (!_logScrollPending) {
    _logScrollPending = true;
    requestAnimationFrame(() => {
      out.scrollTop = out.scrollHeight;
      _logScrollPending = false;
    });
  }
}
function classifyLine(text) {
  const t = text.toLowerCase();
  if (/^═+/.test(text) || /^─+/.test(text)) return 'header';
  if (t.includes('error') || t.includes('failed') || t.includes('exception')) return 'error';
  if (t.includes('warning') || t.includes('warn')) return 'warn';
  if (t.includes('✓') || t.includes('success') || t.includes('complete') || t.includes('ok')) return 'success';
  if (t.startsWith('  ')) return 'dim';
  return 'normal';
}

/* ── Log helpers ───────────────────────────────────────────────────────────── */
function initLog(title) {
  // Prepare the log buffer without showing the panel — scan bar is primary UI
  document.getElementById('log-cmd-label').textContent = title;
  document.getElementById('log-output').innerHTML = '';
  document.getElementById('view-output-btn').style.display = 'none';
}
function toggleLog() {
  const panel = document.getElementById('log-panel');
  if (panel.classList.contains('open')) {
    panel.classList.remove('open');
    document.body.classList.remove('log-open');
  } else {
    panel.classList.add('open');
    document.body.classList.add('log-open');
    document.getElementById('view-output-btn').style.display = 'none';
  }
}

/* ── SSE runner ────────────────────────────────────────────────────────────── */
function runCommand(url, logTitle, onDone, useBar = true, showPrefilter = false) {
  if (isRunning) return;
  initLog(logTitle);
  showScanBar(logTitle);
  isRunning = true;
  setSpinner(true);
  setAllButtons(true);
  appendLog(`▸ ${logTitle}`, 'dim');
  appendLog('', 'dim');

  // Report block capture — delimited by REKITBOX_REPORT_BEGIN / REKITBOX_REPORT_END
  let reportBuffer = [];
  let inReport = false;
  let capturedReportPath = null;   // set by REKITBOX_REPORT_PATH: line

  activeSource = new EventSource(url);

  activeSource.onmessage = (e) => {
    const data = JSON.parse(e.data);
    if (data.line !== undefined) {
      const line = data.line;

      // Detect report block boundaries
      if (line === 'REKITBOX_REPORT_BEGIN') { inReport = true; reportBuffer = []; return; }
      if (line === 'REKITBOX_REPORT_END')   { inReport = false; return; }
      if (inReport) reportBuffer.push(line);

      // Machine-readable report path — capture silently, don't echo to log
      if (line.startsWith('REKITBOX_REPORT_PATH: ')) {
        capturedReportPath = line.slice(22).trim();
        return;
      }
      // Physical scan JSON path — show subtle note in log
      if (line.startsWith('REKITBOX_PHYSICAL_SCAN: ')) {
        const physPath = line.slice(24).trim();
        appendLog(`  📁 Physical scan saved → ${physPath}`);
        return;
      }
      // Structured progress — update scan bar, don't echo to log
      if (line.startsWith('REKITBOX_PROGRESS: ')) {
        if (useBar) {
          try { updateScanBar(JSON.parse(line.slice(19))); } catch(_) {}
        }
        return;
      }
      // Pre-filter summary — show in log as info line
      if (line.startsWith('REKITBOX_PREFILTER: ')) {
        if (showPrefilter) {
          try {
            const pf = JSON.parse(line.slice(20));
            const hasIndex = pf.db_tracks > 0 || pf.scan_tracks > 0;
            if (hasIndex) {
              appendLog(`Index: ${(pf.db_tracks||0).toLocaleString()} tracks from DB + ${(pf.scan_tracks||0).toLocaleString()} from scan index`, 'dim');
            }
            if (pf.skipped > 0) {
              appendLog(`Pre-filter: ${pf.candidates.toLocaleString()} of ${pf.total.toLocaleString()} need fingerprinting — ${pf.skipped.toLocaleString()} skipped (no matching key+BPM+duration)`, 'success');
            } else {
              appendLog(`Pre-filter: all ${pf.total.toLocaleString()} files queued — no index yet. Run Audit + Tag Tracks first to reduce this.`, 'warn');
            }
            if (pf.cached > 0) {
              appendLog(`Cache: ${pf.cached.toLocaleString()} fingerprints reused — only ${pf.to_compute.toLocaleString()} files need fpcalc`, 'success');
            } else {
              appendLog(`Cache: empty — all ${pf.to_compute.toLocaleString()} fingerprints will be computed fresh (subsequent runs will be much faster)`, 'dim');
            }
          } catch(_) {}
        }
        return;
      }
      // Count warnings for scan bar
      const t = line.toLowerCase();
      if (useBar && (t.includes('warning') || t.includes('warn'))) {
        scanWarnings++;
        document.getElementById('sb-warnings').textContent = scanWarnings.toLocaleString();
      }
      appendLog(line, classifyLine(line));
    }
    if (data.done) {
      activeSource.close();
      activeSource = null;
      isRunning = false;
      setSpinner(false);
      setAllButtons(false);
      if (useBar) finishScanBar();
      appendLog('', '');
      if (data.exit_code === 0) {
        appendLog('✓ Finished successfully', 'log-exit-ok');
      } else {
        appendLog(`✗ Exited with code ${data.exit_code}`, 'log-exit-fail');
      }
      // On success: auto-open report modal (user dismisses to pill).
      // On failure: store silently as pill.
      if (reportBuffer.length > 0) {
        const reportText = reportBuffer.join('\n');
        if (data.exit_code === 0) {
          openReportModal(logTitle, reportText, capturedReportPath);
        } else {
          sessionReports[logTitle] = { text: reportText, reportPath: capturedReportPath };
          _addOrUpdateSummaryPill(logTitle);
        }
      }
      if (onDone) onDone(data.exit_code);
    }
  };

  activeSource.onerror = () => {
    activeSource.close();
    activeSource = null;
    isRunning = false;
    setSpinner(false);
    setAllButtons(false);
    if (useBar) finishScanBar();
    appendLog('Connection error — check the server is running.', 'error');
    refreshStatus();
  };
}

/* ── Block helper ──────────────────────────────────────────────────────────── */
function checkRbBlock(msgId) {
  const msg = document.getElementById(msgId);
  if (rbRunning) {
    if (msg) msg.classList.add('visible');
    return true;
  }
  if (msg) msg.classList.remove('visible');
  return false;
}

/* ── Button disable / enable ───────────────────────────────────────────────── */
function setAllButtons(disabled) {
  document.querySelectorAll('.btn').forEach(b => b.disabled = disabled);
}

/* ── Individual command runners ────────────────────────────────────────────── */
function runProcess() {
  const paths = getFolderPaths('process-pills');
  if (!paths.length) { alert('Add at least one music folder first.'); return; }
  const p = new URLSearchParams();
  paths.forEach(path => p.append('path', path));
  if (document.getElementById('process-no-bpm').checked)  p.set('no_bpm', '1');
  if (document.getElementById('process-no-key').checked)  p.set('no_key', '1');
  if (document.getElementById('process-force').checked)   p.set('force',  '1');
  if (document.getElementById('process-enrich-tags')?.checked) p.set('enrich_tags', '1');
  p.set('no_normalize', '1');
  const el = document.getElementById('process-result');
  if (el) el.classList.add('hidden');
  runCommand(`/api/run/process?${p}`, 'Tag Tracks — BPM & Key Detection', null, true, false);
}

function runNormalize() {
  const paths = getFolderPaths('normalize-pills');
  if (!paths.length) { alert('Add at least one music folder first.'); return; }
  const confirmed = confirm(
    'This will rewrite audio files.\n\n' +
    'Originals are renamed .bak during the operation and deleted only after the new file is verified.\n\n' +
    'Make sure you have an independent backup of your drive before proceeding.\n\n' +
    'Continue?'
  );
  if (!confirmed) return;
  const workers = document.getElementById('normalize-workers')?.value || '4';
  const p = new URLSearchParams({ no_bpm: '1', no_key: '1' });
  paths.forEach(path => p.append('path', path));
  if (parseInt(workers) > 1) p.set('workers', workers);
  const el = document.getElementById('normalize-result');
  if (el) el.classList.add('hidden');
  runCommand(`/api/run/process?${p}`, 'Normalize — Loudness to −8.0 LUFS', null, true, false);
}

function runImportDry() {
  const paths = getFolderPaths('import-pills');
  if (!paths.length) { alert('Add at least one music folder first.'); return; }
  const p = new URLSearchParams({ dry_run: '1' });
  paths.forEach(path => p.append('path', path));
  runCommand(`/api/run/import?${p}`, 'Preview Import — Dry Run', null, true, false, null);
}

function runImport() {
  if (checkRbBlock('import-rb-block')) return;
  const paths = getFolderPaths('import-pills');
  if (!paths.length) { alert('Add at least one music folder first.'); return; }
  const p = new URLSearchParams();
  paths.forEach(path => p.append('path', path));
  runCommand(`/api/run/import?${p}`, 'Import — Writing Tracks to Database', null, true);
}

function runLink() {
  if (checkRbBlock('link-rb-block')) return;
  const paths = getFolderPaths('link-pills');
  if (!paths.length) { alert('Add at least one music folder first.'); return; }
  const p = new URLSearchParams();
  paths.forEach(path => p.append('path', path));
  runCommand(`/api/run/link?${p}`, 'Link Playlists — Matching Tracks to Folders', null, true);
}

function runRelocate() {
  if (checkRbBlock('relocate-rb-block')) return;
  const oldPaths = getFolderPaths('relocate-old-pills');
  const new_ = document.getElementById('relocate-new').value.trim();
  if (!oldPaths.length) { alert('Add at least one old path prefix.'); return; }
  if (!new_) { alert('Enter the new (destination) path.'); return; }
  const p = new URLSearchParams({ new_root: new_ });
  oldPaths.forEach(old => p.append('old_root', old));
  runCommand(`/api/run/relocate?${p}`, 'Relocate — Updating File Paths in Database', null, true);
}

function runDuplicates() {
  const paths = getFolderPaths('dupes-pills');
  if (!paths.length) { alert('Add at least one music folder first.'); return; }
  const p = new URLSearchParams();
  paths.forEach(path => p.append('path', path));
  const workers = document.getElementById('dupes-workers')?.value || '4';
  if (parseInt(workers) > 1) p.set('workers', workers);
  // Match mode
  const matchMode = document.querySelector('input[name="dupes-match-mode"]:checked')?.value || 'exact';
  if (matchMode !== 'exact') p.set('match_mode', matchMode);
  // Fuzzy threshold (only relevant when fuzzy or all)
  if (matchMode === 'fuzzy' || matchMode === 'all') {
    const thresholdPct = parseInt(document.getElementById('fuzzy-threshold')?.value || '85');
    p.set('fuzzy_threshold', (thresholdPct / 100).toFixed(2));
  }
  const title = 'Find Duplicates — Acoustic Fingerprinting';
  runCommand(`/api/run/duplicates?${p}`, title, (exitCode) => {
    if (exitCode === 0) {
      const rp = sessionReports[title]?.reportPath;
      if (rp) {
        const el = document.getElementById('prune-csv-path');
        if (el) el.value = rp;
        _autoLoadDupeResults(rp);
      }
    }
  }, true, true);
}

// Show/hide fuzzy threshold row based on match mode selection
function _initMatchModeUI() {
  const radios = document.querySelectorAll('input[name="dupes-match-mode"]');
  const row = document.getElementById('fuzzy-threshold-row');
  if (!row) return;
  radios.forEach(r => r.addEventListener('change', () => {
    const val = document.querySelector('input[name="dupes-match-mode"]:checked')?.value;
    row.style.display = (val === 'fuzzy' || val === 'all') ? 'block' : 'none';
  }));
}
document.addEventListener('DOMContentLoaded', _initMatchModeUI);

function runConvert() {
  const paths = getFolderPaths('convert-pills');
  const format = document.getElementById('convert-format').value.trim();
  if (!paths.length) { alert('Add at least one folder first.'); return; }
  if (!format) { alert('Select a target format.'); return; }
  const workers = document.getElementById('convert-workers')?.value || '4';
  const p = new URLSearchParams({ format });
  paths.forEach(path => p.append('path', path));
  if (parseInt(workers) > 1) p.set('workers', workers);
  runCommand(`/api/run/convert?${p}`, `Converting Audio Files to ${format.toUpperCase()}`);
}

/* ── Pipeline Builder ──────────────────────────────────────────────────────── */

const PIPE_STEPS = {
  audit:      { name: 'Library Audit',      icon: '/static/icon-audit.png',          desc: 'DB snapshot + physical filesystem inventory' },
  process:    { name: 'Tag Tracks',         icon: '/static/icon-tag.png',            desc: 'Write BPM and Key into each file' },
  duplicates: { name: 'Find Duplicates',    icon: '/static/icon-find-duplicate.png', desc: 'Scan for files that are the same recording' },
  prune:      { name: 'Prune Duplicates',   icon: '/static/icon-prune.png',          desc: 'Remove copies found by Find Duplicates' },
  relocate:   { name: 'Fix Broken Paths',   icon: '/static/icon-move.png',           desc: 'Update RekordBox after files have moved' },
  import:     { name: 'Import Tracks',      icon: '/static/icon-import.png',         desc: 'Add new audio files to RekordBox database' },
  link:       { name: 'Link Playlists',     icon: '/static/icon-link.png',           desc: 'Connect tracks to playlists by folder name' },
  normalize:  { name: 'Balance Loudness',   icon: '/static/icon-normalize.png',      desc: 'Bring every track to the same volume' },
  convert:    { name: 'Convert Format',     icon: '/static/icon-convert.png',        desc: 'Change files to AIFF, MP3, WAV, or FLAC' },
  organize:   { name: 'Organize Library',   icon: '/static/icon-organizer.png',      desc: 'Move files into Artist / Album / Track' },
  novelty:    { name: 'Novelty Scan',       icon: '/static/icon-novelty.png',        desc: 'Copy unique tracks from source to home library' },
};

const RECOMMENDED = ['process','duplicates','prune','relocate','import','link','organize'];

let pipelineSteps = [];   // [{id, type}]
let pipeUid = 0;

/* ══ Pipeline Wizard ═════════════════════════════════════════════════════════ */

function openPipelineWizard() {
  const backdrop = document.getElementById('pipeline-wizard-backdrop');
  backdrop.classList.remove('hidden');
  // Check for a saved checkpoint and offer to resume
  const ckpt   = _loadPipeCheckpoint();
  const banner = document.getElementById('pipe-resume-banner');
  if (ckpt && ckpt.steps && ckpt.steps.length > 0) {
    const nextIdx  = ckpt.completedIdx + 1;
    const nextStep = ckpt.steps[nextIdx];
    const nextName = nextStep ? ((PIPE_STEPS[nextStep.type] || {}).name || nextStep.type) : '—';
    const age      = Math.round((Date.now() - (ckpt.ts || 0)) / 60000);
    const ageText  = age < 1 ? 'just now' : age < 60 ? `${age}m ago` : `${Math.round(age / 60)}h ago`;
    document.getElementById('pipe-resume-text').textContent =
      `Checkpoint — resume at step ${nextIdx + 1} of ${ckpt.steps.length}: "${nextName}"`;
    document.getElementById('pipe-resume-sub').textContent =
      `Saved ${ageText} · ${ckpt.dryRun ? 'Dry Run' : 'Live Run'}`;
    if (banner) banner.classList.remove('hidden');
  } else {
    if (banner) banner.classList.add('hidden');
  }
  // Reset to phase 1
  document.getElementById('pipe-wiz-p1').style.opacity = '1';
  document.getElementById('pipe-wiz-p1').classList.remove('hidden');
  document.getElementById('pipe-wiz-p2').classList.add('hidden');
  document.getElementById('pipeline-wizard').classList.remove('wizard-wide');
  const _pwb = document.getElementById('pipeline-wizard');
  void _pwb.offsetWidth; _sbAnim(_pwb, 'sb-modal-in', '.28s');
}

function closePipelineWizard() {
  document.getElementById('pipeline-wizard-backdrop').classList.add('hidden');
  // Resolve any pending gate promise so it doesn't leak across sessions
  if (_pipeGateResolve) { _pipeGateResolve('stop'); _pipeGateResolve = null; }
}

/* Resume a previously interrupted pipeline run from its saved checkpoint */
function resumeFromCheckpoint() {
  const ckpt = _loadPipeCheckpoint();
  if (!ckpt || !ckpt.steps || ckpt.steps.length === 0) return;
  pipelineSteps = ckpt.steps.map(s => ({
    id: ++pipeUid, type: s.type, _config: s._config || {}, _draftConfig: s._config || {},
  }));
  pipelineRender();
  const banner = document.getElementById('pipe-resume-banner');
  if (banner) banner.classList.add('hidden');
  // Transition straight to Phase 2, focused on the next uncompleted step
  const resumeIdx = Math.min(ckpt.completedIdx + 1, pipelineSteps.length - 1);
  const p1  = document.getElementById('pipe-wiz-p1');
  const p2  = document.getElementById('pipe-wiz-p2');
  const wiz = document.getElementById('pipeline-wizard');
  p1.style.transition = 'opacity .2s';
  p1.style.opacity    = '0';
  setTimeout(() => {
    p1.classList.add('hidden'); p1.style.opacity = ''; p1.style.transition = '';
    wiz.classList.add('wizard-wide');
    pipeWizBuildConfigs();
    p2.classList.remove('hidden');
    p2.style.opacity    = '0';
    p2.style.transition = 'opacity .25s';
    document.getElementById('wiz-dry-run-2').checked       = ckpt.dryRun !== false;
    document.getElementById('wiz-confirm-steps-2').checked = true; // always confirm on resume
    requestAnimationFrame(() => requestAnimationFrame(() => {
      p2.style.opacity = '1';
      setTimeout(() => { p2.style.transition = ''; p2.style.opacity = ''; }, 280);
      pipeWizSelectStep(resumeIdx);
    }));
  }, 220);
}

function discardCheckpoint() {
  _clearPipeCheckpoint();
  const banner = document.getElementById('pipe-resume-banner');
  if (banner) banner.classList.add('hidden');
}

function pipeWizNext() {
  if (pipelineSteps.length === 0) {
    alert('Add at least one step to the pipeline first.');
    return;
  }
  const p1 = document.getElementById('pipe-wiz-p1');
  const p2 = document.getElementById('pipe-wiz-p2');
  const wiz = document.getElementById('pipeline-wizard');

  // Fade out phase 1
  p1.style.transition = 'opacity .2s';
  p1.style.opacity = '0';

  setTimeout(() => {
    p1.classList.add('hidden');
    p1.style.opacity = '';
    p1.style.transition = '';

    // Widen the modal
    wiz.classList.add('wizard-wide');

    // Build and show phase 2
    pipeWizBuildConfigs();
    p2.classList.remove('hidden');
    p2.style.opacity = '0';
    p2.style.transition = 'opacity .25s';

    // Sync checkboxes
    document.getElementById('wiz-dry-run-2').checked   = document.getElementById('wiz-dry-run').checked;
    document.getElementById('wiz-confirm-steps-2').checked = document.getElementById('wiz-confirm-steps').checked;

    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        p2.style.opacity = '1';
        setTimeout(() => { p2.style.transition = ''; p2.style.opacity = ''; }, 280);
      });
    });
  }, 220);
}

function pipeWizBack() {
  const p1 = document.getElementById('pipe-wiz-p1');
  const p2 = document.getElementById('pipe-wiz-p2');
  const wiz = document.getElementById('pipeline-wizard');

  p2.style.transition = 'opacity .2s';
  p2.style.opacity = '0';

  setTimeout(() => {
    p2.classList.add('hidden');
    p2.style.opacity = '';
    p2.style.transition = '';
    wiz.classList.remove('wizard-wide');
    p1.classList.remove('hidden');
    p1.style.opacity = '0';
    p1.style.transition = 'opacity .25s';
    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        p1.style.opacity = '1';
        setTimeout(() => { p1.style.transition = ''; p1.style.opacity = ''; }, 280);
      });
    });
  }, 220);
}

function pipeWizRun() {
  if (pipelineSteps.length === 0) return;

  // Read draft from currently active panel before validating
  const active = pipelineSteps[_wizActiveIndex];
  if (active) _pipeWizReadDraft(active);

  // Validate all steps
  const incomplete = pipelineSteps.filter(s => !_stepIsReady(s));
  if (incomplete.length > 0) {
    // Highlight the first incomplete step in the sidebar
    const firstIdx = pipelineSteps.indexOf(incomplete[0]);
    pipeWizSelectStep(firstIdx);
    // Show tooltip over Run button
    const tip = document.getElementById('wiz-next-tooltip');
    tip.style.display = 'block';
    clearTimeout(tip._hideTimer);
    tip._hideTimer = setTimeout(() => { tip.style.display = 'none'; }, 5000);
    const hide = () => { tip.style.display = 'none'; document.removeEventListener('click', hide); };
    setTimeout(() => document.addEventListener('click', hide), 10);
    return;
  }

  // Commit all draft configs
  pipelineSteps.forEach(s => {
    s._config = s._draftConfig || {};
    _savePipeCfg(s.type, s._config);
  });

  const dryRun      = document.getElementById('wiz-dry-run-2').checked;
  const confirmMode = document.getElementById('wiz-confirm-steps-2').checked;
  closePipelineWizard();
  runPipeline(dryRun, confirmMode);
}

/* ── Count type occurrences for duplicate-step labeling ─────────────────── */
function _typeLabel(steps, step, i) {
  const siblings = steps.filter((s, j) => s.type === step.type && j <= i);
  const count = siblings.length;
  const def = PIPE_STEPS[step.type] || { name: step.type };
  return count > 1 ? `${def.name} (${count})` : def.name;
}

/* ── Required fields per step type ──────────────────────────────────────── */
const STEP_REQUIRED_FIELDS = {
  audit:      [],           // paths is optional
  process:    ['paths'],
  normalize:  ['paths'],
  duplicates: ['paths'],
  prune:      [],           // auto-uses CSV from prior duplicates step
  convert:    ['paths'],
  relocate:   ['old_root','new_root'],
  import:     ['paths'],
  link:       ['paths'],
  organize:   ['sources','target'],
  novelty:    ['source','dest'],
};

function _stepIsReady(step) {
  const required = STEP_REQUIRED_FIELDS[step.type] || [];
  if (required.length === 0) return true;
  const cfg = step._draftConfig || {};
  return required.every(f => {
    const val = cfg[f];
    if (Array.isArray(val)) return val.length > 0 && val.some(s => s.trim() !== '');
    return (val || '').trim() !== '';
  });
}

function _wizUpdateProgress() {
  const total = pipelineSteps.length;
  const ready = pipelineSteps.filter(_stepIsReady).length;
  const pct   = total === 0 ? 0 : Math.round((ready / total) * 100);

  document.getElementById('wiz-progress-label').textContent = `${ready} / ${total} steps ready`;
  document.getElementById('wiz-progress-bar').style.width   = pct + '%';

  const allReady = ready === total && total > 0;
  const btn = document.getElementById('wiz-run-btn');
  if (allReady) {
    btn.style.opacity    = '1';
    btn.style.boxShadow  = '';
    btn.style.cursor     = 'pointer';
  } else {
    btn.style.opacity    = '0.45';
    btn.style.boxShadow  = '0 0 12px 2px rgba(239,68,68,.35)';
    btn.style.cursor     = 'default';
  }

  // Update sidebar ready indicators
  pipelineSteps.forEach((s, i) => {
    const si = document.getElementById(`pipe-wiz-si-${i}`);
    if (!si) return;
    const dot = si.querySelector('.wiz-ready-dot');
    if (!dot) return;
    const ready = _stepIsReady(s);
    dot.style.background = ready ? 'var(--safe)' : 'rgba(239,68,68,.6)';
    dot.title = ready ? 'Ready' : 'Needs configuration';
  });
}

let _wizActiveIndex = 0;

function pipeWizBuildConfigs() {
  const stack = document.getElementById('pipe-wiz-stack');
  stack.innerHTML = '';

  // Initialise _draftConfig for each step from saved or empty
  pipelineSteps.forEach(step => {
    if (!step._draftConfig) {
      step._draftConfig = step._config ? { ...step._config } : (_loadPipeCfg(step.type) || {});
    }
  });

  pipelineSteps.forEach((step, i) => {
    const def   = PIPE_STEPS[step.type] || { name: step.type, icon: '/static/SRB_LOGO.png', desc: '' };
    const label = _typeLabel(pipelineSteps, step, i);
    const ready = _stepIsReady(step);

    const si = document.createElement('div');
    si.className = 'pipe-wiz-stack-item' + (i === 0 ? ' active' : '');
    si.id        = `pipe-wiz-si-${i}`;
    si.onclick   = () => pipeWizSelectStep(i);
    si.innerHTML = `
      <div class="pipe-step-num" style="width:18px;height:18px;font-size:.65rem;flex-shrink:0">${i + 1}</div>
      <img src="${def.icon}" style="width:15px;height:15px;object-fit:contain;flex-shrink:0">
      <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:.8rem">${label}</span>
      <span class="wiz-ready-dot" title="${ready ? 'Ready' : 'Needs config'}"
            style="width:7px;height:7px;border-radius:50%;flex-shrink:0;margin-left:4px;
                   background:${ready ? 'var(--safe)' : 'rgba(239,68,68,.6)'}"></span>`;
    stack.appendChild(si);
  });

  _wizActiveIndex = 0;
  pipeWizSelectStep(0);
  _wizUpdateProgress();
}

function pipeWizSelectStep(i) {
  _wizActiveIndex = i;
  document.querySelectorAll('.pipe-wiz-stack-item').forEach((el, j) => {
    el.classList.toggle('active', j === i);
  });

  const step  = pipelineSteps[i];
  if (!step) return;
  const def   = PIPE_STEPS[step.type] || { name: step.type, icon: '/static/SRB_LOGO.png', desc: '' };
  const label = _typeLabel(pipelineSteps, step, i);
  const panel = document.getElementById('pipe-wiz-active-cfg');

  panel.innerHTML = `
    <div style="display:flex;align-items:center;gap:10px;margin-bottom:4px">
      <img src="${def.icon}" style="width:24px;height:24px;object-fit:contain">
      <div>
        <div style="font-weight:700;font-size:1rem">${label}</div>
        <div style="font-size:.75rem;color:var(--text-dim)">${def.desc}</div>
      </div>
    </div>
    <hr style="border:none;border-top:1px solid var(--border);margin:4px 0 12px">
    ${_pipeWizConfigHTML(step, step._draftConfig)}`;

  // Wire up all inputs in the panel to update _draftConfig live
  panel.querySelectorAll('input[type=text], input[type=number], select, textarea').forEach(el => {
    el.addEventListener('input', () => {
      _pipeWizReadDraft(step);
      _wizUpdateProgress();
    });
  });
  // Wire drop zones — single-path inputs use setupDropZone; multi-path textareas use setupMultiDropZone
  panel.querySelectorAll('input[type=text]').forEach(setupDropZone);
  panel.querySelectorAll('textarea.pipe-cfg-input').forEach(setupMultiDropZone);
}

function _pipeWizConfigHTML(step, saved) {
  /* Renders config fields for the active step using data-cfg attributes.
     saved is the _draftConfig object (not localStorage). */
  const v  = (field, fallback) => (saved && saved[field] !== undefined && saved[field] !== '') ? saved[field] : (fallback || '');

  const pathRow = (field, label, placeholder, required = true) => `
    <div class="pipe-cfg-field">
      <label class="pipe-cfg-label">${label}${required ? ' <span style="color:var(--danger)">*</span>' : ''}</label>
      <div class="drop-wrap" style="flex:1">
        <input type="text" class="pipe-cfg-input" data-cfg="${field}"
               value="${v(field)}" placeholder="${placeholder}" style="width:100%">
        <span class="drop-badge">⤵ drop</span>
      </div>
    </div>`;

  /* Multi-path textarea — each line is a folder path, drop appends a new line */
  const multiPathRow = (field, label, placeholder, required = true) => {
    const rawVal     = saved && saved[field] !== undefined ? saved[field] : '';
    const displayVal = Array.isArray(rawVal) ? rawVal.join('\n') : (rawVal || '');
    return `
    <div class="pipe-cfg-field">
      <label class="pipe-cfg-label">${label}${required ? ' <span style="color:var(--danger)">*</span>' : ''}</label>
      <div class="drop-wrap" style="flex:1">
        <textarea class="pipe-cfg-input" data-cfg="${field}" rows="3"
                  placeholder="${placeholder}"
                  style="width:100%;resize:vertical;min-height:58px;font-family:inherit;line-height:1.5;">${displayVal}</textarea>
        <span class="drop-badge" style="top:8px">⤵ drop</span>
      </div>
      <div style="font-size:.72rem;color:var(--text-dim);margin-top:3px;padding-left:2px">One folder per line — drop to append.</div>
    </div>`;
  };

  const workersRow = (def = 4) => `
    <div class="pipe-cfg-field" style="max-width:180px">
      <label class="pipe-cfg-label">Workers</label>
      <select class="pipe-cfg-input" data-cfg="workers"
              style="background:var(--surface-hi);border:1px solid var(--border-hi);color:var(--text);border-radius:var(--radius);padding:8px 10px;font-size:.84rem;">
        ${[1,2,4,6,8].map(n => `<option value="${n}" ${parseInt(v('workers', def)) === n ? 'selected' : ''}>${n} worker${n>1?'s':''}</option>`).join('')}
      </select>
    </div>`;

  switch (step.type) {
    case 'audit':
      return multiPathRow('paths', 'Music folders (optional)', '/Volumes/YourDrive/Music', false);

    case 'process':
      return multiPathRow('paths', 'Music folders', '/Volumes/YourDrive/Music') + workersRow(4);

    case 'normalize':
      return multiPathRow('paths', 'Music folders', '/Volumes/YourDrive/Music') + workersRow(4);

    case 'duplicates':
      return multiPathRow('paths', 'Folders to scan', '/Volumes/YourDrive/Music') + workersRow(4);

    case 'prune':
      return `<p class="pipe-cfg-note" style="color:var(--text-muted);font-size:.84rem;">
        Prune reads the duplicate report produced by the Find Duplicates step above.
        No additional configuration needed — the report path is passed automatically.
      </p>`;

    case 'convert':
      return multiPathRow('paths', 'Folders to convert', '/Volumes/YourDrive/Music') + `
        <div style="display:flex;gap:12px;flex-wrap:wrap">
          <div class="pipe-cfg-field" style="flex:1">
            <label class="pipe-cfg-label">Target format <span style="color:var(--danger)">*</span></label>
            <select class="pipe-cfg-input" data-cfg="format"
                    style="background:var(--surface-hi);border:1px solid var(--border-hi);color:var(--text);border-radius:var(--radius);padding:8px 10px;font-size:.84rem;width:100%">
              ${['aiff','mp3','wav','flac'].map(f =>
                `<option value="${f}" ${v('format','aiff') === f ? 'selected' : ''}>${f.toUpperCase()}</option>`
              ).join('')}
            </select>
          </div>
          ${workersRow(4)}
        </div>`;

    case 'relocate':
      return pathRow('old_root', 'Old path prefix (where files were)', '/Volumes/OLD_DRIVE/Music') +
             pathRow('new_root', 'New path prefix (where files are now)', '/Volumes/YourDrive/Music');

    case 'import':
      return multiPathRow('paths', 'Import from (folders)', '/Volumes/YourDrive/Music');

    case 'link':
      return multiPathRow('paths', 'Library folders', '/Volumes/YourDrive/Music');

    case 'organize':
      return multiPathRow('sources', 'Source folders', '/Volumes/YourDrive/Music') +
             pathRow('target', 'Target (organized root)', '/Volumes/YourDrive/Music') + `
        <div style="display:flex;gap:12px;flex-wrap:wrap">
          <div class="pipe-cfg-field" style="flex:1">
            <label class="pipe-cfg-label">Mode</label>
            <select class="pipe-cfg-input" data-cfg="mode"
                    style="background:var(--surface-hi);border:1px solid var(--border-hi);color:var(--text);border-radius:var(--radius);padding:8px 10px;font-size:.84rem;width:100%">
              <option value="assimilate" ${v('mode','assimilate')==='assimilate'?'selected':''}>Assimilate — move &amp; clean source</option>
              <option value="integrate"  ${v('mode','assimilate')==='integrate'?'selected':''}>Integrate — copy only</option>
            </select>
          </div>
          ${workersRow(1)}
        </div>`;

    case 'novelty':
      return pathRow('source', 'Source drive / folder', '/Volumes/Passport') +
             pathRow('dest',   'Home library destination', '/Volumes/YourDrive/Music') +
             workersRow(4);

    default:
      return `<p class="pipe-cfg-note">No configuration needed for this step.</p>`;
  }
}

function _pipeWizReadDraft(step) {
  /* Read current values from the active panel into step._draftConfig */
  const panel    = document.getElementById('pipe-wiz-active-cfg');
  const get      = field => panel.querySelector(`[data-cfg="${field}"]`)?.value?.trim() || '';
  const getN     = (field, def) => parseInt(panel.querySelector(`[data-cfg="${field}"]`)?.value || def);
  // Read a multi-path textarea: split on newlines, trim, drop blanks
  const getLines = field => {
    const el = panel.querySelector(`[data-cfg="${field}"]`);
    if (!el) return [];
    return el.value.split('\n').map(s => s.trim()).filter(Boolean);
  };

  const draft = {};
  switch (step.type) {
    case 'audit':
      draft.paths = getLines('paths'); break;
    case 'import':
      draft.paths = getLines('paths'); break;
    case 'link':
      draft.paths = getLines('paths'); break;
    case 'process':
      draft.paths       = getLines('paths');
      draft.workers     = getN('workers', 1);
      draft.no_normalize = true; break;
    case 'normalize':
      draft.paths   = getLines('paths');
      draft.workers = getN('workers', 1); break;
    case 'duplicates':
      draft.paths   = getLines('paths');
      draft.workers = getN('workers', 1); break;
    case 'prune':
      break; // no required fields
    case 'convert':
      draft.paths   = getLines('paths');
      draft.format  = get('format') || 'aiff';
      draft.workers = getN('workers', 1); break;
    case 'relocate':
      draft.old_root = get('old_root');
      draft.new_root = get('new_root'); break;
    case 'organize':
      draft.sources = getLines('sources');
      draft.target  = get('target');
      draft.mode    = get('mode') || 'assimilate';
      draft.workers = getN('workers', 1); break;
    case 'novelty':
      draft.source  = get('source');
      draft.dest    = get('dest');
      draft.workers = getN('workers', 1); break;
  }
  step._draftConfig = draft;
  step._config      = draft;  // keep _config in sync for the runner
}


function _savePipeCfg(type, cfg) {
  try { localStorage.setItem(`sb_pipe_cfg_${type}`, JSON.stringify(cfg)); } catch(_) {}
}

function _loadPipeCfg(type) {
  try { return JSON.parse(localStorage.getItem(`sb_pipe_cfg_${type}`)) || {}; } catch(_) { return {}; }
}

/* ── Pipeline checkpoint: survive interruptions and resume ────────────────── */
function _savePipeCheckpoint(steps, completedIdx, dryRun) {
  try {
    localStorage.setItem('sb_pipe_checkpoint', JSON.stringify({
      steps: steps.map(s => ({ type: s.type, _config: s._config || {} })),
      completedIdx,
      dryRun,
      ts: Date.now(),
    }));
  } catch(_) {}
}
function _loadPipeCheckpoint() {
  try { return JSON.parse(localStorage.getItem('sb_pipe_checkpoint')); } catch(_) { return null; }
}
function _clearPipeCheckpoint() {
  try { localStorage.removeItem('sb_pipe_checkpoint'); } catch(_) {}
}

function pipelineAddStep(type) {
  // Prune requires a preceding Find Duplicates step — it has no CSV without one
  if (type === 'prune') {
    const hasDuplicates = pipelineSteps.some(s => s.type === 'duplicates');
    if (!hasDuplicates) {
      showToast('Add a "Find Duplicates" step first — Prune reads its report.', 'warning');
      // Pulse the duplicates button to guide the user
      const dupBtn = [...document.querySelectorAll('#pipe-wiz-p1 .pipe-action-btn')]
        .find(b => (b.getAttribute('onclick') || '').includes("'duplicates'"));
      if (dupBtn) {
        dupBtn.classList.remove('pipe-added'); void dupBtn.offsetWidth; dupBtn.classList.add('pipe-added');
        dupBtn.addEventListener('animationend', () => dupBtn.classList.remove('pipe-added'), { once: true });
      }
      return;
    }
  }
  pipelineSteps.push({ id: ++pipeUid, type });
  pipelineRender();
  // Flash the clicked button
  const btn = [...document.querySelectorAll('#pipe-wiz-p1 .pipe-action-btn')]
    .find(b => (b.getAttribute('onclick') || '').includes(`'${type}'`));
  if (btn) {
    btn.classList.remove('pipe-added');
    void btn.offsetWidth;
    btn.classList.add('pipe-added');
    btn.addEventListener('animationend', () => btn.classList.remove('pipe-added'), { once: true });
  }
}

function pipelineRemoveStep(id) {
  pipelineSteps = pipelineSteps.filter(s => s.id !== id);
  pipelineRender();
}

function pipelineMoveStep(id, dir) {
  const i = pipelineSteps.findIndex(s => s.id === id);
  if (i < 0) return;
  const j = i + dir;
  if (j < 0 || j >= pipelineSteps.length) return;
  [pipelineSteps[i], pipelineSteps[j]] = [pipelineSteps[j], pipelineSteps[i]];
  pipelineRender();
}

function pipelineClear() {
  pipelineSteps = [];
  pipelineRender();
  document.getElementById('pipe-recommended-note').classList.add('hidden');
}

function pipelineLoadRecommended() {
  pipelineSteps = RECOMMENDED.map(type => ({ id: ++pipeUid, type }));
  pipelineRender();
  document.getElementById('pipe-recommended-note').classList.remove('hidden');
}

function pipelineRender() {
  const queue   = document.getElementById('pipeline-queue');
  const empty   = document.getElementById('pipe-empty-msg');
  const note    = document.getElementById('pipe-config-note');

  // Remove only step elements — leave pipe-empty-msg in the DOM so getElementById finds it next time
  queue.querySelectorAll('.pipe-step').forEach(el => el.remove());

  if (pipelineSteps.length === 0) {
    if (empty) empty.classList.remove('hidden');
    if (note) note.classList.add('hidden');
    return;
  }

  if (empty) empty.classList.add('hidden');
  if (note) note.classList.remove('hidden');

  pipelineSteps.forEach((step, i) => {
    const def = PIPE_STEPS[step.type] || { name: step.type, icon: '⚙', desc: '' };
    const el  = document.createElement('div');
    el.className = 'pipe-step';
    el.id = `pipe-step-${step.id}`;
    el.innerHTML = `
      <div class="pipe-step-num">${i + 1}</div>
      <div class="pipe-step-body">
        <div class="pipe-step-name"><img src="${def.icon}" style="width:16px;height:16px;object-fit:contain;vertical-align:middle;margin-right:5px">${def.name}</div>
        <div class="pipe-step-desc">${def.desc}</div>
      </div>
      <div class="pipe-step-controls">
        <button onclick="pipelineMoveStep(${step.id}, -1)" title="Move up" ${i === 0 ? 'disabled' : ''}>↑</button>
        <button onclick="pipelineMoveStep(${step.id},  1)" title="Move down" ${i === pipelineSteps.length - 1 ? 'disabled' : ''}>↓</button>
        <button class="pipe-remove" onclick="pipelineRemoveStep(${step.id})" title="Remove">✕</button>
      </div>`;
    queue.appendChild(el);
  });
}

function _pipelineReadConfig(step, extraCsv) {
  /* Step configs are now managed via _draftConfig → _config in the wizard.
     This function is the fallback for the auto-mode runner — it returns
     whatever is already stored on the step object, with prune CSV injection. */
  const cfg = step._config || _loadPipeCfg(step.type) || {};
  if (step.type === 'prune' && extraCsv && !cfg.csv) {
    return { ...cfg, csv: extraCsv };
  }
  return cfg;
}

/* ── Pipeline confirm-gate state ──────────────────────────────────────────── */
let _pipeGateResolve = null;   // resolves with action string: 'finish' | 'redo' | 'skip' | 'stop'

function pipeGateAction(action) {
  document.getElementById('pipe-confirm-gate').style.display = 'none';
  if (_pipeGateResolve) { _pipeGateResolve(action); _pipeGateResolve = null; }
}
// Legacy aliases for any lingering calls
function pipeConfirmContinue() { pipeGateAction('finish'); }
function pipeConfirmStop()     { pipeGateAction('stop');   }

function _showPipeGate(succeeded, completedName, nextName, summaryLines) {
  /* Returns a Promise that resolves with an action string. */
  const gate     = document.getElementById('pipe-confirm-gate');
  const icon     = document.getElementById('pipe-gate-icon');
  const title    = document.getElementById('pipe-gate-title');
  const body     = document.getElementById('pipe-gate-body');
  const btnFinish = document.getElementById('pipe-btn-finish');
  const btnRedo   = document.getElementById('pipe-btn-redo');
  const btnSkip   = document.getElementById('pipe-btn-skip');
  const nextLabel = document.getElementById('pipe-gate-next-label');

  const nextText = nextName ? ` → ${nextName}` : '';

  if (succeeded) {
    gate.style.setProperty('--pipe-gate-border', 'rgba(52,211,153,.35)');
    gate.style.setProperty('--pipe-gate-bg',     'rgba(52,211,153,.05)');
    icon.textContent  = '✓';
    title.textContent = `"${completedName}" complete`;
    body.textContent  = summaryLines.length
      ? summaryLines.filter(l => l.trim()).slice(-5).join('  ·  ')
      : 'Step finished successfully.';
    btnFinish.textContent = nextName ? `Finish${nextText}` : 'Finish';
    btnFinish.style.display = '';
    btnRedo.style.display  = 'none';
    btnSkip.style.display  = 'none';
    nextLabel.textContent  = '';
  } else {
    gate.style.setProperty('--pipe-gate-border', 'rgba(239,68,68,.35)');
    gate.style.setProperty('--pipe-gate-bg',     'rgba(239,68,68,.05)');
    icon.textContent  = '⚠';
    title.textContent = `"${completedName}" did not complete`;
    body.textContent  = 'Step stopped or failed. Choose how to proceed:';
    btnFinish.style.display = 'none';
    btnRedo.style.display   = '';
    btnSkip.textContent     = nextName ? `Skip${nextText}` : 'Skip';
    btnSkip.style.display   = nextName ? '' : 'none';
    nextLabel.textContent   = '';
  }

  gate.style.display = '';
  return new Promise(resolve => { _pipeGateResolve = resolve; });
}

/* ── Run a single pipeline step via /api/run/pipeline ──────────────────────── */
async function _runOnePipelineStep(step, dryRun, capturedCsv) {
  /* Returns {exitCode, reportPath, outputLines} */
  const stepWithCsv = {
    ...step,
    config: (step.type === 'prune' && capturedCsv) ? { csv: capturedCsv } : (step.config || {}),
  };
  const resp = await fetch('/api/run/pipeline', {
    method:  'POST',
    headers: { 'Content-Type': 'application/json' },
    body:    JSON.stringify({ dry_run: dryRun, steps: [stepWithCsv] }),
  });
  if (!resp.ok) throw new Error(await resp.text());

  const reader   = resp.body.getReader();
  const decoder  = new TextDecoder();
  let   buf      = '';
  let   reportPath = null;
  let   outputLines = [];
  let   exitCode = 0;
  let   inReport = false;

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    const parts = buf.split('\n\n');
    buf = parts.pop();
    for (const part of parts) {
      const dataLine = part.split('\n').find(l => l.startsWith('data: '));
      if (!dataLine) continue;
      try {
        const ev = JSON.parse(dataLine.slice(6));
        if (ev.step_start !== undefined) {
          /* already logged by caller */
        } else if (ev.step_end !== undefined) {
          exitCode = ev.exit_code;
        } else if (ev.line !== undefined) {
          const line = ev.line;
          if (line === 'REKITBOX_REPORT_BEGIN')       { inReport = true; }
          else if (line === 'REKITBOX_REPORT_END')    { inReport = false; }
          else if (line.startsWith('REKITBOX_PROGRESS: ')) {
            try { updateScanBar(JSON.parse(line.slice(19))); } catch(_) {}
          } else if (line.startsWith('REKITBOX_REPORT_PATH: ')) {
            reportPath = line.slice(22).trim();
          } else if (line.startsWith('REKITBOX_PHYSICAL_SCAN: ')) {
            appendLog(`  📁 Physical scan → ${line.slice(24).trim()}`, 'dim');
          } else {
            outputLines.push(line);
            appendLog(line, classifyLine(line));
          }
        } else if (ev.done) {
          exitCode = ev.exit_code || 0;
        }
      } catch (_) {}
    }
  }
  return { exitCode, reportPath, outputLines };
}

async function runPipeline(dryRun = true, confirmMode = false) {
  if (pipelineSteps.length === 0) {
    alert('Add at least one step to the pipeline first.');
    return;
  }
  if (isRunning) return;

  /* dryRun and confirmMode are passed in from pipeWizRun() */
  const label        = dryRun ? 'Pipeline — Dry Run (preview only)' : 'Running Pipeline';
  const total        = pipelineSteps.length;

  /* Reset step visual state */
  pipelineSteps.forEach(s => {
    const el = document.getElementById(`pipe-step-${s.id}`);
    if (el) el.className = 'pipe-step';
  });
  document.getElementById('pipe-confirm-gate').style.display = 'none';

  initLog(label);
  showScanBar(label);
  isRunning = true;
  setSpinner(true);
  setAllButtons(true);
  appendLog(`▸ ${label}${confirmMode ? '  (confirm between steps)' : ''}`, 'dim');
  appendLog('', 'dim');

  let reportBuffer = [];
  let capturedCsv  = null;   // last REKITBOX_REPORT_PATH from a duplicates step

  const finish = (exitCode, failedStep, stopped) => {
    isRunning = false;
    setSpinner(false);
    setAllButtons(false);
    finishScanBar();
    document.getElementById('pipe-confirm-gate').style.display = 'none';
    // Clean up any dangling gate promise
    if (_pipeGateResolve) { _pipeGateResolve('stop'); _pipeGateResolve = null; }
    // Clear checkpoint only when the full pipeline completed cleanly
    if (!failedStep && !stopped && exitCode === 0) _clearPipeCheckpoint();
    appendLog('', '');
    if (stopped) {
      appendLog('⏹ Pipeline stopped by user.', 'log-exit-fail');
    } else if (failedStep) {
      appendLog(`✗ Pipeline stopped — "${failedStep}" had an error.`, 'log-exit-fail');
    } else if (exitCode === 0) {
      appendLog(dryRun
        ? '✓ Preview complete. Uncheck Dry Run and run again to execute.'
        : '✓ Pipeline complete.', 'log-exit-ok');
    } else {
      appendLog(`✗ Exited with code ${exitCode}`, 'log-exit-fail');
    }
    if (reportBuffer.length > 0) {
      sessionReports[label] = { text: reportBuffer.join('\n'), reportPath: null };
      _addOrUpdateSummaryPill(label);
    }
  };

  /* ── CONFIRM MODE: run one step at a time with gate between each ─────────── */
  if (confirmMode) {
    let i = 0;
    while (i < pipelineSteps.length) {
      const s    = pipelineSteps[i];
      const def  = PIPE_STEPS[s.type] || { name: s.type, icon: '⚙', desc: '' };
      const step = { type: s.type, name: def.name, config: s._config || _loadPipeCfg(s.type) || {} };
      const el   = document.getElementById(`pipe-step-${s.id}`);

      if (el) el.className = 'pipe-step running';
      appendLog('', '');
      appendLog(`── Step ${i + 1} / ${total}: ${def.name} ──`, 'dim');

      let result;
      try {
        result = await _runOnePipelineStep(step, dryRun, capturedCsv);
      } catch (err) {
        if (el) el.className = 'pipe-step failed';
        appendLog('[Connection error] ' + err.message, 'error');
        finish(1, def.name, false);
        return;
      }

      if (el) el.className = result.exitCode === 0 ? 'pipe-step done' : 'pipe-step failed';
      if (result.reportPath) capturedCsv = result.reportPath;
      // Save a checkpoint so the run can be resumed from this point if interrupted
      if (result.exitCode === 0) _savePipeCheckpoint(pipelineSteps, i, dryRun);

      const succeeded = result.exitCode === 0;
      const nextStep  = pipelineSteps[i + 1];
      const nextName  = nextStep ? (PIPE_STEPS[nextStep.type] || { name: nextStep.type }).name : null;
      const summaryLines = result.outputLines.filter(l => l.trim());

      /* Show gate — returns 'finish' | 'redo' | 'skip' | 'stop' */
      const action = await _showPipeGate(succeeded, def.name, nextName, summaryLines);

      if (action === 'stop') {
        finish(succeeded ? 0 : result.exitCode, null, true);
        return;
      } else if (action === 'redo') {
        /* Re-do: mark step as pending again and restart the same index */
        if (el) el.className = 'pipe-step';
        appendLog(`↺ Re-doing "${def.name}"…`, 'dim');
        continue;   // i stays the same
      } else if (action === 'skip') {
        /* Skip: advance past this step */
        appendLog(`⤳ Skipped "${def.name}"`, 'dim');
        i++;
        continue;
      } else {
        /* finish: accept step result, advance to next */
        i++;
      }
    }
    finish(0, null, false);
    return;
  }

  /* ── AUTO MODE: send all steps at once (original behaviour) ─────────────── */
  const steps = pipelineSteps.map(s => ({
    type:   s.type,
    name:   (PIPE_STEPS[s.type] || {}).name || s.type,
    config: s._config || {},
  }));
  let stepIdMap = {};
  pipelineSteps.forEach((s, i) => { stepIdMap[i + 1] = `pipe-step-${s.id}`; });
  let inReport = false;

  try {
    const resp = await fetch('/api/run/pipeline', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ dry_run: dryRun, steps }),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({ error: resp.statusText }));
      appendLog('Pipeline error: ' + (err.error || resp.statusText), 'error');
      finish(1, null, false);
      return;
    }

    const reader  = resp.body.getReader();
    const decoder = new TextDecoder();
    let   buf     = '';

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      const parts = buf.split('\n\n');
      buf = parts.pop();

      for (const part of parts) {
        const dataLine = part.split('\n').find(l => l.startsWith('data: '));
        if (!dataLine) continue;
        try {
          const ev = JSON.parse(dataLine.slice(6));
          if (ev.step_start !== undefined) {
            const el = document.getElementById(stepIdMap[ev.step_start]);
            if (el) el.className = 'pipe-step running';
            appendLog('', '');
            appendLog(`── Step ${ev.step_start} / ${ev.total_steps}: ${ev.step_name} ──`, 'dim');
          } else if (ev.step_end !== undefined) {
            const el = document.getElementById(stepIdMap[ev.step_end]);
            if (el) el.className = ev.exit_code === 0 ? 'pipe-step done' : 'pipe-step failed';
          } else if (ev.line !== undefined) {
            const line = ev.line;
            if (line === 'REKITBOX_REPORT_BEGIN')       { inReport = true; reportBuffer = []; }
            else if (line === 'REKITBOX_REPORT_END')    { inReport = false; }
            else if (line.startsWith('REKITBOX_PROGRESS: ')) {
              try { updateScanBar(JSON.parse(line.slice(19))); } catch(_) {}
            } else if (line.startsWith('REKITBOX_REPORT_PATH: ')) {
              /* silently capture */
            } else if (line.startsWith('REKITBOX_PHYSICAL_SCAN: ')) {
              /* silently capture */
            } else {
              if (inReport) reportBuffer.push(line);
              appendLog(line, classifyLine(line));
            }
          } else if (ev.done) {
            finish(ev.exit_code || 0, ev.failed_step || null, false);
          }
        } catch (_) {}
      }
    }
  } catch (err) {
    appendLog('[Connection error] ' + err.message, 'error');
    finish(1, null, false);
  }
}

function orgUpdateMode(val) {
  const badge = document.getElementById('organize-risk-badge');
  if (badge) {
    if (val === 'integrate') {
      badge.textContent = 'Copies Files';
      badge.className   = 'risk-badge safe';
    } else {
      badge.textContent = 'Moves Files';
      badge.className   = 'risk-badge warn';
    }
  }
}

function runOrganize() {
  const sources  = getFolderPaths('organize-source-pills');
  const target   = document.getElementById('organize-target').value.trim();
  const dryRun   = document.getElementById('organize-dry-run').checked;
  const workers  = document.getElementById('organize-workers')?.value || '1';
  const threshold = document.getElementById('organize-mix-threshold')?.value || '15';
  const mode     = document.getElementById('organize-mode')?.value || 'assimilate';
  if (!sources.length) { alert('Enter at least one source folder path.'); return; }
  if (!target) { alert('Enter a target (library root) folder path.'); return; }
  const p = new URLSearchParams();
  sources.forEach(s => p.append('source', s));
  p.set('target', target);
  if (!dryRun) p.set('no_dry_run', '1');
  if (mode !== 'assimilate') p.set('mode', mode);
  if (parseInt(workers) > 1) p.set('workers', workers);
  if (threshold !== '15') p.set('mix_threshold', threshold);
  const modeLabel = mode === 'integrate' ? 'Integration (copies only, source untouched)' : 'Assimilation (move + clean source)';
  const label = dryRun ? `Organize — Dry Run · ${modeLabel}` : `Organize — ${modeLabel}`;
  const _orgTarget = target;
  const _orgDry    = dryRun;
  runCommand(`/api/run/organize?${p}`, label, (exitCode) => {
    if (exitCode === 0 && !_orgDry) _promptSetLibraryRoot(_orgTarget);
  });
}

/* ── Novelty Scanner ───────────────────────────────────────────────────────── */
function runNovelty() {
  const sources = getFolderPaths('novelty-pills');
  const dest    = document.getElementById('novelty-dest').value.trim();
  const dryRun  = document.getElementById('novelty-dry-run').checked;
  if (!sources.length) { alert('Add at least one source drive or folder.'); return; }
  if (!dest)           { alert('Enter a destination (home library) path.'); return; }
  const p = new URLSearchParams();
  sources.forEach(source => p.append('source', source));
  p.set('dest', dest);
  if (!dryRun) p.set('no_dry_run', '1');
  const label = dryRun
    ? 'Novelty Scan — Dry Run (nothing will be copied)'
    : 'Novelty Scan — Copying novel tracks to destination';
  runCommand(`/api/run/novelty?${p}`, label);
}

/* ── Prune Duplicates ──────────────────────────────────────────────────────── */
let pruneGroups        = [];          // current page's groups
let pruneSelected      = new Set();   // file_paths checked for removal
let saState            = null;        // 'best' | 'lower' | null
let prunePage          = 0;
let prunePageSize      = 200;
let pruneTotalGroups   = 0;
let pruneTotalRemove   = 0;
let pruneTotalRemoveMb = 0;
let pruneCsvPath       = '';

async function loadPruneReport() {
  const csvPath = document.getElementById('prune-csv-path').value.trim();
  await _autoLoadDupeResults(csvPath);
}

async function _loadPrunePage(page) {
  let url = `/api/duplicates/load?page=${page}&per_page=${prunePageSize}`;
  if (pruneCsvPath) url += '&csv_path=' + encodeURIComponent(pruneCsvPath);

  const res  = await fetch(url);
  const data = await res.json();
  if (!res.ok) { alert('Could not load report:\n' + data.error); return; }

  pruneGroups        = data.groups;
  prunePage          = data.page;
  pruneTotalGroups   = data.total_groups;
  if (data.total_remove    != null) pruneTotalRemove   = data.total_remove;
  if (data.total_remove_mb != null) pruneTotalRemoveMb = data.total_remove_mb;

  _renderPruneGroups();
  _renderPrunePagination();
  _syncCheckboxes();
  _updateSaButtons();
  _updatePruneSummary();
}

function _renderPruneGroups() {
  const container = document.getElementById('prune-groups');
  container.innerHTML = '';

  pruneGroups.forEach(g => {
    const wrap = document.createElement('div');
    wrap.className = 'prune-group';

    const keep    = g.entries.find(e => e.action === 'KEEP');
    const lowers  = g.entries.filter(e => e.action === 'REVIEW_REMOVE');
    const title   = keep ? keep.filename : ('Group ' + g.group_id);

    wrap.innerHTML = `<div class="prune-group-head">
      <span class="prune-group-title">${_esc(title)}</span>
      <span class="prune-group-count">${g.entries.length} copies</span>
    </div>`;

    if (keep)   wrap.appendChild(_makeRow(keep,  false));
    lowers.forEach(e => wrap.appendChild(_makeRow(e, true)));
    container.appendChild(wrap);
  });
}

function _makeRow(entry, isLower) {
  const row   = document.createElement('div');
  row.className = isLower ? 'prune-row-lower' : 'prune-row-keep';
  // Store path safely as a data attribute — avoids inline onclick string injection
  row.dataset.filePath = entry.file_path;

  const ext   = (entry.format_ext || '').replace('.','').toUpperCase();
  const lossless = ['AIFF','AIF','WAV','FLAC'].includes(ext);
  const fmtCls = lossless ? 'fmt-lossless' : 'fmt-lossy';

  const rankCls = { PN:'rank-pn', MIK:'rank-mik', RAW:'rank-raw' }[entry.rank] || 'rank-raw';
  const checked = pruneSelected.has(entry.file_path);
  const cbCls   = isLower ? 'prune-cb' : 'prune-cb keep-cb';

  row.innerHTML = `
    <input type="checkbox" class="${cbCls}" ${checked ? 'checked' : ''}>
    <span class="prune-star">${isLower ? '' : '★'}</span>
    <span class="prune-fname" title="${_esc(entry.file_path)}">${_esc(entry.filename)}</span>
    <span class="fmt-badge ${fmtCls}">${ext || '?'}</span>
    <span class="prune-meta">${entry.file_size_mb.toFixed(1)} MB</span>
    ${entry.bpm  ? `<span class="prune-meta">${entry.bpm} BPM</span>`  : ''}
    ${entry.key  ? `<span class="prune-meta">${entry.key}</span>` : ''}
    <span class="rank-badge ${rankCls}">${entry.rank}</span>
    ${entry.in_db         ? '<span class="prune-indb">in DB</span>'    : ''}
    ${!entry.exists_on_disk ? '<span class="prune-missing">missing</span>' : ''}
    <button class="prune-preview-btn">▶</button>`;

  // Attach event listeners using the data attribute — safe against any path content
  row.querySelector('input[type=checkbox]').addEventListener('change', function() {
    togglePruneFile(entry.file_path, this.checked);
  });
  row.querySelector('.prune-preview-btn').addEventListener('click', function() {
    previewFile(entry.file_path);
  });

  return row;
}

function togglePruneFile(path, checked) {
  checked ? pruneSelected.add(path) : pruneSelected.delete(path);
  saState = null;
  _updateSaButtons();
  _updatePruneSummary();
}

async function _fetchAllPaths() {
  let url = '/api/duplicates/remove-paths';
  if (pruneCsvPath) url += '?csv_path=' + encodeURIComponent(pruneCsvPath);
  const res  = await fetch(url);
  const data = await res.json();
  if (!res.ok) throw new Error(data.error);
  return data;
}

async function selectAllBest() {
  try {
    const data = await _fetchAllPaths();
    pruneSelected = new Set(data.keep_paths);
    saState = 'best';
    _syncCheckboxes();
    _updateSaButtons();
    _updatePruneSummary();
  } catch (err) { alert('Error: ' + err); }
}

async function selectAllLower() {
  try {
    const data = await _fetchAllPaths();
    pruneSelected = new Set(data.remove_paths);
    saState = 'lower';
    _syncCheckboxes();
    _updateSaButtons();
    _updatePruneSummary();
  } catch (err) { alert('Error: ' + err); }
}

function _renderPrunePagination() {
  const totalPages = Math.ceil(pruneTotalGroups / prunePageSize);
  const pg = document.getElementById('prune-pagination');
  if (totalPages <= 1) { pg.style.display = 'none'; } else { pg.style.display = 'flex'; }

  const start = prunePage * prunePageSize + 1;
  const end   = Math.min((prunePage + 1) * prunePageSize, pruneTotalGroups);
  document.getElementById('prune-page-info').textContent =
    `Groups ${start.toLocaleString()}–${end.toLocaleString()} of ${pruneTotalGroups.toLocaleString()}`;
  document.getElementById('prune-prev-btn').disabled = prunePage === 0;
  document.getElementById('prune-next-btn').disabled = prunePage >= totalPages - 1;

  _updateDupesStats();
}

/* ── Duplicate Tracks — phase switching & stats ────────────────────────── */

async function _autoLoadDupeResults(csvPath) {
  pruneCsvPath = csvPath || document.getElementById('prune-csv-path')?.value.trim() || '';
  prunePage    = 0;
  pruneSelected.clear();
  saState = null;

  try {
    await _loadPrunePage(0);
    await selectAllLower();

    // Switch card into review/prune phase
    document.getElementById('dupes-scan-phase').style.display    = 'none';
    document.getElementById('dupes-results-phase').style.display = 'block';
    const badge = document.getElementById('dupes-risk-badge');
    if (badge) { badge.textContent = 'Writes DB + Files'; badge.className = 'risk-badge danger'; }
    document.getElementById('step-duplicates')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  } catch (err) {
    console.error('Failed to load duplicate results:', err);
    alert('Could not load results: ' + err);
  }
}

function resetDupesScan() {
  document.getElementById('dupes-results-phase').style.display = 'none';
  document.getElementById('dupes-scan-phase').style.display    = '';
  const badge = document.getElementById('dupes-risk-badge');
  if (badge) { badge.textContent = 'Read-Only Scan'; badge.className = 'risk-badge safe'; }

  pruneCsvPath       = '';
  pruneTotalRemoveMb = 0;
  pruneSelected.clear();
  pruneGroups = [];
  const groupsEl = document.getElementById('prune-groups');
  if (groupsEl) groupsEl.innerHTML = '';
  const pgEl = document.getElementById('prune-pagination');
  if (pgEl) pgEl.style.display = 'none';
  _updatePruneSummary();
  _updateDupesStats();
}

function _updateDupesStats() {
  const grpEl = document.getElementById('dupes-stat-groups');
  const rmEl  = document.getElementById('dupes-stat-remove');
  const szEl  = document.getElementById('dupes-stat-size');
  if (!grpEl) return;
  grpEl.textContent = `${pruneTotalGroups.toLocaleString()} group${pruneTotalGroups !== 1 ? 's' : ''}`;
  rmEl.textContent  = `${pruneTotalRemove.toLocaleString()} to remove`;
  const gb = pruneTotalRemoveMb / 1024;
  szEl.textContent  = gb >= 1
    ? `${gb.toFixed(1)} GB recoverable`
    : `${Math.round(pruneTotalRemoveMb)} MB recoverable`;
}

function _syncCheckboxes() {
  document.querySelectorAll('#prune-groups input[type=checkbox]').forEach(cb => {
    const row  = cb.closest('[class^="prune-row"]');
    const path = row ? _rowPath(row) : null;
    if (path) cb.checked = pruneSelected.has(path);
  });
}

function _rowPath(row) {
  // extract path from the title attribute of .prune-fname
  const fname = row.querySelector('.prune-fname');
  return fname ? fname.title : null;
}

function _updateSaButtons() {
  document.getElementById('sa-best-btn') .classList.toggle('active-keep',  saState === 'best');
  document.getElementById('sa-lower-btn').classList.toggle('active-lower', saState === 'lower');
}

function _updatePruneSummary() {
  const n   = pruneSelected.size;
  const lbl = document.getElementById('prune-count-label');
  const sum = document.getElementById('prune-selected-summary');
  const btn = document.getElementById('btn-prune-start');

  lbl.textContent = n === 0 ? '0 files selected' : `${n} file${n > 1 ? 's' : ''} selected`;

  if (n === 0) {
    sum.innerHTML = 'Select files above to continue.';
    btn.disabled  = true;
  } else {
    sum.innerHTML = `<strong>${n}</strong> file${n > 1 ? 's' : ''} queued for removal`;
    btn.disabled  = false;
  }
}

async function previewFile(path) {
  try {
    await fetch('/api/open-file?path=' + encodeURIComponent(path));
  } catch(e) { alert('Could not open file: ' + e); }
}

/* ── Confirmation flow — 3 spatially separated steps ──────────────────────── */
// Each panel is at a different screen position.
// Each action button is at a different corner within its panel.
// User must physically move cursor between each step — no click-through.

function pruneStep1() {
  if (pruneSelected.size === 0) return;
  const n = pruneSelected.size;
  const perm = document.getElementById('prune-permanent-cb').checked;
  document.getElementById('c1-count').textContent = `${n} file${n > 1 ? 's' : ''}`;
  document.getElementById('c1-mode-note').textContent = perm
    ? '⚠ Permanent delete mode — files will be unlinked directly. This cannot be undone.'
    : 'Files will be moved to a recovery folder in Trash. Nothing is permanently deleted.';
  document.getElementById('c1-mode-note').style.color = perm ? 'var(--danger)' : '';
  document.getElementById('btn-execute-prune').textContent = perm
    ? 'Execute — Delete Permanently'
    : 'Execute — Move to Trash';
  _openConfirm('confirm-step1');
}

function pruneStep2() {
  _closeConfirm('confirm-step1');
  const list = document.getElementById('c2-file-list');
  list.innerHTML = '';
  [...pruneSelected].sort().forEach(p => {
    const div = document.createElement('div');
    div.className = 'confirm-file-item';
    div.textContent = p;
    list.appendChild(div);
  });
  // Show or hide the RB warning based on current status
  document.getElementById('prune-final-rb-block').classList.toggle('visible', rbRunning);
  _openConfirm('confirm-step2');
}

function _showPruneStatus(msg, isError) {
  const el = document.getElementById('prune-status-msg');
  if (!el) return;
  el.textContent = msg;
  el.style.display = 'block';
  el.style.background    = isError ? 'rgba(239,68,68,.15)'  : 'rgba(34,197,94,.15)';
  el.style.border        = isError ? '1px solid rgba(239,68,68,.4)' : '1px solid rgba(34,197,94,.4)';
  el.style.color         = isError ? 'var(--danger)' : 'var(--safe)';
  el.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

async function executePrune() {
  // Live RB check — surface the warning and block if open
  await refreshStatus();
  if (rbRunning) {
    document.getElementById('prune-final-rb-block').classList.add('visible');
    return;
  }

  // Guard: another operation is already in progress — make it visible
  if (isRunning) {
    _showPruneStatus('⚠ Another operation is still running. Wait for it to finish, then try again.', true);
    cancelPrune();
    return;
  }

  cancelPrune();   // close all confirm panels

  // Hide any previous status before starting
  const statusEl = document.getElementById('prune-status-msg');
  if (statusEl) statusEl.style.display = 'none';

  const paths   = [...pruneSelected];
  const permanent = document.getElementById('prune-permanent-cb').checked;

  // Stage the paths server-side to avoid blowing the 256 KB header limit
  // when passing thousands of file paths as a query string.
  let token;
  try {
    const res = await fetch('/api/prune/stage', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ paths, permanent, csv_path: pruneCsvPath }),
    });
    const data = await res.json();
    if (!res.ok || !data.token) throw new Error(data.error || 'stage failed');
    token = data.token;
  } catch (e) {
    _showPruneStatus(`✗ Could not stage prune — ${e.message}`, true);
    return;
  }

  const verb   = permanent ? 'deleted permanently' : 'moved to Trash';
  const label  = permanent ? 'deleted' : 'moved to Trash';
  const url    = `/api/run/prune?token=${encodeURIComponent(token)}`;
  runCommand(url, `Prune — ${paths.length} duplicate${paths.length > 1 ? 's' : ''} ${verb}`, (exitCode) => {
    if (exitCode === 0) {
      pruneSelected.clear();
      _updatePruneSummary();
      _showPruneStatus(`✓ Prune complete — ${paths.length} file${paths.length > 1 ? 's' : ''} ${label}. Check the report for details.`, false);
    } else {
      _showPruneStatus('✗ Prune failed — see the log panel (View Output) for details.', true);
    }
  });
}

function cancelPrune() {
  ['confirm-step1','confirm-step2'].forEach(_closeConfirm);
  document.getElementById('confirm-backdrop').classList.remove('open');
}

function _openConfirm(id) {
  document.getElementById(id).classList.add('open');
  document.getElementById('confirm-backdrop').classList.add('open');
}
function _closeConfirm(id) {
  document.getElementById(id).classList.remove('open');
}

function _esc(s) {
  return String(s)
    .replace(/&/g,'&amp;').replace(/</g,'&lt;')
    .replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

/* ── Owl / Glossary system ─────────────────────────────────────────────────── */
const GLOSSARY = [
  // ── Tech ──────────────────────────────────────────────────────────────────
  { id:'db',  cat:'Tech', term:'DB',
    short:'Database — where RekordBox stores everything',
    body:`<p><strong>Database</strong> — a structured file that stores information in organized tables, like a very powerful spreadsheet that the computer reads and writes directly.</p>
<p>RekordBox uses one file called <code>master.db</code> to remember your entire library: track names, BPM, key, playlists, cue points, loops — all of it lives in there.</p>
<p>Every write operation in SuperBox creates a timestamped backup of this file before touching it.</p>`},

  { id:'cli', cat:'Tech', term:'CLI',
    short:'Command-Line Interface — terminal window',
    body:`<p><strong>Command-Line Interface</strong> — the text window (Terminal on Mac) where you type instructions directly to the computer instead of clicking buttons in an app.</p>
<p>SuperBox's CLI is the actual engine doing the work. This web dashboard is just a control panel that talks to the engine so you never have to type commands yourself.</p>`},

  { id:'py',  cat:'Tech', term:'.py / Python',
    short:'The programming language SuperBox is built in',
    body:`<p><strong>Python</strong> — the programming language SuperBox is written in. You don't need to know it or read it.</p>
<p>What you do need: <strong>Python 3.12 or later</strong> installed on your Mac. If something won't start, a wrong Python version is usually the reason. Check with <code>python3 --version</code> in Terminal.</p>`},

  { id:'csv', cat:'Tech', term:'CSV',
    short:'Spreadsheet file — opens in Excel or Numbers',
    body:`<p><strong>Comma-Separated Values</strong> — a plain text file that any spreadsheet app (Excel, Numbers, Google Sheets) can open as a table.</p>
<p>The duplicate detector writes its results to a CSV so you can sort, filter, and decide what to remove at your own pace. SuperBox never deletes files — that decision is always yours.</p>`},

  { id:'sha', cat:'Tech', term:'SHA-256',
    short:'Content fingerprint — proves two files are identical',
    body:`<p>A mathematical fingerprint of a file's content. If two files have the same SHA-256, they are byte-for-byte identical — regardless of filename, location, or metadata.</p>
<p>The Relocate tool uses this to find files you moved and renamed. Even if you changed the filename completely, if the audio content is the same, it gets matched and the database path gets updated.</p>`},

  { id:'sse', cat:'Tech', term:'SSE',
    short:'How live output streams to your browser',
    body:`<p><strong>Server-Sent Events</strong> — the mechanism this app uses to send command output to your browser in real time as it happens.</p>
<p>When you click Execute and see lines appearing live in the output panel, that's SSE at work. The command is running on your computer; the results are being pushed to the page line by line.</p>`},

  { id:'path', cat:'Tech', term:'File Path',
    short:'The full address of a file on your computer',
    body:`<p>The exact location of a file on your drive, written out as a chain of folders.</p>
<p>Example: <code>/Volumes/YourDrive/Music/House/Track.aiff</code></p>
<p>RekordBox stores the path of every track in its database. If you move a file without telling RekordBox, the path in the database points to nowhere — the track shows as missing. The Relocate tool fixes this.</p>`},

  // ── Audio ──────────────────────────────────────────────────────────────────
  {
    id: 'bpm', cat: 'Audio', term: 'BPM',
    short: 'Beats Per Minute — how fast a track is',
    body: `<p><strong>Beats Per Minute</strong> — the tempo of a track. A kick drum at 128 BPM fires 128 times per minute.</p>
<p>RekordBox stores BPM internally as BPM × 100 (so 128.0 BPM is stored as 12800). SuperBox handles that conversion automatically so you never see raw database values.</p>
<p>Detection uses <strong>librosa</strong>, which analyzes the actual audio waveform for beat patterns — not guessing from the filename.</p>`},
  { id:'key', cat:'Audio', term:'Musical Key',
    short:'The harmonic "home base" of a track',
    body:`<p>The musical scale a track is built around — determines which other tracks it will sound harmonically compatible with when mixed.</p>
<p>SuperBox detects key using the <strong>Krumhansl-Schmuckler algorithm</strong> on the audio's chroma features. It understands all three common notations and stores whichever format your database already uses:</p>
<ul><li><strong>Standard</strong> — Am, C, F#m, Bb…</li>
<li><strong>Camelot</strong> — 1A, 8B, 11A…</li>
<li><strong>Open Key</strong> — 1m, 8d, 11m…</li></ul>`},

  { id:'lufs', cat:'Audio', term:'LUFS',
    short:'How loud audio actually sounds — the real measure',
    body:`<p><strong>Loudness Units relative to Full Scale</strong> — the correct way to measure perceived loudness, accounting for how human ears hear different frequencies.</p>
<p><strong>−8.0 LUFS</strong> is the DJ standard — it leaves headroom for the mixer and matches most commercial releases. A track at −14 LUFS will sound noticeably quieter at the same channel level on a CDJ.</p>
<p>Peak levels (waveform height) are a different, less useful measurement. LUFS is what your ears actually hear.</p>`},

  { id:'ebu', cat:'Audio', term:'EBU R128',
    short:'The international loudness measurement standard',
    body:`<p><strong>European Broadcasting Union Recommendation R128</strong> — the international standard defining how to measure integrated loudness correctly.</p>
<p>The same standard Spotify, YouTube, Apple Music, and broadcast TV use for their loudness normalization. SuperBox uses R128 analysis to measure your tracks and target them to −8.0 LUFS.</p>`},

  { id:'cbr', cat:'Audio', term:'CBR 320',
    short:'Highest-quality MP3 encoding setting',
    body:`<p><strong>Constant Bitrate at 320 kbps</strong> — the highest quality setting for MP3 encoding. Every second of audio uses the same amount of data.</p>
<p>When SuperBox normalizes an MP3, it re-encodes at 320 kbps CBR. This is still a lossy process — any re-encode of a lossy file costs some quality — which is why normalization is optional and having a backup first is strongly recommended.</p>
<p>AIFF and WAV files are re-encoded losslessly, so no quality loss at all.</p>`},

  { id:'aiff', cat:'Audio', term:'AIFF / AIF',
    short:'Lossless audio format — full quality, larger file',
    body:`<p><strong>Audio Interchange File Format</strong> — Apple's lossless audio format. Common in professional DJ libraries because it preserves full recording quality and supports embedded cue points that survive a drive wipe.</p>
<p>When SuperBox normalizes an AIFF it re-encodes losslessly at the same bit depth as your original — no generation loss whatsoever.</p>`},

  { id:'id3', cat:'Audio', term:'ID3 Tags',
    short:'Metadata embedded inside the audio file itself',
    body:`<p>The format used to store metadata <em>inside</em> audio files — title, artist, album, BPM, key, year, track number, and more.</p>
<p>When you see track info in RekordBox, Finder, or iTunes, you're reading ID3 tags. SuperBox writes BPM and key into these tags so the data <strong>travels with the file</strong>, not just in the database. If you ever re-import, the tags are already there.</p>`},

  { id:'fp', cat:'Audio', term:'Chromaprint / fpcalc',
    short:'Acoustic fingerprinting — identifies songs by sound',
    body:`<p><strong>Chromaprint</strong> is the fingerprinting library (used by AcoustID and MusicBrainz) that identifies recordings by their acoustic content — not their metadata.</p>
<p><code>fpcalc</code> is the command-line tool it ships with. SuperBox calls it to analyze the first 120 seconds of each file and generate a fingerprint. Two identical fingerprints = same recording, no matter what the files are named or what format they're in.</p>
<p>Requires <code>fpcalc</code> installed on your system: <code>brew install chromaprint</code></p>`},

  // ── RekordBox ──────────────────────────────────────────────────────────────
  { id:'mdb', cat:'RekordBox', term:'master.db',
    short:'RekordBox\'s main database file — back this up',
    body:`<p>The single SQLite file where RekordBox stores your entire library — every track, playlist, cue point, loop, hot cue color, and rating.</p>
<p>Locations:<br>
<code>~/Library/Pioneer/rekordbox/master.db</code> — your Mac<br>
<code>/Volumes/[drive]/PIONEER/Master/master.db</code> — your export drive</p>
<p><strong>Every SuperBox write operation creates a timestamped copy of this file in <code>~/rekordbox-toolkit/backups/</code> before touching it.</strong> The backup header in this app shows you when the last one was made.</p>`},

  { id:'cont', cat:'RekordBox', term:'DjmdContent',
    short:'The track table inside master.db',
    body:`<p>The database table where each track gets one row. Every attribute RekordBox knows about a track — title, artist, BPM, key, file path, bit depth, sample rate, cue points — lives here.</p>
<p>When you import, SuperBox writes rows to this table. When you relocate, it updates the <code>FolderPath</code> column. It's the heart of your library.</p>`},

  { id:'fp2', cat:'RekordBox', term:'FolderPath',
    short:'The stored file path in the database',
    body:`<p>The exact file path stored in <code>DjmdContent</code> pointing to where a track lives on disk.</p>
<p>When you move files to a new folder or drive, the old path no longer resolves — RekordBox shows a broken link icon. The Relocate tool fixes this by updating <code>FolderPath</code> values to where the files actually are now.</p>`},

  { id:'cdj', cat:'RekordBox', term:'CDJ / XDJ',
    short:'Pioneer hardware DJ players used in clubs',
    body:`<p>Pioneer's professional media players — the industry standard hardware in most clubs, festivals, and touring setups.</p>
<p>These players read directly from the exported <code>master.db</code> on your USB drive or rekordbox link. A corrupt or broken database means tracks won't load mid-set. This is why the backup-before-every-write rule is not negotiable.</p>`},

  { id:'cam', cat:'RekordBox', term:'Camelot / Open Key',
    short:'Harmonic mixing notation systems',
    body:`<p>Two notation systems for musical keys designed to make harmonic mixing easy by replacing key names with numbers and letters.</p>
<p><strong>Camelot</strong> — 1A through 12B. Adjacent numbers are harmonically compatible.<br>
<strong>Open Key</strong> — 1m through 12d. Same concept, different notation.</p>
<p>SuperBox maps all notations — including standard (Am, C#, F#m, etc.) — to whichever format your database already uses.</p>`},

  // ── SuperBox ───────────────────────────────────────────────────────────────
  { id:'dry', cat:'SuperBox', term:'Dry Run',
    short:'Preview mode — shows what would happen, writes nothing',
    body:`<p>Running a command with dry run enabled shows you exactly what <em>would</em> happen — how many tracks would be imported, what paths would change — without writing a single byte to the database.</p>
<p><strong>Always run the Preview Import step before the real import.</strong> If the track count looks wrong, you haven't broken anything yet. The dry run is free.</p>`},

  { id:'bat', cat:'SuperBox', term:'Batch Commit',
    short:'Writing changes in chunks of 250',
    body:`<p>Instead of writing one track at a time (slow) or all tracks at once (risky), SuperBox collects 250 changes and writes them as a single transaction.</p>
<p>If that transaction fails, the entire chunk rolls back — you never end up with 137 tracks written and 113 missing in a half-finished state.</p>`},

  { id:'rol', cat:'SuperBox', term:'Rollback',
    short:'Auto-undo on failure — prevents partial writes',
    body:`<p>If any unhandled error occurs during a write operation, the database transaction is automatically cancelled — every pending change in that session is undone as if it never started.</p>
<p>This is the mechanism that prevents partial imports. Either a full batch of 250 tracks lands cleanly, or none of them do. You will never have a half-imported library.</p>`},

  { id:'orp', cat:'SuperBox', term:'Orphan File',
    short:'File on disk that RekordBox doesn\'t know about',
    body:`<p>An audio file that exists in your music folder but has no matching row in the RekordBox database — RekordBox doesn't know it's there.</p>
<p>Orphans appear in the Audit report. Common causes: files copied directly into the folder without going through an import, or leftovers from a failed previous import. The import step is how you bring them in.</p>`},

  { id:'fuz', cat:'SuperBox', term:'Fuzzy Match',
    short:'Approximate name matching — catches near-misses',
    body:`<p>Instead of requiring an exact string match, fuzzy matching scores text similarity and accepts anything above a threshold.</p>
<p>SuperBox uses it in two places:</p>
<ul><li><strong>Playlist linking</strong> — folder name vs. playlist name, 85% threshold</li>
<li><strong>File relocation</strong> — filename stem similarity, 90% threshold</li></ul>
<p>Higher threshold = stricter = fewer false positives, but more unmatched items. The defaults are tuned for DJ library naming conventions.</p>`},

  { id:'rarp', cat:'SuperBox', term:'RARP',
    short:'Duplicate ranking: Pioneer Numbered → MIK → Raw',
    body:`<p>The hierarchy used to recommend which copy to keep when duplicate tracks are found:</p>
<ul>
<li><strong>PN (Pioneer Numbered)</strong> — filename starts with digits + separator, e.g. <code>01 - Title</code>. Suggests it came from a curated, numbered source.</li>
<li><strong>MIK (Mixed In Key tagged)</strong> — has a <code>TKEY</code>/<code>initialkey</code> tag already written by Mix In Key.</li>
<li><strong>RAW</strong> — neither. Likely an unprocessed download.</li>
</ul>
<p>The CSV marks the top-ranked file in each group as KEEP. You review and make the final call — SuperBox never deletes anything.</p>`},

  { id:'bak', cat:'SuperBox', term:'.bak File',
    short:'Temporary safety copy kept during audio processing',
    body:`<p>When normalizing loudness, the original file is renamed to <code>filename.mp3.bak</code> before the replacement is written.</p>
<p>The <code>.bak</code> is only deleted after SuperBox confirms the new file is valid and readable using <code>soundfile</code>. If anything fails, your original is still there — just rename it to remove <code>.bak</code>.</p>
<p>If you see leftover <code>.bak</code> files after an interrupted run, treat them as your originals. Verify the non-<code>.bak</code> version is intact before removing them.</p>`},

  { id:'norm', cat:'SuperBox', term:'Normalization',
    short:'Matching loudness levels across your library',
    body:`<p>The process of analyzing each track's integrated loudness (LUFS) and re-encoding it so every track hits the same target level — <strong>−8.0 LUFS</strong>.</p>
<p>Why it matters: without normalization, different tracks have different volumes. On CDJs you end up riding the channel gain between tracks during a mix. Normalized libraries let you keep gain at unity and focus on the mix.</p>
<p>This is the highest-risk operation in the toolkit because it rewrites audio files. The <code>.bak</code> safety system means your originals are protected, but an independent drive backup first is strongly recommended.</p>`},
];

/* ── Owl interaction ──────────────────────────────────────────────────────── */
let owlHoverTimer  = null;
let owlCardsActive = false;
const pinnedCards  = new Map();   // id → DOM element
let cardZ          = 1000;

function _buildOwlList() {
  const list = document.getElementById('owl-panel-list');
  if (list.children.length) return;
  const groups = ['Tech','Audio','RekordBox','SuperBox'];
  groups.forEach(g => {
    const lbl = document.createElement('div');
    lbl.className = 'owl-group-label';
    lbl.textContent = g;
    list.appendChild(lbl);
    GLOSSARY.filter(t => t.cat === g).forEach(t => {
      const row = document.createElement('div');
      row.className = 'owl-item';
      row.id = `owl-item-${t.id}`;
      row.innerHTML = `<span class="owl-term">${t.term}</span><span class="owl-short">${t.short}</span>`;
      row.onclick = e => { e.stopPropagation(); toggleCard(t.id); };
      list.appendChild(row);
    });
  });
}

function owlHoverIn() {
  clearTimeout(owlHoverTimer);
  _buildOwlList();
  document.getElementById('owl-hover-panel').classList.add('visible');
}
function owlHoverOut() {
  owlHoverTimer = setTimeout(() =>
    document.getElementById('owl-hover-panel').classList.remove('visible'), 220);
}

function owlClick() {
  document.getElementById('owl-hover-panel').classList.remove('visible');
  if (pinnedCards.size > 0) {
    // dismiss all cards
    pinnedCards.forEach(c => c.remove());
    pinnedCards.clear();
    document.querySelectorAll('.owl-item').forEach(i => i.classList.remove('pinned'));
    document.getElementById('owl-btn').classList.remove('active');
    owlCardsActive = false;
  }
}

function toggleCard(id) {
  pinnedCards.has(id) ? closeCard(id) : openCard(id);
}

function openCard(id) {
  const t = GLOSSARY.find(x => x.id === id);
  if (!t) return;

  // cascading spawn positions — stays inside viewport, avoids top nav bars
  const col  = pinnedCards.size % 3;
  const row  = Math.floor(pinnedCards.size / 3) % 4;
  const top  = 130 + row  * 44;
  const left =  20 + col  * 308;

  const card = document.createElement('div');
  card.className = 'gls-card';
  card.style.cssText = `top:${top}px;left:${left}px;z-index:${++cardZ}`;
  card.innerHTML = `
    <div class="gls-card-head">
      <span class="gls-card-term">${t.term}</span>
      <span class="gls-card-cat">${t.cat}</span>
      <button class="gls-card-close" onclick="closeCard('${id}')">✕</button>
    </div>
    <div class="gls-card-body">${t.body}</div>`;
  card.addEventListener('mouseenter', () => { card.style.zIndex = ++cardZ; });
  document.body.appendChild(card);
  pinnedCards.set(id, card);

  const item = document.getElementById(`owl-item-${id}`);
  if (item) item.classList.add('pinned');
  document.getElementById('owl-btn').classList.add('active');
  owlCardsActive = true;
}

function closeCard(id) {
  const c = pinnedCards.get(id);
  if (c) { c.remove(); pinnedCards.delete(id); }
  const item = document.getElementById(`owl-item-${id}`);
  if (item) item.classList.remove('pinned');
  if (pinnedCards.size === 0) {
    document.getElementById('owl-btn').classList.remove('active');
    owlCardsActive = false;
  }
}

document.getElementById('settings-backdrop').addEventListener('click', function(e) {
  if (e.target === this) closeSettings();
});
document.getElementById('report-modal-backdrop').addEventListener('click', function(e) {
  if (e.target === this) closeReportModal();
});

document.addEventListener('keydown', e => {
  if (e.key === 'Escape') {
    closeSettings();
    closeReportModal();
  }
  if ((e.metaKey || e.ctrlKey) && e.key === 'b') {
    e.preventDefault();
    toggleFileBrowser();
  }
});

/* patch openLog / closeLog to shift owl button above the log panel */
const _origOpenLog  = openLog;
const _origCloseLog = closeLog;
openLog  = function(t) { _origOpenLog(t);  document.body.classList.add('log-open');    };
closeLog = function()   { _origCloseLog();  document.body.classList.remove('log-open'); };

/* ── Drag-and-drop path extraction ─────────────────────────────────────── */
/* Three strategies in priority order:                                        */
/* 1. file.path   — Chromium exposes the real OS path directly on the File   */
/*                  object when dropped from Finder. Most reliable on macOS.  */
/* 2. text/uri-list — standard HTML5 drag format: file:///path. Works when   */
/*                    Finder includes URI list data (not always guaranteed).   */
/* 3. text/plain  — fallback for terminal-style drags (absolute paths only). */
function _extractDropPath(e) {
  // Strategy 1: Chromium File.path — real filesystem path, no decoding needed
  const files = e.dataTransfer.files;
  if (files && files.length > 0 && files[0].path) {
    return files[0].path.replace(/\/$/, '');
  }
  // Strategy 2: text/uri-list (standard HTML5, Finder usually provides this)
  const uriList = e.dataTransfer.getData('text/uri-list');
  if (uriList) {
    const first = uriList.trim().split(/\r?\n/).find(l => /^file:\/\//i.test(l) && !l.startsWith('#'));
    if (first) return decodeURIComponent(first.replace(/^file:\/\/[^/]*/i, '').replace(/\/$/, ''));
  }
  // Strategy 3: text/plain fallback (terminal drags, absolute paths only)
  const plain = e.dataTransfer.getData('text/plain');
  if (plain) {
    const t = plain.trim();
    if (t.startsWith('/') || t.startsWith('~')) return t.replace(/\/$/, '');
  }
  return null;
}

/* ── Global drag-state class ────────────────────────────────────────────── */
/* Adds body.has-drag while a drag is in flight so CSS can highlight all     */
/* available drop zones simultaneously.                                       */
/* Also pre-fetches Finder's selection on the very first dragenter — at that  */
/* point Finder still has the dragged item selected (pywebview hasn't taken   */
/* full focus yet). This cached path is used as a fallback on drop, because   */
/* by the time drop fires pywebview has focused and Finder clears its         */
/* selection, causing the post-drop osascript query to return empty.          */
let _docDragCount = 0;
let _finderPathCache = null;   // prefetched on first dragenter, consumed on drop
let _finderPrefetching = false;
document.addEventListener('dragenter', () => {
  if (++_docDragCount === 1) {
    document.body.classList.add('has-drag');
    // Prefetch Finder selection while the item is still selected in Finder
    if (!_finderPrefetching) {
      _finderPrefetching = true;
      _finderPathCache = null;
      fetch('/api/finder-selection?source=drop')
        .then(r => r.json())
        .then(d => { _finderPathCache = d.path || null; })
        .catch(() => {})
        .finally(() => { _finderPrefetching = false; });
    }
  }
});
document.addEventListener('dragleave', () => {
  if (--_docDragCount <= 0) { _docDragCount = 0; document.body.classList.remove('has-drag'); }
});
// Capture-phase drop on document: prevent Chrome from navigating to the
// dropped file (its default behaviour) and reset the drag-state counter.
// Zone handlers still call e.preventDefault() individually — this is the
// safety net for any drop that lands outside a wired zone.
document.addEventListener('dragover', e => e.preventDefault());
document.addEventListener('drop', e => {
  e.preventDefault();
  _docDragCount = 0;
  document.body.classList.remove('has-drag');
}, true);

/* ── Folder pill zone system ────────────────────────────────────────────────
   Replaces single-path text inputs for all "source folder" type cards.
   Each zone uses CAPTURE-phase drag listeners so the inner <input> can never
   absorb the drop event before the zone sees it. A drag counter correctly
   tracks enter/leave across child elements without false positives.
   Dropped or typed paths appear as removable pills; duplicates are rejected.  */

function addFolderPill(pillsId, fullPath) {
  const container = document.getElementById(pillsId);
  if (!container) return;
  // Deduplicate — flash existing pill amber and bail rather than adding a copy
  const dupe = Array.from(container.querySelectorAll('.folder-pill'))
    .find(p => p.dataset.path === fullPath);
  if (dupe) {
    dupe.classList.remove('pill-already');
    void dupe.offsetWidth; // force reflow so re-adding the class restarts animation
    dupe.classList.add('pill-already');
    dupe.addEventListener('animationend', () => dupe.classList.remove('pill-already'), { once: true });
    return;
  }
  const name = fullPath.replace(/\/+$/, '').split('/').pop() || fullPath;
  const pill  = document.createElement('span');
  pill.className    = 'folder-pill';
  pill.title        = fullPath;
  pill.dataset.path = fullPath;
  pill.innerHTML    =
    `<span class="folder-pill-name">${name}</span>` +
    `<button class="folder-pill-x" type="button" title="Remove ${name}">✕</button>`;
  pill.querySelector('.folder-pill-x').addEventListener('click', () => pill.remove());
  container.appendChild(pill);
}

function getFolderPaths(pillsId) {
  const container = document.getElementById(pillsId);
  if (!container) return [];
  return Array.from(container.querySelectorAll('.folder-pill'))
    .filter(p => !p.classList.contains('library-pill'))
    .map(p => p.dataset.path).filter(Boolean);
}

/* Single-path drop zone — same glowing visual as setupFolderZone but populates
   a plain text input directly rather than a pills container.
   Used by: Relocate (old + new), Prune CSV, Organize target.              */
function setupSinglePathZone(zoneId, inputId) {
  const zone  = document.getElementById(zoneId);
  const input = document.getElementById(inputId);
  if (!zone || !input || zone.dataset.zoneReady) return;
  zone.dataset.zoneReady = '1';

  let _dc = 0;

  zone.addEventListener('dragenter', e => {
    e.preventDefault();
    if (++_dc === 1) zone.classList.add('drag-over');
  }, true);

  zone.addEventListener('dragleave', () => {
    if (--_dc <= 0) { _dc = 0; zone.classList.remove('drag-over'); }
  }, true);

  zone.addEventListener('dragover', e => {
    e.preventDefault();
    e.stopPropagation();
    e.dataTransfer.dropEffect = 'copy';
  }, true);

  zone.addEventListener('drop', async e => {
    e.preventDefault();
    e.stopPropagation();
    _dc = 0;
    zone.classList.remove('drag-over');
    let path = _extractDropPath(e);
    if (path) {
      input.value = path;
      input.dispatchEvent(new Event('input', { bubbles: true }));
      _markZoneDropSuccess(zone);
    } else if (e.dataTransfer.files.length > 0 || e.dataTransfer.types.length > 0) {
      path = await _recoverDroppedPath();
      if (path) {
        input.value = path;
        input.dispatchEvent(new Event('input', { bubbles: true }));
        _markZoneDropSuccess(zone);
      } else {
        showToast('Could not read the dropped folder path.', 'error');
      }
    }
  }, true);
}

function setupFolderZone(zoneId, pillsId, textId) {
  const zone = document.getElementById(zoneId);
  const text = document.getElementById(textId);
  if (!zone || !text || zone.dataset.zoneReady) return;
  zone.dataset.zoneReady = '1';

  let _dc = 0; // drag-enter counter — reliably tracks nested enter/leave pairs

  const tryAdd = (val) => {
    const p = decodeURIComponent(val.replace(/^file:\/\/[^/]*/i, '')).trim().replace(/\/$/, '');
    if (p) { addFolderPill(pillsId, p); text.value = ''; }
  };

  // ── Capture-phase listeners ──────────────────────────────────────────────
  // Using capture (third arg = true) means the zone intercepts dragover/drop
  // BEFORE the child <input> element sees them. Without this, WebKit routes
  // the drop to the text input's native handler and it never reaches us.

  zone.addEventListener('dragenter', e => {
    e.preventDefault();
    if (++_dc === 1) zone.classList.add('drag-over');
  }, true);

  zone.addEventListener('dragleave', () => {
    if (--_dc <= 0) { _dc = 0; zone.classList.remove('drag-over'); }
  }, true);

  zone.addEventListener('dragover', e => {
    e.preventDefault();
    e.stopPropagation();
    e.dataTransfer.dropEffect = 'copy';
  }, true);

  zone.addEventListener('drop', async e => {
    e.preventDefault();
    e.stopPropagation();
    _dc = 0;
    zone.classList.remove('drag-over');
    let path = _extractDropPath(e);
    if (path) {
      addFolderPill(pillsId, path);
      _markZoneDropSuccess(zone);
    } else if (e.dataTransfer.files.length > 0 || e.dataTransfer.types.length > 0) {
      path = await _recoverDroppedPath();
      if (path) {
        addFolderPill(pillsId, path);
        _markZoneDropSuccess(zone);
      } else {
        showToast('Could not read the dropped folder path.', 'error');
      }
    }
  }, true);

  // ── Keyboard / button add ────────────────────────────────────────────────
  text.addEventListener('keydown', e => {
    if (e.key === 'Enter') { e.preventDefault(); tryAdd(text.value); }
  });
}

/* Per-zone add-button handlers */
function auditZoneAdd()     { const t = document.getElementById('audit-zone-text');     if (t?.value.trim()) { addFolderPill('audit-pills',     t.value.trim()); t.value = ''; } }
function processZoneAdd()   { const t = document.getElementById('process-zone-text');   if (t?.value.trim()) { addFolderPill('process-pills',   t.value.trim()); t.value = ''; } }
function dupesZoneAdd()     { const t = document.getElementById('dupes-zone-text');     if (t?.value.trim()) { addFolderPill('dupes-pills',     t.value.trim()); t.value = ''; } }
function normalizeZoneAdd() { const t = document.getElementById('normalize-zone-text'); if (t?.value.trim()) { addFolderPill('normalize-pills', t.value.trim()); t.value = ''; } }
function convertZoneAdd()   { const t = document.getElementById('convert-zone-text');   if (t?.value.trim()) { addFolderPill('convert-pills',   t.value.trim()); t.value = ''; } }
function importZoneAdd()    { const t = document.getElementById('import-zone-text');    if (t?.value.trim()) { addFolderPill('import-pills',    t.value.trim()); t.value = ''; } }
function organizeZoneAdd()  { const t = document.getElementById('organize-zone-text');  if (t?.value.trim()) { addFolderPill('organize-source-pills', t.value.trim()); t.value = ''; } }
function relocateOldZoneAdd() { const t = document.getElementById('relocate-old-zone-text'); if (t?.value.trim()) { addFolderPill('relocate-old-pills', t.value.trim()); t.value = ''; } }
function linkZoneAdd()      { const t = document.getElementById('link-zone-text');      if (t?.value.trim()) { addFolderPill('link-pills',      t.value.trim()); t.value = ''; } }
function noveltyZoneAdd()   { const t = document.getElementById('novelty-zone-text');   if (t?.value.trim()) { addFolderPill('novelty-pills',   t.value.trim()); t.value = ''; } }

/* Browse buttons — opens the native folder picker dialog.
   Prefers window.pywebview.api.pick_folder() when running inside the
   PyInstaller bundle (pywebview exposes the _Api class from main.py).
   Falls back to /api/pick-folder (osascript choose folder) in dev mode. */
async function _nativePick() {
  if (window.pywebview && window.pywebview.api && window.pywebview.api.pick_folder) {
    try {
      const path = await window.pywebview.api.pick_folder();
      return path || null;
    } catch (e) {
      console.warn('[_nativePick] pywebview api error, falling back:', e);
    }
  }
  const r = await fetch('/api/pick-folder');
  const d = await r.json();
  return d.path || null;
}
async function pickFolderFor(pillsId) {
  const path = await _nativePick();
  if (path) addFolderPill(pillsId, path);
}
async function pickPathFor(inputId) {
  const path = await _nativePick();
  if (path) {
    const el = document.getElementById(inputId);
    if (el) { el.value = path; el.dispatchEvent(new Event('input', { bubbles: true })); }
  }
}
/* Drop fallback — reads Finder's selection (which still holds the dragged item
   immediately after a drop), so the user never has to navigate twice.
   source=drop tells the server not to open a picker dialog if Finder returns
   nothing — on some drops pywebview focuses before osascript runs and Finder's
   selection is momentarily empty. Silently returns null rather than prompting. */
/* ── Library root indicator pill ────────────────────────────────────────────
   A dimmed, non-removable pill that marks the configured library root.
   Appears at the front of every pill zone that defaults to the music root.
   getFolderPaths() includes it naturally (it carries data-path).            */

let _libraryRoot = '';   // set by prefillDefaults once /api/config loads

function addLibraryPill(pillsId, path) {
  if (!path) return;
  const container = document.getElementById(pillsId);
  if (!container) return;
  // Update existing pill rather than duplicating
  const existing = container.querySelector('.library-pill');
  if (existing) {
    existing.dataset.path = path;
    const name = path.replace(/\/+$/, '').split('/').pop() || path;
    const nameEl = existing.querySelector('.folder-pill-name');
    if (nameEl) nameEl.textContent = `📍 ${name}`;
    existing.title = `Library root: ${path}`;
    return;
  }
  const name = path.replace(/\/+$/, '').split('/').pop() || path;
  const pill = document.createElement('span');
  pill.className = 'folder-pill library-pill';
  pill.title = `Library root: ${path}`;
  pill.dataset.path = path;
  pill.innerHTML = `<span class="folder-pill-name">📍 ${name}</span>`;
  container.insertBefore(pill, container.firstChild);
}

function _refreshLibraryPills(newRoot) {
  ['process-pills','dupes-pills','normalize-pills','convert-pills',
   'import-pills','link-pills','organize-source-pills'].forEach(id => addLibraryPill(id, newRoot));
}

async function setMusicRoot(newPath) {
  document.getElementById('sb-set-root-banner')?.remove();
  try {
    const r = await fetch('/api/config/set-music-root', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: newPath }),
    });
    const d = await r.json();
    if (d.ok) {
      _libraryRoot = newPath;
      _refreshLibraryPills(newPath);
      showToast(`Library root → ${newPath.split('/').pop() || newPath}`, 'success');
    } else {
      showToast(`Could not update root: ${d.error || 'unknown error'}`, 'error');
    }
  } catch (e) {
    showToast('Failed to update library root', 'error');
  }
}

function _promptSetLibraryRoot(newPath) {
  if (!newPath || newPath === _libraryRoot) return;
  const name = newPath.replace(/\/+$/, '').split('/').pop() || newPath;
  let banner = document.getElementById('sb-set-root-banner');
  if (!banner) {
    banner = document.createElement('div');
    banner.id = 'sb-set-root-banner';
    document.body.appendChild(banner);
  }
  Object.assign(banner.style, {
    position:'fixed', bottom:'calc(var(--log-h) + var(--scan-bar-h) + 14px)',
    left:'50%', transform:'translateX(-50%)', zIndex:'1200',
    padding:'11px 16px', borderRadius:'10px',
    background:'rgba(14,14,26,.97)',
    border:'1px solid rgba(129,140,248,.35)',
    boxShadow:'0 8px 32px rgba(0,0,0,.6)',
    display:'flex', alignItems:'center', gap:'12px',
    fontSize:'.84rem', color:'var(--text)',
    maxWidth:'min(660px,92vw)',
  });
  // sanitise path for inline onclick
  const safe = newPath.replace(/\\/g,'\\\\').replace(/'/g,"\\'");
  banner.innerHTML = `
    <span style="flex:1">📍 Organize moved files to <strong>${name}</strong>. Update the library root?</span>
    <button class="btn btn-neon" style="padding:5px 14px;font-size:.8rem;white-space:nowrap"
            onclick="setMusicRoot('${safe}')">Set Root</button>
    <button class="btn btn-ghost" style="padding:5px 10px;font-size:.8rem"
            onclick="document.getElementById('sb-set-root-banner')?.remove()">Dismiss</button>
  `;
}

async function _resolveDropPath() {
  // Use the path prefetched at dragenter — it was read before pywebview took
  // focus and Finder cleared its selection. If the prefetch is still in flight,
  // wait briefly for it; if it already finished, consume the cached value.
  if (_finderPrefetching) await new Promise(res => setTimeout(res, 200));
  if (_finderPathCache) {
    const p = _finderPathCache;
    _finderPathCache = null;
    return p;
  }
  // Cache miss (e.g. prefetch failed or was too slow) — fall back to a fresh query
  try {
    const r = await fetch('/api/finder-selection?source=drop');
    const d = await r.json();
    if (d.path) return d.path;
    // One retry after a short delay
    await new Promise(res => setTimeout(res, 400));
    const r2 = await fetch('/api/finder-selection?source=drop');
    const d2 = await r2.json();
    return d2.path || null;
  } catch { return null; }
}

function _markZoneDropSuccess(zone) {
  if (!zone) return;
  zone.classList.add('drop-success');
  zone.addEventListener('animationend', () => zone.classList.remove('drop-success'), { once: true });
}

async function _recoverDroppedPath() {
  const path = await _resolveDropPath();
  if (path) return path;
  showToast('Drop path was blocked by macOS. Choose the folder once to complete the drop.', 'neutral');
  return await _nativePick();
}

async function dropFolderFor(pillsId) {
  const path = await _recoverDroppedPath();
  if (path) addFolderPill(pillsId, path);
}
async function dropPathFor(inputId) {
  const path = await _recoverDroppedPath();
  if (path) {
    const el = document.getElementById(inputId);
    if (el) { el.value = path; el.dispatchEvent(new Event('input', { bubbles: true })); }
  }
}

function runAudit() {
  const paths = getFolderPaths('audit-pills');
  if (!paths.length) { alert('Add at least one folder path to scan.'); return; }
  const savePhys = document.getElementById('audit-save-physical')?.checked ? '1' : '0';
  const p = new URLSearchParams();
  p.set('save_physical', savePhys);
  // 'paths' param — api_audit() uses first as --root, rest as --also-scan
  paths.forEach(path => p.append('paths', path));
  runCommand(`/api/run/audit?${p.toString()}`, 'Audit — Database + Physical Scan', null, true);
}

/* ── Legacy single-input drop zones (relocate, import, link, settings, etc.) */
function setupDropZone(input) {
  if (!input || input.dataset.dropReady) return;
  input.dataset.dropReady = '1';

  if (!input.parentElement.classList.contains('drop-wrap')) {
    const wrap = document.createElement('div');
    wrap.className = 'drop-wrap';
    if (input.style.flex) wrap.style.flex = input.style.flex;
    input.parentNode.insertBefore(wrap, input);
    wrap.appendChild(input);
    const badge = document.createElement('span');
    badge.className = 'drop-badge';
    badge.textContent = '⤵ drop';
    wrap.appendChild(badge);
  }

  // Attach listeners to the wrap (not the input) using capture phase so we
  // intercept events before WebKit routes them to the input's native handler.
  // This also means dropping on the ⤵ badge works correctly — the wrap sees
  // the event regardless of which child element the pointer is over.
  const wrap = input.closest('.drop-wrap');
  let _dc = 0; // drag counter — tracks nested enter/leave correctly

  wrap.addEventListener('dragenter', e => {
    e.preventDefault();
    if (++_dc === 1) wrap.classList.add('drop-active');
  }, true);

  wrap.addEventListener('dragleave', () => {
    if (--_dc <= 0) { _dc = 0; wrap.classList.remove('drop-active'); }
  }, true);

  wrap.addEventListener('dragover', e => {
    e.preventDefault();
    e.stopPropagation();
    e.dataTransfer.dropEffect = 'copy';
  }, true);

  wrap.addEventListener('drop', async e => {
    e.preventDefault();
    e.stopPropagation();
    _dc = 0;
    wrap.classList.remove('drop-active');
    let path = _extractDropPath(e);
    if (path) {
      input.value = path;
      input.dispatchEvent(new Event('input', { bubbles: true }));
      wrap.classList.add('drop-filled');
      wrap.addEventListener('animationend', () => wrap.classList.remove('drop-filled'), { once: true });
    } else if (e.dataTransfer.files.length > 0 || e.dataTransfer.types.length > 0) {
      path = await _recoverDroppedPath();
      if (path) {
        input.value = path;
        input.dispatchEvent(new Event('input', { bubbles: true }));
        wrap.classList.add('drop-filled');
        wrap.addEventListener('animationend', () => wrap.classList.remove('drop-filled'), { once: true });
      } else {
        showToast('Could not read the dropped folder path.', 'error');
      }
    }
  }, true);
}

function setupAllDropZones() {
  // No legacy plain inputs remain — all zones now use setupSinglePathZone / setupFolderZone
}

/* ── Multi-path textarea drop zone: dropped paths are appended as new lines ── */
function setupMultiDropZone(textarea) {
  if (!textarea || textarea.dataset.dropReady) return;
  textarea.dataset.dropReady = '1';

  // Ensure a .drop-wrap parent exists (same structure as setupDropZone)
  if (!textarea.parentElement.classList.contains('drop-wrap')) {
    const wrap  = document.createElement('div');
    wrap.className = 'drop-wrap';
    textarea.parentNode.insertBefore(wrap, textarea);
    wrap.appendChild(textarea);
    const badge  = document.createElement('span');
    badge.className   = 'drop-badge';
    badge.textContent = '⤵ drop';
    badge.style.top   = '8px';
    wrap.appendChild(badge);
  }

  const wrap = textarea.closest('.drop-wrap');
  let _dc = 0;

  wrap.addEventListener('dragenter', e => {
    e.preventDefault();
    if (++_dc === 1) wrap.classList.add('drop-active');
  }, true);
  wrap.addEventListener('dragleave', () => {
    if (--_dc <= 0) { _dc = 0; wrap.classList.remove('drop-active'); }
  }, true);
  wrap.addEventListener('dragover', e => {
    e.preventDefault(); e.stopPropagation(); e.dataTransfer.dropEffect = 'copy';
  }, true);
  wrap.addEventListener('drop', async e => {
    e.preventDefault(); e.stopPropagation();
    _dc = 0; wrap.classList.remove('drop-active');
    let path = _extractDropPath(e);
    if (!path && (e.dataTransfer.files.length > 0 || e.dataTransfer.types.length > 0)) {
      path = await _recoverDroppedPath();
    }
    if (path) {
      const existing = textarea.value.trim();
      textarea.value  = existing ? existing + '\n' + path : path;
      textarea.dispatchEvent(new Event('input', { bubbles: true }));
      _markZoneDropSuccess(wrap);
    }
  }, true);
}

/* ── DB Rail panel open/close ─────────────────────────────────────────────── */
const DB_PANEL_TITLES = {
  audit:    'Audit Library',
  relocate: 'Relocate — Fix Broken Paths',
  import:   'Import Tracks',
  link:     'Link Playlists',
};
let _dbPanelActive = null;

function openDbPanel(tool) {
  // Deactivate all sections + rail buttons
  document.querySelectorAll('.db-panel-section').forEach(s => s.classList.remove('active'));
  document.querySelectorAll('.db-rail-btn').forEach(b => b.classList.remove('active'));

  const section = document.getElementById('db-panel-' + tool);
  const btn     = document.getElementById('rail-btn-' + tool);
  if (!section) return;

  section.classList.add('active');
  if (btn) btn.classList.add('active');
  document.getElementById('db-panel-title').textContent = DB_PANEL_TITLES[tool] || 'DB Tools';

  document.getElementById('db-panel').classList.add('open');
  document.getElementById('db-panel-backdrop').classList.add('open');
  _dbPanelActive = tool;
}

function closeDbPanel() {
  document.getElementById('db-panel').classList.remove('open');
  document.getElementById('db-panel-backdrop').classList.remove('open');
  document.querySelectorAll('.db-rail-btn').forEach(b => b.classList.remove('active'));
  _dbPanelActive = null;
}

document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && _dbPanelActive) closeDbPanel();
});

/* Wire all drop zones once the DOM is confirmed ready */
document.addEventListener('DOMContentLoaded', () => {
  // Folder-pill zones (multi-path, capture-phase drag)
  setupFolderZone('audit-zone',     'audit-pills',     'audit-zone-text');
  setupFolderZone('process-zone',   'process-pills',   'process-zone-text');
  setupFolderZone('dupes-zone',     'dupes-pills',     'dupes-zone-text');
  setupFolderZone('normalize-zone', 'normalize-pills', 'normalize-zone-text');
  setupFolderZone('convert-zone',   'convert-pills',   'convert-zone-text');
  setupFolderZone('novelty-zone',   'novelty-pills',   'novelty-zone-text');
  setupFolderZone('import-zone',    'import-pills',    'import-zone-text');
  setupFolderZone('link-zone',      'link-pills',      'link-zone-text');
  setupFolderZone('organize-zone',  'organize-source-pills', 'organize-zone-text');
  // Single-path zones (visual feedback + Browse/drop, no pills)
  setupFolderZone('relocate-old-zone', 'relocate-old-pills', 'relocate-old-zone-text');
  setupSinglePathZone('relocate-new-zone',    'relocate-new');
  setupSinglePathZone('organize-target-zone', 'organize-target');
  setupSinglePathZone('novelty-dest-zone',    'novelty-dest');
  setupAllDropZones();
  normPreviewSetupObserver();
});

/* ── Normalize loudness preview player ──────────────────────────────────────
   4 glowing sample rows: quietest original, quietest normalised,
   loudest original, loudest normalised.
   Triggered automatically whenever a folder is added to normalize-pills.     */

let _normPreviewJobId = null;
let _normPreviewTimer = null;
let _normActiveAudio  = null;   // currently-playing Audio element

function normPreviewSetupObserver() {
  const pills = document.getElementById('normalize-pills');
  if (!pills) return;
  new MutationObserver(() => {
    const paths = getFolderPaths('normalize-pills');
    if (paths.length > 0) _normPreviewStart(paths[0]);
    else _normPreviewReset();
  }).observe(pills, { childList: true });
}

async function _normPreviewStart(folderPath) {
  clearTimeout(_normPreviewTimer);
  _normPreviewSetStatus('Scanning tracks…');
  _normPreviewSetSkeleton();

  let resp, data;
  try {
    resp = await fetch('/api/normalize/preview', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: folderPath }),
    });
    data = await resp.json();
  } catch (_) {
    _normPreviewSetStatus('Could not start preview scan.');
    return;
  }
  if (!resp.ok || !data.job_id) {
    _normPreviewSetStatus(data.error || 'Preview scan failed.');
    return;
  }
  _normPreviewJobId = data.job_id;
  _normPreviewPoll();
}

function _normPreviewPoll() {
  clearTimeout(_normPreviewTimer);
  _normPreviewTimer = setTimeout(async () => {
    if (!_normPreviewJobId) return;
    let data;
    try {
      const r = await fetch(`/api/normalize/preview/${_normPreviewJobId}`,
                            { cache: 'no-store' });
      data = await r.json();
    } catch (_) { _normPreviewPoll(); return; }

    const { status, msg, progress, total } = data;
    if (status === 'done') {
      _normPreviewSetStatus('');
      _normPreviewRender(data.clips || []);
    } else if (status === 'error') {
      _normPreviewSetStatus(msg || 'Preview failed.');
    } else {
      const pct = total > 0 ? ` (${progress}/${total})` : '';
      _normPreviewSetStatus((msg || 'Scanning…') + pct);
      _normPreviewPoll();
    }
  }, 800);
}

function _normPreviewRender(clips) {
  const rows = document.querySelectorAll('#norm-sample-list .norm-sample-row');
  clips.slice(0, 4).forEach((clip, i) => {
    const row = rows[i];
    if (!row) return;
    row.classList.remove('norm-sample-placeholder');

    const meta    = row.querySelector('.norm-sample-meta');
    const kindEl  = row.querySelector('.norm-sample-kind');
    const tagEl   = row.querySelector('.norm-sample-tag');
    const btn     = row.querySelector('.norm-play-btn');
    const fill    = row.querySelector('.norm-progress-fill');

    // Inject name + LUFS span if not already present
    let nameEl = row.querySelector('.norm-sample-name');
    if (!nameEl) {
      nameEl = document.createElement('span');
      nameEl.className = 'norm-sample-name';
      meta.insertBefore(nameEl, tagEl);
    }
    let lufsEl = row.querySelector('.norm-sample-lufs');
    if (!lufsEl) {
      lufsEl = document.createElement('span');
      lufsEl.className = 'norm-sample-lufs';
      meta.appendChild(lufsEl);
    }

    kindEl.textContent = clip.kind === 'quietest' ? 'Quietest' : 'Loudest';
    nameEl.textContent = clip.track;
    lufsEl.textContent = `${clip.lufs > 0 ? '+' : ''}${clip.lufs} LUFS`;

    if (!clip.clip_id) { btn.disabled = true; return; }
    btn.disabled = false;

    const audio = new Audio(`/api/normalize/preview/clip/${clip.clip_id}`);
    audio.preload = 'metadata';

    audio.addEventListener('timeupdate', () => {
      if (!audio.duration) return;
      fill.style.width = `${(audio.currentTime / audio.duration) * 100}%`;
    });
    audio.addEventListener('ended', () => {
      btn.classList.remove('playing');
      fill.style.width = '0%';
    });

    btn.onclick = () => _normTogglePlay(audio, btn);

    row.querySelector('.norm-progress-track').onclick = e => {
      if (!audio.duration) return;
      const rect  = e.currentTarget.getBoundingClientRect();
      const ratio = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
      audio.currentTime = ratio * audio.duration;
      fill.style.width  = `${ratio * 100}%`;
    };
  });
}

function _normTogglePlay(audio, btn) {
  // Pause any other playing sample first
  if (_normActiveAudio && _normActiveAudio !== audio) {
    _normActiveAudio.pause();
    document.querySelectorAll('.norm-play-btn.playing')
      .forEach(b => b.classList.remove('playing'));
  }
  if (audio.paused) {
    audio.play().catch(() => {});
    btn.classList.add('playing');
    _normActiveAudio = audio;
  } else {
    audio.pause();
    btn.classList.remove('playing');
    _normActiveAudio = null;
  }
}

function _normPreviewSetStatus(msg) {
  const el = document.getElementById('norm-preview-status');
  if (el) el.textContent = msg;
}

function _normPreviewSetSkeleton() {
  document.querySelectorAll('#norm-sample-list .norm-sample-row').forEach(row => {
    row.classList.add('norm-sample-placeholder');
    const btn = row.querySelector('.norm-play-btn');
    if (btn) { btn.disabled = true; btn.classList.remove('playing'); }
    const fill = row.querySelector('.norm-progress-fill');
    if (fill) fill.style.width = '0%';
    const n = row.querySelector('.norm-sample-name');
    if (n) n.textContent = '';
    const l = row.querySelector('.norm-sample-lufs');
    if (l) l.textContent = '';
  });
  if (_normActiveAudio) { _normActiveAudio.pause(); _normActiveAudio = null; }
}

function _normPreviewReset() {
  clearTimeout(_normPreviewTimer);
  _normPreviewJobId = null;
  _normPreviewSetSkeleton();
  _normPreviewSetStatus('Add a folder above to load loudness previews.');
}

/* ── State tracker — per-library step completion ─────────────────────────
   Calls /api/state on load and after every successful command.
   Cards get .step-complete or .step-error CSS classes.              */
const STATE_STEP_MAP = {
  audit:'rail-btn-audit', process:'step-process', duplicates:'step-duplicates',
  prune:'step-duplicates', relocate:'rail-btn-relocate', import:'rail-btn-import',
  link:'rail-btn-link', normalize:'step-normalize', convert:'step-convert',
  organize:'step-organize', novelty:'step-novelty',
};
async function loadState(libraryRoot) {
  if (!libraryRoot) return;
  try {
    const res = await fetch('/api/state', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ library_root: libraryRoot }),
    });
    if (res.ok) applyStateToUI(await res.json());
  } catch (_) {}
}
function applyStateToUI(state) {
  Object.entries(STATE_STEP_MAP).forEach(([step, cardId]) => {
    const card = document.getElementById(cardId);
    if (!card) return;
    card.classList.remove('step-complete', 'step-error');
    const info = state[step];
    if (!info) return;
    card.classList.add(info.exit_code === 0 ? 'step-complete' : 'step-error');
  });
}
async function _initStateOverlay() {
  try {
    const cfg = await fetch('/api/config').then(r => r.json());
    if (cfg.music_root) loadState(cfg.music_root);
  } catch (_) {}
}
_initStateOverlay();
['organize-target','novelty-dest'].forEach(id => {
  const el = document.getElementById(id);
  if (el) el.addEventListener('change', () => { if (el.value.trim()) loadState(el.value.trim()); });
});
