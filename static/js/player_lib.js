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

// Библиотека — ДЕРЕВО по релизам (как в AIMP), а не плоский список.
// Раньше: файлы вперемешку, обратный порядок, плей = один трек. Теперь: группируем
// по папке релиза, разворачиваем треки по клику, альбом играет ПО ПОРЯДКУ, плей с
// выбранного трека продолжает альбом. Развёрнутые релизы помним между перерисовками.
const _libExpanded = new Set();

function _base(p) {
  const i = Math.max(p.lastIndexOf('\\'), p.lastIndexOf('/'));
  return i >= 0 ? p.slice(i + 1) : p;
}

function _libGroups(items) {
  const map = new Map();                       // folder → релиз
  for (const it of items) {
    const folder = _libFolderOf(it.path);
    let g = map.get(folder);
    if (!g) { g = { folder, album: '', artist: '', coverId: '', tracks: [], mtime: 0 }; map.set(folder, g); }
    g.tracks.push(it);
    if (!g.album && it.album) g.album = it.album;
    if (!g.artist && it.artist) g.artist = it.artist;
    if (!g.coverId && it.has_cover) g.coverId = it.id;
    if ((it.mtime || 0) > g.mtime) g.mtime = it.mtime || 0;
  }
  for (const g of map.values()) {
    // Порядок треков — по имени файла (NN. …), с числовой сортировкой (2 < 10).
    g.tracks.sort((a, b) => _base(a.path).localeCompare(_base(b.path), undefined, { numeric: true }));
    if (!g.album) g.album = _base(g.folder);
  }
  return [...map.values()];
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
  let groups = _libGroups(items);
  switch (sort) {
    case 'artist': groups.sort((a,b) => (a.artist||'').localeCompare(b.artist||'') || (a.album||'').localeCompare(b.album||'')); break;
    case 'album':  groups.sort((a,b) => (a.album ||'').localeCompare(b.album ||'')); break;
    case 'title':  groups.sort((a,b) => (a.album ||'').localeCompare(b.album ||'')); break;
    default:       groups.sort((a,b) => (b.mtime||0) - (a.mtime||0));   // свежие релизы первыми
  }
  const list = document.getElementById('lib-list');
  if (!list) return;
  const slice = groups.slice(0, 300);
  list.innerHTML = slice.map(_libReleaseRow).join('');
  if (groups.length > 300) {
    list.insertAdjacentHTML('beforeend',
      `<div style="padding:14px;text-align:center;color:var(--muted);font-size:11px">+${groups.length - 300} ${t('lib.more_refine')}</div>`);
  }
}

function _libReleaseRow(g) {
  const key = g.folder;
  const open = _libExpanded.has(key);
  const cov = g.coverId
    ? `<img src="/api/library/cover/${g.coverId}" style="width:44px;height:44px;border-radius:6px;object-fit:cover;flex-shrink:0;background:var(--surface2)" loading="lazy" onerror="this.style.display='none'"/>`
    : `<div style="width:44px;height:44px;border-radius:6px;background:rgba(255,255,255,.04);display:flex;align-items:center;justify-content:center;font-size:18px;color:var(--muted);flex-shrink:0">♪</div>`;
  const kd = escJ(key), alb = escJ(g.album), art = escJ(g.artist);
  const tracks = open
    ? `<div style="padding:4px 6px 8px 14px">${g.tracks.map((tr,i)=>_libTrackRow(g,tr,i)).join('')}</div>`
    : '';
  return `<div style="background:var(--surface);border:1px solid var(--border);border-radius:10px;margin-bottom:7px;overflow:hidden">
    <div onclick="_libToggle('${kd}')" style="display:flex;align-items:center;gap:11px;padding:9px 12px;cursor:pointer;transition:background .12s" onmouseover="this.style.background='rgba(192,132,160,.05)'" onmouseout="this.style.background=''">
      <span style="font-size:12px;color:var(--muted2);width:12px;flex-shrink:0;transition:transform .15s">${open?'▾':'▸'}</span>
      ${cov}
      <div style="flex:1;min-width:0">
        <div style="font-size:13px;font-weight:700;color:var(--text);overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(g.album)}">${esc(g.album)}</div>
        <div style="font-size:11px;color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(g.artist||'—')} · ${g.tracks.length} ${t('p.trk_abbr')}</div>
      </div>
      <button onclick="event.stopPropagation();playLocalMix('${kd}',{album:'${alb}',artist:'${art}'})" style="width:34px;height:34px;border-radius:50%;background:rgba(192,132,160,.14);border:1px solid rgba(192,132,160,.4);color:#c084a0;cursor:pointer;font-size:14px;flex-shrink:0" title="${t('lib.play_mix_hint')}">▶</button>
      <button onclick="event.stopPropagation();libQueueAdd('${kd}',{album:'${alb}',artist:'${art}'})" style="width:34px;height:34px;border-radius:50%;background:transparent;border:1px solid var(--border);color:var(--muted);cursor:pointer;font-size:17px;flex-shrink:0" title="${t('lib.add_queue')}">＋</button>
    </div>${tracks}
  </div>`;
}

function _libTrackRow(g, tr, i) {
  const dur = tr.duration ? fmtDur(tr.duration) : '';
  const kd = escJ(g.folder), alb = escJ(g.album), art = escJ(g.artist), pth = escJ(tr.path);
  return `<div onclick="playLocalMix('${kd}',{album:'${alb}',artist:'${art}'},'${pth}')" style="display:flex;align-items:center;gap:12px;padding:6px 10px;border-radius:7px;cursor:pointer;transition:background .1s" onmouseover="this.style.background='rgba(192,132,160,.06)'" onmouseout="this.style.background=''">
    <span style="font-size:11px;color:var(--muted2);font-family:var(--mono);width:22px;text-align:right;flex-shrink:0">${i+1}</span>
    <span style="flex:1;min-width:0;font-size:12px;color:var(--text);overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(tr.title)}">${esc(tr.title)}</span>
    <span style="font-size:11px;color:var(--muted2);font-family:var(--mono);flex-shrink:0">${dur}</span>
    <button onclick="event.stopPropagation();libQueueAddTrack('${pth}','${escJ(tr.title)}','${art}','${alb}')" style="padding:2px 6px;background:transparent;border:1px solid var(--border);border-radius:5px;font-size:12px;color:var(--muted);cursor:pointer;flex-shrink:0" title="${t('lib.add_queue')}">＋</button>
  </div>`;
}

function _libToggle(key) {
  if (_libExpanded.has(key)) _libExpanded.delete(key);
  else _libExpanded.add(key);
  _libApplyFilter();
}

function _libCopyPath(p) {
  try { navigator.clipboard.writeText(p); toast(t('pl.path_copied'), 'var(--muted)', '', 1500); } catch {}
}

function playLibraryTrack(cid) {
  const it = _LIB.items.find(x => x.id === cid);
  if (!it) { toast(t('pl.no_idx'), 'var(--red)'); return; }
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

// ── Play a downloaded album/mix seamlessly from local files (gapless) ───────
// The "Apple mix" local-playback path. Apple's ALAC can't be decoded by Web
// Audio (Chromium throws EncodingError), so /api/localmix routes each track
// through an on-the-fly stereo-FLAC transcode — losslessly, so the sample-
// accurate join between tracks holds — and the proven _WA gapless engine then
// plays the whole folder without a single gap.
async function playLocalMix(dir, meta = {}, startPath = '') {
  if (!dir) return;
  toast(t('lib.mix_loading'), 'var(--muted)', '', 1800);
  let d;
  try {
    const r = await fetch('/api/localmix?dir=' + encodeURIComponent(dir));
    d = await r.json();
  } catch (e) {
    toast(t('lib.mix_fail') + (e.message || ''), 'var(--red)'); return;
  }
  if (!d || !d.ok || !(d.tracks || []).length) {
    toast(t('lib.mix_empty'), 'var(--orange)'); return;
  }
  const album  = meta.album  || d.album  || '';
  const artist = meta.artist || d.artist || '';
  const cover  = meta.cover  || d.cover  || '';
  _mixRadioOff();
  _setupAudioEvents();
  Preview.queue = d.tracks.map((tr, i) => ({
    url:         tr.url,
    title:       tr.title,
    artist:      tr.artist || artist,
    // Автор альбома → заголовок группы трек-листа (у сборника ≠ артист трека).
    albumArtist: artist,
    cover,
    duration:    Number(tr.duration || 0) || 0,
    service:     'local',
    local:       true,
    full:        true,
    label:       `${t('nav.library')} · ${album}`.trim(),
    posKey:      'localmix:' + dir + '#' + i,
  }));
  // Старт с выбранного трека (клик по треку в дереве) — по ПУТИ, а не по позиции
  // в дереве: /api/localmix упорядочивает по номеру, дерево — по имени файла.
  let start = 0;
  if (startPath) {
    const i = d.tracks.findIndex(tr => tr.path === startPath);
    if (i >= 0) start = i;
  }
  Preview.idx = start;
  toast(`▶ ${album || t('lib.mix')}: ${Preview.queue.length} ${t('p.trk_abbr')}`,
        'var(--green)', '', 2500);
  _playPreviewAt(start);
}

// ── Очередь: добавить / играть следующим / убрать / очистить ────────────────
// Строит элемент очереди из локального трека (тот же формат, что playLocalMix).
function _localQueueItem(tr, album, artist) {
  return {
    url: tr.url, title: tr.title, artist: tr.artist || artist, albumArtist: artist,
    cover: '', duration: Number(tr.duration || 0) || 0,
    service: 'local', local: true, full: true,
    label: `${t('nav.library')} · ${album}`.trim(),
    posKey: 'localq:' + (tr.path || tr.url),
  };
}

// Добавить весь релиз (папку) в конец очереди, не прерывая текущее.
async function libQueueAdd(dir, meta = {}) {
  try {
    const d = await fetch('/api/localmix?dir=' + encodeURIComponent(dir)).then(r => r.json());
    if (!d || !d.ok || !(d.tracks || []).length) { toast(t('lib.mix_empty'), 'var(--orange)'); return; }
    const items = d.tracks.map(tr => _localQueueItem(tr, meta.album || d.album || '', meta.artist || d.artist || ''));
    pqAddItems(items);
    toast(`＋ ${items.length} ${t('p.trk_abbr')} → ${t('pq.title')}`, 'var(--green)', '', 2000);
  } catch (e) { toast(t('lib.mix_fail') + (e.message || ''), 'var(--red)'); }
}

// Добавить один трек в очередь.
function libQueueAddTrack(path, title, artist, album) {
  const tr = { url: '/api/localmix/flac?p=' + encodeURIComponent(path), title, path };
  // .flac уже транскод; для FLAC-исходника это тоже сработает (ffmpeg copy→flac),
  // но чтобы не гонять лишний транскод у настоящего flac — отдаём напрямую file.
  if (/\.flac$/i.test(path)) tr.url = '/api/library/file?p=' + encodeURIComponent(path);
  pqAddItems([_localQueueItem(tr, album, artist)]);
  toast(`＋ ${title} → ${t('pq.title')}`, 'var(--green)', '', 1800);
}

// Общий помощник: дописать элементы в конец очереди (создаёт её, если пусто).
function pqAddItems(items) {
  if (!items || !items.length) return;
  if (typeof Preview === 'undefined') return;
  if (!Preview.queue || !Preview.queue.length) {
    _setupAudioEvents();
    Preview.queue = items.slice();
    Preview.idx = 0;
    _playPreviewAt(0);
  } else {
    _mixRadioOff();
    for (const it of items) Preview.queue.push(it);
  }
  if (typeof _ppRenderQueue === 'function') _ppRenderQueue();
}

// «Играть следующим» — вставить сразу после текущего.
function pqPlayNext(items) {
  if (!items || !items.length || typeof Preview === 'undefined') return;
  if (!Preview.queue || !Preview.queue.length) { pqAddItems(items); return; }
  _mixRadioOff();
  Preview.queue.splice(Preview.idx + 1, 0, ...items);
  if (typeof _ppRenderQueue === 'function') _ppRenderQueue();
  toast(`↳ ${items.length} ${t('p.trk_abbr')}`, 'var(--muted)', '', 1600);
}

// Folder that holds a downloaded track — its whole album is the mix.
function _libFolderOf(path) {
  const i = Math.max(path.lastIndexOf('\\'), path.lastIndexOf('/'));
  return i > 0 ? path.slice(0, i) : path;
}

// "▶ микс" on a library row → play that track's whole album seamlessly.
function playLibraryMix(cid) {
  const it = _LIB.items.find(x => x.id === cid);
  if (!it) { toast(t('pl.no_idx'), 'var(--red)'); return; }
  playLocalMix(_libFolderOf(it.path), {
    album:  it.album,
    artist: it.artist,
    cover:  it.has_cover ? `/api/library/cover/${it.id}` : '',
  });
}

// ── Apple-микс: играть, ПОКА качается («play + download» + таймер до старта) ──
// Как договаривались: не ждать всю загрузку — начать трек 1, как только он готов
// (дописан на диск), а остальные дозагружать по мере скачивания. ALAC→FLAC-транскод
// и WA-gapless те же, что у playLocalMix; здесь добавлен поллинг растущей папки.
let _liveMix = null;

function _liveOverlay(html) {
  let el = document.getElementById('livemix-ov');
  if (html == null) { if (el) el.remove(); return; }
  if (!el) {
    el = document.createElement('div');
    el.id = 'livemix-ov';
    // Цвета — ТОЛЬКО через токены. Раньше здесь было зашито тёмное
    // (`rgba(20,16,18,.92)` и `#fff`), и на светлой теме плашка превращалась в
    // чёрный прямоугольник посреди светлого экрана.
    el.className = 'livemix-ov';
    document.body.appendChild(el);
  }
  el.innerHTML = html;
}

function _liveStop() {
  if (_liveMix && _liveMix.timer) clearTimeout(_liveMix.timer);
  if (_liveMix && _liveMix.tick) clearInterval(_liveMix.tick);
  _liveMix = null;
  _liveOverlay(null);
}

// Собрать элемент очереди из трека ответа /api/localmix (тот же формат, что playLocalMix).
function _liveQueueItem(tr, meta) {
  return {
    url: tr.url, title: tr.title, artist: tr.artist || meta.artist,
    albumArtist: meta.artist, cover: meta.cover, duration: Number(tr.duration || 0) || 0,
    service: 'local', local: true, full: true,
    label: `${t('nav.library')} · ${meta.album}`,
    posKey: 'localmix:' + meta.dir + '#' + tr.path,
  };
}

// Стартовать/дозагрузить микс из папки, что ещё наполняется.
// Юзер выбрал КОНКРЕТНЫЙ альбом/микс — радио-автодобор тут не к месту: он
// дописывает «похожие» в очередь и превращает микс в кашу (поймано на тесте
// live-микса: 1 локальный трек + 6 радио = 7). Глушим на входе в любой микс.
function _mixRadioOff() {
  // DigsRadio — top-level const, его НЕТ на window: обращаемся голым именем
  // через typeof (иначе ReferenceError, а window.DigsRadio === undefined и
  // глушилка молча не срабатывает — так и было поймано на тесте).
  try {
    if (typeof DigsRadio === 'object' && DigsRadio && DigsRadio.on) {
      DigsRadio.on = false;
      if (typeof _digsRadioBadge === 'function') _digsRadioBadge();
    }
  } catch (_) {}
}

async function playLocalMixLive(dir, meta = {}, total = 0) {
  _liveStop();
  _mixRadioOff();
  const state = { dir, total: total | 0, started: false, seen: new Set(),
                  eta: 25, elapsed: 0, timer: null, tick: null,
                  album: meta.album || '', artist: meta.artist || '', cover: meta.cover || '' };
  _liveMix = state;

  // Живой обратный отсчёт «до старта», пока первый трек не готов.
  state.tick = setInterval(() => {
    if (!_liveMix || _liveMix.started) return;
    state.elapsed++;
    const left = Math.max(0, state.eta - state.elapsed);
    const got = state.seen.size;
    _liveOverlay(`🦝 ${t('lib.mix_prep')}<br>`
      + `<b>${left > 0 ? ti('lib.mix_eta', {n: left}) : t('lib.mix_almost')}</b>`
      + (state.total
         ? `<br><span class="lm-got">${ti('lib.mix_got', {n: got, total: state.total})}</span>`
         : ''));
  }, 1000);

  const poll = async () => {
    if (_liveMix !== state) return;
    let d;
    try {
      const r = await fetch('/api/localmix?stable=1&dir=' + encodeURIComponent(dir));
      d = await r.json();
    } catch (_) { d = null; }
    const tracks = (d && d.tracks) || [];
    const m = { dir, album: state.album || (d && d.album) || '',
                artist: state.artist || (d && d.artist) || '',
                cover: state.cover || (d && d.cover) || '' };
    state.album = m.album; state.artist = m.artist; state.cover = m.cover;

    // Новые дописанные треки (по пути), в порядке ответа (track_no).
    const fresh = tracks.filter(tr => tr.path && !state.seen.has(tr.path));
    for (const tr of fresh) state.seen.add(tr.path);

    if (!state.started && tracks.length >= 1) {
      // Первый трек готов — стартуем, как playLocalMix.
      _setupAudioEvents();
      Preview.queue = tracks.map(tr => _liveQueueItem(tr, m));
      Preview.idx = 0;
      state.started = true;
      _liveOverlay(null);
      toast(`▶ ${m.album || t('lib.mix')}: ${t('lib.mix_playing_rest')}`, 'var(--green)', '', 3000);
      _playPreviewAt(0);
    } else if (state.started && fresh.length) {
      // Дозагрузка. Дописывать в КОНЕЦ нельзя: файлы докачиваются параллельно и
      // финишируют не по порядку — трек 3 успевает раньше трека 2 и встаёт перед
      // ним. Для DJ-микса это слышно сразу, он непрерывный.
      //
      // Поэтому не «append», а пересборка ХВОСТА в порядке, который вернул
      // /api/localmix (он сортирует по номеру трека). Трогаем только то, что
      // ещё не играло и не запланировано: текущий трек и уже подготовленный
      // стык переставлять нельзя — на них завязан планировщик Web Audio.
      const have = new Set(Preview.queue.map(x => x.posKey));
      const ordered = tracks.map(tr => _liveQueueItem(tr, m));
      let added = ordered.filter(it => !have.has(it.posKey)).length;

      if (added) {
        // Граница неприкосновенного: текущий индекс и, если стык уже
        // запланирован, следующий за ним.
        let keep = Preview.idx;
        try {
          if (typeof _WA === 'object' && _WA && _WA.schedSource && _WA.schedIdx > keep) keep = _WA.schedIdx;
        } catch (e) { /* плеер на <audio> — планировщика нет, хватит idx */ }

        const head = Preview.queue.slice(0, keep + 1);
        const headKeys = new Set(head.map(x => x.posKey));
        // Хвост = всё из отсортированного списка, чего нет в голове. Порядок
        // берём у сервера, а не у очереди готовности.
        const tail = ordered.filter(it => !headKeys.has(it.posKey));
        // Всё, что было в очереди за границей, но сервер уже не отдаёт
        // (не должно случаться), сохраняем, чтобы ничего не потерять.
        const tailKeys = new Set(tail.map(x => x.posKey));
        const orphans = Preview.queue.slice(keep + 1).filter(x => !tailKeys.has(x.posKey));
        Preview.queue = head.concat(tail, orphans);
      }
      // Сказать панели треклиста, что очередь выросла. Без этого треки
      // ДОПИСЫВАЛИСЬ, но человек их не видел, пока не закроет и не откроет
      // панель заново — а смысл живой докачки как раз в том, чтобы видеть, как
      // микс наполняется. `_ppRenderQueue()` без аргумента безопасна: если
      // панель закрыта, она молча ничего не делает.
      if (added && typeof _ppRenderQueue === 'function') _ppRenderQueue();

      // И — главное для бесшовности. Стык планируется ЗАРАНЕЕ и только по уже
      // раскодированному следующему треку. Пока микс качается, играющий трек
      // часто последний в очереди: готовить нечего, планировщик молчит, и когда
      // трек дописали, переход пошёл бы через реактивный onended — то есть с
      // паузой. Пинаем предзагрузку, чтобы свежий трек успел раскодироваться и
      // встать в стык. См. скилл ripster-gapless-player.
      if (added && typeof _waPreloadNext === 'function') {
        try { _waPreloadNext(); } catch (e) { console.warn('[livemix] preload', e); }
      }
    }

    // Условие остановки: все треки на месте (или лимит времени без роста).
    const done = state.total && state.seen.size >= state.total;
    state.noGrow = fresh.length ? 0 : (state.noGrow || 0) + 1;
    if (done || state.noGrow > 40) {         // ~40×3с ≈ 2 мин без новых треков
      if (_liveMix === state) { state.timer = null; if (state.tick) clearInterval(state.tick); }
      if (!state.started) { _liveOverlay(null); toast(t('lib.mix_empty'), 'var(--orange)'); }
      return;
    }
    state.timer = setTimeout(poll, 3000);
  };
  poll();
}

// «Скачать и слушать без пауз»: ставим Apple-альбом в очередь и играем из папки
// по мере скачивания. total — ожидаемое число треков (из карточки альбома).
async function downloadAndPlayApple(url, album, artist, cover, total) {
  if (!url) return;
  try {
    await api('POST', '/api/queue/add', { url, quality: 'alac', engine: 'zhaarey',
                                          source: 'livemix' });
  } catch (e) {
    toast(t('digs.queued') ? (t('t.error_c') + (e.message || '')) : ('✗ ' + e), 'var(--red)');
    return;
  }
  toast('⬇ ' + (album || '') + ' — ' + t('lib.mix_dl_prep'), 'var(--muted)', '', 3000);
  _liveOverlay(`🦝 ${t('lib.mix_queueing')}<br><b>${esc(album || '')}</b>`);

  // Ждём появления папки альбома (загрузчик создаёт её и кладёт первый трек).
  const meta = { album, artist, cover };
  let tries = 0;
  const waitFolder = async () => {
    tries++;
    let f = null;
    try {
      const r = await fetch('/api/localmix/find?album=' + encodeURIComponent(album || '')
        + '&artist=' + encodeURIComponent(artist || ''));
      f = await r.json();
    } catch (_) {}
    if (f && f.ok && f.dir) {
      meta.dir = f.dir;
      playLocalMixLive(f.dir, meta, total | 0);
      return;
    }
    if (tries > 60) {           // ~3 мин ждём папку — дальше сдаёмся молча
      _liveOverlay(null);
      toast(t('lib.mix_fail') + 'папка не появилась', 'var(--orange)', '', 4000);
      return;
    }
    setTimeout(waitFolder, 3000);
  };
  waitFolder();
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
        // Автор АЛЬБОМА для заголовка группы трек-листа. Без него сборник/DJ-микс
        // (fabric presents Mount Kimbie) подписывался артистом первого трека
        // (David Duriez): этот путь — прямой запуск с тайла поиска — единственный
        // из play-путей, где albumArtist забыли (02.08.2026).
        albumArtist: album.artist || fallbackArtist || '',
        duration: Number(t.duration || t.dur || t.length || 0) || 0,
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
    toast(t('pl.alb_err') + e.message, 'var(--red)');
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
      // Автор альбома — для заголовка группы (у компиляции ≠ артист трека).
      albumArtist: album.artist || '',
      cover:   album.cover || '',
      full:    true,
      // Длительность теряли прямо здесь: в трек-листе она есть только у
      // играющего трека (её знает сам <audio>), у остальных стояло «—», хотя
      // Deezer, Qobuz и Tidal отдают её вместе с треком. Переносим — тогда
      // время видно у всех сразу, ещё до проигрывания.
      duration: Number(t.duration || t.dur || t.length || 0) || 0,
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
      `<option value="${escapeHtml(q.id)}">${escapeHtml(q.label)} — ${escapeHtml(q.sub||'')}</option>`
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
      body: JSON.stringify({path: p, lang: S.lang || 'ru'})
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
    fd.append('lang', S.lang || 'ru');
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
    `<span><span style="color:var(--muted)">${k}:</span> <b>${escapeHtml(v)}</b></span>`
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
      toast(t('t.added_q'));
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
    toast(t('pl.search_err') + e.message, 'var(--red)');
  }
}

async function isrcUpgradeAdd(url, title, artist, service, btn) {
  if (!url) { toast(t('pl.no_url'), 'var(--red)'); return; }
  if (btn) { btn.textContent = '…'; btn.disabled = true; }
  const r = await api('POST', '/api/queue/add', {url, title, artist});
  if (r.ok) toast(`+ ${title} [${service}] ${t('lib.queued')}`);
  else toast(t('t.error_c') + (r.detail || r.msg || '?'), 'var(--red)');
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

// TELEMETRY owner tester diagnostics → moved to its own module file (see index.html).
