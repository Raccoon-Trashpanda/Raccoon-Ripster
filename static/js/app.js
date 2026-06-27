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
document.addEventListener('visibilitychange', () => { if (!document.hidden) _wsWatchdog(); });

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
    if(existing.has(task.id)){
      updateQueueItem(task, existing.get(task.id));
    } else {
      el.appendChild(buildQueueItem(task));
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
  // Self-refresh: don't depend on the WS queue_update arriving — right after a
  // server restart / reconnect it can be missed, which made the ✕ look dead.
  try { await api('DELETE',`/api/queue/${id}`); }
  catch(e){ toast('Не удалось удалить задачу','var(--red)'); return; }
  S.queue = S.queue.filter(t => t.id !== id);
  renderQueue(); updateTransport();
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
  const t = (S.queue || []).find(x => x.id === id);
  const title = t && t.meta && (t.meta.title || t.meta.artist);
  return title || `Задача ${String(id).slice(0, 6)}`;
}

// Rebuild the per-task <select> from task ids seen in the log buffer.
function _rebuildConsoleTaskFilter() {
  const sel = document.getElementById('console-task-filter');
  if (!sel) return;
  const cur = sel.value || 'all';
  const seen = new Set();
  let html = '<option value="all">Все задачи</option>';
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
  let html = '<option value="all">Все сервисы</option>';
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
function _refreshConsole() {
  const out = document.getElementById('console-out');
  if (!out) return;
  _rebuildConsoleTaskFilter();
  if (typeof _rebuildConsoleSvcFilter === 'function') _rebuildConsoleSvcFilter();
  out.innerHTML = '';
  const visible = _LOG.filter(_consolePass);
  if (visible.length === 0) {
    const hint = document.createElement('div');
    hint.style.cssText = 'color:var(--muted);font-style:italic;padding:10px 0';
    hint.textContent = _LOG.length
      ? 'Для выбранной задачи логов пока нет.'
      : 'Консоль пуста. Добавь ссылку в очередь и нажми ▶ Старт — логи появятся здесь.';
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
  if (!text) { if (window.toast) toast('Консоль пуста', 'var(--muted)'); return; }
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
    btn.textContent = ok ? '✓ Скопировано' : '✗ Не вышло';
    setTimeout(() => { btn.textContent = orig; }, 1500);
  }
  if (window.toast) toast(ok ? `📋 Скопировано (${text.length} симв.)` : 'Не удалось скопировать — выдели вручную', ok ? 'var(--green)' : 'var(--red)');
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
    if (btn) { const o = btn.textContent; btn.textContent = '⬇ Готово'; setTimeout(() => { btn.textContent = o; }, 1500); }
    if (window.toast) toast('⬇ Скачиваю zip с логами — пришли этот файл для диагностики', 'var(--green)', '', 6000);
  } catch (e) {
    if (window.toast) toast('Не удалось скачать лог: ' + ((e && e.message) || e), 'var(--red)');
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

// ── COOKIES.TXT ────────────────────────────────────────────────
async function importCookiesFile(input) {
  const file = input.files[0];
  if(!file) return;
  const text = await file.text();
  const r    = await api('POST','/api/upload-cookies',{
    content: text,
    path: S.config['gamdl-cookies-path'] || '',
  });
  if(r.ok){
    S.config['gamdl-cookies-path'] = r.path || '';
    setVal('s-cookies-path', r.path||'');
    setVal('t-cookies-path', r.path||'');
    toast('cookies.txt импортирован ✓','var(--green)');
    checkCookies();
  } else toast('Ошибка: '+(r.detail||r.msg||''),'var(--red)');
  input.value='';
}

// Pick a cookies.txt from the file explorer for the gamdl Apple section: read it,
// drop it into the #apple-cookies textarea, and save via the same /api/apple/cookies
// endpoint as the manual paste — so users don't have to open + copy the file by hand.
async function importAppleCookiesFile(input) {
  const file = input.files[0];
  if(!file) return;
  const st = document.getElementById('apple-cookies-status');
  try {
    const text = await file.text();
    const el = document.getElementById('apple-cookies');
    if(el) el.value = text;                         // reflect what was loaded
    if(st){ st.textContent='…'; st.style.color='var(--muted)'; }
    const r = await api('POST','/api/apple/cookies',{text});
    if(r&&r.ok){
      if(st){ st.textContent = r.exists?('✅ Сохранено: '+r.lines+' cookies'+(r.looks_apple?'':' ⚠ нет apple.com')):'🗑 очищено'; st.style.color='var(--green,#30d158)'; }
      toast('cookies.txt загружен из файла ✓','var(--green)');
    } else {
      if(st){ st.textContent='✗ '+((r&&r.error)||'ошибка'); st.style.color='#fc3c44'; }
    }
  } catch(e){ if(st){ st.textContent='✗ '+e; st.style.color='#fc3c44'; } }
  input.value='';
}

async function checkCookies() {
  const statusEl  = document.getElementById('cookies-status');
  const tokEl     = document.getElementById('cookies-tok-status');
  const cookiePill= document.getElementById('cookies-pill');
  const ituaWarn  = document.getElementById('cookies-itua-warn');
  if(statusEl) statusEl.textContent = '⏳…';
  try {
    const r = await fetch('/api/check-cookies');
    const d = await r.json();
    const ok  = d.valid;

    // Show/hide itua warning — the most common failure
    if(ituaWarn) ituaWarn.style.display = (d.exists && d.has_itua===false) ? '' : 'none';

    const msg = ok
      ? `✓ ${d.account||'Apple Music'}${d.storefront?' · '+d.storefront.toUpperCase():''}  (${d.lines||0} cookies)`
      : `✗ ${d.msg||'Invalid'}`;
    const col = ok ? 'var(--green)' : (d.has_itua===false ? 'var(--orange)' : 'var(--red)');
    if(statusEl){ statusEl.textContent=msg; statusEl.style.color=col; }
    if(tokEl)   { tokEl.textContent=msg;    tokEl.style.color=col;    }
    if(cookiePill){
      cookiePill.className = 'pill '+(ok?'pill-ok':'pill-err');
      cookiePill.innerHTML = `<div class="dot"></div>Cookies: ${ok?'OK ✓':d.has_itua===false?'No itua':'✗'}`;
      cookiePill.title = msg;
    }
  } catch(e) {
    if(statusEl){ statusEl.textContent='✗ Ошибка'; statusEl.style.color='var(--red)'; }
  }
}

async function checkAMDStatus() {
  try {
    const d = await (await fetch('/api/amd/status')).json();
    const dot=document.getElementById('amd-status-dot');
    const txt=document.getElementById('amd-status-text');
    const btn=document.getElementById('amd-install-btn');
    if(d.cloned){
      if(dot) dot.textContent='✓';
      if(txt){txt.textContent='Установлен · '+d.path; txt.style.color='var(--green)';}
      if(btn){btn.textContent='↺ Переустановить'; btn.disabled=false;}
    } else {
      if(dot) dot.textContent='○';
      if(txt){txt.textContent='Не установлен'; txt.style.color='var(--orange)';}
      if(btn){btn.textContent='⬇ Установить'; btn.disabled=false;}
    }
  } catch(e){}
}
async function checkAMDWrapperStatus() {
  const el     = document.getElementById('amd-wm-status');
  const dotEl  = document.getElementById('amd-wm-dot');
  const txtEl  = document.getElementById('amd-wm-text');
  const cliEl  = document.getElementById('amd-wm-clients');
  const regEl  = document.getElementById('amd-wm-regions');
  if(!el) return;
  el.style.display = '';
  if(dotEl) { dotEl.textContent = '●'; dotEl.style.color = 'var(--muted)'; }
  if(txtEl)  txtEl.textContent = t('as.checking');
  if(cliEl)  cliEl.textContent = '';
  if(regEl)  regEl.textContent = '';
  try {
    const r = await api('GET', '/api/amd/wrapper-status');
    if(r.error) {
      el.style.background = 'rgba(255,69,58,.1)';
      el.style.border = '1px solid rgba(255,69,58,.2)';
      if(dotEl) { dotEl.textContent='●'; dotEl.style.color='var(--red)'; }
      if(txtEl) { txtEl.textContent=t('as.amd_unavailable'); txtEl.style.color='var(--red)'; }
      if(cliEl)  cliEl.textContent = r.error;
      return;
    }
    if(r.ready) {
      el.style.background = 'rgba(62,207,170,.08)';
      el.style.border = '1px solid rgba(62,207,170,.2)';
      if(dotEl) { dotEl.textContent='●'; dotEl.style.color='var(--green)'; }
      if(txtEl) { txtEl.textContent=t('as.amd_ready'); txtEl.style.color='var(--green)'; }
    } else {
      const hasClients = (r.client_count || 0) > 0;
      el.style.background = hasClients ? 'rgba(239,159,39,.1)' : 'rgba(255,69,58,.1)';
      el.style.border = hasClients ? '1px solid rgba(239,159,39,.25)' : '1px solid rgba(255,69,58,.2)';
      if(dotEl) { dotEl.textContent='●'; dotEl.style.color = hasClients ? 'var(--orange)' : 'var(--red)'; }
      if(txtEl) {
        txtEl.textContent = hasClients
          ? t('as.amd_working_noready')
          : t('as.amd_not_ready_noclients');
        txtEl.style.color = hasClients ? 'var(--orange)' : 'var(--red)';
      }
    }
    if(cliEl) cliEl.textContent = ti('as.amd_clients', {n: r.client_count || 0});
    if(regEl && r.regions?.length) regEl.textContent = ti('as.amd_regions', {list: r.regions.join(', ')});
    else if(regEl) regEl.textContent = t('as.amd_no_accounts');
  } catch(e) {
    if(txtEl) txtEl.textContent = ti('as.amd_error', {msg: e.message});
  }
}

async function installAMD() {
  const btn=document.getElementById('amd-install-btn');
  const txt=document.getElementById('amd-status-text');
  if(btn){btn.disabled=true; btn.textContent='⏳ Устанавливаю…';}
  if(txt){txt.textContent='Клонирование + зависимости…'; txt.style.color='var(--muted)';}
  const sNav=document.querySelector('.nav-item[data-view="setup"]');
  if(sNav) showView('setup',sNav);
  toast('⬇ Устанавливаю AMD v2…','var(--blue)');
  try { await fetch('/api/setup/amd',{method:'POST'}); }
  catch(e){ toast('Ошибка: '+e.message,'var(--red)'); if(btn){btn.disabled=false;} }
}

// ══ SEARCH ═══════════════════════════════════════════════════════
const _SEARCH_SVCS = [
  {value: 'apple',    label: '🍎 Apple Music', key: 'apple'},
  {value: 'deezer',   label: '🎵 Deezer',       key: 'deezer'},
  {value: 'qobuz',    label: '🎼 Qobuz',         key: 'qobuz'},
  {value: 'tidal',    label: '🌊 Tidal',         key: 'tidal'},
  {value: 'spotify',  label: '🟢 Spotify',       key: 'spotify'},
  {value: 'beatport', label: '🎧 Beatport',      key: 'beatport'},
  {value: 'yandex',   label: '🟡 Яндекс.Музыка', key: 'yandex'},
];

async function _refreshSearchSvcSelect() {
  const sel = document.getElementById('search-svc');
  if (!sel) return;
  try {
    const status = await fetch('/api/services/status').then(r => r.json());
    const cur = sel.value;
    const opts = _SEARCH_SVCS.filter(o => status[o.key] !== false && status[o.key]);
    if (!opts.length) return;
    sel.innerHTML = opts.map(o => `<option value="${o.value}">${o.label}</option>`).join('');
    if ([...sel.options].some(o => o.value === cur)) sel.value = cur;
    onSearchSvcChange();
  } catch (_) {}
}

function onSearchSvcChange() {
  const svc   = document.getElementById('search-svc')?.value;
  const hint  = document.getElementById('search-svc-hint');
  const typeEl= document.getElementById('search-type');
  if(hint) { hint.textContent = ''; hint.style.display = 'none'; }
  // Beatport has tracks/releases instead of album/track/artist
  if(typeEl) {
    if(svc === 'beatport') {
      typeEl.innerHTML = `<option value="tracks">Треки</option><option value="releases">Релизы</option>`;
    } else {
      typeEl.innerHTML = `
        <option value="album" data-i18n="search.type_album">Альбомы</option>
        <option value="track" data-i18n="search.type_track">Треки</option>
        <option value="artist" data-i18n="search.type_artist">Артисты</option>
        ${svc === 'apple' ? '<option value="video">Видео (клипы)</option>' : ''}`;
    }
  }
}

let _srchItems = [];   // last raw results (relevance order)

function _searchSort(btn, key) {
  document.querySelectorAll('.srch-sort-btn').forEach(b => {
    const active = b === btn;
    b.style.background = active ? 'var(--surface2)' : 'var(--surface)';
    b.style.color      = active ? 'var(--text)'     : 'var(--muted)';
    b.classList.toggle('active', active);
  });
  const grid = document.getElementById('search-results');
  if(!grid || !_srchItems.length) return;
  let data = _srchItems.slice();
  if(key === 'date_desc')   data.sort((a,b)=>(b.date||b.year||'').localeCompare(a.date||a.year||''));
  else if(key === 'date_asc')  data.sort((a,b)=>(a.date||a.year||'').localeCompare(b.date||b.year||''));
  else if(key === 'tracks_desc') data.sort((a,b)=>(b.tracks||0)-(a.tracks||0));
  else if(key === 'tracks_asc') data.sort((a,b)=>(a.tracks||0)-(b.tracks||0));
  // relevance: restore original order
  _renderSearchGrid(grid, data);
}

function _renderSearchGrid(grid, items) {
  const svc = document.getElementById('search-svc')?.value || 'apple';
  grid.innerHTML = items.map(item => _renderSearchCard(item, svc)).join('');
  const cnt = document.getElementById('search-count');
  if(cnt) cnt.textContent = `${items.length} результатов`;
}

async function doSearch() {
  const q    = document.getElementById('search-q')?.value?.trim();
  const svc  = document.getElementById('search-svc')?.value || 'apple';
  const type = document.getElementById('search-type')?.value || 'album';
  const st   = document.getElementById('search-status');
  const grid = document.getElementById('search-results');
  const sortBar = document.getElementById('search-sort-bar');
  if(!q){ toast('Введи запрос'); return; }

  // If it's a direct URL — use smart service modal (same as main URL bar)
  if(q.startsWith('http')) {
    const urlSvc2 = detectSvcFromUrl(q);
    const qual2   = resolveQuality(urlSvc2 || 'apple');
    if(urlSvc2 === 'spotify') {
      const spEng2 = (S.config && S.config['spotify-engine']) || 'convert';
      if(spEng2 === 'orpheus_spotify') {
        await _doAddUrl(q, S.config['orpheus-quality']||'hifi', 'spotify');
      } else {
        showUrlServiceModal(q, qual2, urlSvc2);
      }
    } else {
      await _doAddUrl(q, qual2, urlSvc2);
    }
    document.getElementById('search-q').value='';
    return;
  }
  if(st){ st.textContent='Ищу…'; st.style.display='block'; }
  if(grid) grid.innerHTML='';
  try {
    let items = [], error = null;

    if(svc === 'beatport') {
      // Route to dedicated Beatport search API
      const bpType = (type === 'tracks' || type === 'track') ? 'tracks' : 'releases';
      const r = await fetch(`/api/beatport/search?q=${encodeURIComponent(q)}&type=${bpType}&per_page=24`);
      let d; try { d = await r.json(); } catch(_) { d = {detail: `HTTP ${r.status}`}; }
      if(!r.ok || d.detail) { error = d.detail || `HTTP ${r.status}`; }
      else { items = d.results || []; }
    } else {
      const r = await fetch(`/api/search?q=${encodeURIComponent(q)}&service=${svc}&type=${type}&limit=24`);
      // Body can be empty (401, CORS preflight, network blip) — guard the parse.
      // Safari throws "Unexpected end of JSON" / "did not match the expected pattern"
      // when feeding "" to JSON.parse, which used to surface as a cryptic toast.
      let d;
      try { d = await r.json(); }
      catch (_) {
        if (r.status === 401) { error = 'Нужен вход (сессия истекла) — обнови страницу.'; }
        else { error = `HTTP ${r.status}${r.statusText ? ' — '+r.statusText : ''}`; }
        d = null;
      }
      if (d) {
        if (d.error && !d.results?.length) { error = d.error; }
        else { items = d.results || []; }
      }
    }

    if(st) st.style.display='none';
    if(error){ if(st){st.textContent='Ошибка: '+error;st.style.display='block';} return; }
    if(!items.length){
      if(sortBar) sortBar.style.display='none';
      // Suggest other services for the same query — Qobuz/Tidal/Beatport
      // catalogs vary a lot; the user shouldn't have to guess.
      const others = ['apple','deezer','qobuz','tidal','spotify','beatport','yandex'].filter(s => s !== svc);
      const links = others.map(s =>
        `<button onclick="document.getElementById('search-svc').value='${s}';doSearch()" style="padding:4px 11px;background:rgba(255,255,255,.06);color:${_svcColor(s)};border:1px solid ${_svcColor(s)}55;border-radius:7px;font-size:12px;font-weight:600;cursor:pointer;font-family:var(--font);margin:2px">${esc(_svcLabel(s))}</button>`
      ).join('');
      if(st){
        st.innerHTML = `Ничего не найдено в <b style="color:${_svcColor(svc)}">${esc(_svcLabel(svc))}</b>.<br><span style="font-size:11px;color:var(--muted2)">Каталог отличается — попробуй другой сервис:</span><div style="margin-top:8px">${links}</div>`;
        st.style.display='block';
      }
      return;
    }

    // Store raw (relevance) order, reset sort UI, render
    _srchItems = items.slice();
    if(sortBar) {
      sortBar.style.display = 'flex';
      document.querySelectorAll('.srch-sort-btn').forEach(b => {
        const active = b.dataset.sort === 'relevance';
        b.style.background = active ? 'var(--surface2)' : 'var(--surface)';
        b.style.color      = active ? 'var(--text)'     : 'var(--muted)';
        b.classList.toggle('active', active);
      });
    }
    if(grid) { _renderSearchGrid(grid, items); }
  } catch(e) {
    if(st){ st.textContent='Ошибка: '+e.message; st.style.display='block'; }
  }
}

function _renderSearchCard(item, svc) {
    const artUrl = item.artworkUrl || item.cover || '';
    const cover = artUrl
      ? `<img src="${esc(artUrl)}" data-lightbox style="width:100%;aspect-ratio:1;object-fit:cover;display:block;cursor:zoom-in" loading="lazy" onerror="this.style.display='none'"/>`
      : `<div style="width:100%;aspect-ratio:1;background:rgba(255,255,255,.05);display:flex;align-items:center;justify-content:center;font-size:28px">♪</div>`;

    const dateStr = item.date || '';
    const dateFmt = dateStr.length >= 10
      ? new Date(dateStr + 'T00:00:00').toLocaleDateString('ru', {day:'numeric', month:'short', year:'numeric'})
      : (item.year || '');
    const hiresBadge = item.hires ? `<span style="font-size:8px;padding:1px 4px;border-radius:3px;background:rgba(255,214,10,.15);color:#ffd60a;font-weight:700;margin-left:3px">HI-RES</span>` : '';

    const linkBtn = item.url
      ? `<a href="${esc(item.url)}" target="_blank" onclick="event.stopPropagation()" style="padding:5px 7px;background:var(--surface2);border:1px solid var(--border);border-radius:6px;font-size:11px;color:var(--muted);text-decoration:none;display:flex;align-items:center;flex-shrink:0">↗</a>`
      : '';
    const copyBtn = item.url
      ? `<button onclick="event.stopPropagation();navigator.clipboard.writeText('${escJ(item.url)}');toast(t('toast.link_copied'),'var(--green)')" title="Скопировать ссылку" style="padding:5px 7px;background:var(--surface2);border:1px solid var(--border);border-radius:6px;font-size:11px;color:var(--muted);cursor:pointer;flex-shrink:0">⎘</button>`
      : '';


      // ── Beatport track card ─────────────────────────────────────
      // Only fire for ACTUAL Beatport results — earlier this branch was
      // gating on `type==='track'` alone and ate Qobuz / Tidal / Apple tracks
      // (all also have type:'track'), painting them with a green BEATPORT badge.
      if(item.type === 'track' && item.service === 'beatport') {
        const previewUrl = item.previewUrl || item.preview || '';
        const previewBtn = previewUrl
          ? `<button onclick="event.stopPropagation();playPreview('${escJ(previewUrl)}','${escJ(item.title)}','${escJ(item.artist)}','${escJ(item.cover||item.artworkUrl||'')}')" style="padding:4px 7px;background:rgba(1,244,156,.15);color:#01f49c;border:1px solid rgba(1,244,156,.4);border-radius:6px;font-size:11px;cursor:pointer" title="Слушать">▶</button>`
          : '';
        const bpmLabel = item.bpm ? `<span style="font-size:9px;color:var(--muted2)">${item.bpm} BPM</span>` : '';
        const genreLabel = item.genre ? `<span style="font-size:9px;background:rgba(1,244,156,.12);color:#01f49c;padding:1px 5px;border-radius:3px;flex-shrink:0">${esc(item.genre)}</span>` : '';
        const mixLabel = item.mix ? ` <span style="color:var(--muted2);font-weight:400">(${esc(item.mix)})</span>` : '';
        return `
          <div style="background:var(--surface);border:1px solid var(--border);border-radius:10px;overflow:hidden;transition:border-color .15s;position:relative" onmouseover="this.style.borderColor='#01f49c'" onmouseout="this.style.borderColor='var(--border)'">
            <div onclick="searchAddToQueue('${escJ(item.url)}','${escJ(item.title)}','${escJ(item.artist)}')" style="cursor:pointer">${cover}</div>
            <div style="position:absolute;top:6px;left:6px;background:rgba(0,0,0,.7);color:#01f49c;font-size:8px;font-weight:700;padding:2px 5px;border-radius:3px;letter-spacing:.4px">BEATPORT</div>
            <div style="padding:8px 9px">
              <div style="font-size:12px;font-weight:600;color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;cursor:pointer" onclick="searchAddToQueue('${escJ(item.url)}','${escJ(item.title)}','${escJ(item.artist)}')" title="${escJ(item.title)}">${esc(item.title)||'—'}${mixLabel}</div>
              <div style="font-size:11px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(item.artist)||''}</div>
              <div style="display:flex;align-items:center;gap:4px;margin-top:2px;margin-bottom:6px;flex-wrap:wrap">
                ${genreLabel}${bpmLabel}
                ${dateFmt ? `<span style="font-size:9px;color:var(--muted2);margin-left:auto">${dateFmt}</span>` : ''}
              </div>
              <div style="display:flex;gap:4px">
                <button onclick="searchAddToQueue('${escJ(item.url)}','${escJ(item.title)}','${escJ(item.artist)}')" style="flex:1;padding:4px 0;background:#01f49c;color:#000;border:none;border-radius:6px;font-size:10px;font-weight:700;cursor:pointer;font-family:var(--font)">⬇</button>
                ${previewBtn}${linkBtn}${copyBtn}
              </div>
            </div>
          </div>`;
      }

      // ── Beatport release card ────────────────────────────────────
      // Same scope-fix as the track branch above.
      if(item.type === 'release' && item.service === 'beatport') {
        const tcLabel = item.trackCount ? `<span style="font-size:10px;color:var(--muted2);flex-shrink:0">${item.trackCount} тр.</span>` : '';
        const labelRow = item.label ? `<div style="font-size:10px;color:var(--muted2);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-top:1px">${esc(item.label)}</div>` : '';
        const upcomingBadge = item.is_upcoming ? `<span style="font-size:8px;background:rgba(255,214,10,.15);color:#ffd60a;padding:1px 4px;border-radius:3px;font-weight:700">PRE</span>` : '';
        return `
          <div style="background:var(--surface);border:1px solid var(--border);border-radius:10px;overflow:hidden;transition:border-color .15s;position:relative" onmouseover="this.style.borderColor='#01f49c'" onmouseout="this.style.borderColor='var(--border)'">
            ${cover}
            <div style="position:absolute;top:6px;left:6px;background:rgba(0,0,0,.7);color:#01f49c;font-size:8px;font-weight:700;padding:2px 5px;border-radius:3px;letter-spacing:.4px">РЕЛИЗ ${upcomingBadge}</div>
            <div style="padding:8px 9px">
              <div style="font-size:12px;font-weight:600;color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis" title="${escJ(item.title)}">${esc(item.title)||'—'}</div>
              <div style="font-size:11px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-top:2px">${esc(item.artist)||''}</div>
              ${labelRow}
              <div style="display:flex;align-items:center;gap:4px;margin-top:2px;margin-bottom:7px">
                <div style="font-size:10px;color:var(--muted2);flex:1">${dateFmt}</div>
                ${tcLabel}
              </div>
              <div style="display:flex;gap:4px">
                <button onclick="searchAddToQueue('${escJ(item.url)}','${escJ(item.title)}','${escJ(item.artist)}')" style="flex:1;padding:5px 0;background:#01f49c;color:#000;border:none;border-radius:6px;font-size:10px;font-weight:700;cursor:pointer;font-family:var(--font)">⬇</button>
                ${linkBtn}${copyBtn}
              </div>
            </div>
          </div>`;
      }

      // ── Standard artist card ────────────────────────────────────
      if(item.type === 'artist') {
        return `
          <div style="background:var(--surface);border:1px solid var(--border);border-radius:10px;overflow:hidden;transition:border-color .15s;position:relative" onmouseover="this.style.borderColor='var(--red)'" onmouseout="this.style.borderColor='var(--border)'">
            ${cover}
            <div style="position:absolute;top:6px;right:6px;background:rgba(0,0,0,.7);color:#fff;font-size:9px;font-weight:700;padding:2px 6px;border-radius:4px;letter-spacing:.5px">АРТИСТ</div>
            <div style="padding:8px 9px">
              <div style="font-size:12px;font-weight:600;color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis" title="${escJ(item.title)}">${item.title||'—'}</div>
              <div style="font-size:11px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-bottom:7px">${item.artist||''}</div>
              <div style="display:flex;gap:5px">
                <button onclick="openArtistPage('${item.service}','${escJ(item.id)}')" style="flex:1;padding:5px 0;background:var(--surface2);color:var(--muted);border:1px solid var(--border);border-radius:6px;font-size:10px;font-weight:600;cursor:pointer;font-family:var(--font)">→ Дискография</button>
                ${linkBtn}${copyBtn}
              </div>
            </div>
          </div>`;
      }

      // ── Standard album / playlist card ──────────────────────────
      if(item.type === 'album' || item.type === 'playlist') {
        const tcLabel = item.tracks ? `<span style="font-size:10px;color:var(--muted2);flex-shrink:0">${item.tracks} тр.</span>` : '';
        const typeTag = item.type === 'playlist' ? 'ПЛЕЙЛИСТ' : 'АЛЬБОМ';
        const labelRow = item.label ? `<div style="font-size:10px;color:var(--muted2);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-top:1px" title="${escJ(item.label)}">${esc(item.label)}</div>` : '';
        const canStream = (item.service === 'qobuz' || item.service === 'tidal' || item.service === 'deezer');
        const playOverlay = canStream
          ? `<div style="position:absolute;bottom:6px;right:6px;background:rgba(0,0,0,.72);border-radius:50%;width:32px;height:32px;display:flex;align-items:center;justify-content:center;font-size:13px;color:#fff;cursor:pointer;backdrop-filter:blur(6px);transition:transform .12s,background .12s" onclick="event.stopPropagation();playAlbumById('${item.service}','${escJ(item.id)}','${escJ(item.title)}','${escJ(item.artist)}','${escJ(item.artworkUrl||item.cover||'')}')" onmouseover="this.style.transform='scale(1.08)';this.style.background='var(--red)'" onmouseout="this.style.transform='';this.style.background='rgba(0,0,0,.72)'" title="Слушать альбом">▶</div>`
          : '';
        return `
          <div style="background:var(--surface);border:1px solid var(--border);border-radius:10px;overflow:hidden;transition:border-color .15s;position:relative" onmouseover="this.style.borderColor='var(--red)'" onmouseout="this.style.borderColor='var(--border)'">
            <div style="position:relative">${cover}${playOverlay}</div>
            <div style="position:absolute;top:6px;left:6px;background:rgba(0,0,0,.65);color:rgba(255,255,255,.65);font-size:8px;font-weight:700;padding:2px 5px;border-radius:3px;letter-spacing:.4px">${typeTag}</div>
            <div style="padding:8px 9px">
              <div style="font-size:12px;font-weight:600;color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis" title="${escJ(item.title)}">${item.title||'—'}${hiresBadge}</div>
              <div style="font-size:11px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-top:2px">${item.artist||''}</div>
              ${labelRow}
              <div style="display:flex;align-items:center;gap:4px;margin-top:2px;margin-bottom:7px">
                <div style="font-size:10px;color:var(--muted2);flex:1">${dateFmt}</div>
                ${tcLabel}
              </div>
              <div style="display:flex;gap:4px">
                <button onclick="searchAddToQueue('${escJ(item.url)}','${escJ(item.title)}','${escJ(item.artist)}')" style="flex:1;padding:5px 0;background:var(--red);color:#fff;border:none;border-radius:6px;font-size:10px;font-weight:700;cursor:pointer;font-family:var(--font)">⬇</button>
                <button onclick="openAlbumPage('${item.service}','${escJ(item.id)}')" style="padding:5px 8px;background:var(--surface2);color:var(--muted);border:1px solid var(--border);border-radius:6px;font-size:10px;font-weight:600;cursor:pointer;font-family:var(--font)" title="Треки">≡</button>
                ${linkBtn}${copyBtn}
              </div>
            </div>
          </div>`;
      }

      // ── Standard track card (fallback) ──────────────────────────
      const svcColor = _svcColor(item.service);
      const svcLabel = (item.service || '').toUpperCase();
      const canStream = (item.service === 'qobuz' || item.service === 'tidal' || item.service === 'deezer');
      const playFull = canStream && item.id
        ? `<button onclick="event.stopPropagation();playStreamTrack('${item.service}','${item.id}','${escJ(item.title)}','${escJ(item.artist)}','${escJ(item.artworkUrl||item.cover||'')}')" style="padding:4px 7px;background:rgba(${item.service==='qobuz'?'24,112,245':item.service==='tidal'?'0,212,179':'162,56,255'},.12);color:${svcColor};border:1px solid rgba(${item.service==='qobuz'?'24,112,245':item.service==='tidal'?'0,212,179':'162,56,255'},.25);border-radius:6px;font-size:11px;cursor:pointer" title="Полный трек">▶</button>`
        : '';
      const previewBtn = item.preview
        ? `<button onclick="event.stopPropagation();playPreview('${escJ(item.preview)}','${escJ(item.title)}','${escJ(item.artist)}','${escJ(item.cover||item.artworkUrl||'')}')" style="padding:4px 7px;background:rgba(255,255,255,.08);color:var(--text);border:1px solid var(--border);border-radius:6px;font-size:11px;cursor:pointer" title="30 сек">▶30</button>`
        : '';
      return `
        <div style="background:var(--surface);border:1px solid var(--border);border-radius:10px;overflow:hidden;transition:border-color .15s;position:relative" onmouseover="this.style.borderColor='${svcColor}'" onmouseout="this.style.borderColor='var(--border)'">
          <div onclick="searchAddToQueue('${escJ(item.url)}','${escJ(item.title)}','${escJ(item.artist)}')" style="cursor:pointer;position:relative">${cover}
            ${svcLabel ? `<div style="position:absolute;top:6px;left:6px;background:rgba(0,0,0,.72);color:${svcColor};font-size:8px;font-weight:700;padding:2px 5px;border-radius:3px;letter-spacing:.4px;backdrop-filter:blur(4px)">${esc(svcLabel)}</div>` : ''}
          </div>
          <div style="padding:8px 9px">
            <div style="font-size:12px;font-weight:600;color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;cursor:pointer" onclick="searchAddToQueue('${escJ(item.url)}','${escJ(item.title)}','${escJ(item.artist)}')" title="${escJ(item.title)}">${item.title||'—'}</div>
            <div style="font-size:11px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-bottom:6px">${item.artist||''}${dateFmt ? ' · '+dateFmt : ''}</div>
            <div style="display:flex;gap:4px">
              <button onclick="searchAddToQueue('${escJ(item.url)}','${escJ(item.title)}','${escJ(item.artist)}')" style="flex:1;padding:4px 0;background:var(--red);color:#fff;border:none;border-radius:6px;font-size:10px;font-weight:700;cursor:pointer;font-family:var(--font)">⬇</button>
              ${playFull}${previewBtn}${linkBtn}${copyBtn}
            </div>
          </div>
        </div>`;
}

// Escape a user string for safe inclusion inside an HTML attribute that wraps
// a single-quoted JS string literal, e.g. onclick="foo('VALUE')".
//
// Two layers to escape for: the HTML attribute parser (decodes &amp; &lt; etc)
// AND the JS string literal that the parser then executes. The order matters:
//   1. HTML entity encoding first (so &quot;/&lt;/&amp; work correctly)
//   2. JS backslash escaping on what the engine actually sees
// This correctly handles apostrophes in titles like "Guns N' Roses" —
// the old one-liner silently did nothing because "\'" in a JS string is
// simply "'".
function escJ(s) {
  if (s == null) return '';
  let out = String(s);
  // HTML-entity layer
  out = out.replace(/&/g, '&amp;');
  out = out.replace(/</g, '&lt;');
  out = out.replace(/>/g, '&gt;');
  out = out.replace(/"/g, '&quot;');
  // JS-string layer (real chars after HTML decoding)
  out = out.replace(/\\/g, '\\\\');
  out = out.replace(/'/g, "\\'");
  out = out.replace(/\r?\n/g, '\\n');
  return out;
}

// ─── Detail overlay (artist / album pages) ────────────────────────────────
// Both pages share the same overlay element and a simple state object.
const Detail = {
  currentArtist: null,   // {service, id, releases, filter}
  currentAlbum:  null,   // {service, id, tracks}
  _stack:        [],     // navigation history for ← back
};

function _detailUpdateBack() {
  const btn = document.getElementById('detail-back-btn');
  if(btn) btn.style.display = Detail._stack.length > 0 ? '' : 'none';
}

function detailGoBack() {
  const prev = Detail._stack.pop();
  if(!prev) { closeDetail(); return; }
  if(prev.type === 'artist') {
    Detail.currentArtist = prev;
    Detail.currentAlbum  = null;
    renderArtistPage();
  } else if(prev.type === 'album') {
    Detail.currentAlbum  = prev;
    Detail.currentArtist = null;
    renderAlbumPage();
  }
  _detailUpdateBack();
}

function closeDetail(){
  const el = document.getElementById('detail-overlay');
  if(!el) return;
  el.classList.remove('open');
  setTimeout(() => { if(!el.classList.contains('open')) el.style.display = 'none'; }, 300);
  Detail.currentArtist = null;
  Detail.currentAlbum  = null;
  Detail._stack        = [];
  _detailUpdateBack();
}

function _detailLoading(msg){
  const c  = document.getElementById('detail-content');
  const bc = document.getElementById('detail-breadcrumb');
  if(c)  c.innerHTML = `<div style="text-align:center;padding:80px 0;color:var(--muted)">${msg||'Загрузка…'}</div>`;
  if(bc) bc.textContent = '';
  const o = document.getElementById('detail-overlay');
  if(!o) return;
  if(o.style.display === 'none' || !o.style.display) {
    o.style.display = 'block';
    requestAnimationFrame(() => o.classList.add('open'));
  }
}

function _detailError(msg){
  const c = document.getElementById('detail-content');
  if(c) c.innerHTML = `<div style="text-align:center;padding:80px 0;color:var(--red)">Ошибка: ${esc(msg)}</div>`;
}

// ─── Artist page ─────────────────────────────────────────────────────────
async function openArtistPage(service, artistId){
  Detail._stack = [];          // root navigation — clear history
  _detailUpdateBack();
  _detailLoading(`Гружу артиста…`);
  try {
    // Ask backend for every release type; we filter client-side for responsiveness.
    const r = await fetch(`/api/artist/${service}/${encodeURIComponent(artistId)}?types=album,single,ep,compilation,live`);
    const d = await r.json();
    if(d.error){ _detailError(d.error); return; }
    Detail.currentArtist = {
      service, id: artistId,
      artist: d.artist || {name:'?'},
      releases: d.releases || [],
      filter: 'all',
    };
    renderArtistPage();
  } catch(e){ _detailError(e.message); }
}

function renderArtistPage(){
  const {artist, releases, filter} = Detail.currentArtist;
  const counts = releases.reduce((acc, r) => {
    acc.all = (acc.all||0)+1;
    acc[r.type] = (acc[r.type]||0)+1;
    return acc;
  }, {});
  const filtered = filter==='all' ? releases : releases.filter(r => r.type===filter);

  const pill = (key, label) => {
    const n = counts[key] || 0;
    if(key !== 'all' && n === 0) return '';  // hide empty categories
    const active = filter===key;
    return `<button onclick="setArtistFilter('${key}')" style="padding:6px 13px;border-radius:8px;background:${active?'var(--red)':'var(--surface)'};color:${active?'#fff':'var(--muted)'};border:1px solid ${active?'var(--red)':'var(--border)'};font-size:12px;font-weight:600;cursor:pointer;font-family:var(--font)">${label} <span style="opacity:.7">${n}</span></button>`;
  };

  const header = `
    <div style="display:flex;gap:20px;margin-bottom:24px;align-items:flex-start;flex-wrap:wrap">
      ${artist.picture ? `<img src="${artist.picture}" data-lightbox style="width:140px;height:140px;border-radius:50%;object-fit:cover;flex-shrink:0;cursor:zoom-in" onerror="this.style.display='none'"/>` : ''}
      <div style="flex:1;min-width:260px">
        <div style="font-size:11px;color:var(--muted);letter-spacing:.8px;text-transform:uppercase;font-family:var(--display)">Артист · ${artist.service}</div>
        <div style="font-family:var(--display);font-size:32px;font-weight:800;color:var(--text);margin-top:4px;line-height:1.1">${esc(artist.name||'—')}</div>
        <div style="font-size:12px;color:var(--muted);margin-top:8px">
          ${artist.albums_total ? `${artist.albums_total} релизов · ` : ''}${artist.fans ? artist.fans.toLocaleString('ru')+' слушателей · ' : ''}${artist.genre ? esc(artist.genre) : ''}
        </div>
        ${artist.url ? `<a href="${artist.url}" target="_blank" style="font-size:11px;color:var(--red);margin-top:6px;display:inline-block">↗ Открыть на ${artist.service}</a>` : ''}
      </div>
    </div>
    <div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:18px">
      ${pill('all', 'Все')}
      ${pill('album', 'Альбомы')}
      ${pill('ep', 'EP')}
      ${pill('single', 'Синглы')}
      ${pill('compilation', 'Сборники')}
      ${pill('live', 'Лайвы')}
      ${pill('appears_on', 'Участие')}
    </div>`;

  const grid = filtered.length === 0
    ? `<div style="text-align:center;padding:60px 0;color:var(--muted)">В этой категории ничего нет</div>`
    : `<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(165px,1fr));gap:12px">
        ${filtered.map(r => `
          <div style="background:var(--surface);border:1px solid var(--border);border-radius:10px;overflow:hidden;transition:border-color .15s" onmouseover="this.style.borderColor='var(--red)'" onmouseout="this.style.borderColor='var(--border)'">
            ${r.cover ? `<img src="${r.cover}" data-lightbox style="width:100%;aspect-ratio:1;object-fit:cover;display:block;cursor:zoom-in" loading="lazy" onerror="this.style.display='none'"/>` : `<div style="width:100%;aspect-ratio:1;background:rgba(255,255,255,.05);display:flex;align-items:center;justify-content:center;font-size:26px">♪</div>`}
            <div style="padding:8px 10px">
              <div style="font-size:12px;font-weight:600;color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis" title="${esc(r.title)}">${esc(r.title)}</div>
              <div style="font-size:10px;color:var(--muted);margin-top:3px;display:flex;justify-content:space-between;gap:4px;margin-bottom:${r.label?'2px':'7px'}">
                <span>${r.year||''}</span>
                <span style="text-transform:uppercase;letter-spacing:.4px;background:rgba(255,255,255,.06);padding:1px 5px;border-radius:3px;font-size:9px">${r.type||'?'}</span>
              </div>
              ${r.label ? `<div style="font-size:10px;color:var(--muted);margin-top:2px;margin-bottom:7px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis" title="${esc(r.label)}">${esc(r.label)}</div>` : ''}
              <div style="display:flex;gap:5px">
                <button onclick="artistReleaseDownload('${r.service}','${esc(r.id)}','${escJ(r.title)}','${escJ(artist.name)}')" style="flex:1;padding:5px 0;background:var(--red);color:#fff;border:none;border-radius:6px;font-size:10px;font-weight:700;cursor:pointer;font-family:var(--font)">⬇</button>
                <button onclick="openAlbumPage('${r.service}','${esc(r.id)}')" style="padding:5px 10px;background:var(--surface2);color:var(--muted);border:1px solid var(--border);border-radius:6px;font-size:10px;font-weight:600;cursor:pointer;font-family:var(--font)" title="Треки">≡</button>
              </div>
            </div>
          </div>`).join('')}
      </div>`;

  const bc = document.getElementById('detail-breadcrumb');
  if(bc) bc.textContent = artist.name || 'Артист';
  document.getElementById('detail-content').innerHTML = header + grid;
}

function setArtistFilter(f){
  if(!Detail.currentArtist) return;
  Detail.currentArtist.filter = f;
  renderArtistPage();
}

// ─── Album page ──────────────────────────────────────────────────────────
async function openAlbumPage(service, albumId){
  // Save current context for back navigation
  if(Detail.currentArtist) {
    Detail._stack.push({type:'artist', ...Detail.currentArtist});
    _detailUpdateBack();
  } else if(Detail.currentAlbum) {
    Detail._stack.push({type:'album', ...Detail.currentAlbum});
    _detailUpdateBack();
  }
  _detailLoading('Гружу альбом…');
  try {
    const r = await fetch(`/api/album/${service}/${encodeURIComponent(albumId)}`);
    const d = await r.json();
    if(d.error){ _detailError(d.error); return; }
    Detail.currentAlbum  = {service, id: albumId, album: d.album||{}, tracks: d.tracks||[]};
    Detail.currentArtist = null;
    renderAlbumPage();
  } catch(e){ _detailError(e.message); }
}

function renderAlbumPage(){
  const {album, tracks, service} = Detail.currentAlbum;
  // Qobuz, Tidal, Deezer have full streaming via /api/stream/{service}/{id}
  const canStream   = (service === 'qobuz' || service === 'tidal' || service === 'deezer');
  const streamColor = service === 'qobuz' ? '#1870f5' : (service === 'tidal' ? '#00d4b3' : '#a238ff');

  // Build a preview queue so next/prev works across all tracks (for services with preview URLs).
  const _prevTracks = [];
  const _prevIdx = {};
  tracks.forEach(t => {
    if (t.preview) {
      _prevIdx[t.preview] = _prevTracks.length;
      _prevTracks.push({url: t.preview, title: t.title, artist: t.artist || album.artist || '',
                        cover: t.cover || album.cover || ''});
    }
  });
  window._albumPreviews = _prevTracks;

  // Map each streamable track to its position in the full-album play queue (the
  // queue playAlbumStreamTrack builds — tracks with an id, in order), so clicking
  // any track plays it WITHIN the album queue → next/prev + gapless work, instead
  // of a lone single-track queue.
  const _streamIdx = {};
  if (canStream) {
    let _si = 0;
    tracks.forEach(t => { if (t.id != null) { _streamIdx[t.id] = _si++; } });
  }

  const bc = document.getElementById('detail-breadcrumb');
  if(bc) bc.textContent = `${album.artist||''} — ${album.title||''}`.replace(/^— /, '');
  const coverHtml = album.cover
    ? `<img src="${album.cover}" data-lightbox style="width:220px;height:220px;border-radius:8px;object-fit:cover;flex-shrink:0;cursor:zoom-in" onerror="this.style.display='none'"/>`
    : `<div style="width:220px;height:220px;border-radius:8px;background:rgba(255,255,255,.05);display:flex;align-items:center;justify-content:center;font-size:48px;flex-shrink:0">♪</div>`;

  const meta = [
    album.label ? `Лейбл: <span style="color:var(--text)">${esc(album.label)}</span>` : '',
    album.date ? `Релиз: <span style="color:var(--text)">${esc(album.date)}</span>` : '',
    album.genre ? `Жанр: <span style="color:var(--text)">${esc(album.genre)}</span>` : '',
    album.upc ? `UPC: <span style="color:var(--text);font-family:var(--mono);font-size:11px">${esc(album.upc)}</span>` : '',
    album.tracks ? `Треков: <span style="color:var(--text)">${album.tracks}</span>` : '',
  ].filter(Boolean).join(' · ');

  const header = `
    <div style="display:flex;gap:24px;margin-bottom:24px;flex-wrap:wrap">
      ${coverHtml}
      <div style="flex:1;min-width:280px">
        <div style="font-size:11px;color:var(--muted);letter-spacing:.8px;text-transform:uppercase;font-family:var(--display)">Альбом · ${album.service}</div>
        <div style="font-family:var(--display);font-size:28px;font-weight:800;color:var(--text);margin-top:4px;line-height:1.15">${esc(album.title||'—')}</div>
        <div style="font-size:14px;color:var(--muted2);margin-top:4px">${esc(album.artist||'')}</div>
        <div style="font-size:11px;color:var(--muted);margin-top:12px;line-height:1.7">${meta}</div>
        <div style="display:flex;gap:8px;margin-top:16px;flex-wrap:wrap">
          <button onclick="albumAddAll()" style="padding:8px 16px;background:var(--red);color:#fff;border:none;border-radius:8px;font-size:12px;font-weight:700;cursor:pointer;font-family:var(--font)">${t('btn.download_album')}</button>
          ${canStream ? `<button onclick="playAlbumAll()" style="padding:8px 16px;background:rgba(${service==='qobuz'?'24,112,245':service==='tidal'?'0,212,179':'162,56,255'},.16);color:${streamColor};border:1px solid rgba(${service==='qobuz'?'24,112,245':service==='tidal'?'0,212,179':'162,56,255'},.4);border-radius:8px;font-size:12px;font-weight:700;cursor:pointer;font-family:var(--font)">${t('btn.play_album')}</button>` : ''}
          ${album.url ? `<a href="${album.url}" target="_blank" style="padding:8px 14px;background:var(--surface);color:var(--muted);border:1px solid var(--border);border-radius:8px;font-size:12px;font-weight:600;text-decoration:none;font-family:var(--font)">↗ Открыть на ${album.service}</a>` : ''}
        </div>
      </div>
    </div>`;

  const _emptyMsg = service === 'apple'
    ? 'DJ-миксы и live-записи Apple Music не возвращают отдельные треки — скачать можно весь альбом целиком.'
    : 'Трек-лист недоступен — скачать можно весь альбом целиком.';
  // Per-track selection toolbar (checkboxes + select-all / per-disc / clear all).
  const _discsSet = [...new Set(tracks.map(t => t.disc || 1))].sort((a,b)=>(+a)-(+b));
  const _selBtnCss = 'padding:5px 11px;background:var(--surface);color:var(--muted);border:1px solid var(--border);border-radius:7px;font-size:11px;font-weight:600;cursor:pointer;font-family:var(--font)';
  const _selToolbar = tracks.length === 0 ? '' : `
    <div style="display:flex;align-items:center;gap:7px;flex-wrap:wrap;margin-bottom:10px">
      <button onclick="albumSelectAll(true)" style="${_selBtnCss}">☑ Выбрать всё</button>
      <button onclick="albumSelectAll(false)" style="${_selBtnCss}">☐ Снять все</button>
      ${_discsSet.length>1 ? _discsSet.map(d=>`<button onclick="albumSelectDisc('${d}')" style="${_selBtnCss}">💿 Диск ${d}</button>`).join('') : ''}
      <button id="alb-dl-sel" onclick="albumDownloadSelected()" disabled style="margin-left:auto;padding:6px 14px;background:var(--red);color:#fff;border:none;border-radius:7px;font-size:11px;font-weight:700;cursor:pointer;font-family:var(--font);opacity:.5">⬇ Скачать выбранное (0)</button>
    </div>`;
  const tracksList = tracks.length === 0
    ? `<div style="text-align:center;padding:40px 0;color:var(--muted)">
        <div style="font-size:28px;margin-bottom:8px">📻</div>
        <div style="margin-bottom:4px">${service === 'apple' ? 'Трек-лист не доступен в iTunes API' : 'Трек-лист недоступен'}</div>
        <div style="font-size:11px;margin-bottom:14px">${_emptyMsg}</div>
        <button onclick="albumAddAll()" style="padding:7px 16px;background:var(--red);color:#fff;border:none;border-radius:8px;font-size:12px;font-weight:700;cursor:pointer;font-family:var(--font)">${t('btn.download_album')}</button>
      </div>`
    : _selToolbar + `<div style="background:var(--surface);border:1px solid var(--border);border-radius:10px;overflow:hidden">
        ${tracks.map((t, i) => `
          <div id="alb-row-${t.id}" style="display:flex;align-items:center;gap:12px;padding:9px 14px;border-bottom:1px solid var(--border);${i===tracks.length-1?'border-bottom:none':''}" onmouseover="this.style.background='rgba(255,255,255,.03)'" onmouseout="this.style.background=''">
            <input type="checkbox" class="alb-trk-cb" data-disc="${t.disc||1}" data-url="${esc(t.url||'')}" ${t.url?'':'disabled'} onchange="_albumUpdateSelCount()" style="width:auto;margin:0;padding:0;background:none;border:none;flex-shrink:0;cursor:pointer" title="Выбрать для скачивания"/>
            <div style="width:26px;text-align:center;color:var(--muted);font-size:11px;font-family:var(--mono);flex-shrink:0">${t.track_no||i+1}</div>
            ${canStream
              ? `<button id="alb-play-${t.id}" onclick="playAlbumStreamTrack(${_streamIdx[t.id]??0})" style="width:28px;height:28px;border-radius:50%;background:rgba(${service==='qobuz'?'24,112,245':service==='tidal'?'0,212,179':'162,56,255'},.12);color:${streamColor};border:1px solid rgba(${service==='qobuz'?'24,112,245':service==='tidal'?'0,212,179':'162,56,255'},.25);cursor:pointer;font-size:10px;flex-shrink:0;transition:background .12s,color .12s;display:inline-flex;align-items:center;justify-content:center;line-height:1" title="Полный трек ▶">▶</button>`
              : t.preview ? `<button onclick="playAlbumTrackPreview(${_prevIdx[t.preview]??0})" style="width:28px;height:28px;border-radius:50%;background:rgba(255,255,255,.08);color:var(--text);border:none;cursor:pointer;font-size:10px;flex-shrink:0" title="Предпрослушка 30с">▶</button>` : `<div style="width:28px;flex-shrink:0"></div>`}
            <div style="flex:1;min-width:0">
              <div style="font-size:13px;color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis" title="${esc(t.title)}">${esc(t.title)}${t.explicit?' <span style="background:rgba(255,255,255,.15);color:var(--muted);font-size:9px;padding:0 4px;border-radius:2px;vertical-align:middle">E</span>':''}</div>
              ${t.artist && t.artist!==album.artist ? `<div style="font-size:10px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(t.artist)}</div>` : ''}
            </div>
            <div style="color:var(--muted);font-size:11px;font-family:var(--mono);flex-shrink:0">${fmtDur(t.duration)}</div>
            <button onclick="albumAddTrack('${esc(t.url||t.id)}','${esc(t.title)}','${esc(t.artist||album.artist||'')}')" style="padding:4px 10px;background:transparent;color:var(--muted);border:1px solid var(--border);border-radius:6px;font-size:10px;cursor:pointer;font-family:var(--font);flex-shrink:0" title="В очередь">⬇</button>
          </div>`).join('')}
      </div>`;

  document.getElementById('detail-content').innerHTML = header + tracksList;
}

// Update play buttons in the open album page to reflect current playback state.
// Called after track start, pause/resume, and from previewToggle in player.js.
function _syncAlbumPlayBtns() {
  const cur = Preview.queue[Preview.idx];
  const curPosKey = cur?.posKey || '';
  // `_WA` is a top-level const in player.js — accessible by name across scripts,
  // but NOT as a window property, so the old `window._WA` was always undefined.
  const _wa = (typeof _WA !== 'undefined') ? _WA : null;
  const isWA = typeof _waEnabled === 'function' && _waEnabled() && _wa?.curSource;
  const isPaused = isWA
    ? (typeof _waIsPaused === 'function' && _waIsPaused())
    : (() => { const a = document.getElementById('pp-audio'); return !a || a.paused; })();
  document.querySelectorAll('[id^="alb-play-"]').forEach(btn => {
    const tid = btn.id.replace('alb-play-', '');
    const detail = typeof Detail !== 'undefined' ? Detail.currentAlbum : null;
    if (!detail) return;
    const svc = detail.service;
    const posKey = `${svc}:${tid}`;
    const rgb = svc === 'qobuz' ? '24,112,245' : svc === 'tidal' ? '0,212,179' : '162,56,255';
    const sc  = svc === 'qobuz' ? '#1870f5'    : svc === 'tidal' ? '#00d4b3'   : '#a238ff';
    const row = document.getElementById('alb-row-' + tid);
    if (curPosKey && posKey === curPosKey) {
      btn.style.background   = isPaused ? `rgba(${rgb},.3)` : `rgba(${rgb},.88)`;
      btn.style.color        = isPaused ? sc : '#fff';
      btn.style.borderColor  = `rgba(${rgb},${isPaused ? '.55' : '.0'})`;
      btn.textContent        = isPaused ? '▶' : '⏸';
      if (row) { row.classList.add('alb-row-playing'); row.style.setProperty('--alb-svc', sc); }
    } else {
      btn.style.background  = `rgba(${rgb},.12)`;
      btn.style.color       = sc;
      btn.style.borderColor = `rgba(${rgb},.25)`;
      btn.textContent       = '▶';
      if (row) row.classList.remove('alb-row-playing');
    }
  });
}

function fmtDur(s){
  if(!s) return '—';
  s = Math.round(s);
  const m = Math.floor(s/60), sec = s%60;
  return `${m}:${sec.toString().padStart(2,'0')}`;
}

async function albumAddAll(){
  const {album} = Detail.currentAlbum;
  if(!album?.url){ toast('Нет URL альбома','var(--red)'); return; }
  const r = await api('POST', '/api/queue/add', {url: album.url, quality: resolveQuality(detectSvcFromUrl(album.url) || 'apple'), title: album.title, artist: album.artist});
  if(r.ok) toast(`+ ${album.title} → очередь`);
  else toast('Ошибка: '+(r.detail||'?'),'var(--red)');
}

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
// ── Lightbox: click-to-zoom for any cover image ─────────────────────────
// Images with ``data-lightbox`` attribute (or a ``data-lightbox-src`` pointing
// at a higher-res URL) open fullscreen on click. Esc or backdrop-click closes.
function openLightbox(src){
  const box = document.getElementById('lightbox');
  const img = document.getElementById('lightbox-img');
  if(!box || !img || !src) return;
  img.src = src;
  box.style.display = 'flex';
  // Lock body scroll while open
  document.body.style.overflow = 'hidden';
}
function closeLightbox(ev){
  // If called from a click event, only close if the click was on the backdrop
  // (not the image itself — image has stopPropagation).
  if(ev && ev.target && ev.target.id !== 'lightbox' && ev.target.tagName !== 'BUTTON') return;
  const box = document.getElementById('lightbox');
  const img = document.getElementById('lightbox-img');
  if(box) box.style.display = 'none';
  if(img) img.removeAttribute('src');
  document.body.style.overflow = '';
}

// Global delegation: any <img data-lightbox> anywhere in the UI becomes clickable.
// Prefer data-lightbox-src (high-res URL) over the img's own src — cover grids
// often show a small thumbnail but have a bigger cover available.
document.addEventListener('click', (ev) => {
  const img = ev.target.closest?.('img[data-lightbox]');
  if(!img) return;
  ev.preventDefault();
  ev.stopPropagation();
  const src = img.dataset.lightboxSrc || img.src;
  openLightbox(src);
});
// Esc closes lightbox (and only lightbox — other overlays have their own handlers)
document.addEventListener('keydown', (ev) => {
  if(ev.key === 'Escape') {
    const box = document.getElementById('lightbox');
    if(box && box.style.display !== 'none') closeLightbox();
  }
});

// Simple HTML-escape for text we insert as innerHTML rather than as attributes.
function esc(s){
  return (s==null?'':String(s))
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}

function resolveQuality(service) {
  const c = S.config || {};
  if(service === 'spotify') {
    const eng = c['spotify-engine'] || 'convert';
    if(eng === 'orpheus_spotify') return c['orpheus-quality'] || 'hifi';
    return c['quality'] || 'alac';
  }
  const svcKey = {
    deezer: 'deezer-quality', qobuz: 'qobuz-quality', tidal: 'tidal-quality',
    beatport: 'beatport-quality', yandex: 'yandex-quality', amazon: 'amazon-quality',
  };
  const k = svcKey[service];
  if (k) return c[k] || {
    deezer: 'flac', qobuz: '27', tidal: 'lossless',
    beatport: 'hifi', yandex: 'flac', amazon: 'High',
  }[service];
  return c['quality'] || 'alac';
}

async function searchAddToQueue(url, title, artist) {
  const task = { url, quality: resolveQuality(detectSvcFromUrl(url) || 'apple'), title, artist };
  const r = await api('POST', '/api/queue/add', task);
  if(r.ok) toast(`+ ${title} → очередь`);
  else toast('Ошибка: '+r.detail,'var(--red)');
}

function toggleBatch() {
  const a = document.getElementById('batch-area');
  if(a) a.style.display = a.style.display==='none' ? '' : 'none';
  // Populate batch quality select
  const bq = document.getElementById('batch-quality');
  if(bq && !bq.options.length) {
    QUALITIES.forEach(q => { const o=document.createElement('option'); o.value=q.id; o.textContent=q.label; bq.appendChild(o); });
  }
}

async function addBatch() {
  const text = document.getElementById('batch-urls')?.value||'';
  const qual = document.getElementById('batch-quality')?.value || S.config['quality'] || 'alac';
  const r = await api('POST', '/api/queue/batch', {text, quality: qual});
  if(r.ok){ toast(`+ ${r.added} ссылок в очередь`); document.getElementById('batch-urls').value=''; }
  else toast('Ошибка: '+(r.error||''),'var(--red)');
}

async function convertSpotifyFromSearch() {
  const url = document.getElementById('search-q')?.value?.trim() || prompt('Вставь Spotify URL:');
  if(!url || !url.includes('spotify.com')){ toast('Введи Spotify URL в поиске'); return; }
  const svc = document.getElementById('search-svc')?.value || 'apple';
  toast('Конвертирую Spotify…','var(--blue)');
  const r = await api('POST','/api/convert/spotify',{url, target: svc});
  if(r.ok && r.target?.url){
    toast(`Найдено: ${r.target.title||r.target.url}`,'var(--green)');
    await api('POST','/api/queue/add',{url: r.target.url, quality: resolveQuality(svc), title: r.target.title});
    toast('Добавлено в очередь!','var(--green)');
  } else {
    toast('Не найдено: '+(r.error||''),'var(--red)');
  }
}

// ══ HISTORY ══════════════════════════════════════════════════════
async function loadHistory() {
  const svc       = document.getElementById('hist-filter')?.value || '';
  const statusF   = document.getElementById('hist-status-filter')?.value || '';
  const list      = document.getElementById('history-list');
  const emp       = document.getElementById('history-empty');
  const cnt       = document.getElementById('hist-count');
  const r = await api('GET', '/api/history?limit=300' + (svc?'&service='+svc:''));
  let items = r.items || [];
  if(statusF) items = items.filter(h => (h.status || 'done') === statusF);
  if(cnt) cnt.textContent = items.length;
  if(emp) emp.style.display = items.length ? 'none' : '';
  if(!list) return;

  const SVC_COLOR = {apple:'#fc3c44', deezer:'#a238ff', qobuz:'#1b68d3', tidal:'#00d4b3', spotify:'#1db954'};
  const SVC_LABEL = {apple:'A', deezer:'D', qobuz:'Q', tidal:'T', spotify:'S'};
  const statusIcon = s => s === 'error' ? '<span style="color:var(--red);font-weight:700">✗</span>'
                        : s === 'cancelled' ? '<span style="color:var(--orange)">⏹</span>'
                        : '<span style="color:var(--green);font-weight:700">✓</span>';

  list.innerHTML = items.map(h => {
    const col = SVC_COLOR[h.service] || '#888';
    const lbl = SVC_LABEL[h.service] || '?';
    const ts  = h.ts ? new Date(h.ts).toLocaleString('ru') : '';
    const title = h.title || _titleFromUrl(h.url);
    const artist = h.artist || '';
    const tracksInfo = h.tracks > 1 ? ` · ${h.tracks} треков` : '';
    const art = h.artworkUrl ? `<img src="${h.artworkUrl}" style="width:100%;height:100%;object-fit:cover;border-radius:6px" loading="lazy"/>` : lbl;
    return `
    <div class="hist-row" style="display:flex;align-items:center;gap:12px;padding:10px 12px;background:var(--surface);border:1px solid var(--border);border-radius:10px">
      <div style="width:40px;height:40px;border-radius:7px;background:${col};color:#fff;font-size:13px;font-weight:700;display:flex;align-items:center;justify-content:center;flex-shrink:0;overflow:hidden">${art}</div>
      <div style="flex:1;min-width:0">
        <div style="display:flex;align-items:center;gap:8px;font-size:13px;font-weight:600;color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis">
          ${statusIcon(h.status || 'done')} <span style="overflow:hidden;text-overflow:ellipsis">${title}</span>
        </div>
        <div style="font-size:11px;color:var(--muted);margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">
          ${artist ? artist + ' · ' : ''}${ts} · ${(h.quality||'?').toUpperCase()}${tracksInfo}
        </div>
      </div>
      <button onclick="redownload(${esc(JSON.stringify(h.url))}, ${esc(JSON.stringify(h.quality||''))})"
        style="padding:5px 11px;background:rgba(192,132,160,.1);border:1px solid rgba(192,132,160,.2);border-radius:7px;font-size:11px;font-weight:700;color:var(--red);cursor:pointer;font-family:var(--font);white-space:nowrap;flex-shrink:0">
        ↺ Повторить
      </button>
    </div>`;
  }).join('');
}

async function redownload(url, quality) {
  const r = await api('POST','/api/queue/add',{url, quality});
  if(r.ok) toast('Добавлено в очередь!');
  else toast('Ошибка','var(--red)');
}

async function clearHistory() {
  // Period selector: "" = everything, "h:N" = older than N hours, "d:N" = older
  // than N days. Maps to the backend's DELETE /api/history?hours=&days= window.
  const sel = document.getElementById('hist-clear-period');
  const v = sel ? sel.value : '';
  let qs = '', what = t('h.clr_confirm_all') || 'всю историю';
  if (v.startsWith('h:'))      { qs = '?hours=' + v.slice(2); what = `историю старше ${v.slice(2)} ч`; }
  else if (v.startsWith('d:')) { qs = '?days='  + v.slice(2); what = `историю старше ${v.slice(2)} дн`; }
  if(!confirm('Очистить ' + what + '?')) return;
  const r = await api('DELETE','/api/history' + qs);
  loadHistory();
  const n = (r && typeof r.removed !== 'undefined') ? r.removed : '';
  toast('История очищена' + (n !== '' && n !== 'all' ? ` (${n})` : ''));
}

// ══ WATCHLIST ═════════════════════════════════════════════════════
async function loadWatchlist() {
  const r = await api('GET','/api/watchlist');
  const items = r.items||[];
  const list  = document.getElementById('wl-list');
  const emp   = document.getElementById('wl-empty');
  if(emp) emp.style.display = items.length?'none':'';
  if(!list) return;
  list.innerHTML = items.map(w => `
    <div style="display:flex;align-items:center;gap:10px;padding:9px 12px;background:var(--surface);border:1px solid var(--border);border-radius:10px;margin-bottom:7px">
      <div style="flex:1;min-width:0">
        <div style="font-size:13px;font-weight:600;color:var(--text)">${w.name||w.url}</div>
        <div style="font-size:11px;color:var(--muted);margin-top:2px">
          ${w.service||'apple'} · ${w.auto_download?'Авто-скачивание':'Только уведомление'}
          ${w.last_check?' · Проверено: '+new Date(w.last_check).toLocaleString('ru'):''}
          ${w.last_release?'<span style="color:var(--green);margin-left:6px">Новый релиз!</span>':''}
        </div>
      </div>
      <label style="display:flex;align-items:center;gap:5px;font-size:11px;color:var(--muted);cursor:pointer;white-space:nowrap">
        <input type="checkbox" ${w.auto_download?'checked':''} onchange="wlToggleAuto('${w.id}',this.checked)"/> Авто
      </label>
      <button onclick="wlRemove('${w.id}')"
        style="padding:4px 8px;background:var(--surface);border:1px solid var(--border);border-radius:6px;font-size:11px;cursor:pointer;color:var(--muted);font-family:var(--font)">
        ✕
      </button>
    </div>`).join('');
}

async function wlAdd() {
  const name = document.getElementById('wl-name')?.value?.trim();
  const url  = document.getElementById('wl-url')?.value?.trim();
  const svc  = document.getElementById('wl-svc')?.value||'apple';
  const auto = document.getElementById('wl-auto')?.checked !== false;
  if(!name && !url){ toast('Введи имя артиста'); return; }
  const r = await api('POST','/api/watchlist',{name,url,service:svc,auto_download:auto});
  if(r.ok){ toast(`+ ${name||url} → watchlist`,'var(--green)'); loadWatchlist(); document.getElementById('wl-name').value=''; document.getElementById('wl-url').value=''; }
  else toast('Ошибка: '+(r.detail||''),'var(--red)');
}

async function wlRemove(id) {
  await api('DELETE','/api/watchlist/'+id);
  loadWatchlist();
}

async function wlToggleAuto(id, val) {
  // Update via re-add (simple)
  toast(val?'Авто-скачивание включено':'Только уведомление');
}

async function wlCheckNow() {
  // The WS events (watchlist_check_*) drive the status line now.
  // A toast would be redundant.
  await api('POST','/api/watchlist/check');
}

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

// ── Service detection in URL bar ─────────────────────────────────
const SVC_COLORS = {apple:'#fc3c44',qobuz:'#1b68d3',deezer:'#a238ff',tidal:'#00d4b3',spotify:'#1db954',soundcloud:'#ff5500',beatport:'#a6ce39',yandex:'#ffcc00',amazon:'#25d1da'};
const SVC_LABELS = {
  apple:      '🍎 Apple Music',
  qobuz:      '🎼 Qobuz',
  deezer:     '🎵 Deezer',
  tidal:      '🌊 Tidal',
  spotify:    '🟢 Spotify — будет конвертирован',
  soundcloud: '🎵 SoundCloud',
  beatport:   '🟣 Beatport',
  yandex:     '🟡 Yandex Music',
  amazon:     '🅰️ Amazon Music',
};

function detectUrlService(val) {
  let svc = '';
  if(val.includes('music.apple.com'))      svc='apple';
  else if(val.includes('qobuz.com'))       svc='qobuz';
  else if(val.includes('deezer.com'))      svc='deezer';
  else if(val.includes('deezer.page'))     svc='deezer';
  else if(val.includes('tidal.com'))       svc='tidal';
  else if(val.includes('soundcloud.com'))  svc='soundcloud';
  else if(val.includes('spotify.com'))     svc='spotify';
  else if(val.includes('beatport.com'))    svc='beatport';
  else if(val.includes('music.yandex.'))   svc='yandex';
  else if(val.includes('music.amazon.'))   svc='amazon';

  const dot = document.getElementById('url-svc-dot');
  const lbl = document.getElementById('url-svc-label');
  if(dot) dot.style.background = svc ? (SVC_COLORS[svc]||'var(--muted)') : 'var(--muted)';
  if(lbl) {
    let label = SVC_LABELS[svc] || svc || (val.startsWith('http') ? '🌐 Неизвестный сервис' : '');
    if(svc === 'spotify') {
      const spEng = (S.config && S.config['spotify-engine']) || 'convert';
      if(spEng === 'orpheus_spotify') label = '🟢 Spotify → OrpheusDL';
      // else keep default "будет конвертирован"
    }
    lbl.textContent = label;
  }

  // Swap quality selector to match the pasted service
  if(svc) updateQualitySelector(svc);
}

// Service pill switcher — updates active service context (for quality list etc)
const SVC_BTN_COLS = {apple:'var(--red)',qobuz:'#1b68d3',deezer:'#a238ff',tidal:'#00d4b3',beatport:'#01f49c'};
function setActiveService(svc, btn) {
  // Style all buttons
  document.querySelectorAll('.svc-btn').forEach(b=>{
    b.style.background='transparent'; b.style.color='var(--muted)';
  });
  if(btn){ btn.style.background=SVC_BTN_COLS[svc]||'var(--red)'; btn.style.color='#fff'; }

  // Update URL bar placeholder
  const inp = document.getElementById('url-input');
  const placeholders = {
    apple:  'Вставь ссылку Apple Music — album, song, playlist, artist…',
    qobuz:  'Вставь ссылку Qobuz — album, track, playlist…',
    deezer: 'Вставь ссылку Deezer — album, track, playlist, artist…',
    tidal:  'Вставь ссылку Tidal — album, track, playlist…',
  };
  if(inp) inp.placeholder = placeholders[svc] || 'Вставь ссылку…';

  // Update dot color
  const dot = document.getElementById('url-svc-dot');
  if(dot) dot.style.background = SVC_BTN_COLS[svc]||'var(--red)';

  // For Apple: also update engine (show engine-specific quality)
  // For others: quality list from their own qualities
  S._activeSvc = svc;
  if(svc === 'apple') {
    // switch engine quality back
  }
}

function detectSpotifyInUrl(val) {
  // If user pastes a Spotify link in the main URL bar, offer to convert
  if(val && val.includes('spotify.com')) {
    toast('Spotify ссылка — перейди в Поиск → Spotify → Конвертировать', 'var(--green,#1db954)');
  }
}
// ══ SETTINGS TABS ════════════════════════════════════════════════
function showStab(id, btn) {
  document.querySelectorAll('.stab-panel').forEach(p=>p.classList.remove('active'));
  document.querySelectorAll('.stab').forEach(b=>b.classList.remove('active'));
  if(id==='global') {
    ['stab-global','stab-global-shared'].forEach(sid=>{
      const el=document.getElementById(sid); if(el) el.classList.add('active');
    });
    try { renderSvcColorGrid?.(); } catch {}
  } else {
    const el=document.getElementById('stab-'+id); if(el) el.classList.add('active');
  }
  if(btn) btn.classList.add('active');
  const vb = document.querySelector('#view-settings .view-body');
  if(vb) vb.scrollTop = 0;
  // Spotify tab → live token auto-sync log (extension pushes); stop polling elsewhere.
  try { if(id==='spotify') startSpotifyTokenPoll?.(); else stopSpotifyTokenPoll?.(); } catch {}
  // Repopulate fields of newly visible tab from S.config
  const c = S.config||{};
  if(id==='deezer')  {
    _setSecret('s-deezer-arl', c['deezer-arl']); setVal('s-deezer-qual',c['deezer-quality']||'flac'); setVal('s-deezer-path',c['deezer-save-path']||'');
    if(c['deezer-arl']) testAuth('deezer');
  }
  if(id==='qobuz')   {
    setVal('s-qobuz-userid',c['qobuz-user-id']||''); _setSecret('s-qobuz-authtok',c['qobuz-auth-token']); setVal('s-qobuz-email',c['qobuz-email']||''); _setSecret('s-qobuz-pass',c['qobuz-password']); setVal('s-qobuz-appid',c['qobuz-app-id']||''); setVal('s-qobuz-secrets',c['qobuz-secrets']||''); setVal('s-qobuz-qual',c['qobuz-quality']||'7'); setVal('s-qobuz-path',c['qobuz-save-path']||'');
    if(c['qobuz-auth-token'] || (c['qobuz-email'] && c['qobuz-password'])) testAuth('qobuz');
  }
  if(id==='tidal')   {
    _setSecret('s-tidal-token',c['tidal-token']); _setSecret('s-tidal-refresh',c['tidal-refresh']); setVal('s-tidal-userid',c['tidal-user-id']||''); setVal('s-tidal-country',c['tidal-country']||'US'); setVal('s-tidal-expiry',c['tidal-token-expiry']||''); setVal('s-tidal-qual',c['tidal-quality']||'lossless'); setVal('s-tidal-path',c['tidal-save-path']||'');
    if(c['tidal-token']) testAuth('tidal');
    loadTokenExpiry('tidal');
  }
  if(id==='spotify') { setVal('s-sp-cid',c['spotify-client-id']||''); setVal('s-sp-csecret',c['spotify-client-secret']||''); _setSecret('s-sp-dc',c['spotify-sp-dc']); setVal('s-sp-days',c['spotify-release-days']||30); setVal('s-sp-types',c['spotify-release-types']||'album,single'); setChk('s-sp-auto',c['spotify-auto-convert']!==false); setVal('s-orp-path',c['orpheus-save-path']||''); setChk('s-orp-mp3',c['orpheus-convert-mp3']===true); setVal('s-orp-quality',c['orpheus-quality']||'hifi'); _renderSpotifySavedTarget(); loadSpotifyStatus(); loadOrpheusStatus(); testAuth('spotify'); }
  if(id==='apple')   {
    updateEngineUI(c['engine']||'zhaarey');
    setVal('s-wrapper-email',c['wrapper-apple-id']||'');
    setVal('s-wrapper-pass',c['wrapper-password']||'');
    const wm = c['wrapper-mode'] || 'docker-remote';
    const radio = document.querySelector(`input[name="wrapper-mode"][value="${wm}"]`);
    if(radio) radio.checked = true;
    _applyWrapperModeUI(wm, null);
    if((c['engine']||'zhaarey')==='amd') checkAMDWrapperStatus();
    refreshAppleAuthStatus();
    loadWrapperSessionStatus();
    if(c['media-user-token'] && c['authorization-token']) testAuth('apple');
  }
  if(id==='soundcloud') { setVal('s-sc-path', c['soundcloud-save-path']||''); setVal('s-sc-oauth', c['soundcloud-oauth-token']||''); setVal('s-sc-wvd-wrapper', c['sc-widevine-wrapper-url']||''); setChk('s-sc-isrc-fallback', !!c['sc-isrc-fallback']); if(c['soundcloud-oauth-token']) testAuth('soundcloud'); scEngineCheck(); try { _scCheckWvd?.(); _scCheckWvdWrapper?.(); } catch {} }
  if(id==='beatport') {
    setVal('s-bp-user', c['beatport-username']||'');
    setVal('s-bp-pass', c['beatport-password']||'');
    setVal('s-bp-path', c['beatport-save-path']||'');
    const bpq = document.getElementById('s-bp-quality');
    if(bpq) bpq.value = c['beatport-quality']||'hifi';
    loadBeatportStatus();
    if(c['beatport-username'] && c['beatport-password']) testAuth('beatport');
  }
  if(id==='yandex') {
    _setSecret('s-yandex-token', c['yandex-token']);
    setVal('s-yandex-qual', c['yandex-quality']||'flac');
    setVal('s-yandex-path', c['yandex-save-path']||'');
  }
  if(id==='amazon') {
    _setSecret('s-amazon-token', c['amazon-token']);
    setVal('s-amazon-qual', c['amazon-quality']||'High');
    setVal('s-amazon-path', c['amazon-save-path']||'');
  }
  if(id==='admin') {
    loadAdminLinks();
    loadRemoteStatus();
    loadTunnelStatus();
  }
  if(id==='bot') loadBotConfig();
  if(id==='global')  {
    loadAuthStatus();
    setChk('s-queue-autostart', c['queue-autostart']!==false);
    const mp = +(c['max-parallel'] || 1);
    const sl = document.getElementById('s-max-parallel');
    const vl = document.getElementById('s-max-parallel-val');
    if(sl) sl.value = mp;
    if(vl) vl.textContent = mp;
  }
}

// ══ BOT CONFIG TAB ════════════════════════════════════════════════════
// Reads/writes tgbot/config.json via /api/admin/bot-config so the bot token,
// owner id, local Bot API, cache channel etc. are edited from the UI instead of
// hand-editing the file (TASKS #12). Secrets (bot_token/api_hash) are never sent
// back from the server — only a "set" flag — and a blank secret field leaves the
// stored value untouched.
async function loadBotConfig(){
  const setSet = (elId, on) => { const e=document.getElementById(elId); if(e){ e.textContent = on ? '✓ задан' : '— не задан'; e.style.color = on ? 'var(--green)' : 'var(--muted)'; } };
  try {
    const c = await api('GET','/api/admin/bot-config');
    setVal('b-owner-id',        c['owner_id'] ?? '');
    setVal('b-api-id',          c['api_id'] ?? '');
    setVal('b-local-api',       c['local_bot_api'] ?? '');
    setVal('b-max-upload',      c['max_upload_mb'] ?? '');
    setVal('b-cache-id',        c['cache_channel_id'] ?? '');
    setVal('b-cache-link',      c['cache_channel_link'] ?? '');
    setVal('b-backend-url',     c['backend_url'] ?? '');
    setVal('b-default-quality', c['default_quality'] ?? '');
    // Secrets: never populate the input, just show whether one is stored.
    const tok=document.getElementById('b-bot-token'); if(tok) tok.value='';
    const ah =document.getElementById('b-api-hash');  if(ah)  ah.value='';
    setSet('bot-token-set',   c['bot_token_set']);
    setSet('bot-apihash-set', c['api_hash_set']);
  } catch(e) {
    const s=document.getElementById('bot-config-status');
    if(s){ s.textContent=t('err.generic')+': '+e.message; s.style.color='var(--red)'; }
  }
}

async function saveBotConfig(){
  const s = document.getElementById('bot-config-status');
  const v = id => (document.getElementById(id)?.value ?? '').trim();
  // Non-secret fields are pre-filled by loadBotConfig, so they carry the current
  // value unless the user changed them. Blank secrets are skipped server-side.
  const body = {
    bot_token:          v('b-bot-token'),
    owner_id:           v('b-owner-id'),
    api_id:             v('b-api-id'),
    api_hash:           v('b-api-hash'),
    local_bot_api:      v('b-local-api'),
    max_upload_mb:      v('b-max-upload'),
    cache_channel_id:   v('b-cache-id'),
    cache_channel_link: v('b-cache-link'),
    backend_url:        v('b-backend-url'),
    default_quality:    v('b-default-quality'),
  };
  if(s){ s.textContent='…'; s.style.color='var(--muted)'; }
  try {
    const r = await api('POST','/api/admin/bot-config', body);
    const n = (r.changed||[]).length;
    let msg = n ? `✓ Сохранено (${n})` : '✓ Без изменений';
    if(r.restart_required) msg += ' — нужен рестарт бота';
    if(s){ s.textContent=msg; s.style.color = r.restart_required ? 'var(--orange)' : 'var(--green)'; }
    try { toast(msg, r.restart_required ? 'var(--orange)' : 'var(--green)'); } catch {}
    loadBotConfig();
  } catch(e) {
    if(s){ s.textContent=t('err.generic')+': '+e.message; s.style.color='var(--red)'; }
    try { toast('✗ '+e.message, 'var(--red)'); } catch {}
  }
}

// ══ APP AUTH ══════════════════════════════════════════════════════
async function loadAuthStatus(){
  const st = document.getElementById('sec-status');
  const curWrap = document.getElementById('sec-current-wrap');
  const logoutBtn = document.getElementById('sec-logout-btn');
  try {
    const r = await fetch('/api/auth-status');
    const d = await r.json();
    if(d.enabled) {
      if(st) { st.innerHTML = '🔒 Защита <strong style="color:var(--green)">включена</strong>'; st.style.background = 'rgba(62,207,170,.08)'; }
      if(curWrap) curWrap.style.display = '';
      if(logoutBtn) logoutBtn.style.display = '';
    } else {
      if(st) { st.innerHTML = '🔓 Защита <strong style="color:var(--muted)">отключена</strong> — доступ только с этого компьютера'; st.style.background = 'rgba(255,255,255,.04)'; }
      if(curWrap) curWrap.style.display = 'none';
      if(logoutBtn) logoutBtn.style.display = 'none';
    }
  } catch(e) {
    if(st) st.textContent = 'Ошибка: '+e.message;
  }
}

async function saveAppPassword(){
  const newPw = document.getElementById('sec-new')?.value || '';
  const curPw = document.getElementById('sec-current')?.value || '';
  const msgEl = document.getElementById('sec-msg');
  if(msgEl) { msgEl.textContent = 'Сохраняю…'; msgEl.style.color = 'var(--muted)'; }
  try {
    const r = await fetch('/api/set-password', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({password: newPw, current: curPw}),
    });
    const d = await r.json().catch(()=>({}));
    if(!r.ok) {
      if(msgEl) { msgEl.textContent = d.detail || 'Ошибка: '+r.status; msgEl.style.color = 'var(--red)'; }
      if(r.status === 401) {
        const curWrap = document.getElementById('sec-current-wrap');
        if(curWrap) curWrap.style.display = '';
      }
      return;
    }
    if(msgEl) { msgEl.textContent = d.auth_enabled ? '✓ Пароль установлен. Если открыт с другого устройства — потребуется вход.' : '✓ Защита отключена.'; msgEl.style.color = 'var(--green)'; }
    document.getElementById('sec-new').value = '';
    document.getElementById('sec-current').value = '';
    loadAuthStatus();
  } catch(e) {
    if(msgEl) { msgEl.textContent = 'Ошибка сети: '+e.message; msgEl.style.color = 'var(--red)'; }
  }
}

async function logoutApp(){
  await fetch('/api/logout', {method:'POST'}).catch(()=>{});
  location.href = '/login';
}

// country code → flag emoji
function _countryFlag(code) {
  if(!code || code.length !== 2) return '';
  const c = code.toUpperCase();
  return String.fromCodePoint(0x1F1E6+c.charCodeAt(0)-65, 0x1F1E6+c.charCodeAt(1)-65);
}

// ── Yandex Music token — automated OAuth device flow ──────────────────────
// Backend gets a short device code from Yandex, we show it + open ya.ru/device,
// then poll until the user confirms; the token is saved to config server-side.
async function yandexGetToken() {
  const hint = document.getElementById('yandex-token-hint');
  const set = (h) => { if (hint) hint.innerHTML = h; };
  set(t('ya.requesting'));
  const r = await api('POST', '/api/yandex/auth/start', {});
  if (!r || !r.ok) { set(`<span style="color:var(--red)">${t('dlg.err')}: ${esc((r && r.error) || '?')}</span>`); return; }
  try { window.open(r.verification_url, '_blank', 'noopener'); } catch (e) {}
  set(ti('dlg.enter_code', {url: esc(r.verification_url)})
    + `<b style="font-size:15px;letter-spacing:2px;color:#ffcc00">${esc(r.user_code)}</b>`
    + ` <span style="opacity:.7">${t('dlg.waiting')}</span>`);
  const deadline = Date.now() + (r.expires_in || 300) * 1000;
  const iv = Math.max(3, (r.interval || 5)) * 1000;
  const poll = async () => {
    if (Date.now() > deadline) { set(`<span style="color:var(--red)">${t('ya.code_expired')}</span>`); return; }
    const p = await api('POST', '/api/yandex/auth/poll', { device_code: r.device_code });
    if (p && p.ok && p.saved) {
      set(`<span style="color:var(--green)">${t('ya.token_saved')}</span>`);
      if (S.config) S.config['yandex-token'] = '••••••';
      _setSecret('s-yandex-token', '••••••');
      return;
    }
    if (p && p.ok && p.pending) { setTimeout(poll, iv); return; }
    set(`<span style="color:var(--red)">${t('dlg.err')}: ${esc((p && p.error) || '?')}</span>`);
  };
  setTimeout(poll, iv);
}

// ── Tidal TV device-flow login (link.tidal.com) ───────────────────────────
// Tidal has NO password API — only the TV device-code flow. Backend fetches a
// device+user code, we show it + open link.tidal.com, then poll until the user
// confirms; the session is saved server-side into OrpheusDL's loginstorage.bin.
// Works on a fresh clone (built-in TV creds) → Tidal authenticates out of the box.
async function tidalTvLogin() {
  const hint = document.getElementById('tidal-tv-hint');
  const btn = document.getElementById('tidal-tv-btn');
  const set = (h) => { if (hint) hint.innerHTML = h; };
  if (btn) btn.disabled = true;
  set(t('td.requesting'));
  const r = await api('POST', '/api/tidal/auth/start', {});
  if (!r || !r.ok) { set(`<span style="color:var(--red)">${t('dlg.err')}: ${esc((r && r.error) || '?')}</span>`); if (btn) btn.disabled = false; return; }
  try { window.open(r.verification_url, '_blank', 'noopener'); } catch (e) {}
  set(ti('dlg.enter_code', {url: esc(r.verification_url)})
    + `<b style="font-size:15px;letter-spacing:2px;color:#00d4b3">${esc(r.user_code)}</b>`
    + ` <span style="opacity:.7">${t('dlg.waiting')}</span>`);
  const deadline = Date.now() + (r.expires_in || 300) * 1000;
  const iv = Math.max(2, (r.interval || 2)) * 1000;
  const poll = async () => {
    if (Date.now() > deadline) { set(`<span style="color:var(--red)">${t('td.code_expired')}</span>`); if (btn) btn.disabled = false; return; }
    const p = await api('POST', '/api/tidal/auth/poll', { device_code: r.device_code });
    if (p && p.ok && p.saved) {
      set(`<span style="color:var(--green)">${t('td.signed_in_pre')}${p.country ? ' (' + esc(p.country) + ')' : ''}${t('td.signed_in_post')}</span>`);
      if (btn) btn.disabled = false;
      return;
    }
    if (p && p.ok && p.pending) { setTimeout(poll, iv); return; }
    set(`<span style="color:var(--red)">${t('dlg.err')}: ${esc((p && p.error) || '?')}</span>`);
    if (btn) btn.disabled = false;
  };
  setTimeout(poll, iv);
}

// ── Spotify out-of-box OGG login (librespot PKCE OAuth) ────────────────────
// Backend runs librespot's browser OAuth flow which saves the durable blob
// (reusable_credentials.json) the keeper + orpheus use. One click, no extension,
// no token paste → Spotify downloads work on a fresh clone. Separate from the
// dev-API "Подключить" (release radar) above.
async function spotifyOggLogin() {
  const hint = document.getElementById('sp-ogg-hint');
  const btn = document.getElementById('sp-ogg-btn');
  const set = (h) => { if (hint) hint.innerHTML = h; };
  if (btn) btn.disabled = true;
  set(t('sp.ogg_starting'));
  const r = await api('POST', '/api/spotify/auth/start', {});
  if (!r || !r.ok) { set(`<span style="color:var(--red)">${t('dlg.err')}: ${esc((r && r.error) || '?')}</span>`); if (btn) btn.disabled = false; return; }
  try { window.open(r.auth_url, '_blank', 'noopener'); } catch (e) {}
  set(t('sp.ogg_page_opened'));
  const deadline = Date.now() + 180 * 1000;
  const poll = async () => {
    if (Date.now() > deadline) { set(`<span style="color:var(--red)">${t('sp.ogg_timeout')}</span>`); if (btn) btn.disabled = false; return; }
    const p = await api('POST', '/api/spotify/auth/status', {});
    if (p && p.ok && p.done) { set(`<span style="color:var(--green)">${t('sp.ogg_signed_in')}</span>`); if (btn) btn.disabled = false; return; }
    if (p && p.ok && p.pending) { setTimeout(poll, 2500); return; }
    set(`<span style="color:var(--red)">${t('dlg.err')}: ${esc((p && p.error) || '?')}</span>`); if (btn) btn.disabled = false;
  };
  setTimeout(poll, 3000);
}

// Alternative: browser (implicit) OAuth — opens Yandex login; after sign-in the
// page redirects to music.yandex.ru with the token in the URL fragment. The user
// copies the whole URL into the paste box; yandexSaveToken extracts the token.
function yandexBrowserToken() {
  const url = 'https://oauth.yandex.ru/authorize?response_type=token'
            + '&client_id=23cabbbdc6cd418abb4b39c32c41195d';
  try { window.open(url, '_blank', 'noopener'); } catch (e) {}
  const hint = document.getElementById('yandex-token-hint');
  if (hint) hint.innerHTML = t('ya.browser_hint');
}

// Accept a pasted redirect URL / fragment / raw token → extract access_token,
// save it server-side and reflect it in the masked token field.
function yandexSaveToken(raw) {
  raw = (raw || '').trim();
  if (!raw) return;
  let tok = '';
  const m = raw.match(/access_token=([^&\s#]+)/i);
  if (m) tok = decodeURIComponent(m[1]);
  else if (!/[\s/]/.test(raw)) tok = raw;   // looks like a bare token
  if (!tok || tok.length < 12) return;      // wait for a full paste
  saveSetting('yandex-token', tok);
  _setSecret('s-yandex-token', '••••••');
  if (S.config) S.config['yandex-token'] = '••••••';
  const hint = document.getElementById('yandex-token-hint');
  if (hint) hint.innerHTML = `<span style="color:var(--green)">${t('ya.token_accepted')}</span>`;
  const box = document.getElementById('s-yandex-paste');
  if (box) box.value = '';
}

// ── Guest service info — what Ripster is + which services are available ───────
async function showGuestServiceInfo() {
  let status = {};
  try { status = await (await fetch('/api/services/status')).json(); } catch(e){}
  const names = {apple:'Apple Music',qobuz:'Qobuz',deezer:'Deezer',tidal:'Tidal',
                 spotify:'Spotify',soundcloud:'SoundCloud',beatport:'Beatport',yandex:'Яндекс.Музыка'};
  const avail = Object.keys(names).filter(k => status[k]);
  const q = ((S.config && S.config.quality) || '—');
  const badges = avail.map(k => `<span style="font-size:11px;padding:3px 9px;border-radius:20px;background:${_svcColor(k)}22;color:${_svcColor(k)};border:1px solid ${_svcColor(k)}55">${esc(names[k])}</span>`).join(' ');
  const ov = document.createElement('div');
  ov.id = 'guest-info-overlay';
  ov.style.cssText = 'position:fixed;inset:0;z-index:99999;background:rgba(0,0,0,.6);display:flex;align-items:center;justify-content:center;padding:20px';
  ov.onclick = () => ov.remove();
  ov.innerHTML = `<div onclick="event.stopPropagation()" style="max-width:420px;width:100%;background:var(--surface,#15151a);border:1px solid var(--border);border-radius:14px;padding:22px 24px;box-shadow:0 20px 60px rgba(0,0,0,.55)">
      <div style="font-family:var(--display);font-size:20px;font-weight:800;margin-bottom:10px">Raccoon <span style="color:var(--red)">Ripster</span></div>
      <div style="font-size:13px;color:var(--muted);line-height:1.7;margin-bottom:14px">Загрузчик музыки в высоком качестве (вплоть до Hi-Res FLAC и Dolby Atmos). Ищи треки и альбомы, ставь в очередь, скачивай — готовое доступно прямо во встроенном плеере.</div>
      <div style="font-size:11px;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:.06em;margin-bottom:8px">Доступные сервисы</div>
      <div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:14px">${badges || '<span style="color:var(--muted)">—</span>'}</div>
      <div style="font-size:12px;color:var(--muted)">Качество по умолчанию: <span style="color:var(--text);font-weight:600">${esc(String(q).toUpperCase())}</span></div>
      <button onclick="document.getElementById('guest-info-overlay').remove()" style="margin-top:16px;width:100%;padding:9px;background:var(--red);color:#fff;border:none;border-radius:8px;font-size:13px;font-weight:700;cursor:pointer;font-family:var(--font)">Понятно</button>
    </div>`;
  document.body.appendChild(ov);
}

// ── Token probe: show live auth status for Qobuz/Tidal/Deezer ─────────────
async function testAuth(service){
  const out = document.getElementById('test-auth-' + service);
  if(out){ out.textContent = t('s.checking'); out.style.color = 'var(--muted)'; }
  try {
    const r = await fetch('/api/test-auth/' + service, {method: 'POST'});
    const d = await r.json().catch(()=>({ok:false,error:t('s.bad_response')}));
    if(d.ok) {
      const u = d.user || {};
      const parts = [];
      if(u.country) parts.push(`<span style="font-size:9px;font-weight:700;padding:1px 5px;border-radius:3px;background:rgba(255,255,255,.08);color:var(--muted);letter-spacing:.5px;vertical-align:middle">${esc(u.country)}</span>`);
      if(u.hires === true)         parts.push('<span style="color:#ffd60a;font-weight:700">Hi-Res ✓</span>');
      else if(u.lossless === true) parts.push('<span style="color:var(--green);font-weight:700">Lossless ✓</span>');
      else if(u.hq === true)       parts.push('<span style="color:var(--orange);font-weight:700">HQ ✓</span>');
      if(u.expiry) parts.push(`<span style="color:var(--muted)">до ${esc(u.expiry)}</span>`);
      if(u.sub_offer) parts.push(`<span style="color:var(--muted)">${esc(u.sub_offer)}</span>`);
      if(u.sub_end){
        const dl = u.sub_days_left;
        const col = u.sub_expired ? 'var(--red)' : (dl!=null && dl<=14) ? 'var(--orange)' : 'var(--green)';
        const left = (dl!=null) ? (u.sub_expired ? ' (истекла)' : ` · ${dl} дн.`) : '';
        parts.push(`<span style="color:${col};font-weight:600">подписка до ${esc(u.sub_end)}${left}</span>`);
      }
      if(u.note) parts.push(`<span style="color:var(--muted)">${esc(u.note)}</span>`);
      const tag = parts.length ? ' &nbsp;' + parts.join(' · ') : '';
      const _lbl = (typeof _svcLabel === 'function' ? _svcLabel(service) : service);
      if(out){ out.innerHTML = `<span style="color:var(--green);font-weight:700">✓ ${esc(_lbl)} ${t('s.works')}</span>` + tag; }
    } else {
      if(out){ out.innerHTML = '<span style="color:var(--red)">✗ ' + esc(d.error || t('s.unknown_error')) + '</span>'; }
    }
  } catch(e) {
    if(out){ out.innerHTML = '<span style="color:var(--red)">✗ ' + t('s.net_error') + ': ' + esc(e.message) + '</span>'; }
  }
}

// Validate every configured service token at startup so they "take" and show
// their status without the user opening each settings tab. Staggered so we
// don't hammer the backend; testAuth() no-ops visually if a tab isn't rendered
// but the server-side probe (token refresh / promotion) still runs.
let _autoValidated = false;
function autoValidateServices() {
  if(_autoValidated) return;            // once per session
  _autoValidated = true;
  const c = S.config || {};
  const jobs = [];
  if(c['deezer-arl'])                                        jobs.push('deezer');
  if(c['qobuz-auth-token'] || (c['qobuz-email'] && c['qobuz-password'])) jobs.push('qobuz');
  if(c['tidal-token'])                                       jobs.push('tidal');
  if(c['media-user-token'] && c['authorization-token'])      jobs.push('apple');
  if(c['soundcloud-oauth-token'])                            jobs.push('soundcloud');
  if(c['beatport-username'] && c['beatport-password'])       jobs.push('beatport');
  if(c['spotify-client-id'] || c['spotify-sp-dc'])           jobs.push('spotify');
  if(c['yandex-token'])                                      jobs.push('yandex');
  jobs.forEach((svc, i) => setTimeout(() => { try { testAuth(svc); } catch {} }, i * 600));
}

async function qobuzPasswordLogin() {
  const out   = document.getElementById('qobuz-pw-status');
  const email = (document.getElementById('s-qobuz-email')?.value || '').trim();
  const pw    = document.getElementById('s-qobuz-pass')?.value || '';
  if (!email || !pw) {
    if (out) out.innerHTML = '<span style="color:var(--danger)">Заполни email и пароль</span>';
    return;
  }
  if (out) { out.textContent = 'Вход в Qobuz…'; out.style.color = 'var(--muted)'; }
  // Persist creds first — the server-side probe reads them from config.
  await saveSetting('qobuz-email', email);
  await saveSetting('qobuz-password', pw);
  try {
    const r = await fetch('/api/test-auth/qobuz', {method:'POST'});
    const d = await r.json().catch(() => ({ok:false, error:'Неверный ответ сервера'}));
    if (d.ok) {
      const u = d.user || {};
      const tier = u.hires ? 'Hi-Res' : (u.lossless ? 'Lossless' : (u.subscription || ''));
      if (out) out.innerHTML = '<span style="color:var(--green)">✓ Вход выполнен'
        + (tier ? ' · ' + esc(tier) : '') + ' — user-id и токен сохранены</span>';
      loadTokensToUI();   // probe auto-promoted email/pass → token; refresh fields
    } else {
      if (out) out.innerHTML = '<span style="color:var(--danger)">✗ ' + esc(d.error || 'Ошибка') + '</span>';
    }
  } catch(e) {
    if (out) out.innerHTML = '<span style="color:var(--danger)">✗ Сеть: ' + esc(e.message) + '</span>';
  }
}

async function saveServiceTab(service) {
  const g  = id => { const el = document.getElementById(id); return el ? el.value : ''; };
  // Only include a secret field if the user actually typed something into it
  // (non-empty value = new input; empty = placeholder state = keep existing).
  const gs = id => { const v = g(id); return v || undefined; };
  let cfg = {};
  if(service === 'qobuz') {
    cfg = {'qobuz-user-id':g('s-qobuz-userid'),'qobuz-email':g('s-qobuz-email'),
           'qobuz-app-id':g('s-qobuz-appid'),'qobuz-secrets':g('s-qobuz-secrets'),
           'qobuz-quality':g('s-qobuz-qual')};
    const tok = gs('s-qobuz-authtok'); if(tok !== undefined) cfg['qobuz-auth-token'] = tok;
    const pw  = gs('s-qobuz-pass');   if(pw  !== undefined) cfg['qobuz-password']    = pw;
  } else if(service === 'deezer') {
    cfg = {'deezer-quality':g('s-deezer-qual')};
    const arl = gs('s-deezer-arl'); if(arl !== undefined) cfg['deezer-arl'] = arl;
  } else if(service === 'tidal') {
    cfg = {'tidal-user-id':g('s-tidal-userid'),'tidal-country':g('s-tidal-country'),
           'tidal-token-expiry':g('s-tidal-expiry'),'tidal-quality':g('s-tidal-qual')};
    const tok = gs('s-tidal-token');   if(tok !== undefined) cfg['tidal-token']   = tok;
    const ref = gs('s-tidal-refresh'); if(ref !== undefined) cfg['tidal-refresh'] = ref;
  } else if(service === 'soundcloud') {
    cfg = {};
    const tok = gs('s-sc-oauth'); if(tok !== undefined) cfg['soundcloud-oauth-token'] = tok;
  } else if(service === 'beatport') {
    cfg = {'beatport-quality':g('s-bp-quality')};
    const user = gs('s-bp-user'); if(user !== undefined) cfg['beatport-username'] = user;
    const pw   = gs('s-bp-pass'); if(pw   !== undefined) cfg['beatport-password']  = pw;
  } else if(service === 'apple') {
    // Only the keys that have visible inputs in the Apple panel — never wipe
    // sibling keys (storefront, amd-instance-url, cookies path) the user set
    // elsewhere. Empty token fields are NOT persisted (keeps existing value).
    cfg = {};
    const sf  = g('s-apple-storefront');   if(sf)  cfg['storefront'] = sf;
    const mut = gs('s-apple-mut');         if(mut !== undefined) cfg['media-user-token'] = mut;
    const bea = gs('s-apple-bearer');      if(bea !== undefined) cfg['authorization-token'] = bea;
    const cp  = g('s-gamdl-cookies-path'); if(cp) cfg['gamdl-cookies-path'] = cp;
    const ai  = g('s-amd-instance');       if(ai) cfg['amd-instance-url'] = ai;
  } else if(service === 'spotify') {
    cfg = {};
    const cid = g('s-sp-cid');   if(cid) cfg['spotify-client-id'] = cid;
    const sec = gs('s-sp-csecret'); if(sec !== undefined) cfg['spotify-client-secret'] = sec;
    const dc  = gs('s-sp-dc');   if(dc  !== undefined) cfg['spotify-sp-dc'] = dc;
    const dy  = g('s-sp-days');  if(dy)  cfg['spotify-release-days'] = parseInt(dy) || 7;
    const tp  = g('s-sp-types'); if(tp)  cfg['spotify-release-types'] = tp;
    const oq  = g('s-orp-quality'); if(oq) cfg['orpheus-quality'] = oq;
  } else if(service === 'yandex') {
    cfg = {'yandex-quality':g('s-yandex-qual')};
    const tok = gs('s-yandex-token'); if(tok !== undefined && !tok.startsWith('••')) cfg['yandex-token'] = tok;
  }
  Object.assign(S.config, cfg);
  try { await api('POST','/api/config',cfg); toast('✓ Сохранено','var(--green)'); }
  catch(e) { toast('✗ '+e.message,'var(--red)'); }
}

async function saveAndTestAuth(service) {
  await saveServiceTab(service);
  await testAuth(service);
}

// ── Tidal: import token.json from tidal-dl-ng / tidalapi ──────────────────
function toggleTidalImport(){
  const p = document.getElementById('tidal-import-panel');
  if(!p) return;
  const showing = p.style.display !== 'none';
  p.style.display = showing ? 'none' : '';
  if(!showing) {
    // Focus textarea when opening for quick paste
    setTimeout(() => document.getElementById('tidal-import-json')?.focus(), 50);
  }
}

async function importTidalToken(){
  const ta  = document.getElementById('tidal-import-json');
  const msg = document.getElementById('tidal-import-msg');
  const raw = (ta?.value || '').trim();
  if(!raw) {
    if(msg){ msg.innerHTML = '<span style="color:var(--red)">Пустое поле</span>'; }
    return;
  }
  if(msg){ msg.innerHTML = '<span style="color:var(--muted)">Импортирую…</span>'; }
  try {
    const r = await fetch('/api/import-token/tidal', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({content: raw}),
    });
    const d = await r.json().catch(()=>({}));
    if(!r.ok) {
      if(msg){ msg.innerHTML = '<span style="color:var(--red)">✗ ' + esc(d.detail || ('HTTP ' + r.status)) + '</span>'; }
      return;
    }
    const imp = d.imported || {};
    // Update S.config so UI fields reflect the new state
    if(S && S.config) {
      S.config['tidal-token']         = '••••••••';   // masked on server; show mask locally
      S.config['tidal-refresh']       = '••••••••';
      S.config['tidal-user-id']       = imp.user_id || '';
      S.config['tidal-country']       = imp.country || '';
      S.config['tidal-token-expiry']  = imp.expiry_unix || '';
    }
    // Re-populate the Tidal tab fields
    showStab('tidal', document.querySelector('.stab[data-stab="tidal"]'));
    if(msg){
      const parts = [];
      if(imp.user_id) parts.push('user_id=' + esc(imp.user_id));
      if(imp.country) parts.push('country=' + esc(imp.country));
      msg.innerHTML = '<span style="color:var(--green)">✓ Импортировано</span>' + (parts.length ? ' · ' + parts.join(' · ') : '');
    }
    // Auto-test after 500ms so user sees it works
    setTimeout(() => testAuth('tidal'), 500);
    // Clear textarea
    if(ta) ta.value = '';
  } catch(e) {
    if(msg){ msg.innerHTML = '<span style="color:var(--red)">✗ ' + esc(e.message) + '</span>'; }
  }
}

// ══ SPOTIFY STATUS ════════════════════════════════════════════════

// Show / hide "saved conversion target" row + reset handler.
function _renderSpotifySavedTarget() {
  const row     = document.getElementById('sp-saved-target-row');
  const nameEl  = document.getElementById('sp-saved-target-name');
  const target  = (S.config && S.config['spotify-default-target']) || '';
  if(!row) return;
  if(target && ['apple','deezer','qobuz'].includes(target)) {
    row.style.display = '';
    if(nameEl) {
      nameEl.textContent = _svcLabel(target);
      nameEl.style.color = _svcColor(target);
    }
  } else {
    row.style.display = 'none';
  }
}

async function resetSpotifyDefaultTarget() {
  try {
    await api('POST','/api/config', { 'spotify-default-target': '' });
    if(S.config) S.config['spotify-default-target'] = '';
    _renderSpotifySavedTarget();
    toast('Выбор сброшен — будет спрашивать снова', 'var(--muted)');
  } catch(e) {
    toast('Не удалось сбросить: '+e.message, 'var(--red)');
  }
}

async function refreshAppleAuthStatus() {
  try {
    const r = await api('GET', '/api/apple/auth-status');
    const el      = document.getElementById('apple-auth-status-text');
    const loginBtn = document.getElementById('btn-apple-login');
    if (!el) return;
    if (r.mut_set) {
      el.textContent = ti('as.token_set', {n: r.mut_length});
      el.style.color = 'var(--green)';
      if(loginBtn) loginBtn.style.display = 'none';
      const inp = document.getElementById('t-mut');
      if (inp && !inp.value && S.config && S.config['media-user-token']) {
        inp.value = S.config['media-user-token'];
      }
    } else {
      el.textContent = t('as.token_not_set');
      el.style.color = 'var(--muted)';
      if(loginBtn) loginBtn.style.display = '';
    }
  } catch(e) {
    const el = document.getElementById('apple-auth-status-text');
    if (el) el.textContent = '';
  }
}

async function loadSpotifyStatus() {
  const r = await api('GET','/api/spotify/status');
  S._spStatus = r;  // cache for _syncReleasesSettingsTab
  const nameEl  = document.getElementById('sp-name');
  const emailEl = document.getElementById('sp-email');
  const logBtn  = document.getElementById('sp-logout-btn');
  const relBtn  = document.getElementById('rel-sp-btn');
  if(r.connected) {
    if(nameEl)  nameEl.textContent  = r.sp_dc_mode ? 'sp_dc (cookie)' : (r.display_name || 'Spotify');
    if(emailEl) emailEl.textContent = r.sp_dc_mode ? t('sp.auth_spdc') : (r.email || '');
    if(logBtn)  logBtn.style.display = r.sp_dc_mode ? 'none' : '';
    if(relBtn)  relBtn.style.display = 'none';
  } else {
    const err403   = r.error && r.error.toLowerCase().includes('not registered');
    const errSpDc  = r.sp_dc_expired;
    if(nameEl)  nameEl.textContent = errSpDc ? t('sp.spdc_expired')
      : err403 ? t('sp.not_registered') : t('sp.not_connected');
    if(emailEl) emailEl.textContent = errSpDc
      ? t('sp.spdc_refresh_hint')
      : err403 ? t('sp.not_registered_hint')
      : '';
    if(logBtn)  logBtn.style.display = 'none';
    if(relBtn)  relBtn.style.display = '';
  }
}

async function spotifyLogout() {
  await api('POST','/api/spotify/logout');
  loadSpotifyStatus();
  toast(t('sp.disconnected'));
}

async function autoExtractSpDc() {
  const btn    = document.getElementById('sp-dc-auto-btn');
  const status = document.getElementById('sp-dc-auto-status');
  const input  = document.getElementById('s-sp-dc');
  if(btn) { btn.disabled = true; btn.textContent = t('sp.searching_btn'); }
  if(status) { status.textContent = t('sp.searching'); status.style.color = 'var(--muted)'; }
  try {
    const r = await api('GET', '/api/spotify/extract-sp-dc');
    if(r.ok && r.value) {
      if(input) input.value = r.value;
      await api('POST', '/api/config', {'spotify-sp-dc': r.value});
      if(S.config) S.config['spotify-sp-dc'] = r.value;
      const _where = r.browser + (r.profile && r.profile!=='Default' ? ' ('+r.profile+')' : '');
      if(status) { status.textContent = ti('sp.found_saved', {where: _where}); status.style.color = 'var(--green)'; }
      loadSpotifyStatus();
      toast(ti('sp.spdc_updated', {browser: r.browser}), 'var(--green)');
    } else {
      // sp_dc — httpOnly, из document.cookie не читается. Показываем Application tab.
      if(status) {
        status.innerHTML = t('sp.spdc_manual_html');
        status.style.color = 'var(--muted)';
      }
    }
  } catch(e) {
    if(status) { status.textContent = '✗ ' + e.message; status.style.color = 'var(--red)'; }
  } finally {
    if(btn) { btn.disabled = false; btn.textContent = t('sp.auto_btn'); }
  }
}

// ── Statistics ────────────────────────────────────────────────────
let _statsPeriod = 'week';

async function loadStats(period) {
  if (period) _statsPeriod = period;

  // Period tab highlight
  document.querySelectorAll('#stats-period-tabs .stab').forEach(b => {
    const on = b.dataset.p === _statsPeriod;
    b.style.borderBottomColor = on ? 'var(--green)' : 'transparent';
    b.style.color      = on ? 'var(--text)' : '';
    b.style.fontWeight = on ? '700' : '';
  });

  const set = (id, html) => { const el = document.getElementById(id); if (el) el.innerHTML = html; };
  const note = msg => set('stats-hero', `<div style="grid-column:1/-1;color:var(--muted);font-size:12px;padding:8px 0">${msg}</div>`);

  let d = null, httpStatus = 0;
  try {
    const resp = await fetch(`/api/stats?period=${_statsPeriod}`);
    httpStatus = resp.status;
    d = await resp.json().catch(() => null);
  } catch (_) { d = null; }

  if (httpStatus === 401 || (d && d.error === 'unauthorized')) {
    note('🔒 Сессия не авторизована — обнови страницу (Ctrl+F5) и войди заново.');
    return;
  }
  if (!d || d.error) {
    note(`Статистика недоступна${d && d.error ? ': ' + esc(d.error) : ''}`);
    return;
  }
  const t = d.totals || {};

  // ── Hero cards ──
  const hero = [
    { icon: '⬇',  label: 'Загрузок',     val: t.downloads,       color: 'var(--green)'  },
    { icon: '♪',  label: 'Треков',        val: t.tracks,          color: 'var(--blue)'   },
    { icon: '🎧', label: 'Прослушиваний', val: t.stream_sessions, color: 'var(--red)'    },
    { icon: '👤', label: 'Гостей',        val: t.guests,          color: 'var(--purple)' },
  ];
  set('stats-hero', hero.map(c => `
    <div class="card" style="padding:14px 16px;display:flex;align-items:center;gap:12px">
      <div style="font-size:24px;line-height:1">${c.icon}</div>
      <div style="min-width:0">
        <div style="font-size:24px;font-weight:800;color:${c.color};font-family:var(--mono);line-height:1.1">${_fmt(c.val || 0)}</div>
        <div style="font-size:11px;color:var(--muted);margin-top:1px">${c.label}</div>
      </div>
    </div>`).join(''));

  // ── Bar-list helper ──
  function bars(items, opts) {
    opts = opts || {};
    const nameKey  = opts.nameKey  || 'name';
    const countKey = opts.countKey || 'count';
    const color    = opts.color    || 'var(--green)';
    const lw       = opts.labelWidth || 120;
    if (!items || !items.length)
      return '<div style="color:var(--muted);font-size:11px;padding:3px 0">Нет данных</div>';
    const max = opts.max || Math.max(...items.map(r => r[countKey] || 0), 1);
    return items.map(r => {
      const pct  = Math.round((r[countKey] || 0) / max * 100);
      const name = r[nameKey] || '—';
      const bcol = typeof color === 'function' ? color(r) : color;
      const bdg  = opts.badge ? opts.badge(r) : '';
      return `<div style="display:flex;align-items:center;gap:8px;margin-bottom:6px">
        <div style="width:${lw}px;font-size:11px;color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;flex-shrink:0" title="${esc(name)}">${esc(name)}</div>
        ${bdg}
        <div style="flex:1;height:8px;background:var(--surface2);border-radius:4px;overflow:hidden;min-width:24px">
          <div style="width:${pct}%;height:100%;background:${bcol};border-radius:4px;transition:width .4s"></div>
        </div>
        <div style="font-size:10px;color:var(--muted);font-family:var(--mono);width:32px;text-align:right;flex-shrink:0">${_fmt(r[countKey] || 0)}</div>
      </div>`;
    }).join('');
  }

  const STREAM_COLOR = { qobuz:'#1870f5', tidal:'#00d4b3', deezer:'#a238ff', bbc:'#e4003b', generic:'var(--muted2)' };
  const STREAM_LABEL = { qobuz:'Qobuz', tidal:'Tidal', deezer:'Deezer', bbc:'BBC', generic:'Другое' };

  // ── Listening: split tiles ──
  const splitTiles = [
    { label: 'Превью треков', val: t.preview_sessions || 0, color: 'var(--green)' },
    { label: 'BBC Sounds',    val: t.bbc_sessions || 0,     color: '#e4003b'      },
  ].map(c => `
    <div style="background:var(--surface2);border-radius:9px;padding:10px 12px">
      <div style="font-size:20px;font-weight:800;color:${c.color};font-family:var(--mono)">${_fmt(c.val)}</div>
      <div style="font-size:10px;color:var(--muted);margin-top:2px">${c.label}</div>
    </div>`).join('');
  const typeTiles = (d.by_stream_type || []).map(r => `
    <div style="background:var(--surface2);border-radius:9px;padding:10px 12px">
      <div style="font-size:20px;font-weight:800;color:${STREAM_COLOR[r.name] || 'var(--muted2)'};font-family:var(--mono)">${_fmt(r.count || 0)}</div>
      <div style="font-size:10px;color:var(--muted);margin-top:2px">${STREAM_LABEL[r.name] || esc(r.name || '—')}</div>
    </div>`).join('');
  set('stats-listen-split', splitTiles + typeTiles);

  // ── Listening: top played ──
  const topL = d.top_streams || [];
  set('stats-listen-top', topL.length
    ? '<div style="font-size:11px;color:var(--muted);margin-bottom:7px">Топ прослушанного</div>' +
      bars(topL, {
        color: r => STREAM_COLOR[r.stream_type] || 'var(--green)',
        badge: r => {
          const st = r.stream_type || 'generic';
          const c  = STREAM_COLOR[st] || 'var(--muted2)';
          return `<div style="font-size:8px;font-weight:700;text-transform:uppercase;letter-spacing:.4px;color:${c};background:${c}22;border-radius:4px;padding:2px 6px;flex-shrink:0">${STREAM_LABEL[st] || esc(st)}</div>`;
        },
      })
    : '<div style="color:var(--muted);font-size:11px">Пока ничего не слушали</div>');

  // ── Listening history — recent plays, newest first ──
  const recent = d.recent_listens || [];
  set('stats-listen-recent', recent.length
    ? '<div style="font-size:11px;color:var(--muted);margin-bottom:7px">История прослушки</div>' +
      recent.slice(0, 40).map(e => {
        const st  = e.type || 'generic';
        const c   = STREAM_COLOR[st] || 'var(--muted2)';
        const dt  = new Date((e.ts || 0) * 1000);
        const tm  = dt.toDateString() === new Date().toDateString()
          ? dt.toTimeString().slice(0, 5)
          : `${dt.getDate()}.${String(dt.getMonth()+1).padStart(2,'0')} ${dt.toTimeString().slice(0,5)}`;
        return `<div style="display:flex;align-items:center;gap:8px;padding:3px 0;font-size:11px;border-top:1px solid var(--surface2)">
          <span style="color:var(--muted2);font-family:var(--mono);width:78px;flex-shrink:0">${tm}</span>
          <span style="font-size:8px;font-weight:700;text-transform:uppercase;color:${c};background:${c}22;border-radius:4px;padding:2px 6px;flex-shrink:0">${STREAM_LABEL[st] || esc(st)}</span>
          <span style="color:var(--text);overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(e.name)}">${esc(e.name)}</span>
        </div>`;
      }).join('')
    : '');

  // ── Service / quality ──
  set('stats-by-service', bars((d.by_service || []).map(r => ({ name: r.label || r.name || '—', count: r.count })), { color:'var(--green)' }));
  set('stats-by-quality', bars((d.by_quality || []).map(r => ({ name: (r.name || '—').toUpperCase(), count: r.count })), { color:'var(--orange)' }));

  // ── Top artists ──
  set('stats-by-artist', bars((d.by_artist || []).slice(0, 20), { color:'var(--purple)', labelWidth:110 }));

  // ── Timeline by day ──
  const days = d.by_day || [];
  const tlMax = Math.max(...days.map(r => r.count || 0), 1);
  set('stats-timeline', days.length
    ? days.map(r => {
        const pct = Math.max(Math.round((r.count || 0) / tlMax * 100), 2);
        return `<div title="${esc(r.date || '')}: ${_fmt(r.count || 0)}" style="flex:1;min-width:11px;max-width:30px;background:var(--green);border-radius:3px 3px 0 0;height:${pct}%;min-height:2px;opacity:.85"></div>`;
      }).join('')
    : '<div style="color:var(--muted);font-size:11px">Нет данных</div>');
  const tlStep = days.length > 30 ? Math.ceil(days.length / 10) : (days.length > 14 ? 3 : 1);
  set('stats-tl-labels', days.map((r, i) =>
    `<div style="flex:1;min-width:11px;max-width:30px;text-align:center;overflow:hidden">${i % tlStep === 0 ? (r.date || '').slice(5) : ''}</div>`).join(''));

  // ── By hour ──
  const hours = Array.isArray(d.by_hour) ? d.by_hour : [];
  const hMax = Math.max(...hours.map(h => h.count || 0), 1);
  set('stats-by-hour', hours.map(h => {
    const pct = Math.round((h.count || 0) / hMax * 100);
    const col = h.hour < 6 ? 'var(--muted2)' : h.hour < 12 ? 'var(--blue)' : h.hour < 18 ? 'var(--green)' : 'var(--purple)';
    return `<div title="${String(h.hour).padStart(2,'0')}:00 — ${_fmt(h.count || 0)}" style="flex:1;background:${col};opacity:${0.25 + pct/100*0.75};border-radius:2px 2px 0 0;height:${Math.max(pct,3)}%;min-height:3px"></div>`;
  }).join(''));
  set('stats-hour-labels', [0,6,12,18,23].map(h =>
    `<div style="flex:${h===0?1:h===23?1:6};text-align:${h===0?'left':h===23?'right':'center'}">${String(h).padStart(2,'0')}</div>`).join(''));

  // ── By weekday ──
  const wd = d.by_weekday || [];
  set('stats-by-weekday', bars(wd, {
    labelWidth: 28,
    color: r => r.day >= 5 ? 'var(--orange)' : 'var(--blue)',
    max: Math.max(...wd.map(r => r.count || 0), 1),
  }));

  // ── Guests ──
  set('stats-guests', `
    <div style="display:flex;gap:28px;flex-wrap:wrap">
      <div><span style="font-size:20px;font-weight:800;color:var(--purple);font-family:var(--mono)">${_fmt(t.guests || 0)}</span>
        <span style="font-size:12px;color:var(--muted);margin-left:6px">уникальных гостей</span></div>
      <div><span style="font-size:20px;font-weight:800;color:var(--orange);font-family:var(--mono)">${_fmt(Math.round(t.guest_minutes || 0))}</span>
        <span style="font-size:12px;color:var(--muted);margin-left:6px">минут онлайн</span></div>
    </div>`);

  const footEl = document.getElementById('stats-footer');
  if (footEl) footEl.textContent = 'Данные за: ' +
    ({ day:'24 часа', week:'7 дней', month:'30 дней', year:'365 дней', all:'всё время' }[_statsPeriod] || _statsPeriod);
}

function _fmt(n) {
  if (n == null) return '0';
  return n >= 1000 ? (n/1000).toFixed(1)+'k' : String(n);
}

// ── OrpheusDL (Spotify) ───────────────────────────────────────────
async function loadOrpheusStatus() {
  const r = await api('GET', '/api/orpheus/status').catch(()=>null);
  const badge   = document.getElementById('orp-badge');
  const authBar = document.getElementById('orp-auth-bar');
  const authDetail = document.getElementById('orp-auth-detail');
  const toggle  = document.getElementById('s-orp-mode');
  const qualSel = document.getElementById('s-orp-quality');
  if(!r) return;

  if(badge) {
    if(r.installed && r.authenticated) {
      badge.textContent = t('orp.connected');
      badge.style.background = 'rgba(62,207,170,.15)';
      badge.style.color = 'var(--green)';
    } else if(r.installed) {
      badge.textContent = t('orp.installed');
      badge.style.background = 'rgba(10,132,255,.15)';
      badge.style.color = '#0a84ff';
    } else {
      badge.textContent = t('orp.not_installed');
      badge.style.background = 'rgba(255,255,255,.07)';
      badge.style.color = 'var(--muted)';
    }
  }
  if(toggle) toggle.checked = (r.mode === 'orpheus_spotify');
  if(qualSel && r.quality) qualSel.value = r.quality;
  const mp3Tog = document.getElementById('s-orp-mp3');
  if(mp3Tog) mp3Tog.checked = S.config['orpheus-convert-mp3'] === true;

  const loginSec = document.getElementById('orp-login-section');
  if(authBar && loginSec) {
    if(r.authenticated) {
      authBar.style.display = 'flex';
      loginSec.style.display = 'none';
      if(authDetail) authDetail.textContent = r.username ? r.username : t('orp.sp_account');
    } else {
      authBar.style.display = 'none';
      loginSec.style.display = '';
    }
  }
}

async function saveSpotifyToken() {
  const ta = document.getElementById('s-sp-token-blob');
  const st = document.getElementById('sp-token-status');
  const blob = (ta && ta.value || '').trim();
  if (!blob) { if(st){st.textContent='вставь заголовки'; st.style.color='var(--muted)';} return; }
  if (st) { st.textContent='сохраняю…'; st.style.color='var(--muted)'; }
  try {
    const r = await api('POST', '/api/admin/spotify-token', { blob });
    if (r && r.ok) {
      if (st) { st.textContent='✓ обновлён: ' + ((r.updated||[]).join(', ')); st.style.color='var(--green)'; }
      if (ta) ta.value='';
    } else if (st) { st.textContent='✗ ' + ((r && r.error) || 'токен не найден'); st.style.color='#ff453a'; }
  } catch (e) {
    if (st) { st.textContent='✗ ' + (e.message || e); st.style.color='#ff453a'; }
  }
}

let _spTokPoll = null;
async function loadSpotifyTokenStatus() {
  const freshEl = document.getElementById('sp-tok-fresh');
  const logEl = document.getElementById('sp-push-log');
  try {
    const r = await api('GET', '/api/admin/spotify-token-status');
    if (freshEl) {
      const a = r.bearer_age_min;
      if (a == null) { freshEl.textContent = '⚪ нет токена'; freshEl.style.color = 'var(--muted)'; }
      else if (r.fresh) { freshEl.textContent = '🟢 свежий (' + a + ' мин)'; freshEl.style.color = 'var(--green)'; }
      else { freshEl.textContent = '🔴 протух (' + a + ' мин)'; freshEl.style.color = '#ff453a'; }
    }
    if (logEl) {
      const log = r.log || [];
      logEl.innerHTML = log.length
        ? log.map(e => `${e.time||''} ${e.status||''}${e.detail ? (' · ' + e.detail) : ''}`).join('<br>')
        : 'Пушей пока нет. Открой Spotify-таб и поиграй трек — расширение пришлёт токен.';
    }
  } catch (e) {
    if (logEl) logEl.textContent = '✗ ' + (e.message || e);
  }
}
function startSpotifyTokenPoll() {
  loadSpotifyTokenStatus();
  if (_spTokPoll) clearInterval(_spTokPoll);
  _spTokPoll = setInterval(loadSpotifyTokenStatus, 20000);
}
function stopSpotifyTokenPoll() { if (_spTokPoll) { clearInterval(_spTokPoll); _spTokPoll = null; } }

// Token expiry badge in the per-service settings tabs (computed from the real
// token, e.g. Tidal's JWT `exp`). One endpoint returns all services; each tab
// renders its own `#<svc>-tok-expiry` element. Tidal first; others follow.
async function loadTokenExpiry(svc) {
  const el = document.getElementById(svc + '-tok-expiry');
  if (!el) return;
  try {
    const r = await api('GET', '/api/admin/token-expiry');
    const t = r && r[svc];
    if (!t) { el.textContent = '⚪ токен не распознан (нет срока в JWT)'; el.style.color = 'var(--muted)'; return; }
    if (t.session === 'device-flow') { el.textContent = '🟢 Вход через device-flow — сессия обновляется сама, ручной токен не нужен'; el.style.color = 'var(--green)'; return; }
    const d = t.days_left;
    if (!t.valid)        { el.textContent = '🔴 токен ИСТЁК — обнови'; el.style.color = '#ff453a'; }
    else if (d < 1)      { el.textContent = '🟠 истекает менее чем через ' + Math.max(1, Math.round(t.hours_left)) + ' ч — обнови сегодня'; el.style.color = '#ff9f0a'; }
    else if (d < 3)      { el.textContent = '🟠 осталось ' + d + ' дн. — скоро обновить'; el.style.color = '#ff9f0a'; }
    else                 { el.textContent = '🟢 осталось ' + Math.round(d) + ' дн. до истечения'; el.style.color = 'var(--green)'; }
  } catch (e) {
    el.textContent = '✗ ' + (e.message || e); el.style.color = 'var(--muted)';
  }
}

async function orpheusLogin() {
  const btn    = document.getElementById('btn-orp-login');
  const stEl   = document.getElementById('orp-login-status');
  const setStatus = (msg, color='var(--muted)') => {
    if(stEl) { stEl.textContent=msg; stEl.style.color=color; stEl.style.display=''; }
  };
  if(btn) { btn.disabled=true; btn.textContent='⏳ Запускаю…'; }
  setStatus('Запрашиваю OAuth URL…');

  const r = await api('POST', '/api/orpheus/login-start');
  if(!r || !r.ok || !r.url) {
    setStatus(r?.error || 'Ошибка запуска OAuth', '#ff453a');
    if(btn) { btn.disabled=false; btn.textContent='🎵 Войти в Spotify'; }
    return;
  }

  setStatus('Открываю окно Spotify — войди любым способом…', '#0a84ff');
  const popup = window.open(r.url, 'orpheus_oauth', 'width=520,height=700');
  if(!popup) {
    setStatus('Popup заблокирован браузером — разреши попапы для этого сайта', 'var(--orange)');
    await api('DELETE', '/api/orpheus/login-cancel');
    if(btn) { btn.disabled=false; btn.textContent='🎵 Войти в Spotify'; }
    return;
  }

  const pollTimer = setInterval(async () => {
    if(popup.closed) {
      clearInterval(pollTimer);
      window._orpheusLoginDone = null;
      setStatus('Окно закрыто до завершения входа', 'var(--orange)');
      await api('DELETE', '/api/orpheus/login-cancel');
      if(btn) { btn.disabled=false; btn.textContent='🎵 Войти в Spotify'; }
    }
  }, 1000);

  window._orpheusLoginDone = () => {
    clearInterval(pollTimer);
    window._orpheusLoginDone = null;
    try { popup.close(); } catch(e) {}
    if(stEl) stEl.style.display='none';
    if(btn) { btn.disabled=false; btn.textContent='🎵 Войти в Spotify'; }
    loadOrpheusStatus();
  };
}

async function orpheusLogout() {
  await api('DELETE', '/api/orpheus/logout');
  toast('OrpheusDL: выход выполнен', 'var(--muted)');
  loadOrpheusStatus();
}

async function setOrpheusMode(enabled) {
  await saveSetting('spotify-engine', enabled ? 'orpheus_spotify' : 'convert');
  const sub = document.getElementById('orp-toggle-sub');
  if(sub) sub.textContent = enabled
    ? 'Spotify-ссылки идут в очередь напрямую'
    : 'Spotify-ссылки конвертируются через другой сервис';
}

// ── SoundCloud / Lucida ───────────────────────────────────────────
async function loadBeatportStatus() {
  const cloneSec    = document.getElementById('bp-clone-section');
  const reinstallSec= document.getElementById('bp-reinstall-section');
  const installBar  = document.getElementById('bp-install-status');
  const installLbl  = document.getElementById('bp-install-label');
  const r = await api('GET', '/api/beatport/status').catch(()=>null);
  if(r && r.module_installed) {
    if(installBar)   installBar.style.display = 'flex';
    if(installLbl)   installLbl.textContent = '✓ Модуль Beatport установлен';
    if(cloneSec)     cloneSec.style.display = 'none';
    if(reinstallSec) reinstallSec.style.display = '';
  } else {
    if(installBar)   installBar.style.display = 'none';
    if(cloneSec)     cloneSec.style.display = '';
    if(reinstallSec) reinstallSec.style.display = 'none';
  }
}

async function installBeatportModule() {
  const btn = document.getElementById('btn-bp-install');
  if(btn){ btn.disabled=true; btn.textContent='⏳ Устанавливаю…'; }
  const nav = document.querySelector('.nav-item[data-view="setup"]');
  if(nav) showView('setup', nav);   // install streams to the Setup console now
  toast('⬇ Устанавливаю orpheusdl-beatport…','#01f49c');
  try {
    await api('POST', '/api/setup/beatport');
  } catch(e) {
    toast('Ошибка: '+e.message,'var(--red)');
    if(btn){ btn.disabled=false; btn.textContent='⬇ Установить автоматически'; }
    return;
  }
  // Poll until module is confirmed installed or 60s timeout
  let attempts = 0;
  const poll = setInterval(async () => {
    attempts++;
    const r = await api('GET', '/api/beatport/status').catch(()=>null);
    if(r && r.module_installed) {
      clearInterval(poll);
      loadBeatportStatus();
      toast('✓ orpheusdl-beatport установлен','#01f49c');
    } else if(attempts >= 12) {
      clearInterval(poll);
      if(btn){ btn.disabled=false; btn.textContent='⬇ Установить автоматически'; }
    }
  }, 5000);
}


// ══ RELEASES ══════════════════════════════════════════════════════

// Cache: avoid re-fetching every time the user switches to the Releases tab
const _relCache = { data: null, ts: 0, key: '' };
const _REL_CACHE_TTL = 10 * 60 * 1000; // 10 min in-memory TTL
const _REL_LS_KEY    = 'ripster_rel_v2';
const _REL_SEEN_KEY  = 'ripster_rel_seen';
const _REL_FAV_KEY   = 'ripster_rel_favs';
const _REL_PREF_KEY  = 'ripster_rel_prefs';
const _REL_PAGE_SIZE = 120;
let _relShowing = _REL_PAGE_SIZE;
let _relFilteredData = [];
let _relView    = 'all';        // 'all' | 'new' | 'fav'
let _relTypeOff = new Set();     // release types toggled off via chips

function _relLoadJSON(key, fallback) {
  try { const r = localStorage.getItem(key); return r ? JSON.parse(r) : fallback; }
  catch(e) { return fallback; }
}
function _relSaveJSON(key, val) {
  try { localStorage.setItem(key, JSON.stringify(val)); } catch(e) {}
}

let _relSeen = new Set(_relLoadJSON(_REL_SEEN_KEY, []));
let _relFavs = _relLoadJSON(_REL_FAV_KEY, []);   // full release objects

function _relUID(rel) {
  return (rel.service||'') + '|' + (rel.id || rel.url || ((rel.title||'')+'~'+(rel.artist||'')));
}
function _relIsNew(rel) { return !_relSeen.has(_relUID(rel)); }
function _relIsFav(rel) {
  const u = _relUID(rel);
  return _relFavs.some(f => _relUID(f) === u);
}

function _relSavePrefs() {
  _relSaveJSON(_REL_PREF_KEY, {
    days:    document.getElementById('rel-days')?.value,
    sort:    document.getElementById('rel-sort')?.value,
    view:    _relView,
    typeOff: [..._relTypeOff],
  });
}
function _relRestorePrefs() {
  const p = _relLoadJSON(_REL_PREF_KEY, null);
  if (!p) return;
  const d = document.getElementById('rel-days');
  const s = document.getElementById('rel-sort');
  if (d && p.days) d.value = p.days;
  if (s && p.sort) s.value = p.sort;
  if (p.view) _relView = p.view;
  if (Array.isArray(p.typeOff)) _relTypeOff = new Set(p.typeOff);
}

function setRelView(v) { _relView = v; _relSavePrefs(); _applyRelFilter(); }
function toggleRelType(t) {
  if (_relTypeOff.has(t)) _relTypeOff.delete(t); else _relTypeOff.add(t);
  _relSavePrefs(); _applyRelFilter();
}
function toggleRelFav(uid) {
  const i = _relFavs.findIndex(f => _relUID(f) === uid);
  if (i >= 0) {
    _relFavs.splice(i, 1);
  } else {
    const rel = (_relCache.data || []).concat(_relFilteredData).find(r => _relUID(r) === uid);
    if (rel) _relFavs.unshift(rel);
    if (_relFavs.length > 500) _relFavs.length = 500;
  }
  _relSaveJSON(_REL_FAV_KEY, _relFavs);
  _applyRelFilter(false);
}
let _relSeenUndo = null;   // snapshot for undo of the last "mark all seen"
function markAllRelSeen() {
  // Snapshot so an accidental click is fully reversible.
  _relSeenUndo = [..._relSeen];
  for (const r of (_relCache.data || [])) _relSeen.add(_relUID(r));
  if (_relSeen.size > 6000) _relSeen = new Set([..._relSeen].slice(-6000));
  _relSaveJSON(_REL_SEEN_KEY, [..._relSeen]);
  toast('Все релизы отмечены просмотренными &nbsp;<button onclick="_relUndoSeen()" style="padding:2px 9px;border-radius:6px;border:1px solid var(--orange);background:transparent;color:var(--orange);font-size:11px;font-weight:700;cursor:pointer;font-family:var(--font)">↩ Отменить</button>', 'var(--green)', '', 9000);
  _applyRelFilter(false);
}
function _relUndoSeen() {
  if (!_relSeenUndo) return;
  _relSeen = new Set(_relSeenUndo);
  _relSeenUndo = null;
  _relSaveJSON(_REL_SEEN_KEY, [..._relSeen]);
  toast('Отменено — отметки «просмотрено» восстановлены', 'var(--orange)', '', 3000);
  _applyRelFilter(false);
}
// Full reset — un-hide everything (recover from an accidental "mark all seen").
function resetRelSeen() {
  _relSeen = new Set();
  _relSeenUndo = null;
  _relSaveJSON(_REL_SEEN_KEY, []);
  toast('Сброшено — все релизы снова «новые»', 'var(--green)', '', 3000);
  _applyRelFilter(false);
}

function _relDateLabel(d) {
  if (!d) return 'Без даты';
  const today = new Date(); today.setHours(0,0,0,0);
  const dt = new Date(d + 'T00:00:00');
  if (isNaN(dt)) return d;
  const diff = Math.round((today - dt) / 86400000);
  if (diff === 0) return 'Сегодня';
  if (diff === 1) return 'Вчера';
  const full = dt.toLocaleDateString('ru', { day:'numeric', month:'long', year:'numeric' });
  if (diff > 1 && diff < 7) {
    const wd = dt.toLocaleDateString('ru', { weekday:'long' });
    return wd.charAt(0).toUpperCase() + wd.slice(1) + ', ' + full;
  }
  return full;
}

function renderRelChips() {
  const data = _relCache.data || [];
  const newCount = data.filter(_relIsNew).length;
  const favCount = _relFavs.length;
  const vc = document.getElementById('rel-view-chips');
  if (vc) {
    const mk = (id, label, clr) => {
      const on = _relView === id;
      return `<button onclick="setRelView('${id}')" style="padding:4px 11px;border-radius:14px;border:1px solid ${on?clr:'var(--border)'};background:${on?clr+'22':'transparent'};color:${on?clr:'var(--muted)'};font-size:11px;font-weight:700;cursor:pointer;font-family:var(--font);white-space:nowrap">${label}</button>`;
    };
    vc.innerHTML =
      mk('all', 'Все', 'var(--text)') +
      mk('new', '🆕 Новое' + (newCount ? ' ' + newCount : ''), 'var(--green)') +
      mk('fav', '★ Избранное' + (favCount ? ' ' + favCount : ''), 'var(--orange)');
  }
  const tc = document.getElementById('rel-type-chips');
  if (tc) {
    const order = ['album','single','ep','compilation','appears_on','live'];
    const lbl   = {album:'Альбомы',single:'Синглы',ep:'EP',compilation:'Сборники',appears_on:'Участвует',live:'Live'};
    const types = [...new Set(data.map(r => r.type || 'album'))]
      .sort((a,b) => (((order.indexOf(a)+1)||99) - ((order.indexOf(b)+1)||99)));
    tc.innerHTML = types.map(t => {
      const on = !_relTypeOff.has(t);
      return `<button onclick="toggleRelType('${t}')" style="padding:4px 10px;border-radius:14px;border:1px solid ${on?'var(--red)':'var(--border)'};background:${on?'rgba(192,132,160,.14)':'transparent'};color:${on?'var(--red)':'var(--muted2)'};font-size:11px;font-weight:600;cursor:pointer;font-family:var(--font);white-space:nowrap">${lbl[t]||t.toUpperCase()}</button>`;
    }).join('');
    tc.style.display = types.length ? '' : 'none';
  }
}

function _relGroupGrid(cardsHtml) {
  return `<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:12px">${cardsHtml}</div>`;
}
function _renderRelFlat(list) {
  return _relGroupGrid(list.map(renderReleaseCard).join(''));
}
function _renderRelGroups(list) {
  let html = '', curDate = null, buf = [];
  const flush = () => {
    if (!buf.length) return;
    html += `<div style="margin-bottom:4px">
      <div style="display:flex;align-items:baseline;gap:8px;margin:16px 0 9px;padding-bottom:5px;border-bottom:1px solid var(--border)">
        <span style="font-size:13px;font-weight:800;color:var(--text)">${_relDateLabel(curDate)}</span>
        <span style="font-size:10px;color:var(--muted2);font-family:var(--mono)">${buf.length} рел.</span>
      </div>
      ${_relGroupGrid(buf.map(renderReleaseCard).join(''))}
    </div>`;
    buf = [];
  };
  for (const rel of list) {
    if (rel.date !== curDate) { flush(); curDate = rel.date; }
    buf.push(rel);
  }
  flush();
  return html;
}

function _applyRelFilter(resetPage) {
  const grid  = document.getElementById('releases-grid');
  const empty = document.getElementById('rel-empty');
  if (!grid) return;
  if (resetPage !== false) _relShowing = _REL_PAGE_SIZE;

  let data = (_relView === 'fav') ? _relFavs.slice() : (_relCache.data || []).slice();

  const q = (document.getElementById('rel-search')?.value || '').toLowerCase().trim();
  if (q) data = data.filter(r => (r.title||'').toLowerCase().includes(q) || (r.artist||'').toLowerCase().includes(q));
  if (_relView === 'new')  data = data.filter(_relIsNew);
  if (_relTypeOff.size)    data = data.filter(r => !_relTypeOff.has(r.type || 'album'));

  const sort = document.getElementById('rel-sort')?.value || 'date_desc';
  switch (sort) {
    case 'date_asc':    data.sort((a,b) => (a.date||'').localeCompare(b.date||'')); break;
    case 'tracks_desc': data.sort((a,b) => (b.tracks||0) - (a.tracks||0)); break;
    case 'tracks_asc':  data.sort((a,b) => (a.tracks||0) - (b.tracks||0)); break;
    case 'artist_asc':  data.sort((a,b) => (a.artist||'').localeCompare(b.artist||'')); break;
    case 'artist_desc': data.sort((a,b) => (b.artist||'').localeCompare(a.artist||'')); break;
    case 'title_asc':   data.sort((a,b) => (a.title||'').localeCompare(b.title||'')); break;
    default:            data.sort((a,b) => (b.date||'').localeCompare(a.date||''));
  }
  _relFilteredData = data;

  const badge = document.getElementById('releases-badge');
  if (badge) { const n = (_relCache.data||[]).length; badge.textContent = n; badge.style.display = n ? '' : 'none'; }

  renderRelChips();

  if (!data.length) {
    grid.innerHTML = '';
    if (empty) {
      const totalData = (_relCache.data || []).length;
      if (_relView === 'new' && totalData) {
        // Everything is marked seen — don't leave a dead screen. Offer recovery
        // (this is exactly the "accidentally pressed «прочитано»" case).
        const btn = (txt, fn, clr) => `<button onclick="${fn}" style="padding:6px 14px;border-radius:8px;border:1px solid ${clr};background:transparent;color:${clr};font-size:12px;font-weight:700;cursor:pointer;font-family:var(--font)">${txt}</button>`;
        empty.innerHTML = `<div style="display:flex;flex-direction:column;align-items:center;gap:12px">
          <div>Новых релизов нет — все ${totalData} отмечены просмотренными</div>
          <div style="display:flex;gap:9px;flex-wrap:wrap;justify-content:center">
            ${btn('Показать все', "setRelView('all')", 'var(--text)')}
            ${btn('↩ Сбросить просмотренное', 'resetRelSeen()', 'var(--orange)')}
          </div></div>`;
      } else {
        empty.textContent = _relView === 'fav' ? 'Нет избранных релизов — нажми ☆ на карточке'
                          : _relView === 'new' ? 'Новых релизов нет — всё просмотрено'
                          : 'Нет релизов за выбранный период';
      }
      empty.style.display = '';
    }
    _relUpdateLoadMore(0);
    return;
  }
  if (empty) empty.style.display = 'none';

  const visible = data.slice(0, _relShowing);
  const grouped = (sort === 'date_desc' || sort === 'date_asc');
  grid.innerHTML = grouped ? _renderRelGroups(visible) : _renderRelFlat(visible);
  _relUpdateLoadMore(data.length);
}

function _relUpdateLoadMore(total) {
  const btn   = document.getElementById('rel-load-more');
  const count = document.getElementById('rel-load-more-count');
  if(!btn) return;
  const remaining = total - _relShowing;
  if(remaining > 0) {
    if(count) count.textContent = `ещё ${remaining}`;
    btn.style.display = '';
  } else {
    btn.style.display = 'none';
  }
}

function _relShowMore() {
  _relShowing += _REL_PAGE_SIZE;
  _applyRelFilter(false);
}

function _relActiveSvcs() {
  const c   = S.config || {};
  const cfg = (c['releases-services'] || 'spotify').split(',').map(s=>s.trim()).filter(Boolean);
  const hasQobuz = !!(c['qobuz-auth-token'] || '').trim();
  const tidalTok = (c['tidal-token'] || '').trim();
  const hasTidal = !!tidalTok && !_jwtExpired(tidalTok);
  return cfg.filter(svc => {
    if(svc === 'spotify') return true; // Spotify auth handled separately
    if(svc === 'qobuz')   return hasQobuz;
    if(svc === 'tidal')   return hasTidal;
    return false;
  });
}

function _relCacheKey() {
  const days  = document.getElementById('rel-days')?.value  || (S.config?.['releases-days'] || '90');
  const types = document.getElementById('rel-types')?.value || (S.config?.['releases-types'] || 'album,single');
  const svcs  = _relActiveSvcs().join(',');
  return `${days}|${types}|${svcs}`;
}

function _renderRelActiveSvcs() {
  const cont = document.getElementById('rel-active-svcs');
  if(!cont) return;
  const svcs = _relActiveSvcs();
  const colors = {spotify:'#1db954',qobuz:'#1870f5',tidal:'#00d4b3'};
  cont.innerHTML = svcs.map(svc =>
    `<span style="display:inline-flex;align-items:center;gap:4px;padding:2px 8px;border-radius:12px;font-size:10px;font-weight:700;border:1px solid ${colors[svc]||'var(--border)'}33;color:${colors[svc]||'var(--muted)'};background:${colors[svc]||'transparent'}11">`+
    `<span style="width:5px;height:5px;border-radius:50%;background:${colors[svc]||'var(--muted)'}"></span>${svc.charAt(0).toUpperCase()+svc.slice(1)}</span>`
  ).join('');
}

function saveRelSvcConfig() {
  const svcs = ['spotify','qobuz','tidal']
    .filter(s => document.getElementById('rel-cfg-'+s)?.checked)
    .join(',');
  saveSetting('releases-services', svcs || 'spotify');
  _renderRelActiveSvcs();
}

function _syncReleasesSettingsTab() {
  const c   = S.config || {};
  const cfg = (c['releases-services'] || 'spotify').split(',').map(s=>s.trim());
  ['spotify','qobuz','tidal'].forEach(svc => {
    const cb = document.getElementById('rel-cfg-'+svc);
    if(cb) cb.checked = cfg.includes(svc);
  });

  // Status labels
  const hasQobuz  = !!(c['qobuz-auth-token'] || '').trim();
  const tidalTok  = (c['tidal-token'] || '').trim();
  const hasTidal  = !!tidalTok;
  const tidalExp  = hasTidal && _jwtExpired(tidalTok);
  const hasSpDc   = !!(c['spotify-sp-dc'] || '').trim();
  const qSt  = document.getElementById('rel-cfg-qobuz-status');
  const tSt  = document.getElementById('rel-cfg-tidal-status');
  const spSt = document.getElementById('rel-cfg-spotify-status');
  if(qSt)  qSt.textContent  = hasQobuz ? '✓ токен есть' : '⚠ нет токена';
  if(tSt)  tSt.textContent  = !hasTidal ? '⚠ нет токена' : (tidalExp ? '⚠ токен истёк' : '✓ токен есть');
  // Spotify: use cached status from S._spStatus set by loadSpotifyStatus
  if(spSt) {
    const ss = S._spStatus;
    if(!hasSpDc) spSt.textContent = '⚠ не авторизован';
    else if(ss && ss.sp_dc_expired) spSt.textContent = '⚠ sp_dc истекла';
    else if(ss && ss.connected) spSt.textContent = '✓ sp_dc';
    else spSt.textContent = hasSpDc ? '? проверяется...' : '⚠ не авторизован';
  }

  // Defaults
  const dSel = document.getElementById('rel-cfg-days');
  const tSel = document.getElementById('rel-cfg-types');
  if(dSel) dSel.value = c['releases-days'] || '90';
  if(tSel) tSel.value = c['releases-types'] || 'album,single';

  _renderRelActiveSvcs();
}

function _relSaveLS(data, key) {
  try { localStorage.setItem(_REL_LS_KEY, JSON.stringify({ data, ts: Date.now(), key })); }
  catch(e) {}
}

function _relLoadLS() {
  try { const r = localStorage.getItem(_REL_LS_KEY); return r ? JSON.parse(r) : null; }
  catch(e) { return null; }
}

function _syncReleasePillsFromConfig() {
  const c = S.config || {};
  const days  = document.getElementById('rel-days');
  const types = document.getElementById('rel-types');
  if(days  && c['releases-days'])  days.value  = c['releases-days'];
  if(types && c['releases-types']) types.value = c['releases-types'];
  _renderRelActiveSvcs();
}

// Called from nav — show persisted data instantly, then refresh if stale
function loadReleasesIfStale() {
  _relRestorePrefs();
  const key = _relCacheKey();
  const age = Date.now() - _relCache.ts;

  // 1. In-memory cache still fresh → render immediately, no network
  if (_relCache.data && age < _REL_CACHE_TTL && _relCache.key === key) {
    _renderCachedReleases();
    return;
  }

  // 2. Nothing in memory → try localStorage (survives page reload)
  if (!_relCache.data) {
    const saved = _relLoadLS();
    if (saved?.data?.length) {
      _relCache.data = saved.data;
      _relCache.ts   = saved.ts;
      _relCache.key  = saved.key;
      _renderCachedReleases();      // show immediately
      const savedAge = Date.now() - saved.ts;
      // If saved data is fresh enough AND same settings → skip network
      if (savedAge < _REL_CACHE_TTL && saved.key === key) return;
    }
  }

  loadReleases(false);
}

function _renderCachedReleases() {
  const st = document.getElementById('rel-status');
  if (st) st.style.display = 'none';
  _applyRelFilter();
}

function _jwtExpired(token) {
  try {
    const payload = JSON.parse(atob(token.split('.')[1].replace(/-/g,'+').replace(/_/g,'/')));
    return payload.exp && payload.exp < Date.now() / 1000;
  } catch { return false; }
}


function renderReleaseCard(rel) {
  const dt = rel.date ? new Date(rel.date + 'T00:00:00').toLocaleDateString('ru', {day:'numeric',month:'short',year:'numeric'}) : '';
  const svcColors = {spotify:'#1db954', qobuz:'#1870f5', tidal:'#00d4b3', apple:'var(--red)', deezer:'#a238ff'};
  const svcClr  = svcColors[rel.service] || 'var(--muted)';
  const typeMap = {album:'ALBUM', single:'SINGLE', ep:'EP', compilation:'СБОРНИК', appears_on:'УЧАСТИЕ', live:'LIVE'};
  const typeClr = rel.type === 'single' ? 'var(--orange)' : (rel.type === 'album' ? '#1db954' : 'var(--muted2)');
  const typeTag = typeMap[rel.type] || (rel.type || '').toUpperCase();
  const hiresBadge = rel.hires ? '<span style="font-size:8px;padding:1px 4px;border-radius:3px;background:rgba(255,214,10,.15);color:#ffd60a;font-weight:700;margin-left:3px">HI-RES</span>' : '';
  const uid   = _relUID(rel);
  const isNew = _relIsNew(rel);
  const isFav = _relIsFav(rel);
  const baseBorder = isNew ? 'rgba(62,207,170,.55)' : 'var(--border)';
  return `<div style="background:var(--surface);border:1px solid ${baseBorder};border-radius:10px;overflow:hidden;transition:border-color .15s" onmouseover="this.style.borderColor='${svcClr}'" onmouseout="this.style.borderColor='${baseBorder}'">
    <div style="position:relative">
      ${rel.cover
        ? `<img src="${esc(rel.cover)}" data-lightbox style="width:100%;aspect-ratio:1;object-fit:cover;display:block;cursor:zoom-in" loading="lazy"/>`
        : `<div style="width:100%;aspect-ratio:1;background:rgba(255,255,255,.04);display:flex;align-items:center;justify-content:center;font-size:32px;color:var(--muted)">♪</div>`}
      <button onclick="event.stopPropagation();playRelease('${esc(rel.service)}','${escJ(rel.url)}','${escJ(rel.title)}','${escJ(rel.artist)}','${escJ(rel.cover||'')}')" title="Прослушать"
        style="position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);width:54px;height:54px;border-radius:50%;background:rgba(0,0,0,.5);border:2px solid rgba(255,255,255,.85);color:#fff;font-size:21px;line-height:1;cursor:pointer;display:flex;align-items:center;justify-content:center;padding-left:4px;backdrop-filter:blur(3px);transition:transform .12s,background .12s;z-index:2" onmouseover="this.style.transform='translate(-50%,-50%) scale(1.12)';this.style.background='rgba(0,0,0,.7)'" onmouseout="this.style.transform='translate(-50%,-50%)';this.style.background='rgba(0,0,0,.5)'">▶</button>
      <div style="position:absolute;top:6px;left:6px"><span style="font-size:9px;padding:2px 5px;border-radius:4px;background:rgba(0,0,0,.72);color:${svcClr};font-weight:700;backdrop-filter:blur(4px)">${(rel.service||'?').toUpperCase()}</span></div>
      <div style="position:absolute;top:6px;right:6px"><span style="font-size:9px;padding:2px 5px;border-radius:4px;background:rgba(0,0,0,.72);color:${typeClr};font-weight:700;backdrop-filter:blur(4px)">${typeTag}</span></div>
      ${isNew ? `<div style="position:absolute;bottom:6px;left:6px"><span style="font-size:8px;padding:2px 6px;border-radius:4px;background:var(--green);color:#06281f;font-weight:800;letter-spacing:.4px">NEW</span></div>` : ''}
    </div>
    <div style="padding:8px 10px">
      <div style="font-size:12px;font-weight:600;color:var(--text);overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(rel.title)}">${esc(rel.title)}${hiresBadge}</div>
      <div style="font-size:11px;color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(rel.artist)}">${esc(rel.artist)}</div>
      ${rel.label ? `<div style="font-size:10px;color:var(--muted);margin-top:1px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;opacity:.7" title="${esc(rel.label)}">${esc(rel.label)}</div>` : ''}
      <div style="font-size:10px;color:var(--muted);margin-top:2px">${dt}${rel.tracks ? ' · ' + rel.tracks + ' тр.' : ''}</div>
      <div style="display:flex;flex-wrap:wrap;gap:5px;margin-top:7px">
        <button onclick="downloadRelease('${esc(rel.service)}','${escJ(rel.url)}','${escJ(rel.title)}','${escJ(rel.artist)}')"
          style="flex:1 1 100%;padding:5px 0;background:rgba(192,132,160,.12);border:1px solid rgba(192,132,160,.2);border-radius:7px;font-size:10px;font-weight:700;color:var(--red);cursor:pointer;font-family:var(--font)">${t('btn.download')}</button>
        <button onclick="smartDownloadRelease(this,'${escJ(rel.url)}','${escJ(rel.title)}','${escJ(rel.artist)}')"
          style="padding:5px 8px;background:transparent;border:1px solid rgba(255,214,10,.35);border-radius:7px;font-size:11px;color:#ffd60a;cursor:pointer;font-family:var(--font)" title="Авто-источник по ISRC: NZ-первым, лучшее доступное качество">⚡</button>
        <button onclick="toggleRelFav('${escJ(uid)}')" style="padding:5px 8px;background:transparent;border:1px solid ${isFav?'var(--orange)':'var(--border)'};border-radius:7px;font-size:11px;color:${isFav?'var(--orange)':'var(--muted)'};cursor:pointer;font-family:var(--font)" title="${isFav?'Убрать из избранного':'В избранное'}">${isFav?'★':'☆'}</button>
        <button onclick="navigator.clipboard.writeText('${escJ(rel.url)}');toast(t('toast.link_copied'))" style="padding:5px 8px;background:transparent;border:1px solid var(--border);border-radius:7px;font-size:10px;color:var(--muted);cursor:pointer;font-family:var(--font)" title="Скопировать ссылку">⎘</button>
        <a href="${esc(rel.url)}" target="_blank" style="padding:5px 8px;background:transparent;border:1px solid var(--border);border-radius:7px;font-size:10px;color:var(--muted);text-decoration:none;display:flex;align-items:center" title="Открыть на ${rel.service}">↗</a>
      </div>
    </div>
  </div>`;
}

async function downloadRelease(service, url, title, artist) {
  if(service === 'spotify') {
    _showSpotifyChoiceToast(url, S.config['quality'] || 'alac');
    return;
  }
  const quality = resolveQuality(service);
  const r = await api('POST', '/api/queue/add', {url, quality, title, artist});
  if(r.ok) toast(`+ ${title} → очередь`);
  else     toast('Ошибка: ' + (r.detail || '?'), 'var(--red)');
}

// Release Radar → авто-скачка с лучшего источника по ISRC.
// Спрашивает у бэкенда (/api/release/smart-resolve), где релиз уже доступен
// (NZ-первым, через публичный враппер Apple без аккаунта; иначе Qobuz Hi-Res /
// Tidal / Deezer по ISRC), и ставит выбранный источник в очередь.
async function smartDownloadRelease(btn, url, title, artist) {
  const old = btn ? btn.textContent : '';
  if(btn) { btn.textContent = '…'; btn.disabled = true; }
  try {
    const r = await api('POST', '/api/release/smart-resolve', {url, title, artist});
    if(!r || !r.ok || !r.chosen) {
      toast('Источник не найден по ISRC', 'var(--red)');
      return;
    }
    const c = r.chosen;
    const svcName = {apple:'Apple', qobuz:'Qobuz', tidal:'Tidal', deezer:'Deezer'}[c.service] || c.service;
    const regionTag = c.region ? ` ${c.region.toUpperCase()}` : '';
    const q = c.quality || resolveQuality(c.service);
    const add = await api('POST', '/api/queue/add', {url: c.url, quality: q, title: c.title || title, artist: c.artist || artist});
    if(add.ok) toast(`⚡ ${svcName}${regionTag} → очередь`, 'var(--green)');
    else       toast('Ошибка: ' + (add.detail || '?'), 'var(--red)');
  } catch(e) {
    toast('Ошибка авто-источника', 'var(--red)');
  } finally {
    if(btn) { btn.textContent = old; btn.disabled = false; }
  }
}

// Play a release card (preview the first track). Expands the album/playlist via
// the engine and queues all tracks for sequential playback through the preview
// player. Works for any service whose engine exposes get_album.
async function playRelease(service, url, title, artist, cover) {
  toast('⏳ ' + title, 'var(--muted)', '', 1800);
  try {
    const r = await fetch(`/api/release/expand?service=${encodeURIComponent(service)}&url=${encodeURIComponent(url)}`);
    if (!r.ok) {
      const detail = await r.text().catch(() => '');
      toast('Ошибка: ' + (detail.slice(0, 120) || r.status), 'var(--red)');
      return;
    }
    const d = await r.json();
    if (!d.ok || !d.tracks?.length) {
      toast('Не удалось получить треки', 'var(--red)');
      return;
    }
    _setupAudioEvents();
    Preview.queue = d.tracks.map(tr => ({
      service:   service,
      id:        String(tr.id),
      title:     tr.title,
      artist:    tr.artist || artist,
      cover:     tr.artwork || cover || '',
      permalink: tr.url || url,
      full:      true,
      label:     `${service[0].toUpperCase()+service.slice(1)} · ${title}`,
      posKey:    `${service}:${tr.id}`,
    }));
    Preview.idx = 0;
    toast(`▶ ${title}: ${d.tracks.length} тр.`, 'var(--green)', '', 2500);
    await _playPreviewAt(0);
  } catch (e) {
    console.error('[playRelease]', e);
    toast('Ошибка: ' + e.message, 'var(--red)');
  }
}

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
// ── Media Session metadata (lockscreen cover + play/pause/next/prev) ──────
function _updateMediaSession(item, sub) {
  if (!('mediaSession' in navigator) || !item) return;
  try {
    const cover = item.cover || '';
    const art   = cover ? [
      { src: cover, sizes: '96x96',   type: '' },
      { src: cover, sizes: '256x256', type: '' },
      { src: cover, sizes: '512x512', type: '' },
    ] : [];
    navigator.mediaSession.metadata = new MediaMetadata({
      title:   item.title  || '—',
      artist:  item.artist || sub || '',
      album:   item.label  || '',
      artwork: art,
    });
  } catch {}
}

// ── Local library (downloaded files) ──────────────────────────────────────
const _LIB = { items: [], ts: 0, loaded: false, loading: false };

function libInit() {
  if (!_LIB.loaded && !_LIB.loading) loadLibrary(false);
}

async function loadLibrary(refresh = false) {
  const status  = document.getElementById('lib-status');
  const btn     = document.getElementById('lib-refresh-btn');
  const rootsEl = document.getElementById('lib-roots');
  const empty   = document.getElementById('lib-empty');
  if (status) { status.textContent = '⟳ Сканирую…'; status.style.display = 'block'; }
  if (btn) btn.disabled = true;
  _LIB.loading = true;
  try {
    const r = await fetch(`/api/library/scan${refresh ? '?refresh=1' : ''}`);
    const d = await r.json();
    if (!d.ok) throw new Error(d.error || 'scan failed');
    _LIB.items  = d.items || [];
    _LIB.ts     = d.ts || Date.now() / 1000;
    _LIB.loaded = true;
    if (rootsEl) rootsEl.textContent = (d.roots || []).map(x => '📂 ' + x).join('   ');
    const badge = document.getElementById('lib-badge');
    if (badge) {
      badge.textContent     = _LIB.items.length;
      badge.style.display   = _LIB.items.length ? '' : 'none';
    }
    if (status) status.style.display = 'none';
    if (empty)  empty.style.display  = _LIB.items.length ? 'none' : '';
    _libApplyFilter();
  } catch (e) {
    if (status) { status.textContent = '✗ ' + e.message; status.style.color = 'var(--red)'; }
  } finally {
    _LIB.loading = false;
    if (btn) btn.disabled = false;
  }
}

function _libApplyFilter() {
  const q = (document.getElementById('lib-q')?.value || '').toLowerCase().trim();
  const sort = document.getElementById('lib-sort')?.value || 'recent';
  let items = _LIB.items.slice();
  if (q) {
    items = items.filter(it =>
      (it.title  || '').toLowerCase().includes(q) ||
      (it.artist || '').toLowerCase().includes(q) ||
      (it.album  || '').toLowerCase().includes(q)
    );
  }
  switch (sort) {
    case 'artist': items.sort((a,b) => (a.artist||'').localeCompare(b.artist||'') || (a.album||'').localeCompare(b.album||'')); break;
    case 'album':  items.sort((a,b) => (a.album ||'').localeCompare(b.album ||'')); break;
    case 'title':  items.sort((a,b) => (a.title ||'').localeCompare(b.title ||'')); break;
    default:       items.sort((a,b) => (b.mtime||0) - (a.mtime||0));   // recent first
  }
  const list = document.getElementById('lib-list');
  if (!list) return;
  // Render up to 500 rows at once — past that it's a virtualization problem (Phase 2).
  const slice = items.slice(0, 500);
  list.innerHTML = slice.map(_libRow).join('');
  if (items.length > 500) {
    list.insertAdjacentHTML('beforeend',
      `<div style="padding:14px;text-align:center;color:var(--muted);font-size:11px">+${items.length - 500} ещё — уточни поиск</div>`);
  }
}

function _libRow(it) {
  const dur = it.duration ? fmtDur(it.duration) : '';
  const cov = it.has_cover ? `<img src="/api/library/cover/${it.id}" style="width:34px;height:34px;border-radius:4px;object-fit:cover;flex-shrink:0;background:var(--surface2)" loading="lazy" onerror="this.style.display='none'"/>`
                           : `<div style="width:34px;height:34px;border-radius:4px;background:rgba(255,255,255,.04);display:flex;align-items:center;justify-content:center;font-size:14px;color:var(--muted);flex-shrink:0">♪</div>`;
  const extBadge = `<span style="font-size:9px;padding:1px 5px;border-radius:3px;background:rgba(192,132,160,.12);color:#c084a0;font-family:var(--mono);font-weight:700">${(it.ext||'').toUpperCase()}</span>`;
  return `<div onclick="playLibraryTrack('${escJ(it.id)}')" style="display:flex;align-items:center;gap:10px;padding:7px 10px;background:var(--surface);border:1px solid var(--border);border-radius:8px;cursor:pointer;transition:background .12s,border-color .12s" onmouseover="this.style.background='rgba(192,132,160,.05)';this.style.borderColor='rgba(192,132,160,.3)'" onmouseout="this.style.background='var(--surface)';this.style.borderColor='var(--border)'">
    ${cov}
    <div style="flex:1;min-width:0">
      <div style="font-size:13px;font-weight:600;color:var(--text);overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(it.title)}">${esc(it.title)}</div>
      <div style="font-size:11px;color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(it.artist)} — ${esc(it.album)}">${esc(it.artist || '—')}${it.album ? ' · ' + esc(it.album) : ''}</div>
    </div>
    <div style="font-size:11px;color:var(--muted2);font-family:var(--mono);flex-shrink:0">${dur}</div>
    ${extBadge}
    <button onclick="event.stopPropagation();_libCopyPath('${escJ(it.path)}')" style="padding:3px 7px;background:transparent;border:1px solid var(--border);border-radius:5px;font-size:10px;color:var(--muted);cursor:pointer;font-family:var(--font);flex-shrink:0" title="Скопировать путь">⎘</button>
  </div>`;
}

function _libCopyPath(p) {
  try { navigator.clipboard.writeText(p); toast('Путь скопирован', 'var(--muted)', '', 1500); } catch {}
}

function playLibraryTrack(cid) {
  const it = _LIB.items.find(x => x.id === cid);
  if (!it) { toast('Трек не найден в индексе', 'var(--red)'); return; }
  _setupAudioEvents();
  const url = `/api/library/file?p=${encodeURIComponent(it.path)}`;
  Preview.queue = [{
    url,
    title:  it.title,
    artist: it.artist,
    cover:  it.has_cover ? `/api/library/cover/${it.id}` : '',
    full:   true,
    label:  `Библиотека · ${it.album || ''}`.trim(),
    posKey: 'lib:' + it.id,
  }];
  Preview.idx = 0;
  _playPreviewAt(0);
}

// ── Play any album by service+id directly (without opening the album page) ──
// Used from search-result tiles — fetches /api/album/<svc>/<id>, builds the
// play queue, and starts. Only meaningful for Qobuz/Tidal/Deezer.
async function playAlbumById(service, albumId, fallbackTitle, fallbackArtist, fallbackCover) {
  if (!(service === 'qobuz' || service === 'tidal' || service === 'deezer')) {
    toast(t('toast.stream_only_premium'), 'var(--orange)');
    return;
  }
  toast(t('toast.loading_album'), 'var(--muted)', '', 1800);
  try {
    const r = await fetch(`/api/album/${service}/${encodeURIComponent(albumId)}`);
    const d = await r.json();
    const tracks = d.tracks || [];
    if (!tracks.length) { toast(t('toast.album_empty'), 'var(--orange)'); return; }
    const album = d.album || {};
    const cover = album.cover || fallbackCover || '';
    _setupAudioEvents();
    Preview.queue = tracks
      .filter(t => t.id != null)
      .map(t => ({
        service,
        id:      String(t.id),
        title:   t.title,
        artist:  t.artist || album.artist || fallbackArtist || '',
        cover,
        full:    true,
        label:   `${_svcLabel(service)} · ${album.title || fallbackTitle || 'альбом'}`,
        posKey:  `${service}:${t.id}`,
      }));
    if (!Preview.queue.length) { toast(t('toast.no_tracks'), 'var(--orange)'); return; }
    Preview.idx = 0;
    toast(`▶ ${album.title || fallbackTitle}: ${Preview.queue.length} тр.`,
          'var(--green)', '', 2500);
    _playPreviewAt(0);
  } catch (e) {
    toast('Ошибка альбома: ' + e.message, 'var(--red)');
  }
}

// ── Universal: play the current album as a play-queue (full-streaming services) ─
// Build the full-album play queue (every track with an id, in order) from the
// currently open album. Returns [] if the album isn't streamable.
function _buildAlbumStreamQueue() {
  const a = (typeof Detail !== 'undefined') ? Detail.currentAlbum : null;
  if (!a || !a.tracks || !a.tracks.length) return [];
  const {album, tracks, service} = a;
  if (!(service === 'qobuz' || service === 'tidal' || service === 'deezer')) return [];
  return tracks
    .filter(t => t.id != null)
    .map(t => ({
      service,
      id:      String(t.id),
      title:   t.title,
      artist:  t.artist || album.artist || '',
      cover:   album.cover || '',
      full:    true,
      label:   `${_svcLabel(service)} · ${album.title || 'альбом'}`,
      posKey:  `${service}:${t.id}`,
    }));
}

// Play one album track WITHIN the full-album queue (so ⏭/⏮ + gapless work).
function playAlbumStreamTrack(idx) {
  const q = _buildAlbumStreamQueue();
  if (!q.length) { toast(t('toast.stream_only_premium'), 'var(--orange)'); return; }
  _setupAudioEvents();
  Preview.queue = q;
  Preview.idx   = Math.max(0, Math.min(idx | 0, q.length - 1));
  _playPreviewAt(Preview.idx);
  setTimeout(_syncAlbumPlayBtns, 150);
}

function playAlbumAll() {
  const a = (typeof Detail !== 'undefined') ? Detail.currentAlbum : null;
  if (!a || !a.tracks || !a.tracks.length) { toast(t('toast.album_empty'), 'var(--orange)'); return; }
  const {album, tracks, service} = a;
  if (!(service === 'qobuz' || service === 'tidal' || service === 'deezer')) {
    toast(t('toast.stream_only_premium'), 'var(--orange)');
    return;
  }
  const q = _buildAlbumStreamQueue();
  if (!q.length) { toast(t('toast.no_tracks'), 'var(--orange)'); return; }
  toast(`▶ ${album.title}: ${q.length} тр.`, 'var(--green)', '', 2500);
  playAlbumStreamTrack(0);
}

// Bulk-download every SC item that's in the current play queue — used when
// a playlist is DRM-blocked from streaming and the user wants Lucida to
// decrypt + download instead.
// SoundCloud actions extracted to /static/js/sc.js
// ── Quality selector ─────────────────────────────────────────────────────
async function updateQualitySelector(svc) {
  const sel = document.getElementById('url-quality');
  if(!sel) return;
  svc = svc || 'apple';
  // For Spotify, use the active engine's qualities
  let apiSvc = svc;
  if(svc === 'spotify') {
    const eng = (S.config && S.config['spotify-engine']) || 'convert';
    if(eng === 'orpheus_spotify') apiSvc = 'orpheus_spotify';
  }
  try {
    const qs = await (await fetch(`/api/qualities?service=${apiSvc}`)).json();
    if(!qs || !qs.length) return;
    sel.innerHTML = qs.map(q =>
      `<option value="${q.id}">${q.label} — ${q.sub||''}</option>`
    ).join('');
    const def = resolveQuality(svc) || qs[0].id;
    if([...sel.options].some(o=>o.value===def)) sel.value = def;
    // Remember which service this option list belongs to so addUrl() knows the
    // selected value is meaningful for that service (and not a stale Apple codec).
    sel.dataset.svc = svc;
  } catch(e) { console.warn('updateQualitySelector:', e); }
}

function copyField(id) {
  const v = document.getElementById(id)?.value;
  if(v){ navigator.clipboard.writeText(v); toast(t('toast.copied')); }
}
function togglePassVis() {
  const el = document.getElementById('s-wrapper-pass');
  if(el) el.type = el.type==='password' ? 'text' : 'password';
}

// ── Spectrogram ───────────────────────────────────────────────────────────────
let _specFile = null; // last dropped/selected File object

function specDropFile(file) {
  if (!file) return;
  _specFile = file;
  document.getElementById('spec-path').value = file.name;
  specAnalyzeFile(file);
}
function specLoadFile(file) {
  if (!file) return;
  _specFile = file;
  document.getElementById('spec-path').value = file.name;
  specAnalyzeFile(file);
}
async function specAnalyzePath() {
  const p = document.getElementById('spec-path').value.trim();
  if (!p) return;
  // If path matches a previously dropped file, re-upload it instead of path lookup
  if (_specFile && (_specFile.name === p || p === _specFile.name)) {
    return specAnalyzeFile(_specFile);
  }
  specShowSpinner(true);
  specShowError('');
  try {
    const r = await fetch('/api/spectrogram', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({path: p})
    });
    const d = await r.json();
    if (!r.ok || d.detail || d.error) throw new Error(d.detail || d.error || 'Ошибка');
    specShowResult(d);
  } catch(e) {
    specShowError(e.message);
  } finally {
    specShowSpinner(false);
  }
}
async function specAnalyzeFile(file) {
  specShowSpinner(true);
  specShowError('');
  try {
    const fd = new FormData();
    fd.append('file', file);
    const r = await fetch('/api/spectrogram/upload', {method:'POST', body: fd});
    const d = await r.json();
    if (!r.ok || d.detail || d.error) throw new Error(d.detail || d.error || 'Ошибка');
    specShowResult(d);
  } catch(e) {
    specShowError(e.message);
  } finally {
    specShowSpinner(false);
  }
}
function specShowSpinner(v) {
  document.getElementById('spec-spinner').style.display = v ? 'block' : 'none';
  if (v) document.getElementById('spec-result').style.display = 'none';
}
function specShowError(msg) {
  const el = document.getElementById('spec-error');
  el.style.display = msg ? 'block' : 'none';
  el.textContent = msg ? '✗ ' + msg : '';
}
function specShowResult(d) {
  document.getElementById('spec-result').style.display = 'block';
  document.getElementById('spec-img').src = 'data:image/png;base64,' + d.image;

  // Info bar
  const info = document.getElementById('spec-info');
  const fields = [
    ['Формат', d.format], ['Кодек', d.codec], ['Битрейт', d.bitrate],
    ['Частота', d.sample_rate], ['Глубина', d.bit_depth],
    ['Каналы', d.channels], ['Длительность', d.duration],
  ].filter(f => f[1]);
  info.innerHTML = fields.map(([k,v]) =>
    `<span><span style="color:var(--muted)">${k}:</span> <b>${v}</b></span>`
  ).join('');

  // Verdict
  const vd = document.getElementById('spec-verdict');
  const ok = d.verdict === 'lossless';
  const warn = d.verdict === 'suspicious';
  vd.style.background = ok ? 'rgba(62,207,170,.12)' : warn ? 'rgba(239,159,39,.12)' : 'rgba(255,69,58,.12)';
  vd.style.border = `1px solid ${ok ? 'rgba(62,207,170,.3)' : warn ? 'rgba(239,159,39,.3)' : 'rgba(255,69,58,.3)'}`;
  vd.style.color  = ok ? 'var(--green)' : warn ? 'var(--orange)' : '#ff453a';
  vd.textContent  = d.verdict_text || (ok ? '✓ Настоящий lossless — полный спектр до Nyquist' : '✗ Lossy-источник (срез частот)');
}

// BBC module extracted to /static/js/bbc.js
async function loadAppInfo() {
  try {
    const r = await fetch('/api/info');
    const info = await r.json();
    const ver = `v${info.version}`;
    const build = info.build;
    const el = document.getElementById('tb-ver');
    if(el) el.textContent = ver;
    const av = document.getElementById('about-ver');
    if(av) av.textContent = ver;
    const ab = document.getElementById('about-build');
    if(ab) ab.textContent = build;
    const ar = document.getElementById('about-repo');
    if(ar){ ar.href = info.repo; ar.textContent = info.repo.replace('https://',''); }
  } catch(e) {}
}

/* ── Mobile drawer helpers ── */
function toggleMobileDrawer() {
  const sb = document.querySelector('.sidebar');
  const ov = document.getElementById('drawer-overlay');
  if (!sb) return;
  const isOpen = sb.classList.contains('open');
  sb.classList.toggle('open', !isOpen);
  if (ov) ov.classList.toggle('open', !isOpen);
}
function closeMobileDrawer() {
  document.querySelector('.sidebar')?.classList.remove('open');
  const ov = document.getElementById('drawer-overlay');
  if (ov) ov.classList.remove('open');
}
function setMobileTab(btn) {
  document.querySelectorAll('#mobile-tabbar .mobile-tab').forEach(b => b.classList.remove('active'));
  if (btn) btn.classList.add('active');
}

function setMobileGuestTab(btn) {
  document.querySelectorAll('#mobile-guest-tabbar .mobile-tab').forEach(b => b.classList.remove('active'));
  if (btn) btn.classList.add('active');
}

async function mobileGuestSubmit() {
  const inp = document.getElementById('mg-url');
  const btn = document.getElementById('mg-btn');
  const url = (inp?.value || '').trim();
  if (!url) { inp?.focus(); return; }
  if (btn) { btn.textContent = '⏳'; btn.disabled = true; btn.style.opacity = '.6'; }
  try {
    const r = await api('POST', '/api/queue/add', { url });
    if (r.ok) {
      inp.value = '';
      toast('Добавлено в очередь');
      const qt = document.getElementById('mgt-queue');
      if (qt) qt.click();
    } else {
      toast(r.detail || r.msg || 'Ошибка', 'var(--red)');
    }
  } catch(e) {
    toast(t('err.generic') + ': ' + e.message, 'var(--red)');
  } finally {
    if (btn) { btn.textContent = '⬇'; btn.disabled = false; btn.style.opacity = '1'; }
  }
}
// Close drawer when a nav item is clicked on mobile
document.querySelectorAll('.nav-item').forEach(item => {
  item.addEventListener('click', () => {
    if (window.innerWidth <= 699) closeMobileDrawer();
  });
});

/* ── Unified download helpers (owner + guest) ── */
async function _triggerDownload(url) {
  // Preflight with ?check=1 — server validates auth/files without building the ZIP.
  // Only one HTTP request triggers actual file transfer (the anchor click below).
  const checkUrl = url + (url.includes('?') ? '&' : '?') + 'check=1';
  try {
    const r = await fetch(checkUrl);
    if (!r.ok) {
      let msg = `HTTP ${r.status}`;
      try { const d = await r.json(); msg = d.error || d.detail || msg; } catch(_) {}
      toast('✗ ' + msg, 'var(--red)');
      return;
    }
  } catch(e) {
    toast('✗ ' + e.message, 'var(--red)');
    return;
  }
  const a = document.createElement('a');
  a.href = url;
  a.style.display = 'none';
  document.body.appendChild(a);
  a.click();
  setTimeout(() => { try { document.body.removeChild(a); } catch(_) {} }, 200);
}

async function isrcUpgrade(taskId) {
  const bar = document.getElementById(`isrc-bar-${taskId}`);
  if (!bar) return;
  const btn = bar.querySelector('button');

  // Remove old results panel if reopening
  const old = bar.querySelector('.isrc-results');
  if (old) { old.remove(); if (btn) { btn.textContent = '🎯 Найти лучше'; btn.disabled = false; } return; }

  const task = (S.queue || []).find(t => t.id === taskId);
  const title  = task?.meta?.title  || task?.title  || '';
  const artist = task?.meta?.artist || task?.artist || '';
  const url    = task?.url || '';

  if (btn) { btn.textContent = '⏳ Ищу…'; btn.disabled = true; }
  try {
    const d = await api('POST', '/api/isrc-upgrade', {url, title, artist});
    if (btn) { btn.textContent = '🎯 Найти лучше'; btn.disabled = false; }

    const SVC = {apple:'🍎',deezer:'🎧',qobuz:'🎵',tidal:'🌊'};
    const QC  = {apple:'#c084a0',deezer:'#3ecfaa',qobuz:'#ffd60a',tidal:'#00d4b3'};
    const panel = document.createElement('div');
    panel.className = 'isrc-results';
    panel.style.cssText = 'margin-top:6px;display:flex;flex-direction:column;gap:4px';

    if (!d.results?.length) {
      panel.innerHTML = `<div style="font-size:11px;color:var(--muted);padding:4px 0">Не найдено на Deezer / Apple Music / Qobuz</div>`;
    } else {
      if (d.isrc) {
        const isrcLine = document.createElement('div');
        isrcLine.style.cssText = 'font-size:10px;color:var(--muted);margin-bottom:2px';
        isrcLine.textContent = `ISRC: ${d.isrc}`;
        panel.appendChild(isrcLine);
      }
      for (const r of d.results) {
        const row = document.createElement('div');
        row.style.cssText = 'display:flex;align-items:center;gap:8px;padding:5px 8px;background:var(--surface2);border-radius:7px;font-size:11px';
        const svcColor = QC[r.service] || 'var(--blue)';
        row.innerHTML = `
          <span style="flex-shrink:0;font-size:14px">${SVC[r.service]||'🎶'}</span>
          <span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(r.title)} — ${esc(r.artist)}">${esc(r.title)}${r.artist?' · <span style="color:var(--muted)">'+esc(r.artist)+'</span>':''}</span>
          <span style="color:${svcColor};font-size:10px;font-weight:700;flex-shrink:0">${esc(r.quality)}</span>
          ${r.match==='exact'?'<span style="color:#22c55e;font-size:10px;flex-shrink:0" title="Точное совпадение по ISRC">✓ISRC</span>':''}
          <button class="isrc-add-btn" style="padding:3px 8px;background:var(--red);color:#fff;border:none;border-radius:5px;font-size:10px;font-weight:700;cursor:pointer;flex-shrink:0">⬇ В очередь</button>
        `;
        const addBtn = row.querySelector('.isrc-add-btn');
        const _url = r.url, _title = r.title, _artist = r.artist, _svc = r.service;
        addBtn.onclick = () => isrcUpgradeAdd(_url, _title, _artist, _svc, addBtn);
        panel.appendChild(row);
      }
    }
    bar.appendChild(panel);
  } catch(e) {
    if (btn) { btn.textContent = '🎯 Найти лучше'; btn.disabled = false; }
    toast('Ошибка поиска: ' + e.message, 'var(--red)');
  }
}

async function isrcUpgradeAdd(url, title, artist, service, btn) {
  if (!url) { toast('Нет URL', 'var(--red)'); return; }
  if (btn) { btn.textContent = '…'; btn.disabled = true; }
  const r = await api('POST', '/api/queue/add', {url, title, artist});
  if (r.ok) toast(`+ ${title} [${service}] → очередь`);
  else toast('Ошибка: ' + (r.detail || r.msg || '?'), 'var(--red)');
  if (btn) { btn.textContent = '✓'; }
}

async function downloadTask(taskId) {
  await _triggerDownload(`/api/download-file?task_id=${encodeURIComponent(taskId)}`);
}

async function downloadTaskZip(taskId) {
  await _triggerDownload(`/api/download-file?task_id=${encodeURIComponent(taskId)}&zip=1`);
}

async function uploadToCloud(taskId, btn) {
  if (!btn) btn = document.querySelector(`.qi[data-id="${taskId}"] .qi-actions .dl-cloud-btn`);
  if (btn) { btn.textContent = '⏳'; btn.disabled = true; }
  try {
    const res = await api('POST', '/api/cloud-upload', { task_id: taskId });
    if (res.ok && res.url) {
      if (btn) {
        btn.textContent = '✓';
        btn.style.color = '#3ecfaa';
        btn.disabled = false;
        btn.title = res.url;
        btn.onclick = () => {
          navigator.clipboard.writeText(res.url).catch(()=>{});
          btn.textContent = '📋';
          setTimeout(() => { btn.textContent = '✓'; }, 1200);
        };
      }
      // Show toast / notification
      const bar = document.getElementById('toast-bar') || (() => {
        const el = document.createElement('div');
        el.id = 'toast-bar';
        el.style.cssText = 'position:fixed;bottom:24px;left:50%;transform:translateX(-50%);background:#1e2533;border:1px solid #3a4460;border-radius:8px;padding:10px 18px;color:#e0e8ff;font-size:13px;z-index:9999;display:flex;align-items:center;gap:10px;max-width:90vw;box-shadow:0 4px 24px #0008';
        document.body.appendChild(el);
        return el;
      })();
      bar.innerHTML = `☁ <span style="word-break:break-all">${esc(res.url)}</span> <button onclick="navigator.clipboard.writeText('${esc(res.url)}').then(()=>{this.textContent='✓'});this.textContent='📋'" style="background:#2a3550;border:1px solid #3a4460;border-radius:4px;color:#7c9fff;cursor:pointer;padding:3px 8px;font-size:12px">📋 Копировать</button>`;
      bar.style.display = 'flex';
      setTimeout(() => { bar.style.display = 'none'; }, 18000);
    } else {
      if (btn) { btn.textContent = '☁'; btn.disabled = false; }
      alert('Ошибка загрузки: ' + (res.detail || res.error || JSON.stringify(res)));
    }
  } catch(e) {
    if (btn) { btn.textContent = '☁'; btn.disabled = false; }
    alert('Ошибка: ' + e);
  }
}


// First-run: ask a forwarding (tester) instance for a display name so the
// developer's Diagnostics tab can tell instances apart. Only when forwarding is on
// (NOT the owner/ingest instance) and no name set yet. Asks once per machine.
async function _maybeAskTelemetryName(){
  try {
    const c = S.config || {};
    if (c['telemetry-ingest-enabled']) return;
    if (String(c['telemetry-forward']) === 'false') return;
    if ((c['telemetry-name']||'').trim()) return;
    if (localStorage.getItem('tlm_named') === '1') return;
    // NOTE: do NOT use window.prompt() — WebView2 (the pywebview backend on
    // Windows, which is what the Ripster.exe launcher uses) suppresses prompt()
    // entirely, so the first-run ask silently never appeared. Use an in-page modal.
    _showFirstRunNameModal();
  } catch(e){}
}

function _showFirstRunNameModal(){
  if(document.getElementById('firstrun-name-modal')) return;
  const modal = document.createElement('div');
  modal.id = 'firstrun-name-modal';
  modal.style.cssText = 'position:fixed;inset:0;z-index:9999;display:flex;align-items:center;justify-content:center;background:rgba(0,0,0,.75);backdrop-filter:blur(4px)';
  modal.innerHTML = `<div style="background:var(--surface,#1c1c1e);border:1px solid rgba(255,255,255,.1);border-radius:16px;padding:24px;width:380px;max-width:90vw">
    <div style="font-size:16px;font-weight:700;color:#f0f0f4;margin-bottom:6px">👋 Добро пожаловать в Ripster</div>
    <div style="font-size:12px;color:var(--muted,#888);margin-bottom:16px">Как тебя подписать для разработчика? Имя/ник поможет понять, чей это Ripster, если пришлёшь диагностику. Можно пропустить — спросим только один раз.</div>
    <input id="firstrun-name-input" type="text" maxlength="48" placeholder="Имя или ник"
      style="width:100%;padding:10px 12px;background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.15);border-radius:9px;color:#f0f0f4;font-size:15px;box-sizing:border-box;outline:none"
      onkeydown="if(event.key==='Enter') _saveFirstRunName()">
    <div style="display:flex;gap:8px;margin-top:14px">
      <button onclick="_saveFirstRunName()" style="flex:1;padding:10px;background:#0a84ff;border:none;border-radius:9px;cursor:pointer;color:#fff;font-weight:600;font-size:13px;font-family:var(--font)">Сохранить</button>
      <button onclick="_skipFirstRunName()" style="padding:10px 16px;background:transparent;border:1px solid rgba(255,255,255,.1);border-radius:9px;cursor:pointer;font-size:13px;color:var(--muted,#888);font-family:var(--font)">Пропустить</button>
    </div>
  </div>`;
  document.body.appendChild(modal);
  setTimeout(()=>{ const i=document.getElementById('firstrun-name-input'); if(i) i.focus(); },50);
}

function _skipFirstRunName(){
  localStorage.setItem('tlm_named','1');
  const m=document.getElementById('firstrun-name-modal'); if(m) m.remove();
}

async function _saveFirstRunName(){
  const inp=document.getElementById('firstrun-name-input');
  const name=((inp&&inp.value)||'').trim().slice(0,48);
  localStorage.setItem('tlm_named','1');
  const m=document.getElementById('firstrun-name-modal'); if(m) m.remove();
  if(!name) return;
  try{
    await api('POST','/api/config', {'telemetry-name': name});
    if(S.config) S.config['telemetry-name']=name;
    toast('Спасибо! Имя сохранено','var(--green)');
  }catch(e){}
}




