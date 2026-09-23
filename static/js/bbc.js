// Ripster BBC Sounds + MixesDB module — search/play/download BBC mixes.
// Loaded AFTER app.js + player.js (relies on Preview, esc, toast, t, S, etc).

// ── MixesDB ──────────────────────────────────────────────────────────────────

// ── BBC download progress ─────────────────────────────────────────────────────
const _bbcDls = {};   // pid → {title, pct}
const _bbcDlQ = {};   // pid → {source_kbps, target_kbps} — измеренные сервером до кодирования

function _bbcDlStart(pid, title) {
  _bbcDls[pid] = { title, pct: 0 };
  _bbcDlRender();
}
function _bbcDlProgress(pid, pct) {
  if (_bbcDls[pid]) { _bbcDls[pid].pct = pct; _bbcDlRender(); }
}
function _bbcDlDone(pid, title) {
  const q = _bbcDlQ[pid] || {};
  delete _bbcDls[pid];
  delete _bbcDlQ[pid];
  _bbcDlRender();
  // «320» в подписи не было бы правдой: BBC in-demand отдаёт не больше
  // 102 кбит/с, и MP3 пишется по этой полосе. Пишем обе цифры — источник и цель.
  toast(t('b.done_c') + title +
        (q.source_kbps ? ti('b.done_kbps', {src: q.source_kbps, kbps: q.target_kbps}) : ''));
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
        <span style="font-size:12px;font-weight:600;flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(d.title)}</span>
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
  upcoming:    false,   // сетка показывает будущие эфиры (вход планировщика)
};

async function bbcInit() {
  if (BBC.inited) return;
  BBC.inited = true;
  bbcLoadChannels();       // список каналов для формы отложенной записи
  await _bbcLoadSched();   // какие эфиры уже запланированы — чтобы знала и панель
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
    `<span onclick="bbcSelectBrand('${_esc(b.id)}',this)" data-brand="${esc(b.id)}"
      style="padding:4px 10px;border-radius:20px;font-size:11px;font-weight:600;cursor:pointer;
             border:1px solid ${b.id===BBC.activeBrand?'var(--red)':'var(--border)'};
             color:${b.id===BBC.activeBrand?'var(--red)':'var(--muted)'};
             background:${b.id===BBC.activeBrand?'rgba(192,132,160,.12)':'var(--surface)'}">
      ${esc(b.label)}
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
  // Guard: an empty brand_id makes the BBC API return 400 → "BBC API 400" (502).
  if (!BBC.activeBrand) BBC.activeBrand = (BBC.brands[0] && BBC.brands[0].id) || 'b006wkfp';
  if (reset) { BBC.offset = 0; BBC.total = 0; }
  const status = document.getElementById('bbc-status');
  const grid   = document.getElementById('bbc-grid');
  const more   = document.getElementById('bbc-more-btn');
  if (status) { status.textContent = t('b.loading'); status.style.display = ''; }
  if (more)   more.style.display = 'none';
  if (reset && grid) grid.innerHTML = '';
  try {
    // Будущие эфиры — тот же вид карточки, что и у вышедших: планирование уже
    // живёт в bbcCard, ему нужен только вход с avail_from в будущем.
    const data = BBC.upcoming
      ? await api('GET', `/api/bbc/upcoming?brand_id=${encodeURIComponent(BBC.activeBrand)}`)
      : await api('GET', `/api/bbc/episodes?brand_id=${BBC.activeBrand}&offset=${BBC.offset}&limit=${BBC.limit}`);
    // api() returns the JSON body even on a non-2xx (e.g. 502 {"detail":"BBC API 400"}
    // or 401 {"error":...}) — so a missing `items` is really that upstream error.
    if (!data || !Array.isArray(data.items)) {
      throw new Error((data && (data.detail || data.error)) || t('b.bad_resp'));
    }
    if (status) status.style.display = 'none';
    BBC.total = data.total || 0;
    BBC.offset += data.items.length;
    if (grid) grid.innerHTML += data.items.map(bbcCard).join('');
    if (more) more.style.display = BBC.offset < BBC.total ? '' : 'none';
    if (BBC.upcoming && !data.items.length && status) {
      // Пустая сетка без объяснения выглядит как сломанная вкладка.
      status.textContent = t('bbc.no_upcoming'); status.style.display = '';
    }
    _bbcEnrichGrid();
    _bbcApplySchedBtns();
  } catch(e) {
    // surface the REAL cause (HTTP 401 = session not carried, 502 = BBC upstream)
    if (status) { status.textContent = t('b.load_err_c') + (e && e.message || e); status.style.display = ''; }
  }
}

function bbcLoadMore() { bbcLoadEpisodes(false); }

async function bbcSearch() {
  const q = (document.getElementById('bbc-q')?.value || '').trim();
  if (!q) { bbcClearSearch(); return; }
  // Поиск идёт по архиву BBC (там нет будущих передач) — режим «ближайшие
  // эфиры» честно выключается, иначе подпись вкладки врала бы о содержимом.
  if (BBC.upcoming) { BBC.upcoming = false; bbcModeButtons(); }
  BBC.searching = true;
  const status = document.getElementById('bbc-status');
  const grid   = document.getElementById('bbc-grid');
  const more   = document.getElementById('bbc-more-btn');
  const clr    = document.getElementById('bbc-clear-btn');
  if (status) { status.textContent = t('b.searching'); status.style.display = ''; }
  if (grid)   grid.innerHTML = '';
  if (more)   more.style.display = 'none';
  if (clr)    clr.style.display = '';
  try {
    const data = await api('GET', `/api/bbc/search?q=${encodeURIComponent(q)}`);
    if (status) status.style.display = 'none';
    if (grid) grid.innerHTML = (data.items || []).map(bbcCard).join('');
    if (!data.items?.length) {
      if (status) { status.textContent = t('b.nothing'); status.style.display = ''; }
    }
    _bbcEnrichGrid();
    _bbcApplySchedBtns();
  } catch(e) {
    if (status) { status.textContent = t('b.search_err'); status.style.display = ''; }
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
  const title = ep.title || t('b.untitled');
  const sub   = ep.subtitle || '';
  const pid   = ep.pid  || '';
  const vpid  = ep.vpid || '';
  const brandLabel = (BBC.brands.find(b => b.id === BBC.activeBrand) || {}).label || '';
  const imgAttr = img ? `src="${esc(img)}"` : '';
  // Будущий эфир записывается с LIVE-потока канала (планировщик — bbc_schedule).
  const start = _bbcLiveFuture(ep);
  const keyAttr = start ? `data-key="${esc((ep.channel||'')+'|'+start)}"` : '';
  return `
  <div id="bbccard-${pid}" data-bbc-pid="${pid}" data-bbc-upcoming="${ep.upcoming?1:''}" data-bbc-title="${_escA(title)}" data-bbc-artist="${_escA(sub)}" data-bbc-img="${_escA(img)}" data-bbc-vpid="${_escA(vpid)}" data-bbc-brand="${_escA(brandLabel)}" data-bbc-date="${_escA(date)}"
    style="background:var(--surface);border:1px solid var(--border);border-radius:12px;overflow:hidden;transition:border-color .15s"
    onmouseenter="this.style.borderColor='var(--border2)'" onmouseleave="this.style.borderColor='var(--border)'">
    <div style="position:relative;cursor:pointer" onclick="_bbcOpenMix('${pid}','${vpid}','${_esc(title)}','${_esc(sub)}','${_esc(img)}',${ep.duration||0},'${_esc(date)}')" title="${t('b.open_mix')}">
      <img id="bbccard-img-${pid}" ${imgAttr} loading="lazy"
        style="width:100%;aspect-ratio:1/1;object-fit:cover;display:block;background:var(--surface2)"
        onerror="_bbcCoverFail(this)"/>
      ${start ? '' : `<div onclick="event.stopPropagation();bbcPlay('${pid}','${vpid}','${_esc(title)}','${_esc(sub)}','${_esc(img)}')" title="${t('btn.play')}" style="position:absolute;bottom:6px;right:6px;background:rgba(0,0,0,.72);border-radius:50%;width:32px;height:32px;display:flex;align-items:center;justify-content:center;font-size:13px;color:#fff;cursor:pointer">▶</div>`}
      ${dur ? `<div style="position:absolute;bottom:6px;left:6px;background:rgba(0,0,0,.72);border-radius:4px;font-size:10px;color:#fff;padding:2px 5px;font-family:var(--mono)">${dur}</div>` : ''}
      <div id="bbcmdb-badge-${pid}" style="display:none;position:absolute;top:6px;left:6px;background:rgba(175,82,222,.88);color:#fff;font-size:9px;padding:2px 7px;border-radius:4px;font-weight:700;backdrop-filter:blur(4px);cursor:pointer;user-select:none"
        onclick="event.stopPropagation();_bbcMdbTracklist('${pid}')" title="${t('b.tl_mdb')}">🗄 MixesDB</div>
    </div>
    <div style="padding:8px 9px 9px">
      <div style="font-size:11.5px;font-weight:700;color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(title)}</div>
      ${sub ? `<div style="font-size:10.5px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-top:1px">${esc(sub)}</div>` : ''}
      ${date ? `<div style="font-size:10px;color:var(--muted2);margin-top:3px">${esc(date)}</div>` : ''}
      <div id="bbcmdb-tl-${pid}" style="display:none;margin-top:6px;max-height:120px;overflow-y:auto;font-size:10px;color:var(--muted);line-height:1.5;border-top:1px solid var(--border);padding-top:5px"></div>
      ${start ? `
      ${_bbcQHTML(ep)}
      <div style="display:flex;gap:5px;margin-top:7px">
        <button class="bbc-sched-btn" ${keyAttr} data-pid="${esc(pid)}" onclick="event.stopPropagation();bbcScheduleToggle(this,'${_esc(ep.channel||'')}','${_esc(start)}',${ep.duration||0},'${_esc(title)}','${_esc(sub)}','${_esc(img)}')"
          style="flex:1;padding:5px 0;border-radius:7px;font-size:11px;font-weight:600;cursor:pointer;font-family:var(--font)"></button>
      </div>` : `
      <div style="display:flex;gap:5px;margin-top:7px">
        <button onclick="bbcDownloadSmart('${pid}','${vpid}','${_esc(title)}','${_esc(sub)}','${_esc(img)}')"
          style="flex:1;padding:5px 0;background:rgba(192,132,160,.12);border:1px solid rgba(192,132,160,.22);border-radius:7px;font-size:11px;font-weight:600;cursor:pointer;color:var(--red);font-family:var(--font)">
          ⬇ MP3
        </button>
        <button onclick="bbcGetCue('${pid}','${_esc(title)}','${_esc(sub)}')" title="${t('b.dl_cue')}"
          style="padding:5px 9px;background:var(--surface2);border:1px solid var(--border);border-radius:7px;font-size:11px;cursor:pointer;color:var(--muted);font-family:var(--font)">
          CUE
        </button>
      </div>`}
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
    // Air date keeps "same DJ, another year" out; a Classic re-air names the
    // set's own year in the title — then that year is the one to match.
    const aired  = (card.dataset.bbcDate || '').slice(0, 10);
    const yrs    = (title + ' ' + artist).match(/\b(?:19|20)\d{2}\b/g) || [];
    const when   = (yrs.length === 1 && !aired.startsWith(yrs[0])) ? yrs[0] : aired;
    if (!pid || _bbcMdb.has(pid)) continue;
    // Background fetch — don't await
    fetch(`/api/bbc/mixesdb/match?title=${encodeURIComponent(title)}&artist=${encodeURIComponent(artist)}&brand=${encodeURIComponent(brand)}&date=${encodeURIComponent(when)}`)
      .then(r => r.json())
      .then(d => {
        _bbcMdb.set(pid, d);
        if (d.found) {
          const badge = document.getElementById(`bbcmdb-badge-${pid}`);
          if (badge) badge.style.display = '';
          if (d.artworkUrl) {
            const imgEl = document.getElementById(`bbccard-img-${pid}`);
            if (imgEl) {
              if (!imgEl.dataset.bbcFallback) imgEl.dataset.bbcFallback = imgEl.getAttribute('src') || '';
              imgEl.src = d.artworkUrl;
            }
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
  if (!d?.tracklist?.length) { toast(t('b.no_tl')); return; }
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
      <div style="font-size:14px;font-weight:700;color:var(--text);margin-bottom:4px">${t('b.pick_cover')}</div>
      <div style="font-size:11px;color:var(--muted);margin-bottom:18px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(title)}</div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:18px">
        <div>
          <img src="${esc(bbcImgUrl)}" style="width:100%;aspect-ratio:1;object-fit:cover;border-radius:10px;border:2px solid transparent;cursor:pointer;transition:border-color .15s" id="bbc-cover-opt-bbc"
            onmouseover="this.style.borderColor='var(--red)'" onmouseout="this.style.borderColor='transparent'"/>
          <div style="font-size:10px;font-weight:700;color:var(--muted);text-align:center;margin-top:5px">BBC</div>
          <button onclick="document.getElementById('bbc-cover-choice').remove();bbcDownload('${_esc(pid)}','${_esc(vpid)}','${_esc(title)}','${_esc(artist)}','${_esc(bbcImg)}','')"
            style="width:100%;margin-top:6px;padding:6px 0;background:rgba(255,255,255,.08);border:1px solid var(--border);border-radius:7px;color:var(--text);font-size:11px;font-weight:600;cursor:pointer;font-family:var(--font)">${t('b.use_word')}</button>
        </div>
        <div>
          <img src="${esc(mdbImgUrl)}" style="width:100%;aspect-ratio:1;object-fit:cover;border-radius:10px;border:2px solid transparent;cursor:pointer;transition:border-color .15s" id="bbc-cover-opt-mdb"
            onmouseover="this.style.borderColor='#af52de'" onmouseout="this.style.borderColor='transparent'"/>
          <div style="font-size:10px;font-weight:700;color:#af52de;text-align:center;margin-top:5px">🗄 MixesDB</div>
          <button onclick="document.getElementById('bbc-cover-choice').remove();bbcDownload('${_esc(pid)}','${_esc(vpid)}','${_esc(title)}','${_esc(artist)}','${_esc(bbcImg)}','${_esc(mdbImg)}')"
            style="width:100%;margin-top:6px;padding:6px 0;background:rgba(175,82,222,.18);border:1px solid rgba(175,82,222,.35);border-radius:7px;color:#af52de;font-size:11px;font-weight:600;cursor:pointer;font-family:var(--font)">${t('b.use_word')}</button>
        </div>
      </div>
      <button onclick="document.getElementById('bbc-cover-choice').remove()"
        style="width:100%;padding:7px 0;background:transparent;border:1px solid var(--border);border-radius:8px;color:var(--muted);font-size:11px;cursor:pointer;font-family:var(--font)">${t('s.cancel')}</button>
    </div>`;
  document.body.appendChild(overlay);
}

// Значение для JS-строки ВНУТРИ HTML-атрибута (onclick="f('…')"). Два слоя:
// браузер сначала раскодирует сущности атрибута, потом исполнит JS. Старый
// вариант не трогал `\` и `&`: название с обратным слэшем или литеральным
// `&#39;` выходило из строки и становилось кодом (23.09.2026).
function _esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;')
    .replace(/\\/g,'\\\\').replace(/'/g,"\\'").replace(/\r?\n/g,' ');
}
// Значение для data-атрибута: читается через dataset как ТЕКСТ, JS-слэши там
// лишние (название с апострофом получало `\` в плеере).
function _escA(s) {
  return String(s == null ? '' : s)
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}

// ── Обложки ──────────────────────────────────────────────────────────────────
// BBC-адреса приходит уже в 320×320 (размер сетки). Для зума и большого плеера
// адрес переставляется на крупный через relCover: у ichef дискретная лесенка
// и 1000×1000 среди них нет (403), живой потолок — 1024×1024.
function _bbcCover(url, px) {
  if (!url) return '';
  return (typeof relCover === 'function') ? relCover(url, px) : url;
}

// Нет картинки / не грузится → нейтральная плашка 📻. Специально НЕ подставляем
// чужой кадр: серый квадрат честнее обложки от другой передачи. Если сорвался
// подставленный MixesDB-кадр, сначала возвращаем родную обложку BBC.
function _bbcCoverFail(el) {
  const orig = el.dataset.bbcFallback;
  if (orig && el.getAttribute('src') !== orig) {
    el.dataset.bbcFallback = '';
    el.src = orig;
    return;
  }
  const ph = document.createElement('div');
  ph.textContent = '📻';
  ph.setAttribute('aria-hidden', 'true');
  ph.style.cssText = 'width:100%;aspect-ratio:1/1;display:flex;align-items:center;'
    + 'justify-content:center;font-size:26px;background:var(--surface2);color:var(--muted2)';
  el.replaceWith(ph);
}
function _bbcFmtDur(s) {
  s = Math.floor(+s);
  const h = Math.floor(s/3600), m = Math.floor((s%3600)/60), ss = s%60;
  return h ? `${h}:${String(m).padStart(2,'0')}:${String(ss).padStart(2,'0')}`
           : `${m}:${String(ss).padStart(2,'0')}`;
}

async function bbcPlay(pid, vpid, title, artist, art) {
  // Единый владелец воспроизведения (см. `_silenceAllBut` в player.js): глушим
  // ВЕСЬ главный плеер — pp-audio, Web Audio, свой тракт, FairPlay/HLS — и
  // отменяем его незавершённый асинхронный старт, прежде чем зазвучит BBC.
  // Раньше здесь лежала своя выборочная остановка (pp-audio + _WA.curSource),
  // и она не знала про `_NA`/`_fpsEl` — переключение на радио оставляло их
  // играть фоном. Теперь решение об «остальном» живёт в одном месте.
  try { if (typeof _silenceAllBut === 'function') _silenceAllBut('bbc'); } catch {}
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
  // Обложка в плеер: полосе хватает 320, развёрнутый плеер большой — там крупный
  // кадр (1024; 1000×1000 ichef не отдаёт).
  ['pp-art','pp-art-big'].forEach(id => {
    const el = document.getElementById(id); if (!el) return;
    const url = art ? _bbcCover(art, id === 'pp-art-big' ? 1000 : 320) : '';
    el.innerHTML = url
      ? `<img src="${esc(url)}" onerror="_bbcCoverFail(this)" style="width:100%;height:100%;object-fit:cover"/>`
      : '📻';
  });
  ['pp-fill','pp-fill-big'].forEach(id => { const el = document.getElementById(id); if(el) el.style.width = '0%'; });
  ['pp-cur','pp-cur-big'].forEach(id => { const el = document.getElementById(id); if(el) el.textContent = (id==='pp-cur') ? '0:00.000' : '0:00'; });
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

  toast(t('b.getting'));
  try {
    const qs = `pid=${encodeURIComponent(pid)}${vpid ? '&vpid=' + encodeURIComponent(vpid) : ''}&name=${encodeURIComponent(title || pid)}`;
    const info = await api('GET', `/api/bbc/stream?${qs}`);
    if (!info.url) throw new Error((typeof errText==='function'?errText(info.detail):info.detail) || t('b.no_stream_url'));
    BBC.duration = info.duration || 0;
    _bbcStartStream(info.url);
    // 1001Tracklists — authoritative tracklist + cue-time chapters for the mix.
    if (typeof _playerSetChapters === 'function') _playerSetChapters([]);
    BBC._ticksDone = false;
    _bbcFetch1001(pid, title, artist, BBC.duration);
  } catch(e) {
    toast(t('b.stream_err') + e.message);
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
      if (data.fatal) toast(t('b.hls_err') + data.type);
    });
  } else if (audio.canPlayType('application/vnd.apple.mpegurl')) {
    audio.src = url;
    audio.play();
    if (playBtn)  playBtn.textContent  = '⏸';
    if (playBtnB) playBtnB.textContent = '⏸';
  } else {
    toast(t('b.hls_no'));
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
        toast(ti('p.resume_from',{t:_bbcFmtDur(_bbcResumeAt)}), 'var(--muted)', 2600);
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
  // BBC plays through its own <audio id="bbc-audio">, deliberately NOT read by
  // the shared rAF ms-loop (_ppMsLoop — see its comment), so #pp-cur is owned
  // here instead. Match the M:SS.mmm format the loop uses elsewhere so pausing
  // doesn't drop the fractional part and reflow the player controls.
  const curMs = fmtDurMs(cur); const _i = curMs.lastIndexOf('.');
  const curEl = document.getElementById('pp-cur');
  if (curEl) curEl.innerHTML = _i>0 ? curMs.slice(0,_i)+'<span class="pp-ms">'+curMs.slice(_i)+'</span>' : curMs;
  const curBigEl = document.getElementById('pp-cur-big');
  if (curBigEl) curBigEl.textContent = curStr;
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
  // Server picks the source: BBC's own timed list → 1001Tracklists (DJ + air
  // date verified) → BBC names → MixesDB. Nothing confident → found:false.
  const qs = `pid=${encodeURIComponent(pid)}&title=${encodeURIComponent(title || '')}`
    + `&artist=${encodeURIComponent(artist || '')}&dur=${Math.round(dur || 0)}`;
  fetch('/api/bbc/tracklist-best?' + qs)
    .then(r => r.json())
    .then(res => {
      const tracks = (res && res.found ? res.tracks : []) || [];
      const chapters = tracks
        .filter(t => t.seconds != null && !t.is_with)
        .map(t => ({ seconds: t.seconds,
                     label: (t.artist ? t.artist + ' — ' : '') + (t.title || '') }))
        .filter(c => c.label);
      const r = { found: !!(res && res.found), tracks, chapters, url: res?.url,
                  source: res?.source || '' };
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
  const credit = `<div style="display:flex;align-items:center;justify-content:space-between;gap:8px;font-size:10px;color:var(--muted2);margin-bottom:8px;padding-bottom:6px;border-bottom:1px solid var(--border)"><span>${esc(creditLabel)} · ${tracks.length} ${t('p.trk_abbr')}</span></div>`;
  tl.innerHTML = credit + tracks.map((tr, i) => {
    const tsEl = tr.timestamp
      ? `<span class="ts" onclick="previewSeekTo(${tr.seconds || 0})" title="${t('b.seek')}">${esc(tr.timestamp)}</span>`
      : '';
    return `<div class="scd-trk"><span class="n">${i+1}</span>${tsEl}<span class="nm">${esc(nm(tr))}</span></div>`;
  }).join('');
}

// Load a BBC mix tracklist into the drawer: MixesDB instantly (if cached), then
// 1001Tracklists takes priority when found — same source logic as SoundCloud.
function _bbcLoadTracklistInto(pid, title, artist, dur, tl) {
  if (!tl) return;
  // No unverified MixesDB preview any more: the server's chain already falls
  // back to MixesDB (date-checked) — a wrong list flashing first is worse than a spinner.
  tl.innerHTML = `<div style="font-size:11px;color:var(--muted2)">⏱ ${t('b.tl_searching')}</div>`;
  const LBL = { bbc: '📻 BBC', '1001tracklists': '🎚 1001Tracklists', mixesdb: '🗄 MixesDB' };
  _bbcFetch1001(pid, title, artist, dur, (r) => {
    if (r.found && r.tracks.length) _bbcRenderTL(tl, r.tracks, LBL[r.source] || r.source || '');
    else tl.innerHTML = `<div style="font-size:11px;color:var(--muted2)">${t('b.tl_none')}</div>`;
  });
}

// Единая строка действий: у вышедшего выпуска — играть/качать/CUE, у будущего
// — только план записи. Кнопка «▶ Play» над эфиром, которого ещё нет, была бы
// обещанием; в доме это запрещено (см. agents.md «настройка, которая ничего не
// меняет, хуже отсутствующей»).
function _bbcDetailActions(schedBtn, pid, vpid, title, artist, art) {
  const btn = (bg, bd, clr) => `flex:1;padding:8px 0;border:1px solid ${bd};background:${bg};border-radius:8px;font-size:12px;font-weight:600;color:${clr};cursor:pointer;font-family:var(--font)`;
  const j = (s) => (s || '').replace(/'/g, "\\'").replace(/"/g, '&quot;');
  if (schedBtn) return `<div style="display:flex;gap:7px;margin-top:14px">${schedBtn}</div>`;
  return `
      <div style="display:flex;gap:7px;margin-top:14px">
        <button onclick="bbcPlay('${j(pid)}','${j(vpid)}','${j(title)}','${j(artist)}','${j(art)}')" style="${btn('rgba(255,85,0,.14)','rgba(255,85,0,.25)','#ff7a33')}">▶ ${t('btn.play')}</button>
        <button onclick="bbcDownloadSmart('${j(pid)}','${j(vpid)}','${j(title)}','${j(artist)}','${j(art)}')" style="${btn('rgba(255,255,255,.06)','var(--border)','var(--text)')}">⬇ MP3</button>
        <button onclick="bbcGetCue('${j(pid)}','${j(title)}','${j(artist)}')" title="${t('b.dl_cue')}" style="padding:8px 11px;border:1px solid var(--border);border-radius:8px;font-size:13px;color:var(--muted);background:transparent;cursor:pointer;font-family:var(--font)">CUE</button>
      </div>`;
}

// Unified mix-detail drawer for BBC — reuses the SoundCloud drawer (#sc-detail).
function _bbcDetailHTML(pid, vpid, title, artist, art, dur, date, schedBtn) {
  const meta = [];
  if (date) meta.push(esc(date));
  if (dur)  meta.push(_bbcFmtDur(dur));
  return `
    <div class="scd-head" style="display:flex;align-items:center;justify-content:space-between;padding:14px 16px 10px;border-bottom:1px solid var(--border);flex:0 0 auto">
      <div style="font-size:13px;font-weight:700;color:var(--text)">📻 BBC · ${t('b.mix_card')}</div>
      <button class="scd-close" onclick="_scCloseMix()" title="${t('b.close_esc')}" style="width:30px;height:30px;border-radius:8px;border:1px solid var(--border);background:transparent;color:var(--muted);cursor:pointer;font-size:15px;flex:0 0 auto;display:flex;align-items:center;justify-content:center;line-height:1;padding:0">✕</button>
    </div>
    <div class="scd-body" style="overflow-y:auto;padding:16px 16px 168px;flex:1 1 auto">
      ${art ? `<img class="scd-cover" src="${esc(_bbcCover(art, 480))}" data-lightbox data-lightbox-src="${esc(_bbcCover(art, 1000))}" onerror="_bbcCoverFail(this)" style="width:100%;aspect-ratio:1;object-fit:cover;border-radius:12px;display:block;background:var(--surface2);cursor:zoom-in"/>` : `<div class="scd-cover" style="width:100%;aspect-ratio:1;display:flex;align-items:center;justify-content:center;font-size:54px;border-radius:12px;background:var(--surface2);color:var(--muted2)">📻</div>`}
      <div style="font-size:16px;font-weight:700;color:var(--text);margin-top:13px;line-height:1.3">${esc(title)}</div>
      <div style="font-size:13px;color:var(--muted);margin-top:3px">${esc(artist || '📻 BBC Sounds')}</div>
      ${meta.length ? `<div style="font-size:11px;color:var(--muted2);margin-top:7px">${meta.join('  ·  ')}</div>` : ''}
      ${_bbcDetailActions(schedBtn, pid, vpid, title, artist, art)}
      <div style="font-size:11px;font-weight:700;color:var(--muted);margin:18px 0 8px;text-transform:uppercase;letter-spacing:.4px">${t('b.tl_word')}</div>
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
  // Кнопка плана переезжает из карточки как есть: тот же обработчик, тот же
  // data-key, та же подсветка — второго пути планировать не появляется.
  const card = document.getElementById('bbccard-' + pid);
  const schedBtn = (card && card.dataset.bbcUpcoming === '1')
    ? (card.querySelector('.bbc-sched-btn')?.outerHTML || '') : '';
  d.innerHTML = _bbcDetailHTML(pid, vpid, title, artist, art, dur, date, schedBtn);
  bd.style.display = 'block'; bd.classList.add('show');
  requestAnimationFrame(() => { d.classList.add('open'); d.style.transform = 'translateX(0)'; });
  if (schedBtn) _bbcApplySchedLabels();
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
  toast(t('b.dling_c')+title+'…');
  try {
    const r = await api('POST', '/api/bbc/download', {
      pid, vpid: vpid || '', title, artist: artist || 'BBC Radio',
      image_url: image_url || '', cover_url: cover_url || ''
    });
    // Ответ приходит раньше события bbc_dl_done: запоминаем полосы, чтобы
    // «готово» сказало числом, чем файл является, а не просто «готово».
    if (r && r.source_kbps) _bbcDlQ[pid] = r;
    toast('⬇ BBC: '+title+' — '+t('b.dl_started'));
  } catch(e) {
    toast(t('b.dl_err') + e.message);
  }
}

async function bbcGetCue(pid, title, artist) {
  const url = `/api/bbc/cue?pid=${pid}&title=${encodeURIComponent(title)}&artist=${encodeURIComponent(artist||'BBC Radio')}`;
  try {
    const r = await fetch(url);
    if (!r.ok) { toast(t('b.no_tl_ep')); return; }
    const blob = await r.blob();
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `${title}.cue`;
    a.click();
    toast(t('b.cue_dl'));
  } catch(e) {
    toast(t('b.cue_err') + e.message);
  }
}

// ── Запись будущих эфиров (планировщик — ripster/bbc_schedule.py) ───────────
// Кнопка есть только на карточках, у которых будущий старт (availability.from)
// и канал из каталога живых потоков. Повторный клик по запланированной карточке
// отменяет план; удаление карточки из очереди отменяет его на сервере тоже.
const _bbcSched = new Map();   // "channel|start_utc" → id плана

function _bbcNormIso(iso) {
  const d = new Date(iso);
  return isNaN(d) ? '' : d.toISOString().replace(/\.\d{3}Z$/, 'Z');
}

function _bbcLiveFuture(ep) {
  if (!ep || !ep.schedulable || !ep.avail_from) return '';
  const s = _bbcNormIso(ep.avail_from);
  return (s && new Date(s).getTime() > Date.now()) ? s : '';
}

// ── Честный прогноз качества ─────────────────────────────────────────────────
// Что запись ЭТОГО эфира даст на самом деле — до нажатия, а не после. Числа
// приходит с сервера (ripster/bbc_quality.py: живой промер лестницы live-потока,
// first_broadcast_date и наши журналы); здесь только подписывает их словом.
// Никакого «320» по умолчанию: если лестницу прочитать не удалось, так и сказано.

function _bbcQualText(lv) {
  if (!lv || !Object.keys(lv).length) return t('bbc.q_pending');
  if (!lv.ok) return t('bbc.q_unknown');
  const v = ti('bbc.q_value', {kbps: lv.best_kbps || 0, codec: lv.best_codec || '?'});
  if (lv.has_320_lc) return '✓ ' + v;
  // Ступени 320 в лесенке нет — это не авария, а потолок канала сегодня.
  return '⚠ ' + v + ' · ' + t('bbc.q_no320');
}

function _bbcQualColor(lv) {
  if (!lv || !lv.ok) return 'var(--orange)';
  return lv.has_320_lc ? 'var(--green)' : 'var(--orange)';
}

function _bbcQualTitle(lv) {
  if (!lv || !lv.ok) return t('bbc.q_unknown_t');
  const when = lv.checked_utc ? new Date(lv.checked_utc).toLocaleString() : '';
  const rungs = (lv.variants || []).map(v => v.kbps + ' ' + v.codec).join(' / ');
  return ti('bbc.q_measured', {rungs, when}) + '\n' + (lv.url || '');
}

function _bbcQHTML(ep) {
  const lv   = ep.live || {};
  const bits = [`<span style="color:${_bbcQualColor(lv)}">${esc(_bbcQualText(lv))}</span>`];
  if (ep.repeat)   bits.push(`<span style="color:var(--muted)">${t('bbc.tag_repeat')}</span>`);
  if (ep.recorded) bits.push(`<span style="color:var(--yellow)">${t('bbc.tag_owned')}</span>`);
  return `<div title="${esc(_bbcQualTitle(lv))}"
    style="display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin-top:6px;font-size:9.5px;font-family:var(--mono);letter-spacing:.2px">
    ${bits.join('<span style="color:var(--border2)">·</span>')}
  </div>`;
}

// Текст подтверждения записи: конкретный ответ про конкретный слот. Если
// сервер не смог измерить поток, вопрос всё равно задаём — но с явной строкой
// «неизвестно», а не молчаливым «320».
function _bbcForecastAsk(fc, title, start) {
  const when = (typeof _fmtSchedFor === 'function' ? _fmtSchedFor(start) : start);
  const lines = ['📻 ' + (title || '') + ' · ' + when];
  if (!fc) { lines.push('⚠ ' + t('bbc.fc_noanswer')); return lines.join('\n'); }
  const lv = fc.live || {};
  const ch = _bbcChLabel(fc.channel || '');
  lines.push((lv.has_320_lc ? '✓ ' : '⚠ ') +
             ti('bbc.fc_stream', {ch, kbps: lv.best_kbps || 0, codec: lv.best_codec || '?'}));
  if (lv.ok && !lv.has_320_lc) lines.push('   ' + t('bbc.fc_nohi'));
  if (!lv.ok) lines.push('   ' + t('bbc.q_unknown_t'));
  lines.push('   ' + ti('bbc.fc_ondemand', {kbps: (fc.ondemand || {}).ceiling_kbps || 0}));
  const rp = fc.repeat || {};
  if (rp.state === 'repeat') {
    lines.push('↻ ' + ti('bbc.fc_repeat',
      {date: (rp.first_broadcast_utc || '').slice(0, 10), n: Math.abs(rp.days || 0)}));
  } else if (rp.state === 'debut') {
    lines.push('★ ' + t('bbc.fc_debut'));
  } else {
    lines.push('? ' + t('bbc.fc_repeat_unknown'));
  }
  const own = fc.own || {};
  if (own.have) lines.push('💾 ' + ti('bbc.fc_own', {what: own.title || own.ts || ''}));
  lines.push('');
  lines.push(t('bbc.fc_commit'));
  return lines.join('\n');
}

// Вердикт записанного файла — из строки плана (verdict ставит планировщик,
// спрашивая ffprobe, а не шильдик задачи).
function _bbcVerdictText(r) {
  const v = r.verdict;
  if (!v) return t('bbc.v_running');
  const m = v.measured || {};
  const num = ti('bbc.q_value', {kbps: v.kbps || 0, codec: v.codec || m.profile || '?'});
  if (v.state === 'as_promised') return '✓ ' + num;
  if (v.state === 'below') return '⚠ ' + num + ' · ' + ti('bbc.v_below', {promised: v.promised_kbps || 0});
  if (v.state === 'failed') return '✕ ' + t('bbc.v_failed');
  if (v.state === 'no_file') return '✕ ' + t('bbc.v_nofile');
  return '? ' + t('bbc.v_unmeasured');
}

function _bbcVerdictColor(r) {
  const s = ((r.verdict || {}).state) || 'running';
  return s === 'as_promised' ? 'var(--green)'
       : s === 'below'       ? 'var(--orange)'
       : s === 'running'     ? 'var(--muted)' : 'var(--red)';
}


async function _bbcLoadSched() {
  let items = [];
  try {
    const d = await api('GET', '/api/bbc/schedule');
    items = (d && d.items) || [];
  } catch(_) {}
  _bbcSched.clear();
  items.forEach(r => {
    if (r.status === 'pending') _bbcSched.set(r.channel + '|' + _bbcNormIso(r.start_utc), r.id);
  });
  // Одна выборка с сервера кормит и подсветку кнопок, и панель планов: иначе
  // они неизбежно разъезжаются (план сняли крестиком в очереди — кнопка помнит).
  _bbcSchedRenderList(items);
  return items;
}

async function bbcScheduleToggle(btn, channel, start, dur, title, sub, img) {
  const key = channel + '|' + start;
  const sid = _bbcSched.get(key);
  if (sid) {
    try { await api('DELETE', '/api/bbc/schedule/' + sid); _bbcSched.delete(key); toast(t('b.sched_cancelled')); }
    catch(e) { toast(t('b.sched_err') + e.message, 'var(--red)'); return; }
  } else {
    if (!dur) { toast(t('b.sched_no_dur'), 'var(--red)'); return; }
    const pid = (btn && btn.dataset && btn.dataset.pid) || '';
    // Спрашиваем сервер ПЕРЕД постановкой: человек подтверждает конкретное
    // измерение («320 AAC-LC, проверено сейчас»), а не красивое слово в кнопке.
    let fc = null;
    try {
      fc = await api('GET', '/api/bbc/schedule/forecast?' + new URLSearchParams(
        { channel, start_utc: start, duration: dur, pid }));
    } catch(_) { fc = null; }
    if (!confirm(_bbcForecastAsk(fc, sub || title, start))) { _bbcApplySchedBtns(); return; }
    try {
      const r = await api('POST', '/api/bbc/schedule',
        { channel, start_utc: start, duration: dur, title: sub || title,
          subtitle: sub || '', cover: img || '', pid });
      if (r && r.ok && r.row) { _bbcSched.set(key, r.row.id); toast(ti('b.sched_set', { time: (typeof _fmtSchedFor==='function'?_fmtSchedFor(start):start) })); }
      else toast(((r && (r.detail || r.error)) || t('b.sched_err')), 'var(--red)');
    } catch(e) { toast(t('b.sched_err') + e.message, 'var(--red)'); }
  }
  _bbcApplySchedBtns();
}

// Перекрашивает ВСЕ кнопки планирования (карточки BBC и радарные — общие):
// одна и та же передача может висеть в двух вкладках, состояние синхронно.
// Сначала сверяется с сервером — план могли снять и без клика по кнопке
// (крестик в очереди), а карта _bbcSched локальна.
function _bbcApplySchedBtns() {
  _bbcApplySchedLabels();
  _bbcLoadSched().then(_bbcApplySchedLabels);
}

function _bbcApplySchedLabels() {
  document.querySelectorAll('.bbc-sched-btn').forEach(b => {
    const on = _bbcSched.has(b.dataset.key || '');
    b.textContent   = on ? t('b.sched_on') : t('b.sched_rec');
    b.style.color        = on ? 'var(--muted)' : '#e4003b';
    b.style.borderColor  = on ? 'var(--border)' : 'rgba(228,0,59,.45)';
    b.style.background   = on ? 'var(--surface2)' : 'rgba(228,0,59,.12)';
    b.title              = on ? t('b.sched_on_hint') : t('b.sched_rec_hint');
  });
}

// ── Панель планов и форма записи с живого канала ─────────────────────────────
// Живой канал — второй вход планировщика (первый — будущие эфиры в сетке).
// Ему неоткуда взять название: карточка строится из того, что ввёл человек.
let _bbcChannels = [];

function _bbcChLabel(id) {
  if (!id) return '';
  const k = 'bbc.ch.' + id, v = t(k);
  if (v !== k) return v;
  // Ключа нет (новый канал в каталоге) — читаемое имя из id, а не «bbc_radio_one».
  return id.replace(/^bbc_/, '').replace(/_/g, ' ').replace(/\b\w/g, s => s.toUpperCase());
}

// datetime-local хочет локальное время в форме «ГГГГ-ММ-ДДЧЧ:ММ».
function _bbcLocalInput(iso) {
  const d = iso ? new Date(iso) : new Date();
  if (isNaN(d)) return '';
  const p = n => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
}

async function bbcLoadChannels() {
  try {
    const d = await api('GET', '/api/bbc/channels');
    _bbcChannels = (d && d.channels) || [];
  } catch(_) { _bbcChannels = []; }
  const sel = document.getElementById('bbc-sch-channel');
  if (sel) sel.innerHTML = _bbcChannels.map(c =>
    `<option value="${esc(c.id)}">${esc(_bbcChLabel(c.id))}</option>`).join('');
  const dur = document.getElementById('bbc-sch-dur');
  if (dur) dur.innerHTML = [30, 60, 90, 120, 180, 240].map(m =>
    `<option value="${m*60}"${m === 120 ? ' selected' : ''}>${ti('bbc.sch_min', {n: m})}</option>`).join('');
}

function bbcSchedForm(show) {
  const f = document.getElementById('bbc-sched-form');
  if (!f) return;
  const open = show !== 'false' && show !== false;
  f.style.display = open ? '' : 'none';
  if (!open) return;
  const start = document.getElementById('bbc-sch-start');
  if (start && !start.value) {
    // Ближайшие 15 минут — гарантированно не в прошлом к моменту отправки.
    const d = new Date(Date.now() + 15 * 60000);
    d.setMinutes(Math.ceil(d.getMinutes() / 15) * 15, 0, 0);
    start.value = _bbcLocalInput(d);
  }
  if (start) start.min = _bbcLocalInput(new Date());
  try { start.focus(); } catch(_) {}
}

async function bbcSchedCreate() {
  const channel = document.getElementById('bbc-sch-channel')?.value || '';
  const startEl = document.getElementById('bbc-sch-start');
  const dur     = parseInt(document.getElementById('bbc-sch-dur')?.value || '0', 10);
  if (!channel || !startEl || !startEl.value) { toast(t('bbc.sch_need'), 'var(--red)'); return; }
  const local = new Date(startEl.value);          // datetime-local — это локальное время
  if (isNaN(local.getTime())) { toast(t('bbc.sch_need'), 'var(--red)'); return; }
  const start_utc = local.toISOString().replace(/\.\d{3}Z$/, 'Z');
  // Ручная форма — тот же вход в ту же запись, что и кнопка на карточке,
  // поэтому и спрашивает она то же самое: чем будет файл.
  let fc = null;
  try {
    fc = await api('GET', '/api/bbc/schedule/forecast?' + new URLSearchParams(
      { channel, start_utc, duration: dur }));
  } catch(_) { fc = null; }
  if (!confirm(_bbcForecastAsk(fc, _bbcChLabel(channel), start_utc))) return;
  try {
    const r = await api('POST', '/api/bbc/schedule',
      { channel, start_utc, duration: dur, title: '', subtitle: '', cover: '' });
    if (r && r.ok && r.row) {
      toast(ti('b.sched_set', { time: (typeof _fmtSchedFor === 'function' ? _fmtSchedFor(start_utc) : start_utc) }));
      bbcSchedForm(false);
    } else {
      toast((r && (r.detail || r.error)) || t('b.sched_err'), 'var(--red)');
      return;
    }
  } catch(e) { toast(t('b.sched_err') + e.message, 'var(--red)'); return; }
  _bbcApplySchedBtns();   // он же перечитает план и перерисует панель
}

async function bbcSchedCancel(sid) {
  if (!sid) return;
  try {
    const r = await api('DELETE', '/api/bbc/schedule/' + sid);
    if (r && r.ok === false) throw new Error(r.detail || r.error || '');
    toast(t('b.sched_cancelled'));
  } catch(e) { toast(t('b.sched_err') + (e && e.message || ''), 'var(--red)'); return; }
  _bbcApplySchedBtns();
}

function _bbcSchedTile(r) {
  const art = r.cover ? _bbcCover(r.cover, 96) : '';
  return art
    ? `<img src="${esc(art)}" loading="lazy" onerror="_bbcCoverFail(this)" style="width:38px;height:38px;border-radius:7px;object-fit:cover;flex-shrink:0;background:var(--surface)"/>`
    : `<div aria-hidden="true" style="width:38px;height:38px;border-radius:7px;flex-shrink:0;display:flex;align-items:center;justify-content:center;font-size:17px;background:var(--surface);color:var(--muted2)">📻</div>`;
}

function _bbcSchedWhen(r) {
  const dur = r.duration ? ` · ${_bbcFmtDur(r.duration)}` : '';
  return esc(_bbcChLabel(r.channel)) + ' · ' +
    esc(typeof _fmtSchedFor === 'function' ? _fmtSchedFor(r.start_utc) : (r.start_utc || '')) + esc(dur);
}

function _bbcSchedPendingRow(r) {
  return `
    <div style="display:flex;align-items:center;gap:9px;background:var(--surface2);border:1px solid var(--border);border-radius:10px;padding:6px 8px">
      ${_bbcSchedTile(r)}
      <div style="flex:1;min-width:0">
        <div style="font-size:11.5px;font-weight:700;color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(r.title || _bbcChLabel(r.channel))}</div>
        <div style="font-size:10.5px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${_bbcSchedWhen(r)}</div>
        <div style="font-size:9.5px;font-family:var(--mono);margin-top:2px;color:${esc(_bbcQualColor((r.forecast || {}).live))}">${esc(_bbcQualText((r.forecast || {}).live))}</div>
      </div>
      <button onclick="bbcSchedCancel('${esc(r.id)}')" title="${esc(t('bbc.sch_cancel_t'))}"
        style="flex-shrink:0;width:26px;height:26px;border-radius:7px;background:transparent;border:1px solid var(--border);color:var(--muted);font-size:12px;cursor:pointer;font-family:var(--font)">✕</button>
    </div>`;
}

// Записанное: вердикт файла, а не название качества. Планировщик измеряет
// готовый файл ffprobe (ripster/bbc_schedule.settle_result) — здесь только то,
// что он нашёл.
function _bbcSchedDoneRow(r) {
  const v = r.verdict || {};
  const m = v.measured || {};
  const detail = [ti('bbc.v_line', {got: _bbcVerdictText(r), promised: v.promised_kbps || 0})];
  if (m.sample_rate) detail.push(esc(m.sample_rate / 1000 + ' кГц'));
  if (m.duration)    detail.push(esc(_bbcFmtDur(m.duration)));
  if (v.file)        detail.push(esc(v.file));
  return `
    <div style="display:flex;align-items:center;gap:9px;background:var(--surface2);border:1px solid var(--border);border-radius:10px;padding:6px 8px;opacity:.92">
      ${_bbcSchedTile(r)}
      <div style="flex:1;min-width:0">
        <div style="font-size:11.5px;font-weight:700;color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(r.title || _bbcChLabel(r.channel))}</div>
        <div style="font-size:10.5px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${_bbcSchedWhen(r)}</div>
        <div style="font-size:9.5px;font-family:var(--mono);margin-top:2px;color:${_bbcVerdictColor(r)};white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${detail.join(' · ')}</div>
      </div>
      ${v.state ? `<button onclick="bbcSchedCancel('${esc(r.id)}')" title="${esc(t('bbc.v_forget_t'))}"
        style="flex-shrink:0;width:26px;height:26px;border-radius:7px;background:transparent;border:1px solid var(--border);color:var(--muted);font-size:12px;cursor:pointer;font-family:var(--font)">✕</button>` : ''}
    </div>`;
}

function _bbcSchedRenderList(items) {
  const list  = document.getElementById('bbc-sched-list');
  const empty = document.getElementById('bbc-sched-empty');
  const count = document.getElementById('bbc-sched-count');
  if (!list) return;
  const all     = (items || []);
  const pending = all.filter(r => r.status === 'pending')
                     .sort((a, b) => String(a.start_utc).localeCompare(String(b.start_utc)));
  // Идёт запись или уже кончилась — показываем хвост: человек должен увидеть
  // результат планирования, а не потерять его в очередном обновлении очереди.
  const fired   = all.filter(r => r.status !== 'pending')
                     .sort((a, b) => String(b.start_utc).localeCompare(String(a.start_utc)))
                     .slice(0, 12);
  if (count) count.textContent = pending.length ? ti('bbc.sched_count', { n: pending.length }) : '';
  if (empty) empty.style.display = (pending.length || fired.length) ? 'none' : '';
  list.innerHTML = pending.map(_bbcSchedPendingRow).join('') +
    (fired.length ? `<div style="font-size:10px;color:var(--muted2);text-transform:uppercase;letter-spacing:.4px;margin:8px 0 4px">${t('bbc.sched_done_title')}</div>` +
                   fired.map(_bbcSchedDoneRow).join('') : '');
}

// Переключатель сетки: вышедшие выпуски ↔ ближайшие эфиры.
function bbcModeButtons() {
  document.querySelectorAll('#bbc-mode button').forEach(b => {
    const act = b.dataset.mode === (BBC.upcoming ? 'up' : 'past');
    b.style.background  = act ? 'rgba(228,0,59,.12)' : 'var(--surface)';
    b.style.color       = act ? '#e4003b' : 'var(--muted)';
    b.style.borderColor = act ? 'rgba(228,0,59,.45)' : 'var(--border)';
  });
}

function bbcToggleUpcoming(on) {
  const want = (on === 'true' || on === true);
  if (want === BBC.upcoming) return;
  BBC.upcoming = want;
  bbcModeButtons();
  if (BBC.searching) bbcClearSearch(); else bbcLoadEpisodes(true);
}

// ── Load app info
