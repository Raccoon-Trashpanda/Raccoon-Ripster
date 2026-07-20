// Ripster SoundCloud module — tab UI, search/play/download, MixesDB,
// sign-in, Lucida install. Loaded AFTER app.js + player.js.

// ── SoundCloud tab ─────────────────────────────────────────────────────────
let _scResults = [];
let _scPrevSearch = null;   // {q, kind, results} saved when browsing into a channel

// ── SC stream URL cache (instant play like YT/SC) ─────────────────────────
// Stream URLs expire in ~30 min on SC's CDN. We evict after 25 min to stay safe.
const _scStreamCache = new Map();
const _SC_CACHE_TTL  = 25 * 60 * 1000;

function _scCacheGet(id) {
  const e = _scStreamCache.get(String(id));
  if (!e) return null;
  if (Date.now() - e.ts > _SC_CACHE_TTL) { _scStreamCache.delete(String(id)); return null; }
  return e;
}
function _scCacheSet(id, data) {
  _scStreamCache.set(String(id), { ...data, ts: Date.now() });
}

// In-flight guard — prevents duplicate concurrent prewarm requests.
const _scPrewarmInflight = new Set();

// DRM preference is detected in player.js (loaded first) and stored as window._scDrmPrefer.
function _scDrmPreferGet() { return window._scDrmPrefer || 'ctr'; }

// ── Widevine L3 device upload + status + peer wrapper ─────────────────────

// Status chips carry their own tinted background + border (not a bare text
// colour on a hardcoded rgba(0,0,0,.25) box) so they stay readable on BOTH
// themes — a fixed dark box washed out the mint-green "OK" text on light
// theme (green-on-muted-tan ≈ unreadable). Same pattern as the AMD-wrapper
// status widget in cookies_ui.js.
const _SC_CHIP = {
  muted: { c: 'var(--muted)', bg: 'rgba(128,128,128,.10)', bd: 'var(--border)' },
  ok:    { c: '#1f9d78',      bg: 'rgba(62,207,170,.14)',  bd: 'rgba(62,207,170,.35)' },
  warn:  { c: '#b9760a',      bg: 'rgba(239,159,39,.14)',  bd: 'rgba(239,159,39,.35)' },
  err:   { c: '#a8506d',      bg: 'rgba(192,132,160,.16)', bd: 'rgba(192,132,160,.35)' },
};
function _scChip(el, kind) {
  const s = _SC_CHIP[kind] || _SC_CHIP.muted;
  el.style.color = s.c;
  el.style.background = s.bg;
  el.style.border = '1px solid ' + s.bd;
}

async function _scCheckWvdWrapper() {
  const inp = document.getElementById('s-sc-wvd-wrapper');
  const out = document.getElementById('sc-wvd-wrapper-status');
  if (!out) return;
  const url = (inp?.value || '').trim();
  if (!url) {
    out.textContent = t('sc.wvdw_unset');
    out.style.color = 'var(--muted)';
    return;
  }
  out.textContent = t('sc.wvdw_probing');
  out.style.color = 'var(--muted)';
  try {
    const r = await fetch('/api/wv-wrapper/probe?url=' + encodeURIComponent(url));
    const d = await r.json();
    if (d.ready) {
      out.textContent = ti('sc.wvdw_ready', { url });
      out.style.color = '#1f9d78';
    } else {
      out.textContent = ti('sc.wvdw_fail', { err: d.error || t('sc.wvdw_noresp') });
      out.style.color = '#a8506d';
    }
  } catch (e) {
    out.textContent = '✗ ' + e.message;
    out.style.color = '#a8506d';
  }
}

async function _scCheckWvd() {
  const out = document.getElementById('sc-wvd-status');
  if (!out) return;
  out.textContent = t('sc.wvd_checking');
  _scChip(out, 'muted');
  try {
    const r = await fetch('/api/soundcloud/wvd-status');
    const d = await r.json();
    if (!d.installed) {
      out.textContent = t('sc.wvd_missing');
      _scChip(out, 'err');
      return;
    }
    if (d.valid) {
      out.textContent = ti('sc.wvd_ok', { size: d.size });
      _scChip(out, 'ok');
    } else {
      out.textContent = ti('sc.wvd_invalid', { err: d.error || '?' });
      _scChip(out, 'warn');
    }
  } catch (e) {
    out.textContent = t('sc.wvd_err') + e.message;
    _scChip(out, 'err');
  }
}

async function _scUploadWvd(input) {
  const out = document.getElementById('sc-wvd-status');
  const file = input?.files?.[0];
  if (!file) return;
  if (file.size > 100000 || file.size < 100) {
    if (out) { out.textContent = ti('sc.wvd_badsize', { size: file.size }); _scChip(out, 'err'); }
    return;
  }
  if (out) { out.textContent = t('sc.wvd_uploading'); _scChip(out, 'muted'); }
  try {
    const buf = await file.arrayBuffer();
    const bytes = new Uint8Array(buf);
    let bin = ''; for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
    const b64 = btoa(bin);
    const r = await fetch('/api/soundcloud/upload-wvd', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({content_b64: b64}),
    });
    const d = await r.json();
    if (d.ok) {
      if (out) { out.textContent = ti('sc.wvd_installed', { size: d.size, path: d.path }); _scChip(out, 'ok'); }
      toast(t('sc.wvd_toast'), 'var(--green)');
      input.value = '';
    } else {
      if (out) { out.textContent = '✗ ' + (d.detail || t('sc.wvd_err_generic')); _scChip(out, 'err'); }
    }
  } catch (e) {
    if (out) { out.textContent = '✗ ' + e.message; _scChip(out, 'err'); }
  }
}

// Auto-load status when SC settings tab opens
document.addEventListener('DOMContentLoaded', () => {
  // Run once after a tick — settings.html may not be rendered yet
  setTimeout(_scCheckWvd, 1500);
});

// Prewarm the FairPlay cert at page load so the FIRST mix click doesn't burn a
// network round-trip *inside* the user-gesture window — that was the root cause
// of "first click does nothing, second click works" on iOS: the cert fetch
// awaited inside _scDrmHls broke the user-gesture chain so fpsEl.play() got
// blocked with "AbortError: The operation was aborted." On the second click the
// cert was already cached, gesture chain stayed intact, play() proceeded.
(function _prewarmFpsCert() {
  if (window._fpsCertCache instanceof Uint8Array) return;
  if (!('WebKitMediaKeys' in window)) return;   // not iOS Safari, no FPS path
  fetch('/api/sc_fps_cert')
    .then(r => r.ok ? r.arrayBuffer() : null)
    .then(buf => { if (buf) { window._fpsCertCache = new Uint8Array(buf);
                              console.log('[FPS] cert prewarmed', window._fpsCertCache.length, 'B'); } })
    .catch(() => {});
})();

// Update the quality badge on a visible SC tile after prewarm resolves.
function _scUpdateQualityBadge(id, format) {
  const el = document.getElementById(`sc-fmt-${id}`);
  if (!el) return;
  const fmtLabel = { 'drm-hls-cbc': ['DRM·CBC', '#888'], 'drm-hls-ctr': ['DRM·CTR', '#888'], 'drm-hls': ['DRM', '#888'], 'hls': ['HLS', '#1db954'], mp3: ['MP3', '#1870f5'], aac: ['AAC·HQ', '#1db954'] };
  const [label, color] = fmtLabel[format] || [format.toUpperCase(), '#aaa'];
  el.textContent = label;
  el.style.color = color;
  el.style.display = '';
}

// Resolve and cache a single SC track stream URL in the background.
// Called on hover (debounced) and after _scRender() for the top-N results.
async function scPrewarmTrack(id, title, artist) {
  const sid = String(id);
  if (_scCacheGet(sid) || _scPrewarmInflight.has(sid)) return;
  _scPrewarmInflight.add(sid);
  try {
    const resp = await fetch(
      `/api/stream/soundcloud/${sid}` +
      `?name=${encodeURIComponent(title || '')}` +
      `&artist=${encodeURIComponent(artist || '')}` +
      `&prefer=${_scDrmPreferGet()}`
    );
    if (!resp.ok) return;
    const ct = (resp.headers.get('content-type') || '').toLowerCase();
    if (ct.includes('application/json')) {
      const r = await resp.json();
      if (r.url) {
        _scCacheSet(sid, { url: r.url, format: r.format || '', license_token: r.license_token || '', cover: r.artwork || '' });
        _scUpdateQualityBadge(sid, r.format || '');
        console.log(`[sc-prewarm] ${sid} ok format=${r.format}`);
      }
    } else {
      _scCacheSet(sid, { url: resp.url, format: 'mp3', license_token: '', cover: '' });
      _scUpdateQualityBadge(sid, 'mp3');
    }
  } catch (e) {
    console.warn(`[sc-prewarm] ${sid}:`, e.message);
  } finally {
    _scPrewarmInflight.delete(sid);
  }
}

// Debounced hover prewarm — 80ms delay. Was 200ms which felt sluggish; 80ms
// still suppresses the fetch storm during quick sweeps but kicks off the
// resolve almost immediately when the cursor settles.
const _scHoverTimers = new Map();
function scHoverPrewarm(id, title, artist) {
  const sid = String(id);
  if (_scCacheGet(sid) || _scPrewarmInflight.has(sid) || _scHoverTimers.has(sid)) return;
  const t = setTimeout(() => {
    _scHoverTimers.delete(sid);
    scPrewarmTrack(sid, title, artist).catch(() => {});
  }, 80);
  _scHoverTimers.set(sid, t);
}
function scHoverPrewarmCancel(id) {
  const t = _scHoverTimers.get(String(id));
  if (t) { clearTimeout(t); _scHoverTimers.delete(String(id)); }
}

// Pre-resolve stream URLs for SC queue items in the background so there's no
// fetch delay when the user moves to the next track. Runs 3 requests at a time.
// Also checks and populates _scStreamCache so single-play paths benefit too.
async function _scPreloadQueue(queue, startIdx = 1) {
  const todo = [];
  for (let i = startIdx; i < queue.length; i++) {
    if (queue[i] && !queue[i].url && queue[i].service === 'soundcloud') {
      // Cache hit → fill immediately, no network needed
      const _c = _scCacheGet(queue[i].id);
      if (_c) {
        queue[i].url           = _c.url;
        queue[i].format        = _c.format || '';
        queue[i].license_token = _c.license_token || '';
        if (_c.cover && !queue[i].cover) queue[i].cover = _c.cover;
        console.log(`[sc-preload] idx=${i} from cache`);
      } else {
        todo.push(i);
      }
    }
  }
  const CONCURRENT = 3;
  for (let c = 0; c < todo.length; c += CONCURRENT) {
    await Promise.all(todo.slice(c, c + CONCURRENT).map(async i => {
      const item = queue[i];
      try {
        const resp = await fetch(
          `/api/stream/soundcloud/${item.id}` +
          `?name=${encodeURIComponent(item.title || '')}` +
          `&artist=${encodeURIComponent(item.artist || '')}` +
          `&prefer=${_scDrmPreferGet()}`
        );
        if (!resp.ok) return;
        const ct = (resp.headers.get('content-type') || '').toLowerCase();
        if (ct.includes('application/json')) {
          const r = await resp.json();
          if (r.url) {
            item.url           = r.url;
            item.format        = r.format || '';
            item.license_token = r.license_token || '';
            if (r.artwork && !item.cover) item.cover = r.artwork;
            _scCacheSet(item.id, { url: r.url, format: r.format || '', license_token: r.license_token || '', cover: r.artwork || '' });
            console.log(`[sc-preload] idx=${i} ok format=${r.format}`);
          }
        } else {
          item.url    = resp.url;
          item.format = 'mp3';
          _scCacheSet(item.id, { url: resp.url, format: 'mp3', license_token: '', cover: '' });
        }
      } catch (e) {
        console.warn(`[sc-preload] idx=${i}:`, e.message);
      }
    }));
    // Stop if the queue has changed (user switched playlist)
    if (queue !== Preview.queue) break;
  }
}

function scInit() {
  const inp = document.getElementById('sc-q');
  if (inp && !inp.value) setTimeout(() => { try { inp.focus(); } catch(_){} }, 60);
}

function _scDur(sec) {
  sec = Math.max(0, sec || 0);
  const h = Math.floor(sec/3600), m = Math.floor((sec%3600)/60), s = sec%60;
  return h ? `${h}:${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}`
           : `${m}:${String(s).padStart(2,'0')}`;
}

function renderScTile(it) {
  const isPl       = it.kind === 'playlist';
  // SC `set_type`: album / ep / single / compilation / playlist (default).
  // Honest labelling stops users from thinking a user-curated "Mixes" sampler
  // is the same as a release album.
  const setType    = (it.set_type || (isPl ? 'playlist' : '')).toLowerCase();
  const badgeMap   = {
    album:       [t('sc.badge_album'),    '#1db954'],
    ep:          ['EP',                   '#1db954'],
    single:      [t('sc.badge_single'),   '#ff8800'],
    compilation: [t('sc.badge_comp'),     '#af52de'],
    playlist:    [t('sc.badge_playlist'), '#1870f5'],
  };
  const [badgeTrack, clrTrack] = [t('sc.badge_mix'), '#ff5500'];
  const [badge, badgeClr] = isPl ? (badgeMap[setType] || badgeMap.playlist) : [badgeTrack, clrTrack];
  const sub      = isPl ? (it.tracks ? it.tracks + ' ' + t('sc.tracks_short') : '') : _scDur(it.duration);
  const art      = it.artwork || '';
  // Cover and ▶ both trigger play — track plays one, playlist enqueues all.
  const playCall = isPl
    ? `playScPlaylist('${it.id}','${escJ(it.title)}','${escJ(it.artist)}','${escJ(it.artwork_sm||it.artwork||'')}')`
    : `playStreamTrack('soundcloud','${it.id}','${escJ(it.title)}','${escJ(it.artist)}','${escJ(it.artwork_sm||it.artwork||'')}')`;
  // Tracks get hover prewarm so clicking play is instant (URL already resolved).
  // Playlists skip hover prewarm — they need a separate endpoint for the track list.
  const hoverPre = !isPl
    ? `onmouseenter="scHoverPrewarm('${it.id}','${escJ(it.title)}','${escJ(it.artist)}')" onmouseleave="scHoverPrewarmCancel('${it.id}')" `
    : '';
  const coverClick = `cursor:pointer" ${hoverPre}onclick="${playCall}`;
  return `<div class="sc-tile">
    <div style="position:relative;${coverClick}">
      ${art
        ? `<img src="${esc(art)}" onerror="this.onerror=null;this.src='${escJ(it.artwork_sm||art)}'" onload="this.style.opacity='1'" style="opacity:0;transition:opacity .28s ease;width:100%;aspect-ratio:1;object-fit:cover;display:block;background:var(--surface2)" loading="lazy"/>`
        : `<div style="width:100%;aspect-ratio:1;background:rgba(255,255,255,.04);display:flex;align-items:center;justify-content:center;font-size:30px;color:var(--muted)">☁</div>`}
      <div style="position:absolute;top:6px;left:6px"><span style="font-size:9px;padding:2px 6px;border-radius:4px;background:rgba(0,0,0,.72);color:${badgeClr};font-weight:700;backdrop-filter:blur(4px)">${badge}</span></div>
      <div id="scmdb-badge-${it.id}" style="display:${it.has_tracklist ? '' : 'none'};position:absolute;top:6px;right:6px;background:rgba(175,82,222,.9);color:#fff;font-size:8px;padding:2px 6px;border-radius:4px;font-weight:700;backdrop-filter:blur(4px);cursor:pointer"
        onclick="event.stopPropagation();_scOpenMix('${it.id}')" title="${t('b.tl_word')}">📋</div>
      ${!isPl ? `<div id="sc-fmt-${it.id}" style="display:none;position:absolute;top:28px;right:6px;font-size:8px;font-weight:700;padding:1px 5px;border-radius:4px;background:rgba(0,0,0,.72);backdrop-filter:blur(4px);pointer-events:none"></div>` : ''}
      <div style="position:absolute;bottom:6px;right:6px;background:rgba(0,0,0,.72);border-radius:50%;width:32px;height:32px;display:flex;align-items:center;justify-content:center;font-size:13px;color:#fff;cursor:pointer" onclick="event.stopPropagation();${playCall}" title="${isPl ? t('sc2.play_pl') : t('btn.play')}">▶</div>
      ${sub ? `<div style="position:absolute;bottom:6px;left:6px;background:rgba(0,0,0,.72);border-radius:4px;font-size:10px;color:#fff;padding:2px 5px;font-family:var(--mono)">${esc(sub)}</div>` : ''}
    </div>
    <div style="padding:8px 10px">
      <div onclick="_scOpenMix('${it.id}')" style="cursor:pointer" title="${t('sc2.open_mix')}">
        <div style="font-size:12px;font-weight:600;color:var(--text);overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(it.title)}</div>
        <div style="font-size:11px;color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap${it.user_permalink ? ';cursor:pointer' : ''}"
          ${it.user_permalink ? `onclick="event.stopPropagation();scBrowseUser('${escJ(it.user_permalink)}','${escJ(it.artist)}')" title="${t('sc.open_channel')}" onmouseover="this.style.textDecoration='underline'" onmouseout="this.style.textDecoration='none'"` : ''}>${esc(it.artist)}</div>
        ${it.date ? `<div style="font-size:10px;color:var(--muted2);margin-top:3px">${esc(it.date)}</div>` : ''}
      </div>
      <div style="display:flex;gap:5px;margin-top:7px">
        <button onclick="scDownload('${it.id}')"
          style="flex:1;padding:5px 0;background:rgba(255,85,0,.12);border:1px solid rgba(255,85,0,.22);border-radius:7px;font-size:11px;font-weight:600;color:#ff7a33;cursor:pointer;font-family:var(--font)">${t('btn.download')}</button>
        <a href="${esc(it.url)}" target="_blank" style="padding:5px 9px;background:transparent;border:1px solid var(--border);border-radius:7px;font-size:11px;color:var(--muted);text-decoration:none;display:flex;align-items:center" title="${t('sc2.open_on_sc')}">↗</a>
      </div>
    </div>
  </div>`;
}

async function scSearch() {
  const q      = (document.getElementById('sc-q')?.value || '').trim();
  const kind   = document.getElementById('sc-kind')?.value || 'all';
  const grid   = document.getElementById('sc-grid');
  const status = document.getElementById('sc-status');
  const empty  = document.getElementById('sc-empty');
  _scPrevSearch = null;
  _scHideChannelBar();
  if (!q) {
    if (empty) { empty.textContent = t('sc.empty_query'); empty.style.display = ''; }
    if (grid)  grid.innerHTML = '';
    return;
  }
  if (empty)  empty.style.display = 'none';
  if (grid)   grid.innerHTML = '';
  if (status) { status.textContent = t('sc.searching'); status.style.display = ''; }

  let d = null;
  try {
    d = await api('GET', `/api/soundcloud/search?q=${encodeURIComponent(q)}&kind=${kind}`);
  } catch(e) { d = null; }
  if (status) status.style.display = 'none';

  if (!d || !d.ok) {
    if (empty) { empty.textContent = (d && d.error) || t('sc.search_error'); empty.style.display = ''; }
    return;
  }
  _scResults = d.results || [];
  if (!_scResults.length) {
    if (empty) { empty.textContent = t('sc.not_found'); empty.style.display = ''; }
    return;
  }
  _scRender();
}

function _scHideChannelBar() {
  const bar = document.getElementById('sc-channel-bar');
  if (bar) bar.style.display = 'none';
}

// Browse a channel's own uploads (newest first) — the reliable path when
// free-text search can't find a track (a permalink slug shares no words with
// the real title, or SC's search index hasn't caught up on a fresh upload).
async function scBrowseUser(permalink, displayName) {
  if (!permalink) return;
  const grid   = document.getElementById('sc-grid');
  const status = document.getElementById('sc-status');
  const empty  = document.getElementById('sc-empty');
  const bar    = document.getElementById('sc-channel-bar');
  const nameEl = document.getElementById('sc-channel-name');
  const avEl   = document.getElementById('sc-channel-avatar');

  // Only remember the search we came FROM — browsing channel A → channel B
  // must still return to the original search, not to channel A.
  if (!_scPrevSearch) {
    _scPrevSearch = {
      q: document.getElementById('sc-q')?.value || '',
      results: _scResults.slice(),
    };
  }

  if (empty)  empty.style.display = 'none';
  if (grid)   grid.innerHTML = '';
  if (status) { status.textContent = t('sc.searching'); status.style.display = ''; }
  if (bar)    bar.style.display = 'flex';
  if (nameEl) nameEl.textContent = displayName || permalink;
  if (avEl)   avEl.style.display = 'none';

  let d = null;
  try {
    d = await api('GET', `/api/soundcloud/user/${encodeURIComponent(permalink)}/tracks`);
  } catch (e) { d = null; }
  if (status) status.style.display = 'none';

  if (!d || !d.ok) {
    if (empty) { empty.textContent = (d && d.error) || t('sc.search_error'); empty.style.display = ''; }
    return;
  }
  if (d.channel) {
    if (nameEl) nameEl.textContent = d.channel.username || displayName || permalink;
    if (avEl && d.channel.avatar) { avEl.src = d.channel.avatar; avEl.style.display = ''; }
  }
  _scResults = d.results || [];
  const sortSel = document.getElementById('sc-sort');
  if (sortSel) sortSel.value = 'new';   // channel browse = newest-first by default
  if (!_scResults.length) {
    if (empty) { empty.textContent = t('sc.not_found'); empty.style.display = ''; }
    return;
  }
  _scRender();
}

function scBackFromChannel() {
  if (!_scPrevSearch) { _scHideChannelBar(); return; }
  const q = document.getElementById('sc-q');
  if (q) q.value = _scPrevSearch.q;
  _scResults = _scPrevSearch.results;
  _scPrevSearch = null;
  _scHideChannelBar();
  const empty = document.getElementById('sc-empty');
  if (!_scResults.length) {
    if (empty) { empty.textContent = t('sc.empty_query'); empty.style.display = ''; }
    return;
  }
  if (empty) empty.style.display = 'none';
  _scRender();
}

// Sort the in-memory results client-side and repaint — no re-fetch.
function _scRender() {
  const grid = document.getElementById('sc-grid');
  if (!grid) return;
  const sort = document.getElementById('sc-sort')?.value || 'relevance';
  const list = _scResults.slice();
  switch (sort) {
    case 'new':      list.sort((a,b) => (b.date||'').localeCompare(a.date||'')); break;
    case 'old':      list.sort((a,b) => (a.date||'').localeCompare(b.date||'')); break;
    case 'plays':    list.sort((a,b) => (b.plays||0) - (a.plays||0)); break;
    case 'duration': list.sort((a,b) => (b.duration||0) - (a.duration||0)); break;
    // 'relevance' → keep the API order
  }
  grid.innerHTML = list.map(renderScTile).join('');
  _scEnrichGrid();
  // Prewarm top 5 visible tracks immediately — instant play when user clicks.
  // Playlists are skipped (separate endpoint; their tracks aren't individually preloaded here).
  list.slice(0, 5).forEach(it => {
    if (it.kind !== 'playlist') scPrewarmTrack(it.id, it.title, it.artist).catch(() => {});
  });
}

// Queue a SoundCloud item — passes the metadata we already have (cover, title,
// artist, duration) so the queue card is populated immediately.
async function scDownload(id) {
  const it = _scResults.find(x => String(x.id) === String(id));
  if (!it) { toast(t('err.generic'), 'var(--red)'); return; }
  // HQ AAC needs a Go+ OAuth token; without one, MP3 128 (public, always works).
  const quality = (S.config && (S.config['soundcloud-oauth-token'] || '').trim()) ? 'hq' : 'mp3';
  const meta = {
    title:      it.title,
    artist:     it.artist,
    artworkUrl: it.artwork_sm || it.artwork,
    type:       it.kind === 'playlist' ? 'playlist' : 'track',
    duration:   it.duration || 0,
    trackCount: it.tracks || (it.kind === 'playlist' ? 0 : 1),
  };
  // Cover-source override chosen in the mix drawer (empty = use SoundCloud's own).
  if (it._coverUrl) meta.coverUrl = it._coverUrl;
  // Optimistic feedback — show the toast *before* the API call returns so the
  // button click feels instant. Final state replaces it on error / duplicate.
  toast(`⏳ ${it.title}`, 'var(--muted)', '', 1500);
  const r = await api('POST', '/api/queue/add',
                      { url: it.url, quality, title: it.title, artist: it.artist, meta });
  if (r && r.ok)             toast(`+ ${it.title} → ${t('nav.queue').toLowerCase()}`);
  else if (r && r.duplicate) toast(t('sc.already_queued'), 'var(--muted)');
  else                       toast(t('err.generic') + ': ' + ((r && (r.msg || r.detail)) || '?'), 'var(--red)');
}

// ── SoundCloud MixesDB tracklist + YouTube timecodes ───────────────────────
// _scMdb stores: { found, tracklist, url, yt: {found, video_id, title, timecodes} }
const _scMdb = new Map();

function _scEnrichGrid() {
  for (const it of _scResults) {
    if (!it || !it.id) continue;
    const cached = _scMdb.get(it.id);
    if (cached !== undefined) {
      // Re-apply badge from cache (null = fetch in progress, skip)
      if (cached) {
        const b = document.getElementById(`scmdb-badge-${it.id}`);
        if (b) {
          const isCachedMix = it.kind !== 'playlist' && (it.duration || 0) > 1800;
          if (cached.found)        { b.style.display = ''; b.textContent = '🗄 MDB'; }
          else if (cached.yt?.found) { b.style.display = ''; b.textContent = '⏱ YT'; }
          else if (isCachedMix)    { b.style.display = ''; b.textContent = '⏱'; }
        }
      }
      continue;
    }
    // Mark as pending so we don't double-fetch
    _scMdb.set(it.id, null);
    const isMix = it.kind !== 'playlist' && (it.duration || 0) > 1800;
    fetch(`/api/bbc/mixesdb/match?title=${encodeURIComponent(it.title||'')}&artist=${encodeURIComponent(it.artist||'')}`)
      .then(r => r.json())
      .then(d => {
        _scMdb.set(it.id, d || {});
        const b = document.getElementById(`scmdb-badge-${it.id}`);
        if (d?.found && b) { b.style.display = ''; b.textContent = '🗄 MDB'; }
        else if (isMix && b) { b.style.display = ''; b.textContent = '⏱'; }
      })
      .catch(() => { if (isMix) { const b = document.getElementById(`scmdb-badge-${it.id}`); if(b){b.style.display='';b.textContent='⏱';} } });
  }
}

// Fetch the authoritative 1001Tracklists tracklist for a mix (server verifies +
// disk-caches it 10 days, so this rarely hits the site). Stores into _scMdb.tl1001.
function _scFetch1001(id, cb) {
  const it = _scResults.find(x => String(x.id) === String(id)) || {};
  const d  = _scMdb.get(id) || {};
  if (d.tl1001) { cb?.(); return; }                 // already fetched (hit or miss)
  const scTl = (d.sc?.tracklist?.length ? d.sc.tracklist : (it.tracklist || []));
  fetch('/api/soundcloud/tracklist-1001', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      title: it.title || '', artist: it.artist || '',
      dur: Math.round(it.duration || 0), id: String(id),
      sc_tracklist: scTl,
      source_urls: [it.permalink_url || it.url || ''].filter(Boolean),
    }),
  })
    .then(r => r.json())
    .then(res => {
      const dd = _scMdb.get(id) || {};
      dd.tl1001 = (res && res.found && res.tracks?.length)
        ? { tracks: res.tracks, url: res.url, match: res.match }
        : { empty: true };
      _scMdb.set(id, dd);
      if (res?.found) {
        const b = document.getElementById(`scmdb-badge-${id}`);
        if (b) { b.style.display = ''; b.textContent = '🎚 1001TL'; }
      }
      cb?.();
    })
    .catch(() => cb?.());
}

function _scFetchYtTimecodes(id, cb) {
  const it = _scResults.find(x => String(x.id) === String(id));
  if (!it) return;
  const q = encodeURIComponent((it.title || '') + ' ' + (it.artist || ''));
  const dur = Math.round(it.duration || 0);
  fetch(`/api/bbc/youtube-timecodes?q=${q}&dur=${dur}`)
    .then(r => r.json())
    .then(yt => {
      const existing = _scMdb.get(id) || {};
      existing.yt = yt;
      _scMdb.set(id, existing);
      if (yt?.found) {
        const b = document.getElementById(`scmdb-badge-${id}`);
        if (b) { b.style.display = ''; if (b.textContent === '⏱') b.textContent = '⏱ YT'; }
      }
      cb?.();
    })
    .catch(() => cb?.());
}

function _tsToSec(ts) {
  if (!ts) return null;
  const p = ts.split(':').map(Number);
  if (p.some(isNaN)) return null;
  return p.length === 3 ? p[0]*3600 + p[1]*60 + p[2]
       : p.length === 2 ? p[0]*60 + p[1] : null;
}

// Build YouTube-style chapters [{seconds,label}] for a SC mix, if its tracklist
// carries timestamps (SC description timestamps, or matched YouTube timecodes).
// Returns [] when there are no time markers — the player then shows no chapters.
function _scChaptersFor(id) {
  const it = (typeof _scResults !== 'undefined' ? _scResults.find(x => String(x.id) === String(id)) : null) || {};
  const d  = (typeof _scMdb !== 'undefined' ? _scMdb.get(id) : null) || {};
  const nameOf = (tr) => (tr.artist ? tr.artist + ' — ' : '') + (tr.title || '');

  // 0) 1001Tracklists — authoritative cue times, already verified server-side
  //    against this mix. Highest priority when present.
  const tl1 = (d.tl1001 && d.tl1001.tracks) || [];
  const ch1 = tl1.filter(tr => tr.seconds != null)
                 .map(tr => ({ seconds: tr.seconds, label: nameOf(tr) }))
                 .filter(c => c.seconds != null && c.label);
  if (ch1.length) return ch1.sort((a, b) => a.seconds - b.seconds);

  const scT  = (d.sc?.tracklist?.length ? d.sc.tracklist : (it.tracklist || []));
  const base = scT.length ? scT : (d.tracklist || []);   // SC description → MixesDB

  // 1) Real timestamps embedded in the tracklist itself — always trustworthy.
  let ch = base.filter(tr => tr.timestamp)
              .map(tr => ({ seconds: _tsToSec(tr.timestamp), label: nameOf(tr) }))
              .filter(c => c.seconds != null && c.label);
  if (ch.length) return ch.sort((a, b) => a.seconds - b.seconds);

  // 2) YouTube timecodes — only trust them when they line up with THIS mix:
  //    either there's no local tracklist to contradict them, or the counts match.
  //    Mismatched counts == YouTube matched a DIFFERENT set (e.g. kloyd vs
  //    Braxton, 13≠9) → never overlay someone else's tracklist.
  const yt = d.yt?.timecodes || [];
  if (yt.length && (!base.length || yt.length === base.length)) {
    ch = base.length === yt.length
      ? base.map((tr, i) => ({ seconds: yt[i].seconds, label: nameOf(tr) || yt[i].title }))
      : yt.map(tc => ({ seconds: tc.seconds, label: tc.title }));
    return ch.filter(c => c.seconds != null && c.label).sort((a, b) => a.seconds - b.seconds);
  }
  return [];
}

function _scRenderTracklist(id, tl) {
  if (!tl) return;
  const it        = _scResults.find(x => String(x.id) === String(id)) || {};
  const d         = _scMdb.get(id) || {};

  // 1001Tracklists — authoritative, verified server-side. Render and return.
  const tl1 = (d.tl1001 && d.tl1001.tracks) || [];
  if (tl1.length) {
    const nm1 = (tr) => (tr.artist ? tr.artist + ' — ' : '') + (tr.title || '');
    const hasTs1 = tl1.some(tr => tr.timestamp);
    const cue1 = hasTs1
      ? `<button onclick="_scDownloadCue('${id}')" style="font-size:10px;padding:3px 9px;background:rgba(175,82,222,.15);border:1px solid rgba(175,82,222,.3);border-radius:6px;color:#af52de;cursor:pointer;font-family:var(--font)">📄 .cue</button>`
      : '';
    const cr1 = `<div style="display:flex;align-items:center;justify-content:space-between;gap:8px;font-size:10px;color:var(--muted2);margin-bottom:8px;padding-bottom:6px;border-bottom:1px solid var(--border)"><span>🎚 1001Tracklists · ${tl1.length} ${t('p.trk_abbr')}</span>${cue1}</div>`;
    tl.innerHTML = cr1 + tl1.map((tr, i) => {
      const tsEl = tr.timestamp
        ? `<span class="ts" onclick="scSeekTo(${tr.seconds||0})" title="${t('sc2.seek')}">${esc(tr.timestamp)}</span>`
        : '';
      return `<div class="scd-trk"><span class="n">${i+1}</span>${tsEl}<span class="nm">${esc(nm1(tr))}</span></div>`;
    }).join('');
    return;
  }

  // SC description tracklist — from search payload (it.tracklist) or the per-id fetch.
  const scTracks  = (d.sc?.tracklist?.length ? d.sc.tracklist : (it.tracklist || []));
  const mdbTracks = d.tracklist || [];
  const ytCodes   = d.yt?.timecodes || [];

  // Names/order — source of truth is the SoundCloud description; MixesDB only as
  // a fallback (it can fuzzy-match a stranger's set).
  const base = scTracks.length ? scTracks : mdbTracks;
  const nameOf = (tr) => (tr.artist ? tr.artist + ' — ' : '') + (tr.title || '');

  let tracks, creditLabel = '';
  if (base.length) {
    if (ytCodes.length && ytCodes.length === base.length) {
      // Counts match → confident to overlay YouTube timecodes onto the real names.
      tracks = base.map((tr, i) => ({
        timestamp: ytCodes[i].time, seconds: ytCodes[i].seconds,
        label: nameOf(tr) || ytCodes[i].title,
      }));
    } else {
      // Use the tracklist's own timestamps (if any) — never mis-zip mismatched YT.
      tracks = base.map(tr => ({
        timestamp: tr.timestamp || '', seconds: _tsToSec(tr.timestamp), label: nameOf(tr),
      }));
    }
    creditLabel = scTracks.length ? t('sc2.from_desc') : '🗄 MixesDB';
  } else if (ytCodes.length) {
    tracks = ytCodes.map(tc => ({ timestamp: tc.time, seconds: tc.seconds, label: tc.title }));
    creditLabel = '⏱ YouTube · ' + (d.yt?.title || '');
  } else {
    return;
  }
  if (!tracks.length) return;

  const hasTs  = tracks.some(tr => tr.timestamp);
  const cueBtn = hasTs
    ? `<button onclick="_scDownloadCue('${id}')" style="font-size:10px;padding:3px 9px;background:rgba(175,82,222,.15);border:1px solid rgba(175,82,222,.3);border-radius:6px;color:#af52de;cursor:pointer;font-family:var(--font)">📄 .cue</button>`
    : '';
  const credit = `<div style="display:flex;align-items:center;justify-content:space-between;gap:8px;font-size:10px;color:var(--muted2);margin-bottom:8px;padding-bottom:6px;border-bottom:1px solid var(--border)"><span>${esc(creditLabel)} · ${tracks.length} ${t('p.trk_abbr')}</span>${cueBtn}</div>`;
  tl.innerHTML = credit + tracks.map((tr, i) => {
    const tsEl = tr.timestamp
      ? `<span class="ts" onclick="scSeekTo(${tr.seconds||0})" title="${t('sc2.seek')}">${esc(tr.timestamp)}</span>`
      : '';
    return `<div class="scd-trk"><span class="n">${i+1}</span>${tsEl}<span class="nm">${esc(tr.label)}</span></div>`;
  }).join('');
}

// If the active tracklist has no timestamps and YouTube hasn't been tried yet,
// fetch YT in the background; the re-render overlays times only if counts align.
function _scTryEnrichTimecodes(id, tl) {
  const d = _scMdb.get(id) || {};
  const base = d.sc?.tracklist?.length ? d.sc.tracklist : (d.tracklist || []);
  const hasTs = base.some(tr => tr.timestamp) || (d.yt?.timecodes?.length);
  if (hasTs || d.yt) return;
  _scFetchYtTimecodes(id, () => { _scRenderTracklist(id, tl); _scRenderCoverPicker(id); });
}

// Fallback path (no SC-description tracklist): cached MixesDB / YouTube, or fetch YT.
function _scFallbackTracklist(id, tl) {
  const d = _scMdb.get(id) || {};
  const mdbTracks = d.tracklist || [];
  const ytCodes   = d.yt?.timecodes || [];
  if (mdbTracks.length || ytCodes.length) {
    _scRenderTracklist(id, tl);
    const hasTs = mdbTracks.some(tr => tr.timestamp) || ytCodes.length;
    if (!hasTs && !d.yt) {
      tl.innerHTML += `<div id="sc-yt-loading-${id}" style="font-size:9px;color:var(--muted2);margin-top:4px">${t('sc2.yt_tc')}</div>`;
      _scFetchYtTimecodes(id, () => { _scRenderTracklist(id, tl); });
    }
  } else {
    tl.innerHTML = `<div style="font-size:9px;color:var(--muted2)">${t('sc2.yt_tc')}</div>`;
    _scFetchYtTimecodes(id, () => {
      const ytC = (_scMdb.get(id) || {}).yt?.timecodes || [];
      if (ytC.length) { _scRenderTracklist(id, tl); }
      else { tl.innerHTML = `<div style="font-size:9px;color:var(--muted2)">${t('sc.tracklist_empty')}</div>`; }
    });
  }
}

// Load the tracklist into a given container (the detail drawer). SC description
// first (search payload or per-id fetch), then MixesDB / YouTube fallback.
function _scLoadTracklistInto(id, tl) {
  if (!tl) return;
  // 1001Tracklists in parallel — authoritative; re-render to it if found.
  _scFetch1001(id, () => {
    if ((_scMdb.get(id) || {}).tl1001?.tracks?.length) _scRenderTracklist(id, tl);
  });
  const d = _scMdb.get(id) || {};
  if (d.scChecked) {
    if (d.sc?.tracklist?.length) { _scRenderTracklist(id, tl); _scTryEnrichTimecodes(id, tl); }
    else                         { _scFallbackTracklist(id, tl); }
    return;
  }
  tl.innerHTML = `<div style="font-size:11px;color:var(--muted2)">${t('sc2.tl_loading')}</div>`;
  fetch(`/api/soundcloud/tracklist/${id}`)
    .then(r => r.json())
    .then(scd => {
      const dd = _scMdb.get(id) || {};
      dd.scChecked = true;
      if (scd?.found && scd.tracklist?.length) {
        dd.sc = { tracklist: scd.tracklist, has_timestamps: scd.has_timestamps };
        _scMdb.set(id, dd);
        _scRenderTracklist(id, tl);
        _scTryEnrichTimecodes(id, tl);
      } else {
        _scMdb.set(id, dd);
        _scFallbackTracklist(id, tl);
      }
    })
    .catch(() => {
      const dd = _scMdb.get(id) || {}; dd.scChecked = true; _scMdb.set(id, dd);
      _scFallbackTracklist(id, tl);
    });
}

// Build the right-side mix detail drawer markup.
function _scDetailHTML(it) {
  const isPl = it.kind === 'playlist';
  const art  = it.artwork || it.artwork_sm || '';
  const meta = [];
  if (it.date)     meta.push(esc(it.date));
  if (it.duration) meta.push(_scDur(it.duration));
  if (it.plays)    meta.push((it.plays).toLocaleString('ru-RU') + ' ▶');
  if (it.genre)    meta.push(esc(it.genre));
  const playCall = isPl
    ? `playScPlaylist('${it.id}','${escJ(it.title)}','${escJ(it.artist)}','${escJ(it.artwork_sm||art)}')`
    : `playStreamTrack('soundcloud','${it.id}','${escJ(it.title)}','${escJ(it.artist)}','${escJ(it.artwork_sm||art)}')`;
  const btn = (bg, bd, clr) => `flex:1;padding:8px 0;border:1px solid ${bd};background:${bg};border-radius:8px;font-size:12px;font-weight:600;color:${clr};cursor:pointer;font-family:var(--font)`;
  return `
    <div class="scd-head" style="display:flex;align-items:center;justify-content:space-between;padding:14px 16px 10px;border-bottom:1px solid var(--border);flex:0 0 auto">
      <div style="font-size:13px;font-weight:700;color:var(--text)">${t('sc2.mix_card')}</div>
      <button class="scd-close" onclick="_scCloseMix()" title="${t('b.close_esc')}" style="width:30px;height:30px;border-radius:8px;border:1px solid var(--border);background:transparent;color:var(--muted);cursor:pointer;font-size:15px;flex:0 0 auto;display:flex;align-items:center;justify-content:center;line-height:1;padding:0">✕</button>
    </div>
    <div class="scd-body" style="overflow-y:auto;padding:16px 16px 168px;flex:1 1 auto">
      ${art ? `<img id="scd-cover-img" class="scd-cover" src="${esc(art)}" onerror="this.onerror=null;this.src='${escJ(it.artwork_sm||art)}'" style="width:100%;aspect-ratio:1;object-fit:cover;border-radius:12px;display:block;background:var(--surface2)"/>` : ''}
      <div id="scd-cover-pick" style="display:flex;gap:6px;margin-top:8px;flex-wrap:wrap"></div>
      <div style="font-size:16px;font-weight:700;color:var(--text);margin-top:13px;line-height:1.3">${esc(it.title)}</div>
      <div style="font-size:13px;color:var(--muted);margin-top:3px">${esc(it.artist)}</div>
      ${meta.length ? `<div style="font-size:11px;color:var(--muted2);margin-top:7px">${meta.join('  ·  ')}</div>` : ''}
      <div style="display:flex;gap:7px;margin-top:14px">
        <button onclick="${playCall}" style="${btn('rgba(255,85,0,.14)','rgba(255,85,0,.25)','#ff7a33')}">▶ ${t('btn.play')||'Играть'}</button>
        <button onclick="scDownload('${it.id}')" style="${btn('rgba(255,255,255,.06)','var(--border)','var(--text)')}">${t('btn.download')}</button>
        <a href="${esc(it.url)}" target="_blank" title="${t('sc2.open_on_sc')}" style="padding:8px 11px;border:1px solid var(--border);border-radius:8px;font-size:13px;color:var(--muted);text-decoration:none;display:flex;align-items:center">↗</a>
      </div>
      <div style="font-size:11px;font-weight:700;color:var(--muted);margin:18px 0 8px;text-transform:uppercase;letter-spacing:.4px">${t('b.tl_word')}</div>
      <div id="scd-tl"></div>
    </div>`;
}

function _scOpenMix(id) {
  const it = _scResults.find(x => String(x.id) === String(id));
  if (!it) return;
  let bd = document.getElementById('sc-detail-backdrop');
  if (!bd) {
    bd = document.createElement('div'); bd.id = 'sc-detail-backdrop'; bd.onclick = _scCloseMix;
    // Inline styles so it works even if main.css is stale-cached.
    bd.style.cssText = 'position:fixed;inset:0;z-index:1199;background:transparent;display:none';
    document.body.appendChild(bd);
  }
  let d = document.getElementById('sc-detail');
  if (!d) {
    d = document.createElement('div'); d.id = 'sc-detail';
    d.style.cssText = 'position:fixed;top:0;right:0;height:100vh;width:min(480px,100vw);'
      + 'background:var(--surface,#15151a);border-left:1px solid var(--border);'
      + 'box-shadow:-10px 0 36px rgba(0,0,0,.45);z-index:1200;transform:translateX(102%);'
      + 'transition:transform .26s cubic-bezier(.4,0,.2,1);display:flex;flex-direction:column;overflow:hidden';
    document.body.appendChild(d);
    document.addEventListener('keydown', e => { if (e.key === 'Escape') _scCloseMix(); });
  }
  d.innerHTML = _scDetailHTML(it);
  bd.style.display = 'block'; bd.classList.add('show');
  requestAnimationFrame(() => { d.classList.add('open'); d.style.transform = 'translateX(0)'; });
  // Seed the SC-description tracklist from the search payload so it renders instantly.
  if (it.tracklist && it.tracklist.length) {
    const dd = _scMdb.get(id) || {};
    dd.sc = { tracklist: it.tracklist, has_timestamps: it.tracklist.some(x => x.timestamp) };
    dd.scChecked = true;
    _scMdb.set(id, dd);
  }
  _scLoadTracklistInto(id, d.querySelector('#scd-tl'));
  _scRenderCoverPicker(id);
}

function _scCloseMix() {
  const d  = document.getElementById('sc-detail');
  if (d)  { d.classList.remove('open'); d.style.transform = 'translateX(102%)'; }
  const bd = document.getElementById('sc-detail-backdrop');
  if (bd) { bd.classList.remove('show'); bd.style.display = 'none'; }
}

// Back-compat: anything still calling the old name opens the drawer.
function _scMdbTracklist(id) { _scOpenMix(id); }

// ── Cover-source picker (SoundCloud / MixesDB / YouTube) ───────────────────────
function _scCoverSources(it) {
  const d = (typeof _scMdb !== 'undefined' ? _scMdb.get(it.id) : null) || {};
  const out = [];
  if (it.artwork || it.artwork_sm) out.push({ key: 'sc',  label: 'SoundCloud', url: it.artwork || it.artwork_sm });
  if (d.artworkUrl)                out.push({ key: 'mdb', label: 'MixesDB',    url: d.artworkUrl });
  if (d.yt?.thumbnail)             out.push({ key: 'yt',  label: 'YouTube',    url: d.yt.thumbnail });
  return out;
}

// Render the picker; hidden when there's only one source. Affects the drawer
// cover AND (via it._coverUrl) the cover embedded on download.
function _scRenderCoverPicker(id) {
  const box = document.getElementById('scd-cover-pick'); if (!box) return;
  const it  = _scResults.find(x => String(x.id) === String(id)); if (!it) return;
  const srcs = _scCoverSources(it);
  if (srcs.length < 2) { box.style.display = 'none'; return; }
  box.style.display = 'flex';
  const cur = it._coverKey || 'sc';
  box.innerHTML = '<span style="font-size:10px;color:var(--muted2);align-self:center;margin-right:2px">' + t('sc2.cover_lbl') + '</span>' +
    srcs.map(s => `<button onclick="_scPickCover('${id}','${s.key}')" style="font-size:10px;padding:3px 9px;border-radius:6px;cursor:pointer;font-family:var(--font);border:1px solid ${s.key===cur?'var(--red)':'var(--border)'};background:${s.key===cur?'rgba(192,132,160,.12)':'transparent'};color:${s.key===cur?'var(--text)':'var(--muted)'}">${esc(s.label)}</button>`).join('');
}

function _scPickCover(id, key) {
  const it = _scResults.find(x => String(x.id) === String(id)); if (!it) return;
  const src = _scCoverSources(it).find(s => s.key === key); if (!src) return;
  it._coverKey = key;
  it._coverUrl = (key === 'sc') ? '' : src.url;   // 'sc' = default (no override)
  const img = document.getElementById('scd-cover-img'); if (img) img.src = src.url;
  _scRenderCoverPicker(id);
}

// Seek the current SC stream to the given number of seconds.
function scSeekTo(seconds) {
  if (typeof _waEnabled === 'function' && _waEnabled() && window._WA?.curBuffer) {
    if (typeof _waSeek === 'function') { _waSeek(seconds); return; }
  }
  const audio = document.getElementById('pp-audio');
  if (!audio || !audio.src) return;
  const doSeek = () => {
    if (isFinite(audio.duration) && audio.duration > 0)
      audio.currentTime = Math.max(0, Math.min(seconds, audio.duration - 0.5));
  };
  if (isFinite(audio.duration) && audio.duration > 0) doSeek();
  else audio.addEventListener('loadedmetadata', doSeek, { once: true });
}

// Build a .cue sheet — uses YT timecodes if available, falls back to MDB tracklist.
function _scDownloadCue(id) {
  const it = _scResults.find(x => String(x.id) === String(id));
  const d  = _scMdb.get(id) || {};
  const ytCodes   = d.yt?.timecodes || [];
  // Names source of truth: SoundCloud description > MixesDB.
  const base = (d.sc?.tracklist?.length ? d.sc.tracklist : (d.tracklist || []));
  let tracks;
  if (ytCodes.length && ytCodes.length === base.length && base.length) {
    tracks = base.map((tr, i) => ({ timestamp: ytCodes[i].time, title: tr.title || ytCodes[i].title, artist: tr.artist || '' }));
  } else if (base.length) {
    tracks = base;
  } else {
    tracks = ytCodes.map(tc => ({ timestamp: tc.time, title: tc.title, artist: '' }));
  }
  if (!it || !tracks.length) return;
  const cueTime = (ts) => {
    const p = String(ts || '0:00').split(':').map(n => parseInt(n, 10) || 0);
    const sec = p.length === 3 ? p[0]*3600 + p[1]*60 + p[2] : p[0]*60 + (p[1] || 0);
    return `${String(Math.floor(sec/60)).padStart(2,'0')}:${String(sec%60).padStart(2,'0')}:00`;
  };
  const q = s => String(s || '').replace(/"/g, "'");
  let cue = `PERFORMER "${q(it.artist)}"\r\nTITLE "${q(it.title)}"\r\nFILE "${q(it.title)}.mp3" MP3\r\n`;
  tracks.forEach((tr, i) => {
    cue += `  TRACK ${String(i+1).padStart(2,'0')} AUDIO\r\n`;
    cue += `    TITLE "${q(tr.title)}"\r\n`;
    cue += `    PERFORMER "${q(tr.artist || it.artist)}"\r\n`;
    cue += `    INDEX 01 ${cueTime(tr.timestamp)}\r\n`;
  });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([cue], { type: 'text/plain' }));
  a.download = (it.title || 'mix').replace(/[\\/:*?"<>|]/g, '_') + '.cue';
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  toast(t('sc.cue_downloaded'));
}


async function scDownloadCurrentQueue() {
  const items = (Preview.queue || []).filter(it => it.service === 'soundcloud');
  if (!items.length) { toast(t('sc.queue_empty'), 'var(--muted)'); return; }
  let ok = 0, fail = 0;
  for (const it of items) {
    try {
      const url = it.permalink;  // SC page permalink — what Lucida/downloader expects
      if (!url) { fail++; continue; }
      const r = await api('POST', '/api/queue/add', {url, title: it.title, artist: it.artist, quality: 'mp3'});
      if (r?.ok) ok++; else fail++;
    } catch { fail++; }
  }
  toast(`+ ${ok} ${t('sc2.q_ok')}${fail?` · ${fail} ${t('sc2.q_fail')}`:''}`, ok ? 'var(--green)' : 'var(--red)', '', 3500);
}

// ── SoundCloud: play an entire playlist (loads queue, plays first) ─────────
async function playScPlaylist(plId, plTitle, plArtist, plCover) {
  console.log('[scplay] start playlist', plId, plTitle);
  toast(t('toast.loading_playlist'), 'var(--muted)', '', 2000);
  try {
    const r = await fetch(`/api/soundcloud/playlist/${plId}`);
    console.log('[scplay] playlist endpoint', {ok: r.ok, status: r.status});
    if (!r.ok) {
      let detail = '';
      try { const j = await r.clone().json(); detail = j?.detail || j?.error || ''; } catch {}
      toast(`${t('sc2.pl_load_fail')} (${r.status}${detail?': '+detail:''})`, 'var(--red)');
      return;
    }
    const d = await r.json();
    console.log('[scplay] tracks received', d.tracks?.length, 'first:', d.tracks?.[0]);
    if (!d.tracks?.length) { toast(t('toast.playlist_empty'), 'var(--orange)'); return; }
    _setupAudioEvents();
    // NOTE: `tr` not `t` — avoid shadowing the global `t()` i18n function.
    Preview.queue = d.tracks.map(tr => ({
      service:   'soundcloud',
      id:        String(tr.id),
      title:     tr.title,
      artist:    tr.artist,
      cover:     tr.artwork_sm || tr.artwork || plCover || '',
      permalink: tr.url || '',   // SC page URL — for downloads; NOT used as audio src
      full:      true,
      label:     `SoundCloud · ${d.title || plTitle || t('card.playlist')}`,
      posKey:    'soundcloud:' + tr.id,
    }));
    Preview.idx = 0;
    // Cache hit on track 0 → fill URL now so _playPreviewAt starts instantly.
    if (!Preview.queue[0].url) {
      const _c0 = _scCacheGet(Preview.queue[0].id);
      if (_c0) {
        Preview.queue[0].url           = _c0.url;
        Preview.queue[0].format        = _c0.format || '';
        Preview.queue[0].license_token = _c0.license_token || '';
        if (_c0.cover && !Preview.queue[0].cover) Preview.queue[0].cover = _c0.cover;
      }
    }
    console.log('[scplay] queue built, starting first track');
    toast(`▶ ${plTitle || d.title}: ${d.tracks.length} ${t('p.trk_abbr')}`, '#ff5500', '', 3000);
    try {
      await _playPreviewAt(0);
      console.log('[scplay] _playPreviewAt(0) resolved');
    } catch (e) {
      console.error('[scplay] _playPreviewAt threw:', e);
      toast('Не удалось запустить трек: ' + e.message, 'var(--red)');
    }
    // Pre-resolve stream URLs for remaining tracks in background so playback
    // between tracks is instant (no fetch delay at each track boundary).
    if (Preview.queue.length > 1) {
      _scPreloadQueue(Preview.queue, 1).catch(() => {});
    }
  } catch (e) {
    console.error('[scplay] fatal:', e);
    toast('Ошибка плейлиста: ' + e.message, 'var(--red)');
  }
}

// ── SoundCloud sign-in (email + password → oauth_token) ───────────────────
async function scSignIn() {
  const email = (document.getElementById('s-sc-email')?.value || '').trim();
  const pass  = document.getElementById('s-sc-pass')?.value || '';
  const out   = document.getElementById('sc-signin-status');
  if (!out) return;
  if (!email || !pass) {
    out.textContent = t('sc2.enter_creds');
    out.style.color = '#ff5500';
    return;
  }
  out.textContent = t('sc.login_progress');
  out.style.color = 'var(--muted)';
  try {
    const r = await fetch('/api/soundcloud/login', {
      method:  'POST',
      headers: {'Content-Type': 'application/json'},
      body:    JSON.stringify({email, password: pass}),
    });
    const d = await r.json();
    if (d && d.ok) {
      out.textContent = `${t('sc2.login_ok')} (${d.token_length} ${t('ui.chars')})`;
      out.style.color = '#3ecfaa';
      try {
        const c = await fetch('/api/config').then(x => x.json());
        if (c['soundcloud-oauth-token']) {
          const inp = document.getElementById('s-sc-oauth');
          if (inp) inp.value = c['soundcloud-oauth-token'];
        }
      } catch {}
      const pwd = document.getElementById('s-sc-pass'); if (pwd) pwd.value = '';
    } else {
      // Friendly path: SC killed the password API but the user already pasted
      // a cookie OAuth token — that's enough, no further action needed.
      const existing = (document.getElementById('s-sc-oauth')?.value || '').trim();
      if (d.removed_api && existing.length >= 20) {
        out.innerHTML = t('sc2.email_dead_html');
        out.style.color = 'var(--green)';
      } else {
        out.textContent = '✗ ' + (d.error || t('sc2.login_err'));
        out.style.color = '#ff5500';
      }
    }
  } catch (e) {
    out.textContent = t('au.net_pfx') + e.message;
    out.style.color = '#ff5500';
  }
}

// ── SoundCloud engine (Lucida) install ─────────────────────────────────────
async function scEngineCheck() {
  const lbl = document.getElementById('sc-engine-status');
  const btn = document.getElementById('sc-install-btn');
  if (!lbl || !btn) return;
  const r = await api('GET', '/api/soundcloud/status').catch(() => null);
  if (!r) { lbl.textContent = t('sc.eng_check_fail'); return; }
  if (r.installed) {
    lbl.innerHTML = t('sc.eng_installed')
      + (r.node_ver ? ` · Node ${esc(r.node_ver)}` : '');
    btn.disabled = false;
    btn.textContent = t('sc.eng_reinstall');
  } else if (!r.node_ok) {
    lbl.innerHTML = t('sc.eng_node_missing');
    btn.disabled = true;
  } else {
    lbl.innerHTML = t('sc.eng_not_installed');
    btn.disabled = false;
    btn.textContent = t('sc.eng_install');
  }
}

async function scInstallEngine() {
  const btn = document.getElementById('sc-install-btn');
  if (btn) { btn.disabled = true; btn.textContent = t('sc.eng_installing'); }
  toast(t('sc.eng_installing_toast'), '#ff5500');
  try {
    await api('POST', '/api/soundcloud/install');
  } catch(e) {
    toast(t('sc.eng_install_err') + ((e && e.message) || e), 'var(--red)');
  }
  // progress streams to the Console; completion arrives via WS 'soundcloud_installed'
  setTimeout(scEngineCheck, 2000);
}

