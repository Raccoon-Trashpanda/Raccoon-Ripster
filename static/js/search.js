// ======================================================================
// SEARCH / BROWSE (search grid + cards + artist/album/detail pages)
// Extracted from app.js (mechanical split — same global functions, no behaviour
// change). Loaded AFTER app.js in index.html, so it sees S/api/toast/etc.
// ======================================================================

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
  // Re-mark the currently-playing track (pause glyph + row highlight) — the rows
  // are freshly built, so without this a card reopened mid-playback shows no
  // indication of what's playing. Safe no-op when nothing is playing.
  try { if (typeof _syncAlbumPlayBtns === 'function') _syncAlbumPlayBtns(); } catch {}
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

