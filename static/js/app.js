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
  // Defensive: t() returns the raw KEY when a translation is missing. Assign only
  // when a real translation exists (v !== key) — otherwise KEEP the element's
  // existing HTML fallback text instead of showing an ugly raw key like
  // "setup.deps_hdr". Fixes the whole missing-key class, not one-off keys.
  const _set = (el, attr, prop) => { const k = el.dataset[attr], v = t(k); if (v !== k) el[prop] = v; };
  document.querySelectorAll('[data-i18n]').forEach(el => _set(el, 'i18n', 'textContent'));
  document.querySelectorAll('[data-i18n-ph]').forEach(el => _set(el, 'i18nPh', 'placeholder'));
  document.querySelectorAll('[data-i18n-title]').forEach(el => _set(el, 'i18nTitle', 'title'));
  document.querySelectorAll('[data-i18n-html]').forEach(el => _set(el, 'i18nHtml', 'innerHTML'));
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
  if (!r || !r.ok) { toast('Ошибка', 'var(--red)'); return; }
  if (r.restarting) { toast('Гости простаивают — перезапускаю сейчас', 'var(--green)'); return; }
  if (r.pending) {
    toast(`↺ Перезапущу, как гости освободятся (сессий: ${r.sessions||0}${r.queue_running?', очередь идёт':''})`, 'var(--accent)', '', 5000);
  } else {
    toast('Отложенный рестарт снят', 'var(--muted)');
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
    pullQueue();   // re-sync the queue over REST on EVERY (re)connect — heals a
                   // half-open socket that silently missed queue_update pushes
                   // (the "task didn't appear / console empty, only F5 fixes it" bug)
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
    // Open but silent past ~1.5× the server's 20s heartbeat → probably half-open.
    // Cycle it (a needless cycle on a live server is cheap: reconnect re-sends init
    // + pullQueue re-syncs instantly). Was 45s — too slow to feel responsive.
    try { ws.close(); } catch {}  // triggers onclose → reconnect in 2s
  }
}
setInterval(_wsWatchdog, 15000);
// Self-heal the queue: a socket can stay "alive" (log/progress events keep
// resetting _wsLastMsg so the watchdog never cycles it) yet silently miss a
// queue_update — leaving S.queue frozen on a stale snapshot ("bot added a task
// but Ripster never shows it"). Periodically re-pull the authoritative REST queue.
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
      applyConfig(); renderQueue(); updateTransport(); updatePills(); renderQualityGrid(); renderConfig(); _syncReleasesSettingsTab();
      setTimeout(_maybeAskTelemetryName, 2500);  // first-run: ask the tester for a name so the dev can tell instances apart
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
      const title = msg.title || 'трек';
      toast(`🔁 SC недоступен → ${svc}: ${title}`, '#ff5500', '', 4500);
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
              toggle.textContent = (toggle.textContent.startsWith('▼') ? '▼' : '▶') + ` лог (${panel.children.length})`;
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
      toast('Spotify подключён!', '#1db954');
      break;
    }
    case 'spotify_sp_dc_updated': {
      const inp = document.getElementById('s-sp-dc');
      const sts = document.getElementById('sp-dc-auto-status');
      if(sts) { sts.textContent = '✓ sp_dc сохранена'; sts.style.color = 'var(--green)'; }
      loadSpotifyStatus();
      toast('sp_dc обновлена!', '#1db954');
      // Reload config to update the input field value
      api('GET','/api/config').then(cfg => { if(inp && cfg['spotify-sp-dc']) inp.value = cfg['spotify-sp-dc']; });
      break;
    }
    case 'apple_authed': {
      refreshAppleAuthStatus();
      toast('🍎 Apple Music токен обновлён!', '#0a84ff');
      break;
    }
    case 'watchlist_new_release': {
      const txt = `Новый релиз: ${msg.release || ''} — ${msg.artist || ''}`;
      toast(txt, 'var(--green)');
      // Refresh watchlist if open
      if(document.getElementById('view-watchlist')?.style.display !== 'none') loadWatchlist();
      break;
    }
    case 'watchlist_check_start': {
      setWatchlistStatus(`⟳ Проверяю ${msg.total} артистов…`, 0, msg.total);
      break;
    }
    case 'watchlist_check_progress': {
      setWatchlistStatus(`⟳ ${msg.current}/${msg.total} · ${msg.artist}`, msg.current, msg.total);
      break;
    }
    case 'watchlist_check_done': {
      if(msg.new > 0) setWatchlistStatus(`✓ Проверено ${msg.checked} · новых релизов: ${msg.new}`, msg.checked, msg.checked, 'var(--green)');
      else            setWatchlistStatus(`✓ Проверено ${msg.checked} · новых нет`, msg.checked, msg.checked);
      // Auto-clear after 4s
      setTimeout(() => clearWatchlistStatus(), 4000);
      break;
    }
    case 'releases_scan_start': {
      const _svcLbl = msg.service ? ` [${msg.service}]` : '';
      const _svcClr = ({spotify:'#1db954',qobuz:'#1870f5',tidal:'#00d4b3'})[msg.service] || 'var(--red)';
      if(msg.phase === 'artists') {
        setReleasesStatus(`⟳ Получаю артистов${_svcLbl}…`, 0, 1, _svcClr);
      } else if(msg.phase === 'albums') {
        setReleasesStatus(`⟳ Сканирую ${msg.total} арт.${_svcLbl}…`, 0, msg.total, _svcClr);
      }
      break;
    }
    case 'releases_scan_progress': {
      const _svcLbl2 = msg.service ? ` [${msg.service}]` : '';
      const _svcClr2 = ({spotify:'#1db954',qobuz:'#1870f5',tidal:'#00d4b3'})[msg.service] || 'var(--red)';
      const foundTxt = msg.found ? ` · найдено: ${msg.found}` : '';
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
      setReleasesStatus(`✓${_svcLbl3} ${msg.artists_checked} арт. · ${msg.releases_count} рел.`, msg.artists_checked, msg.artists_checked, 'var(--green)');
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
      toast('✅ AMD v2 готов! Нажми AMD в топбаре','var(--green)');
      break;
    }
    case 'gamdl_deps_fixed': {
      const btn2 = document.getElementById('fix-deps-btn');
      if(btn2){ btn2.disabled=false; btn2.textContent='🔧 Fix gamdl deps (protobuf)'; btn2.style.display='none'; }
      toast('✅ gamdl зависимости исправлены!', 'var(--green)');
      break;
    }
    case 'gamdl_needs_upgrade': {
      const sNav = document.querySelector('.nav-item[data-view="setup"]');
      if(sNav) showView('setup', sNav);
      toast('⚠ Устаревший gamdl — нажми Auto-install', 'var(--orange)');
      break;
    }
    case 'show_wrapper_logs_hint': {
      const ob = document.getElementById('wrapper-ok-banner');
      if(ob && ob.style.display!=='none') toggleWrapperLogs();
      toast('💡 no codec found — открой 📋 Логи в баннере wrapper', 'var(--orange)');
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
      toast('⚠ ALAC/Atmos требует wrapper — запусти Docker или переключись на AAC', 'var(--orange)');
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
        toast('🔌 Туннель готов: ' + msg.url, '#22c55e');
        updateRemoteUI(true, msg.url, 0);
      } else if (!msg.running && !msg.connecting) {
        // tunnel died unexpectedly
        if (document.getElementById('tunnel-stop-btn')?.style.display !== 'none') {
          toast('Туннель serveo отключился', 'var(--red)');
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
      toast('✗ Неверный пароль Apple ID — wrapper остановлен', 'var(--red)', 'Проверь Settings → Apple Music → Wrapper');
      appendLog('[WRAPPER] ✗ Login failed — исправь пароль в настройках и перезапусти wrapper', 'error');
      checkWrapperStatus();
      break;
    case 'wrapper_started':
      _wrapperStarting=false;
      toast('✓ Wrapper запущен!','var(--green)');
      checkWrapperStatus();
      break;
    case 'amd_wrapper_not_ready': {
      const _inst = msg.instance || 'wm.wol.moe';
      toast(`⚠ AMD wrapper «${_inst}» не готов — загрузки пропущены`, 'var(--orange)');
      // Refresh the status widget if visible
      const _wmEl = document.getElementById('amd-wm-status');
      if(_wmEl && _wmEl.style.display !== 'none') checkAMDWrapperStatus();
      break;
    }
    case 'orpheus_authed': {
      loadOrpheusStatus();
      const authUser = msg.username ? ` (${msg.username})` : '';
      toast(`✓ Spotify: вход выполнен${authUser}`, 'var(--green)');
      if(window._orpheusLoginDone) { window._orpheusLoginDone(); window._orpheusLoginDone = null; }
      break;
    }
    case 'orpheus_not_authed':
      toast('OrpheusDL: нет авторизации Spotify', 'var(--red)', 'Войди через Settings → Spotify', 10000);
      loadOrpheusStatus();
      showStab('spotify');
      break;
    case 'soundcloud_installed':
      toast('✅ SoundCloud готов к работе!', '#ff5500');
      scEngineCheck();
      break;
    case 'wrapper_built': {
      const bBtn = document.getElementById('btn-wrapper-build');
      const bSt  = document.getElementById('wrapper-build-status');
      if(bBtn) { bBtn.disabled=false; bBtn.textContent='🔨 Build local image'; }
      if(bSt)  bSt.textContent = '✓ Собран';
      toast('✅ Local wrapper image собран!', 'var(--blue)');
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
    if (!r.ok) throw new Error(`сервер недоступен (HTTP ${r.status})`);
    return {};
  }
  try {
    return JSON.parse(text);   // valid JSON (200 or a 4xx with a {detail} body) → caller handles
  } catch (_) {
    throw new Error(!r.ok ? `сервер недоступен (HTTP ${r.status})` : 'некорректный ответ сервера');
  }
}

async function loadQualities() {
  const q = await api('GET','/api/qualities');
  QUALITIES.length = 0;
  QUALITIES.push(...q);
  populateQualitySelect();
  renderQualityGrid();
}

// GUEST / ADMIN (guest sessions, admin links, per-guest activity) → moved to its own module file (see index.html).

// ── Remote access ─────────────────────────────────────────────

function updateRemoteUI(enabled, publicUrl, activeSessions) {
  const pill  = document.getElementById('remote-status-pill');
  const cnt   = document.getElementById('remote-sessions-count');
  const startBtn = document.getElementById('remote-start-btn');
  const stopBtn  = document.getElementById('remote-stop-btn');
  const urlInput = document.getElementById('remote-public-url');
  if (pill) {
    pill.textContent  = enabled ? t('remote.on') : t('remote.off');
    pill.style.background = enabled ? 'rgba(34,197,94,.15)' : 'rgba(252,60,68,.15)';
    pill.style.color      = enabled ? '#22c55e' : 'var(--red)';
  }
  if (cnt) cnt.textContent = activeSessions > 0 ? `${activeSessions} ${t('remote.sessions')||'активных сессий'}` : '';
  if (startBtn) startBtn.style.display = enabled ? 'none'  : '';
  if (stopBtn)  stopBtn.style.display  = enabled ? '' : 'none';
  if (urlInput && publicUrl) urlInput.value = publicUrl;
}

async function loadRemoteStatus() {
  try {
    const r = await fetch('/api/remote/status');
    if (!r.ok) return;
    const d = await r.json();
    updateRemoteUI(d.enabled, d.public_url, d.active_links);
  } catch(e) {}
}

async function remoteStart() {
  const pub = (document.getElementById('remote-public-url')?.value || '').trim();
  try {
    const r = await api('POST', '/api/remote/start', { public_url: pub });
    if (r.ok) {
      updateRemoteUI(true, r.public_url, 0);
      toast(S.lang==='en'?'Remote access enabled':'Удалённый доступ включён', '#22c55e');
      await loadAdminLinks();
    } else {
      toast(r.detail || 'Error', 'var(--red)');
    }
  } catch(e) { toast(t('err.generic') + ': ' + e.message, 'var(--red)'); }
}

async function remoteStop() {
  try {
    const r = await api('POST', '/api/remote/stop');
    if (r.ok) {
      updateRemoteUI(false, '', 0);
      toast(S.lang==='en'?`Remote stopped, ${r.revoked} links revoked`:`Остановлено, ${r.revoked} ссылок отозвано`, 'var(--red)');
      await loadAdminLinks();
    } else {
      toast(r.detail || 'Error', 'var(--red)');
    }
  } catch(e) { toast(t('err.generic') + ': ' + e.message, 'var(--red)'); }
}

async function saveRemoteUrl() {
  const pub = (document.getElementById('remote-public-url')?.value || '').trim();
  if (!pub) return;
  try { await api('POST', '/api/remote/start', { public_url: pub }); } catch(e) {}
}

// ── Serveo tunnel ──────────────────────────────────────────────────────────
function updateTunnelUI(running, connecting, url) {
  const pill     = document.getElementById('tunnel-status-pill');
  const urlRow   = document.getElementById('tunnel-url-row');
  const urlInput = document.getElementById('tunnel-url-display');
  const startBtn = document.getElementById('tunnel-start-btn');
  const stopBtn  = document.getElementById('tunnel-stop-btn');
  if (pill) {
    if (connecting) {
      pill.textContent = '⏳ Подключение…';
      pill.style.background = 'rgba(234,179,8,.15)'; pill.style.color = '#eab308';
    } else if (running) {
      pill.textContent = '● Активен';
      pill.style.background = 'rgba(34,197,94,.15)'; pill.style.color = '#22c55e';
    } else {
      pill.textContent = '● Выключен';
      pill.style.background = 'rgba(252,60,68,.15)'; pill.style.color = 'var(--red)';
    }
  }
  if (urlRow)   urlRow.style.display   = url ? '' : 'none';
  if (urlInput && url) urlInput.value  = url;
  if (startBtn) { startBtn.style.display = running || connecting ? 'none' : ''; startBtn.textContent = '▶ Запустить'; startBtn.disabled = false; }
  if (stopBtn)  stopBtn.style.display  = running || connecting ? '' : 'none';
  if (url) {
    const pubInput = document.getElementById('remote-public-url');
    if (pubInput) pubInput.value = url;
  }
}

async function tunnelStart() {
  const startBtn = document.getElementById('tunnel-start-btn');
  if (startBtn) { startBtn.textContent = '⏳…'; startBtn.disabled = true; }
  updateTunnelUI(false, true, '');
  try {
    const r = await api('POST', '/api/tunnel/start', {});
    if (!r.ok) {
      updateTunnelUI(false, false, '');
      toast('Ошибка туннеля: ' + (r.error || '?'), 'var(--red)');
    }
    // URL arrives via WebSocket tunnel_status event
  } catch(e) {
    updateTunnelUI(false, false, '');
    toast(t('err.generic') + ': ' + e.message, 'var(--red)');
  }
}

async function tunnelStop() {
  try {
    await api('POST', '/api/tunnel/stop');
    updateTunnelUI(false, false, '');
    toast('Туннель остановлен');
  } catch(e) { toast(t('err.generic') + ': ' + e.message, 'var(--red)'); }
}

async function loadTunnelStatus() {
  try {
    const r = await fetch('/api/tunnel/status');
    if (!r.ok) return;
    const d = await r.json();
    updateTunnelUI(d.running, d.connecting, d.url || '');
  } catch(e) {}
}

async function createGuestLink() {
  const label     = document.getElementById('gl-label')?.value.trim() || '';
  const qtype     = document.getElementById('gl-quota-type')?.value || 'unlimited';
  const qlimit    = parseInt(document.getElementById('gl-quota-val')?.value) || 20;
  const tokenMode = document.getElementById('gl-token-mode')?.value || 'owner';
  const quota     = qtype === 'unlimited' ? {type:'unlimited'} : {type:qtype, limit:qlimit};
  try {
    const r = await api('POST', '/api/admin/links/create', {label, quota, token_mode: tokenMode});
    if (r.ok) {
      const box = document.getElementById('gl-new-link');
      if (box) { box.style.display = 'block'; setTimeout(() => { box.style.display = 'none'; }, 3000); }
      document.getElementById('gl-label').value = '';
      await loadAdminLinks();
    } else {
      toast(r.detail || 'Ошибка создания ссылки', 'var(--red)');
    }
  } catch(e) { toast(t('err.generic') + ': ' + e.message, 'var(--red)'); }
}

function copyGuestLink() {
  const url = document.getElementById('gl-new-link-url')?.textContent || '';
  if (!url) return;
  navigator.clipboard.writeText(url).then(() => toast(t('toast.link_copied'))).catch(() => {
    const ta = document.createElement('textarea');
    ta.value = url; document.body.appendChild(ta); ta.select();
    document.execCommand('copy'); document.body.removeChild(ta);
    toast(t('toast.link_copied'));
  });
}

async function revokeGuestLink(token) {
  try {
    await api('POST', '/api/admin/links/revoke', {token});
    await loadAdminLinks();
    toast('Ссылка отозвана');
  } catch(e) { toast(t('err.generic') + ': ' + e.message, 'var(--red)'); }
}

async function toggleTokenMode(token, newMode) {
  try {
    await api('POST', '/api/admin/links/token-mode', {token, token_mode: newMode});
    await loadAdminLinks();
  } catch(e) { toast(t('err.generic') + ': ' + e.message, 'var(--red)'); }
}

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
});

// WRAPPER MANAGEMENT (Apple Docker wrapper UI) → moved to its own module file (see index.html).

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
  if(!_raw){ toast('Вставь ссылку','var(--red)'); return; }

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
  }).join('') + (n>8 ? `<div style="font-size:10px;color:var(--muted);margin-top:2px">…и ещё ${n-8}</div>` : '');

  const modal = document.createElement('div');
  modal.id = 'multi-url-modal';
  modal.style.cssText = 'position:fixed;inset:0;z-index:9999;display:flex;align-items:center;justify-content:center;background:rgba(0,0,0,.7);backdrop-filter:blur(4px)';
  modal.innerHTML = `<div style="background:var(--surface,#1c1c1e);border:1px solid rgba(255,255,255,.1);border-radius:16px;padding:24px;width:440px;max-width:90vw">
    <div style="font-size:16px;font-weight:700;color:#f0f0f4;margin-bottom:6px">Вставлено несколько ссылок</div>
    <div style="font-size:12px;color:var(--muted);margin-bottom:12px">Найдено <b style="color:#f0f0f4">${n}</b> ссылок одним запросом${summary?` · ${summary}`:''}. Что делать?</div>
    <div style="background:rgba(0,0,0,.3);border-radius:8px;padding:8px 10px;margin-bottom:16px;max-height:170px;overflow:auto">${list}</div>
    <div style="display:flex;flex-direction:column;gap:8px">
      <button onclick="_multiUrlChoose('all')" style="padding:11px;background:var(--green,#34c759);color:#000;border:none;border-radius:10px;font-size:13px;font-weight:700;cursor:pointer;font-family:var(--font)">⬇ Качать все (${n})</button>
      <button onclick="_multiUrlChoose('first')" style="padding:10px;background:rgba(255,255,255,.06);color:#f0f0f4;border:1px solid rgba(255,255,255,.12);border-radius:10px;font-size:12px;font-weight:600;cursor:pointer;font-family:var(--font)">1️⃣ Только первую</button>
      <button onclick="_multiUrlChoose('cancel')" style="padding:8px;background:transparent;color:var(--muted);border:1px solid rgba(255,255,255,.1);border-radius:10px;font-size:12px;cursor:pointer;font-family:var(--font)">Отмена</button>
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
    toast(`Добавлено в очередь: ${r.added != null ? r.added : st.urls.length}`, 'var(--green)');
  } else {
    toast('Не удалось добавить пакет ссылок', 'var(--red)');
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
    toast('Не найдено на '+_svcLabel(target)+' — выбери другой', 'var(--orange)', r.error||'');
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

// ── Settings → Цвета сервисов: per-service color picker ───────────────────
const _SVC_PICKER_LIST = ['apple','qobuz','tidal','deezer','spotify','soundcloud','bbc','beatport'];
function renderSvcColorGrid() {
  const grid = document.getElementById('svc-color-grid');
  if (!grid) return;
  const cfg = (S.config && S.config['service-colors']) || {};
  grid.innerHTML = _SVC_PICKER_LIST.map(svc => {
    const val = cfg[svc] || SVC_BRAND[svc] || '#888888';
    return `
      <label style="display:flex;align-items:center;gap:8px;padding:6px 10px;background:var(--surface);border:1px solid var(--border);border-radius:8px;cursor:pointer">
        <input type="color" value="${val}" onchange="saveSvcColor('${svc}',this.value)"
          style="width:32px;height:24px;padding:0;border:none;background:transparent;cursor:pointer;flex-shrink:0"/>
        <span style="font-size:12px;font-weight:700;color:${val}">${esc(_svcLabel(svc))}</span>
      </label>`;
  }).join('');
}
async function saveSvcColor(svc, color) {
  const cfg = {...(S.config['service-colors'] || {}), [svc]: color};
  S.config['service-colors'] = cfg;
  try { await api('POST', '/api/config', {'service-colors': cfg}); } catch {}
  renderSvcColorGrid();
  // Re-render dynamic views so the new colour shows everywhere immediately.
  try { renderQueue?.(); } catch {}
  try { _scRender?.(); } catch {}
  try { _applyRelFilter?.(); } catch {}
  try { _libApplyFilter?.(); } catch {}
}
async function resetSvcColors() {
  S.config['service-colors'] = {};
  try { await api('POST', '/api/config', {'service-colors': {}}); } catch {}
  renderSvcColorGrid();
  try { renderQueue?.(); } catch {}
  try { _scRender?.(); } catch {}
  try { _applyRelFilter?.(); } catch {}
}

// Holds pending Spotify picker data by notif id, keyed so button handlers
// can fetch url/quality without stuffing JSON into HTML attributes (which
// breaks on the double-quotes in ``https://``).
const _spPickerData = new Map();

function _showSpotifyChoiceToast(url, quality) {
  const stack = document.getElementById('notif-stack');
  if(!stack) return;
  const id = 'sp_choice_' + Date.now();
  _spPickerData.set(id, { url, quality });

  const el = document.createElement('div');
  el.className = 'notif notif-enter';
  el.id = id;
  el.style.maxWidth = '340px';
  el.innerHTML = `
    <div class="notif-dot" style="background:#1db954;color:#1db954"></div>
    <div class="notif-body">
      <div class="notif-msg">Spotify — конвертировать через:</div>
      <div class="sp-picker-btns" style="display:flex;gap:6px;margin-top:8px;flex-wrap:wrap">
        <button data-target="apple"
          style="padding:5px 11px;background:rgba(192,132,160,.15);border:1px solid rgba(192,132,160,.25);border-radius:8px;font-size:11px;font-weight:700;color:var(--red);cursor:pointer;font-family:var(--font)">Apple Music</button>
        <button data-target="deezer"
          style="padding:5px 11px;background:rgba(162,56,255,.18);border:1px solid rgba(162,56,255,.3);border-radius:8px;font-size:11px;font-weight:700;color:#a238ff;cursor:pointer;font-family:var(--font)">Deezer</button>
        <button data-target="qobuz"
          style="padding:5px 11px;background:rgba(27,104,211,.18);border:1px solid rgba(27,104,211,.3);border-radius:8px;font-size:11px;font-weight:700;color:#1b68d3;cursor:pointer;font-family:var(--font)">Qobuz</button>
      </div>
      <label class="sp-remember" style="display:flex;align-items:center;gap:6px;margin-top:8px;font-size:11px;color:var(--muted);cursor:pointer;user-select:none">
        <input type="checkbox" class="sp-remember-chk" style="accent-color:#1db954"/>
        Запомнить выбор (не спрашивать снова)
      </label>
    </div>
    <div class="notif-close" onclick="_closeNotif('${id}')">✕</div>`;

  // Wire up button handlers via JS — this is the key fix.
  el.querySelectorAll('.sp-picker-btns button').forEach(btn => {
    btn.addEventListener('click', () => {
      const target   = btn.dataset.target;
      const remember = !!el.querySelector('.sp-remember-chk')?.checked;
      _chooseSpTarget(id, target, remember);
    });
  });

  stack.appendChild(el);
  requestAnimationFrame(()=>requestAnimationFrame(()=>el.classList.remove('notif-enter')));
  // Auto-dismiss after 15s — bit longer now that there's a checkbox to read.
  _notifTimers.set(id, setTimeout(()=>_closeNotif(id), 15000));
}

async function _chooseSpTarget(notifId, target, remember) {
  const ctx = _spPickerData.get(notifId);
  if(!ctx) return;                         // already handled or expired
  _spPickerData.delete(notifId);
  _closeNotif(notifId);

  // Persist the preference IMMEDIATELY so if the convert call is slow
  // and the user tries another URL, the new choice is already remembered.
  if(remember) {
    try {
      await api('POST','/api/config',{ 'spotify-default-target': target });
      if(S.config) S.config['spotify-default-target'] = target;
      toast(`Spotify → ${_svcLabel(target)} (запомнено)`, _svcColor(target));
    } catch(e) {
      console.warn('save remember:', e);
    }
  } else {
    toast('Конвертирую Spotify…', '#1db954');
  }

  const r = await api('POST','/api/convert/spotify', { url: ctx.url, target });
  if(r.ok && r.target?.url) {
    await api('POST','/api/queue/add', { url: r.target.url, quality: resolveQuality(target), title: r.target.title });
    document.getElementById('url-input').value = '';
    detectUrlService('');
    toast('+ '+r.target.title, _svcColor(target), _svcLabel(target));
  } else {
    toast('Не найдено на '+_svcLabel(target), 'var(--orange)', r.error||'');
  }
}

function detectSvcFromUrl(url) {
  if(url.includes('music.apple.com'))  return 'apple';
  if(url.includes('qobuz.com'))        return 'qobuz';
  if(url.includes('deezer.com'))       return 'deezer';
  if(url.includes('tidal.com'))        return 'tidal';
  if(url.includes('soundcloud.com'))   return 'soundcloud';
  if(url.includes('spotify.com'))      return 'spotify';
  if(url.includes('beatport.com'))     return 'beatport';
  if(url.includes('music.yandex.'))    return 'yandex';
  if(url.includes('music.amazon.'))    return 'amazon';
  return null;
}

// Show a modal asking which engine/service to use for this URL
function showUrlServiceModal(url, quality, detectedSvc) {
  const existing = document.getElementById('url-svc-modal');
  if(existing) existing.remove();

  const SVC_INFO = {
    apple:    {label:'Apple Music', color:'#fc3c44', engines:['AMD v2','gamdl','zhaarey']},
    qobuz:    {label:'Qobuz',       color:'#1b68d3', engines:['Qobuz API']},
    deezer:   {label:'Deezer',      color:'#a238ff', engines:['Deezer ARL']},
    tidal:    {label:'Tidal',       color:'#00d4b3', engines:['Tidal API']},
    spotify:  {label:'Spotify',     color:'#1db954', engines:['→ Apple Music','→ Deezer','→ Qobuz']},
    beatport: {label:'Beatport',    color:'#01f49c', engines:['OrpheusDL']},
  };

  const svcInfo = SVC_INFO[detectedSvc] || {label:detectedSvc,color:'var(--muted)',engines:['Авто']};
  const shortUrl = url.length > 60 ? url.slice(0,57)+'…' : url;

  const modal = document.createElement('div');
  modal.id = 'url-svc-modal';
  modal.style.cssText = 'position:fixed;inset:0;z-index:9999;display:flex;align-items:center;justify-content:center;background:rgba(0,0,0,.7);backdrop-filter:blur(4px)';

  const isSpotify = detectedSvc === 'spotify';
  const targetOptions = isSpotify
    ? ['apple','qobuz','deezer']
    : [detectedSvc];

  const targetBtns = targetOptions.map(t => {
    const ti = SVC_INFO[t]||{label:t,color:'var(--muted)'};
    return `<button onclick="chooseUrlSvc(${JSON.stringify(url)},${JSON.stringify(quality)},${JSON.stringify(detectedSvc)},${JSON.stringify(t)})"
      style="flex:1;padding:10px 14px;background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.12);border-radius:9px;cursor:pointer;font-family:var(--font);transition:.15s;text-align:center"
      onmouseover="this.style.borderColor='${ti.color}'" onmouseout="this.style.borderColor='rgba(255,255,255,.12)'">
      <div style="font-size:13px;font-weight:700;color:${ti.color}">${ti.label}</div>
      ${isSpotify?'<div style="font-size:10px;color:var(--muted);margin-top:3px">конвертировать</div>':''}
    </button>`;
  }).join('');

  modal.innerHTML = `<div style="background:var(--surface,#1c1c1e);border:1px solid rgba(255,255,255,.1);border-radius:16px;padding:24px;width:420px;max-width:90vw">
    <div style="font-size:11px;color:var(--muted,#888);margin-bottom:4px">Определён сервис</div>
    <div style="display:flex;align-items:center;gap:8px;margin-bottom:14px">
      <div style="width:10px;height:10px;border-radius:50%;background:${svcInfo.color}"></div>
      <div style="font-size:16px;font-weight:700;color:#f0f0f4">${svcInfo.label}</div>
    </div>
    <div style="font-size:11px;color:var(--muted,#888);font-family:monospace;background:rgba(0,0,0,.3);border-radius:7px;padding:7px 10px;margin-bottom:16px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${shortUrl}</div>
    <div style="font-size:12px;font-weight:600;color:#f0f0f4;margin-bottom:10px">${isSpotify?'Конвертировать и скачать через:':'Скачать через:'}</div>
    <div style="display:flex;gap:8px;margin-bottom:16px">${targetBtns}</div>
    <div style="display:flex;justify-content:flex-end">
      <button onclick="document.getElementById('url-svc-modal').remove()"
        style="padding:6px 14px;background:transparent;border:1px solid rgba(255,255,255,.1);border-radius:8px;cursor:pointer;font-size:12px;color:var(--muted,#888);font-family:var(--font)">
        Отмена
      </button>
    </div>
  </div>`;

  document.body.appendChild(modal);
  modal.onclick = e => { if(e.target===modal) modal.remove(); };
}

async function chooseUrlSvc(url, quality, srcSvc, targetSvc) {
  const modal = document.getElementById('url-svc-modal');
  if(modal) modal.remove();

  if(srcSvc === 'spotify') {
    // Convert Spotify → target service then add with target-service quality
    toast('Конвертирую Spotify…', '#1db954');
    const r = await api('POST','/api/convert/spotify',{url,target:targetSvc});
    if(r.ok && r.target?.url) {
      await api('POST','/api/queue/add',{url:r.target.url, quality: resolveQuality(targetSvc), title:r.target.title});
      document.getElementById('url-input').value='';
      toast('+ '+r.target.title+' → очередь','#1db954');
    } else {
      toast('Не найдено: '+(r.error||url),'var(--orange)');
    }
    return;
  }

  await _doAddUrl(url, quality, targetSvc);
}

async function _doAddUrl(url, quality, svc) {
  const r = await api('POST','/api/queue/add',{url,quality});
  if(r.ok) {
    document.getElementById('url-input').value='';
    detectUrlService(''); // clear indicator
    toast(r.count > 1 ? `Добавлено ${r.count} треков в очередь` : 'Добавлено в очередь');
    pullQueue();   // reflect immediately even if the WS push is lagging/dead
    // ISRC cross-service check (non-blocking; skip for tidal — no metadata ISRC)
    if(svc !== 'tidal') _checkIsrc(url, svc);
  } else if(r.spotify) {
    // Backend rejected Spotify URL — spotify-engine not set to a direct engine
    toast('Включи OrpheusDL в Settings → Spotify', 'var(--orange)');
  } else if(r.duplicate) {
    toast('Уже в очереди', 'var(--muted)');
  } else {
    toast(r.msg || r.detail || 'Ошибка URL', 'var(--red)');
  }
}

function _isrcQualLabel(svc, m) {
  if (svc === 'qobuz') {
    if (m.hires && m.bit_depth >= 20)
      return `Hi-Res ${m.bit_depth}bit / ${m.sample_rate}kHz`;
    return m.hires ? 'Hi-Res FLAC' : 'FLAC 16bit';
  }
  if (svc === 'tidal') {
    const q = (m.audio_quality || '').toUpperCase();
    if (q === 'MASTER')   return 'MQA Master';
    if (q === 'HI_RES')   return 'Hi-Res';
    if (q === 'LOSSLESS') return 'FLAC';
    if (q === 'HIGH')     return 'AAC 320';
    return 'FLAC';
  }
  if (svc === 'deezer') return 'FLAC / MP3';
  return '';
}

async function _checkIsrc(url, skipSvc = '') {
  try {
    const r = await api('POST', '/api/isrc/resolve', { url, skip: skipSvc });
    if (!r.ok || !r.matches) return;
    const svcs = Object.keys(r.matches);
    if (!svcs.length) return;

    const stack = document.getElementById('notif-stack');
    if (!stack) return;

    const title  = r.title  ? `«${esc(r.title)}»`  : 'Трек';
    const artist = r.artist ? ` — ${esc(r.artist)}` : '';

    const n = document.createElement('div');
    n.className = 'notif';
    // display:block overrides the .notif class's `display:flex` — otherwise the
    // title and the per-service rows lay out horizontally and the title column
    // collapses to one word per line. We want a vertical stack here.
    n.style.cssText = 'display:block;background:var(--surface2);border:1px solid var(--border2);border-radius:10px;padding:10px 14px;font-size:11.5px;width:300px;max-width:calc(100vw - 24px);position:relative';

    const rowsHtml = svcs.map(s =>
      `<div class="isrc-row" data-svc="${s}" style="display:flex;align-items:center;gap:8px;padding:5px 6px;margin:0 -6px;cursor:pointer;border-radius:6px">
        <span style="color:${_svcColor(s)};font-weight:700;min-width:50px">${esc({qobuz:'Qobuz',tidal:'Tidal',deezer:'Deezer'}[s]||s)}</span>
        <span style="color:var(--muted);flex:1;font-size:10.5px">${esc(_isrcQualLabel(s, r.matches[s]))}</span>
        <span style="opacity:.7;font-size:13px" title="Добавить в очередь">⬇</span>
      </div>`
    ).join('');

    n.innerHTML = `
      <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:7px">
        <span style="font-weight:600;color:var(--text);line-height:1.3">🔍 ${title}${artist}</span>
        <span class="isrc-x" style="cursor:pointer;color:var(--muted);padding-left:10px;font-size:18px;line-height:1;flex-shrink:0">×</span>
      </div>
      ${rowsHtml}`;

    n.querySelector('.isrc-x').onclick = e => { e.stopPropagation(); n.remove(); };

    n.querySelectorAll('.isrc-row').forEach(row => {
      const svc = row.dataset.svc;
      const m   = r.matches[svc];
      if (!m) return;
      row.addEventListener('mouseenter', () => row.style.background = 'var(--surface3,rgba(255,255,255,.05))');
      row.addEventListener('mouseleave', () => row.style.background = '');
      row.addEventListener('click', async () => {
        const trackUrl = m.track_url || m.url;
        if (!trackUrl) return;
        const res = await api('POST', '/api/queue/add', { url: trackUrl });
        if (res.ok) { toast('Добавлено в очередь'); n.remove(); }
        else toast(res.msg || res.detail || 'Ошибка', 'var(--red)');
      });
    });

    stack.appendChild(n);
    setTimeout(() => { try { n.remove(); } catch(_){} }, 18000);
  } catch(_) {}
}

function addCurrentPage() {
  document.getElementById('url-input').value = location.href;
  addUrl();
}

function renderQueue() {
  const el = document.getElementById('queue-list');
  const empty = document.getElementById('queue-empty');
  if(!S.queue.length){ empty.style.display='flex'; el.innerHTML=''; el.appendChild(empty); return; }
  empty.style.display='none';

  // keep existing items, add/remove as needed
  const existing = new Map([...el.querySelectorAll('.qi')].map(n=>[n.dataset.id,n]));
  const ids = new Set(S.queue.map(t=>t.id));

  // remove gone
  existing.forEach((node,id)=>{ if(!ids.has(id)) node.remove(); });

  S.queue.forEach(task=>{
    // Per-task try/catch: a single malformed task (missing meta etc.) must NOT
    // throw out of forEach and abort the loop — that left every task AFTER it
    // unrendered ("some tasks show, some don't"). Skip the bad one, keep going.
    try {
      if(existing.has(task.id)){
        updateQueueItem(task, existing.get(task.id));
      } else {
        el.appendChild(buildQueueItem(task));
      }
    } catch(e){
      console.error('renderQueue: skipped bad task', task && task.id, e);
    }
  });

  // badge
  const pending = S.queue.filter(t=>t.status==='queued').length;
  const badge = document.getElementById('queue-badge');
  if(pending>0){ badge.textContent=pending; badge.style.display=''; }
  else badge.style.display='none';
}

// Cache of qualities per engine so we don't refetch for every row
const _QUALITIES_BY_ENGINE = {};
async function _qualitiesForEngine(engine) {
  if(!engine) return QUALITIES;
  if(_QUALITIES_BY_ENGINE[engine]) return _QUALITIES_BY_ENGINE[engine];
  try {
    const svcMap = {deezer:'deezer',qobuz:'qobuz',tidal:'tidal',soundcloud:'soundcloud',beatport:'beatport'};
    const svc = svcMap[engine] || 'apple';
    const qs = await (await fetch(`/api/qualities?service=${svc}`)).json();
    _QUALITIES_BY_ENGINE[engine] = Array.isArray(qs) ? qs : QUALITIES;
    return _QUALITIES_BY_ENGINE[engine];
  } catch(e) { return QUALITIES; }
}

function _qualityFor(task) {
  // Look up the per-engine quality list first, fall back to the Apple one.
  const list = _QUALITIES_BY_ENGINE[task.engine] || QUALITIES;
  return list.find(x => x.id === task.quality)
      || { color:'#888', label: (task.quality||'—').toUpperCase(), badge:'—', sub:'' };
}

function _tracksDone(task) {
  // Returns {done, total} using the most accurate source available.
  // Prefer engine's actual current/total (when total > 1, i.e. real track counter).
  // Fall back to estimating from progress% × meta.trackCount.
  const metaTotal = (task.meta?.trackCount || task.meta?.totalTracks || 0);
  const isSingle  = task.meta?.type === 'song' || task.meta?.type === 'track';
  const tc = isSingle ? 1 : metaTotal;

  // StreamripMixin: explicit track-completion counter (total=0 sentinel events)
  if ((task._tracksCompleted || 0) > 0) {
    return { done: task._tracksCompleted, total: tc || task._tracksCompleted };
  }

  const engTotal   = task._progTotal   || 0;
  const engCurrent = task._progCurrent || 0;

  // Engine reports actual track N/M (not percentage 0-100)
  if (engTotal > 1 && engTotal !== 100) {
    const total = tc > 1 ? Math.max(tc, engTotal) : engTotal;
    return { done: engCurrent, total };
  }

  // Only percentage known — estimate
  const pct = Math.max(0, Math.min(100, task.progress || 0));
  if (tc > 1) {
    return { done: pct >= 100 ? tc : Math.floor(pct / 100 * tc), total: tc };
  }
  return { done: 0, total: tc };
}

function _renderBlocks(progress, trackCount, color, isRunning, task) {
  const pct = Math.max(0, Math.min(100, progress || 0));
  const tc  = trackCount || 0;

  // Animated raccoon for ANY running task — sits to the LEFT of the bar/blocks.
  const raccoon = isRunning
    ? `<span class="qi-raccoon" style="display:inline-block;animation:qiRaccoonBob .9s ease-in-out infinite;font-size:14px;line-height:1;margin-right:6px;vertical-align:middle">🦝</span>`
    : '';

  // No track count known — raccoon + 6-char filled bar
  if (!tc) {
    if (!isRunning) return `<span style="opacity:.15;font-family:monospace;letter-spacing:0">░░░░░░░░</span>`;
    const filledW = Math.round(pct / 100 * 6);
    return `<span style="display:inline-flex;align-items:center">${raccoon}` +
      `<span style="font-family:monospace;letter-spacing:0">` +
        `<span style="color:${color}">${'█'.repeat(filledW)}</span>` +
        `<span style="opacity:.15">${'░'.repeat(6 - filledW)}</span>` +
      `</span></span>`;
  }

  // Known track count — show real blocks (capped at 20 visually)
  const n = Math.min(tc, 20);
  const { done: doneTracks } = task ? _tracksDone(task) : { done: pct >= 100 ? tc : Math.floor(pct / 100 * tc) };
  const done = Math.min(Math.round(doneTracks / tc * n), n);

  if (pct >= 100)
    return `<span style="color:${color};font-family:monospace;letter-spacing:0">${'█'.repeat(n)}</span>`;

  if (!isRunning)
    return `<span style="opacity:.15;font-family:monospace;letter-spacing:0">${'█'.repeat(n)}</span>`;

  const empty = Math.max(0, n - done - 1);
  let h = `<span style="display:inline-flex;align-items:center">${raccoon}<span style="font-family:monospace;letter-spacing:0">`;
  if (done  > 0) h += `<span style="color:${color}">${'█'.repeat(done)}</span>`;
  h += `<span class="qi-blk-cur" style="color:${color}">█</span>`;
  if (empty > 0) h += `<span style="opacity:.15">${'█'.repeat(empty)}</span>`;
  h += '</span></span>';
  return h;
}

function _blocksInfo(progress, trackCount, task) {
  const pct = Math.max(0, Math.min(100, progress || 0));
  const n   = trackCount || 0;
  if (n > 1) {
    const { done, total } = task ? _tracksDone(task) : { done: pct >= 100 ? n : Math.floor(pct / 100 * n), total: n };
    return `${done}/${total}`;
  }
  return pct > 0 && pct < 100 ? `${Math.round(pct)}%` : '';
}

// Per-task log lines for the queue-tile panel. Guests get a laconic subset
// (milestones only) so raw engine output never reaches them.
function _visibleLog(task) {
  let lines = task.log || [];
  if (_isGuest()) {
    lines = lines.filter(t => _isMilestone({
      level: /ERROR|✗/.test(t) ? 'error' : /WARN|⚠/.test(t) ? 'warn'
           : /✓|Done|Saved/.test(t) ? 'success' : 'stdout',
      text: t,
    }));
  }
  return lines.slice(-20);
}

function _qiStatusChip(task) {
  if(task.partial || task._partial) return `<span class="qi-st st-partial">⚠ частично</span>`;
  if(task.status==='running') return `<span class="qi-st st-run"><span class="qi-spinner"></span>${task._retry_count?('догрузка '+task._retry_count):(task._auto_retry?'догрузка':'качаю')}</span>`;
  if(task.status==='done')   return `<span class="qi-st st-done">✓ готово</span>`;
  if(task.status==='error')  return `<span class="qi-st st-err">✗ ошибка</span>`;
  if(task.status==='paused') return `<span class="qi-st">⏸ пауза</span>`;
  return `<span class="qi-st st-q">в очереди</span>`;
}

function buildQueueItem(task) {
  // Kick off a quality-list fetch for this engine so the badge updates
  // automatically on the next render pass.
  if(task.engine && !_QUALITIES_BY_ENGINE[task.engine]) {
    _qualitiesForEngine(task.engine).then(()=>updateQueueItem(task));
  }
  const q = _qualityFor(task);
  const m = task.meta;
  const el = document.createElement('div');
  const _isPartial = !!(task.partial || task._partial);
  el.className = `qi ${task.status}${_isPartial?' partial':''}`;
  el.dataset.id = task.id;
  el.dataset.st = task.status;
  el.dataset.partial = String(_isPartial);
  el.style.setProperty('--qi-p', (task.progress||0) + '%');
  const _isSingleTrack = m?.type === 'song' || m?.type === 'track';
  const trackCount = _isSingleTrack ? 1 : (m?.trackCount || m?.totalTracks || 0);
  const trackInfo  = trackCount > 1 ? `${trackCount} треков` : (trackCount === 1 ? '1 трек' : '');
  const hasMeta    = m && (m.title || m.artist);
  const typeLabel  = _typeLabel(m);
  const durInfo    = (m && m.duration && ['soundcloud','bbc'].includes(m.service)) ? _scDur(m.duration) : '';
  const artistLine = hasMeta
    ? [m.artist || '—', m.year, m.label, typeLabel, trackInfo, durInfo].filter(Boolean).join(' · ')
    : (m?.meta_error ? `⚠ ${m.meta_error}` : (m?.enriched ? '' : 'Получаю метаданные…'));
  const logLines   = (task.log || []).slice(-20);
  const logHtml    = logLines.map(l => {
    const lvl = /ERROR|✗/.test(l) ? 'error' : /WARN|⚠/.test(l) ? 'warn' : /✓|Done|Saved/.test(l) ? 'success' : /INFO|STEP/.test(l) ? 'info' : 'stdout';
    return `<div class="ll-${lvl}">${esc(l)}</div>`;
  }).join('');
  // ── compact-row state: progress, count, status chip, actions ──
  const _pct      = Math.max(0, Math.min(100, task.progress||0));
  const _partial  = task.partial || task._partial;
  const _got      = task.got || task._got || (task._files?.length) || 0;
  const _tdone    = trackCount > 1 ? _tracksDone(task).done : 0;
  const _countTxt = _partial ? `${_got||_tdone}/${trackCount}`
                  : trackCount > 1 ? `${_tdone}/${trackCount}`
                  : (_pct>0 && _pct<100 ? `${Math.round(_pct)}%` : '');
  const _showBar  = task.status==='running' || task.status==='queued';
  const _st       = _qiStatusChip(task);
  const _acts =
    (task.service==='spotify' ? `<button class="dl-action-btn" onclick="isrcUpgrade('${task.id}')" title="🎯 Найти лучше (ISRC)" style="color:#c084f5;border-color:#c084f544">🎯</button>` : '') +
    (task.status==='done' ? `<button class="dl-action-btn dl-btn" onclick="downloadTask('${task.id}')" title="${t('btn.download')}" style="color:#3ecfaa;border-color:#3ecfaa44">⬇${(task._dl_file||0)>0?`<span class="dl-cnt">${task._dl_file}</span>`:''}</button><button class="dl-action-btn dl-zip-btn" onclick="downloadTaskZip('${task.id}')" title="${t('q.dl_zip')}" style="color:#7c9fff;border-color:#7c9fff44">📦${(task._dl_zip||0)>0?`<span class="dl-cnt">${task._dl_zip}</span>`:''}</button><button class="dl-action-btn dl-cloud-btn" onclick="uploadToCloud('${task.id}',this)" title="Внешняя ссылка (Gofile)" style="color:#f0a050;border-color:#f0a05044">🔗${(task._dl_gofile||0)>0?`<span class="dl-cnt">${task._dl_gofile}</span>`:''}</button>${(((trackCount||0)>1)||((m?.totalTracks||0)>1)||((m?.trackCount||0)>1))?`<button class="dl-action-btn owner-only" onclick="coderMix('${task.id}')" title="🎚 Ripster Coder: склеить DJ-mix + CUE" style="color:#c9a0ff;border-color:#c9a0ff44">🎚</button>`:''}` : '') +
    ((task.status==='error'||task.status==='cancelled'||_partial) ? `<button class="dl-action-btn" onclick="retryTask('${task.id}')" title="↺ Догрузить недостающие треки (пропускает уже скачанные)" style="color:#ffd60a;border-color:#ffd60a44">↺</button>` : '');

  el.innerHTML = `
    <div class="qi-art">${m?.artworkUrl?`<img src="${esc(m.artworkUrl)}" data-cover data-lightbox onload="this.classList.add('loaded')" style="cursor:zoom-in" loading="lazy"/>`:'🎵'}</div>
    <div class="qi-body">
      <div class="qi-l1">
        <span class="qi-title">${esc(m?.title || _titleFromUrl(task.url))}</span>
        ${artistLine?`<span class="qi-artist">— ${esc(artistLine)}</span>`:''}
      </div>
      <div class="qi-l2">
        <span class="qi-badge" style="background:${q.color}22;color:${q.color}">${esc(q.label)}</span>
        ${_showBar?`<div class="qi-prog-wrap"><div class="qi-prog-bar" style="width:${_pct}%;background:${q.color}"></div></div>`:''}
        ${_countTxt?`<span class="qi-count">${_countTxt}</span>`:''}
        ${_st}
        ${logLines.length?`<button class="qi-log-toggle" onclick="toggleTaskLog('${task.id}',this)" title="показать лог">▶${logLines.length}</button>`:''}
        <div class="qi-actions">${_acts}</div>
      </div>
      ${logLines.length?`<div class="qi-log-panel" id="qi-log-${task.id}">${logHtml}</div>`:''}
    </div>
    <button class="qi-close owner-only" onclick="removeTask('${task.id}')" title="Удалить">✕</button>
  `;
  return el;
}

// CODER / CONVERTER / TAGGER (file converter + tag editor UI) → moved to its own module file (see index.html).

function toggleTaskLog(id, btn) {
  const panel = document.getElementById(`qi-log-${id}`);
  if(!panel) return;
  const open = panel.style.display === 'block';
  panel.style.display = open ? 'none' : 'block';
  if(!open) panel.scrollTop = panel.scrollHeight;
  if(btn) btn.textContent = btn.textContent.replace(/^[▶▼]/, open ? '▶' : '▼');
}

function _typeLabel(m) {
  if(!m) return '';
  if(m.albumType) return m.albumType;
  return {
    albums:'Альбом', album:'Альбом',
    single:'Сингл', ep:'EP', compilation:'Сборник',
    songs:'Трек', song:'Трек', track:'Трек',
    playlists:'Плейлист', playlist:'Плейлист',
    artist:'Артист', 'music-videos':'Видео',
  }[m.type] || '';
}

function _titleFromUrl(url) {
  try {
    const u = new URL(url);
    const parts = u.pathname.split('/').filter(Boolean).map(p => {
      try { return decodeURIComponent(p); } catch(_) { return p; }
    });
    const idx = parts.findIndex(p => ['album','track','song','playlist','artist'].includes(p));
    if(idx >= 0) return `${parts[idx]} · ${parts[idx+1]||''}`;
    return url;
  } catch(_) { return url; }
}


function updateQueueItem(task, el) {
  el = el || document.querySelector(`.qi[data-id="${task.id}"]`);
  if(!el) return;
  const _partial = String(!!(task.partial || task._partial));
  // A status (or partial) change alters the action set + layout → full rebuild,
  // preserving an open log panel. Resume-in-place keeps the SAME card (same id).
  if(el.dataset.st !== task.status || el.dataset.partial !== _partial) {
    const logOpen = el.querySelector('.qi-log-panel')?.style.display === 'block';
    const fresh = buildQueueItem(task);
    if(logOpen){
      const p = fresh.querySelector('.qi-log-panel'); if(p) p.style.display='block';
      const tg = fresh.querySelector('.qi-log-toggle'); if(tg) tg.textContent = tg.textContent.replace(/^▶/,'▼');
    }
    el.replaceWith(fresh);
    return;
  }
  // Same status → cheap in-place update (no flicker, no rebuild).
  const q   = _qualityFor(task);
  const pct = Math.max(0, Math.min(100, task.progress||0));
  el.style.setProperty('--qi-p', pct + '%');
  const bar = el.querySelector('.qi-prog-bar');
  if(bar){ bar.style.width = pct + '%'; bar.style.background = q.color; }
  const m  = task.meta || {};
  const _isSingle = m.type === 'song' || m.type === 'track';
  const tc = _isSingle ? 1 : (m.trackCount || m.totalTracks || 0);
  const cntEl = el.querySelector('.qi-count');
  if(cntEl) cntEl.textContent = tc > 1 ? `${_tracksDone(task).done}/${tc}`
                              : (pct>0 && pct<100 ? `${Math.round(pct)}%` : '');
  const stEl = el.querySelector('.qi-st');
  if(stEl) stEl.outerHTML = _qiStatusChip(task);
  const badgeEl = el.querySelector('.qi-badge');
  if(badgeEl){ badgeEl.textContent = q.label; badgeEl.style.background = q.color+'22'; badgeEl.style.color = q.color; }
  // Metadata enrichment — title / artist / cover appear once they arrive.
  if(m.title || m.artist){
    const titleEl = el.querySelector('.qi-title');
    if(titleEl) titleEl.textContent = m.title || _titleFromUrl(task.url);
    const tcInfo  = tc > 1 ? `${tc} треков` : (tc === 1 ? '1 трек' : '');
    const durInfo = (m.duration && ['soundcloud','bbc'].includes(m.service)) ? _scDur(m.duration) : '';
    const line = [m.artist || '—', m.year, m.label, _typeLabel(m), tcInfo, durInfo].filter(Boolean).join(' · ');
    let artistEl = el.querySelector('.qi-artist');
    if(artistEl) artistEl.textContent = '— ' + line;
    else if(titleEl && line){ const s=document.createElement('span'); s.className='qi-artist'; s.textContent='— '+line; titleEl.after(s); }
    const artEl = el.querySelector('.qi-art');
    if(artEl && m.artworkUrl && !artEl.querySelector('img'))
      artEl.innerHTML = `<img src="${esc(m.artworkUrl)}" data-cover data-lightbox onload="this.classList.add('loaded')" style="cursor:zoom-in" loading="lazy"/>`;
  }
  // Download counters on existing action buttons (done state).
  if(task.status === 'done'){
    const _setCnt = (sel, n) => {
      const btn = el.querySelector(sel); if(!btn) return;
      let cnt = btn.querySelector('.dl-cnt');
      if(n > 0){ if(!cnt){ cnt=document.createElement('span'); cnt.className='dl-cnt'; btn.appendChild(cnt);} cnt.textContent=n; }
      else if(cnt) cnt.remove();
    };
    _setCnt('.dl-btn', task._dl_file||0);
    _setCnt('.dl-zip-btn', task._dl_zip||0);
    _setCnt('.dl-cloud-btn', task._dl_gofile||0);
  }
}

function statusLabel(task) {
  if(task.status==='running'){
    const _isSingle = task.meta?.type === 'song' || task.meta?.type === 'track';
    const tc = _isSingle ? 1 : (task.meta?.trackCount || task.meta?.totalTracks || 0);
    const spin = '<span class="qi-spinner"></span>';
    if(tc > 1){
      const done = Math.min(tc, Math.floor((task.progress||0)/100*tc));
      return `${spin}${done}/${tc}`;
    }
    return `${spin}${task.progress||0}%`;
  }
  if(task.status==='done')    return t('status.done');
  if(task.status==='error')   return t('status.error');
  if(task.status==='paused')  return t('status.paused');
  return t('status.queued');
}

async function removeTask(id) {
  // Optimistic: drop the card from the UI FIRST so ✕ feels instant (don't wait for
  // the DELETE round-trip or a WS queue_update). Reconcile via pullQueue on failure.
  S.queue = S.queue.filter(t => t.id !== id);
  renderQueue(); updateTransport();
  try { await api('DELETE',`/api/queue/${id}`); }
  catch(e){ toast('Не удалось удалить задачу','var(--red)'); pullQueue(); }
}

async function retryTask(id) {
  const r = await api('POST', `/api/queue/retry/${id}`);
  if(r.ok) toast(r.reused ? '↺ Повтор запущен' : '↺ Добавлено в очередь');
  else if(r.duplicate) toast('Уже в очереди', 'var(--muted)');
  else toast(r.msg || 'Ошибка повтора', 'var(--red)');
}

async function clearDone() {
  // Finished = done / error / cancelled.
  const done = S.queue.filter(t => t.status==='done' || t.status==='error' || t.status==='cancelled');
  if(!done.length){ toast('Нет готовых задач для очистки','var(--muted)'); return; }
  const removed = [];
  for(const t of done){
    try { await api('DELETE',`/api/queue/${t.id}`); removed.push(t.id); }
    catch(e){ /* keep it; reported below */ }
  }
  // Self-refresh: don't depend on the WS queue_update arriving — right after a
  // server restart / reconnect it can be missed, which made the button look dead.
  if(removed.length){
    const gone = new Set(removed);
    S.queue = S.queue.filter(t => !gone.has(t.id));
    renderQueue(); updateTransport();
  }
  const failed = done.length - removed.length;
  if(failed) toast(`Не удалось убрать ${failed} — сервер ответил ошибкой`,'var(--orange)');
  else toast(`Убрано: ${removed.length}`,'var(--green)');
}

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
    toast(r.msg || 'Не удалось запустить очередь','var(--orange)');
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
    if(_adv) _adv.textContent = (_ad === 0 ? 'Выкл' : _ad + ' мин'); }
  { const _ap = +(c['amd-parallel'] || 2);
    setVal('s-amd-parallel', _ap); }
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
      <label class="lbl">${esc(svc.toUpperCase())} · качество по умолчанию</label>
      <select onchange="saveSetting('guest-quality-${svc}',this.value)" style="width:100%">
        ${QSEL[svc].map(([v,l]) =>
          `<option value="${v}" ${(cfg['guest-quality-'+svc]===v)?'selected':''}>${esc(l)}</option>`
        ).join('')}
      </select>
    </div>`;

  root.innerHTML = `
    <div class="block">
      <div class="block-title">🎧 Плеер</div>
      ${toggle('player-gapless','Идеально без разрыва (gapless)','Web Audio API gapless. Пред-декодирует следующий трек (10–20 МБ).')}
      ${toggle('player-preload','Pre-load следующего трека','Гэп до ~200мс', true)}
      ${toggle('player-spin','Вращение обложки', 'CD-эффект на полноэкранном плеере.', true)}
      ${toggle('player-mobile-fs','Авто-полноэкранный на телефоне','Тап по плееру → fullscreen.', true)}
      ${toggle('player-viz','Визуализатор','FFT-спектр на background.', false)}
      <div class="settings-grid mt8">
        <div class="field-group">
          <label class="lbl">Громкость по умолчанию</label>
          <input type="range" min="0" max="1" step="0.05" value="${cfg['player-volume']??1}"
            oninput="saveSetting('player-volume',parseFloat(this.value));const _sv=parseFloat(this.value);if(window._WA?._audioSourceNode||window._WA?.curSource){if(typeof _waSetVolume==='function')_waSetVolume(_sv);const _sa=document.getElementById('pp-audio');if(_sa){_sa.volume=1;_sa.muted=(_sv===0);}}else{const _sa=document.getElementById('pp-audio');if(_sa){_sa.volume=_sv;}}"/>
        </div>
        <div class="field-group">
          <label class="lbl">Скорость воспроизведения</label>
          <select onchange="saveSetting('player-speed',parseFloat(this.value));const a=document.getElementById('pp-audio');if(a)a.playbackRate=parseFloat(this.value)">
            <option value="1" ${cfg['player-speed']==1?'selected':''}>1× (нормально)</option>
            <option value="1.25" ${cfg['player-speed']==1.25?'selected':''}>1.25×</option>
            <option value="1.5"  ${cfg['player-speed']==1.5?'selected':''}>1.5×</option>
            <option value="1.75" ${cfg['player-speed']==1.75?'selected':''}>1.75×</option>
            <option value="2"    ${cfg['player-speed']==2?'selected':''}>2×</option>
          </select>
        </div>
        <div class="field-group">
          <label class="lbl">Качество потока</label>
          <select onchange="saveSetting('player-stream-quality',this.value)">
            <option value="mp3"      ${ cfg['player-stream-quality']==='mp3'      || !cfg['player-stream-quality'] ? 'selected':''}>MP3 · 320 kbps</option>
            <option value="lossless" ${ cfg['player-stream-quality']==='lossless' ? 'selected':''}>FLAC · Lossless</option>
            <option value="hires"    ${ cfg['player-stream-quality']==='hires'    ? 'selected':''}>Hi-Res · 24-bit</option>
          </select>
        </div>
      </div>
    </div>

    <div class="block mt12">
      <div class="block-title">🎚 Эквалайзер</div>
      <div class="settings-grid" style="grid-template-columns:1fr 1fr 1fr">
        ${['bass','mid','treble'].map(b => `
          <div class="field-group">
            <label class="lbl">${esc(b==='bass'?'Низкие':b==='mid'?'Средние':'Высокие')} · ${parseFloat(cfg['player-eq-'+b]??0)} dB</label>
            <input type="range" min="-12" max="12" step="0.5" value="${cfg['player-eq-'+b]??0}"
              oninput="setEQ('${b}',this.value);this.previousElementSibling.firstElementChild?.replaceWith(document.createTextNode(this.value+' dB'));this.parentElement.querySelector('.lbl').textContent='${b==='bass'?'Низкие':b==='mid'?'Средние':'Высокие'} · '+this.value+' dB'"/>
          </div>`).join('')}
      </div>
      <div style="display:flex;gap:8px;margin-top:10px">
        <button class="btn-ghost btn-sm" onclick="resetEQ();renderGuestPrefs()">↺ Сбросить</button>
      </div>
    </div>

    <div class="block mt12">
      <div class="block-title">🎵 Качество скачивания (по умолчанию)</div>
      <div class="settings-grid">
        ${qSel('qobuz')}
        ${qSel('tidal')}
        ${qSel('deezer')}
      </div>
      <div style="font-size:10px;color:var(--muted2);margin-top:8px;line-height:1.5">
        Применяется при добавлении трека в очередь. Хозяин системы может ограничить максимальное качество.
      </div>
    </div>

    <div class="block mt12">
      <div class="block-title">🌐 Язык интерфейса</div>
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

// ── Dependency updates (owner-only, Settings → О сервисе) ──────────────────
async function loadDeps() {
  const box = document.getElementById('deps-list');
  if (box) box.innerHTML = '⏳ Проверяю pip (может занять до минуты)…';
  try {
    const r = await api('GET', '/api/admin/deps');
    const pkgs = r.packages || [];
    if (!pkgs.length) { box.innerHTML = '✅ Всё актуально — устаревших пакетов нет.'; return; }
    box.innerHTML = pkgs.map(p => {
      const pin = p.pinned ? ' <span title="закреплён — обновление ломает сборку">📌</span>' : '';
      const col = p.pinned ? '#ffb84d' : 'var(--text)';
      return `<div style="display:flex;justify-content:space-between;align-items:center;gap:8px;padding:5px 0;border-bottom:1px solid #ffffff11">
        <span style="color:${col};min-width:0;overflow:hidden;text-overflow:ellipsis">${p.name}${pin}
          <span style="color:var(--muted)">${p.version} → ${p.latest}</span></span>
        <button onclick="updateDep('${p.name}',${p.pinned?'true':'false'})"
          style="flex-shrink:0;padding:4px 10px;border-radius:7px;border:1px solid var(--red);background:transparent;color:var(--text);cursor:pointer;font-size:12px">⬆</button>
      </div>`;
    }).join('');
  } catch (e) { box.innerHTML = '⛔ ' + (e.message || e); }
}
async function updateDep(pkg, pinned) {
  if (pinned && !confirm(pkg + ' закреплён — обновление может сломать сборку (Qobuz/AMD/Widevine). Точно обновить?')) return;
  const box = document.getElementById('deps-list');
  if (box) box.innerHTML = '⏳ Обновляю ' + pkg + '… (до 10 мин)';
  try {
    const r = await api('POST', '/api/admin/deps/update', { package: pkg, force: !!pinned });
    if (r.pinned) { alert(r.msg); loadDeps(); return; }
    alert((r.ok ? '✅ ' : '⚠️ ') + pkg + ' — ' + (r.ok ? 'обновлён. Нужен рестарт app.py.' : 'не удалось, см. консоль.'));
    loadDeps();
  } catch (e) { alert('⛔ ' + (e.message || e)); loadDeps(); }
}
async function updateAllDeps() {
  if (!confirm('Обновить ВСЕ незакреплённые пакеты? Закреплённые (📌) не трогаются. Может занять время; после — рестарт app.py.')) return;
  const box = document.getElementById('deps-list');
  if (box) box.innerHTML = '⏳ Обновляю все незакреплённые… (это долго)';
  try {
    const r = await api('POST', '/api/admin/deps/update', { package: 'all' });
    const n = (r.updated || []).length;
    alert((r.ok ? '✅ ' : '⚠️ ') + 'Обработано пакетов: ' + n + (r.msg ? ('\n' + r.msg) : '') + '\nНужен рестарт app.py.');
    loadDeps();
  } catch (e) { alert('⛔ ' + (e.message || e)); loadDeps(); }
}

async function saveSetting(key, value) {
  const configKey = SETTING_KEY_MAP[key] || key;
  const _triggerEl = document.activeElement;
  // GUEST PATH: never write to server config. Store locally only.
  if (typeof _isGuest === 'function' && _isGuest()) {
    if (!_isGuestWritable(configKey)) {
      console.warn(`[guest] dropping write of '${configKey}' — owner-only setting`);
      return;
    }
    S.config[configKey] = value;
    _guestPrefsSave(configKey, value);
    if (configKey === 'quality') renderQualityGrid?.();
    else _showSavedChip(_triggerEl);
    return;
  }
  // OWNER PATH (server)
  const SECRET_KEYS = new Set(['qobuz-auth-token','qobuz-password','deezer-arl','tidal-token','tidal-refresh','media-user-token','authorization-token','qobuz-secrets','spotify-sp-dc']);
  if (SECRET_KEYS.has(configKey) && !value) return;
  S.config[configKey] = value;
  await api('POST','/api/config',{[configKey]:value});
  if(configKey==='quality') renderQualityGrid();
  else _showSavedChip(_triggerEl);
  if(configKey.startsWith('releases-') || configKey === 'qobuz-auth-token' || configKey === 'tidal-token') _syncReleasesSettingsTab();
  renderConfig();
}

// ── TOKENS ───────────────────────────────────────────────────
function _isMasked(v){ return typeof v === 'string' && v.startsWith('••'); }

// Set a secret/password field: if value is masked, clear the field and show
// a placeholder so the user sees an empty input (safe to paste into) instead
// of a mask string that would be re-sent and blocked by the server.
// Never overwrites a field the user is currently editing (document.activeElement check).
function _setSecret(id, v) {
  const el = document.getElementById(id);
  if (!el) return;
  if (document.activeElement === el) return; // user is typing — don't clobber
  if (_isMasked(v)) {
    const m = v.match(/\((\d+)\s/);
    el.placeholder = m ? ti('s.saved_chars', {n: m[1]}) : t('s.saved_replace');
    el.value = '';
  } else {
    el.placeholder = el.dataset.ph || '••••••••';
    el.value = v || '';
  }
}

function loadTokensToUI() {
  // If the server redacted a secret, show the field as empty with a hint
  // in the placeholder — never echo the mask dots into the input.
  const mut    = S.config['media-user-token']    || '';
  const bearer = S.config['authorization-token'] || '';
  const sf     = S.config['storefront']          || '';
  const mutEl    = document.getElementById('t-mut');
  const bearerEl = document.getElementById('t-bearer');
  if(mutEl){
    if(_isMasked(mut)){
      mutEl.value = '';
      mutEl.placeholder = ti('s.saved_chars2', {n: mut.match(/\d+/)?.[0]||'?'});
    } else { mutEl.value = mut; }
  }
  if(bearerEl){
    if(_isMasked(bearer)){
      bearerEl.value = '';
      bearerEl.placeholder = ti('s.saved_chars2', {n: bearer.match(/\d+/)?.[0]||'?'});
    } else { bearerEl.value = bearer; }
  }
  setVal('t-sf', sf);
}

async function saveTokens() {
  const mut    = document.getElementById('t-mut').value.trim();
  const bearer = document.getElementById('t-bearer').value.trim();
  const sf     = document.getElementById('t-sf').value.trim();
  // Only send fields the user actually filled in — empty means "keep existing".
  const patch  = {storefront: sf};
  if(mut    && !_isMasked(mut))    patch['media-user-token']     = mut;
  if(bearer && !_isMasked(bearer)) patch['authorization-token']  = bearer;
  await api('POST','/api/config', patch);
  if(mut)    S.config['media-user-token']    = mut;
  if(bearer) S.config['authorization-token'] = bearer;
  S.config['storefront'] = sf;
  updatePills();
  toast('Tokens saved!');
  // also notify via WebSocket so server can use them immediately
  if(ws?.readyState===WebSocket.OPEN && (mut || bearer)) {
    ws.send(JSON.stringify({type:'token_update', bearer: bearer||undefined, mut: mut||undefined}));
  }
}

async function autoFetchBearer() {
  const btn = document.getElementById('btn-autofetch');
  const status = document.getElementById('bearer-status');
  btn.disabled = true;
  btn.textContent = '⏳ Fetching…';
  status.textContent = 'Connecting to Apple Music…';
  try {
    const r = await fetch('/api/fetch-bearer');
    const data = await r.json();
    if(r.ok && data.token) {
      document.getElementById('t-bearer').value = data.token;
      S.config['authorization-token'] = data.token;
      status.textContent = '✓ Got token: ' + data.token.slice(0,20) + '…';
      status.style.color = 'var(--green)';
      updatePills();
      toast('Bearer token auto-fetched! 🎉', 'var(--green)');
    } else {
      status.textContent = data.detail || 'Failed';
      status.style.color = 'var(--red)';
      toast('Auto-fetch failed — paste manually', 'var(--red)');
    }
  } catch(e) {
    status.textContent = 'Network error: ' + e.message;
    status.style.color = 'var(--red)';
    toast('Error: ' + e.message, 'var(--red)');
  } finally {
    btn.disabled = false;
    btn.textContent = '⚡ Auto-fetch from Apple Music';
  }
}

function copyAllTokens() {
  const lines = [
    S.config['media-user-token']&&`media-user-token: ${S.config['media-user-token']}`,
    S.config['authorization-token']&&`bearer: ${S.config['authorization-token']}`,
    S.config['storefront']&&`storefront: ${S.config['storefront']}`,
  ].filter(Boolean).join('\n');
  if(lines){ navigator.clipboard.writeText(lines); toast('Copied!'); }
  else toast('No tokens saved yet','var(--orange)');
}

// ── CONFIG YAML ───────────────────────────────────────────────
function renderConfig() {
  const c = S.config;
  const q = QUALITIES.find(x=>x.id===c['quality'])||QUALITIES[0]||{};
  const yaml = `# Apple Music Downloader — config.yaml
# Quality: ${q.label||''} (${q.sub||''})  flag: ${q.flag||'(default)'}

media-user-token: "${c['media-user-token']||''}"
authorization-token: ""  # auto-fetched from browser
storefront: "${c['storefront']||'us'}"
language: "${c['language']||''}"

embed-cover: ${c['embed-cover']!==false}
cover-size: ${c['cover-size']==='original'?'0':c['cover-size']||'3000x3000'}
cover-format: ${c['cover-format']||'jpg'}
save-artist-cover: ${!!c['save-cover-to-folder']}
save-animated-artwork: false

embed-lrc: ${c['embed-lrc']!==false}
save-lrc-file: ${!!c['save-lrc-file']}
lrc-type: "${c['lrc-type']||'lyrics'}"
lrc-format: "${c['lrc-format']||'lrc'}"

alac-save-folder: "${c['save-path']||'downloads'}"
atmos-save-folder: "${c['save-path']||'downloads'}/Atmos"
aac-save-folder: "${c['save-path']||'downloads'}/AAC"

decrypt-m3u8-port: "${c['decrypt-port']||'127.0.0.1:10020'}"
get-m3u8-port: "${c['m3u8-port']||'127.0.0.1:20020'}"
max-memory-limit: ${c['max-memory']||256}
atmos-max: ${c['atmos-max']||2448}`;

  const el = document.getElementById('config-code');
  if(el) el.innerHTML = yaml
    .replace(/^([\w-]+):/gm, '<span class="ck">$1</span>:')
    .replace(/(#.*)$/gm, '<span class="cm">$1</span>')
    .replace(/"([^"]*)"/g, '<span class="cs">"$1"</span>');

  // CLI commands
  const cmdsEl = document.getElementById('cli-cmds');
  if(cmdsEl) {
    if(!S.queue.length){
      cmdsEl.innerHTML = `<div style="font-size:12px;color:var(--muted)">Add items to queue to see commands</div>`;
    } else {
      cmdsEl.innerHTML = S.queue.map(t=>{
        const q2 = QUALITIES.find(x=>x.id===t.quality)||QUALITIES[0]||{flag:''};
        const flag = q2.flag?q2.flag+' ':'';
        return `<div class="code-block" style="font-size:10.5px;padding:8px 12px">go run main.go ${flag}"${t.url}"</div>`;
      }).join('');
    }
  }
}

function copyConfig() {
  const c = S.config;
  const q = QUALITIES.find(x=>x.id===c['quality'])||{label:'',sub:'',flag:''};
  const yaml = `media-user-token: "${c['media-user-token']||''}"\nstorefront: "${c['storefront']||'us'}"\nquality: ${c['quality']||'alac'}\nembed-cover: ${c['embed-cover']!==false}\ncover-size: ${c['cover-size']||'3000x3000'}\nembed-lrc: ${c['embed-lrc']!==false}`;
  navigator.clipboard.writeText(yaml);
  toast('config.yaml copied!');
}
function refreshConfig(){ renderConfig(); toast('Refreshed'); }

// CONSOLE (log console view: render, copy, download, fix-deps) → moved to its own module file (see index.html).

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

  rows.push(_detailRow('Движок',  engineLabels[engine] || engine, '#0a84ff'));

  if(engine === 'amd') {
    rows.push(_detailRow('Instance', c['amd-instance-url'] || 'wm.wol.moe', 'var(--green)'));
    dotColor = 'var(--green)';  // AMD v2 works via public instance, so OK by default
  } else if(engine === 'gamdl') {
    rows.push(_detailRow('Cookies', (c['gamdl-cookies-path'] ? '✓ настроены' : '✗ не настроены'),
                         c['gamdl-cookies-path'] ? 'var(--green)' : 'var(--danger)'));
    dotColor = c['gamdl-cookies-path'] ? 'var(--green)' : 'var(--red)';
  } else if(engine === 'zhaarey') {
    // Apple Music with zhaarey needs both tokens
    const mut    = c['media-user-token'];
    const bearer = c['authorization-token'];
    rows.push(_detailRow('MUT',    mut    ? '✓ установлен' : '✗ отсутствует', mut    ? 'var(--green)' : 'var(--danger)'));
    rows.push(_detailRow('Bearer', bearer ? '✓ установлен' : '⏳ не получен',  bearer ? 'var(--green)' : 'var(--orange)'));
    if(mut && bearer)       dotColor = 'var(--green)';
    else if(mut || bearer)  dotColor = 'var(--orange)';
    else                    dotColor = 'var(--red)';
  } else if(engine === 'deezer') {
    const arl = c['deezer-arl'];
    rows.push(_detailRow('ARL', arl ? '✓ установлен' : '✗ отсутствует', arl ? 'var(--green)' : 'var(--danger)'));
    dotColor = arl ? 'var(--green)' : 'var(--red)';
  }

  if(q && q.label) rows.push(_detailRow('Качество', q.label, q.color || '#0a84ff'));
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

// SETUP + SELF-UPDATE (component checklist, installer, self-update, restart) → moved to its own module file (see index.html).

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

function applyStoredPrefs() {
  const t = localStorage.getItem('amd-theme') || 'dark';
  const f = localStorage.getItem('amd-font')  || 'system';
  setTheme(t);
  setFont(f);
  // Player preferences land from S.config (which is loaded a bit later).
  // Apply them once config is available, plus mirror to the UI inputs.
  const tryApply = () => {
    if (!S.config) { setTimeout(tryApply, 100); return; }
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
  const _msgs = {zhaarey:'🔵 zhaarey engine', gamdl:'🐍 gamdl engine', amd:'✨ AMD v2 — ALAC/Atmos без Apple ID!'};
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
  if(b){ b.textContent = `⬇ Скачать выбранное (${n})`; b.disabled = n === 0; b.style.opacity = n ? '1' : '.5'; }
}
async function albumDownloadSelected(){
  const sel = _albumSelCbs().filter(cb => cb.checked && cb.dataset.url);
  if(!sel.length){ toast('Отметь треки галочками','var(--orange)'); return; }
  const svc = (typeof Detail !== 'undefined' && Detail.currentAlbum) ? Detail.currentAlbum.service : 'apple';
  const q = resolveQuality(svc);
  const b = document.getElementById('alb-dl-sel');
  if(b){ b.disabled = true; b.textContent = '⏳ Добавляю…'; }
  let ok = 0;
  for(const cb of sel){
    try { const r = await api('POST','/api/queue/add',{url: cb.dataset.url, quality: q}); if(r && r.ok) ok++; } catch {}
  }
  toast(`+ ${ok}/${sel.length} ${ok===1?'трек':'треков'} → очередь`, ok ? 'var(--green)' : 'var(--red)');
  if(b){ b.textContent = `⬇ Скачать выбранное (${sel.length})`; b.disabled = false; b.style.opacity = '1'; }
}

async function artistReleaseDownload(service, releaseId, title, artist) {
  try {
    const r = await fetch(`/api/album/${service}/${encodeURIComponent(releaseId)}`);
    const d = await r.json();
    const url = d.album?.url;
    if (!url) { toast('Нет URL альбома', 'var(--red)'); return; }
    const res = await api('POST', '/api/queue/add', {url, quality: resolveQuality(service), title, artist});
    if (res.ok) toast(`+ ${title} → очередь`);
    else toast('Ошибка: ' + (res.detail || '?'), 'var(--red)');
  } catch(e) {
    toast(t('err.generic') + ': ' + e.message, 'var(--red)');
  }
}

async function albumAddTrack(urlOrId, title, artist){
  if(!urlOrId || !urlOrId.startsWith('http')){
    toast('Не могу добавить — нет URL трека','var(--red)'); return;
  }
  const r = await api('POST', '/api/queue/add', {url: urlOrId, quality: resolveQuality(detectSvcFromUrl(urlOrId) || 'apple'), title, artist});
  if(r.ok) toast(`+ ${title}`);
  else toast('Ошибка: '+(r.detail||'?'),'var(--red)');
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

// Service login UIs (Yandex/Tidal/Spotify) + token probe + Tidal import → moved to its own module file (see index.html).

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
    setReleasesStatus('✗ Сканер не отвечает — обнови страницу', 0, 1, 'var(--red)');
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
  try { toast('⚠ Spotify: ' + err + ' — Settings → Spotify', 'var(--orange)', '', 9000); } catch {}
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
  if(st) { st.textContent = hasPrev ? 'Обновляю…' : 'Загружаю релизы…'; st.style.display = 'block'; }
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
      empty.textContent = 'Нет подключённых сервисов — настрой в Settings → Релизы';
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
    if(st) { st.textContent = '⟳ Сканирование…'; st.style.display = 'block'; }
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
      toast('⚠ Spotify 403: добавь аккаунт в developer.spotify.com/dashboard → Settings → User Management', 'var(--orange)', '', 8000);
    } else {
      toast('⚠ ' + errors.slice(0, 2).join('; '), 'var(--orange)', '', 4000);
    }
  }

  if(!allReleases.length) {
    // No new results — keep previous data visible if available
    if(hasPrev) {
      toast('Нет новых релизов за выбранный период', 'var(--muted)', '', 3000);
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





