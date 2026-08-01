// ======================================================================
// «Раскопки» (Digs) — вкладка.
//
// Порядок на экране не косметический: СНАЧАЛА видно, на чём построен подбор,
// и только потом сам подбор. Рекомендация без видимого происхождения читается
// как реклама.
//
// Пласты жанров — это диаграмма долей, а не украшение, поэтому она сделана по
// правилам визуализации, а не на глаз:
//   • задача данных — величина (доля жанра), значит ОДИН тон, светлее→темнее
//     (порядковая шкала), а не радуга из категориальных цветов;
//   • тёмная и светлая темы имеют СВОИ шкалы: автоматический переворот тёмной
//     в светлую не работает — бледный конец перестаёт читаться на белом
//     (проверено: #f3d08a даёт 1.44:1 при пороге 2:1);
//   • обе шкалы прогнаны валидатором палитры (монотонность, зазор между
//     ступенями, контраст к поверхности, единство тона) — не «подобраны глазом»;
//   • подписи носят текстовые цвета, а не цвет своей полосы;
//   • у каждой полосы прямая подпись, поэтому легенда не нужна.
// ======================================================================

let _digsData = null;

// Вьюха и её скрипт версионируются РАЗДЕЛЬНО, поэтому у человека может оказаться
// новый HTML со старым JS (или наоборот) — и тогда любой getElementById по
// переименованному узлу возвращает null, а присваивание innerHTML роняет всю
// отрисовку целиком. Ровно это и случилось при перестройке вкладки 01.08.2026:
// «Cannot set properties of null». Пишем через заглушку: несовпадение версий
// теперь означает «часть блоков не отрисовалась», а не «вкладка мертва».
const _DG_SINK = { style: {}, innerHTML: '', textContent: '', firstChild: { nodeValue: '' } };
function _dg(id) { return document.getElementById(id) || _DG_SINK; }


// Порядковая янтарная шкала. Янтарь — не вкусовщина: вкладка про раскопки,
// и слои породы читаются этим тоном естественно. Значения ниже — те, что
// прошли проверку; менять их на глаз нельзя, только заново через валидатор.
const _DIGS_RAMP_DARK  = ['#f3d08a', '#e8b45c', '#d9962e', '#c07a1c', '#9c6015', '#75470f'];
const _DIGS_RAMP_LIGHT = ['#e0a746', '#c9862a', '#ab6b1b', '#8c5314', '#6d3f0f', '#4e2c0a'];

const _DIGS_SECTIONS = [
  { key: 'played_not_owned', t: 'digs.s_played' },
  { key: 'missing_release',  t: 'digs.s_missing' },
  { key: 'show_guest',       t: 'digs.s_guest'  },
  { key: 'forgotten',        t: 'digs.s_forgot' },
];

// Готовые направления для первого запуска. Список — не «жанры вообще», а то,
// что вообще имеет смысл искать этой качалкой; человек отмечает своё.
const _DIGS_GENRE_PICKS = [
  'deep house', 'progressive house', 'melodic techno', 'techno', 'trance',
  'balearic', 'chillout', 'ambient', 'downtempo', 'liquid funk',
  'drum & bass', 'jungle', 'breakbeat', 'synthwave', 'indie', 'idm',
  'dub techno', 'lo-fi', 'jazz', 'classical',
];

function _digsRampStep(i, total) {
  const dark = !document.documentElement.matches('[data-theme="light"]');
  const ramp = dark ? _DIGS_RAMP_DARK : _DIGS_RAMP_LIGHT;
  // Самая крупная доля — самый насыщенный конец шкалы.
  const idx = Math.min(ramp.length - 1, Math.round((i / Math.max(1, total - 1)) * (ramp.length - 1)));
  return ramp[idx];
}

// Пласт породы. Находки этого жанра лежат ВНУТРИ него — от этого метафора
// становится структурной, а не наклеенной: прокрутка вниз и есть углубление.
function _digsSeam(g, i, total, items) {
  const color = _digsRampStep(i, total);
  const w = Math.max(3, Math.min(100, g.share));
  return `<div class="dg-seam" style="background:linear-gradient(90deg,${color}0d,transparent 62%)">
    <div class="dg-seam-bar" style="width:${w}%;background:${color}"
         role="img" aria-label="${esc(g.genre)}: ${g.share}%"></div>
    <div class="dg-seam-head">
      <span class="dg-seam-name">${esc(g.genre)}</span>
      <span class="dg-seam-share">${g.share}%</span>
      <span class="dg-seam-who" title="${esc((g.artists || []).join(' · '))}">${esc((g.artists || []).slice(0, 3).join(' · '))}</span>
    </div>
    <div class="dg-grid">${items.map(_digsItem).join('')}</div>
  </div>`;
}

// Разложить находки по пластам: у каждой находки есть артист, у артиста — жанр.
// Что не легло ни в один пласт (жанр неизвестен) — уходит в общий список ниже,
// а не выбрасывается: «не знаю жанр» не повод прятать находку.
function _digsByGenre(profile, items) {
  const g = {};
  (profile.artists || []).forEach(a => { if (a.genre) g[a.name.toLowerCase()] = a.genre; });
  const out = {}, rest = [];
  items.forEach(it => {
    const gen = g[(it.artist || '').toLowerCase()];
    if (gen) (out[gen] = out[gen] || []).push(it); else rest.push(it);
  });
  return { out, rest };
}

function _digsChip(text, sub, cls, onx) {
  return `<span class="dg-chip ${cls || ''}">${esc(text)}`
    + (sub ? `<span style="color:var(--muted);font-size:10px">${esc(sub)}</span>` : '')
    + (onx ? `<button onclick="${onx}" title="${t('digs.not_mine')}">×</button>` : '')
    + `</span>`;
}

// Находка — предмет, откопанный в слое: круглая обложка, имя, ПРИЧИНА. Причина
// обязательна: подбор без видимого происхождения читается как реклама.
function _digsItem(it) {
  const title = it.title ? `${it.artist} — ${it.title}` : it.artist;
  const cover = it.cover
    ? `<img class="dg-cover" src="${esc(it.cover)}" loading="lazy" decoding="async" alt="">`
    : `<div class="dg-blank">♪</div>`;
  // Причина строится как «что ты делал, ЗАПЯТАЯ, чего не хватает». Вторая
  // половина и есть ответ на «почему мне это показывают» — её и подсвечиваем.
  const why = String(it.reason || '');
  const cut = why.indexOf(', ');
  const whyHtml = cut > 0
    ? esc(why.slice(0, cut + 2)) + '<b>' + esc(why.slice(cut + 2)) + '</b>'
    : esc(why);
  const enc = encodeURIComponent(it.artist || '');
  // Действия названы словами: иконка лупы не читалась совсем.
  const acts = (it.url
      ? `<button class="dg-act primary" onclick="event.stopPropagation();digsQueue(decodeURIComponent('${encodeURIComponent(it.url)}'))">${t('digs.a_get')}</button>`
      : '')
    + `<button class="dg-act" onclick="event.stopPropagation();digsBubbles(decodeURIComponent('${enc}'))">${t('digs.a_similar')}</button>`
    + `<button class="dg-act" onclick="event.stopPropagation();digsExclude(decodeURIComponent('${enc}'))">${t('digs.a_hide')}</button>`;
  const meta = ` data-artist="${esc(it.artist || '')}" data-title="${esc(it.title || '')}"`
    + ` data-url="${esc(it.url || '')}" data-svc="${esc(it.service || '')}"`
    + ` data-cover="${esc(it.cover || '')}"`;
  return `<div class="dg-item" tabindex="0"${meta} title="${esc(title)}">${cover}
    <div style="flex:1;min-width:0">
      <div class="dg-name">${esc(title)}</div>
      <div class="dg-why">${whyHtml}</div>
    </div>
    <div class="dg-acts">${acts}</div></div>`;
}

function digsRenderHero(d) {
  const p = d.profile || {};
  const all = Object.values(d.digs || {}).reduce((n, a) => n + (a ? a.length : 0), 0);
  const set = (id, v) => {
    const el = document.getElementById(id);
    if (el) el.firstChild.nodeValue = String(v);
  };
  set('dg-n-finds', all);
  set('dg-n-seams', (p.genres || []).length);
  set('dg-n-owned', d.owned_albums != null ? d.owned_albums : ((p.totals || {}).download_events || 0));
  // Объяснение — в подсказку на счётчиках: оно нужно один раз, а место занимало
  // постоянно (замечание владельца: «мелкие буквы с мануалом прямо на странице»).
  const cap = _dg('dg-hero-cap-host');
  const top = (p.genres || [])[0];
  if (cap && cap.setAttribute) cap.setAttribute('title', (top
    ? ti('digs.hero', { genre: top.genre, share: top.share,
                        dl: (p.totals || {}).download_events || 0,
                        art: (p.totals || {}).artists_known || 0 })
    : t('digs.hero_empty')).replace(/<[^>]+>/g, ''));

}

function digsRender() {
  const dta = _digsData;
  if (!dta) return;
  const p = dta.profile || {};
  _dg('digs-loading').style.display = 'none';
  try { digsApplyLook(); } catch (e) {}   // вид из настроек — до отрисовки
  digsRenderHero(dta);
  if (!p.seeded) digsShowOnboard();
  _dg('digs-profile').style.display = '';

  // В пласты идут находки, привязанные к релизу или треку. Гости шоу и забытое
  // жанра не имеют — им отдельные жилы ниже.
  const inSeams = [].concat(dta.digs.missing_release || [], dta.digs.played_not_owned || []);
  const { out, rest } = _digsByGenre(p, inSeams);
  const gs = (p.genres || []).slice(0, 7);
  _dg('digs-seams').innerHTML =
    gs.map((g, i) => _digsSeam(g, i, gs.length, (out[g.genre] || []).slice(0, 6))).join('')
    || `<div style="color:var(--muted);font-size:11px;padding:14px 0">${t('digs.no_genres')}</div>`;

  const extra = [
    { t: 'digs.s_guest',  items: dta.digs.show_guest || [] },
    { t: 'digs.s_forgot', items: dta.digs.forgotten || [] },
    { t: 'digs.s_other',  items: rest },
  ];
  _dg('digs-sections').innerHTML = extra.map(s2 => {
    if (!s2.items.length) return '';
    return `<div class="dg-vein"><h3>${t(s2.t)}</h3><span>${s2.items.length}</span></div>`
      + `<div class="dg-grid">${s2.items.slice(0, 12).map(_digsItem).join('')}</div>`;
  }).join('');

  _dg('digs-artists').innerHTML =
    (p.artists || []).filter(a => !a.is_show).slice(0, 20)
      // Имя кодируем, а не экранируем кавычки руками: у артистов бывают
      // апострофы («Sam Feldt's…»), и наивная подстановка в onclick рвёт
      // разметку. encodeURIComponent снимает вопрос целиком.
      .map(a => _digsChip(a.name, a.downloads ? String(a.downloads) : '',
                          a.favorite ? 'fav' : '',
                          `digsExclude(decodeURIComponent('${encodeURIComponent(a.name)}'))`)).join('');

  const shows = (p.shows || []);
  if (shows.length) {
    _dg('digs-shows-wrap').style.display = '';
    _dg('digs-shows').innerHTML =
      shows.slice(0, 10).map(s2 => _digsChip(s2.name, String(s2.episodes), '', '')).join('');
  }
  const tot = p.totals || {};
  _dg('digs-foot').innerHTML =
    ti('digs.foot', { dl: tot.download_events || 0, pl: tot.play_events || 0,
                      art: tot.artists_known || 0, foreign: dta.foreign_filtered || 0 });
}

async function digsLoad(force) {
  const btn = _dg('digs-refresh');
  if (btn) btn.disabled = true;
  try {
    _digsData = await api('GET', '/api/digs/finds' + (force ? '?force=1' : ''));
    digsRender();
  } catch (e) {
    const el = _dg('digs-loading');
    if (el) { el.style.display = ''; el.textContent = '✗ ' + (e.message || e); }
  } finally {
    if (btn) btn.disabled = false;
  }
}

function digsInit() {
  if (!_digsData) digsLoad(false);
}

function digsShowOnboard() {
  const box = _dg('digs-onboard');
  if (!box) return;
  box.style.display = '';
  const wrap = _dg('digs-ob-genres');
  const chosen = new Set(((_digsData || {}).profile || {}).favorite_genres || []);
  wrap.innerHTML = _DIGS_GENRE_PICKS.map(g =>
    `<span class="dg-chip dg-pick${chosen.has(g) ? ' on' : ''}" data-g="${esc(g)}"
       onclick="this.classList.toggle('on')">${esc(g)}</span>`).join('');
  const ta = _dg('digs-ob-artists');
  if (ta && !ta.value) {
    const favs = ((_digsData || {}).profile || {}).artists || [];
    ta.value = favs.filter(a => a.favorite).map(a => a.name).join(', ');
  }
  box.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

async function digsSaveFavorites() {
  const st = _dg('digs-ob-status');
  const genres = Array.from(document.querySelectorAll('#digs-ob-genres .dg-pick.on'))
    .map(e => e.dataset.g);
  const artists = (_dg('digs-ob-artists').value || '')
    .split(',').map(s => s.trim()).filter(Boolean);
  if (st) { st.textContent = t('digs.saving'); st.style.color = 'var(--muted)'; }
  try {
    await api('POST', '/api/digs/favorites', { artists, genres });
    if (st) { st.textContent = t('digs.saved'); st.style.color = 'var(--green)'; }
    _dg('digs-onboard').style.display = 'none';
    await digsLoad(true);
  } catch (e) {
    if (st) { st.textContent = '✗ ' + (e.message || e); st.style.color = 'var(--red)'; }
  }
}

// «Это не моё» — слово владельца выше любых улик из статистики: надёжно вывести
// принадлежность из данных нельзя, а в базе лежат ещё и загрузки гостей бота.
async function digsExclude(name) {
  try {
    await api('POST', '/api/digs/exclude', { artist: name });
    toast(ti('digs.excluded', { name }), 'var(--muted)');
    await digsLoad(true);
  } catch (e) {
    toast('✗ ' + (e.message || e), 'var(--red)');
  }
}

// Найденное отправляем обычным путём очереди — отдельной «загрузки
// рекомендации» не заводим, иначе у неё будет своя судьба и свои баги.
async function digsQueue(url) {
  try {
    await api('POST', '/api/download', { url });
    toast(t('digs.queued'), 'var(--green)');
  } catch (e) {
    toast('✗ ' + (e.message || e), 'var(--red)');
  }
}

function digsSearch(artist) {
  if (!artist) return;
  const nav = document.querySelector('.nav-item[data-view="search"]');
  if (typeof showView === 'function') showView('search', nav);
  // Поле поиска называется search-q. Раньше здесь стояло search-input — такого
  // элемента нет вовсе, поэтому запрос никуда не подставлялся и «Искать»
  // открывало пустой поиск. Ищем по АРТИСТУ, а не по альбому: мы пришли сюда с
  // именем артиста, и искать его в альбомах — не то, чего человек ждёт.
  const inp = document.getElementById('search-q');
  if (!inp) return;
  inp.value = artist;
  const ty = document.getElementById('search-type');
  if (ty && Array.from(ty.options).some(o => o.value === 'artist')) ty.value = 'artist';
  // Вьюха поиска подгружается лениво — если её ещё нет, ждём появления поля.
  if (typeof doSearch === 'function') setTimeout(doSearch, 60);
}


// ── Оформление из настроек ────────────────────────────────────────────────
// Настройки живут в Настройках → Раскопки, а применяются здесь через CSS-
// переменные: так вид меняется без перерисовки данных и без второго запроса.
const _DG_SHAPES = { circle: '50%', squircle: '14px', square: '4px', hex: '0' };
const _DG_DENSITY = { tight: '4px', normal: '8px', airy: '15px' };

function digsApplyLook() {
  const c = (window.S && S.config) || {};
  const root = document.getElementById('view-digs');
  if (!root) return;
  // Умолчание держим тем же, что в CSS вьюхи (56): иначе JS перебивает
  // стиль и обложка тихо остаётся мелкой.
  const size = parseInt(c['digs-size'] || 56, 10);
  root.style.setProperty('--dg-size', size + 'px');
  root.style.setProperty('--dg-radius', _DG_SHAPES[c['digs-shape'] || 'circle'] || '50%');
  root.style.setProperty('--dg-gap', _DG_DENSITY[c['digs-density'] || 'normal'] || '8px');
  // Шестиугольник — не радиус, а обрезка формы; поэтому отдельным свойством.
  root.style.setProperty('--dg-clip', (c['digs-shape'] === 'hex')
    ? 'polygon(25% 5%,75% 5%,100% 50%,75% 95%,25% 95%,0% 50%)' : 'none');
  root.dataset.bg = c['digs-bg'] || 'earth';
  root.dataset.motion = c['digs-motion'] || 'full';
  root.dataset.covers = (c['digs-covers'] !== false) ? '1' : '0';
  root.dataset.coon = (c['digs-coon'] !== false) ? '1' : '0';
}

// Чипы «сервисы» и «разделы» для вкладки настроек. Строим из кода, а не руками
// в разметке: список сервисов должен совпадать с тем, что реально умеет качалка.
const _DG_SVCS = ['apple', 'spotify', 'deezer', 'tidal', 'qobuz', 'soundcloud', 'beatport', 'yandex'];
const _DG_SECS = [
  ['played_not_owned', 'digs.s_played'], ['missing_release', 'digs.s_missing'],
  ['show_guest', 'digs.s_guest'], ['forgotten', 'digs.s_forgot'],
];

function _dgChipList(hostId, items, cfgKey, labelFn, defaultAll) {
  const host = document.getElementById(hostId);
  if (!host) return;
  const c = (window.S && S.config) || {};
  const cur = c[cfgKey];
  const on = new Set(Array.isArray(cur) && cur.length ? cur : (defaultAll ? items.map(i => i[0] || i) : []));
  host.innerHTML = items.map(it => {
    const key = it[0] || it;
    return `<span class="dg-chip dg-pick${on.has(key) ? ' on' : ''}" data-k="${esc(key)}"
      onclick="this.classList.toggle('on');_dgSaveChips('${hostId}','${cfgKey}')">${esc(labelFn(it))}</span>`;
  }).join('');
}

function _dgSaveChips(hostId, cfgKey) {
  const on = Array.from(document.querySelectorAll('#' + hostId + ' .dg-pick.on')).map(e => e.dataset.k);
  saveSetting(cfgKey, on);
}

function digsBuildSettingChips() {
  _dgChipList('dg-svc-list', _DG_SVCS, 'digs-services', s => s, false);
  _dgChipList('dg-sec-list', _DG_SECS, 'digs-sections', it => t(it[1]), true);
}


// ── Клики по находкам ─────────────────────────────────────────────────────
// Одинарный и двойной клик висят на ОДНОМ элементе, поэтому одинарный
// откладывается: браузер всегда выдаёт click первым, и без задержки двойной
// срабатывал бы ещё и как одинарный. 260 мс — обычный порог двойного клика.
let _dgClickTimer = null;

function _dgItemData(el) {
  return {
    artist: el.dataset.artist || '', title: el.dataset.title || '',
    url: el.dataset.url || '', service: el.dataset.svc || '',
    cover: el.dataset.cover || '',
  };
}

// Сервис выводим из ссылки: у находок из стора радара поля service нет, оно
// есть только у прослушиваний.
function _dgService(d) {
  if (d.service) return d.service;
  const u = (d.url || '').toLowerCase();
  for (const n of ['spotify', 'deezer', 'tidal', 'qobuz', 'soundcloud', 'apple']) {
    if (u.includes(n)) return n;
  }
  return '';
}

function digsItemClick(el) {
  const d = _dgItemData(el);
  const mode = ((window.S && S.config) || {})['digs-click'] || 'play';
  if (mode === 'none') return;
  if (!d.url) {
    // Играть нечего: у «гостей шоу» и «забытого» конкретного релиза нет —
    // предлагается артист, и правильное действие это поиск, а не тишина.
    digsSearch(d.artist);
    return;
  }
  const svc = _dgService(d);
  if (mode === 'open') { openExternal(d.url); return; }
  // Играем НАШИМ плеером и нашими токенами — никаких чужих превью.
  if (typeof playRelease === 'function') {
    playRelease(svc, d.url, d.title || d.artist, d.artist, d.cover);
  } else {
    digsSearch(d.artist);
  }
}

function digsItemDbl(el) {
  if (((window.S && S.config) || {})['digs-dblclick'] === 'off') return;
  digsBubbles(_dgItemData(el).artist);
}

// Делегирование: находки перерисовываются целиком, и вешать слушатели на
// каждую заново — лишняя работа и источник утечек.
document.addEventListener('click', (e) => {
  const el = e.target.closest && e.target.closest('#view-digs .dg-item');
  if (!el || e.target.closest('.dg-act')) return;   // кнопка действия — своя логика
  clearTimeout(_dgClickTimer);
  _dgClickTimer = setTimeout(() => digsItemClick(el), 260);
});
document.addEventListener('dblclick', (e) => {
  const el = e.target.closest && e.target.closest('#view-digs .dg-item');
  if (!el) return;
  clearTimeout(_dgClickTimer);                      // отменяем отложенный одинарный
  digsItemDbl(el);
});
// Клавиатура: находка — это tabindex-элемент, значит должна открываться Enter.
document.addEventListener('keydown', (e) => {
  if (e.key !== 'Enter') return;
  const el = e.target.closest && e.target.closest('#view-digs .dg-item');
  if (el) digsItemClick(el);
});

// ── Дерево похожих артистов ───────────────────────────────────────────────
// Пузыри по кругу вокруг центрального. Клик по пузырю уходит глубже — так и
// получается дерево: каждый шаг это новый круг вокруг выбранного.
// Выбранные в дереве. Множественный выбор нужен затем, что находки приходят
// пачкой: отметить пятерых и разом взять их в вишлист — обычный сценарий, а
// по одному это пять кругов туда-обратно.
let _dgPicked = new Set();

function _dgBubbleFace(name, pic, size) {
  // Фото, а если его нет — инициал. Пустой кружок с одним текстом читается
  // хуже, а подставлять чужое лицо нельзя, поэтому имя в ответе сверяется.
  return pic
    ? `<img src="${esc(pic)}" alt="" loading="lazy" decoding="async"
         style="width:100%;height:100%;object-fit:cover;border-radius:50%">`
    : `<span class="dg-bb-ini">${esc((name || '?').trim()[0] || '?')}</span>`;
}

function digsPick(name, el) {
  if (_dgPicked.has(name)) _dgPicked.delete(name); else _dgPicked.add(name);
  if (el) el.classList.toggle('on', _dgPicked.has(name));
  const bar = document.getElementById('dg-bb-picked');
  if (bar) {
    bar.style.display = _dgPicked.size ? '' : 'none';
    const cnt = document.getElementById('dg-bb-cnt');
    if (cnt) cnt.textContent = String(_dgPicked.size);
  }
}

async function digsPickedWatch(btn) {
  const names = Array.from(_dgPicked);
  if (!names.length) return;
  if (btn) btn.disabled = true;
  let ok = 0;
  for (const n of names) {
    try { await api('POST', '/api/watchlist', { name: n, service: 'apple', kind: 'artist' }); ok++; }
    catch (e) { /* по одному: одна неудача не должна ронять всю пачку */ }
  }
  toast(ti('digs.picked_watched', { n: ok }), 'var(--green)');
  if (btn) btn.disabled = false;
}

async function digsPickedMine(btn) {
  const names = Array.from(_dgPicked);
  if (!names.length) return;
  if (btn) btn.disabled = true;
  const cur = ((window.S && S.config) || {})['digs-favorite-artists'] || [];
  const next = Array.from(new Set([...cur, ...names]));
  try {
    await api('POST', '/api/digs/favorites',
              { artists: next, genres: (S.config || {})['digs-favorite-genres'] || [] });
    if (S.config) S.config['digs-favorite-artists'] = next;
    _digsData = null;                     // профиль изменился — пересчитать
    toast(ti('digs.picked_mined', { n: names.length }), 'var(--green)');
  } catch (e) { toast('✗ ' + (e.message || e), 'var(--red)'); }
  if (btn) btn.disabled = false;
}

async function digsBubbles(artist) {
  if (!artist) return;
  let ov = document.getElementById('dg-bubbles');
  if (!ov) {
    ov = document.createElement('div');
    ov.id = 'dg-bubbles';
    ov.onclick = (e) => { if (e.target === ov) { ov.remove(); _dgPicked.clear(); } };
    document.body.appendChild(ov);
  }
  ov.innerHTML = `<div class="dg-bb-wrap"><div class="dg-bb-load">${t('digs.bb_loading')}</div></div>`;
  let items = [], corePic = '';
  try {
    const r = await api('GET', '/api/digs/similar?artist=' + encodeURIComponent(artist) + '&limit=12');
    items = (r && r.items) || [];
    corePic = (r && r.pic) || '';
  } catch (e) { /* пусто — покажем честное «не нашёл» */ }

  if (!items.length) {
    ov.innerHTML = `<div class="dg-bb-wrap"><div class="dg-bb-load">`
      + ti('digs.bb_empty', { name: esc(artist) }) + `</div></div>`;
    return;
  }
  const max = Math.max(...items.map(i => i.score || 1)) || 1;
  const R = 195;
  const nodes = items.map((it, i) => {
    const ang = (i / items.length) * Math.PI * 2 - Math.PI / 2;
    // Размер несёт похожесть — пузырь остаётся данными, а не украшением.
    const sz = 54 + Math.round(34 * ((it.score || 1) / max));
    const enc = encodeURIComponent(it.name);
    return `<div class="dg-bb${_dgPicked.has(it.name) ? ' on' : ''}"
      style="left:calc(50% + ${Math.cos(ang) * R}px);top:calc(50% + ${Math.sin(ang) * R}px);
      width:${sz}px;height:${sz}px;animation-delay:${(i * 0.045).toFixed(2)}s"
      title="${esc(it.name)}"
      ondblclick="digsBubbles(decodeURIComponent('${enc}'))">
      ${_dgBubbleFace(it.name, it.pic, sz)}
      <span class="dg-bb-cap">${esc(it.name)}</span>
      <button class="dg-bb-tick" title="${t('digs.pick')}"
        onclick="event.stopPropagation();digsPick(decodeURIComponent('${enc}'),this.parentNode)">✓</button>
    </div>`;
  }).join('');
  const encA = encodeURIComponent(artist);
  ov.innerHTML = `<div class="dg-bb-wrap">
      <button class="dg-bb-close" onclick="document.getElementById('dg-bubbles').remove()">×</button>
      <div class="dg-bb dg-bb-core" style="left:50%;top:50%;width:112px;height:112px">
        ${_dgBubbleFace(artist, corePic, 112)}
        <span class="dg-bb-cap">${esc(artist)}</span>
      </div>
      ${nodes}
      <div class="dg-bb-acts">
        <button class="dg-bb-act" onclick="digsWatch(decodeURIComponent('${encA}'),this)">${t('digs.bb_watch')}</button>
        <button class="dg-bb-act" onclick="digsMarkMine(decodeURIComponent('${encA}'),this)">${t('digs.bb_mine')}</button>
        <button class="dg-bb-act" onclick="digsSearch(decodeURIComponent('${encA}'))">${t('digs.bb_find')}</button>
      </div>
      <div class="dg-bb-picked" id="dg-bb-picked" style="display:${_dgPicked.size ? '' : 'none'}">
        <span>${t('digs.picked')} <b id="dg-bb-cnt">${_dgPicked.size}</b></span>
        <button class="dg-bb-act" onclick="digsPickedWatch(this)">${t('digs.bb_watch')}</button>
        <button class="dg-bb-act" onclick="digsPickedMine(this)">${t('digs.bb_mine')}</button>
        <button class="dg-bb-act" onclick="_dgPicked.clear();digsBubbles(decodeURIComponent('${encA}'))">${t('digs.picked_clear')}</button>
      </div>
      <div class="dg-bb-hint">${t('digs.bb_hint')}</div>
    </div>`;
}

// ── Действия над найденным артистом ───────────────────────────────────────
// Следить через ВИШЛИСТ, а не через свою сущность: у вишлиста уже есть проверка
// новых релизов, автоскачка и вся обвязка. Заводить рядом второй список
// «избранное раскопок» значило бы поддерживать две судьбы одного намерения.
async function digsWatch(name, btn) {
  if (!name) return;
  try {
    // Следим ВСЕГДА через Apple: это единственный бесплатный полный каталог по
    // артисту (см. правило вишлиста); service говорит лишь куда качать.
    await api('POST', '/api/watchlist', { name, service: 'apple', kind: 'artist' });
    if (btn) { btn.classList.add('done'); btn.textContent = t('digs.bb_watching'); }
    toast(ti('digs.watched', { name }), 'var(--green)');
  } catch (e) {
    toast('✗ ' + (e.message || e), 'var(--red)');
  }
}

// «Это моё» — дописывает артиста в профиль вкуса. Именно здесь и замыкается
// самообучение: откопал → отметил → следующий подбор считается уже с ним.
async function digsMarkMine(name, btn) {
  if (!name) return;
  const cur = ((window.S && S.config) || {})['digs-favorite-artists'] || [];
  const next = Array.from(new Set([...cur, name]));
  try {
    await api('POST', '/api/digs/favorites', { artists: next, genres: (S.config || {})['digs-favorite-genres'] || [] });
    if (S.config) S.config['digs-favorite-artists'] = next;
    if (btn) { btn.classList.add('done'); btn.textContent = t('digs.bb_mine_done'); }
    toast(ti('digs.mined', { name }), 'var(--green)');
    _digsData = null;                     // профиль изменился — пересчитать при возврате
  } catch (e) {
    toast('✗ ' + (e.message || e), 'var(--red)');
  }
}
