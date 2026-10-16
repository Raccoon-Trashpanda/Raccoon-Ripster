// ======================================================================
// Трек-лист: перетаскивание файлов и папок (AIMP-style) + нативный выбор
// папки. Tracker #9014.
//
// ДВЕ ДОРОЖКИ, ОДИН ТРЕК-ЛИСТ.
// 1. DRAG&DROP — быстрый путь, но окно НЕ ЗНАЕТ настоящих путей файлов
//    (pywebview 6.2.1 не отдаёт их фронту; проверено 21.09.2026). Треки
//    играют из blob-URL и живут до перезапуска — UI говорит об этом прямо.
// 2. КНОПКА «ДОБАВИТЬ ПАПКУ» — мост лончера (pywebview.api.pick_folder)
//    открывает нативный диалог, бэкенд сканирует папку своим сканером
//    (mutagen, те же id/cover/stream). Такие треки — настоящие и вечные.
// Оба пути складывают объекты в ОДНУ Preview.queue и зовут ОДИН renderer.
//
// МАСШТАБ И ПОТОК. Строка появляется в тот же миг, как файл найден: обход
// сразу заводит треки в список с именем из файла, а теги дочитываются ПОЗАДИ
// уже стоящих строк (фоновый читатель тегов идёт параллельно обходу). Никакой
// «сначала обойдём всё и прочитаем всё, потом покажем»: на дереве из 500 файлов
// такой порядок давал 22 секунды до первой строки, на 20 000 — 166 (21.09.2026).
// Прогресс виден с первой секунды; отмена останавливает обе фазы на границе
// файла/партии. Не-аудио не исчезает молча: счётчик попадает в итоговый тост.
//
// ПОРЯДОК. Как в Проводе: по уровням каталога, папки раньше файлов, цифры
// сравниваются числами (Track 2 < Track 10). Порядок НЕ пересобирается после
// прихода тегов: переставлять строки под ногами человека — хуже, чем порядок
// имён файлов, который в альбомных папках почти всегда и есть порядок дисков.
// ======================================================================

const DND_LIMIT_FILES = 2000;     // на одно перетаскивание: больше — в библиотеку
const DND_WALK_YIELD  = 150;      // записей на уступ main-потоку
const DND_PEEK_BATCH  = 24;       // файлов на запрос к /api/library/peek (заголовок 16 КБ — дёшево)
const DND_DEEP_BATCH  = 6;        // полное чтение (до 320 КБ/файл) — партии мельче:
                                  // 24×320 КБ через btoa и есть тот 9-секундный
                                  // longtask, что вешал окно на ровном месте
const DND_PEEK_HEAD   = 16000;    // заголовок, где живёт title/artist/album
const DND_PEEK_BYTES  = 320000;   // читаем только начало файла
const DND_POLL_MS     = 900;
const DND_RENDER_MS   = 250;      // как часто перерисовывать панель при потоке

const _dndAudioExt = /\.(mp3|flac|m4a|aac|alac|ogg|opus|wav|aif|aiff)$/i;
const _dndBlobs = new Map();      // posKey → object URL (ревокем при удалении)
const _dndState = {running: false, cancel: null};

function _dndNatKey(s) {
  // «Track 2» < «Track 10»: цифры сравниваем числами, остальное — как строки.
  return String(s).toLowerCase().split(/(\d+)/)
    .map(p => /^\d+$/.test(p) ? String(Number(p)).padStart(12, '0') : p).join('');
}

// Не перехватываем чужие перетаскивания: реагируем ТОЛЬКО на системные файлы
// (тип 'Files'). Внутренние DnD приложения (auth_ui: text/plain «move») и
// перетаскивание текста/ссылок проходят мимо.
function _dndHasFiles(e) {
  const dt = e.dataTransfer;
  if (!dt || !dt.types) return false;
  for (const ty of dt.types) if (ty === 'Files') return true;
  return false;
}

function _dndIsOwnDropZone(el) {
  // любая зона, явно объявленная фронтом как приёмник дропов, — не наша
  return !!(el && el.closest && el.closest('[data-drop-zone]'));
}

// ── панель прогресса с отменой ───────────────────────────────────────────────
function _dndBar() {
  let el = document.getElementById('pq-dnd');
  if (!el) {
    el = document.createElement('div');
    el.id = 'pq-dnd';
    const lab = document.createElement('span');
    lab.className = 'dnd-count';
    const btn = document.createElement('button');
    btn.className = 'dnd-cancel';
    btn.type = 'button';
    btn.textContent = t('dnd.cancel');
    btn.onclick = () => { if (_dndState.cancel) _dndState.cancel(); };
    el.appendChild(lab); el.appendChild(btn);
    document.body.appendChild(el);
  }
  return el;
}
function _dndProgress(labelKey, params, withCancel) {
  const el = _dndBar();
  el.classList.add('on');
  el.querySelector('.dnd-count').textContent =
    params ? ti(labelKey, params) : t(labelKey);
  el.querySelector('.dnd-cancel').style.display = withCancel ? '' : 'none';
}
function _dndProgressDone() {
  const el = document.getElementById('pq-dnd');
  if (el) el.remove();
}

// ── обход webkitGetAsEntry: итеративно, уступами, с отменой ─────────────────
async function _dndWalkEntries(items, st) {
  let stack = [];
  for (const it of items) {
    let ent = null;
    try { ent = it.webkitGetAsEntry && it.webkitGetAsEntry(); } catch (_) {}
    if (ent) stack.push(ent);
  }
  let sinceYield = 0;
  while (stack.length) {
    if (st.cancelled) return;
    const entry = stack.pop();
    if (entry.isDirectory) {
      const rd = entry.createReader();
      // readEntries отдаёт по ~100 за вызов — цикл обязателен, иначе молча
      // теряем всё после первой сотни (папки «на десять тысяч» из них и состоят)
      let batch;
      const kids = [];
      do {
        batch = await new Promise(res => rd.readEntries(res, () => res([])));
        kids.push(...batch);
      } while (batch.length);
      kids.sort((a, b) => (a.isDirectory ? 0 : 1) - (b.isDirectory ? 0 : 1)
                      || _dndNatKey(a.name).localeCompare(_dndNatKey(b.name)));
      stack.push(...kids.slice().reverse());   // pop() пойдёт в прямом порядке
    } else if (entry.isFile) {
      const file = await new Promise(res => entry.file(res, () => res(null)));
      st.scanned++;
      if (!file) st.skippedBad++;
      else if (!_dndAudioExt.test(file.name)) st.skippedNonAudio++;
      else if (st.pending.length >= DND_LIMIT_FILES) st.truncated++;
      else _dndAdmit(st, file, entry.fullPath || file.name);
      if (st.cancelled) return;                // отмена бьёт по каждому файлу,
      sinceYield++;                            // не только по границе папки
      if (sinceYield >= DND_WALK_YIELD) {
        sinceYield = 0;
        _dndProgress('dnd.walking', {n: st.scanned, k: st.added}, true);
        await new Promise(r => setTimeout(r, 0));   // отдаём кадр окну
      }
    }
  }
}

// ── строка сразу при нахождении файла ────────────────────────────────────────
//
// Трек заводится СРАЗУ с именем из файла: ждать свою пачку тегов человеку
// незачем. Метаданные догоняет фоновый читатель (_dndPeekStream), строка на
// глазах богатится артистом/альбомом/длительностью.
function _dndAdmit(st, file, path) {
  const dndKey = file.name + '|' + file.size + '|' + (file.lastModified || 0);
  if (st.dup.has('k:' + dndKey)) { st.duplicates++; return; }
  st.dup.add('k:' + dndKey);
  const url = URL.createObjectURL(file);
  _dndBlobs.set(dndKey, url);
  const item = {
    url,
    title: file.name.replace(/\.[^.]+$/, ''),
    artist: '', album: '', duration: 0,
    service: 'local', local: true, full: true,
    dndKey,
    posKey: 'dnd:' + dndKey,
    label: t('dnd.session_label'),
  };
  Preview.queue.push(item);
  st.added++;
  st.pending.push({ file, path, item, meta: null });
  if (st.added === 1) {
    // первую строку рисуем СРАЗУ, не дожидаясь троттла: человеку видно, что
    // окно его дроп приняло, в ту же секунду, что он отпустил кнопку.
    try { _ppRenderQueue(); } catch (_) {}
    if (st.idleAtStart && typeof _playPreviewAt === 'function') {
      try { _playPreviewAt(Preview.queue.length - 1); } catch (_) {}
    }
  } else {
    _dndRenderSoon();
  }
}

// ── метаданные: ТОТ ЖЕ парсер, что у сканера библиотеки (mutagen на сервере) ─
//
// КРУПНЫЙ ПЛАН. 2000 файлов по 320 КБ — это 640 МБ, перегнанных через FileReader,
// base64 и JSON: проверка на дереве в 2000 файлов показывала минуты вместо
// секунд. Поэтому первый проход читает ТОЛЬКО ЗАГОЛОВОК (16 КБ: там title/artist/
// album у mp3/flac/ogg), и только то, что на нём не прочиталось, дочитываем
// полным бюджетом. Тег с обложкой внутри — редкость против заголовка в 16 КБ,
// а цена ошибки — повторная попытка для этого файла, а не для всего дропа.
async function _dndPeekOnce(chunk, budget) {
  const payload = await Promise.all(chunk.map(async s => {
    const buf = await s.file.slice(0, budget).arrayBuffer();
    const bytes = new Uint8Array(buf);
    let bin = '';
    for (let k = 0; k < bytes.length; k += 0x8000)
      bin += String.fromCharCode.apply(null, bytes.subarray(k, k + 0x8000));
    return {name: s.file.name, b64: btoa(bin)};
  }));
  const r = await api('POST', '/api/library/peek', {files: payload});
  return (r && r.results) || null;
}

// стоит дочитать полным бюджетом: тегов нет вовсе, либо есть только заголовок
// из имени файла (то есть сервер их не нашёл)
function _dndNeedsDeep(s) {
  const m = s.meta || {};
  return !s.meta || (!m.artist && !m.album && !m.duration);
}

function _dndApplyTags(s, m) {
  s.meta = m;
  const it = s.item;
  if (m.title)  it.title  = String(m.title);
  if (m.artist) it.artist = String(m.artist);
  if (m.album)  it.album  = String(m.album);
  const d = Number(m.duration || 0);
  if (d > 0) it.duration = d;
}

// ── фоновый читатель тегов: идёт ПОЗАДИ уже стоящих строк ────────────────────
//
// Живёт параллельно обходу: как только в st.pending набралась партия, она
// уходит на сервер, не дожидаясь конца дерева. Потребляется по индексу i —
// обход тем временем только дописывает хвост, расходиться им нечего.
async function _dndPeekStream(st) {
  let i = 0;
  for (;;) {
    if (st.cancelled) return;
    if (i >= st.pending.length) {
      if (!st.walking) return;                 // обход кончился, всё прочитано
      await new Promise(r => setTimeout(r, 120));
      continue;
    }
    const chunk = st.pending.slice(i, i + DND_PEEK_BATCH);
    i += chunk.length;
    try {
      const results = await _dndPeekOnce(chunk, DND_PEEK_HEAD);
      chunk.forEach((s, k) => {
        const res = results && results[k];
        if (res && res.tags) _dndApplyTags(s, res.tags);
      });
    } catch (_) { /* сервер молчит? ярлык по имени — честнее падения */ }
    const deep = chunk.filter(_dndNeedsDeep);
    for (let j = 0; j < deep.length; j += DND_DEEP_BATCH) {
      if (st.cancelled) return;
      const sub = deep.slice(j, j + DND_DEEP_BATCH);
      try {
        const results = await _dndPeekOnce(sub, DND_PEEK_BYTES);
        sub.forEach((s, k) => {
          const res = results && results[k];
          if (res && res.tags) _dndApplyTags(s, res.tags);
        });
      } catch (_) {}
      // 320 КБ × 6 через btoa и JSON — уже сотни миллисекунд main-потока;
      // без уступа между тяжёлыми партиями окно снова стоит колом
      await new Promise(r => setTimeout(r, 0));
    }
    _dndProgress('dnd.tags', {n: Math.min(i, st.pending.length), k: st.pending.length}, true);
    _dndRenderSoon();                          // голые имена богатятся тегами
    await new Promise(r => setTimeout(r, 0));
  }
}

function _dndDupKeys() {
  // один ключ на трек: бэкендовый id/path для библиотечных, name|size|mtime
  // для перетащенных (у них нет пути — это цена drag&drop в webview, см. шапку)
  const set = new Set();
  const q = (typeof Preview !== 'undefined' && Preview.queue) || [];
  for (const it of q) {
    if (it.id) set.add('id:' + it.id);
    if (it.path) set.add('p:' + String(it.path).toLowerCase());
    if (it.dndKey) set.add('k:' + it.dndKey);
  }
  return set;
}

let _dndRenderTimer = null;
function _dndRenderSoon() {
  if (_dndRenderTimer) return;
  _dndRenderTimer = setTimeout(() => {
    _dndRenderTimer = null;
    try { _ppRenderQueue(); } catch (_) {}
  }, DND_RENDER_MS);
}

function _dndSummaryToast(st) {
  const skipped = st.skippedNonAudio + st.skippedBad;
  let msg = ti('dnd.added_n', {n: st.added});
  if (skipped) msg += ti('dnd.skipped_n', {k: skipped});
  if (st.duplicates) msg += ti('dnd.dups_n', {d: st.duplicates});
  const sub = [t('dnd.session_note')];
  if (st.truncated) sub.push(ti('dnd.truncated', {lim: DND_LIMIT_FILES}));
  toast(msg, st.added ? 'var(--green)' : 'var(--orange)', sub.join(' · '), 7000);
}

async function _dndHandleDrop(e) {
  if (!_dndHasFiles(e)) return;
  e.preventDefault();
  _dndDragVisualOff();
  if (_dndState.running) return;    // одна операция за раз — иначе счётчики врут
  _dndState.running = true;
  const st = {cancelled: false, walking: true, pending: [],
              scanned: 0, skippedNonAudio: 0, skippedBad: 0, truncated: 0,
              duplicates: 0, added: 0, dup: _dndDupKeys(),
              idleAtStart: (typeof Preview !== 'undefined' && Preview.idx < 0)};
  _dndState.cancel = () => { st.cancelled = true; _dndState.cancel = null; };
  const toastCancel = () =>
    toast(t('dnd.cancelled'), 'var(--muted)', ti('dnd.added_n', {n: st.added}), 4500);
  try {
    const items = [...e.dataTransfer.items];
    _dndProgress('dnd.walking', {n: 0, k: 0}, true);
    const peeker = _dndPeekStream(st);      // теги догоняют строки, а не наоборот
    try {
      await _dndWalkEntries(items, st);
    } finally {
      st.walking = false;
    }
    const earlyCancel = st.cancelled;
    if (earlyCancel) toastCancel(); else _dndSummaryToast(st);
    await peeker;                           // кнопка «Отменить» жива до конца тегов
    if (st.cancelled && !earlyCancel) toastCancel();
  } finally {
    _dndState.running = false;
    _dndState.cancel = null;
    _dndProgressDone();
    try { _ppRenderQueue(); } catch (_) {}
  }
}

// ── визуализация: рамка на трек-листе + баннер «что будет, если отпустить» ───
//
// Баннер живёт НА document.body, а не внутри #pp-queue: панель трек-листа
// перерисовывается через innerHTML и стёрла бы приписанный внутрь оверлей на
// первом же обновлении.
function _dndZone() {
  let box = document.getElementById('pp-queue');
  if (!box) {
    try { _ppRenderQueue(true); } catch (_) {}   // человек тащит музыку — покажи список
    box = document.getElementById('pp-queue');
  }
  return box;
}

// Число верхнеуровневых позиций: папка считается одной, пока её не обошли.
// Называем это «объектов», а не «треков» — недооценить объём хуже, чем
// оценить его нейтрально.
function _dndItemCount(e) {
  const it = e.dataTransfer && e.dataTransfer.items;
  return (it && it.length) || 0;
}

function _dndDragVisualOn(e) {
  const box = _dndZone();
  if (box) box.classList.add('pq-drag');
  let z = document.getElementById('pq-dropzone');
  if (!z) {
    z = document.createElement('div');
    z.id = 'pq-dropzone';
    const hint = document.createElement('div');
    hint.className = 'dnd-hint';
    const sub = document.createElement('div');
    sub.className = 'dnd-sub';
    z.appendChild(hint); z.appendChild(sub);
    document.body.appendChild(z);
  }
  const n = _dndItemCount(e);
  z.querySelector('.dnd-hint').textContent =
    n ? ti('dnd.drop_hint_n', {n: n}) : t('dnd.drop_hint');
  z.querySelector('.dnd-sub').textContent = t('dnd.drop_sub');
  z.classList.add('on');
}

function _dndDragVisualOff() {
  const box = document.getElementById('pp-queue');
  if (box) box.classList.remove('pq-drag');
  const z = document.getElementById('pq-dropzone');
  if (z) z.remove();
}

document.addEventListener('dragenter', e => {
  if (!_dndHasFiles(e) || _dndIsOwnDropZone(e.target)) return;
  _dndDragVisualOn(e);
}, true);

document.addEventListener('dragover', e => {
  if (!_dndHasFiles(e) || _dndIsOwnDropZone(e.target)) return;
  e.preventDefault();                       // без этого drop не случится
  e.dataTransfer.dropEffect = 'copy';
  _dndDragVisualOn(e);                      // ещё пара позиций — баннер не врёт
}, true);

// уход из окна и отмена клавишей/Esc — рамка и баннер должны гаснуть, иначе
// «можно бросить» горит над пустым экраном
document.addEventListener('dragleave', e => {
  if (e.relatedTarget) return;               // перешёл на другой элемент — не выход
  _dndDragVisualOff();
}, true);
document.addEventListener('dragend', () => _dndDragVisualOff(), true);

document.addEventListener('drop', e => {
  if (!_dndHasFiles(e)) return;
  if (_dndIsOwnDropZone(e.target)) return;   // чужая зона — не перехватываем
  _dndHandleDrop(e);
}, true);

// удалённый из очереди трек освобождает свой blob (иначе гигабайты живут молча)
function _dndGcBlobs() {
  try {
    const live = new Set(((typeof Preview !== 'undefined' && Preview.queue) || [])
      .map(it => it.dndKey));
    for (const [k, url] of _dndBlobs) {
      if (!live.has(k)) { URL.revokeObjectURL(url); _dndBlobs.delete(k); }
    }
  } catch (_) {}
}
document.addEventListener('ripster:track-start', () => setTimeout(_dndGcBlobs, 300));
setInterval(_dndGcBlobs, 15000);

// ── дорожка 2: нативный выбор папки → бэкендовый скан (вечные треки) ───────
function _dndBridge() {
  return (window.pywebview && window.pywebview.api && window.pywebview.api.pick_folder)
    ? window.pywebview.api : null;
}

async function ppAddFolderFromNativePicker() {
  const bridge = _dndBridge();
  if (!bridge) { toast(t('dnd.no_bridge'), 'var(--orange)', t('dnd.no_bridge_sub'), 6000); return; }
  if (_dndState.running) return;
  _dndState.running = true;
  let paths = null;
  try {
    _dndProgress('dnd.pick_open', null, false);
    paths = await bridge.pick_folder('');
  } catch (e) {
    toast(t('dnd.pick_failed') + ': ' + esc(String((e && e.message) || e)), 'var(--red)');
    _dndState.running = false; _dndProgressDone(); return;
  }
  if (!paths || !paths.length) {           // [] = отменил, null = моста нет
    _dndState.running = false; _dndProgressDone();
    if (paths) toast(t('dnd.pick_cancelled'), 'var(--muted)', '', 2500);
    return;
  }
  const st = {cancelled: false, added: 0, duplicates: 0, skipped: 0, unreadable: 0};
  _dndState.cancel = () => { st.cancelled = true; };
  try {
    const r = await api('POST', '/api/library/folder', {paths});
    const started = (r && r.started) || [];
    const refused = (r && r.refused) || [];
    if (!started.length) {
      const why = refused.length ? refused.map(x => `${x.path}: ${x.error}`).join('\n') : '';
      toast(refused.length ? t('dnd.scan_failed') : t('dnd.already_in_library'),
            'var(--orange)', why, 7000);
      return;
    }
    const dup = _dndDupKeys();
    for (const j of started) {
      if (st.cancelled) break;
      let after = 0, rootName = String(j.path).split(/[\\/]/).pop();
      for (;;) {
        const tr = await _dndGetTracks(j.job, after);
        // api() отдаёт и 4xx телом: {detail: ...} без items — это уже не работа
        if (!tr || !Array.isArray(tr.items)) break;
        for (const it of tr.items) {
          const key = 'p:' + String(it.path).toLowerCase();
          if (dup.has(key) || dup.has('id:' + it.id)) { st.duplicates++; continue; }
          dup.add(key); dup.add('id:' + it.id);
          Preview.queue.push({
            id: it.id, path: it.path,
            url: '/api/library/file?p=' + encodeURIComponent(it.path),
            title: it.title, artist: it.artist, album: it.album, duration: it.duration,
            service: 'local', local: true, full: true, label: t('lib.label'),
            cover: it.has_cover ? '/api/library/cover/' + it.id : '',
            posKey: 'lib:' + it.id,
          });
          st.added++;
        }
        after = tr.next;
        _dndRenderSoon();
        const s = await _dndJobStatus(j.job);
        st.skipped = s.skipped || 0;
        st.unreadable = s.unreadable || 0;
        _dndProgress('dnd.scanning', {n: s.found, k: s.total, r: rootName}, true);
        if (st.cancelled && s.state === 'running') { await _dndJobCancel(j.job); break; }
        if (s.state !== 'running') break;
        await new Promise(res => setTimeout(res, DND_POLL_MS));
      }
    }
    let msg = ti('dnd.added_n', {n: st.added});
    if (st.duplicates) msg += ti('dnd.dups_n', {d: st.duplicates});
    if (st.skipped) msg += ti('dnd.skipped_n', {k: st.skipped});
    if (st.unreadable) msg += ti('dnd.unreadable_n', {u: st.unreadable});
    const sub = [st.cancelled ? t('dnd.cancelled') : t('dnd.folder_permanent')];
    if (refused.length) sub.push(ti('dnd.refused_n', {n: refused.length}) + ' — ' + refused[0].error);
    toast(msg, st.added ? 'var(--green)' : 'var(--muted)', sub.join(' · '), 6500);
  } catch (e) {
    toast(t('dnd.scan_failed') + ': ' + esc(String((e && e.message) || e)), 'var(--red)');
  } finally {
    _dndState.running = false;
    _dndState.cancel = null;
    _dndProgressDone();
    try { _ppRenderQueue(); } catch (_) {}
  }
}

// Все обращения к job-эндпоинтам — через api(): у него есть таймаут, а голый
// fetch на упавшем сервере висит вечно и держит _dndState.running (ни одного
// последующего дропа приложение уже не примет).
async function _dndGetTracks(job, after) {
  try {
    return await api('GET',
      `/api/library/folder/tracks?job=${encodeURIComponent(job)}&after=${after}`, null, 15000);
  } catch (_) { return null; }
}
async function _dndJobStatus(job) {
  try {
    const s = await api('GET', `/api/library/folder/status?job=${encodeURIComponent(job)}`, null, 15000);
    return (s && s.state) ? s : {state: 'done', found: 0, total: 0, skipped: 0, unreadable: 0};
  } catch (_) { return {state: 'done', found: 0, total: 0, skipped: 0, unreadable: 0}; }
}
async function _dndJobCancel(job) {
  try { await api('GET', `/api/library/folder/status?job=${encodeURIComponent(job)}&cancel=1`, null, 15000); } catch (_) {}
}

// сама кнопка живёт в шапке трек-листа (player_queue.js); здесь — видимость
// только там, где мост реально есть (в браузерном режиме её быть не должно:
// настройка, которая ничего не меняет, хуже отсутствующей)
document.addEventListener('ripster:queue-rendered', () => {
  const b = document.querySelector('#pp-queue .pq-folder');
  if (b) b.hidden = !_dndBridge();
});
