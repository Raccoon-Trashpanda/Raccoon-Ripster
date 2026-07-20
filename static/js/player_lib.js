// ======================================================================
// Media Session + local library + play-album + quality + spectrogram
// Extracted from app.js (mechanical split — same global functions, no behaviour
// change). Loaded AFTER app.js in index.html, so it sees S/api/toast/etc.
// ======================================================================

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
  if (status) { status.textContent = '⟳ ' + t('lib.scanning'); status.style.display = 'block'; }
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
      `<div style="padding:14px;text-align:center;color:var(--muted);font-size:11px">+${items.length - 500} ${t('lib.more_refine')}</div>`);
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
    <button onclick="event.stopPropagation();_libCopyPath('${escJ(it.path)}')" style="padding:3px 7px;background:transparent;border:1px solid var(--border);border-radius:5px;font-size:10px;color:var(--muted);cursor:pointer;font-family:var(--font);flex-shrink:0" title="${t('lib.copy_path')}">⎘</button>
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
    label:  `${t('nav.library')} · ${it.album || ''}`.trim(),
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
        label:   `${_svcLabel(service)} · ${album.title || fallbackTitle || t('card.album')}`,
        posKey:  `${service}:${t.id}`,
      }));
    if (!Preview.queue.length) { toast(t('toast.no_tracks'), 'var(--orange)'); return; }
    Preview.idx = 0;
    toast(`▶ ${album.title || fallbackTitle}: ${Preview.queue.length} ${t('p.trk_abbr')}`,
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
      label:   `${_svcLabel(service)} · ${album.title || t('card.album')}`,
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
  toast(`▶ ${album.title}: ${q.length} ${t('p.trk_abbr')}`, 'var(--green)', '', 2500);
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
    if (!r.ok || d.detail || d.error) throw new Error(d.detail || d.error || t('err.generic'));
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
    if (!r.ok || d.detail || d.error) throw new Error(d.detail || d.error || t('err.generic'));
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
    [t('lib.format'), d.format], [t('lib.codec'), d.codec], [t('lib.bitrate'), d.bitrate],
    [t('lib.samplerate'), d.sample_rate], [t('lib.bitdepth'), d.bit_depth],
    [t('lib.channels'), d.channels], [t('lib.duration'), d.duration],
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
  vd.textContent  = d.verdict_text || (ok ? t('lib.true_lossless') : t('lib.lossy_src'));
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
      toast(r.detail || r.msg || t('err.generic'), 'var(--red)');
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
  if (old) { old.remove(); if (btn) { btn.textContent = t('lib.find_better'); btn.disabled = false; } return; }

  const task = (S.queue || []).find(t => t.id === taskId);
  const title  = task?.meta?.title  || task?.title  || '';
  const artist = task?.meta?.artist || task?.artist || '';
  const url    = task?.url || '';

  if (btn) { btn.textContent = t('lib.searching'); btn.disabled = true; }
  try {
    const d = await api('POST', '/api/isrc-upgrade', {url, title, artist});
    if (btn) { btn.textContent = t('lib.find_better'); btn.disabled = false; }

    const SVC = {apple:'🍎',deezer:'🎧',qobuz:'🎵',tidal:'🌊'};
    const QC  = {apple:'#c084a0',deezer:'#3ecfaa',qobuz:'#ffd60a',tidal:'#00d4b3'};
    const panel = document.createElement('div');
    panel.className = 'isrc-results';
    panel.style.cssText = 'margin-top:6px;display:flex;flex-direction:column;gap:4px';

    if (!d.results?.length) {
      panel.innerHTML = `<div style="font-size:11px;color:var(--muted);padding:4px 0">${t('lib.not_found_on')}</div>`;
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
          ${r.match==='exact'?'<span style="color:#22c55e;font-size:10px;flex-shrink:0" title="' + t('lib.isrc_exact') + '">✓ISRC</span>':''}
          <button class="isrc-add-btn" style="padding:3px 8px;background:var(--red);color:#fff;border:none;border-radius:5px;font-size:10px;font-weight:700;cursor:pointer;flex-shrink:0">${t('lib.to_queue')}</button>
        `;
        const addBtn = row.querySelector('.isrc-add-btn');
        const _url = r.url, _title = r.title, _artist = r.artist, _svc = r.service;
        addBtn.onclick = () => isrcUpgradeAdd(_url, _title, _artist, _svc, addBtn);
        panel.appendChild(row);
      }
    }
    bar.appendChild(panel);
  } catch(e) {
    if (btn) { btn.textContent = t('lib.find_better'); btn.disabled = false; }
    toast('Ошибка поиска: ' + e.message, 'var(--red)');
  }
}

async function isrcUpgradeAdd(url, title, artist, service, btn) {
  if (!url) { toast('Нет URL', 'var(--red)'); return; }
  if (btn) { btn.textContent = '…'; btn.disabled = true; }
  const r = await api('POST', '/api/queue/add', {url, title, artist});
  if (r.ok) toast(`+ ${title} [${service}] ${t('lib.queued')}`);
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
      alert(t('lib.upload_error') + (res.detail || res.error || JSON.stringify(res)));
    }
  } catch(e) {
    if (btn) { btn.textContent = '☁'; btn.disabled = false; }
    alert(t('ui.err_pfx') + e);
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
  modal.innerHTML = `<div style="background:var(--surface,#1c1c1e);border:1px solid var(--border);border-radius:16px;padding:24px;width:380px;max-width:90vw">
    <div style="font-size:16px;font-weight:700;color:var(--text);margin-bottom:6px">👋 Добро пожаловать в Ripster</div>
    <div style="font-size:12px;color:var(--muted,#888);margin-bottom:16px">Как тебя подписать для разработчика? Имя/ник поможет понять, чей это Ripster, если пришлёшь диагностику. Можно пропустить — спросим только один раз.</div>
    <input id="firstrun-name-input" type="text" maxlength="48" placeholder="Имя или ник"
      style="width:100%;padding:10px 12px;background:var(--surface2);border:1px solid var(--border);border-radius:9px;color:var(--text);font-size:15px;box-sizing:border-box;outline:none"
      onkeydown="if(event.key==='Enter') _saveFirstRunName()">
    <div style="display:flex;gap:8px;margin-top:14px">
      <button onclick="_saveFirstRunName()" style="flex:1;padding:10px;background:#0a84ff;border:none;border-radius:9px;cursor:pointer;color:#fff;font-weight:600;font-size:13px;font-family:var(--font)">Сохранить</button>
      <button onclick="_skipFirstRunName()" style="padding:10px 16px;background:transparent;border:1px solid var(--border);border-radius:9px;cursor:pointer;font-size:13px;color:var(--muted,#888);font-family:var(--font)">Пропустить</button>
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









