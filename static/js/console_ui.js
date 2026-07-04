// ======================================================================
// Console log view
// Extracted from app.js (mechanical split — same global functions, no behaviour
// change). Loaded AFTER app.js in index.html, so it sees S/api/toast/etc.
// ======================================================================

// ── CONSOLE ──────────────────────────────────────────────────
//
// Design notes: the DOM path into #console-out has broken on us repeatedly in
// production (CSS collapse, the element not existing at log time, the view
// rendering but being off-screen, etc.). So the authoritative store is an
// in-memory ring buffer on `window.__ripsterLog`. The DOM element is a
// best-effort render target: we (re)paint it from the buffer whenever the
// Console tab is shown, or whenever new logs arrive while it's already open.
// If the DOM element breaks for any reason, `window.__ripsterLog` is always
// readable from DevTools (`console.table(__ripsterLog)`).

const MAX_LOG = 1000;
window.__ripsterLog = window.__ripsterLog || [];   // exposed on purpose
const _LOG = window.__ripsterLog;

// Visual level -> (DevTools method, CSS class)
const _LEVELS = {
  error:   { dev: 'error', cls: 'log-error'   },
  warn:    { dev: 'warn',  cls: 'log-warn'    },
  success: { dev: 'log',   cls: 'log-success' },
  info:    { dev: 'log',   cls: 'log-info'    },
  stdout:  { dev: 'log',   cls: 'log-stdout'  },
};

function _isGuest() {
  return document.body.classList.contains('guest-mode');
}

// A "milestone" line — status changes and progress. This is all a guest is
// shown; raw stdout / engine tracebacks are owner-only noise.
function _isMilestone(entry) {
  if (entry.level === 'error' || entry.level === 'warn' || entry.level === 'success')
    return true;
  return /[▶✓✗⚠⟳⚡♻⏳]|\d{1,3}\s*%/.test(entry.text || '');
}

function _consoleTask() {
  return document.getElementById('console-task-filter')?.value || 'all';
}

// Should this entry show in the console right now? Honors the per-task
// filter and the guest-laconic rule.
function _consolePass(entry) {
  const f = _consoleTask();
  if (f !== 'all' && (entry.task_id || '') !== f) return false;
  const s = (typeof _consoleSvc === 'function') ? _consoleSvc() : 'all';
  if (s !== 'all' && (entry.service || '') !== s) return false;
  const lvl = (typeof _consoleLevel === 'function') ? _consoleLevel() : 'all';
  if (lvl !== 'all') {
    const order = { error:3, warn:2, success:1, info:1, stdout:0 };
    const need  = { error:3, warn:2, info:1 }[lvl] || 0;
    if ((order[entry.level] || 0) < need) return false;
  }
  if (_isGuest() && !_isMilestone(entry)) return false;
  return true;
}

function _taskLabel(id) {
  const tk = (S.queue || []).find(x => x.id === id);   // not `t` — shadows the i18n t()
  const title = tk && tk.meta && (tk.meta.title || tk.meta.artist);
  return title || t('cn.task_word') + ' ' + String(id).slice(0, 6);
}

// Rebuild the per-task <select> from task ids seen in the log buffer.
function _rebuildConsoleTaskFilter() {
  const sel = document.getElementById('console-task-filter');
  if (!sel) return;
  const cur = sel.value || 'all';
  const seen = new Set();
  let html = '<option value="all">' + t('cn.all_tasks') + '</option>';
  for (const e of _LOG) {
    if (e.task_id && !seen.has(e.task_id)) {
      seen.add(e.task_id);
      html += `<option value="${esc(e.task_id)}">${esc(_taskLabel(e.task_id))}</option>`;
    }
  }
  sel.innerHTML = html;
  sel.value = (cur === 'all' || seen.has(cur)) ? cur : 'all';
}

// Service palette — distinct hue per service so the eye finds them instantly.
const _SVC_COLOR = {
  apple:'#ff453a', qobuz:'#1870f5', tidal:'#00d4b3', deezer:'#a238ff',
  spotify:'#1db954', soundcloud:'#ff5500', bbc:'#e4003b', yandex:'#ffcc00',
  lucida:'#ff7a33', orpheus:'#1db954', amd:'#ff453a', gamdl:'#ff453a',
  zhaarey:'#ff453a', beatport:'#01f49c', wrapper:'#af52de',
  watchlist:'#ffd60a', release:'#1db954', guest:'#c084a0',
  stats:'#3ecfaa', tunnel:'#6a6a8a', ngrok:'#6a6a8a',
  tokens:'#c084a0', startup:'#c084a0', queue:'#c084a0',
  meta:'#af52de', isrc:'#af52de', csrf:'#e24b4a',
};
function _svcColor(svc) { return _SVC_COLOR[svc] || 'var(--muted2)'; }

function appendLog(text, level='info', taskId='', service='') {
  text = (text == null) ? '' : String(text);
  const L   = _LEVELS[level] || _LEVELS.info;
  const ts  = new Date();
  const hms = ts.toTimeString().slice(0,8);
  // Auto-extract `[svc]` prefix if backend forgot to attribute.
  if (!service) {
    const m = /^\s*\[([a-z][a-z0-9:_-]+)\]/i.exec(text);
    if (m) service = m[1].toLowerCase().split(':')[0];
  }
  const entry = { ts, hms, level, text, cls: L.cls, task_id: taskId || '', service: service || '' };

  const newTask = taskId && !_LOG.some(e => e.task_id === taskId);
  const newSvc  = service && !_LOG.some(e => e.service === service);

  // Collapse repetitive Deezer/streamrip per-track stdout lines.
  // Otherwise a 100-track playlist floods the console with identical "OK · ARI"
  // shape lines and the actual signal (errors / milestones) gets lost.
  const last = _LOG[_LOG.length - 1];
  if (last && last.level === level && last.task_id === entry.task_id
      && _isLogSpamPair(last.text, text)) {
    last.count = (last.count || 1) + 1;
    last.text  = text.replace(/ × \d+$/, '') + `  × ${last.count}`;
    // Repaint the last visible console line in place.
    const out  = document.getElementById('console-out');
    const node = out && out.lastElementChild;
    if (node) {
      const tag = (_consoleTask() === 'all' && entry.task_id)
        ? `[${String(entry.task_id).slice(0, 6)}] ` : '';
      node.textContent = `[${last.hms}] ${tag}${last.text}`;
    }
    return;   // do NOT push a fresh entry
  }

  // 1) Push to the in-memory buffer — the canonical store.
  _LOG.push(entry);
  if (_LOG.length > MAX_LOG) _LOG.splice(0, _LOG.length - MAX_LOG);

  const cntEl = document.getElementById('console-count');
  if (cntEl) cntEl.textContent = `${_LOG.length} log${_LOG.length===1?'':'s'}`;

  // 2) Mirror to DevTools as a fallback.
  try { console[L.dev]('[ripster]', text); } catch(_) {}

  // 3) A previously-unseen task / service → refresh the dropdowns.
  if (newTask) _rebuildConsoleTaskFilter();
  if (newSvc && typeof _rebuildConsoleSvcFilter === 'function') _rebuildConsoleSvcFilter();

  // 4) Best-effort: incrementally paint the open Console view (if the entry
  //    passes the current task filter / guest-laconic rule).
  const out = document.getElementById('console-out');
  if (out && _consolePass(entry)) {
    _appendLogLine(out, entry);
    _trimConsoleDom(out);
    _maybeAutoscroll(out);
  }

  // 5) Error badge on sidebar.
  if (level === 'error') {
    const badge = document.getElementById('log-badge');
    if (badge) badge.style.display = '';
  }
}

// Two lines are considered "the same repetitive log spam" iff:
//   * both match a spam-prone shape (per-track OK markers from streamrip /
//     Deezer ARL rotation, "[FA] OK · …", "OK · ARI …", etc.)
//   * after stripping numbers / hex IDs / × N tail, they normalise to the
//     same skeleton.
function _isLogSpamPair(prev, next) {
  const _spammy = (s) =>
    /^\s*\[?[A-Z]{1,3}\]?\s*OK\b/.test(s) ||
    /\bOK\s*[·•]\s*\w+\b/.test(s) ||
    /\b(free|premium),\s*\d+\s*day/i.test(s);
  if (!_spammy(prev) || !_spammy(next)) return false;
  const _norm = (s) => s
    .replace(/ × \d+$/, '')                     // strip our own counter
    .replace(/\b[A-F0-9]{6,}\b/gi, '#')         // hex IDs
    .replace(/\b[A-Z]{2,}[A-Z0-9]{4,}\b/g, '#') // tokens like ARIXXXX
    .replace(/\d+/g, '#')                       // any digits
    .replace(/\s+/g, ' ').trim().slice(0, 80);
  return _norm(prev) === _norm(next);
}

function _appendLogLine(out, entry) {
  const line = document.createElement('div');
  line.className = entry.cls;
  line.style.cssText = 'display:flex;gap:6px;align-items:baseline;padding:1px 4px;font-family:var(--mono);font-size:11px';
  const tag = (_consoleTask() === 'all' && entry.task_id)
    ? `[${String(entry.task_id).slice(0, 6)}] ` : '';
  const svc = entry.service || '';
  const svcChip = svc
    ? `<span style="flex-shrink:0;font-size:9px;font-weight:700;padding:1px 6px;border-radius:3px;background:${_svcColor(svc)}22;color:${_svcColor(svc)};text-transform:uppercase;letter-spacing:.5px">${esc(svc)}</span>`
    : '';
  // Strip the `[svc]` prefix from text since we render a chip — keeps the line short.
  let bodyText = entry.text;
  if (svc) bodyText = bodyText.replace(/^\s*\[[a-z][a-z0-9:_-]*\]\s*/i, '');
  line.innerHTML = `<span style="flex-shrink:0;color:var(--muted2)">[${entry.hms}]</span>${svcChip}<span style="flex:1;min-width:0;white-space:pre-wrap;word-break:break-word">${esc(tag + bodyText)}</span>`;
  // Subtle service tint on the row background — eye finds clusters instantly.
  if (svc) line.style.borderLeft = `2px solid ${_svcColor(svc)}66`;
  out.appendChild(line);
}

// Console filter helpers
function _consoleSvc() {
  return document.getElementById('console-svc-filter')?.value || 'all';
}
function _consoleLevel() {
  return document.getElementById('console-level-filter')?.value || 'all';
}
function _rebuildConsoleSvcFilter() {
  const sel = document.getElementById('console-svc-filter');
  if (!sel) return;
  const cur = sel.value || 'all';
  const seen = new Set();
  let html = '<option value="all">' + t('cn.all_svcs') + '</option>';
  for (const e of _LOG) if (e.service && !seen.has(e.service)) { seen.add(e.service); }
  // Stable ordering: alphabetical
  for (const svc of [...seen].sort()) {
    html += `<option value="${esc(svc)}" style="color:${_svcColor(svc)}">${esc(svc)}</option>`;
  }
  sel.innerHTML = html;
  sel.value = (cur === 'all' || seen.has(cur)) ? cur : 'all';
}

function _trimConsoleDom(out) {
  while (out.children.length > MAX_LOG) out.removeChild(out.firstChild);
}

function _maybeAutoscroll(out) {
  const cb = document.getElementById('autoscroll');
  if (cb && cb.checked) out.scrollTop = out.scrollHeight;
}

// Repaint the Console from the buffer — honors the per-task filter.
function _refreshConsole(_tries) {
  const out = document.getElementById('console-out');
  if (!out) {
    // The console view fragment may still be loading (showView injects it async) —
    // retry briefly so the buffer paints once #console-out exists, instead of
    // silently leaving the console blank. (This DOM path has broken repeatedly.)
    if ((_tries || 0) < 12) setTimeout(() => _refreshConsole((_tries || 0) + 1), 120);
    return;
  }
  _rebuildConsoleTaskFilter();
  if (typeof _rebuildConsoleSvcFilter === 'function') _rebuildConsoleSvcFilter();
  out.innerHTML = '';
  const visible = _LOG.filter(_consolePass);
  if (visible.length === 0) {
    const hint = document.createElement('div');
    hint.style.cssText = 'color:var(--muted);font-style:italic;padding:10px 0';
    hint.textContent = _LOG.length
      ? t('cn.no_task_logs')
      : t('cn.empty');
    out.appendChild(hint);
  } else {
    for (const e of visible) _appendLogLine(out, e);
  }
  _maybeAutoscroll(out);
  _checkConsoleVisible(out);
  const cntEl = document.getElementById('console-count');
  if (cntEl) cntEl.textContent = `${_LOG.length} log${_LOG.length===1?'':'s'}`;
}

// If the container exists but has zero rendered size, something external
// (CSS, parent display:none, browser extension) is hiding it. Emit a clear
// warning AND apply an inline-style fallback so the user still sees logs.
function _checkConsoleVisible(out) {
  const rect = out.getBoundingClientRect();
  if (rect.width > 0 && rect.height > 0) return;

  // Inline fallback — forces a usable console regardless of broken parent chain.
  out.style.cssText += ';position:relative;display:block;height:60vh;min-height:320px;width:100%;';

  // Diagnostic dump of the chain so we can see WHY flex collapsed.
  const chain = [];
  let el = out;
  while (el && el !== document.body) {
    const cs = getComputedStyle(el);
    chain.push({
      tag: el.tagName + (el.id?'#'+el.id:'') + (el.className?'.'+String(el.className).replace(/\s+/g,'.'):''),
      display: cs.display,
      flex: cs.flex,
      height: cs.height,
      minHeight: cs.minHeight,
      rect: el.getBoundingClientRect().height + 'px',
    });
    el = el.parentElement;
  }
  console.warn(
    `[ripster] Console DOM had zero size (${rect.width}×${rect.height}) — applied inline fallback. ` +
    `Logs are also in window.__ripsterLog.\nParent chain:`,
    chain
  );
}

function clearConsole() {
  _LOG.length = 0;
  const out = document.getElementById('console-out');
  if (out) out.innerHTML = '';
  const badge = document.getElementById('log-badge');
  if (badge) badge.style.display = 'none';
  const cntEl = document.getElementById('console-count');
  if (cntEl) cntEl.textContent = '0 logs';
}

// Copy a whole console's text to the clipboard. WebView2 often blocks manual
// text-selection / right-click in the log panel, so a one-click "Copy all" is the
// reliable way for the user to grab error logs. Tries the async Clipboard API
// (127.0.0.1 is a secure context + the click is a user gesture) and falls back to
// the legacy textarea+execCommand path if that's unavailable.
async function copyConsole(elId, btn) {
  const el = document.getElementById(elId || 'console-out');
  if (!el) return;
  const text = (el.innerText || el.textContent || '').trim();
  if (!text) { if (window.toast) toast(t('cn.empty'), 'var(--muted)'); return; }
  let ok = false;
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(text);
      ok = true;
    }
  } catch (e) { ok = false; }
  if (!ok) {
    try {
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.style.cssText = 'position:fixed;left:-9999px;top:0;opacity:0';
      document.body.appendChild(ta);
      ta.focus(); ta.select();
      ok = document.execCommand('copy');
      document.body.removeChild(ta);
    } catch (e) { ok = false; }
  }
  if (btn) {
    const orig = btn.textContent;
    btn.textContent = ok ? '✓ ' + t('cn.copied_word') : '✗ ' + t('cn.copy_fail_word');
    setTimeout(() => { btn.textContent = orig; }, 1500);
  }
  if (window.toast) toast(ok ? ti('cn.copied_n',{n:text.length}) : t('cn.copy_fail'), ok ? 'var(--green)' : 'var(--red)');
}

// Download ALL diagnostic logs as one zip (console + errors + launcher). The
// best way for a remote tester to hand us the full picture — one file to attach,
// no copy-paste, nothing scrolled off. Same-origin nav carries the session cookie.
function downloadLogs(btn) {
  try {
    const a = document.createElement('a');
    a.href = '/api/logs/download';
    a.download = '';
    document.body.appendChild(a);
    a.click();
    a.remove();
    if (btn) { const o = btn.textContent; btn.textContent = '⬇ ' + t('cn.ready_word'); setTimeout(() => { btn.textContent = o; }, 1500); }
    if (window.toast) toast(t('cn.dl_logs'), 'var(--green)', 6000);
  } catch (e) {
    if (window.toast) toast(t('cn.dl_fail') + ((e && e.message) || e), 'var(--red)');
  }
}

// Convenience for the user / us: dump the last N entries to DevTools
window.ripsterDumpLogs = function(n = 50) {
  console.table(_LOG.slice(-n).map(e => ({ time: e.hms, level: e.level, text: e.text })));
  return _LOG.slice(-n);
};

async function fixGamdlDeps() {
  const btn = document.getElementById('fix-deps-btn');
  if(btn){ btn.disabled=true; btn.textContent='⏳ Fixing…'; }
  appendLog('[FIX] Upgrading protobuf + pywidevine…', 'warn');
  await fetch('/api/fix-gamdl-deps', {method:'POST'});
}

