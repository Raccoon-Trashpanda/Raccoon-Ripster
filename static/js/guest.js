// ======================================================================
// GUEST / ADMIN (guest sessions, admin links, per-guest activity)
// Extracted from app.js (mechanical split — same global functions, no behaviour
// change). Loaded AFTER app.js in index.html, so it sees S/api/toast/etc.
// ======================================================================

// ── GUEST / ADMIN ─────────────────────────────────────────────

let _guestExpiryInterval = null;

async function checkSessionMode() {
  try {
    const r = await fetch('/api/session-info');
    if (!r.ok) return;
    const d = await r.json();
    if (d.mode === 'guest') {
      S.guestMode = true;
      document.body.classList.add('guest-mode');
      const lbl    = document.getElementById('guest-label');
      const expEl  = document.getElementById('guest-expiry');
      const quota  = document.getElementById('guest-quota');
      if (lbl && d.label) lbl.textContent = d.label;
      if (quota && d.quota) {
        const q = d.quota;
        if (q.type === 'count')
          quota.textContent = `${q.limit - q.used} загр. осталось`;
        else if (q.type === 'time')
          quota.textContent = `⏱ ${d.quota_minutes_left ?? '?'} мин.`;
      }
      // Session expiry countdown
      if (d.expires_at && expEl) {
        const expiresMs = new Date(d.expires_at).getTime();
        const tick = () => {
          const left = Math.max(0, expiresMs - Date.now());
          const h = Math.floor(left / 3600000);
          const m = Math.floor((left % 3600000) / 60000);
          expEl.textContent = left > 0
            ? `· ⏱ ${h}ч ${m}м`
            : '· ссылка истекла';
        };
        tick();
        if (_guestExpiryInterval) clearInterval(_guestExpiryInterval);
        _guestExpiryInterval = setInterval(tick, 30000);
      }
      // Load guest download history + service status into the tokens view
      loadGuestHistory();
      loadGuestSvcStatus();
    } else if (d.mode === 'owner') {
      S.guestMode = false;
      loadAdminLinks();
      loadRemoteStatus();
      loadTunnelStatus();
    }
  } catch(e) {}
}

async function loadGuestSvcStatus() {
  const el = document.getElementById('gt-svc-status-body');
  if (!el) return;
  const labels = {
    apple: {icon:'🍎', name:'Apple Music'},
    deezer: {icon:'🎵', name:'Deezer'},
    qobuz: {icon:'🎼', name:'Qobuz'},
    tidal: {icon:'🔵', name:'Tidal'},
    spotify: {icon:'🟢', name:'Spotify'},
    beatport: {icon:'🎧', name:'Beatport'},
    soundcloud: {icon:'🟠', name:'SoundCloud'},
  };
  try {
    const status = await fetch('/api/services/status').then(r => r.json());
    el.innerHTML = Object.entries(labels).map(([key, {icon, name}]) => {
      const on = status[key];
      return `<span style="display:inline-flex;align-items:center;gap:5px;padding:4px 10px;border-radius:20px;font-size:12px;font-weight:600;background:${on ? 'rgba(74,222,128,.12)' : 'rgba(255,255,255,.05)'};color:${on ? '#4ade80' : 'var(--muted)'};border:1px solid ${on ? 'rgba(74,222,128,.25)' : 'var(--border)'}">
        ${icon} ${name}
        <span style="width:6px;height:6px;border-radius:50%;background:${on ? '#4ade80' : '#555'};flex-shrink:0"></span>
      </span>`;
    }).join('');
  } catch { el.innerHTML = '<span style="font-size:12px;color:var(--muted)">Ошибка загрузки</span>'; }
}

async function loadGuestHistory() {
  const container = document.getElementById('guest-history-list');
  if (!container) return;
  try {
    const r = await fetch('/api/guest/history');
    if (!r.ok) return;
    const d = await r.json();
    const acts = d.activity || [];
    if (!acts.length) {
      container.innerHTML = `<div style="font-size:12px;color:var(--muted);text-align:center;padding:12px">${t('act.empty')||'Нет загрузок'}</div>`;
      return;
    }
    const SVC_ICON = {apple:'🍎',qobuz:'🎵',deezer:'🎧',tidal:'🌊',spotify:'💚',soundcloud:'☁',bbc:'📻'};
    // Summary
    const done = acts.filter(a=>a.status==='done').length;
    const errs = acts.filter(a=>a.status==='error').length;
    const svcs = [...new Set(acts.map(a=>a.service).filter(Boolean))];
    const svcStr = svcs.map(s=>`${SVC_ICON[s]||'🎶'} ${s}`).join(' · ');
    container.innerHTML = `
      <div style="display:flex;gap:14px;font-size:11px;color:var(--muted);margin-bottom:8px;flex-wrap:wrap">
        <span>✓ <b style="color:#22c55e">${done}</b></span>
        ${errs?`<span>✗ <b style="color:var(--red)">${errs}</b></span>`:''}
        ${svcStr?`<span>${svcStr}</span>`:''}
      </div>
      <div style="display:flex;flex-direction:column;gap:3px;max-height:200px;overflow-y:auto">
        ${acts.map(a => {
          const ts  = a.ts ? new Date(a.ts).toLocaleTimeString() : '';
          const svc = (a.service||'').toLowerCase();
          const ico = SVC_ICON[svc] || '🎶';
          const ok  = a.status === 'done';
          const col = ok ? '#22c55e' : 'var(--red)';
          const lbl = a.title || a.url || '—';
          return `<div style="display:flex;align-items:baseline;gap:5px;font-size:11px">
            <span style="color:var(--muted);flex-shrink:0">${ts}</span>
            <span>${ico}</span>
            <span style="color:${col};flex-shrink:0">${ok?'✓':'✗'}</span>
            <span style="color:var(--text);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:260px" title="${esc(lbl)}">${esc(lbl)}</span>
            ${a.quality?`<span style="color:var(--muted);font-size:10px">${esc(a.quality)}</span>`:''}
          </div>`;
        }).join('')}
      </div>`;
  } catch(e) {}
}

function guestDownloadTask(taskId) { downloadTask(taskId); }

async function saveGuestTokens() {
  const body = {
    'media-user-token':       document.getElementById('gt-apple-mut')?.value || '',
    'qobuz-auth-token':       document.getElementById('gt-qobuz-token')?.value || '',
    'qobuz-user-id':          document.getElementById('gt-qobuz-uid')?.value || '',
    'deezer-arl':             document.getElementById('gt-deezer-arl')?.value || '',
    'tidal-token':            document.getElementById('gt-tidal-token')?.value || '',
    'soundcloud-oauth-token': document.getElementById('gt-sc-token')?.value || '',
  };
  const msg = document.getElementById('gt-msg');
  try {
    const r = await api('POST', '/api/guest/tokens', body);
    if (r.ok) {
      if(msg) msg.textContent = '✓ Сохранено';
      _refreshSearchSvcSelect();
    } else {
      if(msg) msg.textContent = r.detail || 'Ошибка';
    }
  } catch(e) { if(msg) msg.textContent = 'Ошибка сети'; }
}

// ── Per-guest live download lamp/bar helpers (admin links view) ────────────
// A queue task carries the session_id that created it; lk.sessions lists a
// guest's live sessions — match them to know if THIS guest is downloading now.
function _guestRunningTask(sidSet) {
  return (S.queue || []).find(tk => {
    if (!sidSet.has(tk.session_id)) return false;
    const st = (tk.status || '').toLowerCase();
    return st === 'running' || st === 'downloading' || st === 'processing';
  }) || null;
}
function _guestTaskPct(task) {
  if (!task) return 0;
  const p = (task.progress != null ? task.progress : (task.pct != null ? task.pct : 0));
  return Math.max(0, Math.min(100, Math.round(p || 0)));
}
// Lightweight live tick: move existing guest bars straight from the queue with no
// refetch (called on every 'progress'). Structural changes — a download starting/
// stopping (bar appears/vanishes) and lamp colour — come from loadAdminLinks on
// 'queue_update'.
function updateGuestDownloadBars() {
  const list = document.getElementById('admin-links-list');
  if (!list || !list.offsetParent) return;            // not mounted/visible → skip
  for (const lk of (S._adminLinks || [])) {
    const fill = document.getElementById(`dlbar-fill-${lk.token}`);
    if (!fill) continue;
    const pct = _guestTaskPct(_guestRunningTask(new Set(lk.sessions || [])));
    fill.style.width = pct + '%';
    const pe = document.getElementById(`dlbar-pct-${lk.token}`);
    if (pe) pe.textContent = pct + '%';
  }
}

async function loadAdminLinks() {
  const container = document.getElementById('admin-links-list');
  if (!container) return;
  try {
    const [linksRes, cfgRes] = await Promise.all([
      fetch('/api/admin/links'),
      fetch('/api/config'),
    ]);
    if (!linksRes.ok) { container.innerHTML = '<div style="font-size:12px;color:var(--muted);text-align:center;padding:16px">Нет доступа</div>'; return; }
    const links = await linksRes.json();
    S._adminLinks = links;   // cache for the lightweight live bar updater
    const freshCfg = cfgRes.ok ? await cfgRes.json() : {};
    if (freshCfg['public-url']) Object.assign(S.config || {}, {'public-url': freshCfg['public-url']});
    if (!links.length) {
      container.innerHTML = `<div style="font-size:12px;color:var(--muted);text-align:center;padding:16px">${t('s.admin_no_links')||'Нет ссылок'}</div>`;
      return;
    }
    const _baseUrl = (freshCfg['public-url'] || S.config?.['public-url'] || '').replace(/\/$/, '') || window.location.origin;
    container.innerHTML = links.map(lk => {
      const active = lk.active && new Date(lk.expires_at) > new Date();
      const exp    = lk.expires_at ? new Date(lk.expires_at).toLocaleString() : '—';
      const q      = lk.quota || {};
      let qtxt     = t('s.admin_unlimited');
      if (q.type === 'count') qtxt = `${q.used||0} / ${q.limit} ${t('s.admin_count')}`;
      if (q.type === 'time')  qtxt = `${q.limit} ${t('s.admin_time')}`;
      // 3-state traffic-light lamp + live download bar. Correlate the queue with
      // this guest via their active session ids (lk.sessions): a task carries the
      // session_id that created it, so we know if THIS guest is downloading now.
      const _sids = new Set(lk.sessions || []);
      const _run  = _guestRunningTask(_sids);
      const _pct  = _guestTaskPct(_run);
      const lamp  = lk.session_count > 0 ? (_run ? '🟢' : '🟡') : '○';
      const lampTxt = lk.session_count > 0
        ? (_run ? (t('act.downloading') || 'качает') : t('act.online'))
        : t('act.offline');
      const onlineDot = `${lamp} <span style="color:var(--muted)">${lampTxt}${lk.session_count > 1 ? ' (' + lk.session_count + ')' : ''}</span>`;
      const dlBar = _run
        ? `<div style="margin-top:6px">
             <div style="display:flex;align-items:center;gap:7px">
               <div style="flex:1;height:5px;border-radius:3px;background:rgba(255,255,255,.08);overflow:hidden">
                 <div id="dlbar-fill-${lk.token}" style="height:100%;width:${_pct}%;background:linear-gradient(90deg,#22c55e,#16a34a);transition:width .4s"></div>
               </div>
               <span id="dlbar-pct-${lk.token}" style="font-size:10px;color:#22c55e;font-weight:700;white-space:nowrap">${_pct}%</span>
             </div>
             <div style="font-size:10px;color:var(--muted);margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(_run.title || _run.url || '')}</div>
           </div>`
        : '';
      const statusColor = active ? '#22c55e' : 'var(--muted)';
      const tok = lk.token;
      const guestUrl = `${_baseUrl}/guest/${tok}`;
      return `<div style="background:var(--surface);border:1px solid var(--border);border-radius:10px;overflow:hidden;font-size:12px">
        <div style="display:flex;align-items:center;gap:8px;padding:9px 12px">
          <div style="width:7px;height:7px;border-radius:50%;background:${statusColor};flex-shrink:0"></div>
          <div style="flex:1;min-width:0">
            <div style="font-weight:700;color:var(--text)">${esc(lk.label||'—')}
              <span style="font-weight:400;font-size:11px;color:var(--muted);margin-left:6px">${onlineDot}</span>
            </div>
            <div style="color:var(--muted);font-size:11px;margin-top:1px">
              ${active?t('admin.until')+' '+exp:t('admin.expired')} · ${qtxt} · ${lk.token_mode==='owner'?t('s.admin_owner_tok'):t('s.admin_guest_tok')}
            </div>
            ${dlBar}
            <!-- URL row — always visible, click to copy -->
            <div style="display:flex;align-items:center;gap:6px;margin-top:5px">
              <code style="flex:1;min-width:0;font-size:10.5px;font-family:var(--mono);color:${active?'#c084fc':'var(--muted)'};background:rgba(0,0,0,.18);padding:3px 7px;border-radius:5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(guestUrl)}</code>
              <button onclick="navigator.clipboard.writeText('${escJ(guestUrl)}').then(()=>toast(t('toast.copied'),'var(--green)')).catch(()=>{const ta=document.createElement('textarea');ta.value='${escJ(guestUrl)}';document.body.appendChild(ta);ta.select();document.execCommand('copy');document.body.removeChild(ta);toast(t('toast.copied'),'var(--green)')})"
                style="padding:3px 9px;border-radius:6px;border:1px solid rgba(175,82,222,.3);background:rgba(175,82,222,.12);color:#c084fc;font-size:11px;font-weight:600;cursor:pointer;flex-shrink:0;white-space:nowrap">
                ⎘ Копировать
              </button>
            </div>
          </div>
          <div style="display:flex;flex-direction:column;gap:4px;flex-shrink:0;align-items:flex-end">
            <div style="display:flex;gap:5px">
              <button onclick="showGuestActivity('${tok}')"
                id="act-btn-${tok}"
                style="padding:4px 9px;border-radius:7px;border:1px solid var(--border);background:var(--surface2);color:var(--muted);font-size:11px;cursor:pointer">
                ${t('act.show')||'▾ Активность'}
              </button>
              ${active?`<button onclick="revokeGuestLink('${tok}')"
                style="padding:4px 9px;border-radius:7px;border:1px solid rgba(252,60,68,.25);background:rgba(252,60,68,.08);color:var(--red);font-size:11px;cursor:pointer">
                ${t('admin.revoke')}
              </button>`:''}
            </div>
            ${active?`<button onclick="toggleTokenMode('${tok}','${lk.token_mode==='owner'?'guest':'owner'}')"
              style="padding:3px 9px;border-radius:7px;border:1px solid var(--border);background:var(--surface2);color:var(--muted);font-size:10px;cursor:pointer;white-space:nowrap">
              ${lk.token_mode==='owner'?t('admin.to_guest'):t('admin.to_owner')}
            </button>`:''}
          </div>
        </div>
        <div id="act-panel-${tok}" style="display:none;border-top:1px solid var(--border);padding:10px 12px;background:var(--bg)">
          <div style="font-size:11px;color:var(--muted)">${t('act.loading')}</div>
        </div>
      </div>`;
    }).join('');
  } catch(e) {
    container.innerHTML = `<div style="font-size:12px;color:var(--red);text-align:center;padding:16px">${t('act.error_load')}</div>`;
  }
}

async function showGuestActivity(token) {
  const panel = document.getElementById(`act-panel-${token}`);
  const btn   = document.getElementById(`act-btn-${token}`);
  if (!panel) return;
  if (panel.style.display !== 'none') {
    panel.style.display = 'none';
    if (btn) btn.textContent = t('act.show')||'▾ Активность';
    return;
  }
  panel.style.display = 'block';
  if (btn) btn.textContent = t('act.hide')||'▴ Скрыть';
  try {
    const r = await fetch(`/api/admin/links/${token}/activity`);
    const d = await r.json();
    const acts = d.activity || [];
    const exp  = d.expires_at ? new Date(d.expires_at) : null;
    const now  = Date.now();
    const timeLeft = exp ? Math.max(0, exp.getTime() - now) : 0;
    const h = Math.floor(timeLeft / 3600000);
    const m = Math.floor((timeLeft % 3600000) / 60000);
    const expStr = exp
      ? (timeLeft > 0 ? `⏱ ${h}ч ${m}м` : 'истекла')
      : '';
    const q = d.quota || {};
    let qStr = '';
    if (q.type === 'count') qStr = ` · ${q.used||0}/${q.limit} загр.`;
    else if (q.type === 'time') qStr = ` · лимит ${q.limit} мин.`;
    const sessStr = d.session_count > 0
      ? `<span style="color:#22c55e">● ${d.session_count} онлайн</span>`
      : `<span style="color:var(--muted)">○ офлайн</span>`;

    const SVC_ICON = {apple:'🍎',qobuz:'🎵',deezer:'🎧',tidal:'🌊',spotify:'💚',soundcloud:'☁',bbc:'📻'};
    const done = acts.filter(a=>a.status==='done'||a.event==='dl_ok').length;
    const errs = acts.filter(a=>a.status==='error'||a.event==='dl_error'||a.event==='add_blocked').length;
    const svcs = [...new Set(acts.map(a=>a.service).filter(Boolean))];
    const svcStr = svcs.map(s=>`${SVC_ICON[s]||'🎶'} ${s}`).join(' · ');

    const header = `<div style="display:flex;gap:12px;font-size:11px;flex-wrap:wrap;margin-bottom:6px;padding-bottom:6px;border-bottom:1px solid var(--border)">
      ${sessStr}
      ${expStr?`<span style="color:var(--muted)">${expStr}</span>`:''}
      ${qStr?`<span style="color:var(--muted)">${qStr}</span>`:''}
      ${done?`<span>✓ <b style="color:#22c55e">${done}</b></span>`:''}
      ${errs?`<span>✗ <b style="color:var(--red)">${errs}</b></span>`:''}
      ${svcStr?`<span style="color:var(--muted)">${svcStr}</span>`:''}
    </div>`;

    if (!acts.length) {
      panel.innerHTML = header + `<div style="font-size:11px;color:var(--muted)">${t('act.empty')||'Нет загрузок'}</div>`;
      return;
    }
    const _BR = {'quota_exceeded':'квота','rate_limit':'лимит запросов'};
    const _DR = {'task_not_found':'нет задачи','not_finished':'не готово',
                 'access_denied':'нет доступа','files_missing':'нет файлов','no_audio_files':'нет аудио'};
    panel.innerHTML = header + `<div style="display:flex;flex-direction:column;gap:4px;max-height:200px;overflow-y:auto">` +
      acts.slice().reverse().map(a => {
        const ts     = a.ts ? new Date(a.ts).toLocaleTimeString() : '';
        const svc    = (a.service||'').toLowerCase();
        const svcIco = SVC_ICON[svc] || '';
        let evtIco, col, lbl;
        if (a.event === 'queued') {
          evtIco = '⬇'; col = 'var(--blue)';
          lbl = a.url || '—';
        } else if (a.event === 'add_blocked') {
          evtIco = '🚫'; col = 'var(--red)';
          lbl = (_BR[a.reason]||a.reason||'заблокировано') + (a.url ? ' · '+a.url : '');
        } else if (a.event === 'dl_ok') {
          evtIco = '📥'; col = '#22c55e';
          lbl = a.filename || a.title || '—';
        } else if (a.event === 'dl_error') {
          evtIco = '❌'; col = 'var(--red)';
          lbl = (_DR[a.reason]||a.reason||'ошибка') + (a.title ? ' — '+a.title : '');
        } else if (a.status === 'done') {
          evtIco = '✓'; col = '#22c55e';
          lbl = a.title || a.url || '—';
        } else {
          evtIco = '✗'; col = 'var(--red)';
          lbl = a.title || a.url || '—';
        }
        return `<div style="display:flex;align-items:baseline;gap:6px;font-size:11px">
          <span style="color:var(--muted);flex-shrink:0">${ts}</span>
          ${svcIco?`<span>${svcIco}</span>`:''}
          <span style="flex-shrink:0">${evtIco}</span>
          <span style="color:${col};overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:220px" title="${esc(lbl)}">${esc(lbl)}</span>
          ${a.quality?`<span style="color:var(--muted);font-size:10px">${esc(a.quality)}</span>`:''}
        </div>`;
      }).join('') + `</div>`;
  } catch(e) {
    panel.innerHTML = `<div style="font-size:11px;color:var(--red)">Ошибка загрузки активности</div>`;
  }
}
