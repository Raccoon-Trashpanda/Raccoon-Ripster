// ======================================================================
// Дерево задачи в очереди: карточка альбома раскрывается вниз списком треков,
// у каждого — узкая полоска. Идея из Ripster Mobile, но устроено иначе: в ПК
// альбом качается ОДНОЙ задачей, так что строки — это треки внутри неё, а не
// отдельные задачи.
//
// Честность полосок. Движки сообщают только «сколько треков готово» и общий
// процент, а не состояние каждого файла. Поэтому: треки до счётчика — готовы,
// следующий — качается (доля внутри трека выводится из общего процента, если он
// процентный; иначе полоска бегущая, без выдуманной цифры), остальные — ждут.
// Названия треков приходят отдельной ручкой /api/queue/{id}/tracks и только при
// раскрытии; нет названий — строки просто пронумерованы.
// ======================================================================

const _QT_OPEN   = new Set();   // id раскрытых задач — состояние просмотра, не задачи
const _QT_TRACKS = new Map();   // id → [{title, artist}] | 'loading'

function _qtTrackCount(task) {
  const m = task.meta || {};
  if (m.type === 'song' || m.type === 'track') return 1;
  return m.trackCount || m.totalTracks || 0;
}

// Кнопка раскрытия — только там, где есть что раскрывать.
function qtToggleBtn(task) {
  if (_qtTrackCount(task) < 2) return '';
  const open = _QT_OPEN.has(task.id);
  return `<button class="qi-tree-toggle" onclick="qtToggle('${task.id}')"
      aria-expanded="${open}" title="${esc(t(open ? 'q.tree_hide' : 'q.tree_show'))}">${open ? '⌃' : '⌄'}</button>`;
}

function qtPanel(task) {
  if (_qtTrackCount(task) < 2) return '';
  const open = _QT_OPEN.has(task.id);
  return `<div class="qi-tree" id="qi-tree-${task.id}"${open ? '' : ' hidden'}>${open ? _qtRowsHtml(task) : ''}</div>`;
}

async function qtToggle(id) {
  const task = (S.queue || []).find(x => x.id === id);
  if (!task) return;
  const open = !_QT_OPEN.has(id);
  if (open) _QT_OPEN.add(id); else _QT_OPEN.delete(id);
  const el = document.querySelector(`.qi[data-id="${id}"]`);
  const btn = el?.querySelector('.qi-tree-toggle');
  if (btn) {
    btn.textContent = open ? '⌃' : '⌄';
    btn.setAttribute('aria-expanded', String(open));
    btn.title = t(open ? 'q.tree_hide' : 'q.tree_show');
  }
  const panel = document.getElementById(`qi-tree-${id}`);
  if (!panel) return;
  panel.hidden = !open;
  if (!open) return;
  panel.innerHTML = _qtRowsHtml(task);
  if (!_QT_TRACKS.has(id)) {
    _QT_TRACKS.set(id, 'loading');
    let list = [];
    try {
      const r = await api('GET', `/api/queue/${encodeURIComponent(id)}/tracks`);
      list = (r && Array.isArray(r.tracks)) ? r.tracks : [];
    } catch (e) { list = []; }
    // Пусто — не кэшируем навсегда: сеть могла моргнуть, следующее раскрытие спросит снова.
    if (list.length) _QT_TRACKS.set(id, list); else _QT_TRACKS.delete(id);
    const cur = (S.queue || []).find(x => x.id === id);
    if (cur) qtRefresh(cur);
  }
}

// Вызывается из updateQueueItem на каждом тике — дёшево, если панель закрыта.
function qtRefresh(task, el) {
  if (!_QT_OPEN.has(task.id)) return;
  const panel = el ? el.querySelector(`#qi-tree-${task.id}`)
                   : document.getElementById(`qi-tree-${task.id}`);
  if (panel && !panel.hidden) panel.innerHTML = _qtRowsHtml(task);
}

function _qtRowsHtml(task) {
  const names = _QT_TRACKS.get(task.id);
  const listed = Array.isArray(names) ? names : [];
  const total = Math.max(listed.length, _qtTrackCount(task));
  if (!total) return '';
  const q = _qualityFor(task);
  const pct = Math.max(0, Math.min(100, task.progress || 0));
  const partial = !!(task.partial || task._partial);
  let done = Math.min(total, _tracksDone(task).done || 0);
  if (task.status === 'done' && !partial) done = total;
  if (partial) done = Math.min(total, task.got || task._got || (task._files?.length) || done);

  // Доля текущего трека: только если движок отдаёт ПРОЦЕНТ (а не счётчик N/M) —
  // иначе число было бы выдумкой, и полоска честно «бежит».
  const pctMode = !((task._progTotal || 0) > 1 && task._progTotal !== 100);
  let curFrac = null;
  if (pctMode) curFrac = Math.max(0, Math.min(1, pct / 100 * total - done));

  const rows = [];
  for (let i = 0; i < total; i++) {
    const tr = listed[i] || {};
    const title = tr.title || ti('q.tree_track_n', {n: i + 1});
    const artist = tr.artist && tr.artist !== (task.meta?.artist || '') ? tr.artist : '';
    let st, w, cls = '';
    if (i < done) { st = 'done'; w = 100; }
    else if (i === done && task.status === 'running') {
      st = 'run';
      if (curFrac === null) { w = 100; cls = ' qt-indet'; } else { w = Math.round(curFrac * 100); }
    }
    else if (task.status === 'error' && i === done) { st = 'err'; w = 0; }
    else if (partial || (task.status === 'done')) { st = 'miss'; w = 0; }
    else { st = 'wait'; w = 0; }
    const mark = st === 'done' ? '✓' : st === 'run' ? '↓' : st === 'err' ? '✗' : st === 'miss' ? '–' : '';
    rows.push(`<div class="qt-row qt-${st}">
        <span class="qt-n">${i + 1}</span>
        <span class="qt-name">${esc(title)}${artist ? `<span class="qt-art"> · ${esc(artist)}</span>` : ''}</span>
        <span class="qt-mark">${mark}</span>
        <div class="qt-track"><div class="qt-bar${cls}" style="width:${w}%;background:${q.color}"></div></div>
      </div>`);
  }
  const loading = names === 'loading' ? `<div class="qt-note">${esc(t('q.tree_loading'))}</div>` : '';
  return loading + rows.join('');
}
