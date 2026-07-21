// Ripster main script — extracted from index.html

// ── STATE ──────────────────────────────────────────────────────
const S = { config:{}, queue:[], running:false, paused:false, guestMode:false, lang:'ru' };
let ws = null;
const QUALITIES = [];

// ── i18n ───────────────────────────────────────────────────────
// LANG loaded from /static/js/i18n.js (loaded BEFORE this file)

// Lookup order: current locale → English → Russian → the raw key. So a string
// that's only translated in ru+en still renders in en for hi/ja/zh instead of
// dumping the raw key on screen — partial translations degrade gracefully.
const t = key => {
  const L = LANG[S.lang || 'ru'] || LANG.ru;
  return L[key] ?? (LANG.en && LANG.en[key]) ?? LANG.ru[key] ?? key;
};
// Interpolating translate: fills {named} placeholders from `params`. Returns the
// raw key if missing (same as `t`) — callers that need a fallback should check
// the dict for the key first (see case 'log').
const ti = (key, params) => { let s = t(key); if (params) for (const k in params) s = s.replaceAll('{'+k+'}', params[k]); return s; };

// BCP-47 tag for Date#toLocaleDateString — was hardcoded 'ru' at every call
// site, so date labels stayed Russian even after switching the UI language.
const _DATE_LOCALE = {ru:'ru', en:'en', hi:'hi-IN', ja:'ja', zh:'zh-CN'};
function _dateLoc() { return _DATE_LOCALE[S.lang] || 'en'; }

function setLang(lang) {
  if(!LANG[lang]) return;
  S.lang = lang;
  localStorage.setItem('ripster-lang', lang);
  applyLang();
  // Re-render any view that builds its DOM via t() at runtime — otherwise
  // switching language leaves yesterday's RU strings sitting in the DOM.
  try { if(typeof renderQueue       === 'function') renderQueue(); }       catch {}
  try { if(typeof _scRender         === 'function') _scRender(); }         catch {}
  try { if(typeof _applyRelFilter   === 'function') _applyRelFilter(); }   catch {}
  try { if(typeof _libApplyFilter   === 'function') _libApplyFilter(); }   catch {}
  try { if(typeof renderAlbumPage   === 'function' && (typeof Detail !== 'undefined') && Detail.currentAlbum) renderAlbumPage(); } catch {}
  try { if(typeof renderArtistPage  === 'function' && (typeof Detail !== 'undefined') && Detail.currentArtist) renderArtistPage(); } catch {}
}

function toggleLangDropdown(e) {
  let dd = document.getElementById('lang-dropdown');
  if(!dd) return;
  // Ensure the dropdown lives directly under body so backdrop-filter on .topbar
  // doesn't act as a containing block and clip position:fixed.
  if(dd.parentElement !== document.body) {
    document.body.appendChild(dd);
  }
  const open = dd.style.display !== 'none';
  if(open) {
    dd.style.display = 'none';
  } else {
    const btn = document.getElementById('lang-current');
    if(btn) {
      const r = btn.getBoundingClientRect();
      dd.style.top   = (r.bottom + 4) + 'px';
      dd.style.right = (window.innerWidth - r.right) + 'px';
      dd.style.left  = '';
    }
    dd.style.display = 'block';
    const close = () => { dd.style.display='none'; document.removeEventListener('click',close); };
    setTimeout(() => document.addEventListener('click', close), 0);
  }
  if(e) e.stopPropagation();
}

function applyLang() {
  const lang = S.lang || 'ru';
  const flags = {ru:'🇷🇺',en:'🇬🇧',hi:'🇮🇳',ja:'🇯🇵',zh:'🇨🇳'};
  document.documentElement.lang = lang;
  // t() returns the key itself when a translation is missing — in that case KEEP
  // the element's inline (authored) text instead of overwriting it with the raw
  // key string (that's how "setup.deps_note" leaked into the UI).
  const _tx = (k) => { const v = t(k); return v === k ? null : v; };
  document.querySelectorAll('[data-i18n]').forEach(el => { const v=_tx(el.dataset.i18n); if(v!=null) el.textContent = v; });
  document.querySelectorAll('[data-i18n-ph]').forEach(el => { const v=_tx(el.dataset.i18nPh); if(v!=null) el.placeholder = v; });
  document.querySelectorAll('[data-i18n-title]').forEach(el => { const v=_tx(el.dataset.i18nTitle); if(v!=null) el.title = v; });
  document.querySelectorAll('[data-i18n-html]').forEach(el => { const v=_tx(el.dataset.i18nHtml); if(v!=null) el.innerHTML = v; });
  const cur = document.getElementById('lang-current');
  if(cur) cur.textContent = flags[lang] || lang.toUpperCase();
}

// ── GLOBAL STATE (must be before any function uses them) ──────────
const STEP_DEFS_ZHAAREY = [
  { key: 'go',          label: 'Go runtime',  icon: '🔵' },
  { key: 'downloader',  label: 'Downloader',  icon: '📥' },
  { key: 'MP4Box',      label: 'MP4Box',      icon: '🎬' },
  { key: 'mp4decrypt',  label: 'mp4decrypt',  icon: '🔓' },
  { key: 'ffmpeg',      label: 'FFmpeg',      icon: '🎞' },
];
const STEP_DEFS_GAMDL = [
  { key: 'go',          label: 'gamdl (pip)', icon: '🐍' },
  { key: 'downloader',  label: 'cookies.txt', icon: '🍪' },
  { key: 'MP4Box',      label: 'MP4Box',      icon: '🎬' },
  { key: 'mp4decrypt',  label: 'mp4decrypt',  icon: '🔓' },
  { key: 'ffmpeg',      label: 'FFmpeg',      icon: '🎞' },
];
const STEP_DEFS_AMD = [
  { key: 'go',         label: 'AMD clone',  icon: '📦' },
  { key: 'downloader', label: 'AMD deps',   icon: '🔧' },
  { key: 'MP4Box',     label: 'MP4Box',     icon: '🎬' },
  { key: 'mp4decrypt', label: 'mp4decrypt', icon: '🔓' },
  { key: 'ffmpeg',     label: 'FFmpeg',     icon: '🎞' },
];
let STEP_DEFS = STEP_DEFS_ZHAAREY;
const stepState = { go:'idle', downloader:'idle', MP4Box:'idle', mp4decrypt:'idle', ffmpeg:'idle' };
let toolsState = {};
let setupRunning = false;
let _wrapperWasDown   = false;
let _wrapperDismissed = false;
let _wrapperStarting  = false;
let _dockerAvailable  = false;
let _detailAutoOpened  = false;

// ── RESTART ───────────────────────────────────────────────────
let _restartPending = false;
async function restartServer() {
  if (_restartPending) return;
  _restartPending = true;
  const ov = document.getElementById('restart-overlay');
  if (ov) ov.style.display = 'flex';
  try { await api('POST', '/api/admin/restart'); } catch(e) {}
}

// Deferred restart: apply staged changes the moment guests stop downloading, so
// nobody is cut off mid-download. Restarts immediately if guests are already idle.
async function restartAppWhenIdle() {
  const r = await api('POST', '/api/admin/restart-when-idle', {});
  if (!r || !r.ok) { toast(t('t.error'), 'var(--red)'); return; }
  if (r.restarting) { toast(t('t.guests_idle'), 'var(--green)'); return; }
  if (r.pending) {
    toast(ti('t.restart_when_free',{n:r.sessions||0,extra:(r.queue_running?t('t.queue_going'):'')}), 'var(--accent)', '', 5000);
  } else {
    toast(t('t.deferred_off'), 'var(--muted)');
  }
}

// ── WEBSOCKET ─────────────────────────────────────────────────
function connectWS() {
  ws = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws`);
  ws.onopen = () => {
    if (_restartPending) {
      _restartPending = false;
      location.reload();
      return;
    }
    setStatus('● Connected', 'var(--green)');
    appendLog('WebSocket connected — ready', 'success');
    pullQueue();   // resync queue from the authoritative REST on every (re)connect —
                   // a long-lived socket that silently missed queue_update events
                   // otherwise leaves S.queue frozen on a stale snapshot.
  };
  ws.onclose = () => {
    setStatus('● Disconnected', 'var(--red)');
    setTimeout(connectWS, 2000);
  };
  ws.onerror = () => ws.close();
  ws.onmessage = (e) => {
    _wsLastMsg = Date.now();
    const msg = JSON.parse(e.data);
    handleMessage(msg);
  };
}

// WS health watchdog: a dropped TCP connection without a close frame leaves the
// socket half-open — onclose never fires, so reconnect never runs and live
// queue_update/log events silently stop arriving (the "only F5 shows it" bug).
// Detect a stale/dead socket and force a fresh connection, which re-sends `init`
// (config+queue) and restores live updates without a page reload.
let _wsLastMsg = Date.now();
function _wsWatchdog() {
  const st = ws && ws.readyState;
  if (st === WebSocket.CLOSED || st === WebSocket.CLOSING || st === undefined) {
    connectWS();
  } else if (st === WebSocket.OPEN && Date.now() - _wsLastMsg > 30000) {
    // Open but silent far past the server's heartbeat → probably half-open. Cycle it.
    try { ws.close(); } catch {}  // triggers onclose → reconnect in 2s
  }
}
setInterval(_wsWatchdog, 15000);
// Self-heal the queue: even an "alive but stale" socket that silently missed a
// queue_update is corrected by periodically re-pulling the authoritative REST
// queue. Fixes "bot added a task but Ripster shows a stale/empty queue".
setInterval(() => { if (ws && ws.readyState === WebSocket.OPEN) pullQueue(); }, 15000);
document.addEventListener('visibilitychange', () => { if (!document.hidden) { _wsWatchdog(); pullQueue(); } });

// Pull the authoritative queue over REST and re-render. A safety net so a
// successful add reflects immediately even if the WS push is lagging/dead.
async function pullQueue() {
  try {
    const q = await (await fetch('/api/queue')).json();
    if (Array.isArray(q)) { S.queue = q; renderQueue(); updateTransport(); }
  } catch {}
}

function handleMessage(msg) {
  // Resolve any Setup-checklist waiter keyed on this message type (e.g. a
  // 'soundcloud_installed' that unblocks the next component in installSelected).
  if(typeof _wsWaiters !== 'undefined' && _wsWaiters.has(msg.type)) {
    try { _wsWaiters.get(msg.type)(); } catch {}
  }
  switch(msg.type) {
    case 'init':
      S.config  = msg.config  || {};
      S.queue   = msg.queue   || [];
      S.running = msg.running || false;
      S.paused  = msg.paused  || false;
      if(typeof STEP_DEFS_GAMDL!=='undefined') STEP_DEFS = (S.config['engine']==='gamdl') ? STEP_DEFS_GAMDL : STEP_DEFS_ZHAAREY;
      // Re-merge guest-local prefs (player-spin etc.) every time — this 'init'
      // message fires on every WS reconnect, not just first load, and would
      // otherwise silently drop guest-only prefs (see _applyPlayerPrefsToUI).
      try { _applyPlayerPrefsToUI?.(); } catch {}
      applyConfig(); renderQueue(); updateTransport(); updatePills(); renderQualityGrid(); renderConfig(); _syncReleasesSettingsTab();
      if(typeof _maybeAskTelemetryName!=='undefined') setTimeout(_maybeAskTelemetryName, 2500);  // first-run consent ask (owner/tester builds only — public mirror doesn't ship telemetry_ui.js)
      setTimeout(autoValidateServices, 1500);   // probe all configured tokens on startup — no need to open each tab
      if(S.config['engine']==='gamdl') setTimeout(checkCookies, 1200);
      if(S.config['engine']==='amd')   setTimeout(checkAMDStatus, 800);
      // Init quality selector for current service
      updateQualitySelector('apple');
      _refreshSearchSvcSelect();
      // Populate batch quality in background
      setTimeout(()=>{ const bq=document.getElementById('batch-quality'); if(bq&&!bq.options.length) QUALITIES.forEach(q=>{const o=document.createElement('option');o.value=q.id;o.textContent=q.label;bq.appendChild(o);}); }, 1000);
      // Offer to resume a long mix from where user left off last session
      setTimeout(()=>{ try { _offerMixResume?.(); } catch {} }, 2000);
      break;
    case 'queue_update':
      S.queue = msg.queue || [];
      renderQueue(); updateTransport(); renderConfig();
      // Restructure the per-guest lamps/bars when a download starts/stops.
      if (document.getElementById('admin-links-list')?.offsetParent) loadAdminLinks();
      break;
    case 'dl_counter': {
      const task = S.queue.find(t=>t.id===msg.task_id);
      if(task){ task[msg.counter] = msg.value; updateQueueItem(task); }
      break;
    }
    case 'pool_update':
      // Real-time Apple wrapper-pool usage → admin console card.
      try { window.adminPoolUpdate?.(msg.pool); } catch {}
      break;
    case 'queue_started': S.running=true;  S.paused=false; updateTransport(); break;
    case 'queue_paused':  S.paused=true;   updateTransport(); break;
    case 'queue_resumed': S.paused=false;  updateTransport(); break;
    case 'queue_stopped': S.running=false; S.paused=false; updateTransport(); break;
    case 'queue_done':    S.running=false; S.paused=false; updateTransport(); toast('All done! 🎉','var(--green)'); break;
    case 'sc_fallback_added': {
      const svc = (msg.service || '').toUpperCase();
      const title = msg.title || t('cd.op_track');
      toast('🔁 '+ti('t.sc_fallback',{svc:svc,title:title}), '#ff5500', '', 4500);
      // Re-render so the origin tile shows "→ перенаправлено" right away
      renderQueue();
      break;
    }
    case 'progress': {
      const task = S.queue.find(t=>t.id===msg.id);
      if(task){
        task.progress     = msg.progress;
        task._progTotal   = msg.total   || 0;
        task._progCurrent = msg.current || 0;
        // total=0 → StreamripMixin track-completion counter (N done, total unknown)
        if (msg.total === 0 && msg.current > 0) {
          task._tracksCompleted = msg.current;
        }
        // zhaarey/Apple emit a real "Track N of M" tally where total === the
        // album's track count. Capture it as the authoritative track counter so
        // the FAR more frequent yt-dlp fragment events (total=100) can't clobber
        // it — otherwise the card falls back to floor(pct%×N) and shows 1/N
        // while the bot (which reads the text) correctly shows N/M. Gated on an
        // exact match with meta.trackCount so segment/byte totals never trip it.
        else {
          const _mt = (task.meta && (task.meta.trackCount || task.meta.totalTracks)) || 0;
          if (_mt > 1 && msg.total === _mt && msg.current > 0 && msg.current <= _mt) {
            task._tracksCompleted = msg.current;
          }
        }
        // Do NOT overwrite meta.trackCount — it comes from the API fetch.
        updateQueueItem(task);
      }
      updateTransport();
      updateGuestDownloadBars();   // move per-guest bars live (cheap, visibility-guarded)
      break;
    }
    case 'log': {
      // Server may send a localizable log: msg_key (console.* namespace) + params.
      // Translate to the user's language if we have the key; else fall back to the
      // server-provided RU text (msg) / raw text.
      let text;
      if (msg.msg_key && (LANG[S.lang||'ru']||LANG.ru)[msg.msg_key]) {
        text = ti(msg.msg_key, msg.params);
      } else {
        text = msg.text || msg.msg || '';
      }
      const level = msg.level || 'info';
      appendLog(text, level, msg.task_id || '', msg.service || '');
      if(msg.task_id) {
        const lvl = /ERROR|✗/.test(text) ? 'error' : /WARN|⚠/.test(text) ? 'warn' : /✓|Done|Saved/.test(text) ? 'success' : /INFO|STEP/.test(text) ? 'info' : 'stdout';
        // Guests get a laconic per-task log — drop raw stdout / traceback noise.
        if(_isGuest() && !_isMilestone({level:lvl, text})) break;
        const panel = document.getElementById(`qi-log-${msg.task_id}`);
        if(panel) {
          const line = document.createElement('div');
          line.className = `ll-${lvl}`;
          line.textContent = text;
          panel.appendChild(line);
          // keep only last 30 lines in panel
          while(panel.children.length > 30) panel.removeChild(panel.firstChild);
          if(panel.style.display !== 'none') panel.scrollTop = panel.scrollHeight;
          // update toggle button count
          const task = S.queue.find(t => t.id === msg.task_id);
          if(task) {
            task.log = task.log || [];
            task.log.push(text);
            const toggle = panel.previousElementSibling;
            if(toggle?.classList.contains('qi-log-toggle')) {
              toggle.textContent = (toggle.textContent.startsWith('▼') ? '▼' : '▶') + ` ${t('q.log_word')} (${panel.children.length})`;
            }
          }
          break;
        }
        // panel not in DOM yet — ensure log toggle appears on next rebuild
        const task = S.queue.find(t => t.id === msg.task_id);
        if(task) {
          task.log = task.log || [];
          task.log.push(text);
          const el = document.querySelector(`.qi[data-id="${msg.task_id}"]`);
          if(el) updateQueueItem(task, el);
        }
      }
      break;
    }
    case 'coder_progress': { try { coderProgress(msg); } catch(e){} break; }
    case 'history_updated': {
      // If the History or Stats view is currently showing, refresh it live.
      const histActive  = document.getElementById('view-history')?.classList.contains('active');
      const statsActive = document.getElementById('view-stats')?.classList.contains('active');
      if(histActive)  loadHistory();
      if(statsActive) loadStats();
      break;
    }
    case 'show_fix_deps_btn': {
      const btn = document.getElementById('fix-deps-btn');
      if(btn) btn.style.display='';
      // Auto-switch to console tab
      const cNav = document.querySelector('.nav-item[data-view="console"]');
      if(cNav) showView('console', cNav);
      break;
    }
    case 'spotify_authed': {
      loadSpotifyStatus();
      toast(t('t.sp_connected'), '#1db954');
      break;
    }
    case 'spotify_sp_dc_updated': {
      const inp = document.getElementById('s-sp-dc');
      const sts = document.getElementById('sp-dc-auto-status');
      if(sts) { sts.textContent = t('t.spdc_saved'); sts.style.color = 'var(--green)'; }
      loadSpotifyStatus();
      toast(t('t.spdc_upd'), '#1db954');
      // Reload config to update the input field value
      api('GET','/api/config').then(cfg => { if(inp && cfg['spotify-sp-dc']) inp.value = cfg['spotify-sp-dc']; });
      break;
    }
    case 'apple_authed': {
      refreshAppleAuthStatus();
      toast(t('t.apple_tok_upd'), '#0a84ff');
      break;
    }
    case 'watchlist_new_release': {
      const txt = ti('t.new_release',{r:msg.release || '',a:msg.artist || ''});
      toast(txt, 'var(--green)');
      // Refresh watchlist if open
      if(document.getElementById('view-watchlist')?.style.display !== 'none') loadWatchlist();
      break;
    }
    case 'watchlist_check_start': {
      setWatchlistStatus(ti('w.checking_n',{n:msg.total}), 0, msg.total);
      break;
    }
    case 'watchlist_check_progress': {
      setWatchlistStatus(`⟳ ${msg.current}/${msg.total} · ${msg.artist}`, msg.current, msg.total);
      break;
    }
    case 'watchlist_check_done': {
      if(msg.new > 0) setWatchlistStatus(ti('w.checked_new',{c:msg.checked,n:msg.new}), msg.checked, msg.checked, 'var(--green)');
      else            setWatchlistStatus(ti('w.checked_none',{c:msg.checked}), msg.checked, msg.checked);
      // Auto-clear after 4s
      setTimeout(() => clearWatchlistStatus(), 4000);
      break;
    }
    case 'releases_scan_start': {
      const _svcLbl = msg.service ? ` [${msg.service}]` : '';
      const _svcClr = ({spotify:'#1db954',qobuz:'#1870f5',tidal:'#00d4b3'})[msg.service] || 'var(--red)';
      if(msg.phase === 'artists') {
        setReleasesStatus(ti('w.fetch_artists',{svc:_svcLbl}), 0, 1, _svcClr);
      } else if(msg.phase === 'albums') {
        setReleasesStatus(ti('w.scanning_n',{n:msg.total,svc:_svcLbl}), 0, msg.total, _svcClr);
      }
      break;
    }
    case 'releases_scan_progress': {
      const _svcLbl2 = msg.service ? ` [${msg.service}]` : '';
      const _svcClr2 = ({spotify:'#1db954',qobuz:'#1870f5',tidal:'#00d4b3'})[msg.service] || 'var(--red)';
      const foundTxt = msg.found ? ` · ${t('w.found_word')}: ${msg.found}` : '';
      setReleasesStatus(`⟳ ${msg.current}/${msg.total}${_svcLbl2} · ${msg.artist}${foundTxt}`, msg.current, msg.total, _svcClr2);
      break;
    }
    case 'releases_scan_done': {
      _relStopPoll();  // WS arrived — kill the poll fallback
      const _svcLbl3 = msg.service ? ` [${msg.service}]` : '';
      if(msg.error) {
        setReleasesStatus(`✗ ${msg.error}`, 0, 1, 'var(--red)');
        setTimeout(() => clearReleasesStatus(), 8000);
        _relShowAuthHint(msg.error);
        break;
      }
      setReleasesStatus(`✓${_svcLbl3} ${msg.artists_checked} ${t('w.art_abbr')} · ${msg.releases_count} ${t('w.rel_abbr')}`, msg.artists_checked, msg.artists_checked, 'var(--green)');
      setTimeout(() => clearReleasesStatus(), 3000);
      // If backend sent releases in the WS message, render them directly
      if(msg.releases?.length) {
        _relCache.data = msg.releases;
        _relCache.ts   = Date.now();
        _relCache.key  = _relCacheKey();
        _relSaveLS(msg.releases, _relCacheKey());
        const st = document.getElementById('rel-status');
        if(st) st.style.display = 'none';
        _applyRelFilter();
      } else if(msg.releases_count === 0) {
        // Scan done, truly empty
        const empty = document.getElementById('rel-empty');
        const st    = document.getElementById('rel-status');
        if(st)    st.style.display = 'none';
        if(!_relCache.data?.length && empty) empty.style.display = '';
      }
      break;
    }
    case 'bbc_dl_start': {
      _bbcDlStart(msg.pid, msg.title, msg.artist);
      break;
    }
    case 'bbc_dl_progress': {
      _bbcDlProgress(msg.pid, msg.pct);
      break;
    }
    case 'bbc_dl_done': {
      _bbcDlDone(msg.pid, msg.title);
      break;
    }
    case 'amd_ready': {
      checkAMDStatus();
      toast(t('t.amd_ready'),'var(--green)');
      break;
    }
    case 'gamdl_deps_fixed': {
      const btn2 = document.getElementById('fix-deps-btn');
      if(btn2){ btn2.disabled=false; btn2.textContent='🔧 Fix gamdl deps (protobuf)'; btn2.style.display='none'; }
      toast(t('t.gamdl_fixed'), 'var(--green)');
      break;
    }
    case 'gamdl_needs_upgrade': {
      const sNav = document.querySelector('.nav-item[data-view="setup"]');
      if(sNav) showView('setup', sNav);
      toast(t('t.gamdl_old'), 'var(--orange)');
      break;
    }
    case 'show_wrapper_logs_hint': {
      const ob = document.getElementById('wrapper-ok-banner');
      if(ob && ob.style.display!=='none') toggleWrapperLogs();
      toast(t('t.no_codec'), 'var(--orange)');
      break;
    }
    case 'show_wrapper_needed': {
      // Show wrapper banner and switch to queue view
      const qNav = document.querySelector('.nav-item[data-view="queue"]');
      if(qNav) showView('queue', qNav);
      // Force wrapper banner visible
      const wb = document.getElementById('wrapper-banner');
      if(wb) wb.style.display='';
      _wrapperDismissed = false;
      toast(t('t.alac_wrap'), 'var(--orange)');
      break;
    }
    case 'bearer_updated':
      // Server no longer ships the token over the WS. Refetch the (redacted)
      // config so the UI can show "set / not set" in the token field.
      (async () => {
        try {
          const cfg = await (await fetch('/api/config')).json();
          Object.assign(S.config, cfg);
          const bEl = document.getElementById('t-bearer');
          if(bEl) bEl.value = S.config['authorization-token'] || '';
          updatePills();
        } catch(e) { console.warn('bearer_updated refetch failed:', e); }
      })();
      break;
    case 'config_update':
      if(msg.config){ Object.assign(S.config, msg.config); applyConfig(); updatePills(); } break;
    case 'engine_changed':
      S.config['engine'] = msg.engine;
      if(msg.qualities){ QUALITIES.length=0; QUALITIES.push(...msg.qualities);
        const sel2=document.getElementById('url-quality');
        if(sel2) sel2.innerHTML=QUALITIES.map(q=>`<option value="${q.id}">${q.label} — ${q.sub}</option>`).join('');
      }
      updateEngineUI(msg.engine); renderQualityGrid(); updatePills(); break;
    // ── Setup ──────────────────────────────────────────────────
    case 'install_log':
      appendSetupLog(msg.entry);
      appendLog('[SETUP] ' + msg.entry.text, msg.entry.level||'info');
      { const lbl = document.getElementById('setup-running-label');
        if(lbl && msg.entry.text.trim())
          lbl.textContent = msg.entry.text.replace(/[┌│└═▸⬇🔧📦✓✗⚠🚀]/gu,'').trim().slice(0,55); }
      break;
    case 'install_step':
      if(typeof renderChecklist === 'function') renderChecklist(); break;
    case 'tunnel_status':
      updateTunnelUI(msg.running, msg.connecting || false, msg.url || '');
      if (msg.running && msg.url) {
        toast(t('t.tunnel_ready') + msg.url, '#22c55e');
        updateRemoteUI(true, msg.url, 0);
      } else if (!msg.running && !msg.connecting) {
        // tunnel died unexpectedly
        if (document.getElementById('tunnel-stop-btn')?.style.display !== 'none') {
          toast(t('t.tunnel_off'), 'var(--red)');
        }
      }
      break;
    case 'tools_status': break;
    case 'restart_required': {
      const banner = document.getElementById('restart-banner');
      const reason = document.getElementById('restart-reason');
      if(banner) banner.style.display='';
      if(reason && msg.reason) reason.innerHTML = msg.reason;
      toast('⚠ Restart required — see Setup tab','var(--orange)');
      const nav = document.querySelector('.nav-item[data-view="setup"]');
      if(nav) showView('setup',nav);
      break;
    }
    case 'wrapper_status':
      updateWrapperUI(msg.running, msg.port||S.config['decrypt-port']||'127.0.0.1:10020', msg.docker, msg.docker_msg);
      break;
    case 'wrapper_log':
      appendWrapperLog(msg.text);
      appendLog('[WRAPPER] '+msg.text, msg.level||'info');
      break;
    case 'wrapper_login_failed':
      _wrapperStarting = false;
      toast(t('t.wrong_apple_pw'), 'var(--red)', t('t.check_wrapper_path'));
      appendLog('[WRAPPER] ✗ Login failed — '+t('t.wrapper_fix_pw'), 'error');
      checkWrapperStatus();
      break;
    case 'wrapper_started':
      _wrapperStarting=false;
      toast(t('t.wrapper_up'),'var(--green)');
      checkWrapperStatus();
      break;
    case 'amd_wrapper_not_ready': {
      const _inst = msg.instance || 'wm.wol.moe';
      toast('⚠ '+ti('t.wrapper_not_ready',{inst:_inst}), 'var(--orange)');
      // Refresh the status widget if visible
      const _wmEl = document.getElementById('amd-wm-status');
      if(_wmEl && _wmEl.style.display !== 'none') checkAMDWrapperStatus();
      break;
    }
    case 'orpheus_authed': {
      loadOrpheusStatus();
      const authUser = msg.username ? ` (${msg.username})` : '';
      toast(t('t.sp_authed')+authUser, 'var(--green)');
      if(window._orpheusLoginDone) { window._orpheusLoginDone(); window._orpheusLoginDone = null; }
      break;
    }
    case 'orpheus_not_authed':
      toast(t('t.orph_no_auth'), 'var(--red)', t('t.login_via_sp'), 10000);
      loadOrpheusStatus();
      showStab('spotify');
      break;
    case 'soundcloud_installed':
      toast(t('t.sc_ready'), '#ff5500');
      scEngineCheck();
      break;
    case 'wrapper_built': {
      const bBtn = document.getElementById('btn-wrapper-build');
      const bSt  = document.getElementById('wrapper-build-status');
      if(bBtn) { bBtn.disabled=false; bBtn.textContent='🔨 Build local image'; }
      if(bSt)  bSt.textContent = t('t.built');
      toast(t('t.local_img'), 'var(--blue)');
      break;
    }
    case 'wrapper_2fa_needed':
      showWrapper2FAModal();
      break;
    case 'setup_done': {
      setupRunning = false;
      const sb = document.getElementById('btn-setup');
      const ss = document.getElementById('setup-spinner');
      const sl = document.getElementById('setup-running-label');
      if(sb){ sb.disabled=false; sb.textContent='⚡ Auto-install everything'; }
      if(ss) ss.style.display='none';
      if(sl) sl.textContent='';
      if(msg.need_restart){
        const b2 = document.getElementById('restart-banner');
        if(b2) b2.style.display='';
        toast('⚠ RESTART REQUIRED','var(--orange)');
      } else if(!msg.missing||!msg.missing.length){
        toast('✅ All dependencies ready!','var(--green)');
      } else {
        toast('⚠ Some tools need manual install','var(--orange)');
      }
      checkTools(); break;
    }
  }
}

// ── API ───────────────────────────────────────────────────────
async function api(method, path, body) {
  const opts = { method, headers:{'Content-Type':'application/json'} };
  if(body) opts.body = JSON.stringify(body);
  const r = await fetch(path, opts);
  const text = await r.text();
  // Empty body — typically a 502/503/504 the tunnel returns while the origin
  // (server) is down or restarting. Surface a clean message instead of letting
  // r.json() throw the cryptic "Unexpected end of JSON input".
  if (!text.trim()) {
    if (!r.ok) throw new Error(`${t('t.server_down')} (HTTP ${r.status})`);
    return {};
  }
  try {
    return JSON.parse(text);   // valid JSON (200 or a 4xx with a {detail} body) → caller handles
  } catch (_) {
    throw new Error(!r.ok ? `${t('t.server_down')} (HTTP ${r.status})` : t('t.bad_server_resp'));
  }
}

async function loadQualities() {
  const q = await api('GET','/api/qualities');
  QUALITIES.length = 0;
  QUALITIES.push(...q);
  populateQualitySelect();
  renderQualityGrid();
}

// Guest/admin + per-guest live lamp helpers → moved to its own module file (see index.html).

// Remote access + Serveo tunnel UI → moved to its own module file (see index.html).

// ── INIT ─────────────────────────────────────────────────────
window.addEventListener('load', async () => {
  // PWA service worker — registered lazily so a SW bug never blocks app boot.
  if ('serviceWorker' in navigator && location.protocol !== 'file:') {
    try { navigator.serviceWorker.register('/static/sw.js'); } catch (e) {}
  }
  S.lang = localStorage.getItem('ripster-lang') || 'ru';
  await _loadAllViews();
  applyLang();
  await loadQualities();
  await checkSessionMode();
  connectWS();
  // Seed queue from REST while WS establishes (critical on HTTPS/ngrok where
  // WSS handshake may lag, leaving the queue empty until the first init message)
  fetch('/api/queue').then(r=>r.ok?r.json():null).then(q=>{
    if(Array.isArray(q) && q.length && !S.queue.length){ S.queue=q; renderQueue(); }
  }).catch(()=>{});
  applyStoredPrefs();
  loadAppInfo();
  // Apply initial engine state after qualities load
  setTimeout(()=>{ updateEngineUI(S.config['engine']||'zhaarey'); updatePills(); }, 1200);
  setTimeout(checkTools, 900);
  // Wrapper status polling every 10 seconds
  checkWrapperStatus();
  setInterval(checkWrapperStatus, 10000);
  // Public Apple wrapper (wm.wol.moe) health — shown even when the LOCAL wrapper
  // is the active engine, so a public-wrapper outage is visible. Heavier gRPC
  // check → poll it less often than the local one.
  setTimeout(checkPublicWrapperStatus, 2000);
  setInterval(checkPublicWrapperStatus, 90000);
});

// Wrapper management UI → moved to its own module file (see index.html).

// ── VIEW SWITCHING ────────────────────────────────────────────
function showView(name, el) {
  // Old views that got folded into Settings — redirect and open the right stab.
  const redirects = {quality:'apple', tokens:'apple', tags:'apple', config:'global'};
  if (redirects[name]) {
    const targetStab = redirects[name];
    name = 'settings';
    el = document.querySelector('.nav-item[data-view="settings"]');
    setTimeout(() => {
      showStab(targetStab, document.querySelector(`.stab[data-stab="${targetStab}"]`));
    }, 0);
  }
  document.querySelectorAll('.view').forEach(v=>v.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(n=>n.classList.remove('active'));
  document.getElementById('view-'+name)?.classList.add('active');
  el?.classList.add('active');
  if(name==='setup')     { checkTools(); loadDeps?.(); checkRipsterUpdate?.(true); }
  if(name==='history')   loadHistory();
  if(name==='watchlist') loadWatchlist();
  if(name==='releases')  { loadSpotifyStatus(); _syncReleasePillsFromConfig(); loadReleasesIfStale(); }
  if(name==='bbc')       { bbcInit(); }
  if(name==='soundcloud') scInit();
  if(name==='coder')     { coderInit(); coderFmtChange(); }
  if(name==='tagger')    taggerInit();
  if(name==='stats')     loadStats();
  if(name==='telemetry') telemetryInit();
  if(name==='console')   _refreshConsole();
  if(name==='guest-tokens') loadGuestSvcStatus();
  if(name==='settings') {
    loadTokensToUI();
    renderQualityGrid();
    const activeStab = document.querySelector('.stab.active');
    if(!activeStab) showStab('global', document.querySelector('.stab[data-stab="global"]'));
  }
}

// ── QUALITY SELECT POPULATE ────────────────────────────────────
function populateQualitySelect() {
  const sel = document.getElementById('url-quality');
  sel.innerHTML = QUALITIES.map(q=>`<option value="${q.id}">${q.label} — ${q.sub}</option>`).join('');
  sel.value = resolveQuality('apple');
}

// ── QUEUE ─────────────────────────────────────────────────────
// Pull every http(s) URL out of a pasted blob (newlines OR spaces between them),
// trimming trailing punctuation and de-duplicating while preserving order.
function _extractUrls(text) {
  const m = (text || '').match(/https?:\/\/[^\s]+/g) || [];
  return [...new Set(m.map(u => u.replace(/[)\].,;]+$/, '')))];
}

async function addUrl() {
  const _raw = document.getElementById('url-input').value.trim();
  const q   = document.getElementById('url-quality').value;
  if(!_raw){ toast(t('t.paste_link'),'var(--red)'); return; }

  // Multi-link: several URLs pasted at once → ask what to do (all / first / cancel).
  const _urls = _extractUrls(_raw);
  if (_urls.length > 1) { _multiUrlPrompt(_urls, q); return; }
  const url = _urls[0] || _raw;

  const urlSvc = detectSvcFromUrl(url);

  // Spotify → if OrpheusDL is the active engine, download directly.
  // Otherwise ask the user which service to convert to.
  if(urlSvc === 'spotify') {
    const spEngine = (S.config && S.config['spotify-engine']) || 'convert';
    if(spEngine === 'orpheus_spotify') {
      const spQuality = S.config['orpheus-quality'] || 'hifi';
      await _doAddUrl(url, spQuality, 'spotify');
      return;
    }
    const savedTarget = (S.config && S.config['spotify-default-target']) || '';
    if(savedTarget && ['apple','deezer','qobuz'].includes(savedTarget)) {
      _chooseSpTargetDirect(url, q, savedTarget);
    } else {
      _showSpotifyChoiceToast(url, q);
    }
    return;
  }

  // The URL-quality selector is repopulated per service by updateQualitySelector
  // (it stamps sel.dataset.svc). When the dropdown already holds THIS service's
  // own quality list, honour the user's pick. Only fall back to the service
  // default when the selector is stale (still showing another service's codecs)
  // — otherwise a Deezer task would silently get its default instead of the
  // FLAC / MP3-320 the user actually chose.
  const _qSel = document.getElementById('url-quality');
  const _selSvc = _qSel ? (_qSel.dataset.svc || 'apple') : 'apple';
  let qFinal;
  if (!urlSvc || urlSvc === 'apple') {
    qFinal = q;
  } else if (_selSvc === urlSvc) {
    qFinal = q;                       // dropdown matches the URL's service → user's pick
  } else {
    qFinal = resolveQuality(urlSvc);  // stale selector → safe service default
  }
  await _doAddUrl(url, qFinal, urlSvc);
}

// ── Multi-link prompt: a pasted batch of URLs → all / first / cancel ──────────
function _multiUrlPrompt(urls, quality) {
  const old = document.getElementById('multi-url-modal');
  if(old) old.remove();
  const n = urls.length;
  const svcs = {};
  urls.forEach(u => { const s = (detectSvcFromUrl(u) || '?'); svcs[s] = (svcs[s]||0)+1; });
  const summary = Object.entries(svcs)
    .map(([s,c]) => `${(typeof _svcLabel==='function'?_svcLabel(s):s)||s}×${c}`).join(' · ');
  const list = urls.slice(0,8).map((u,i) => {
    const short = u.length>56 ? u.slice(0,53)+'…' : u;
    return `<div style="font-size:10px;font-family:monospace;color:var(--muted);padding:1px 0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${i+1}. ${short}</div>`;
  }).join('') + (n>8 ? `<div style="font-size:10px;color:var(--muted);margin-top:2px">…${t('t.and_more')} ${n-8}</div>` : '');

  const modal = document.createElement('div');
  modal.id = 'multi-url-modal';
  modal.style.cssText = 'position:fixed;inset:0;z-index:9999;display:flex;align-items:center;justify-content:center;background:rgba(0,0,0,.7);backdrop-filter:blur(4px)';
  modal.innerHTML = `<div style="background:var(--surface,#1c1c1e);border:1px solid rgba(255,255,255,.1);border-radius:16px;padding:24px;width:440px;max-width:90vw">
    <div style="font-size:16px;font-weight:700;color:var(--text);margin-bottom:6px">${t('t.multi_pasted')}</div>
    <div style="font-size:12px;color:var(--muted);margin-bottom:12px">${t('t.found_word')} <b style="color:var(--text)">${n}</b> ${t('t.links_one_req')}${summary?` · ${summary}`:''}. ${t('t.what_do')}</div>
    <div style="background:rgba(0,0,0,.3);border-radius:8px;padding:8px 10px;margin-bottom:16px;max-height:170px;overflow:auto">${list}</div>
    <div style="display:flex;flex-direction:column;gap:8px">
      <button onclick="_multiUrlChoose('all')" style="padding:11px;background:var(--green,#34c759);color:#000;border:none;border-radius:10px;font-size:13px;font-weight:700;cursor:pointer;font-family:var(--font)">⬇ ${ti('t.dl_all',{n:n})}</button>
      <button onclick="_multiUrlChoose('first')" style="padding:10px;background:rgba(255,255,255,.06);color:var(--text);border:1px solid rgba(255,255,255,.12);border-radius:10px;font-size:12px;font-weight:600;cursor:pointer;font-family:var(--font)">${t('t.only_first')}</button>
      <button onclick="_multiUrlChoose('cancel')" style="padding:8px;background:transparent;color:var(--muted);border:1px solid rgba(255,255,255,.1);border-radius:10px;font-size:12px;cursor:pointer;font-family:var(--font)">${t('s.cancel')}</button>
    </div>
  </div>`;
  document.body.appendChild(modal);
  modal.onclick = e => { if(e.target===modal) _multiUrlChoose('cancel'); };
  window._multiUrlState = { urls, quality };
}

async function _multiUrlChoose(action) {
  const modal = document.getElementById('multi-url-modal');
  if(modal) modal.remove();
  const st = window._multiUrlState || {};
  window._multiUrlState = null;
  if(action === 'cancel' || !st.urls) return;
  if(action === 'first') {
    document.getElementById('url-input').value = st.urls[0];
    addUrl();                       // re-run the normal single-URL flow on the first link
    return;
  }
  // 'all' → hand the whole batch to the queue/batch endpoint.
  const r = await api('POST','/api/queue/batch', { text: st.urls.join('\n'), quality: st.quality });
  if(r && r.ok) {
    document.getElementById('url-input').value = '';
    try { detectUrlService(''); } catch(e) {}
    toast(t('t.added_q')+': '+(r.added != null ? r.added : st.urls.length), 'var(--green)');
  } else {
    toast(t('t.batch_fail'), 'var(--red)');
  }
}

// Variant of _chooseSpTarget that doesn't come from a picker — used when the
// user has saved a default target and we skip the picker entirely.
async function _chooseSpTargetDirect(url, quality, target) {
  toast(`Spotify → ${_svcLabel(target)}`, _svcColor(target));
  const r = await api('POST','/api/convert/spotify', { url, target });
  if(r.ok && r.target?.url) {
    await api('POST','/api/queue/add', { url: r.target.url, quality: resolveQuality(target), title: r.target.title });
    document.getElementById('url-input').value = '';
    detectUrlService('');
    toast('+ '+r.target.title, _svcColor(target));
  } else {
    // Conversion failed — show a friendly toast with the option to pick
    // a different service, since the remembered one couldn't find the track.
    toast(t('t.not_found_on')+_svcLabel(target)+' — '+t('t.pick_other'), 'var(--orange)', r.error||'');
    _showSpotifyChoiceToast(url, quality);
  }
}

function _svcLabel(svc){ return {apple:'Apple Music',qobuz:'Qobuz',deezer:'Deezer',tidal:'Tidal',spotify:'Spotify',soundcloud:'SoundCloud',yandex:'Яндекс.Музыка'}[svc]||svc; }
// ── Service brand colors (single source of truth) ─────────────────────────
// Default = real brand hues. User can override per service in Settings →
// General → "Цвета сервисов" — overrides land in S.config['service-colors'].
const SVC_BRAND = {
  apple:'#fc3c44', qobuz:'#1b68d3', tidal:'#00d4b3', deezer:'#a238ff',
  spotify:'#1db954', soundcloud:'#ff5500', bbc:'#e4003b', yandex:'#ffcc00',
  lucida:'#ff7a33', orpheus:'#1db954', amd:'#fc3c44', gamdl:'#fc3c44',
  zhaarey:'#fc3c44', beatport:'#01f49c', wrapper:'#af52de',
  watchlist:'#ffd60a', release:'#1db954', guest:'#c084a0',
  stats:'#3ecfaa', tunnel:'#6a6a8a', ngrok:'#6a6a8a',
  tokens:'#c084a0', startup:'#c084a0', queue:'#c084a0',
  meta:'#af52de', isrc:'#af52de', csrf:'#e24b4a',
};
function _svcColor(svc){
  if (!svc) return 'var(--muted2)';
  const overrides = (typeof S !== 'undefined' && S.config && S.config['service-colors']) || {};
  return overrides[svc] || SVC_BRAND[svc] || 'var(--green)';
}
// Render a service name as a coloured span — `<span style="color:#hex">Qobuz</span>`.
function svcLabelHTML(svc, label){
  const color = _svcColor(svc);
  return `<span style="color:${color};font-weight:700">${esc(label || _svcLabel(svc))}</span>`;
}

// Settings per-service color picker → moved to its own module file (see index.html).

// Coder + Tagger + shared folder tree → moved to its own module file (see index.html).

// ── TRANSPORT ─────────────────────────────────────────────────
function updateTransport() {
  const total = S.queue.length;
  const done  = S.queue.filter(t=>t.status==='done').length;
  const pct   = total>0?Math.round(done/total*100):0;
  document.getElementById('tp-bar').style.width = pct+'%';
  document.getElementById('tp-label').textContent = `${done}/${total} done${S.running?(S.paused?' · PAUSED':' · Running'):''}`;
  const btnStart = document.getElementById('btn-start');
  const btnPause = document.getElementById('btn-pause');
  const btnStop  = document.getElementById('btn-stop');
  btnStart.disabled = S.running;
  btnPause.disabled = !S.running;
  btnStop.disabled  = !S.running;
  btnPause.textContent = S.paused?'▶ Resume':'⏸ Pause';
}

async function startQueue() {
  const r = await api('POST','/api/queue/start');
  if(r && r.ok === false) {
    toast(r.msg || t('t.queue_start_fail'),'var(--orange)');
  }
}
async function pauseQueue() { await api('POST','/api/queue/pause'); }
async function stopQueue()  { await api('POST','/api/queue/stop'); }

// ── QUALITY ───────────────────────────────────────────────────
function renderQualityGrid() {
  const grid = document.getElementById('quality-grid');
  if(!grid||!QUALITIES.length) return;
  const cur  = S.config['quality']||'alac';
  const curQ = QUALITIES.find(q => q.id === cur) || QUALITIES[0];
  // Unified compact UI — same look as Qobuz/Tidal/Deezer:
  //   • <select> for the choice
  //   • thin caption row with brand-coloured badge + bitrate + req
  grid.innerHTML = `
    <div class="field-group">
      <label class="lbl">${t('s.default_quality')}</label>
      <select id="q-select" onchange="selectQuality(this.value)" style="width:100%">
        ${QUALITIES.map(q =>
          `<option value="${q.id}" ${q.id===cur?'selected':''}>${q.label}${q.sub?' — '+q.sub.replace(/—.*/,'').trim():''}</option>`
        ).join('')}
      </select>
      <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-top:7px;font-size:11px;color:var(--muted)">
        <span style="font-size:9px;font-weight:800;padding:2px 8px;border-radius:5px;background:${curQ.color}22;color:${curQ.color};letter-spacing:.4px">${esc(curQ.badge)}</span>
        <span style="color:${curQ.color};font-weight:700">${esc(curQ.bitrate)}</span>
        <span>·</span>
        <span>${curQ.req==='wrapper'?t('s.req_wrapper'):t('s.req_mut')}</span>
        <span>·</span>
        <span style="font-family:var(--mono);font-size:10px">.${esc(curQ.ext)}</span>
      </div>
    </div>
  `;
  renderQualityNote(cur);
}

function selectQuality(id) {
  S.config['quality'] = id;
  const qSel = document.getElementById('q-select');
  saveSetting('quality', id);
  const sel = document.getElementById('url-quality');
  if(sel) sel.value = id;
  if(qSel) _showSavedChip(qSel);
  renderQualityGrid();
  _wrapperDismissed = false;  // reset dismiss so banner shows if new quality needs wrapper
  checkWrapperStatus();
}

const Q_NOTES = {
  alac: 'Apple Lossless Audio — highest quality, lossless. Needs <b>wrapper</b> running on ports 10020/20020. Output: .m4a (ALAC container). No media-user-token required.',
  atmos: 'Dolby Atmos / EC-3 spatial audio. Needs <b>wrapper</b>. Set atmos-max in settings (2448 or 2768 kbps). Output: .m4a.',
  aac: 'Standard AAC 256 kbps stereo. Use <code>--aac</code> flag. May require media-user-token for some storefronts.',
  'aac-lc': 'AAC Low Complexity. Requires <b>active media-user-token</b> with subscription.',
  binaural: 'Binaural stereo mix optimised for headphones. Requires <b>wrapper</b>.',
  downmix: 'Stereo downmix from spatial source. Requires <b>wrapper</b>.',
  mv: 'Music Video (MP4 / 1080p). Requires <b>media-user-token</b> + MP4Box + mp4decrypt in PATH.',
};

function renderQualityNote(id) {
  const el = document.getElementById('quality-note');
  if(!el) return;
  const q = QUALITIES.find(x=>x.id===id)||QUALITIES[0];
  el.innerHTML = `<div class="block-title" style="color:${q?.color}">${q?.label} — CLI flag: <code style="color:${q?.color}">${q?.flag||'(default)'}</code></div>
    <div style="font-size:12px;color:var(--muted);line-height:1.7">${Q_NOTES[id]||''}</div>`;
}

// ── TAGS ──────────────────────────────────────────────────────
async function fetchTags() {
  const url = document.getElementById('tag-url').value.trim();
  if(!url){ toast('Enter a URL','var(--red)'); return; }
  document.getElementById('tag-result').innerHTML = `<div class="empty-state" style="height:120px"><div class="empty-icon" style="font-size:28px">⏳</div><div>Fetching from Apple Music API…</div></div>`;
  try {
    const r = await fetch(`/api/meta?url=${encodeURIComponent(url)}`);
    if(!r.ok){ const err=await r.json(); throw new Error(err.detail||'Failed'); }
    const meta = await r.json();
    renderMeta(meta);
  } catch(e) {
    document.getElementById('tag-result').innerHTML = `<div class="empty-state" style="height:120px"><div class="empty-icon" style="font-size:28px">⚠️</div><div class="empty-text">${e.message}</div></div>`;
  }
}

function renderMeta(m) {
  const rows = [
    ['Title', m.title],['Artist', m.artist],['Album', m.album],
    ['Year', m.year],['Genre', m.genre],
    ['Track #', m.trackNumber?(m.trackNumber+(m.totalTracks?'/'+m.totalTracks:'')):''],
    ['Disc #', m.discNumber||''],['ISRC', m.isrc||''],['UPC', m.upc||''],
    ['Label', m.label],['Copyright', m.copyright],
    ['Explicit', m.explicit?'Yes':'No'],
    ['Storefront', m.storefront],['Type', m.type],['ID', m.id],
  ].filter(r=>r[1]);

  const fmts = (m.formats||[]).map(f=>{
    const cls = f.includes('atmos')?'atmos':f.includes('hi-res')?'hires':'';
    return `<span class="fmt-pill ${cls}">${f}</span>`;
  }).join(' ');

  document.getElementById('tag-result').innerHTML = `
    <div class="card">
      <div class="meta-hero">
        ${m.artworkUrl?`<div class="meta-hero-blur" style="background-image:url(${m.artworkUrl})"></div>`:''}
        <div class="meta-hero-content">
          <div class="meta-art">${m.artworkUrl?`<img src="${m.artworkUrl}"/>`:'🎵'}</div>
          <div>
            <div class="meta-ti">${m.title}</div>
            <div class="meta-ar">${m.artist}</div>
            <div class="meta-badges">
              ${m.hasAtmos?'<span class="fmt-pill atmos">ATMOS</span>':''}
              ${m.hasHiRes?'<span class="fmt-pill hires">HI-RES</span>':''}
              ${m.explicit?'<span class="fmt-pill explicit">EXPLICIT</span>':''}
            </div>
          </div>
        </div>
      </div>
      <div class="meta-body">
        ${rows.map(([k,v])=>`<div class="meta-row"><div class="meta-key">${k}</div><div class="meta-val">${v}</div></div>`).join('')}
        ${fmts?`<div class="meta-row"><div class="meta-key">Formats</div><div class="meta-val" style="display:flex;gap:5px;flex-wrap:wrap">${fmts}</div></div>`:''}
        ${m.artworkUrl?`<div class="meta-row"><div class="meta-key">Artwork</div><div class="meta-val"><a href="${m.artworkUrl}" target="_blank" style="color:var(--blue)">${m.artworkUrl.split('/').pop().split('?')[0]}</a></div></div>`:''}
      </div>
    </div>`;
}

// ── SETTINGS ──────────────────────────────────────────────────
function applyConfig() {
  const c = S.config;
  // Shared
  setVal('s-savepath',     c['save-path']||'');
  // Parallel-downloads slider — restore HERE (runs on every config load) and not
  // only inside showStab('global'), which fires on tab CLICK. On a fresh page
  // load the General tab is already active so showStab never ran → the slider
  // stayed at its hardcoded value="1" even though config.yaml had e.g. 2.
  { const _mp = +(c['max-parallel'] || 1);
    setVal('s-max-parallel', _mp);
    const _mpv = document.getElementById('s-max-parallel-val'); if(_mpv) _mpv.textContent = _mp; }
  { const _ad = +(c['auto-delete-minutes'] || 0);
    setVal('s-autodel', _ad);
    const _adv = document.getElementById('s-autodel-val');
    if(_adv) _adv.textContent = (_ad === 0 ? t('gp.off') : _ad + ' ' + t('gp.min')); }
  { const _ap = +(c['amd-parallel'] || 2);
    setVal('s-amd-parallel', _ap); }
  setChk('s-apple-parallel', c['apple-parallel-tracks']);
  setChk('s-quality-subfolders', c['quality-subfolders']);
  setVal('s-transcode-format', c['transcode-format'] || (c['transcode-flac'] ? 'flac' : c['transcode-mp3'] ? 'mp3' : ''));
  setChk('s-transcode-keep', c['transcode-keep-original']);
  setVal('s-rename-tmpl',  c['file-rename-template']||'');
  setChk('s-embedcover', c['embed-cover']);
  setChk('s-savecover',  c['save-cover-to-folder']);
  setVal('s-coversize',  c['cover-size']||'3000x3000');
  setVal('s-coverfmt',   c['cover-format']||'jpg');
  setChk('s-embedlrc',   c['embed-lrc']);
  setChk('s-savelrc',    c['save-lrc-file']);
  setVal('s-lrctype',    c['lrc-type']||'lyrics');
  setVal('s-lrcfmt',     c['lrc-format']||'lrc');
  // zhaarey
  setVal('s-dlpath',     c['main-go-path']||'');
  setChk('s-gorun',      c['use-go-run']);
  setVal('s-wrapper-email', c['wrapper-apple-id']||'');
  setVal('s-wrapper-pass',  c['wrapper-password']||'');
  setVal('s-dport',      c['decrypt-port']||'127.0.0.1:10020');
  setVal('s-mport',      c['m3u8-port']||'127.0.0.1:20020');
  // Apple wrapper selector (local/public/auto) — reflect the saved choice.
  // _hlAppleWrapper lives in settings.html's inline script (executed by views.js).
  try{ if(typeof _hlAppleWrapper==='function') _hlAppleWrapper(c['apple-wrapper']||'public'); }catch(e){}
  setVal('s-mem',        c['max-memory']||256);
  setVal('s-atmosmax',   c['atmos-max']||2448);
  // gamdl
  setChk('s-gamdl-wrapper',    c['gamdl-use-wrapper']);
  setVal('s-gamdl-wrapper-url',c['gamdl-wrapper-account-url']||'http://127.0.0.1:30020');
  toggleGamdlAuth(!!c['gamdl-use-wrapper']);
  setVal('s-cookies-path',   c['gamdl-cookies-path']||'');
  setVal('s-dlmode',         c['gamdl-download-mode']||'ytdlp');
  setVal('s-mvremux',        c['gamdl-mv-remux-mode']||'ffmpeg');
  setVal('s-nm3u8',          c['gamdl-nm3u8dlre-path']||'N_m3u8DL-RE');
  setVal('s-ffmpeg',         c['gamdl-ffmpeg-path']||'ffmpeg');
  setVal('s-tpl-folder',     c['gamdl-album-template']||'{album_artist}/{album}');
  setVal('s-tpl-file',       c['gamdl-file-template']||'{track:02d} {title}');
  setChk('s-overwrite',      c['gamdl-overwrite']);
  setChk('s-savepls',        c['gamdl-save-playlist']);
  setChk('s-autosel',        c['gamdl-artist-auto-select']);
  setVal('s-gamdl-lrc',      c['gamdl-synced-lyrics-format']||'lrc');
  setChk('s-gamdl-nolrc',    c['gamdl-no-synced-lyrics']);
  setChk('s-gamdl-lrconly',  c['gamdl-lyrics-only']);
  setChk('s-gamdl-albumdate',c['gamdl-use-album-date']);
  setChk('s-gamdl-extratags',c['gamdl-fetch-extra-tags']);
  setVal('s-gamdl-mvres',    c['gamdl-mv-resolution']||'1080p');
  setVal('s-gamdl-coversize',c['gamdl-cover-size']||1200);
  setVal('s-gamdl-coverfmt', c['gamdl-cover-format']||'jpg');
  setVal('s-gamdl-exclude',  c['gamdl-exclude-tags']||'');
  setVal('s-gamdl-truncate', c['gamdl-truncate']||100);
  setVal('s-lang',           c['language']||'en-US');
  setVal('t-cookies-path',   c['gamdl-cookies-path']||'');
  updateEngineUI(c['engine']||'zhaarey');
  loadTokensToUI();
}

function setVal(id,v){ const el=document.getElementById(id); if(el) el.value=v??''; }
function setChk(id,v){ const el=document.getElementById(id); if(el) el.checked=!!v; }

const SETTING_KEY_MAP = {
  'download-mode':     'gamdl-download-mode',
  'mv-remux-mode':     'gamdl-mv-remux-mode',
  'nm3u8dlre-path':    'gamdl-nm3u8dlre-path',
  'ffmpeg-path':       'gamdl-ffmpeg-path',
  'overwrite':         'gamdl-overwrite',
  'save-playlist':     'gamdl-save-playlist',
  'skip-mv':           'gamdl-skip-mv',
  'artist-auto-select':'gamdl-artist-auto-select',
  'template-folder':   'gamdl-album-template',
  'template-file':     'gamdl-file-template',
};
// Settings keys a GUEST may change. These are local-preference only —
// stored in browser localStorage, never sent to the server. Trying to write
// anything else as a guest is silently dropped (server would reject anyway).
const GUEST_WRITABLE_PREFIXES = [
  'player-',          // volume, spin, gapless, EQ, viz, mobile-FS, speed, etc
  'guest-quality-',   // per-service preferred default quality
  'guest-folder',     // display label for local download folder
  'releases-',        // their release radar filter prefs
  'language',         // UI language
  'service-colors',   // their per-service color overrides
];
function _isGuestWritable(key) {
  return GUEST_WRITABLE_PREFIXES.some(p => key === p || key.startsWith(p));
}
const _GUEST_PREFS_LS = 'ripster_guest_prefs';
function _guestPrefsLoad() {
  try { return JSON.parse(localStorage.getItem(_GUEST_PREFS_LS) || '{}') || {}; }
  catch { return {}; }
}
function _guestPrefsSave(key, value) {
  const all = _guestPrefsLoad();
  all[key] = value;
  try { localStorage.setItem(_GUEST_PREFS_LS, JSON.stringify(all)); } catch {}
}

// Apply locally-stored guest prefs into S.config on boot, so the UI matches
// what the user picked in a previous session. Called from applyStoredPrefs.
function _guestPrefsApplyAll() {
  if (typeof _isGuest !== 'function' || !_isGuest()) return;
  const prefs = _guestPrefsLoad();
  Object.assign(S.config, prefs);
  if (typeof _playerCfgChanged === 'function') _playerCfgChanged();
}

// Render the guest "Мои настройки" view.
function renderGuestPrefs() {
  const root = document.getElementById('guest-prefs-body');
  if (!root) return;
  const cfg = S.config || {};
  const toggle = (key, label, sub, def = false) => `
    <div class="toggle-row mt8">
      <div class="toggle-info">
        <div class="toggle-label">${esc(label)}</div>
        ${sub ? `<div class="toggle-sub">${esc(sub)}</div>` : ''}
      </div>
      <label class="toggle-wrap"><input type="checkbox" class="toggle-inp"
        ${(cfg[key] ?? def) ? 'checked' : ''}
        onchange="saveSetting('${key}',this.checked);${key==='player-spin'||key==='player-viz'?'_playerCfgChanged?.();':''}_vizConfigChanged?.()"><div class="toggle-slider"></div></label>
    </div>`;
  const QSEL = {
    qobuz:  [['7','Hi-Res 24/96'], ['6','FLAC 16/44'], ['5','MP3 320']],
    tidal:  [['hires','Hi-Res / MAX'], ['lossless','LOSSLESS FLAC'], ['high','AAC 320'], ['low','AAC 96']],
    deezer: [['flac','FLAC'], ['mp3_320','MP3 320'], ['mp3_128','MP3 128']],
  };
  const qSel = (svc) => `
    <div class="field-group">
      <label class="lbl">${esc(svc.toUpperCase())} · ${t('gp.def_quality')}</label>
      <select onchange="saveSetting('guest-quality-${svc}',this.value)" style="width:100%">
        ${QSEL[svc].map(([v,l]) =>
          `<option value="${v}" ${(cfg['guest-quality-'+svc]===v)?'selected':''}>${esc(l)}</option>`
        ).join('')}
      </select>
    </div>`;

  root.innerHTML = `
    <div class="block">
      <div class="block-title">${t('p.player')}</div>
      ${toggle('player-gapless',t('gp.gapless'),t('gp.gapless_sub'))}
      ${toggle('player-preload',t('gp.preload'),t('gp.preload_sub'), true)}
      ${toggle('player-spin',t('gp.spin'), t('gp.spin_sub'), true)}
      ${toggle('player-mobile-fs',t('gp.mobile_fs'),t('gp.mobile_fs_sub'), true)}
      ${toggle('player-viz',t('gp.viz'),t('gp.viz_sub'), false)}
      <div class="settings-grid mt8">
        <div class="field-group">
          <label class="lbl">${t('p.def_volume')}</label>
          <input type="range" min="0" max="1" step="0.05" value="${cfg['player-volume']??1}"
            oninput="saveSetting('player-volume',parseFloat(this.value));const _sv=parseFloat(this.value);if(window._WA?._audioSourceNode||window._WA?.curSource){if(typeof _waSetVolume==='function')_waSetVolume(_sv);const _sa=document.getElementById('pp-audio');if(_sa){_sa.volume=1;_sa.muted=(_sv===0);}}else{const _sa=document.getElementById('pp-audio');if(_sa){_sa.volume=_sv;}}"/>
        </div>
        <div class="field-group">
          <label class="lbl">${t('p.pb_speed')}</label>
          <select onchange="saveSetting('player-speed',parseFloat(this.value));const a=document.getElementById('pp-audio');if(a)a.playbackRate=parseFloat(this.value)">
            <option value="1" ${cfg['player-speed']==1?'selected':''}>1× (${t('gp.normal_word')})</option>
            <option value="1.25" ${cfg['player-speed']==1.25?'selected':''}>1.25×</option>
            <option value="1.5"  ${cfg['player-speed']==1.5?'selected':''}>1.5×</option>
            <option value="1.75" ${cfg['player-speed']==1.75?'selected':''}>1.75×</option>
            <option value="2"    ${cfg['player-speed']==2?'selected':''}>2×</option>
          </select>
        </div>
        <div class="field-group">
          <label class="lbl">${t('p.stream_q')}</label>
          <select onchange="saveSetting('player-stream-quality',this.value)">
            <option value="mp3"      ${ cfg['player-stream-quality']==='mp3'      || !cfg['player-stream-quality'] ? 'selected':''}>MP3 · 320 kbps</option>
            <option value="lossless" ${ cfg['player-stream-quality']==='lossless' ? 'selected':''}>FLAC · Lossless</option>
            <option value="hires"    ${ cfg['player-stream-quality']==='hires'    ? 'selected':''}>Hi-Res · 24-bit</option>
          </select>
        </div>
      </div>
    </div>

    <div class="block mt12">
      <div class="block-title">${t('p.eq')}</div>
      <div class="settings-grid" style="grid-template-columns:1fr 1fr 1fr">
        ${['bass','mid','treble'].map(b => `
          <div class="field-group">
            <label class="lbl">${esc(b==='bass'?t('gp.bass'):b==='mid'?t('gp.mid'):t('gp.high'))} · ${parseFloat(cfg['player-eq-'+b]??0)} dB</label>
            <input type="range" min="-12" max="12" step="0.5" value="${cfg['player-eq-'+b]??0}"
              oninput="setEQ('${b}',this.value);this.previousElementSibling.firstElementChild?.replaceWith(document.createTextNode(this.value+' dB'));this.parentElement.querySelector('.lbl').textContent='${b==='bass'?t('gp.bass'):b==='mid'?t('gp.mid'):t('gp.high')} · '+this.value+' dB'"/>
          </div>`).join('')}
      </div>
      <div style="display:flex;gap:8px;margin-top:10px">
        <button class="btn-ghost btn-sm" onclick="resetEQ();renderGuestPrefs()">↺ ${t('gp.reset')}</button>
      </div>
    </div>

    <div class="block mt12">
      <div class="block-title">${t('p.dl_q')}</div>
      <div class="settings-grid">
        ${qSel('qobuz')}
        ${qSel('tidal')}
        ${qSel('deezer')}
      </div>
      <div style="font-size:10px;color:var(--muted2);margin-top:8px;line-height:1.5">
        ${t('gp.q_note')}
      </div>
    </div>

    <div class="block mt12">
      <div class="block-title">${t('p.ui_lang')}</div>
      <select onchange="setLang(this.value)" style="width:auto">
        <option value="ru" ${(cfg['language']||'ru')==='ru'?'selected':''}>🇷🇺 Русский</option>
        <option value="en" ${cfg['language']==='en'?'selected':''}>🇬🇧 English</option>
        <option value="hi" ${cfg['language']==='hi'?'selected':''}>🇮🇳 हिन्दी</option>
        <option value="ja" ${cfg['language']==='ja'?'selected':''}>🇯🇵 日本語</option>
        <option value="zh" ${cfg['language']==='zh'?'selected':''}>🇨🇳 中文</option>
      </select>
    </div>
  `;
}

// Brief "✓" floating chip near the changed element — confirms auto-save.
// Deduplication: removes any existing chip on the same element before adding a new one.
function _showSavedChip(el) {
  if (!el || !el.getBoundingClientRect) return;
  const rect = el.getBoundingClientRect();
  if (!rect.width) return;
  // Remove existing chip for this element if user triggers again quickly
  const prev = el._savedChip;
  if (prev) { prev.remove(); el._savedChip = null; }
  const chip = document.createElement('span');
  chip.textContent = '✓';
  chip.style.cssText = [
    'position:fixed',
    `left:${Math.min(rect.right - 34, window.innerWidth - 40)}px`,
    `top:${rect.top + rect.height/2 - 11}px`,
    'font-size:11px;font-weight:700;color:var(--green)',
    'background:rgba(62,207,170,.14)',
    'border:1px solid rgba(62,207,170,.25)',
    'border-radius:5px;padding:1px 7px',
    'pointer-events:none;z-index:99999',
    'opacity:0;transition:opacity .15s,transform .3s ease',
    'transform:translateY(2px)',
  ].join(';');
  el._savedChip = chip;
  document.body.appendChild(chip);
  requestAnimationFrame(() => {
    chip.style.opacity = '1';
    chip.style.transform = 'translateY(0)';
    setTimeout(() => {
      chip.style.opacity = '0';
      chip.style.transform = 'translateY(-6px)';
      setTimeout(() => { chip.remove(); if (el._savedChip === chip) el._savedChip = null; }, 320);
    }, 1000);
  });
}

// Dependency updates UI → moved to its own module file (see index.html).

// Tokens view UI → moved to its own module file (see index.html).

// Config YAML editor UI → moved to its own module file (see index.html).

// Console log view → moved to its own module file (see index.html).

// ── PILLS ─────────────────────────────────────────────────────
function updatePills() {
  const c      = S.config;
  const engine = c['engine'] || 'zhaarey';
  // The global QUALITIES is the Apple list — fine for the Apple-side display,
  // and if we're on Deezer/Qobuz that pane of the popover just shows the
  // service name instead of a quality tag.
  const q      = QUALITIES.find(x=>x.id===c['quality']) || QUALITIES[0] || {};

  // ─ Engine-specific readiness dot ─
  // green = ready to download, orange = needs attention, red = missing a critical prereq
  let dotColor = 'var(--muted)';
  const rows   = [];

  const engineLabels = {
    zhaarey: 'zhaarey (Go)',
    gamdl:   'gamdl (Python)',
    amd:     'AMD v2 (Python)',
    deezer:  'Deezer (deemix)',
  };

  rows.push(_detailRow(t('dt.engine'),  engineLabels[engine] || engine, '#0a84ff'));

  if(engine === 'amd') {
    rows.push(_detailRow('Instance', c['amd-instance-url'] || 'wm.wol.moe', 'var(--green)'));
    dotColor = 'var(--green)';  // AMD v2 works via public instance, so OK by default
  } else if(engine === 'gamdl') {
    rows.push(_detailRow('Cookies', (c['gamdl-cookies-path'] ? '✓ '+t('dt.configured') : '✗ '+t('dt.not_configured')),
                         c['gamdl-cookies-path'] ? 'var(--green)' : 'var(--danger)'));
    dotColor = c['gamdl-cookies-path'] ? 'var(--green)' : 'var(--red)';
  } else if(engine === 'zhaarey') {
    // Apple Music with zhaarey needs both tokens
    const mut    = c['media-user-token'];
    const bearer = c['authorization-token'];
    rows.push(_detailRow('MUT',    mut    ? '✓ '+t('dt.set_word') : '✗ '+t('dt.missing_word'), mut    ? 'var(--green)' : 'var(--danger)'));
    rows.push(_detailRow('Bearer', bearer ? '✓ '+t('dt.set_word') : '⏳ '+t('dt.not_received'),  bearer ? 'var(--green)' : 'var(--orange)'));
    if(mut && bearer)       dotColor = 'var(--green)';
    else if(mut || bearer)  dotColor = 'var(--orange)';
    else                    dotColor = 'var(--red)';
  } else if(engine === 'deezer') {
    const arl = c['deezer-arl'];
    rows.push(_detailRow('ARL', arl ? '✓ '+t('dt.set_word') : '✗ '+t('dt.missing_word'), arl ? 'var(--green)' : 'var(--danger)'));
    dotColor = arl ? 'var(--green)' : 'var(--red)';
  }

  if(q && q.label) rows.push(_detailRow(t('dt.quality'), q.label, q.color || '#0a84ff'));
  if(c['storefront']) rows.push(_detailRow('Storefront', (c['storefront']||'').toUpperCase(), '#0a84ff'));

  // Quick-link shortcuts inside the popover
  rows.push('<div style="display:flex;gap:6px;margin-top:10px;flex-wrap:wrap">' +
    `<button class="btn-ghost btn-sm" onclick="closeDetailsPopover();showView('settings',document.querySelector('[data-view=settings]'))">Settings</button>` +
    `<button class="btn-ghost btn-sm" onclick="closeDetailsPopover();showView('tokens',  document.querySelector('[data-view=tokens]'))">Tokens</button>` +
    `<button class="btn-ghost btn-sm" onclick="closeDetailsPopover();showView('quality', document.querySelector('[data-view=quality]'))">Quality</button>` +
  '</div>');

  const body = document.getElementById('tb-details-body');
  if(body) body.innerHTML = rows.join('');
  const dot  = document.getElementById('tb-details-dot');
  if(dot) dot.style.background = dotColor;
}

function _detailRow(label, value, color) {
  return `<div style="display:flex;justify-content:space-between;align-items:center;padding:5px 0;border-bottom:1px solid var(--border)">
    <span style="color:var(--muted);font-size:11px">${label}</span>
    <span style="color:${color||'var(--text)'};font-weight:600;font-size:12px">${value}</span>
  </div>`;
}

function toggleDetailsPopover(ev) {
  ev?.stopPropagation();
  const pop = document.getElementById('tb-details-pop');
  if(!pop) return;
  const visible = pop.style.display !== 'none';
  if(visible) { pop.style.display = 'none'; return; }
  updatePills();  // refresh contents before showing
  pop.style.display = '';
  // Close on outside click
  setTimeout(() => {
    document.addEventListener('click', _detailsOutsideClick, { once: true });
  }, 0);
}
function closeDetailsPopover(){
  const pop = document.getElementById('tb-details-pop');
  if(pop) pop.style.display = 'none';
}
function _detailsOutsideClick(e) {
  const pop = document.getElementById('tb-details-pop');
  const btn = document.getElementById('tb-details-btn');
  if(!pop || !btn) return;
  if(pop.contains(e.target) || btn.contains(e.target)) {
    // re-arm listener because popover wasn't actually closed
    document.addEventListener('click', _detailsOutsideClick, { once: true });
    return;
  }
  closeDetailsPopover();
}
function pill(type, text){ return `<div class="pill pill-${type}"><div class="dot"></div>${text}</div>`; }

// ── HELPERS ───────────────────────────────────────────────────
function setStatus(text, color) {
  const el = document.getElementById('tb-status');
  if(el){ el.textContent=text; el.style.color=color; }
}



// ── Contextual field help ("?" icon + tap/click popover) ──────────────────
// helpQ('i18n.key') returns markup for an inline "?" icon; clicking it opens
// a small popover with t('i18n.key') as HTML (data-i18n-html semantics —
// content can use <b>/<code>/<a> same as the rest of the i18n system).
// Works via click (not hover-only), so it's usable on touch devices, unlike
// the native title="" tooltips this replaces on the Setup checklist.
function helpQ(key){
  return `<span class="help-q" onclick="event.stopPropagation();showHelpPop(this,'${key}')" role="button" tabindex="0">?</span>`;
}

let _helpPopEl = null, _helpPopKey = null;
function showHelpPop(anchor, key){
  // second click on the SAME icon while its popover is open → close (toggle)
  if(_helpPopEl && _helpPopKey === key){ closeHelpPop(); return; }
  closeHelpPop();
  const html = t(key);
  if(!html) return;
  const pop = document.createElement('div');
  pop.className = 'help-pop';
  pop.innerHTML = html;
  document.body.appendChild(pop);
  const r = anchor.getBoundingClientRect();
  const pw = pop.offsetWidth, ph = pop.offsetHeight;
  let left = Math.min(Math.max(8, r.left), window.innerWidth - pw - 8);
  let top  = r.bottom + 8;
  if(top + ph > window.innerHeight - 8) top = Math.max(8, r.top - ph - 8);  // flip above if no room below
  pop.style.left = left + 'px';
  pop.style.top  = top + 'px';
  _helpPopEl = pop; _helpPopKey = key;
  setTimeout(() => {  // next tick so the opening click doesn't immediately close it
    document.addEventListener('click', _onHelpPopOutsideClick);
    document.addEventListener('keydown', _onHelpPopEscape);
  }, 0);
}
function closeHelpPop(){
  if(_helpPopEl){ _helpPopEl.remove(); _helpPopEl = null; _helpPopKey = null; }
  document.removeEventListener('click', _onHelpPopOutsideClick);
  document.removeEventListener('keydown', _onHelpPopEscape);
}
function _onHelpPopOutsideClick(e){ if(_helpPopEl && !_helpPopEl.contains(e.target)) closeHelpPop(); }
function _onHelpPopEscape(e){ if(e.key === 'Escape') closeHelpPop(); }

// ── Notification stack ──────────────────────────────────────────
let toastTimer; // kept for legacy compat
const _notifTimers = new Map();

function toast(msg, color='var(--green)', sub='', duration=6000) {
  const stack = document.getElementById('notif-stack');
  if(!stack) return;

  const id = 'n' + Date.now() + Math.random().toString(36).slice(2,6);
  const el = document.createElement('div');
  el.className = 'notif notif-enter';
  el.id = id;

  // Resolve color to a valid CSS value
  const dotColor = color.startsWith('var(') ? color : color;

  el.innerHTML = `
    <div class="notif-dot" style="background:${dotColor};color:${dotColor}"></div>
    <div class="notif-body">
      <div class="notif-msg">${msg}</div>
      ${sub ? `<div class="notif-sub">${sub}</div>` : ''}
      <div class="notif-prog" id="prog-${id}" style="background:${dotColor};width:100%;max-width:100%"></div>
    </div>
    <div class="notif-close" onclick="_closeNotif('${id}')">✕</div>`;

  stack.appendChild(el);

  // Animate in after paint
  requestAnimationFrame(()=>{
    requestAnimationFrame(()=>{ el.classList.remove('notif-enter'); });
  });

  // Progress bar countdown
  const prog = document.getElementById('prog-'+id);
  if(prog) {
    const start = Date.now();
    const tick = () => {
      const pct = Math.max(0, 100 - (Date.now()-start)/duration*100);
      prog.style.width = pct+'%';
      if(pct > 0) requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  }

  // Auto-dismiss
  const timer = setTimeout(()=>_closeNotif(id), duration);
  _notifTimers.set(id, timer);

  // Max 5 notifications — remove oldest
  const all = stack.querySelectorAll('.notif');
  if(all.length > 5) _closeNotif(all[0].id);
}

function _closeNotif(id) {
  clearTimeout(_notifTimers.get(id));
  _notifTimers.delete(id);
  const el = document.getElementById(id);
  if(!el) return;
  el.classList.add('notif-exit');
  setTimeout(()=>el.remove(), 300);
}

// Setup tab provisioning UI → moved to its own module file (see index.html).

// Ripster self-update UI → moved to its own module file (see index.html).

// ── THEME & FONT ────────────────────────────────────────────────────────
const FONT_MAP = {
  system:  { css: "system-ui,-apple-system,'Segoe UI',sans-serif", display: "system-ui,-apple-system,'Segoe UI',sans-serif" },
  dm:      { css: "'DM Sans',system-ui,sans-serif",      display: "'DM Sans',sans-serif" },
  inter:   { css: "'Inter',system-ui,sans-serif",         display: "'Inter',sans-serif" },
  geist:   { css: "'Geist',system-ui,sans-serif",         display: "'Geist',sans-serif" },
  nunito:  { css: "'Nunito',system-ui,sans-serif",        display: "'Nunito',sans-serif" },
  roboto:  { css: "'Roboto',system-ui,sans-serif",        display: "'Roboto',sans-serif" },
  manrope: { css: "'Manrope',system-ui,sans-serif",       display: "'Manrope',sans-serif" },
};

function setTheme(t) {
  document.documentElement.classList.toggle('light', t === 'light');
  localStorage.setItem('amd-theme', t);
  // update cards
  document.getElementById('theme-dark')?.style.setProperty('border-color', t==='dark' ? 'var(--red)' : 'transparent');
  document.getElementById('theme-light')?.style.setProperty('border-color', t==='light' ? '#e8000f' : 'transparent');
  document.getElementById('theme-btn').textContent = t === 'light' ? '☀️' : '🌙';
  // update selects
  ['theme-dark-opt','theme-light-opt'].forEach(id => {});
}

function toggleTheme() {
  const isLight = document.documentElement.classList.contains('light');
  setTheme(isLight ? 'dark' : 'light');
}

function setFont(key) {
  const f = FONT_MAP[key];
  if(!f) return;
  document.documentElement.style.setProperty('--font', f.css);
  document.documentElement.style.setProperty('--display', f.display);
  localStorage.setItem('amd-font', key);
  // sync both pickers
  const p1 = document.getElementById('font-picker');
  const p2 = document.getElementById('font-picker2');
  if(p1) p1.value = key;
  if(p2) p2.value = key;
  // update preview
  const prev = document.getElementById('font-preview');
  if(prev) prev.style.fontFamily = f.css;
}

// Re-merge guest-local prefs (localStorage) into S.config and re-mirror them
// into the player/settings UI. Must run not just on first boot but every time
// S.config gets wholesale-replaced — the 'init' WS message (sent on EVERY
// reconnect, not just first load) overwrites S.config from the server, which
// never carries guest-only prefs like player-spin. Without re-applying here,
// a guest's "spin off" choice silently reverts on the next reconnect — which
// on mobile (screen lock / app background / network switch) happens often,
// making the toggle look like it "keeps turning itself back on".
function _applyPlayerPrefsToUI() {
  if (!S.config) return;
  // For guests — pull their browser-stored prefs into S.config first.
  try { _guestPrefsApplyAll?.(); } catch {}
  const spin = S.config['player-spin'] !== false;   // default true
  document.body.classList.toggle('no-spin', !spin);
    const vol = S.config['player-volume'];
    if (typeof vol === 'number') {
      const a = document.getElementById('pp-audio'); if (a) a.volume = vol;
      ['pp-vol','pp-vol-big'].forEach(id => { const el = document.getElementById(id); if(el) el.value = vol; });
    }
    const sp  = S.config['player-speed'];
    if (typeof sp === 'number') {
      const a = document.getElementById('pp-audio'); if (a) a.playbackRate = sp;
    }
    // Mirror saved values into settings-tab inputs
    const set = (id, v) => { const el = document.getElementById(id); if (el) { if (el.type==='checkbox') el.checked=!!v; else el.value=v; } };
    set('s-player-gapless',   S.config['player-gapless']);
    set('s-player-preload',   S.config['player-preload'] !== false);
    set('s-player-spin',      spin);
    set('s-player-mobile-fs', S.config['player-mobile-fs'] !== false);
    set('s-player-viz',       !!S.config['player-viz']);
    set('s-player-volume',    typeof vol === 'number' ? vol : 1);
    set('s-player-speed',     sp || 1);
    set('s-player-stream-quality', S.config['player-stream-quality'] || 'mp3');
    // EQ slider values + labels
    ['bass','mid','treble'].forEach(b => {
      const v = parseFloat(S.config['player-eq-'+b] ?? 0) || 0;
      const sl = document.getElementById('s-eq-'+b); if (sl) sl.value = v;
      const lb = document.getElementById('s-eq-'+b+'-val');
      if (lb) lb.textContent = v > 0 ? `+${v}` : String(v);
    });
}

function applyStoredPrefs() {
  const t = localStorage.getItem('amd-theme') || 'dark';
  const f = localStorage.getItem('amd-font')  || 'system';
  setTheme(t);
  setFont(f);
  // Player preferences land from S.config (which is loaded a bit later on
  // first boot) — poll until it's available, then apply.
  const tryApply = () => {
    if (!S.config) { setTimeout(tryApply, 100); return; }
    _applyPlayerPrefsToUI();
  };
  tryApply();
}


// ── ENGINE SWITCHER ───────────────────────────────────────────
function toggleGamdlAuth(useWrapper) {
  const ws = document.getElementById('gamdl-wrapper-section');
  const cs = document.getElementById('gamdl-cookies-section');
  if(ws) ws.style.display = useWrapper ? '' : 'none';
  if(cs) cs.style.display = useWrapper ? 'none' : '';
}

function updateEngineUI(engine) {
  const isGamdl = engine === 'gamdl';
  const isAMD   = engine === 'amd';
  const isZhaar = engine === 'zhaarey';
  // Switch step track defs
  if(typeof renderChecklist === 'function') renderChecklist();
  // Topbar buttons
  const zh  = document.getElementById('eng-zh');
  const gm  = document.getElementById('eng-gm');
  const amd = document.getElementById('eng-amd');
  const _b = 'flex:1;padding:7px 10px;font-size:11px;font-weight:700;border:none;cursor:pointer;border-radius:7px;transition:all .15s;font-family:var(--display);';
  if(zh)  zh.style.cssText  = _b + (isZhaar ? 'background:var(--red);color:#fff'   : 'background:transparent;color:var(--muted)');
  if(gm)  gm.style.cssText  = _b + (isGamdl ? 'background:var(--blue);color:#fff'  : 'background:transparent;color:var(--muted)');
  if(amd) amd.style.cssText = _b + (isAMD   ? 'background:var(--green);color:#fff' : 'background:transparent;color:var(--muted)');
  // Settings blocks
  const zhB = document.getElementById('zhaarey-settings-block');
  const gmB = document.getElementById('gamdl-settings-block');
  const amB = document.getElementById('amd-settings-block');
  if(zhB) zhB.style.display = isZhaar ? '' : 'none';
  if(gmB) gmB.style.display = isGamdl ? '' : 'none';
  if(amB) amB.style.display = isAMD   ? '' : 'none';
  // Unified cover/tag option sections
  const coverGamdl = document.getElementById('cover-gamdl-opts');
  const tagsGamdl  = document.getElementById('tags-gamdl-opts');
  const tagsAmd    = document.getElementById('tags-amd-opts');
  if(coverGamdl) coverGamdl.style.display = isGamdl ? '' : 'none';
  if(tagsGamdl)  tagsGamdl.style.display  = isGamdl ? '' : 'none';
  if(tagsAmd)    tagsAmd.style.display     = isAMD   ? '' : 'none';
  // Tokens blocks
  const bearerB  = document.getElementById('bearer-token-block');
  const cookiesB = document.getElementById('cookies-token-block');
  if(bearerB)  bearerB.style.display  = isGamdl ? 'none' : '';
  if(cookiesB) cookiesB.style.display = isGamdl ? '' : 'none';
  // Wrapper banner: only for zhaarey
  if(!isGamdl) checkWrapperStatus();
  else {
    const wb = document.getElementById('wrapper-banner');
    if(wb) wb.style.display='none';
  }
  // Apple-wrapper preference (local/public/auto) is always active — it is the
  // single source of truth for which Apple lossless wrapper is used, regardless
  // of the engine button. (Earlier it was greyed on non-zhaarey engines, which
  // wrongly blocked choosing "local" while on AMD.)
  const _awHint=document.getElementById('aw-disabled-hint');
  if(_awHint) _awHint.style.display='none';
}

async function switchEngine(engine) {
  S.config['engine'] = engine;
  const r  = await api('POST','/api/engine',{engine});
  if(!r.ok){ toast('Error switching engine','var(--red)'); return; }
  // Update qualities list
  QUALITIES.length = 0;
  QUALITIES.push(...(r.qualities||[]));
  // Rebuild quality select
  const sel = document.getElementById('url-quality');
  if(sel) sel.innerHTML = QUALITIES.map(q=>`<option value="${q.id}">${q.label} — ${q.sub}</option>`).join('');
  // Pick first quality of new engine
  const first = QUALITIES[0];
  if(first && S.config['quality'] !== first.id) {
    S.config['quality'] = first.id;
    await api('POST','/api/config',{quality: first.id});
    if(sel) sel.value = first.id;
  }
  updateEngineUI(engine);
  renderQualityGrid();
  updatePills();
  updateQualitySelector('apple');
  const _msgs = {zhaarey:'🔵 zhaarey engine', gamdl:'🐍 gamdl engine', amd:'✨ '+t('t.amd_v2_msg')};
  const _clrs = {zhaarey:'var(--blue)', gamdl:'var(--blue)', amd:'var(--green)'};
  toast(_msgs[engine]||engine, _clrs[engine]||'var(--text)');
  if(engine === 'amd') checkAMDWrapperStatus();
}

// cookies.txt upload UI → moved to its own module file (see index.html).

// ── Album per-track selection: checkboxes + select-all / per-disc / clear ──────
function _albumSelCbs(){ return Array.from(document.querySelectorAll('#detail-content .alb-trk-cb')); }
function albumSelectAll(on){ _albumSelCbs().forEach(cb => { if(!cb.disabled) cb.checked = !!on; }); _albumUpdateSelCount(); }
function albumSelectDisc(d){ _albumSelCbs().forEach(cb => { cb.checked = !cb.disabled && String(cb.dataset.disc) === String(d); }); _albumUpdateSelCount(); }
function _albumUpdateSelCount(){
  const n = _albumSelCbs().filter(cb => cb.checked).length;
  const b = document.getElementById('alb-dl-sel');
  if(b){ b.textContent = '⬇ '+ti('ck.dl_sel_n',{n:n}); b.disabled = n === 0; b.style.opacity = n ? '1' : '.5'; }
}
async function albumDownloadSelected(){
  const sel = _albumSelCbs().filter(cb => cb.checked && cb.dataset.url);
  if(!sel.length){ toast(t('t.check_tracks'),'var(--orange)'); return; }
  const svc = (typeof Detail !== 'undefined' && Detail.currentAlbum) ? Detail.currentAlbum.service : 'apple';
  const q = resolveQuality(svc);
  const b = document.getElementById('alb-dl-sel');
  if(b){ b.disabled = true; b.textContent = t('t.adding'); }
  let ok = 0;
  for(const cb of sel){
    try { const r = await api('POST','/api/queue/add',{url: cb.dataset.url, quality: q}); if(r && r.ok) ok++; } catch {}
  }
  toast(`+ ${ok}/${sel.length} ${t('ck.trk_to_queue')}`, ok ? 'var(--green)' : 'var(--red)');
  if(b){ b.textContent = '⬇ '+ti('ck.dl_sel_n',{n:sel.length}); b.disabled = false; b.style.opacity = '1'; }
}

async function artistReleaseDownload(service, releaseId, title, artist) {
  try {
    const r = await fetch(`/api/album/${service}/${encodeURIComponent(releaseId)}`);
    const d = await r.json();
    const url = d.album?.url;
    if (!url) { toast(t('t.no_alb_url'), 'var(--red)'); return; }
    const res = await api('POST', '/api/queue/add', {url, quality: resolveQuality(service), title, artist});
    if (res.ok) toast('+ '+title+' → '+t('q.queue_word'));
    else toast(t('t.error_c') + (res.detail || '?'), 'var(--red)');
  } catch(e) {
    toast(t('err.generic') + ': ' + e.message, 'var(--red)');
  }
}

// Queue every release currently shown on the artist page (respects the active
// type filter). Works for any service: each release resolves to a real URL —
// from r.url when present, else a one-off /api/album lookup — then goes into the
// queue at that service's configured quality. Sequential so a huge discography
// doesn't fire 100 parallel lookups; progress is reported via toasts.
async function downloadArtistDiscography(){
  const A = (typeof Detail !== 'undefined') ? Detail.currentArtist : null;
  if(!A){ return; }
  const items = (A.filter && A.filter !== 'all')
    ? (A.releases || []).filter(r => r.type === A.filter)
    : (A.releases || []);
  if(!items.length){ toast(t('ck.cat_empty'), 'var(--muted)'); return; }
  const name = (A.artist && A.artist.name) || '';
  if(!confirm(ti('ck.dl_all_confirm', {n: items.length, name}))) return;
  toast(ti('ck.dl_all_start', {n: items.length}), 'var(--muted)', '', 3000);
  let ok = 0, fail = 0;
  for(const r of items){
    try{
      let url = r.url;
      if(!url){
        const rr = await fetch(`/api/album/${r.service}/${encodeURIComponent(r.id)}`);
        const dd = await rr.json();
        url = dd.album?.url;
      }
      if(!url){ fail++; continue; }
      const res = await api('POST', '/api/queue/add',
        {url, quality: resolveQuality(r.service), title: r.title, artist: name});
      if(res && res.ok) ok++; else fail++;
    }catch(e){ fail++; }
  }
  toast(ti('ck.dl_all_done', {ok, fail}), ok ? 'var(--green)' : 'var(--red)', '', 5000);
}

async function albumAddTrack(urlOrId, title, artist){
  if(!urlOrId || !urlOrId.startsWith('http')){
    toast(t('t.no_trk_url'),'var(--red)'); return;
  }
  const r = await api('POST', '/api/queue/add', {url: urlOrId, quality: resolveQuality(detectSvcFromUrl(urlOrId) || 'apple'), title, artist});
  if(r.ok) toast(`+ ${title}`);
  else toast(t('t.error_c')+(r.detail||'?'),'var(--red)');
}

// ─── Preview player ──────────────────────────────────────────────────────
// Player module extracted to /static/js/player.js

// Lightbox click-to-zoom → moved to its own module file (see index.html).

// ── Watchlist status line (driven by WS events) ───────────────────────────
// Shown while `/api/watchlist/check` (or the 6-hour background loop) runs.
// Design: single row with text + count + thin progress bar. Auto-hides via
// clearWatchlistStatus() 4s after completion.
function setWatchlistStatus(text, current, total, color){
  const bar   = document.getElementById('wl-status-bar');
  const txt   = document.getElementById('wl-status-text');
  const cnt   = document.getElementById('wl-status-count');
  const fill  = document.getElementById('wl-status-fill');
  if(!bar) return;
  bar.style.display = '';
  if(txt) txt.textContent = text || '';
  if(cnt) cnt.textContent = total ? `${current}/${total}` : '';
  if(fill) {
    const pct = total > 0 ? Math.round(100 * current / total) : 0;
    fill.style.width = pct + '%';
    fill.style.background = color || 'var(--red)';
  }
}
function clearWatchlistStatus(){
  const bar = document.getElementById('wl-status-bar');
  if(bar) bar.style.display = 'none';
}

// ── Releases view status line (Spotify scan progress) ─────────────────────
// The Spotify releases scan walks every followed artist and their albums —
// easily 30+ seconds for well-followed users. Without status, "Загружаю
// релизы…" looks frozen. These helpers render a live progress bar driven by
// WS events (releases_scan_start / _progress / _done).
function setReleasesStatus(text, current, total, color){
  const bar   = document.getElementById('rel-status-bar');
  const txt   = document.getElementById('rel-status-text');
  const cnt   = document.getElementById('rel-status-count');
  const fill  = document.getElementById('rel-status-fill');
  if(!bar) return;
  bar.style.display = '';
  if(txt) txt.textContent = text || '';
  if(cnt) cnt.textContent = total ? `${current}/${total}` : '';
  if(fill) {
    const pct = total > 0 ? Math.round(100 * current / total) : 0;
    fill.style.width = pct + '%';
    fill.style.background = color || '#1db954';
  }
}
function clearReleasesStatus(){
  const bar = document.getElementById('rel-status-bar');
  if(bar) bar.style.display = 'none';
}

// Service detection in URL bar → moved to its own module file (see index.html).

// Service login UIs + token probe + Tidal import → moved to its own module file (see index.html).

// Statistics view → moved to its own module file (see index.html).

// OrpheusDL (Spotify) setup UI → moved to its own module file (see index.html).

// SoundCloud / Lucida tab UI → moved to its own module file (see index.html).

// ── Release-radar poll fallback ────────────────────────────────────────────
// The WS done event can be missed if the user switches tabs and the tunnel
// drops the connection mid-scan. As a belt-and-suspenders we poll the GET
// endpoint while a scan is running and render whatever the server has.
let   _relPollHandle = null;
const _REL_POLL_MS   = 8000;
const _REL_POLL_MAX_RUNS = 60;     // ~8 minutes
let   _relPollRuns   = 0;
function _relStopPoll() {
  if (_relPollHandle) { clearInterval(_relPollHandle); _relPollHandle = null; }
  _relPollRuns = 0;
}
function _relStartPoll() {
  if (_relPollHandle) return;
  _relPollRuns = 0;
  _relPollHandle = setInterval(_relPollOnce, _REL_POLL_MS);
}
async function _relPollOnce() {
  _relPollRuns++;
  if (_relPollRuns > _REL_POLL_MAX_RUNS) {
    _relStopPoll();
    setReleasesStatus('✗ '+t('w.scanner_dead'), 0, 1, 'var(--red)');
    return;
  }
  const days     = document.getElementById('rel-days')?.value || (S.config?.['releases-days'] || '90');
  const cfgTypes = (S.config?.['releases-types'] || 'album,single');
  let spTypes = 'album,single,compilation';
  if (cfgTypes.includes('appears_on')) spTypes += ',appears_on';
  try {
    const r = await fetch(`/api/spotify/releases?days=${days}&types=${encodeURIComponent(spTypes)}`);
    const d = await r.json();
    if (d?.last_error && !d.scanning) {
      _relStopPoll();
      const st = document.getElementById('rel-status');
      if (st) { st.textContent = '✗ ' + d.last_error; st.style.color = 'var(--red)'; }
      _relShowAuthHint(d.last_error);
      return;
    }
    if (d?.releases?.length && !d.scanning) {
      _relStopPoll();
      _relCache.data = d.releases;
      _relCache.ts   = Date.now();
      _relCache.key  = _relCacheKey();
      _relSaveLS(d.releases, _relCacheKey());
      const st = document.getElementById('rel-status');
      if (st) st.style.display = 'none';
      _applyRelFilter();
    }
  } catch (e) { /* keep polling */ }
}
function _relShowAuthHint(err) {
  const lower = (err || '').toLowerCase();
  const sp_dc = lower.includes('sp_dc') || lower.includes('cookie') || lower.includes('не авторизован') || lower.includes('обновить oauth');
  if (!sp_dc) return;
  // Reuse the toast — keep wording concrete + action-oriented
  try { toast(t('t.sp_c') + err + ' — Settings → Spotify', 'var(--orange)', 9000); } catch {}
}

async function loadReleases(force = false) {
  _renderRelActiveSvcs();

  const days  = document.getElementById('rel-days')?.value  || (S.config?.['releases-days'] || '90');
  const grid  = document.getElementById('releases-grid');
  const st    = document.getElementById('rel-status');
  const empty = document.getElementById('rel-empty');
  const btn   = document.getElementById('rel-refresh-btn');

  // Always fetch a superset of release types so the client-side chip filter
  // has data to work with (appears_on stays opt-in — it makes the scan heavy).
  const cfgTypes = (S.config?.['releases-types'] || 'album,single');
  let spTypes = 'album,single,compilation';
  if (cfgTypes.includes('appears_on')) spTypes += ',appears_on';

  if (force) {
    _relCache.data = null;
    _relCache.ts   = 0;
    try { localStorage.removeItem(_REL_LS_KEY); } catch(e) {}
  }

  const hasPrev = !!_relCache.data?.length;

  if (!hasPrev) {
    if(grid)  grid.innerHTML = '';
    if(empty) empty.style.display = 'none';
  }
  if(st) { st.textContent = hasPrev ? t('su.updating') : t('w.loading_rel'); st.style.display = 'block'; }
  if(btn) btn.disabled = true;

  const activeSvcs = _relActiveSvcs();
  const useSpotify = activeSvcs.includes('spotify');
  const useQobuz   = activeSvcs.includes('qobuz');
  const useTidal   = activeSvcs.includes('tidal');

  const fetches = [];
  if(useSpotify) fetches.push(
    fetch(`/api/spotify/releases?days=${days}&types=${encodeURIComponent(spTypes)}${force ? '&force=1' : ''}`)
      .then(r => r.json()).catch(e => ({ok: false, releases: [], error: e.message}))
  );
  if(useQobuz) fetches.push(
    fetch(`/api/releases/qobuz?days=${days}`)
      .then(r => r.json()).catch(e => ({ok: false, releases: [], error: e.message}))
  );
  if(useTidal) fetches.push(
    fetch(`/api/releases/tidal?days=${days}`)
      .then(r => r.json()).catch(e => ({ok: false, releases: [], error: e.message}))
  );

  if(!fetches.length) {
    if(st) st.style.display = 'none';
    if(btn) btn.disabled = false;
    if(!hasPrev && empty) {
      empty.textContent = t('t.no_services');
      empty.style.display = '';
    }
    return;
  }

  const settled = await Promise.allSettled(fetches);

  let allReleases = [];
  const errors = [];
  let anyScanning = false;
  for(const r of settled) {
    if(r.status === 'fulfilled') {
      if(r.value?.releases?.length) allReleases.push(...r.value.releases);
      if(!r.value?.ok && r.value?.error) errors.push(r.value.error);
      if(r.value?.scanning) anyScanning = true;
    }
  }

  if(st) st.style.display = 'none';
  if(btn) btn.disabled = false;

  // Backend is running a background scan — WS will push results when done.
  // We also start a poll fallback in case the WS message is lost (slow client,
  // tab in background, broker drop, tunnel cut).
  if(anyScanning && !allReleases.length && !hasPrev) {
    if(st) { st.textContent = t('t.scanning'); st.style.display = 'block'; }
    _relStartPoll();
    return;
  }
  _relStopPoll();

  // Deduplicate by title+artist+year across services
  const seen = new Set();
  allReleases = allReleases.filter(rel => {
    const key = `${(rel.title||'').toLowerCase()}|${(rel.artist||'').toLowerCase()}|${(rel.year||rel.date||'').slice(0,4)}`;
    if(seen.has(key)) return false;
    seen.add(key);
    return true;
  });
  allReleases.sort((a, b) => (b.date || '').localeCompare(a.date || ''));

  if(errors.length) {
    const has403 = errors.some(e => e && e.toLowerCase().includes('not registered'));
    if(has403) {
      toast(t('t.sp_403'), 'var(--orange)', 8000);
    } else {
      toast('⚠ ' + errors.slice(0, 2).join('; '), 'var(--orange)', 4000);
    }
  }

  if(!allReleases.length) {
    // No new results — keep previous data visible if available
    if(hasPrev) {
      toast(t('t.no_new_rel'), 'var(--muted)', 3000);
    } else {
      if(empty) empty.style.display = '';
    }
    return;
  }

  // New results found — update cache + localStorage + render
  _relCache.data = allReleases;
  _relCache.ts   = Date.now();
  _relCache.key  = _relCacheKey();
  _relSaveLS(allReleases, _relCacheKey());

  if(empty) empty.style.display = 'none';
  _applyRelFilter();
}

// kept for compatibility — Spotify-specific redirect to choice toast
async function convertRelease(spotifyUrl, title, artist) {
  _showSpotifyChoiceToast(spotifyUrl, S.config['quality'] || 'alac');
}

// SoundCloud UI extracted to /static/js/sc.js

// Media Session + local library + play-album + quality + spectrogram → moved to its own module file (see index.html).



















