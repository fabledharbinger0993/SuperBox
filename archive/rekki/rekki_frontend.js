// ARCHIVED Rekki frontend code from rekitbox.js

// == Opening Rekki setup block (lines 1-73) ==
let _rekkiMode = localStorage.getItem('rekitbox-mode') || 'suburban';

function _rekkiEnabled() {
  return _rekkiMode === 'suburban';
}

function _showRekkiDisabledToast() {
  showToast('Rekki is disabled in Rural mode. Switch to Suburban to enable AI.', 'neutral');
}

function _applyRekkiModeUI(mode, model = '') {
  _rekkiMode = mode || 'suburban';
  localStorage.setItem('rekitbox-mode', _rekkiMode);

  const enabled = _rekkiEnabled();
  const home = document.getElementById('rekki-home');
  const avatar = document.getElementById('rekki-avatar');
  const stage = document.getElementById('rekki-stage');
  const connDot = document.getElementById('rekki-conn-dot');
  const status = document.getElementById('rekki-status');

  if (home) {
    home.classList.toggle('rekki-disabled', !enabled);
    home.setAttribute('aria-disabled', String(!enabled));
    home.dataset.rekkiModeLabel = enabled ? 'AI' : 'OFF';
    home.title = enabled
      ? `Rekki — ${model ? `AI: ${model}` : 'AI enabled'} · drag onto anything or click to chat`
      : 'Rekki is disabled in Rural mode — switch to Suburban to enable AI';
  }

  if (avatar) {
    avatar.draggable = enabled;
    avatar.alt = enabled ? 'Rekki' : 'Rekki (disabled)';
  }

  if (!enabled) {
    document.body.classList.remove('rekki-dragging');
    if (_rekkiOpen && stage) {
      _rekkiOpen = false;
      stage.classList.add('hidden');
      _rekkiHideAnim();
    }
    if (typeof rekkiClearChip === 'function') rekkiClearChip();
    if (connDot) {
      connDot.classList.remove('online');
      connDot.classList.add('offline');
    }
    if (status) status.textContent = 'Rural mode — Rekki disabled';
  }
}

// Sync the actual Rekki avatar/stage with the configured mode.
function showRekitBoxMode() {
  fetch('/api/config').then(r => r.json()).then(cfg => {
    _applyRekkiModeUI(cfg.mode || 'suburban', cfg.rekki_model || '');
    if (typeof _rekkiRefreshStatus === 'function') _rekkiRefreshStatus();
  }).catch(() => {
    _applyRekkiModeUI(localStorage.getItem('rekitbox-mode') || 'suburban');
  });
}

document.addEventListener('DOMContentLoaded', () => {
  _applyRekkiModeUI(localStorage.getItem('rekitbox-mode') || 'suburban');
  showRekitBoxMode();
  
  // Initialize tool drawer pin button
  const pinBtn = document.getElementById('tool-drawer-pin');
  if (pinBtn) {
    _syncToolDrawerPinState();
  }
});


// == Main Rekki section (lines ~5815-6357) ==
// ── Rekki ─────────────────────────────────────────────────────────────────────

let _rekkiOpen    = false;
let _rekkiHistory = [];
let _rekkiChip    = null;   // { type, label, description, tool, raw } — current element context
let _rekkiCtxTarget = null; // element the right-click menu was triggered on

// ── History hydration (restores thread across page reloads) ──────────────────

async function _rekkiBootHistory() {
  try {
    const r = await fetch('/api/rekki/history?limit=60', { cache: 'no-store' });
    const d = await r.json();
    if (!d.ok || !d.messages || d.messages.length === 0) return;

    const log = document.getElementById('rekki-chat-log');
    if (log) {
      const divider = document.createElement('div');
      divider.className = 'rekki-history-divider';
      divider.textContent = '— previous session —';
      log.appendChild(divider);
      d.messages.forEach(m => {
        const el = document.createElement('div');
        el.className = `rekki-msg ${m.role === 'user' ? 'user' : 'rekki'}`;
        if (m.source && m.source !== 'main') el.dataset.source = m.source;
        el.textContent = m.content;
        log.appendChild(el);
      });
      // Slight visual break before new session messages
      const gap = document.createElement('div');
      gap.className = 'rekki-history-divider';
      gap.textContent = '— now —';
      log.appendChild(gap);
      log.scrollTop = log.scrollHeight;
    }

    // Seed _rekkiHistory with the tail so Rekki has conversational context
    // (capped at 8 pairs — matches backend sanitized_history slice)
    _rekkiHistory = d.messages
      .slice(-16)
      .map(m => ({ role: m.role, content: m.content }));
  } catch {
    // Non-fatal — continue without history
  }
}

// ── Panel open/close ──────────────────────────────────────────────────────────

function toggleRekkiPanel(ctx) {
  if (!_rekkiEnabled()) {
    _showRekkiDisabledToast();
    return;
  }
  const stage = document.getElementById('rekki-stage');
  if (!stage) return;
  const opening = !_rekkiOpen;
  _rekkiOpen = opening;
  if (opening) {
    stage.classList.remove('hidden');
    if (ctx) _rekkiLoadChip(ctx);
    _rekkiRefreshStatus();
    _rekkiPlayClip('entrance', () => _rekkiPlayClip('chat', null, true));
    requestAnimationFrame(() => {
      const input = document.getElementById('rekki-input');
      if (input) input.focus();
    });
  } else {
    _rekkiPlayClip('exit', () => {
      stage.classList.add('hidden');
      _rekkiHideAnim();
    });
  }
}

function _rekkiSetStatus(text) {
  const el = document.getElementById('rekki-status');
  if (el) el.textContent = text;
}

function _rekkiSetConn(online) {
  const dot = document.getElementById('rekki-conn-dot');
  if (!dot) return;
  dot.classList.toggle('online', online);
  dot.classList.toggle('offline', !online);
}

function _rekkiAppend(role, content, source) {
  const log = document.getElementById('rekki-chat-log');
  if (!log) return;
  const msg = document.createElement('div');
  msg.className = `rekki-msg ${role === 'user' ? 'user' : 'rekki'}`;
  if (source) msg.dataset.source = source;
  msg.textContent = content;
  log.appendChild(msg);
  log.scrollTop = log.scrollHeight;
}

async function _rekkiRefreshStatus() {
  if (!_rekkiEnabled()) {
    _rekkiSetConn(false);
    _rekkiSetStatus('Rural mode — Rekki disabled');
    return;
  }
  try {
    const r = await fetch('/api/rekki/status', { cache: 'no-store' });
    const d = await r.json();
    if (d.provider === 'scripted-local') {
      _rekkiSetConn(true);
      _rekkiSetStatus('Scripted Local Mode');
    } else if (d.ollama_reachable && d.model_available !== false) {
      _rekkiSetConn(true);
      _rekkiSetStatus(`${d.resolved_model || d.model}`);
    } else {
      _rekkiSetConn(false);
      _rekkiSetStatus(d.error || 'Rekki offline');
    }
  } catch {
    _rekkiSetConn(false);
    _rekkiSetStatus('Rekki offline');
  }
}

// ── Context chip ──────────────────────────────────────────────────────────────

function _rekkiLoadChip(ctx) {
  _rekkiChip = ctx;
  const chip  = document.getElementById('rekki-ctx-chip');
  const label = document.getElementById('rekki-ctx-chip-label');
  if (!chip || !label) return;
  const icon = ctx.type === 'error' ? '⚠ ' : ctx.type === 'tool-card' ? '⚙ ' : '◈ ';
  label.textContent = icon + (ctx.label || ctx.type);
  chip.classList.remove('hidden');
  chip.title = ctx.description || '';
}

function rekkiClearChip() {
  _rekkiChip = null;
  const chip = document.getElementById('rekki-ctx-chip');
  if (chip) chip.classList.add('hidden');
}

// ── Send message ──────────────────────────────────────────────────────────────

function _rekkiSourceFor(chipCtx) {
  if (!chipCtx) return 'main';
  if (chipCtx.type === 'tool-card') {
    return `card-${(chipCtx.tool || chipCtx.label || 'unknown').replace(/\s+/g, '-').toLowerCase()}`;
  }
  return chipCtx.type || 'main';
}

function _rekkiContextHistoryMessage(chipCtx) {
  if (!chipCtx) return null;
  const parts = [
    `Active Rekki context: ${chipCtx.label || chipCtx.type || 'unknown item'}`,
    chipCtx.type ? `Type: ${chipCtx.type}` : '',
    chipCtx.tool ? `Tool: ${chipCtx.tool}` : '',
    chipCtx.severity ? `Severity: ${chipCtx.severity}` : '',
    chipCtx.description ? `What Rekki already knows: ${chipCtx.description}` : '',
    'Answer the next user message about this context unless they clearly switch topics.',
  ].filter(Boolean);
  return { role: 'assistant', content: parts.join('\n') };
}

function _rekkiBuildRequestHistory(chipCtx) {
  const base = _rekkiHistory.slice(-15);
  const ctxMsg = _rekkiContextHistoryMessage(chipCtx);
  return ctxMsg ? [...base, ctxMsg] : base;
}

async function _rekkiDispatchMessage(text, options = {}) {
  if (!_rekkiEnabled()) {
    _showRekkiDisabledToast();
    return;
  }
  const chipCtx = options.chipCtx === undefined ? _rekkiChip : options.chipCtx;
  const displayText = (options.displayText || text || '').trim();
  const rawText = (text || '').trim();
  const send = document.getElementById('rekki-send');
  if (!rawText || !send) return;

  const msgSource = _rekkiSourceFor(chipCtx);
  if (options.appendUser !== false) {
    _rekkiAppend('user', displayText, msgSource);
  }
  _rekkiHistory.push({ role: 'user', content: displayText });
  send.disabled = true;
  _rekkiSetStatus(options.pendingStatus || 'Thinking…');

  try {
    const body = {
      message: rawText,
      history: _rekkiBuildRequestHistory(chipCtx),
      source: msgSource,
    };
    if (chipCtx) body.element_context = chipCtx;

    const r = await fetch('/api/rekki/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const d = await r.json();
    if (!r.ok || !d.ok) throw new Error(d.error || 'chat failed');
    _rekkiAppend('rekki', d.reply || '(no reply)', msgSource);
    _rekkiHistory.push({ role: 'assistant', content: d.reply || '' });
    _rekkiSetStatus(`${d.model}`);
  } catch (err) {
    if (options.fallbackReply) {
      _rekkiAppend('rekki', options.fallbackReply, msgSource);
      _rekkiHistory.push({ role: 'assistant', content: options.fallbackReply });
      _rekkiSetStatus('Context ready');
    } else {
      _rekkiAppend('rekki', `Error: ${err.message || err}`);
      _rekkiSetStatus('Unavailable');
    }
  } finally {
    send.disabled = false;
  }
}

async function rekkiSendMessage() {
  const input = document.getElementById('rekki-input');
  if (!input) return;
  const text = (input.value || '').trim();
  if (!text) return;
  input.value = '';
  await _rekkiDispatchMessage(text, { chipCtx: _rekkiChip, displayText: text });
}

// ── DOM scraper ───────────────────────────────────────────────────────────────

function _rekkiScrape(el) {
  const parentChain = [];
  let node = el.parentElement;
  for (let i = 0; i < 6 && node && node !== document.body; i++, node = node.parentElement) {
    const t = (node.getAttribute('aria-label') || node.id || node.className || '').slice(0, 80);
    if (t) parentChain.push(t);
  }

  const siblings = [];
  const sibs = el.parentElement ? [...el.parentElement.children] : [];
  for (const s of sibs) {
    if (s !== el && s.textContent.trim()) siblings.push(s.textContent.trim().slice(0, 80));
    if (siblings.length >= 5) break;
  }

  let sectionHeading = '';
  let n = el;
  while (n && n !== document.body) {
    const h = n.querySelector('h2,h3,h4,label,.card-title,.panel-title');
    if (h && h.textContent.trim()) { sectionHeading = h.textContent.trim().slice(0, 120); break; }
    n = n.parentElement;
  }

  const toolPanel = document.querySelector('.card.active, [data-tool].active, section.active');
  const logTail   = [...document.querySelectorAll('#log-output .log-line, #log-output .log-entry')]
    .slice(-5).map(e => e.textContent.trim().slice(0, 100));

  return {
    elementText: el.textContent.trim().slice(0, 400),
    elementTag: el.tagName.toLowerCase(),
    parentChain,
    siblings,
    sectionHeading,
    toolPanel: toolPanel ? (toolPanel.id || toolPanel.dataset.tool || toolPanel.className.slice(0, 60)) : '',
    existingAttributes: Object.fromEntries(
      [...el.attributes].filter(a => a.name !== 'style').map(a => [a.name, a.value.slice(0, 80)])
    ),
    pageState: {
      activeTool: document.querySelector('[data-tool].active')?.dataset.tool || '',
      lastRunStatus: document.querySelector('#scan-status, .last-status')?.textContent?.trim()?.slice(0, 60) || '',
      logTail,
    },
  };
}

// ── Context resolution: drop or right-click ───────────────────────────────────

async function _rekkiResolveContext(el) {
  // 1. Element already has context — use it directly
  const raw = el.dataset.rekkiContext;
  if (raw) {
    try { return JSON.parse(raw); } catch { /* fall through to inference */ }
  }

  // 2. Walk up to find annotated ancestor
  let node = el.parentElement;
  while (node && node !== document.body) {
    if (node.dataset.rekkiContext) {
      try { return JSON.parse(node.dataset.rekkiContext); } catch { break; }
    }
    node = node.parentElement;
  }

  // 3. Infer via Rekki — scrape DOM and call backend
  _rekkiSetStatus('Reading context…');
  try {
    const scrape = _rekkiScrape(el);
    const r = await fetch('/api/rekki/infer-context', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ scrape }),
    });
    const d = await r.json();
    if (d.ok && d.context) {
      // Write inferred context back to the element so next drop is instant
      el.dataset.rekkiContext = JSON.stringify(d.context);
      return d.context;
    }
  } catch { /* fall through */ }

  // 4. Fallback — generic context from text content
  return {
    type: 'generic',
    label: el.textContent.trim().slice(0, 55) || el.tagName.toLowerCase(),
    description: 'I was dropped on this element. Let me take a look.',
    tool: null,
    severity: null,
  };
}

async function _rekkiEngageWith(el) {
  const ctx = await _rekkiResolveContext(el);
  if (!_rekkiOpen) toggleRekkiPanel(ctx);
  else _rekkiLoadChip(ctx);

  const prompt = ctx.type === 'log-entry' || ctx.type === 'error'
    ? 'Explain what happened here, why it matters, and the next safe step.'
    : 'Explain what this is in RekitBox, what it does, and anything important I should know before using it.';
  const displayText = ctx.label ? `Explain: ${ctx.label}` : 'Explain this item';
  await _rekkiDispatchMessage(prompt, {
    chipCtx: ctx,
    displayText,
    fallbackReply: ctx.description || 'I have this item selected and I am ready to talk about it.',
  });
}

// Called by the ✦ button embedded in each tool card header
function _rekkiEngageWithCard(btn) {
  const card = btn.closest('[data-rekki-context]');
  if (card) _rekkiEngageWith(card);
}

// ── Pipeline wizard voice strip ───────────────────────────────────────────────

const _REKKI_STEP_NOTES = {
  tag:        "Tag runs BPM and key detection — the foundation everything else depends on. Do this first.",
  duplicates: "Duplicate detection uses audio fingerprinting — thorough but slow. Review the report carefully before you prune anything.",
  prune:      "Prune permanently deletes files. The dry-run preview is your safety net — read every line before unchecking it.",
  relocate:   "Relocate rewrites broken DB paths. Safe for your library, but Rekordbox must be closed first.",
  organize:   "Organize restructures your folder hierarchy. File paths in the DB will change — run Relocate after if needed.",
  normalize:  "Normalize adjusts track loudness. Your originals are preserved unless you explicitly enable overwrite.",
  scan:       "Scan indexes the filesystem — no writes, safe any time. Good first step.",
  import:     "Import adds new files to the Rekordbox DB. Rekordbox must be closed. No file moves happen here.",
  rename:     "Rename rewrites file names from tags. Preview the changes in dry-run before committing.",
};

function _rekkiWizardMessage(phase, text) {
  const el = document.getElementById(`rekki-wiz-msg-${phase}`);
  if (!el) return;
  el.textContent = text;
  el.style.opacity = '0';
  el.style.transition = 'opacity .25s';
  requestAnimationFrame(() => requestAnimationFrame(() => {
    el.style.opacity = '1';
    setTimeout(() => { el.style.transition = ''; }, 280);
  }));
}

function _rekkiWizardOpenChat(phase) {
  const ctx = {
    type: 'wizard',
    label: 'Pipeline Wizard',
    description: phase === 'p1'
      ? "I can help you plan your pipeline. What are you trying to fix in your library?"
      : "What questions do you have about configuring these steps?",
    source: 'wizard',
    icon: '⚙️',
  };
  if (!_rekkiOpen) toggleRekkiPanel(ctx);
  else _rekkiLoadChip(ctx);
  if (ctx.description) {
    _rekkiSetStatus('');
    _rekkiAppend('rekki', ctx.description, ctx.label);
    _rekkiHistory.push({ role: 'assistant', content: ctx.description });
  }
}

// ── Hidden music discovery ─────────────────────────────────────────────────────

async function rekkiDiscoverMusic(searchPath) {
  if (!searchPath) return;
  _rekkiAppend('rekki', `Scanning \`${searchPath}\` for audio files not in your library…`, 'Discovery');
  _rekkiSetStatus('scanning…');
  try {
    const res = await fetch(`/api/rekki/discover-music?path=${encodeURIComponent(searchPath)}&limit=200`);
    const data = await res.json();
    if (!data.ok) {
      _rekkiAppend('rekki', `Couldn't scan that path: ${data.error}`, 'Discovery');
      _rekkiSetStatus('');
      return;
    }
    const { discovered, total, library_source } = data;
    if (total === 0) {
      _rekkiAppend('rekki', `No unindexed audio files found in \`${searchPath}\`. Your library already covers everything here.`, 'Discovery');
    } else {
      const preview = discovered.slice(0, 10).map(f =>
        `• ${f.path.split('/').slice(-2).join('/')}  (${f.size_mb} MB)`
      ).join('\n');
      const more = total > 10 ? `\n…and ${total - 10} more.` : '';
      const src  = library_source !== 'none' ? ` (checked against ${library_source})` : '';
      _rekkiAppend('rekki',
        `Found **${total}** unindexed audio file${total !== 1 ? 's' : ''} in \`${searchPath}\`${src}:\n\n${preview}${more}\n\nWant me to help import these?`,
        'Discovery'
      );
    }
  } catch (err) {
    _rekkiAppend('rekki', `Discovery failed: ${err.message}`, 'Discovery');
  }
  _rekkiSetStatus('');
}

function _rekkiAvatarInit() {
  const avatar = document.getElementById('rekki-avatar');
  const home   = document.getElementById('rekki-home');
  if (!avatar || !home) return;

  avatar.addEventListener('dragstart', (e) => {
    if (!_rekkiEnabled()) {
      e.preventDefault();
      _showRekkiDisabledToast();
      return;
    }
    e.dataTransfer.setData('application/rekki', '1');
    e.dataTransfer.effectAllowed = 'copy';
    e.dataTransfer.setDragImage(avatar, avatar.naturalWidth ? 24 : 20, 24);
    document.body.classList.add('rekki-dragging');
  });

  avatar.addEventListener('dragend', () => {
    document.body.classList.remove('rekki-dragging');
    avatar.classList.add('returning');
    avatar.addEventListener('animationend', () => avatar.classList.remove('returning'), { once: true });
  });

  // Make the home element itself clickable to toggle panel
  home.addEventListener('click', (e) => {
    if (e.target !== avatar && e.target !== home) return;
    if (!_rekkiEnabled()) {
      _showRekkiDisabledToast();
      return;
    }
    toggleRekkiPanel();
  });
}

function _rekkiPlayClip(name, onended, loop) {
  const anim = document.getElementById('rekki-anim');
  const src  = anim && anim.querySelector('source');
  if (!anim || !src) return;
  src.src = `/static/rekki-${name}.mp4`;
  anim.load();
  anim.loop = !!loop;
  anim.play().catch(() => {});
  if (onended) anim.addEventListener('ended', onended, { once: true });
}

function _rekkiHideAnim() {
  const anim = document.getElementById('rekki-anim');
  if (anim) { anim.pause(); anim.currentTime = 0; }
}

// ── Global drop target ────────────────────────────────────────────────────────

document.addEventListener('dragover', (e) => {
  if (!document.body.classList.contains('rekki-dragging')) return;
  e.preventDefault();
  e.dataTransfer.dropEffect = 'copy';
});

document.addEventListener('drop', async (e) => {
  if (!document.body.classList.contains('rekki-dragging')) return;
  if (!e.dataTransfer.getData('application/rekki')) return;
  e.preventDefault();
  const target = document.elementFromPoint(e.clientX, e.clientY);
  if (!target || target === document.getElementById('rekki-avatar')) return;
  await _rekkiEngageWith(target);
});

// ── Right-click context menu ──────────────────────────────────────────────────

document.addEventListener('contextmenu', (e) => {
  const menu = document.getElementById('rekki-ctx-menu');
  if (!menu) return;

  if (!_rekkiEnabled()) {
    menu.classList.add('hidden');
    return;
  }

  // Don't intercept clicks inside the Rekki panel itself
  if (e.target.closest('#rekki-stage, #rekki-ctx-menu')) return;

  e.preventDefault();
  _rekkiCtxTarget = e.target;

  const vw = window.innerWidth, vh = window.innerHeight;
  let x = e.clientX, y = e.clientY;
  menu.classList.remove('hidden');
  const mw = menu.offsetWidth, mh = menu.offsetHeight;
  if (x + mw > vw) x = vw - mw - 6;
  if (y + mh > vh) y = vh - mh - 6;
  menu.style.left = x + 'px';
  menu.style.top  = y + 'px';
});

document.addEventListener('click', () => {
  const menu = document.getElementById('rekki-ctx-menu');
  if (menu) menu.classList.add('hidden');
});

async function rekkiMenuExplain() {
  document.getElementById('rekki-ctx-menu')?.classList.add('hidden');
  if (!_rekkiEnabled()) return;
  if (!_rekkiCtxTarget) return;
  await _rekkiEngageWith(_rekkiCtxTarget);
}

async function rekkiMenuTag() {
  document.getElementById('rekki-ctx-menu')?.classList.add('hidden');
  if (!_rekkiEnabled()) return;
  if (!_rekkiCtxTarget) return;

  // Force inference even if context already exists, to allow re-tagging
  delete _rekkiCtxTarget.dataset.rekkiContext;
  const ctx = await _rekkiResolveContext(_rekkiCtxTarget);

  // Write back and show chip only — don't open chat
  _rekkiCtxTarget.dataset.rekkiContext = JSON.stringify(ctx);
  if (_rekkiOpen) _rekkiLoadChip(ctx);
  _rekkiSetStatus(`Tagged: ${ctx.label || ctx.type}`);
}
