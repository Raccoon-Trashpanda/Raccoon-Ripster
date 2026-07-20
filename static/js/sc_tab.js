// ======================================================================
// SoundCloud / Lucida tab UI
// Extracted from app.js (mechanical split — same global functions, no behaviour
// change). Loaded AFTER app.js in index.html, so it sees S/api/toast/etc.
// ======================================================================

// ── SoundCloud / Lucida ───────────────────────────────────────────
async function loadBeatportStatus() {
  const cloneSec    = document.getElementById('bp-clone-section');
  const reinstallSec= document.getElementById('bp-reinstall-section');
  const installBar  = document.getElementById('bp-install-status');
  const installLbl  = document.getElementById('bp-install-label');
  const r = await api('GET', '/api/beatport/status').catch(()=>null);
  if(r && r.module_installed) {
    if(installBar)   installBar.style.display = 'flex';
    if(installLbl)   installLbl.textContent = '✓ '+t('bp.installed');
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
  if(btn){ btn.disabled=true; btn.textContent='⏳ '+t('setup.st_installing'); }
  const nav = document.querySelector('.nav-item[data-view="setup"]');
  if(nav) showView('setup', nav);   // install streams to the Setup console now
  toast(t('sc.inst_bp'),'#01f49c');
  try {
    await api('POST', '/api/setup/beatport');
  } catch(e) {
    toast(t('t.error_c')+e.message,'var(--red)');
    if(btn){ btn.disabled=false; btn.textContent='⬇ '+t('bp.auto_install'); }
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
      toast(t('sc.bp_ok'),'#01f49c');
    } else if(attempts >= 12) {
      clearInterval(poll);
      if(btn){ btn.disabled=false; btn.textContent='⬇ '+t('bp.auto_install'); }
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
  toast(t('rl.all_seen')+' &nbsp;<button onclick="_relUndoSeen()" style="padding:2px 9px;border-radius:6px;border:1px solid var(--orange);background:transparent;color:var(--orange);font-size:11px;font-weight:700;cursor:pointer;font-family:var(--font)">↩ '+t('rl.undo')+'</button>', 'var(--green)', '', 9000);
  _applyRelFilter(false);
}
function _relUndoSeen() {
  if (!_relSeenUndo) return;
  _relSeen = new Set(_relSeenUndo);
  _relSeenUndo = null;
  _relSaveJSON(_REL_SEEN_KEY, [..._relSeen]);
  toast(t('sc.cancel_seen'), 'var(--orange)', '', 3000);
  _applyRelFilter(false);
}
// Full reset — un-hide everything (recover from an accidental "mark all seen").
function resetRelSeen() {
  _relSeen = new Set();
  _relSeenUndo = null;
  _relSaveJSON(_REL_SEEN_KEY, []);
  toast(t('sc.reset'), 'var(--green)', '', 3000);
  _applyRelFilter(false);
}

function _relDateLabel(d) {
  if (!d) return t('rl.no_date');
  const today = new Date(); today.setHours(0,0,0,0);
  const dt = new Date(d + 'T00:00:00');
  if (isNaN(dt)) return d;
  const diff = Math.round((today - dt) / 86400000);
  if (diff === 0) return t('rl.today');
  if (diff === 1) return t('rl.yesterday');
  const full = dt.toLocaleDateString(_dateLoc(), { day:'numeric', month:'long', year:'numeric' });
  if (diff > 1 && diff < 7) {
    const wd = dt.toLocaleDateString(_dateLoc(), { weekday:'long' });
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
      mk('all', t('ck.f_all'), 'var(--text)') +
      mk('new', '🆕 '+t('rl.new_word') + (newCount ? ' ' + newCount : ''), 'var(--green)') +
      mk('fav', '★ '+t('rl.fav_word') + (favCount ? ' ' + favCount : ''), 'var(--orange)');
  }
  const tc = document.getElementById('rel-type-chips');
  if (tc) {
    const order = ['album','single','ep','compilation','appears_on','live'];
    const lbl   = {album:t('ck.f_albums'),single:t('ck.f_singles'),ep:'EP',compilation:t('ck.f_comps'),appears_on:t('rl.appears'),live:'Live'};
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
        <span style="font-size:10px;color:var(--muted2);font-family:var(--mono)">${buf.length} ${t('w.rel_abbr')}</span>
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
          <div>${ti('rl.none_all_seen',{n:totalData})}</div>
          <div style="display:flex;gap:9px;flex-wrap:wrap;justify-content:center">
            ${btn(t('rl.show_all'), "setRelView('all')", 'var(--text)')}
            ${btn('↩ '+t('rl.reset_seen'), 'resetRelSeen()', 'var(--orange)')}
          </div></div>`;
      } else {
        empty.textContent = _relView === 'fav' ? t('rl.no_fav')
                          : _relView === 'new' ? t('rl.no_new')
                          : t('rl.none_period');
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
  _relHydrateQualitySelects();
}

function _relUpdateLoadMore(total) {
  const btn   = document.getElementById('rel-load-more');
  const count = document.getElementById('rel-load-more-count');
  if(!btn) return;
  const remaining = total - _relShowing;
  if(remaining > 0) {
    if(count) count.textContent = ti('rl.more_n',{n:remaining});
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
  if(qSt)  qSt.textContent  = hasQobuz ? '✓ '+t('rl.has_token') : '⚠ '+t('rl.no_token');
  if(tSt)  tSt.textContent  = !hasTidal ? '⚠ '+t('rl.no_token') : (tidalExp ? '⚠ '+t('rl.token_expired') : '✓ '+t('rl.has_token'));
  // Spotify: use cached status from S._spStatus set by loadSpotifyStatus
  if(spSt) {
    const ss = S._spStatus;
    if(!hasSpDc) spSt.textContent = '⚠ '+t('rl.not_authed');
    else if(ss && ss.sp_dc_expired) spSt.textContent = '⚠ '+t('rl.spdc_expired');
    else if(ss && ss.connected) spSt.textContent = '✓ sp_dc';
    else spSt.textContent = hasSpDc ? '? '+t('rl.checking_word') : '⚠ '+t('rl.not_authed');
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
  const bg = document.getElementById('rel-bg-scan');
  if(bg) bg.checked = !!c['spotify-bg-scan'];
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
  const dt = rel.date ? new Date(rel.date + 'T00:00:00').toLocaleDateString(_dateLoc(), {day:'numeric',month:'short',year:'numeric'}) : '';
  const svcColors = {spotify:'#1db954', qobuz:'#1870f5', tidal:'#00d4b3', apple:'var(--red)', deezer:'#a238ff'};
  const svcClr  = svcColors[rel.service] || 'var(--muted)';
  const typeMap = {album:'ALBUM', single:'SINGLE', ep:'EP', compilation:t('rl.comp_badge'), appears_on:t('rl.appears_badge'), live:'LIVE'};
  const typeClr = rel.type === 'single' ? 'var(--orange)' : (rel.type === 'album' ? '#1db954' : 'var(--muted2)');
  const typeTag = typeMap[rel.type] || (rel.type || '').toUpperCase();
  const hiresBadge = rel.hires ? '<span style="font-size:8px;padding:1px 4px;border-radius:3px;background:rgba(255,214,10,.15);color:#ffd60a;font-weight:700;margin-left:3px">HI-RES</span>' : '';
  const uid   = _relUID(rel);
  const isNew = _relIsNew(rel);
  const isFav = _relIsFav(rel);
  const isLive = !!rel.live;   // caught by the instant queryWhatsNewFeed hook, not the per-artist crawl
  const baseBorder = isNew ? 'rgba(62,207,170,.55)' : 'var(--border)';
  const isSpotify = rel.service === 'spotify';
  const qualSelect = isSpotify ? '' : `
        <select class="rel-q-select" data-svc="${esc(rel.service)}" title="${t('rl.quality_label')}"
          onclick="event.stopPropagation()" onchange="event.stopPropagation();_relSetQuality(this)"
          style="width:100%;margin-top:6px;padding:3px 6px;background:var(--surface2);border:1px solid var(--border);border-radius:6px;font-size:10px;color:var(--muted);cursor:pointer;outline:none">
          <option value="${esc(resolveQuality(rel.service))}">${esc(resolveQuality(rel.service))}</option>
        </select>`;
  return `<div class="rel-card${isNew ? ' rel-card-new' : ''}" style="background:var(--surface);border:1px solid ${baseBorder};border-radius:10px;overflow:hidden;transition:border-color .15s" onmouseover="this.style.borderColor='${svcClr}'" onmouseout="this.style.borderColor='${baseBorder}'">
    <div style="position:relative">
      ${rel.cover
        ? `<img src="${esc(rel.cover)}" data-lightbox style="width:100%;aspect-ratio:1;object-fit:cover;display:block;cursor:zoom-in" loading="lazy"/>`
        : `<div style="width:100%;aspect-ratio:1;background:rgba(255,255,255,.04);display:flex;align-items:center;justify-content:center;font-size:32px;color:var(--muted)">♪</div>`}
      <button onclick="event.stopPropagation();playRelease('${esc(rel.service)}','${escJ(rel.url)}','${escJ(rel.title)}','${escJ(rel.artist)}','${escJ(rel.cover||'')}')" title="${t('rl.listen')}"
        style="position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);width:54px;height:54px;border-radius:50%;background:rgba(0,0,0,.5);border:2px solid rgba(255,255,255,.85);color:#fff;font-size:21px;line-height:1;cursor:pointer;display:flex;align-items:center;justify-content:center;padding-left:4px;backdrop-filter:blur(3px);transition:transform .12s,background .12s;z-index:2" onmouseover="this.style.transform='translate(-50%,-50%) scale(1.12)';this.style.background='rgba(0,0,0,.7)'" onmouseout="this.style.transform='translate(-50%,-50%)';this.style.background='rgba(0,0,0,.5)'">▶</button>
      <div style="position:absolute;top:6px;left:6px"><span style="font-size:9px;padding:2px 5px;border-radius:4px;background:rgba(0,0,0,.72);color:${svcClr};font-weight:700;backdrop-filter:blur(4px)">${(rel.service||'?').toUpperCase()}</span></div>
      <div style="position:absolute;top:6px;right:6px"><span style="font-size:9px;padding:2px 5px;border-radius:4px;background:rgba(0,0,0,.72);color:${typeClr};font-weight:700;backdrop-filter:blur(4px)">${typeTag}</span></div>
      ${isNew ? `<div style="position:absolute;bottom:6px;left:6px"><span style="font-size:8px;padding:2px 6px;border-radius:4px;background:var(--green);color:#06281f;font-weight:800;letter-spacing:.4px">${t('rl.new_badge')}</span></div>` : ''}
      ${isLive ? `<div style="position:absolute;bottom:6px;right:6px" title="${t('rl.live_title')}"><span class="rel-live-badge"><span class="rel-live-dot"></span>${t('rl.live_badge')}</span></div>` : ''}
    </div>
    <div style="padding:8px 10px">
      <div style="font-size:12px;font-weight:600;color:var(--text);overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(rel.title)}">${esc(rel.title)}${hiresBadge}</div>
      <div style="font-size:11px;color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(rel.artist)}">${esc(rel.artist)}</div>
      ${rel.label ? `<div style="font-size:10px;color:var(--muted);margin-top:1px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;opacity:.7" title="${esc(rel.label)}">${esc(rel.label)}</div>` : ''}
      <div style="font-size:10px;color:var(--muted);margin-top:2px">${dt}${rel.tracks ? ' · ' + rel.tracks + ' ' + t('p.trk_abbr') : ''}</div>
      ${qualSelect}
      <div style="display:flex;flex-wrap:wrap;gap:5px;margin-top:7px">
        <button onclick="downloadRelease(this,'${esc(rel.service)}','${escJ(rel.url)}','${escJ(rel.title)}','${escJ(rel.artist)}')"
          style="flex:1 1 100%;padding:5px 0;background:rgba(192,132,160,.12);border:1px solid rgba(192,132,160,.2);border-radius:7px;font-size:10px;font-weight:700;color:var(--red);cursor:pointer;font-family:var(--font)">${t('btn.download')}</button>
        <button onclick="smartDownloadRelease(this,'${escJ(rel.url)}','${escJ(rel.title)}','${escJ(rel.artist)}')"
          style="padding:5px 8px;background:transparent;border:1px solid rgba(255,214,10,.35);border-radius:7px;font-size:11px;color:#ffd60a;cursor:pointer;font-family:var(--font)" title="${t('rl.auto_src')}">⚡</button>
        <button onclick="toggleRelFav('${escJ(uid)}')" style="padding:5px 8px;background:transparent;border:1px solid ${isFav?'var(--orange)':'var(--border)'};border-radius:7px;font-size:11px;color:${isFav?'var(--orange)':'var(--muted)'};cursor:pointer;font-family:var(--font)" title="${isFav?t('sc2.unfav'):t('sc2.fav')}">${isFav?'★':'☆'}</button>
        <button onclick="navigator.clipboard.writeText('${escJ(rel.url)}');toast(t('toast.link_copied'))" style="padding:5px 8px;background:transparent;border:1px solid var(--border);border-radius:7px;font-size:10px;color:var(--muted);cursor:pointer;font-family:var(--font)" title="${t('ck.copy_link')}">⎘</button>
        <a href="${esc(rel.url)}" target="_blank" style="padding:5px 8px;background:transparent;border:1px solid var(--border);border-radius:7px;font-size:10px;color:var(--muted);text-decoration:none;display:flex;align-items:center" title="${t('ck.open_on')} ${rel.service}">↗</a>
      </div>
    </div>
  </div>`;
}

async function downloadRelease(btn, service, url, title, artist) {
  if(service === 'spotify') {
    _showSpotifyChoiceToast(url, S.config['quality'] || 'alac');
    return;
  }
  const card    = btn && btn.closest ? btn.closest('.rel-card') : null;
  const sel     = card ? card.querySelector('.rel-q-select') : null;
  const quality = (sel && sel.value) ? sel.value : resolveQuality(service);
  const r = await api('POST', '/api/queue/add', {url, quality, title, artist});
  if(r.ok) toast('+ '+title+' → '+t('q.queue_word'));
  else     toast(t('t.error_c') + (r.detail || '?'), 'var(--red)');
}

// Per-card quality picker → writes straight to the same global per-service
// quality setting Settings/Queue/etc use, so "remembering" a choice here is
// just the ordinary saveSetting() persistence — one source of truth, no
// shadow radar-only state to keep in sync.
function _relSetQuality(sel) {
  const svc = sel.dataset.svc;
  const keyMap = {
    qobuz: 'qobuz-quality', tidal: 'tidal-quality', deezer: 'deezer-quality',
    beatport: 'beatport-quality', yandex: 'yandex-quality', amazon: 'amazon-quality',
    apple: 'quality',
  };
  saveSetting(keyMap[svc] || 'quality', sel.value);
}

// Selects render with just the currently-resolved quality as a single option
// (cheap, synchronous, no per-card network call) — this upgrades them to the
// full per-service option list once, using the same cached _qualitiesForEngine
// the rest of the app already warms (Settings/Queue).
async function _relHydrateQualitySelects() {
  const selects = document.querySelectorAll('#releases-grid .rel-q-select');
  if (!selects.length) return;
  const bySvc = {};
  selects.forEach(sel => {
    const svc = sel.dataset.svc;
    if (!bySvc[svc]) bySvc[svc] = [];
    bySvc[svc].push(sel);
  });
  for (const svc of Object.keys(bySvc)) {
    let list;
    try { list = await _qualitiesForEngine(svc); } catch (e) { continue; }
    if (!Array.isArray(list) || !list.length) continue;
    const cur = resolveQuality(svc);
    const optsHtml = list.map(q =>
      `<option value="${esc(q.id)}" ${q.id === cur ? 'selected' : ''}>${esc(q.badge || q.label || q.id)}</option>`
    ).join('');
    bySvc[svc].forEach(sel => {
      const wasFocused = document.activeElement === sel;
      sel.innerHTML = optsHtml;
      if (wasFocused) sel.focus();
    });
  }
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
      toast(t('sc.no_isrc'), 'var(--red)');
      return;
    }
    const c = r.chosen;
    const svcName = {apple:'Apple', qobuz:'Qobuz', tidal:'Tidal', deezer:'Deezer'}[c.service] || c.service;
    const regionTag = c.region ? ` ${c.region.toUpperCase()}` : '';
    const q = c.quality || resolveQuality(c.service);
    const add = await api('POST', '/api/queue/add', {url: c.url, quality: q, title: c.title || title, artist: c.artist || artist});
    if(add.ok) toast(`⚡ ${svcName}${regionTag} → ${t('q.queue_word')}`, 'var(--green)');
    else       toast(t('t.error_c') + (add.detail || '?'), 'var(--red)');
  } catch(e) {
    toast(t('sc.autosrc'), 'var(--red)');
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
      toast(t('t.error_c') + (detail.slice(0, 120) || r.status), 'var(--red)');
      return;
    }
    const d = await r.json();
    if (!d.ok || !d.tracks?.length) {
      toast(t('sc.no_tracks'), 'var(--red)');
      return;
    }
    let tracks = d.tracks;
    // Spotify has no /api/stream proxy of its own (streaming its audio through
    // our backend would risk the account's token) — the backend already
    // resolved each track by ISRC to a Deezer/Qobuz copy that CAN actually be
    // streamed. Drop tracks without a match; the player still shows "Spotify",
    // never the real source, per the whole point of this workaround.
    if (service === 'spotify') {
      const total = tracks.length;
      tracks = tracks.filter(tr => tr.playable_service && tr.playable_id != null);
      if (!tracks.length) {
        // Not a fetch failure — the release itself just isn't on Deezer under
        // this UPC (small/regional label, Spotify-exclusive, etc). Say so,
        // rather than the generic "could not fetch tracks".
        toast(t('rl.no_preview_match'), 'var(--orange)', '', 5000);
        return;
      }
      if (tracks.length < total) {
        toast(ti('rl.preview_partial', {n: tracks.length, total}), 'var(--muted)', '', 3000);
      }
    }
    _setupAudioEvents();
    Preview.queue = tracks.map(tr => ({
      service:        service,
      id:             String(tr.id),
      _streamService: tr.playable_service || service,
      _streamId:      tr.playable_id != null ? String(tr.playable_id) : String(tr.id),
      title:     tr.title,
      artist:    tr.artist || artist,
      cover:     tr.artwork || cover || '',
      permalink: tr.url || url,
      full:      true,
      label:     `${service[0].toUpperCase()+service.slice(1)} · ${title}`,
      posKey:    `${service}:${tr.id}`,
    }));
    Preview.idx = 0;
    toast(`▶ ${title}: ${tracks.length} ${t('p.trk_abbr')}`, 'var(--green)', '', 2500);
    await _playPreviewAt(0);
  } catch (e) {
    console.error('[playRelease]', e);
    toast(t('t.error_c') + e.message, 'var(--red)');
  }
}

