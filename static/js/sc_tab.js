
// Обложка под РЕАЛЬНЫЙ размер на экране, а не «как отдали».
//
// Замер 01.08.2026: все 31975 обложек в сторе радара — 640×640, при ширине
// карточки ~180 px. Это 409600 декодируемых точек вместо 32400 на каждую
// карточку, и декодирование идёт на CPU. Отсюда и рывки при прокрутке.
//
// Пережимать ничего не надо: сервисы отдают те же файлы меньшего размера, надо
// лишь попросить правильный адрес.
function relCover(url, px) {
  if (!url) return url;
  try {
    // Spotify. 01.08.2026 здесь стоял вывод «300px отдаёт 404, живы только 640
    // и 64», и карточки радара грузили 640×640. Вывод был ОШИБОЧНЫМ: проверяли
    // код `e02d`, которого у Spotify не существует вовсе — настоящий средний
    // размер кодируется как `1e02`. Перепроверено 02.08.2026 на живом CDN:
    // 8 обложек из 8 отдают и 640, и 300, и 64.
    //
    // Цена ошибки была не косметическая: карточка шириной ~180px декодировала
    // 409600 точек вместо 90000, и именно это владелец видел как рывки при
    // прокрутке радара.
    if (url.includes('i.scdn.co/image/ab67616d0000')) {
      const code = px <= 96 ? 'ab67616d00004851'
                 : px <= 400 ? 'ab67616d00001e02'
                 : 'ab67616d0000b273';
      return url.replace(/ab67616d0000(b273|1e02|4851)/, code);
    }
    // Apple: размер — настоящий сегмент пути, любые значения живы (проверено).
    // Для зума (px>=700) берём 1000×1000 — правило по всему Ripster.
    if (url.includes('mzstatic.com')) {
      const n = px <= 96 ? 128 : (px <= 320 ? 296 : (px <= 700 ? 632 : 1000));
      return url.replace(/\/\d+x\d+([a-z]{0,2})\.(jpg|png|webp)/i, `/${n}x${n}$1.$2`);
    }
    // Deezer: то же самое, проверено. dzcdn отдаёт и 1000×1000.
    if (url.includes('dzcdn.net') || url.includes('deezer.com')) {
      const n = px <= 96 ? 120 : (px <= 320 ? 264 : (px <= 700 ? 500 : 1000));
      return url.replace(/\/\d+x\d+-/, `/${n}x${n}-`);
    }
    // Tidal: resources.tidal.com/images/<uuid>/WxH.jpg — живы 80/160/320/640/1280.
    if (url.includes('resources.tidal.com')) {
      const n = px <= 96 ? 160 : (px <= 320 ? 320 : (px <= 700 ? 640 : 1280));
      return url.replace(/\/\d+x\d+\.(jpg|png|webp)/i, `/${n}x${n}.$1`);
    }
    // Qobuz: static.qobuz.com/images/covers/.../_600.jpg — размер в суффиксе
    // (_50/_150/_230/_600/_max/_org). Для зума берём _org (оригинал), для сетки — _230.
    if (url.includes('qobuz.com') && /_(?:\d+|max|org)\.(jpg|png|webp)/i.test(url)) {
      const suf = px <= 96 ? '_50' : (px <= 320 ? '_230' : (px <= 700 ? '_600' : '_org'));
      return url.replace(/_(?:\d+|max|org)\.(jpg|png|webp)/i, `${suf}.$1`);
    }
  } catch (e) { /* адрес незнакомого вида — отдаём как есть */ }
  return url;
}
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
// Избранное хранится НА СЕРВЕРЕ. localStorage привязан к браузерному профилю, а
// у Ripster их минимум два — окно программы (WebView2) и запасной ярлык в
// браузере; в каждом был свой список, и любая чистка данных сайта стирала звёзды
// молча. Владелец так и сказал: «жму в избранное не первый раз, не помнит»
// (01.08.2026). Локальная копия осталась как мгновенный отклик и запасной путь,
// если сервер не ответил.
function toggleRelFav(uid) {
  const i = _relFavs.findIndex(f => _relUID(f) === uid);
  let rel = null, removing = i >= 0;
  if (removing) {
    rel = _relFavs[i];
    _relFavs.splice(i, 1);
  } else {
    rel = (_relCache.data || []).concat(_relFilteredData).find(r => _relUID(r) === uid);
    if (rel) _relFavs.unshift(rel);
    if (_relFavs.length > 500) _relFavs.length = 500;
  }
  _relSaveJSON(_REL_FAV_KEY, _relFavs);
  _applyRelFilter(false);
  // Ответ сервера не ждём: карточка должна отзываться мгновенно, а сеть здесь
  // локальная. Ошибку глотаем — локальная копия уже верна.
  api('POST', '/api/rel-favs', { uid, item: rel || undefined, remove: removing })
    .catch(() => {});
}

// Подтянуть избранное с сервера при открытии радара — и один раз перелить туда
// то, что осталось в браузере от прежних версий.
async function _relSyncFavs() {
  try {
    const local = _relFavs.slice();
    if (local.length) {
      const m = await api('POST', '/api/rel-favs', { merge: true, items: local });
      if (m && m.items) _relFavs = m.items;
    } else {
      const r = await api('GET', '/api/rel-favs');
      if (r && r.items) _relFavs = r.items;
    }
    _relSaveJSON(_REL_FAV_KEY, _relFavs);
  } catch (e) { /* сервер молчит — работаем на локальной копии */ }
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
  const labelCount = data.filter(_relIsLabelRel).length;
  const vc = document.getElementById('rel-view-chips');
  if (vc) {
    const mk = (id, label, clr) => {
      const on = _relView === id;
      return `<button onclick="setRelView('${id}')" style="padding:4px 11px;border-radius:14px;border:1px solid ${on?clr:'var(--border)'};background:${on?clr+'22':'transparent'};color:${on?clr:'var(--muted)'};font-size:11px;font-weight:700;cursor:pointer;font-family:var(--font);white-space:nowrap">${label}</button>`;
    };
    vc.innerHTML =
      mk('all', t('ck.f_all'), 'var(--text)') +
      mk('new', '🆕 '+t('rl.new_word') + (newCount ? ' ' + newCount : ''), 'var(--green)') +
      mk('fav', '★ '+t('rl.fav_word') + (favCount ? ' ' + favCount : ''), 'var(--orange)') +
      // Лейблы — это ПРИЗНАК релиза, а не раздел ленты. Отдельный блок сверху
      // ломал единственную ось радара: наверху переставало быть «самое новое»
      // и становилось «сначала лейблы, потом новое». Две оси на одном экране
      // не читаются — владелец назвал это кашей, и он прав. Чип сужает ленту,
      // оставляя её одной и по датам. Показываем только когда лейбловое есть:
      // чип, который всегда даёт пусто, — это шум.
      (labelCount ? mk('labels', '🏷 '+t('rl.labels_block') + ' ' + labelCount, 'var(--green)') : '');
  }
  const tc = document.getElementById('rel-type-chips');
  if (tc) {
    const order = ['album','single','ep','compilation','mix','appears_on','live'];
    const lbl   = {album:t('ck.f_albums'),single:t('ck.f_singles'),ep:'EP',compilation:t('ck.f_comps'),mix:t('rl.mix'),appears_on:t('rl.appears'),live:'Live'};
    const types = [...new Set(data.map(r => r.type || 'album'))]
      .sort((a,b) => (((order.indexOf(a)+1)||99) - ((order.indexOf(b)+1)||99)));
    tc.innerHTML = types.map(t => {
      const on = !_relTypeOff.has(t);
      return `<button onclick="toggleRelType('${t}')" style="padding:4px 10px;border-radius:14px;border:1px solid ${on?'var(--red)':'var(--border)'};background:${on?'rgba(192,132,160,.14)':'transparent'};color:${on?'var(--red)':'var(--muted2)'};font-size:11px;font-weight:600;cursor:pointer;font-family:var(--font);white-space:nowrap">${escapeHtml(lbl[t]||t.toUpperCase())}</button>`;
    }).join('');
    tc.style.display = types.length ? '' : 'none';
  }
}

// Карточка попала в ленту потому, что мы следим за ЛЕЙБЛОМ, а не за артистом.
// Это разный повод показать релиз. Сначала мы выносили такие карточки отдельным
// блоком наверх — оказалось хуже: у радара одна смысловая ось, дата, и блок
// сверху отнимал у верха страницы значение «самое новое». Теперь повод виден
// бейджем на карточке, а чип «🏷 Лейблы» сужает ленту, не переставляя её.
// Признак ставит бэкенд (`via_label`), фронт его не выдумывает.
function _relIsLabelRel(r) { return !!(r && r.via_label); }

function _relGroupGrid(cardsHtml) {
  // .card-grid — общая для всех витрин ширина карточки (main.css, --card-min).
  // Там же align-items:stretch: у части релизов есть селектор качества, у части
  // нет, и без него ряды кнопок стояли вразнобой.
  return `<div class="card-grid">${cardsHtml}</div>`;
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
  if (_relView === 'labels') data = data.filter(_relIsLabelRel);
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

  const grouped = (sort === 'date_desc' || sort === 'date_asc');
  // Лейбловые релизы отделяем В ОБОИХ режимах — и в группах по дате, и в
  // «плоском». Блок отвечает на вопрос «почему эта карточка здесь», а не «как
  // отсортировано»: в плоском режиме растворить его значило бы вернуть ровно ту
  // потерю повода, из-за которой блок и заводится. Сортировка при этом общая —
  // она применена выше, внутри блока порядок тот же, что и в остальной ленте.
  //
  // ⚠️ Блок строится из ВСЕГО отфильтрованного набора, а НЕ из видимой страницы.
  // Первая редакция брала `data.slice(0, _relShowing)` — и при 2673 релизах в
  // ленте в первую сотню попадало два лейбловых из двадцати четырёх, которые
  // честно отдал сервер. Снаружи это выглядело как «источник не работает».
  // Постраничность — свойство основной ленты, к отдельному разделу она не
  // применяется: лейбловых релизов десятки, а не тысячи.
  const visible = data.slice(0, _relShowing);
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

// Search fires on every keystroke and _applyRelFilter rebuilds the whole visible
// list through innerHTML — at _REL_PAGE_SIZE=120 cards that is ~3600 nodes
// discarded and re-parsed per character. Coalesce the typing: the filter itself
// is unchanged, it just stops running mid-word.
let _relFilterTimer = null;
function _relFilterDebounced(ms) {
  clearTimeout(_relFilterTimer);
  _relFilterTimer = setTimeout(() => _applyRelFilter(), ms == null ? 160 : ms);
}

function _relShowMore() {
  const grid = document.getElementById('releases-grid');
  // Отфильтрованный список уже посчитан фильтром и лежит здесь —
  // считать его второй раз значит рисковать расхождением.
  const data = _relFilteredData;
  const from = _relShowing;
  _relShowing += _REL_PAGE_SIZE;
  const slice = data.slice(from, _relShowing);
  if (!grid || !slice.length || from === 0) { _applyRelFilter(); return; }
  // Догрузка дописывает карточки В КОНЕЦ сетки, а лейбловый блок стоит
  // отдельно и выше — добавка ушла бы мимо него. Пока в ленте есть лейбловые
  // релизы, перерисовываем видимое целиком (страница уже отфильтрована и
  // отсортирована, считать заново нечего). Нет их — путь ровно прежний.
  if (data.some(_relIsLabelRel)) { _applyRelFilter(false); return; }

  const sortSel = document.getElementById('rel-sort');
  const grouped = (() => { const v = sortSel && sortSel.value; return v === 'date_desc' || v === 'date_asc'; })();

  // Рисуем ТОЛЬКО добавку. Полная перерисовка выбрасывала и разбирала заново
  // все уже показанные карточки вместе с их декодированными обложками.
  const holder = document.createElement('div');
  holder.innerHTML = grouped ? _renderRelGroups(slice) : _renderRelFlat(slice);

  if (grouped) {
    // Если добавка начинается той же датой, на которой список оборвался, её
    // карточки переносятся в уже существующую сетку — иначе получится второй
    // заголовок с той же датой.
    const lastGroup = grid.lastElementChild;
    const firstNew = holder.firstElementChild;
    const dateOf = (el) => el && el.querySelector('span') ? el.querySelector('span').textContent.trim() : null;
    if (lastGroup && firstNew && dateOf(lastGroup) === dateOf(firstNew)) {
      const intoGrid = lastGroup.querySelector('div[style*="grid"]');
      const fromGrid = firstNew.querySelector('div[style*="grid"]');
      if (intoGrid && fromGrid) {
        const added = fromGrid.children.length;
        while (fromGrid.firstChild) intoGrid.appendChild(fromGrid.firstChild);
        const cnt = lastGroup.querySelectorAll('span')[1];
        if (cnt) {
          const was = parseInt(cnt.textContent, 10) || 0;
          cnt.textContent = `${was + added} ${t('w.rel_abbr')}`;
        }
        firstNew.remove();
      }
    }
  }
  while (holder.firstChild) grid.appendChild(holder.firstChild);

  _relUpdateLoadMore(data.length);
  _relHydrateQualitySelects();
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
    // Deezer читает подписки по ARL — той же сессии, что уже качает.
    if(svc === 'deezer')  return !!(c['deezer-arl'] || '').trim();
    // BBC shows / SC channels / Apple artists need no service token of their
    // own — they follow the watchlist (and the known BBC show list), so they
    // are available whenever the user has switched them on.
    if(svc === 'bbc' || svc === 'soundcloud' || svc === 'apple') return true;
    return false;
  });
}

function _relCacheKey() {
  const days  = document.getElementById('rel-days')?.value  || (S.config?.['releases-days'] || '90');
  const types = document.getElementById('rel-types')?.value || (S.config?.['releases-types'] || 'album,single');
  const svcs  = _relActiveSvcs().join(',');
  // Источник лейблов входит в ключ: иначе выключение переключателя оставляло бы
  // на экране закэшированную ленту С лейблами до следующего сканирования.
  // Выключен — ключ ровно прежний, старый кэш продолжает подходить.
  const lbl   = (S.config?.['show-radar-labels'] === true) ? '|labels' : '';
  return `${days}|${types}|${svcs}${lbl}`;
}

function _renderRelActiveSvcs() {
  const cont = document.getElementById('rel-active-svcs');
  if(!cont) return;
  const svcs = _relActiveSvcs();
  const colors = {spotify:'#1db954',qobuz:'#1870f5',tidal:'#00d4b3',
                  bbc:'#ff4d4d',soundcloud:'#ff5500',apple:'#fc3c44',
                  deezer:'#a238ff'};
  cont.innerHTML = svcs.map(svc =>
    `<span style="display:inline-flex;align-items:center;gap:4px;padding:2px 8px;border-radius:12px;font-size:10px;font-weight:700;border:1px solid ${colors[svc]||'var(--border)'}33;color:${colors[svc]||'var(--muted)'};background:${colors[svc]||'transparent'}11">`+
    `<span style="width:5px;height:5px;border-radius:50%;background:${colors[svc]||'var(--muted)'}"></span>${svc.charAt(0).toUpperCase()+svc.slice(1)}</span>`
  ).join('')
  // Лейблы — не сервис, а отдельный источник: показываем отдельным бейджем,
  // и только когда владелец его включил.
  + ((S.config?.['show-radar-labels'] === true)
      ? `<span style="display:inline-flex;align-items:center;gap:4px;padding:2px 8px;border-radius:12px;font-size:10px;font-weight:700;border:1px solid rgba(62,207,170,.3);color:var(--green);background:rgba(62,207,170,.08)" title="${t('rl.src_labels')}">🏷 ${t('rl.labels_badge')}</span>`
      : '');
}

function saveRelSvcConfig() {
  const ALL = ['spotify','qobuz','tidal','bbc','soundcloud','apple','deezer'];
  const prev = new Set(((S.config || {})['releases-services'] || 'spotify')
    .split(',').map(x => x.trim()).filter(Boolean));
  // Only decide for the checkboxes that are actually ON SCREEN. A settings view
  // cached from before a source existed has no checkbox for it, and reading that
  // as "unchecked" silently dropped the source from the config — unticking
  // SoundCloud used to take BBC and Apple down with it.
  const svcs = ALL.filter(s => {
    const cb = document.getElementById('rel-cfg-' + s);
    return cb ? cb.checked : prev.has(s);
  }).join(',');
  saveSetting('releases-services', svcs || 'spotify');
  _renderRelActiveSvcs();
}

function _syncReleasesSettingsTab() {
  const c   = S.config || {};
  const cfg = (c['releases-services'] || 'spotify').split(',').map(s=>s.trim());
  ['spotify','qobuz','tidal','bbc','soundcloud','apple','deezer'].forEach(svc => {
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

  // Лейблы: отдельный источник, свой ключ конфига, по умолчанию выключен.
  const lcb = document.getElementById('rel-cfg-labels');
  if (lcb) lcb.checked = (c['show-radar-labels'] === true);

  // Defaults
  const dSel = document.getElementById('rel-cfg-days');
  const tSel = document.getElementById('rel-cfg-types');
  if(dSel) dSel.value = c['releases-days'] || '90';
  if(tSel) tSel.value = c['releases-types'] || 'album,single';

  _renderRelActiveSvcs();
}

// Переключатель «следить по лейблам». Отдельная функция, а не голый
// saveSetting в разметке: после переключения ленту надо перечитать — иначе
// источник включён, а на экране прежний кэш.
function saveRadarLabels(on) {
  saveSetting('show-radar-labels', !!on);
  _renderRelActiveSvcs();
  if (typeof loadReleases === 'function') loadReleases(false);
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
  // Избранное берём с сервера (и один раз переливаем туда старое из браузера).
  // Не ждём: список звёзд не должен задерживать показ карточек — придёт и
  // перерисует.
  _relSyncFavs().then(() => { if (_relView === 'fav') _applyRelFilter(false); });
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
  const typeMap = {album:'ALBUM', single:'SINGLE', ep:'EP', compilation:t('rl.comp_badge'), mix:t('rl.mix_badge'), appears_on:t('rl.appears_badge'), live:'LIVE'};
  const typeClr = rel.type === 'single' ? 'var(--orange)' : (rel.type === 'album' ? '#1db954' : 'var(--muted2)');
  const typeTag = typeMap[rel.type] || escapeHtml((rel.type || '').toUpperCase());
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
  // Lyrics only wire up where an engine actually fetches them: Apple
  // (zhaarey/amd, embed-lrc/save-lrc-file) and Deezer (deemix's own
  // lyrics/syncedLyrics). Qobuz/Tidal (streamrip) have no real lyrics-fetch
  // path yet — no point showing a checkbox that silently does nothing.
  const _lyricsSvcs = {apple: true, deezer: true};
  // Apple defaults to the existing global embed-lrc/save-lrc-file setting;
  // Deezer has no such global toggle (deemix ships lyrics off) — default off.
  const lyricsDefault = rel.service === 'apple'
    ? (S.config['embed-lrc'] !== false || !!S.config['save-lrc-file'])
    : false;
  const lyricsToggle = _lyricsSvcs[rel.service] ? `
        <label class="rel-lyrics-toggle" onclick="event.stopPropagation()"
          style="display:flex;align-items:center;gap:5px;margin-top:6px;font-size:10px;color:var(--muted);cursor:pointer;user-select:none">
          <input type="checkbox" class="rel-lyrics-chk" ${lyricsDefault ? 'checked' : ''}
            style="width:13px;height:13px;accent-color:var(--red);cursor:pointer"/>${esc(t('rl.lyrics_toggle'))}
        </label>` : '';
  // content-visibility lets the browser skip layout+paint for cards that are
  // off-screen (the grid renders 120 at a time); contain-intrinsic-size keeps
  // the scrollbar honest for the ones it skipped.
  return `<div class="rel-card${isNew ? ' rel-card-new' : ''}" style="background:var(--surface);border:1px solid ${baseBorder};border-radius:10px;overflow:hidden;transition:border-color .15s;content-visibility:auto;contain-intrinsic-size:auto 300px;display:flex;flex-direction:column;height:100%" onmouseover="this.style.borderColor='${svcClr}'" onmouseout="this.style.borderColor='${baseBorder}'">
    <div style="position:relative">
      ${rel.cover
        ? `<img src="${esc(relCover(rel.cover, 300))}" data-lightbox-src="${esc(rel.cover)}" onerror="if(this.src!==this.dataset.lightboxSrc){this.src=this.dataset.lightboxSrc}" data-lightbox style="width:100%;aspect-ratio:1;object-fit:cover;display:block;cursor:zoom-in" loading="lazy" decoding="async"/>`
        : `<div style="width:100%;aspect-ratio:1;background:rgba(255,255,255,.04);display:flex;align-items:center;justify-content:center;font-size:32px;color:var(--muted)">♪</div>`}
      <button onclick="event.stopPropagation();playRelease('${esc(rel.service)}','${escJ(rel.url)}','${escJ(rel.title)}','${escJ(rel.artist)}','${escJ(rel.cover||'')}')" title="${t('rl.listen')}"
        class="rel-play" aria-label="${t('rl.listen')}"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 6.5 L18 12 L9 17.5 Z"/></svg></button>
      <div style="position:absolute;top:6px;left:6px"><span style="font-size:9px;padding:2px 5px;border-radius:4px;background:rgba(0,0,0,.72);color:${svcClr};font-weight:700">${escapeHtml((rel.service||'?').toUpperCase())}</span></div>
      <div style="position:absolute;top:6px;right:6px"><span style="font-size:9px;padding:2px 5px;border-radius:4px;background:rgba(0,0,0,.72);color:${typeClr};font-weight:700">${typeTag}</span></div>
      ${isNew ? `<div style="position:absolute;bottom:6px;left:6px"><span style="font-size:8px;padding:2px 6px;border-radius:4px;background:var(--green);color:#06281f;font-weight:800;letter-spacing:.4px">${t('rl.new_badge')}</span></div>` : ''}
      ${isLive ? `<div style="position:absolute;bottom:6px;right:6px" title="${t('rl.live_title')}"><span class="rel-live-badge"><span class="rel-live-dot"></span>${t('rl.live_badge')}</span></div>` : ''}
    </div>
    <div style="padding:8px 10px">
      <div style="font-size:12px;font-weight:600;color:var(--text);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;cursor:pointer" title="${esc(rel.title)} — ${t('rl.listen')}"
        onclick="playRelease('${esc(rel.service)}','${escJ(rel.url)}','${escJ(rel.title)}','${escJ(rel.artist)}','${escJ(rel.cover||'')}')"
        onmouseover="this.style.color='var(--red)'" onmouseout="this.style.color='var(--text)'">${esc(rel.title)}${hiresBadge}</div>
      <div style="font-size:11px;color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap${rel.artist_id ? ';cursor:pointer' : ''}" title="${esc(rel.artist)}"
        ${rel.artist_id ? `onclick="event.stopPropagation();openArtistPage('${esc(rel.service)}','${escJ(rel.artist_id)}')" onmouseover="this.style.textDecoration='underline'" onmouseout="this.style.textDecoration='none'"` : ''}>${esc(rel.artist)}</div>
      ${rel.label ? `<div style="font-size:10px;color:${rel.via_label ? 'var(--green)' : 'var(--muted)'};margin-top:1px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;opacity:${rel.via_label ? '1' : '.7'};cursor:pointer" title="${esc(rel.label)} — ${t('lbl.open_page')}"
        onclick="event.stopPropagation();openLabelPage('${escJ(rel.label)}')"
        onmouseover="this.style.textDecoration='underline'" onmouseout="this.style.textDecoration='none'">${rel.via_label ? '🏷 ' : ''}${esc(rel.label)}</div>` : ''}
      <div style="font-size:10px;color:var(--muted);margin-top:2px">${dt}${rel.tracks ? ' · ' + rel.tracks + ' ' + t('p.trk_abbr') : ''}</div>
      ${qualSelect}
      ${lyricsToggle}
      <div class="rel-avail" style="margin-top:6px;font-size:10px;line-height:1.5">
        <button onclick="event.stopPropagation();relCheckAvail(this,'${escJ(rel.url)}','${escJ(rel.title)}','${escJ(rel.artist)}')"
          style="padding:3px 7px;background:transparent;border:1px dashed var(--border);border-radius:6px;font-size:10px;color:var(--muted);cursor:pointer;font-family:var(--font)"
          title="${t('rl.avail_hint')}">${t('rl.avail_check')}</button>
      </div>
      <button class="rel-dl-btn" onclick="downloadRelease(this,'${esc(rel.service)}','${escJ(rel.url)}','${escJ(rel.title)}','${escJ(rel.artist)}')"
        style="padding:5px 4px;background:rgba(192,132,160,.12);border:1px solid rgba(192,132,160,.2);border-radius:7px;font-size:10px;font-weight:700;color:var(--red);cursor:pointer;font-family:var(--font)">${t('btn.download')}</button>
      <!-- .rel-act-row (main.css) — сетка равных колонок: перенос кнопок
           невозможен по построению, включая пятую («открыть релиз»), которая
           при старом flex-wrap:wrap болталась на второй строке. Отступы и
           min-width:0 живут в классе, здесь их задавать НЕЛЬЗЯ: inline
           перебьёт класс, и колонки снова перестанут сжиматься. -->
      <div class="rel-act-row">
        <button onclick="smartDownloadRelease(this,'${escJ(rel.url)}','${escJ(rel.title)}','${escJ(rel.artist)}')"
          style="background:transparent;border:1px solid rgba(255,214,10,.35);border-radius:7px;font-size:11px;color:#ffd60a;cursor:pointer;font-family:var(--font)" title="${t('rl.auto_src')}">⚡</button>
        ${typeof afButton === 'function' ? afButton(rel.artist, 'background:transparent;border:1px solid var(--border);border-radius:7px;font-size:11px;cursor:pointer;font-family:var(--font)') : ''}
        <button onclick="toggleRelFav('${escJ(uid)}')" style="background:transparent;border:1px solid ${isFav?'var(--orange)':'var(--border)'};border-radius:7px;font-size:11px;color:${isFav?'var(--orange)':'var(--muted)'};cursor:pointer;font-family:var(--font)" title="${isFav?t('sc2.unfav'):t('sc2.fav')}">${isFav?'★':'☆'}</button>
        <button onclick="navigator.clipboard.writeText('${escJ(rel.url)}');toast(t('toast.link_copied'))" style="background:transparent;border:1px solid var(--border);border-radius:7px;font-size:10px;color:var(--muted);cursor:pointer;font-family:var(--font)" title="${t('ck.copy_link')}">⎘</button>
        <a href="${esc(rel.url)}" onclick="event.preventDefault();event.stopPropagation();openExternal(this.href);return false" style="background:transparent;border:1px solid var(--border);border-radius:7px;font-size:10px;color:var(--muted);text-decoration:none" title="${t('ck.open_on')} ${escapeHtml(rel.service)}">↗</a>
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
  const lyricsChk = card ? card.querySelector('.rel-lyrics-chk') : null;
  const body = {url, quality, title, artist};
  if (lyricsChk) body.lyrics = lyricsChk.checked;
  const r = await api('POST', '/api/queue/add', body);
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
  const key = keyMap[svc];
  // Запасной путь `|| 'quality'` был миной: глобальный ключ `quality` — это
  // качество APPLE. Карточка сервиса без собственного ключа (BBC, SoundCloud,
  // Spotify, да и любая новая) переписывала им настройку Apple, и следующая
  // загрузка Apple уходила с чужим качеством. Со стороны это выглядело как
  // «просил один сервис, поехало другим» (разбор 01.08.2026: задача с Apple-
  // ссылкой и качеством `27` из Qobuz).
  //
  // Нет своего ключа — ничего не сохраняем: выбор действует на эту загрузку,
  // а глобальную настройку молча не трогает.
  if (!key) {
    if (typeof toast === 'function') toast(ti('rl.q_not_saved', { svc: svc || '?' }), 'var(--muted)');
    return;
  }
  saveSetting(key, sel.value);
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
    const svcName = {apple:'Apple', qobuz:'Qobuz', tidal:'Tidal', deezer:'Deezer',
                     beatport:'Beatport', yandex:'Yandex'}[c.service] || c.service;
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
  // BBC — не «релиз из треков», а одна многочасовая передача: играется по своему
  // пути (HLS-поток по pid), и трек-лист у неё появляется отдельно, из описания.
  // Общий разворот релиза её не знает и честно отвечал «Unsupported service: bbc»
  // — с точки зрения владельца это выглядело как «BBC не играет» (01.08.2026).
  if (service === 'bbc') {
    const pid = (url.match(/programmes\/([a-z0-9]+)/i) || [])[1];
    if (!pid) { toast(t('b.no_stream_url'), 'var(--red)'); return; }
    if (typeof bbcPlay !== 'function') { toast(t('t.error'), 'var(--red)'); return; }
    bbcPlay(pid, '', title, artist, cover || '');
    return;
  }
  toast('⏳ ' + title, 'var(--muted)', '', 1800);
  try {
    // title/artist — не для показа, а чтобы бэкенд мог найти тот же релиз в
    // Deezer, когда Spotify под лимитом запросов. Без них запасной путь
    // искать нечем, и ▶ просто отказывает.
    const r = await fetch(`/api/release/expand?service=${encodeURIComponent(service)}`
      + `&url=${encodeURIComponent(url)}`
      + `&title=${encodeURIComponent(title || '')}&artist=${encodeURIComponent(artist || '')}`);
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
    // ТОЛЬКО Spotify. Apple сюда добавлять НЕЛЬЗЯ, пока его движок не отдаёт
    // штрихкод релиза: подбор копии без UPC не находит ничего, фильтр вычищает
    // все треки, и Apple-карточка перестаёт играть ВООБЩЕ — вместо превью
    // получается тишина. Поймано проверкой сразу после правки (02.08.2026):
    // «треков 4, с играбельной копией 0, UPC альбома: None».
    if (service === 'spotify' || service === 'apple') {
      // Apple теперь ОТДАЁТ штрихбар (get_album → amp-api UPC), поэтому copy-
      // резолвер находит Deezer-копию и фильтр не вычищает всё в тишину. Если
      // копии нет (Apple-эксклюзив) — треков 0 → честное сообщение ниже, как у
      // Spotify, а не безмолвный обрыв.
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
      // Автор АЛЬБОМА (аргумент playRelease) — для заголовка группы в трек-листе.
      // Артист трека у компиляции ≠ автор сборника (David Duriez ≠ Mount Kimbie).
      albumArtist: artist || '',
      cover:     tr.artwork || cover || '',
      permalink: tr.url || url,
      full:      true,
      // Длительность из ответа /api/release/expand — иначе в трек-листе время
      // было только у играющего трека, у остальных «—» (бэкенд его отдаёт).
      duration:  Number(tr.duration || tr.length || 0) || 0,
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


/* Где релиз реально можно взять прямо сейчас.
 *
 * По нажатию, а не сразу: в сетке до 120 карточек, и опрос витрин за все — это
 * сотни запросов ради данных, на которые никто не смотрит. Мировая дата релиза
 * ничего не говорит о том, откуда файл отдаётся: витрины наполняются вразнобой,
 * а аккаунты у нас в разных странах.
 */
async function relCheckAvail(btn, url, title, artist) {
  const box = btn.parentNode;
  btn.disabled = true;
  btn.textContent = '⏳ ' + t('rl.avail_checking');
  try {
    const q = new URLSearchParams({url: url || '', title: title || '', artist: artist || ''});
    const r = await api('GET', '/api/availability?' + q.toString());
    if (!r || !r.ok) {
      box.innerHTML = '<span style="color:var(--orange)">' + esc((r && r.error) || t('rl.avail_fail')) + '</span>';
      return;
    }
    const svcs = r.services || {};
    const ready = Object.keys(svcs).filter(function(s){ return svcs[s] && svcs[s].available; });
    if (!ready.length) {
      box.innerHTML = '<span style="color:var(--muted)">' + esc(_availSummary(svcs)) + '</span>';
      return;
    }
    // Кнопка на каждый сервис, где релиз ЕСТЬ — качать из недоступного нельзя.
    // Собираем через DOM, а не склейкой строк: адрес релиза попадал бы внутрь
    // атрибута onclick, и одна кавычка в нём ломала бы всю карточку.
    box.innerHTML = '';
    const head = document.createElement('div');
    head.style.cssText = 'color:var(--muted);margin-bottom:2px';
    head.textContent = _availSummary(svcs);
    box.appendChild(head);
    ready.forEach(function(s){
      const b = document.createElement('button');
      b.textContent = s;
      b.style.cssText = 'padding:3px 7px;margin:2px 3px 0 0;background:rgba(62,207,170,.12);'
                      + 'border:1px solid rgba(62,207,170,.3);border-radius:6px;font-size:10px;'
                      + 'color:var(--green);cursor:pointer;font-family:var(--font)';
      b.onclick = function(ev){
        ev.stopPropagation();
        downloadRelease(b, s, String(svcs[s].url || ''), title, artist);
      };
      box.appendChild(b);
    });
  } catch (e) {
    box.innerHTML = '<span style="color:var(--orange)">' + esc(t('rl.avail_fail')) + '</span>';
  }
}

/* Сводка доступности собирается ЗДЕСЬ, а не на сервере.
 *
 * Сервер отдаёт состояние по каждому сервису с МАШИННОЙ причиной, а человеческий
 * текст строится на языке интерфейса. Первая версия присылала уже готовую
 * русскую строку — и она лезла в английский интерфейс как есть.
 */
function _availSummary(svcs) {
  const by = {ready: [], waiting: [], region: [], rights: [], notoken: [], noid: []};
  Object.keys(svcs || {}).forEach(function (s) {
    const v = svcs[s] || {};
    if (v.available) { by.ready.push(s); return; }
    if (v.reason === 'region_locked')  { by.region.push(s);  return; }
    // «Есть в каталоге, но у нашей учётки нет прав» — НЕ то же, что гео-лок:
    // регион лечится аккаунтом в другой стране, права — другой учёткой или
    // подпиской. Свалить их в одну строку значит отправить человека искать
    // прокси там, где прокси не поможет.
    if (v.reason === 'no_entitlement') { by.rights.push(s);  return; }
    if (v.reason === 'no_token')       { by.notoken.push(s); return; }
    if (v.reason === 'no_identifier')  { by.noid.push(s);    return; }
    by.waiting.push(s);
  });
  const parts = [];
  if (by.ready.length)   parts.push('✅ ' + by.ready.join(', ')   + ' — ' + t('rl.av_ready'));
  if (by.waiting.length) parts.push('⏳ ' + by.waiting.join(', ') + ' — ' + t('rl.av_waiting'));
  if (by.region.length)  parts.push('🚫 ' + by.region.join(', ')  + ' — ' + t('rl.av_region'));
  if (by.rights.length)  parts.push('🔒 ' + by.rights.join(', ')  + ' — ' + t('rl.av_rights'));
  if (by.notoken.length) parts.push('🔑 ' + by.notoken.join(', ') + ' — ' + t('rl.av_notoken'));
  if (by.noid.length)    parts.push('❔ ' + by.noid.join(', ')    + ' — ' + t('rl.av_noid'));
  return parts.join(' · ') || t('rl.av_nowhere');
}
