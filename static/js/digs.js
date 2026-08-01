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
      <span class="dg-seam-who">${esc((g.artists || []).join(' · '))}</span>
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
  // Кнопка «в очередь» только там, где есть КОНКРЕТНЫЙ релиз. У гостей шоу и
  // забытого релиза нет — там предлагается артист, и уместен поиск.
  const act = it.url
    ? `<button class="dg-act" onclick="digsQueue(decodeURIComponent('${encodeURIComponent(it.url)}'))" title="${t('digs.add')}">↓</button>`
    : `<button class="dg-act" onclick="digsSearch(decodeURIComponent('${encodeURIComponent(it.artist)}'))" title="${t('digs.find')}">🔍</button>`;
  return `<div class="dg-item">${cover}
    <div style="flex:1;min-width:0">
      <div class="dg-name">${esc(title)}</div>
      <div class="dg-why">${esc(it.reason || '')}</div>
    </div>${act}</div>`;
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
  const cap = _dg('dg-hero-cap');
  const top = (p.genres || [])[0];
  if (cap) cap.innerHTML = top
    ? ti('digs.hero', { genre: esc(top.genre), share: top.share,
                        dl: (p.totals || {}).download_events || 0,
                        art: (p.totals || {}).artists_known || 0 })
    : t('digs.hero_empty');
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
  const nav = document.querySelector('.nav-item[data-view="search"]');
  if (typeof showView === 'function') showView('search', nav);
  const inp = document.getElementById('search-input');
  if (inp) { inp.value = artist; if (typeof doSearch === 'function') doSearch(); }
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
  const size = parseInt(c['digs-size'] || 44, 10);
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
