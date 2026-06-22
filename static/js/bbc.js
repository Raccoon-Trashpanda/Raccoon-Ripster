// Ripster BBC Sounds + MixesDB module — search/play/download BBC mixes.
// Loaded AFTER app.js + player.js (relies on Preview, esc, toast, t, S, etc).

// ── MixesDB ──────────────────────────────────────────────────────────────────

// ── BBC download progress ─────────────────────────────────────────────────────
const _bbcDls = {};   // pid → {title, pct}

function _bbcDlStart(pid, title) {
  _bbcDls[pid] = { title, pct: 0 };
  _bbcDlRender();
}
function _bbcDlProgress(pid, pct) {
  if (_bbcDls[pid]) { _bbcDls[pid].pct = pct; _bbcDlRender(); }
}
function _bbcDlDone(pid, title) {
  delete _bbcDls[pid];
  _bbcDlRender();
  toast(`✓ BBC скачано: ${title}`);
}
function _bbcDlRender() {
  const box = document.getElementById('bbc-dl-list');
  if (!box) return;
  const items = Object.entries(_bbcDls);
  if (!items.length) { box.style.display = 'none'; return; }
  box.style.display = '';
  box.innerHTML = items.map(([pid, d]) => `
    <div style="background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:9px 12px;margin-bottom:6px">
      <div style="display:flex;align-items:center;gap:8px;margin-bottom:6px">
        <span style="font-size:11px;color:var(--muted)">⬇</span>
        <span style="font-size:12px;font-weight:600;flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${d.title}</span>
        <span style="font-size:11px;font-family:var(--mono);color:var(--muted);flex-shrink:0">${d.pct}%</span>
      </div>
      <div style="height:4px;background:rgba(255,255,255,.06);border-radius:2px;overflow:hidden">
        <div style="height:100%;width:${d.pct}%;background:var(--red);border-radius:2px;transition:width .4s ease"></div>
      </div>
    </div>`).join('');
}

// ══════════════════════════════════════════════════════════════════════════════
// BBC SOUNDS
// ══════════════════════════════════════════════════════════════════════════════

const BBC = {
  brands:      [],
  activeBrand: "b006wkfp",
  offset:      0,
  limit:       20,
  total:       0,
  hls:         null,
  pid:         null,
  title:       null,
  art:         null,
  duration:    0,
  inited:      false,
  searching:   false,
};

async function bbcInit() {
  if (BBC.inited) return;
  BBC.inited = true;
  await bbcLoadBrands();
  bbcLoadEpisodes(true);
}

async function bbcLoadBrands() {
  try {
    const data = await api('GET', '/api/bbc/brands');
    BBC.brands = data.brands || [];
  } catch(e) {
    BBC.brands = [{id:"b006wkfp",label:"Essential Mix"}];
  }
  const el = document.getElementById('bbc-brands');
  if (!el) return;
  el.innerHTML = BBC.brands.map(b =>
    `<span onclick="bbcSelectBrand('${b.id}',this)" data-brand="${b.id}"
      style="padding:4px 10px;border-radius:20px;font-size:11px;font-weight:600;cursor:pointer;
             border:1px solid ${b.id===BBC.activeBrand?'var(--red)':'var(--border)'};
             color:${b.id===BBC.activeBrand?'var(--red)':'var(--muted)'};
             background:${b.id===BBC.activeBrand?'rgba(192,132,160,.12)':'var(--surface)'}">
      ${b.label}
    </span>`
  ).join('');
}

function bbcSelectBrand(id, el) {
  if (BBC.searching) { bbcClearSearch(); }
  BBC.activeBrand = id;
  document.querySelectorAll('#bbc-brands span').forEach(s => {
    const active = s.dataset.brand === id;
    s.style.borderColor  = active ? 'var(--red)' : 'var(--border)';
    s.style.color        = active ? 'var(--red)' : 'var(--muted)';
    s.style.background   = active ? 'rgba(192,132,160,.12)' : 'var(--surface)';
  });
  bbcLoadEpisodes(true);
}

async function bbcLoadEpisodes(reset) {
  if (reset) { BBC.offset = 0; BBC.total = 0; }
  const status = document.getElementById('bbc-status');
  const grid   = document.getElementById('bbc-grid');
  const more   = document.getElementById('bbc-more-btn');
  if (status) { status.textContent = 'Загружаю…'; status.style.display = ''; }
  if (more)   more.style.display = 'none';
  if (reset && grid) grid.innerHTML = '';
  try {
    const data = await api('GET', `/api/bbc/episodes?brand_id=${BBC.activeBrand}&offset=${BBC.offset}&limit=${BBC.limit}`);
    if (status) status.style.display = 'none';
    BBC.total = data.total || 0;
    BBC.offset += data.items.length;
    if (grid) grid.innerHTML += data.items.map(bbcCard).join('');
    if (more) more.style.display = BBC.offset < BBC.total ? '' : 'none';
    _bbcEnrichGrid();
  } catch(e) {
    if (status) { status.textContent = 'Ошибка загрузки BBC API'; status.style.display = ''; }
  }
}

function bbcLoadMore() { bbcLoadEpisodes(false); }

async function bbcSearch() {
  const q = (document.getElementById('bbc-q')?.value || '').trim();
  if (!q) { bbcClearSearch(); return; }
  BBC.searching = true;
  const status = document.getElementById('bbc-status');
  const grid   = document.getElementById('bbc-grid');
  const more   = document.getElementById('bbc-more-btn');
  const clr    = document.getElementById('bbc-clear-btn');
  if (status) { status.textContent = 'Ищу…'; status.style.display = ''; }
  if (grid)   grid.innerHTML = '';
  if (more)   more.style.display = 'none';
  if (clr)    clr.style.display = '';
  try {
    const data = await api('GET', `/api/bbc/search?q=${encodeURIComponent(q)}`);
    if (status) status.style.display = 'none';
    if (grid) grid.innerHTML = (data.items || []).map(bbcCard).join('');
    if (!data.items?.length) {
      if (status) { status.textContent = 'Ничего не найдено'; status.style.display = ''; }
    }
    _bbcEnrichGrid();
  } catch(e) {
    if (status) { status.textContent = 'Ошибка поиска'; status.style.display = ''; }
  }
}

function bbcClearSearch() {
  BBC.searching = false;
  const q   = document.getElementById('bbc-q');
  const clr = document.getElementById('bbc-clear-btn');
  if (q)   q.value = '';
  if (clr) clr.style.display = 'none';
  bbcLoadEpisodes(true);
}

// MixesDB match cache: pid → {found, artworkUrl, tracklist, url, page_title}
const _bbcMdb = new Map();

function bbcCard(ep) {
  const dur   = ep.duration ? _bbcFmtDur(ep.duration) : '';
  const date  = ep.date ? ep.date.slice(0,10) : '';
  const img   = ep.image || '';
  const title = ep.title || 'Без названия';
  const sub   = ep.subtitle || '';
  const pid   = ep.pid  || '';
  const vpid  = ep.vpid || '';
  const brandLabel = (BBC.brands.find(b => b.id === BBC.activeBrand) || {}).label || '';
  const imgAttr = img ? `src="${img}"` : '';
  return `
  <div id="bbccard-${pid}" data-bbc-pid="${pid}" data-bbc-title="${_esc(title)}" data-bbc-artist="${_esc(sub)}" data-bbc-img="${_esc(img)}" data-bbc-vpid="${_esc(vpid)}" data-bbc-brand="${_esc(brandLabel)}"
    style="background:var(--surface);border:1px solid var(--border);border-radius:12px;overflow:hidden;transition:border-color .15s"
    onmouseenter="this.style.borderColor='var(--border2)'" onmouseleave="this.style.borderColor='var(--border)'">
    <div style="position:relative;cursor:pointer" onclick="_bbcOpenMix('${pid}','${vpid}','${_esc(title)}','${_esc(sub)}','${_esc(img)}',${ep.duration||0},'${date}')" title="Открыть карточку микса">
      <img id="bbccard-img-${pid}" ${imgAttr} loading="lazy"
        style="width:100%;aspect-ratio:1/1;object-fit:cover;display:block;background:var(--surface2)"
        onerror="this.removeAttribute('src')"/>
      <div onclick="event.stopPropagation();bbcPlay('${pid}','${vpid}','${_esc(title)}','${_esc(sub)}','${_esc(img)}')" title="Играть" style="position:absolute;bottom:6px;right:6px;background:rgba(0,0,0,.72);border-radius:50%;width:32px;height:32px;display:flex;align-items:center;justify-content:center;font-size:13px;color:#fff;cursor:pointer">▶</div>
      ${dur ? `<div style="position:absolute;bottom:6px;left:6px;background:rgba(0,0,0,.72);border-radius:4px;font-size:10px;color:#fff;padding:2px 5px;font-family:var(--mono)">${dur}</div>` : ''}
      <div id="bbcmdb-badge-${pid}" style="display:none;position:absolute;top:6px;left:6px;background:rgba(175,82,222,.88);color:#fff;font-size:9px;padding:2px 7px;border-radius:4px;font-weight:700;backdrop-filter:blur(4px);cursor:pointer;user-select:none"
        onclick="event.stopPropagation();_bbcMdbTracklist('${pid}')" title="Трек-лист с MixesDB">🗄 MixesDB</div>
    </div>
    <div style="padding:8px 9px 9px">
      <div style="font-size:11.5px;font-weight:700;color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${title}</div>
      ${sub ? `<div style="font-size:10.5px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-top:1px">${sub}</div>` : ''}
      ${date ? `<div style="font-size:10px;color:var(--muted2);margin-top:3px">${date}</div>` : ''}
      <div id="bbcmdb-tl-${pid}" style="display:none;margin-top:6px;max-height:120px;overflow-y:auto;font-size:10px;color:var(--muted);line-height:1.5;border-top:1px solid var(--border);padding-top:5px"></div>
      <div style="display:flex;gap:5px;margin-top:7px">
        <button onclick="bbcDownloadSmart('${pid}','${vpid}','${_esc(title)}','${_esc(sub)}','${_esc(img)}')"
          style="flex:1;padding:5px 0;background:rgba(192,132,160,.12);border:1px solid rgba(192,132,160,.22);border-radius:7px;font-size:11px;font-weight:600;cursor:pointer;color:var(--red);font-family:var(--font)">
          ⬇ MP3
        </button>
        <button onclick="bbcGetCue('${pid}','${_esc(title)}','${_esc(sub)}')" title="Скачать CUE"
          style="padding:5px 9px;background:var(--surface2);border:1px solid var(--border);border-radius:7px;font-size:11px;cursor:pointer;color:var(--muted);font-family:var(--font)">
          CUE
        </button>
      </div>
    </div>
  </div>`;
}

// Called after grid renders — background MixesDB lookup for each card
function _bbcEnrichGrid() {
  const grid = document.getElementById('bbc-grid');
  if (!grid) return;
  const cards = grid.querySelectorAll('[data-bbc-pid]');
  for (const card of cards) {
    const pid    = card.dataset.bbcPid;
    const title  = card.dataset.bbcTitle  || '';
    const artist = card.dataset.bbcArtist || '';
    const brand  = card.dataset.bbcBrand  || '';
    if (!pid || _bbcMdb.has(pid)) continue;
    // Background fetch — don't await
    fetch(`/api/bbc/mixesdb/match?title=${encodeURIComponent(title)}&artist=${encodeURIComponent(artist)}&brand=${encodeURIComponent(brand)}`)
      .then(r => r.json())
      .then(d => {
        _bbcMdb.set(pid, d);
        if (d.found) {
          const badge = document.getElementById(`bbcmdb-badge-${pid}`);
          if (badge) badge.style.display = '';
          if (d.artworkUrl) {
            const imgEl = document.getElementById(`bbccard-img-${pid}`);
            if (imgEl) imgEl.src = d.artworkUrl;
          }
        }
      })
      .catch(() => {});
  }
}

function _bbcMdbTracklist(pid) {
  const tl  = document.getElementById(`bbcmdb-tl-${pid}`);
  if (!tl) return;
  if (tl.style.display !== 'none') { tl.style.display = 'none'; return; }
  const d = _bbcMdb.get(pid);
  if (!d?.tracklist?.length) { toast('Трек-лист не найден'); return; }
  tl.innerHTML = d.tracklist.map(t =>
    `<div style="display:flex;gap:5px;padding:1px 0"><span style="color:var(--muted2);font-family:var(--mono);flex-shrink:0">${esc(t.timestamp||'')}</span><span>${esc(t.artist?t.artist+' — ':'')}${esc(t.title)}</span></div>`
  ).join('');
  tl.style.display = '';
}

// Smart download: shows cover choice if MixesDB cover is available
async function bbcDownloadSmart(pid, vpid, title, artist, bbcImg) {
  const d = _bbcMdb.get(pid);
  if (d?.found && d.artworkUrl) {
    _bbcShowCoverChoice(pid, vpid, title, artist, bbcImg, d.artworkUrl);
  } else {
    await bbcDownload(pid, vpid, title, artist, bbcImg);
  }
}

function _bbcShowCoverChoice(pid, vpid, title, artist, bbcImg, mdbImg) {
  // Remove old choice UI if any
  document.getElementById('bbc-cover-choice')?.remove();

  const overlay = document.createElement('div');
  overlay.id = 'bbc-cover-choice';
  overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.72);z-index:9800;display:flex;align-items:center;justify-content:center;backdrop-filter:blur(6px)';
  overlay.onclick = e => { if (e.target === overlay) overlay.remove(); };

  const bbcImgUrl  = bbcImg  ? bbcImg.replace(/\{recipe\}|\d+x\d+/g, '400x400') : '';
  const mdbImgUrl  = mdbImg;

  overlay.innerHTML = `
    <div style="background:var(--surface);border:1px solid var(--border);border-radius:16px;padding:24px;max-width:480px;width:90%;box-shadow:0 24px 80px rgba(0,0,0,.6)">
      <div style="font-size:14px;font-weight:700;color:var(--text);margin-bottom:4px">Выбери обложку для скачивания</div>
      <div style="font-size:11px;color:var(--muted);margin-bottom:18px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(title)}</div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:18px">
        <div>
          <img src="${esc(bbcImgUrl)}" style="width:100%;aspect-ratio:1;object-fit:cover;border-radius:10px;border:2px solid transparent;cursor:pointer;transition:border-color .15s" id="bbc-cover-opt-bbc"
            onmouseover="this.style.borderColor='var(--red)'" onmouseout="this.style.borderColor='transparent'"/>
          <div style="font-size:10px;font-weight:700;color:var(--muted);text-align:center;margin-top:5px">BBC</div>
          <button onclick="document.getElementById('bbc-cover-choice').remove();bbcDownload('${_esc(pid)}','${_esc(vpid)}','${_esc(title)}','${_esc(artist)}','${_esc(bbcImg)}','')"
            style="width:100%;margin-top:6px;padding:6px 0;background:rgba(255,255,255,.08);border:1px solid var(--border);border-radius:7px;color:var(--text);font-size:11px;font-weight:600;cursor:pointer;font-family:var(--font)">Использовать</button>
        </div>
        <div>
          <img src="${esc(mdbImgUrl)}" style="width:100%;aspect-ratio:1;object-fit:cover;border-radius:10px;border:2px solid transparent;cursor:pointer;transition:border-color .15s" id="bbc-cover-opt-mdb"
            onmouseover="this.style.borderColor='#af52de'" onmouseout="this.style.borderColor='transparent'"/>
          <div style="font-size:10px;font-weight:700;color:#af52de;text-align:center;margin-top:5px">🗄 MixesDB</div>
          <button onclick="document.getElementById('bbc-cover-choice').remove();bbcDownload('${_esc(pid)}','${_esc(vpid)}','${_esc(title)}','${_esc(artist)}','${_esc(bbcImg)}','${_esc(mdbImg)}')"
            style="width:100%;margin-top:6px;padding:6px 0;background:rgba(175,82,222,.18);border:1px solid rgba(175,82,222,.35);border-radius:7px;color:#af52de;font-size:11px;font-weight:600;cursor:pointer;font-family:var(--font)">Использовать</button>
        </div>
      </div>
      <button onclick="document.getElementById('bbc-cover-choice').remove()"
        style="width:100%;padding:7px 0;background:transparent;border:1px solid var(--border);border-radius:8px;color:var(--muted);font-size:11px;cursor:pointer;font-family:var(--font)">Отмена</button>
    </div>`;
  document.body.appendChild(overlay);
}

function _esc(s) { return (s||'').replace(/'/g,"\\'").replace(/"/g,'&quot;').replace(/\n/g,' '); }
function _bbcFmtDur(s) {
  s = Math.floor(+s);
  const h = Math.floor(s/3600), m = Math.floor((s%3600)/60), ss = s%60;
  return h ? `${h}:${String(m).padStart(2,'0')}:${String(ss).padStart(2,'0')}`
           : `${m}:${String(ss).padStart(2,'0')}`;
}

async function bbcPlay(pid, vpid, title, artist, art) {
  // Stop any running service preview silently — BOTH paths:
  //  • pp-audio (plain <audio> previews)
  //  • the Web Audio buffer source (service streams play through the AudioContext,
  //    NOT through pp-audio — pausing pp-audio alone leaves it running under the
  //    BBC stream, causing double audio + its UI tick overwriting the player with
  //    the streaming-service track). Hard-stop the source so it can't auto-advance.
  const ppAudio = document.getElementById('pp-audio');
  if (ppAudio && !ppAudio.paused) { ppAudio.pause(); ppAudio.removeAttribute('src'); ppAudio.load(); }
  try {
    if (typeof _WA !== 'undefined' && _WA.curSource) {
      try { _WA.curSource.onended = null; _WA.curSource.stop(0); } catch {}
      _WA.curSource = null;
    }
    if (typeof _waStopKeepalive === 'function') _waStopKeepalive();
  } catch {}
  Preview.queue = [];
  Preview.idx   = -1;
  Preview.mode  = 'bbc';

  BBC.pid   = pid;
  BBC.title = title;
  BBC.art   = art;

  // Update global player UI
  const titleStr = artist ? `${title} — ${artist}` : title;
  document.getElementById('pp-title').textContent      = titleStr;
  document.getElementById('pp-artist').textContent     = '📻 BBC Sounds';
  document.getElementById('pp-title-big').textContent  = titleStr;
  document.getElementById('pp-artist-big').textContent = '📻 BBC Sounds';
  const coverHtml = art ? `<img src="${esc(art)}" style="width:100%;height:100%;object-fit:cover"/>` : '📻';
  ['pp-art','pp-art-big'].forEach(id => { const el = document.getElementById(id); if(el) el.innerHTML = coverHtml; });
  ['pp-fill','pp-fill-big'].forEach(id => { const el = document.getElementById(id); if(el) el.style.width = '0%'; });
  ['pp-cur','pp-cur-big'].forEach(id => { const el = document.getElementById(id); if(el) el.textContent = '0:00'; });
  ['pp-dur','pp-dur-big'].forEach(id => { const el = document.getElementById(id); if(el) el.textContent = '0:00'; });

  const playBtn  = document.getElementById('pp-play');
  const playBtnB = document.getElementById('pp-play-big');
  const prevBtn  = document.getElementById('pp-prev');
  const nextBtn  = document.getElementById('pp-next');
  if (playBtn)  playBtn.textContent  = '⟳';
  if (playBtnB) playBtnB.textContent = '⟳';
  if (prevBtn)  prevBtn.disabled = true;
  if (nextBtn)  nextBtn.disabled = true;

  const bar  = document.getElementById('preview-player');
  const main = document.querySelector('.main');
  if (bar) bar.classList.add('visible');
  if (main) {
    const isExpanded = document.getElementById('pp-expanded')?.style.display !== 'none';
    main.removeAttribute('data-preview-open');
    main.removeAttribute('data-preview-expanded');
    main.setAttribute(isExpanded ? 'data-preview-expanded' : 'data-preview-open', '1');
  }

  toast('BBC: получаю поток…');
  try {
    const qs = `pid=${encodeURIComponent(pid)}${vpid ? '&vpid=' + encodeURIComponent(vpid) : ''}&name=${encodeURIComponent(title || pid)}`;
    const info = await api('GET', `/api/bbc/stream?${qs}`);
    if (!info.url) throw new Error(info.detail || 'Нет URL потока');
    BBC.duration = info.duration || 0;
    _bbcStartStream(info.url);
    // 1001Tracklists — authoritative tracklist + cue-time chapters for the mix.
    if (typeof _playerSetChapters === 'function') _playerSetChapters([]);
    BBC._ticksDone = false;
    _bbcFetch1001(pid, title, artist, BBC.duration);
  } catch(e) {
    toast('Ошибка потока BBC: ' + e.message);
    if (playBtn)  playBtn.textContent  = '▶';
    if (playBtnB) playBtnB.textContent = '▶';
  }
}

function _bbcStartStream(url) {
  const audio    = document.getElementById('bbc-audio');
  const playBtn  = document.getElementById('pp-play');
  const playBtnB = document.getElementById('pp-play-big');
  if (!audio) return;

  if (BBC.hls) { BBC.hls.destroy(); BBC.hls = null; }

  if (typeof Hls !== 'undefined' && Hls.isSupported()) {
    const hls = new Hls({ enableWorker: false });
    BBC.hls = hls;
    hls.loadSource(url);
    hls.attachMedia(audio);
    hls.on(Hls.Events.MANIFEST_PARSED, () => {
      audio.play();
      if (playBtn)  playBtn.textContent  = '⏸';
      if (playBtnB) playBtnB.textContent = '⏸';
    });
    hls.on(Hls.Events.ERROR, (ev, data) => {
      if (data.fatal) toast('HLS ошибка: ' + data.type);
    });
  } else if (audio.canPlayType('application/vnd.apple.mpegurl')) {
    audio.src = url;
    audio.play();
    if (playBtn)  playBtn.textContent  = '⏸';
    if (playBtnB) playBtnB.textContent = '⏸';
  } else {
    toast('HLS не поддерживается в этом браузере');
    return;
  }

  audio.ontimeupdate = _bbcTimeUpdate;
  audio.onended      = () => {
    if (playBtn)  playBtn.textContent  = '▶';
    if (playBtnB) playBtnB.textContent = '▶';
  };
  // Resume a long mix from its saved position.
  const _bbcResumeAt = _mixPosGet('bbc:' + (BBC.pid || ''));
  if (_bbcResumeAt > 0) {
    audio.addEventListener('loadedmetadata', function _r() {
      audio.removeEventListener('loadedmetadata', _r);
      if (audio.duration && _bbcResumeAt < audio.duration - 20) {
        try { audio.currentTime = _bbcResumeAt; } catch(_) {}
        toast(`▶ Продолжаю с ${_bbcFmtDur(_bbcResumeAt)}`, 'var(--muted)', 2600);
      }
    });
  }
}

function _bbcTimeUpdate() {
  const audio = document.getElementById('bbc-audio');
  if (!audio) return;
  const cur    = audio.currentTime || 0;
  const dur    = audio.duration   || BBC.duration || 0;
  const pct    = dur ? (cur / dur * 100) + '%' : '0%';
  const curStr = _bbcFmtDur(cur);
  const durStr = _bbcFmtDur(dur);
  ['pp-fill','pp-fill-big'].forEach(id => { const el = document.getElementById(id); if(el) el.style.width = pct; });
  ['pp-cur','pp-cur-big'].forEach(id => { const el = document.getElementById(id); if(el) el.textContent = curStr; });
  ['pp-dur','pp-dur-big'].forEach(id => { const el = document.getElementById(id); if(el) el.textContent = durStr; });
  _mixPosSave('bbc:' + (BBC.pid || ''), cur, dur);
  // 1001Tracklists chapters: highlight current track + draw ticks once duration known.
  if (typeof _updateCurrentChapter === 'function') _updateCurrentChapter(cur);
  if (!BBC._ticksDone && dur && (Preview._chapters || []).length && typeof _renderChapterTicks === 'function') {
    _renderChapterTicks(); BBC._ticksDone = true;
  }
}

// 1001Tracklists for a BBC mix: authoritative tracklist + cue-time chapters.
// One fetch (server verifies + disk-caches 10 days), reused for both the player
// chapters and the detail drawer. cb(result{tracks,chapters,url,found}).
const _bbc1001 = new Map();   // pid -> {found, tracks, chapters, url}
function _bbcFetch1001(pid, title, artist, dur, cb) {
  const applyChapters = (r) => {
    if (Preview.mode === 'bbc' && BBC.pid === pid && r.chapters?.length &&
        typeof _playerSetChapters === 'function') {
      _playerSetChapters(r.chapters); BBC._ticksDone = false;
    }
  };
  const cached = _bbc1001.get(pid);
  if (cached) { applyChapters(cached); cb?.(cached); return; }
  fetch('/api/soundcloud/tracklist-1001', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ title: title || '', artist: artist || '',
                           dur: Math.round(dur || 0), id: 'bbc_' + pid }),
  })
    .then(r => r.json())
    .then(res => {
      const tracks = (res && res.found ? res.tracks : []) || [];
      const chapters = tracks
        .filter(t => t.seconds != null)
        .map(t => ({ seconds: t.seconds,
                     label: (t.artist ? t.artist + ' — ' : '') + (t.title || '') }))
        .filter(c => c.label);
      const r = { found: !!(res && res.found), tracks, chapters, url: res?.url };
      _bbc1001.set(pid, r);
      applyChapters(r);
      cb?.(r);
    })
    .catch(() => cb?.({ found: false, tracks: [], chapters: [] }));
}

// Render a tracklist into a container in the unified SC style (.scd-trk rows).
function _bbcRenderTL(tl, tracks, creditLabel) {
  if (!tl || !tracks || !tracks.length) return;
  const nm = (tr) => (tr.artist ? tr.artist + ' — ' : '') + (tr.title || '');
  const credit = `<div style="display:flex;align-items:center;justify-content:space-between;gap:8px;font-size:10px;color:var(--muted2);margin-bottom:8px;padding-bottom:6px;border-bottom:1px solid var(--border)"><span>${esc(creditLabel)} · ${tracks.length} тр.</span></div>`;
  tl.innerHTML = credit + tracks.map((tr, i) => {
    const tsEl = tr.timestamp
      ? `<span class="ts" onclick="previewSeekTo(${tr.seconds || 0})" title="Перемотать">${esc(tr.timestamp)}</span>`
      : '';
    return `<div class="scd-trk"><span class="n">${i+1}</span>${tsEl}<span class="nm">${esc(nm(tr))}</span></div>`;
  }).join('');
}

// Load a BBC mix tracklist into the drawer: MixesDB instantly (if cached), then
// 1001Tracklists takes priority when found — same source logic as SoundCloud.
function _bbcLoadTracklistInto(pid, title, artist, dur, tl) {
  if (!tl) return;
  const mdb = (_bbcMdb.get(pid) || {}).tracklist || [];
  if (mdb.length) _bbcRenderTL(tl, mdb, '🗄 MixesDB');
  else tl.innerHTML = `<div style="font-size:11px;color:var(--muted2)">⏱ Ищу трек-лист…</div>`;
  _bbcFetch1001(pid, title, artist, dur, (r) => {
    if (r.found && r.tracks.length) _bbcRenderTL(tl, r.tracks, '🎚 1001Tracklists');
    else if (!mdb.length) tl.innerHTML = `<div style="font-size:11px;color:var(--muted2)">Трек-лист не найден</div>`;
  });
}

// Unified mix-detail drawer for BBC — reuses the SoundCloud drawer (#sc-detail).
function _bbcDetailHTML(pid, vpid, title, artist, art, dur, date) {
  const meta = [];
  if (date) meta.push(esc(date));
  if (dur)  meta.push(_bbcFmtDur(dur));
  const btn = (bg, bd, clr) => `flex:1;padding:8px 0;border:1px solid ${bd};background:${bg};border-radius:8px;font-size:12px;font-weight:600;color:${clr};cursor:pointer;font-family:var(--font)`;
  const j = (s) => (s || '').replace(/'/g, "\\'").replace(/"/g, '&quot;');
  return `
    <div class="scd-head" style="display:flex;align-items:center;justify-content:space-between;padding:14px 16px 10px;border-bottom:1px solid var(--border);flex:0 0 auto">
      <div style="font-size:13px;font-weight:700;color:var(--text)">📻 BBC · карточка микса</div>
      <button class="scd-close" onclick="_scCloseMix()" title="Закрыть (Esc)" style="width:30px;height:30px;border-radius:8px;border:1px solid var(--border);background:transparent;color:var(--muted);cursor:pointer;font-size:15px;flex:0 0 auto;display:flex;align-items:center;justify-content:center;line-height:1;padding:0">✕</button>
    </div>
    <div class="scd-body" style="overflow-y:auto;padding:16px 16px 168px;flex:1 1 auto">
      ${art ? `<img class="scd-cover" src="${esc(art)}" onerror="this.removeAttribute('src')" style="width:100%;aspect-ratio:1;object-fit:cover;border-radius:12px;display:block;background:var(--surface2)"/>` : ''}
      <div style="font-size:16px;font-weight:700;color:var(--text);margin-top:13px;line-height:1.3">${esc(title)}</div>
      <div style="font-size:13px;color:var(--muted);margin-top:3px">${esc(artist || '📻 BBC Sounds')}</div>
      ${meta.length ? `<div style="font-size:11px;color:var(--muted2);margin-top:7px">${meta.join('  ·  ')}</div>` : ''}
      <div style="display:flex;gap:7px;margin-top:14px">
        <button onclick="bbcPlay('${j(pid)}','${j(vpid)}','${j(title)}','${j(artist)}','${j(art)}')" style="${btn('rgba(255,85,0,.14)','rgba(255,85,0,.25)','#ff7a33')}">▶ Играть</button>
        <button onclick="bbcDownloadSmart('${j(pid)}','${j(vpid)}','${j(title)}','${j(artist)}','${j(art)}')" style="${btn('rgba(255,255,255,.06)','var(--border)','var(--text)')}">⬇ MP3</button>
        <button onclick="bbcGetCue('${j(pid)}','${j(title)}','${j(artist)}')" title="Скачать CUE" style="padding:8px 11px;border:1px solid var(--border);border-radius:8px;font-size:13px;color:var(--muted);background:transparent;cursor:pointer;font-family:var(--font)">CUE</button>
      </div>
      <div style="font-size:11px;font-weight:700;color:var(--muted);margin:18px 0 8px;text-transform:uppercase;letter-spacing:.4px">Трек-лист</div>
      <div id="scd-tl"></div>
    </div>`;
}

function _bbcOpenMix(pid, vpid, title, artist, art, dur, date) {
  // Reuse the SoundCloud drawer shell (#sc-detail + backdrop + _scCloseMix).
  let bd = document.getElementById('sc-detail-backdrop');
  if (!bd) {
    bd = document.createElement('div'); bd.id = 'sc-detail-backdrop'; bd.onclick = _scCloseMix;
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
  d.innerHTML = _bbcDetailHTML(pid, vpid, title, artist, art, dur, date);
  bd.style.display = 'block'; bd.classList.add('show');
  requestAnimationFrame(() => { d.classList.add('open'); d.style.transform = 'translateX(0)'; });
  _bbcLoadTracklistInto(pid, title, artist, dur, d.querySelector('#scd-tl'));
}

function bbcTogglePlay() {
  const audio    = document.getElementById('bbc-audio');
  const playBtn  = document.getElementById('pp-play');
  const playBtnB = document.getElementById('pp-play-big');
  if (!audio) return;
  if (audio.paused) {
    audio.play();
    if (playBtn)  playBtn.textContent  = '⏸';
    if (playBtnB) playBtnB.textContent = '⏸';
  } else {
    audio.pause();
    if (playBtn)  playBtn.textContent  = '▶';
    if (playBtnB) playBtnB.textContent = '▶';
  }
}

function bbcStop() {
  const audio = document.getElementById('bbc-audio');
  if (BBC.hls) { BBC.hls.destroy(); BBC.hls = null; }
  if (audio)   { audio.pause(); audio.src = ''; }
  if (Preview.mode === 'bbc') {
    Preview.mode  = 'spotify';
    if (typeof _playerSetChapters === 'function') _playerSetChapters([]);
    const bar  = document.getElementById('preview-player');
    const main = document.querySelector('.main');
    const exp  = document.getElementById('pp-expanded');
    const btn  = document.getElementById('pp-expand-btn');
    if (bar)   bar.classList.remove('visible');
    if (exp)   exp.style.display = 'none';
    if (btn)   btn.classList.remove('expanded');
    if (main)  { main.removeAttribute('data-preview-open'); main.removeAttribute('data-preview-expanded'); }
    const playBtn  = document.getElementById('pp-play');     if (playBtn)  playBtn.textContent  = '▶';
    const playBtnB = document.getElementById('pp-play-big'); if (playBtnB) playBtnB.textContent = '▶';
  }
}

function bbcSeek(pct) {
  const audio = document.getElementById('bbc-audio');
  if (!audio || !audio.duration) return;
  audio.currentTime = (pct / 100) * audio.duration;
}

function bbcVol(v) {
  const audio = document.getElementById('bbc-audio');
  const fv = parseFloat(v);
  if (audio) audio.volume = fv;
  ['pp-vol','pp-vol-big'].forEach(id => { const el = document.getElementById(id); if(el) el.value = fv; });
}

async function bbcDownload(pid, vpid, title, artist, image_url, cover_url = '') {
  toast(`Скачиваю: ${title}…`);
  try {
    await api('POST', '/api/bbc/download', {
      pid, vpid: vpid || '', title, artist: artist || 'BBC Radio',
      image_url: image_url || '', cover_url: cover_url || ''
    });
    toast(`⬇ BBC: ${title} — скачивание запущено`);
  } catch(e) {
    toast('Ошибка загрузки BBC: ' + e.message);
  }
}

async function bbcGetCue(pid, title, artist) {
  const url = `/api/bbc/cue?pid=${pid}&title=${encodeURIComponent(title)}&artist=${encodeURIComponent(artist||'BBC Radio')}`;
  try {
    const r = await fetch(url);
    if (!r.ok) { toast('Нет трек-листа для этого эпизода'); return; }
    const blob = await r.blob();
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `${title}.cue`;
    a.click();
    toast('CUE скачан');
  } catch(e) {
    toast('Ошибка CUE: ' + e.message);
  }
}

// ── Load app info
