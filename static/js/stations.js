/* ══ СТАНЦИИ ══════════════════════════════════════════════════════════════════
 *
 * Вкладка «Станции»: жанровые плитки плюс то, что собрано из СОБСТВЕННОЙ
 * истории — кого слушал, что качал, какие жанры при этом звучали.
 *
 * Радар сюда не тянем: там своя тема (грядущее и подписки), и владелец
 * попросил оставить её отдельно.
 *
 * Правило показа одно и то же во всех секциях: **не показывать того, чего не
 * знаем**. Нет прослушиваний — секции нет, а не «рекомендуем популярное».
 * Жанр не лёг ни на одну плитку — он остаётся в списке фактом, но без ссылки:
 * увести человека на соседний жанр хуже, чем честно ничего не предложить.
 */

/* ── Вид плитки: один в один с мобильной WaveTile ─────────────────────────
 *
 * Палитра и правило выбора цветов взяты из `ui/screens/HomeScreen.kt`
 * (WAVE_PALETTE + WaveTile), включая ФОРМУЛУ ХЕША. Это не педантизм: хеш
 * там — обычный `String.hashCode()` из Java, и повторив его здесь, мы
 * получаем ОДИН И ТОТ ЖЕ цвет у одного и того же жанра на телефоне и на
 * компьютере. Разъехавшиеся цвета выглядели бы как два разных продукта.
 */
const ST_PALETTE = ['#FF4D8F', '#A238FF', '#3A5FD9', '#1ECBE1', '#FF5C3C', '#38E0A0'];

/** `String.hashCode()` из Java: h = 31*h + c, с переполнением в 32 бита. */
function stHash(s) {
  let h = 0;
  for (let i = 0; i < s.length; i++) h = (Math.imul(31, h) + s.charCodeAt(i)) | 0;
  return Math.abs(h);
}

function stHexA(hex, a) {
  const n = parseInt(hex.slice(1), 16);
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
}

/** Градиент плитки по её имени. Те же альфы, что в WaveTile: .9 → .55. */
function stGradient(seed, a1, a2) {
  const h = stHash(String(seed || ''));
  const i = h % ST_PALETTE.length;
  let j = (Math.floor(h / 7) + 3) % ST_PALETTE.length;
  // Если оба индекса совпали, градиента нет — плитка выходит плоской заливкой.
  // На тридцати наших станциях так случалось у трёх; сдвиг на один цвет
  // убирает это, не трогая остальные. Та же страховка добавлена в мобильную
  // WaveTile, чтобы вид не разъехался между версиями.
  if (j === i) j = (j + 1) % ST_PALETTE.length;
  return `linear-gradient(135deg, ${stHexA(ST_PALETTE[i], a1)}, ${stHexA(ST_PALETTE[j], a2)})`;
}

let _stHome = null;          // ответ /api/stations/home
let _stBusy = false;
let _stMineIds = { has: () => false, plays: () => 0 };  // станции своего жанра

async function stationsInit(force) {
  if (_stBusy) return;
  if (_stHome && !force) { stRender(); return; }
  _stBusy = true;
  const box = document.getElementById('st-body');
  if (box) box.innerHTML = `<div class="st-note">${t('st.loading')}</div>`;
  try {
    _stHome = await api('GET', '/api/stations/home');
    stRender();
  } catch (e) {
    if (box) box.innerHTML = `<div class="st-note st-err">${esc(t('st.load_failed'))}: ${esc(String(e))}</div>`;
  } finally {
    _stBusy = false;
  }
}

function stRender() {
  const box = document.getElementById('st-body');
  if (!box || !_stHome) return;
  const d = _stHome;
  const parts = [];

  const mine = stMyWave(d);
  _stMineIds = stMyGenreIds(d);

  // ── Моя волна ─────────────────────────────────────────────────────────────
  // Одна большая карточка наверху, как у Яндекса, Spotify и Apple. Крутилка
  // характера живёт внутри неё под «⚙ Настроить», а не отдельной полосой над
  // всем экраном (PLAN.md, «Экран станций», владелец 29.09).
  //
  // Если своей основы нет ни под что — карточки нет вовсе. «Моя волна» без
  // собственной истории была бы обещанием, которое сервер не выполнит. Ручка
  // при этом не исчезает: без карточки она остаётся в своей полосе над
  // плитками (см. stRenderKnobs).
  if (mine) parts.push(stHeroHtml(mine));

  // ── Свои артисты: ряд круглых карточек ────────────────────────────────────
  // Подпись-объяснение убрана: ряд из лиц говорит сам за себя, а счётчики
  // остались в подсказке на наведение.
  if ((d.artists || []).length) {
    parts.push(stSection(t('st.your_artists'), '',
      `<div class="st-artrow">` + (d.artists || []).map(a => stArtistCard(a)).join('') + `</div>`));
  }

  // ── Все жанровые станции ──────────────────────────────────────────────────
  // Отдельного блока «ваши жанры» больше нет: свои жанры идут ПЕРВЫМИ здесь же,
  // с маленькой меткой «твоё». На экран при этом попадает только НАЗВАНИЕ
  // станции — сервисные строки вроде «Алтернативен рок» остаются в сопоставлении,
  // потому что человек выбирал не их.
  const mineIds = _stMineIds;
  const tiles = stOrderTiles(d.tiles || [], mineIds);
  parts.push(stSection(
    t('st.all_genres'), '',
    tiles.map(s => stTileHtml(s, mineIds.has(s.id))).join('')
  ));

  // ── Что играло недавно ────────────────────────────────────────────────────
  if ((d.recent || []).length) {
    parts.push(stSection(
      t('st.recent'), '',
      `<div class="st-list">` + (d.recent || []).slice(0, 12).map(r =>
        `<div class="st-row">
           <div class="st-row-main">
             <div class="st-row-t">${esc(r.title || '')}</div>
             <div class="st-row-a">${esc(r.artist || '')}${r.service ? ' · ' + esc(r.service) : ''}${r.times > 1 ? ' · ' + esc(t('st.chip_plays').replace('{0}', r.times)) : ''}</div>
           </div>
           <button class="st-mini" onclick="stPlayArtist(${escJ2(r.artist || '')})">${esc(t('st.station_of'))}</button>
         </div>`).join('') + `</div>`
    ));
  }

  // ── Откуда качалось ───────────────────────────────────────────────────────
  if ((d.services || []).length) {
    parts.push(stSection(
      t('st.services'), '',
      `<div class="st-svcs">` + (d.services || []).map(([name, n]) =>
        `<span class="st-svc">${esc(name)} <b>${n}</b></span>`).join('') + `</div>`
    ));
  }

  const c = d.counts || {};
  parts.push(`<div class="st-note st-foot">${esc(
    t('st.footer').replace('{0}', c.plays || 0).replace('{1}', c.downloads || 0))}</div>`);

  box.innerHTML = parts.join('');
  stRenderKnobs();
  stWarmPreviews();
  stRugStart();
}

/**
 * Плитка станции.
 *
 * Обложки — из НАСТОЯЩЕЙ выдачи этой станции, а не подобранная под жанр
 * картинка: подставить «примерную» обложку значит соврать ровно так же, как
 * бейджем качества из константы. Пока станция ни разу не собиралась, обложек
 * нет — и на их месте честно остаётся градиент, а не заглушка, притворяющаяся
 * музыкой.
 */
/* «Ковёр» (владелец 25.09): вся карточка — кнопка «играть», по центру большая
   ▶, под ней мозаика обложек станции, которая медленно плывёт, сама меняет
   клетки и откликается на мышь. Клеток всегда ST_RUG_CELLS: обложек меньше —
   повторяем по кругу; нет вовсе — честный градиент без картинок. */
const ST_RUG_CELLS = 8;

function stTileHtml(s, mine) {
  const isMine = (mine === undefined) ? _stMineIds.has(s.id) : !!mine;
  const covers = (s.covers || []).filter(Boolean);
  const grad = stGradient(s.id, .9, .55);
  // Обложка — <img>, а не фон: только у картинки есть `onerror` (часть ссылок
  // отдаёт 404, снимок 07.09.2026). Не пришла — прячем, остаётся градиент.
  const cells = covers.length
    ? Array.from({ length: ST_RUG_CELLS }, (_, i) =>
        `<img src="${esc(covers[i % covers.length])}" alt="" loading="lazy" decoding="async"
              onerror="this.style.visibility='hidden'">`).join('')
    : '';
  const sub = (s.services || []).length
    ? (s.services || []).slice(0, 3).join(' · ')
    : t('st.tile_no_preview');
  const cnt = s.tracks ? `${s.tracks} · ` : '';
  const phase = (stHash(s.id) % 9) * -1.7;      // ковры не плывут в ногу
  return `<button class="st-tile st-rugcard" data-st="${esc(s.id)}" onclick="stPlay(${escJ2(s.id)})"
                  title="${esc(t('st.tile_play').replace('{0}', s.title))}"
                  aria-label="${esc(t('st.tile_play').replace('{0}', s.title))}"
                  onpointermove="stRugTilt(event,this)" onpointerleave="stRugTilt(null,this)">
    <span class="st-rug" style="background:${grad}">
      <span class="st-rug-grid" style="animation-delay:${phase}s">${cells}</span>
    </span>
    <span class="st-rug-shade"></span>
    <span class="st-bigplay" style="background:${stGradient(s.id, 1, .8)}">&#9654;</span>
    <span class="st-tile-body">
      <span class="st-tile-name">${esc(s.title)}</span>
      ${isMine ? `<span class="st-mine">${esc(t('st.mine'))}</span>` : ''}
      <span class="st-tile-sub">${esc(cnt + sub)}</span>
    </span>
  </button>`;
}

/** Отклик на мышь: ковёр чуть смещается за курсором. */
function stRugTilt(ev, el) {
  if (!el) return;
  if (!ev) { el.style.removeProperty('--mx'); el.style.removeProperty('--my'); return; }
  const r = el.getBoundingClientRect();
  el.style.setProperty('--mx', (((ev.clientX - r.left) / r.width) - .5).toFixed(3));
  el.style.setProperty('--my', (((ev.clientY - r.top) / r.height) - .5).toFixed(3));
}

/* Живая смена клеток: раз в пару секунд у одной видимой карточки одна клетка
   плавно меняет обложку на другую из той же станции. Вкладка скрыта или
   человек просил меньше движения — стоим. */
let _stRugTimer = null;
function stRugStart() {
  if (_stRugTimer) return;
  if (window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches) return;
  _stRugTimer = setInterval(() => {
    const view = document.getElementById('view-stations');
    if (!view || !view.classList.contains('active') || document.hidden || !_stHome) return;
    const cards = Array.from(view.querySelectorAll('.st-rugcard')).filter(c => {
      const r = c.getBoundingClientRect();
      return r.bottom > 0 && r.top < innerHeight;
    });
    if (!cards.length) return;
    const card = cards[Math.floor(Math.random() * cards.length)];
    const st = (_stHome.tiles || []).find(x => x.id === card.dataset.st);
    const covers = ((st && st.covers) || []).filter(Boolean);
    if (covers.length < 2) return;
    const imgs = card.querySelectorAll('.st-rug-grid img');
    if (!imgs.length) return;
    const img = imgs[Math.floor(Math.random() * imgs.length)];
    const next = covers[Math.floor(Math.random() * covers.length)];
    if (img.getAttribute('src') === next) return;
    img.classList.add('swap');
    setTimeout(() => {
      img.style.visibility = '';
      img.src = next;
      img.onload = () => img.classList.remove('swap');
      setTimeout(() => img.classList.remove('swap'), 900);
    }, 450);
  }, 1600);
}

/**
 * Догреть недостающие превью — по одной станции, в фоне.
 *
 * Собрать все тридцать разом нельзя: это тридцать опросов витрин ради
 * картинок, на которые ещё никто не смотрел. Поэтому берём небольшую порцию
 * за посещение, строго по очереди, и обновляем плитку сразу, как узнали. Кэш
 * на сервере живёт неделю, так что со временем страница наполняется сама.
 */
const ST_WARM_PER_VISIT = 6;
let _stWarming = false;

async function stWarmPreviews() {
  if (_stWarming || !_stHome) return;
  _stWarming = true;
  try {
    const need = (_stHome.tiles || []).filter(s => !(s.covers || []).length)
                                      .slice(0, ST_WARM_PER_VISIT);
    for (const s of need) {
      // Вкладку могли закрыть — тогда греть незачем.
      if (!document.getElementById('view-stations')?.classList.contains('active')) break;
      let r = null;
      try { r = await api('GET', '/api/station/preview?id=' + encodeURIComponent(s.id)); }
      catch (_) { continue; }
      if (!r || !(r.covers || []).length) continue;
      s.covers = r.covers; s.services = r.services || []; s.tracks = r.tracks || 0;
      const el = document.querySelector(`.st-tile[data-st="${CSS.escape(s.id)}"]`);
      if (el) el.outerHTML = stTileHtml(s);
    }
  } finally {
    _stWarming = false;
  }
}

/**
 * Основа «моей волны»: {sid, name, artist}.
 *
 * Приоритет — жанровая станция, а не артист: только у сессии есть дозаказ и
 * ручки, а эфир «вокруг артиста» — одна пачка, где «⚙ Настроить» обещала бы то,
 * чего не сделать (AGENTS.md: настройка, которая ничего не меняет, хуже
 * отсутствующей).
 */
function stMyWave(d) {
  const g = (d.genres || []).filter(x => x.station)[0];
  if (g) {
    const tile = (d.tiles || []).filter(x => x.id === g.station)[0];
    return { sid: g.station, name: tile ? tile.title : '', artist: '' };
  }
  const a = (d.artists || [])[0];
  if (a) return { sid: '', name: a.name, artist: a.name };
  return null;
}

/** Станции, к которым привёл собственный жанр: id → сколько прослушиваний. */
function stMyGenreIds(d) {
  const m = new Map();
  (d.genres || []).forEach(g => {
    if (g.station) m.set(g.station, (m.get(g.station) || 0) + (g.plays || 0));
  });
  return { has: id => m.has(id), plays: id => m.get(id) || 0 };
}

/** Свои жанры — первыми, порядок внутри них — по прослушиваниям убывания. */
function stOrderTiles(tiles, mineIds) {
  const rank = new Map();
  (tiles || []).forEach((s, i) => rank.set(s.id, i));
  return (tiles || []).slice().sort((a, b) => {
    const ma = mineIds.has(a.id) ? 1 : 0, mb = mineIds.has(b.id) ? 1 : 0;
    return (mb - ma) || (mineIds.plays(b.id) - mineIds.plays(a.id)) || (rank.get(a.id) - rank.get(b.id));
  });
}

/** Подпись текущего характера эфира — по таблице ST_KNOBS, а не склейкой ключа
 *  из переменной: динамический `t('st.div_' + x)` не проверяется ничем. */
function stDivLabel(v) {
  const g = ST_KNOBS.filter(x => x.key === 'diversity')[0];
  const hit = g && g.values.filter(x => x[0] === v)[0];
  return hit ? t(hit[1]) : '';
}

function stHeroHtml(mine) {
  const div = stDivLabel(_stKnobs.diversity);
  const tail = mine.artist
    ? t('st.hero_from_artist').replace('{0}', mine.name)
    : t('st.hero_from').replace('{0}', mine.name);
  const act = mine.artist ? 'stPlayArtist(' + escJ2(mine.artist) + ')' : 'stPlay(' + escJ2(mine.sid) + ')';
  const seed = mine.sid || mine.name;
  return `<div class="st-hero" data-st="${esc(mine.sid)}">
    <button class="st-hero-go" onclick="${act}"
            title="${esc(t('st.hero_play').replace('{0}', mine.name))}"
            aria-label="${esc(t('st.hero_play').replace('{0}', mine.name))}">
      <span class="st-hero-art" style="background:${stGradient(seed, .9, .55)}">
        <span class="st-bigplay" style="background:${stGradient(seed, 1, .8)}">&#9654;</span>
      </span>
      <span class="st-hero-txt">
        <span class="st-hero-t">${esc(t('st.hero_title'))}</span>
        <span class="st-hero-sub">${esc(div)} · ${esc(tail)}</span>
      </span>
    </button>
    <button class="st-hero-cfg" onclick="stToggleWaveCfg()" aria-expanded="false"
            title="${esc(t('st.hero_cfg_t'))}">${esc(t('st.hero_cfg'))}</button>
    <div class="st-hero-panel" id="st-hero-panel" hidden>
      <div class="st-knobs st-knobs-in" id="st-knobs-hero"></div>
    </div>
  </div>`;
}

function stToggleWaveCfg(open) {
  const p = document.getElementById('st-hero-panel');
  if (!p) return;
  const show = (open === undefined) ? p.hidden : !!open;
  p.hidden = !show;
  const btn = document.querySelector('.st-hero-cfg');
  if (btn) {
    btn.setAttribute('aria-expanded', show ? 'true' : 'false');
    btn.classList.toggle('on', show);
  }
  if (show) stRenderKnobs();
}

/** Круглая карточка артиста: лицо из кэша, а если его нет — первые буквы. */
function stArtistCard(a) {
  const tip = t('st.play_artist') + ' — ' +
    t('st.chip_counts').replace('{0}', a.plays || 0).replace('{1}', a.downloads || 0);
  const name = String(a.name || '');
  const initials = name.split(/\s+/).filter(Boolean).slice(0, 2)
    .map(w => w[0]).join('').toUpperCase() || '?';
  const face = a.pic
    ? `<img src="${esc(a.pic)}" alt="" loading="lazy" decoding="async"
             onerror="this.closest('.st-artcard').classList.add('st-art-empty')">`
    : '';
  return `<button class="st-artcard${a.pic ? '' : ' st-art-empty'}"
                   onclick="stPlayArtist(${escJ2(name)})" title="${esc(tip)}"
                   aria-label="${esc(tip)}">
    <span class="st-art" style="background:${stGradient(name, .55, .3)}">
      ${face}<span class="st-art-i">${esc(initials)}</span>
    </span>
    <span class="st-art-name">${esc(name)}</span>
  </button>`;
}

function stSection(title, hint, inner) {
  return `<div class="st-sec">
    <div class="st-sec-h">
      <span class="st-sec-t">${esc(title)}</span>
      ${hint ? `<span class="st-sec-hint">${esc(hint)}</span>` : ''}
    </div>
    <div class="st-sec-b">${inner}</div>
  </div>`;
}

/** Кавычки для onclick — отдельно от escJ, чтобы не зависеть от чужого файла. */
function escJ2(s) {
  // Два слоя, как у escJ: результат стоит внутри onclick="…", и `"` в имени
  // артиста закрывал атрибут (23.09.2026) — сначала HTML-сущности, потом JS.
  return "'" + String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;')
    .replace(/\\/g, '\\\\').replace(/'/g, "\\'").replace(/\r?\n/g, ' ') + "'";
}

/* ══ СЕССИЯ: ПАЧКИ, ДОЗАКАЗ, РУЧКИ ═════════════════════════════════════════
 *
 * Станция больше НЕ запрашивается одним выстрелом. Сервер держит эфир в
 * `station_sessions.py`: открываем сессию — это ЕДИНСТВЕННЫЙ запрос, который
 * ходит в витрины и MusicBrainz (секунды), — получаем короткую пачку, а когда
 * очередь тает, просим следующую. `next` по построению не трогает сеть и
 * приходит за миллисекунды, поэтому начинаем его ЗА ТРИ трека, а не когда
 * колонки уже молчат. Так эфир перестаёт кончаться на тридцатом треке и
 * начинает учитывать то, что человек сказал внутри этих тридцати.
 *
 * Запас (`reserve_min`) и размер пачки читаются с сервера, а не заводятся
 * здесь константой: решение «когда дозаказывать» принимает тот, кто держит
 * пул.
 *
 * СОБЫТИЯ уезжают не по одному, а ТЕЛОМ запроса `next` (см. `_stevTakePending`
 * в player.js) — ровно как `feedbacks` у Яндекса: сервер пересобирает остаток
 * пула уже зная, что человек сделал с прошлой пачкой.
 */
const ST_BATCH = 10;

/*
 * ИГРАЕМОСТЬ — честный ответ на «треть выдачи выбрасывается в браузере».
 *
 * Плеер стримит ровно три сервиса: на сервере есть /api/stream/qobuz|tidal|
 * deezer/{id}. У строк apple и yandex такого пути нет вовсе, soundcloud есть,
 * но это то, чем станцию лучше НЕ кормить: чарт приносит часовые DJ-сеты под
 * «артистом»-лейблом (docs/STATIONS_GAP.md, Р2), а Apple отдаёт 30-секундное
 * превью. И то и другое отравило бы ровно тот сигнал глубины, который мы
 * сейчас чиним: превью считалось бы скипом на 15% длины, сет — часовым
 * прослушиванием чужого имени.
 *
 * Поэтому ВЫБРАНО НЕ «починить играбельность», а «перестать платить за то, что
 * не звучит»: фронт СКАЗЫВАЕТ серверу, какие сервисы он способен сыграть
 * (`services` в теле открытия сессии), и пул собирается только из них. Раньше
 * браузер молча выбрасывал эти строки ПОСЛЕ того, как сервер заплатил за них
 * сетевыми запросами и сверкой жанра, а `reserve` врал: считал то, что никогда
 * не заиграет, и дозаказ начинался позже, чем надо.
 *
 * Фильтр ниже оставлен как страховка — и он НЕ молчит: если сервер всё-таки
 * пришлёт неигрубельное, человек это увидит.
 */
const ST_PLAYABLE = ['deezer', 'qobuz', 'tidal'];

const ST_KNOB_NEUTRAL = { diversity: 'default', energy: 'all', language: 'any' };

/** Значения — СЕРВЕРНЫЕ (`stations.DIVERSITIES/ENERGIES/LANGUAGES`): ручка с
 *  набором значений, которых бэкенд не знает, это та самая подмена. */
const ST_KNOBS = [
  { key: 'diversity', title: 'st.knob_diversity', values: [
    ['favorite', 'st.div_favorite'], ['default', 'st.div_default'],
    ['discover', 'st.div_discover'], ['popular', 'st.div_popular']] },
  { key: 'energy', title: 'st.knob_energy', values: [
    ['calm', 'st.en_calm'], ['all', 'st.en_all'], ['active', 'st.en_active']] },
  { key: 'language', title: 'st.knob_language', values: [
    ['any', 'st.lg_any'], ['russian', 'st.lg_russian'],
    ['not-russian', 'st.lg_not_russian']] },
];

let _stSess = null;         // живой эфир: {id, stationId, label, title, batchId, …}
let _stReport = null;       // последний `knob_report` сервера
let _stKnobs = stKnobLoad();

function stKnobLoad() {
  const out = { diversity: 'default', energy: 'all', language: 'any' };
  try {
    const raw = JSON.parse(localStorage.getItem('st.knobs') || '{}');
    Object.keys(out).forEach(k => { if (typeof raw[k] === 'string') out[k] = raw[k]; });
  } catch (_) {}
  return out;
}

function stKnobSave() {
  try { localStorage.setItem('st.knobs', JSON.stringify(_stKnobs)); } catch (_) {}
}

function stUuid() {
  try { if (crypto.randomUUID) return crypto.randomUUID(); } catch (_) {}
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c => {
    const r = Math.random() * 16 | 0;
    return (c === 'x' ? r : (r & 3) | 8).toString(16);
  });
}

function stStatus(msg) {
  const b = document.getElementById('st-status');
  if (b) b.textContent = msg;
}

/** Сколько треков осталось до конца очереди, считая играющий. */
function stRemaining() {
  if (typeof Preview === 'undefined' || !Preview.queue) return 0;
  return Preview.queue.length - 1 - Preview.idx;
}

/** Станция играет и очередь всё ещё наша (чужой запуск снимает метку в
 *  player.js — по идентичности массива). */
function stSessionLive() {
  return !!(_stSess && typeof _StEv !== 'undefined' && _StEv.session === _stSess.id);
}

function stCard(x) {
  return {
    url: '', service: x.service, id: String(x.id),
    title: x.title || '', artist: x.artist || '', cover: x.cover || '', full: true,
    // Длительность с сервера: события прослушивания берут length_s отсюда,
    // а не из того, что в этот момент стоит в <audio>.
    duration: Number(x.duration) || 0,
    // Ссылка на карточку сервиса — она нужна кнопке «скачать» (постановка в
    // очередь понимает только ссылку). В `url` её класть нельзя: туда плеер
    // ждёт аудиопуть, и адрес сайта играл бы тишиной.
    dl: x.url || '',
  };
}

/** Оставить только то, что этот плеер физически способен сыграть. */
function stPlayable(tracks) {
  const items = [], dropped = {};
  (tracks || []).forEach(x => {
    const svc = String(x.service || '').toLowerCase();
    if (x.id && ST_PLAYABLE.indexOf(svc) >= 0) items.push(stCard(x));
    else dropped[svc || '?'] = (dropped[svc || '?'] || 0) + 1;
  });
  return { items: items, dropped: dropped };
}

/** Неигрубельные строки НЕ исчезают молча: раньше треть выдачи просто
 *  проходила мимо `r.tracks.length`, и человек не мог отличить «столько и
 *  собрали» от «столько нашли, остальное нечем играть». */
function stTellDropped(dropped) {
  const names = Object.keys(dropped || {});
  if (!names.length) return;
  const n = names.reduce((a, k) => a + dropped[k], 0);
  stStatus(ti('st.dropped', { n: n, svc: names.slice(0, 3).join(', ') }));
}

function stShowLive(extra) {
  const s = _stSess;
  if (!s) return;
  const bits = [t('st.playing').replace('{0}', s.title || s.label),
                ti('st.in_queue', { n: Math.max(0, stRemaining() + 1) }),
                ti('st.reserve', { n: s.reserve })];
  if (extra) bits.push(extra);
  stStatus(bits.join('  ·  '));
}

// Мёртвый источник обязан назвать причину, а не молчаливый ноль (претензия
// 23.09.2026). Сервер кладёт в `sources` вместо числа i18n-ключ причины.
const ST_SRC_LABEL = { 'soundcloud:chart': 'SoundCloud', deezer: 'Deezer',
                       qobuz: 'Qobuz', tidal: 'Tidal', apple: 'Apple', yandex: 'Yandex' };
function stSourceNotes(sources) {
  const bits = [];
  Object.keys(sources || {}).forEach(k => {
    const v = sources[k];
    if (typeof v === 'string') bits.push((ST_SRC_LABEL[k] || k) + ': ' + t(v));
  });
  return bits.length ? ti('st.src_notes', { list: bits.join(' · ') }) : '';
}

/**
 * Открыть сессию. Вся сеть — здесь; `next` ниже её не трогает.
 */
function stSessionRequest(id) {
  return api('POST', '/api/station/session',
             { id: id, batch: ST_BATCH, knobs: _stKnobs, services: ST_PLAYABLE,
               seed: Date.now() & 0xffff });
}

async function stPlay(id) {
  const tile = (((_stHome || {}).tiles) || []).filter(x => x.id === id)[0];
  const label = (tile && tile.title) || id;
  stStatus(t('st.building').replace('{0}', label));
  let r = null;
  try { r = await stSessionRequest(id); } catch (e) {
    stStatus(t('st.load_failed') + ': ' + String(e)); return;
  }
  if (!r || !r.ok || !(r.tracks || []).length) {
    stShowEmpty(r);
    _stSess = null; stRenderKnobs();
    return;
  }
  const got = stPlayable(r.tracks);
  if (!got.items.length) {
    stStatus(t('st.nothing_playable')); stTellDropped(got.dropped); return;
  }
  if (typeof _setupAudioEvents !== 'function' || typeof _playPreviewAt !== 'function') return;
  // НОВЫЙ массив = новый эфир: плеер по идентичности отличает станцию от
  // любого другого запуска (`_stevActive` в player.js).
  Preview.queue = got.items;
  Preview.idx = 0;
  _stSess = { id: r.session_id, stationId: r.station_id || id, label: label,
              title: r.title || label, batchId: r.batch_id || '', seq: r.sequence || 1,
              reserve: Number(r.reserve) || 0, reserveMin: Number(r.reserve_min) || 3,
              suggest: r.suggest || 'next_batch', exhausted: !!r.exhausted, waiting: null };
  _stReport = r.knob_report || null;
  _setupAudioEvents();
  if (typeof _stevStationStart === 'function') {
    try { _stevStationStart(label, r.session_id, r.batch_id); } catch (_) {}
  }
  stRenderKnobs();
  stShowLive(stSourceNotes(r.sources));
  _playPreviewAt(0);
  if (got.dropped && Object.keys(got.dropped).length) stTellDropped(got.dropped);
}

/** Пустой эфир обязан СКАЗАТЬ почему — не «проверьте связь» и не молчание. */
function stShowEmpty(r) {
  const note = stSourceNotes(r && r.sources);
  stStatus(note ? ti('st.empty_reason', { reason: note }) : t('st.empty'));
}

/**
 * Принять пачку: доклеить к ТОМУ ЖЕ массиву очереди и перевести учёт на неё.
 * Новый массив здесь обнулил бы весь учёт эфира на середине вечера.
 */
function stAdopt(r, fresh) {
  const s = _stSess;
  if (!s) return 0;
  const got = stPlayable(r.tracks);
  if (got.items.length) Array.prototype.push.apply(Preview.queue, got.items);
  s.id = r.session_id || s.id;
  s.batchId = r.batch_id || '';
  s.seq = r.sequence || (s.seq + 1);
  s.reserve = Number(r.reserve) || 0;
  if (r.reserve_min) s.reserveMin = Number(r.reserve_min);
  s.suggest = r.suggest || 'next_batch';
  s.exhausted = !!r.exhausted;
  if (r.knob_report) { _stReport = r.knob_report; stRenderKnobs(); }
  if (fresh && typeof _stevStationStart === 'function') {
    // Ротация: старая сессия закрывается (и сливает свои события в базу),
    // новая начинается тем же эфиром — ЗВУК НЕ ПРЕРЫВАЕТСЯ.
    try { _stevStationStart(s.label, s.id, s.batchId); } catch (_) {}
  } else if (typeof _stevBatch === 'function') {
    try { _stevBatch(s.batchId); } catch (_) {}
  }
  stShowLive(got.dropped && Object.keys(got.dropped).length
    ? ti('st.dropped_short', { n: Object.keys(got.dropped).reduce((a, k) => a + got.dropped[k], 0) })
    : (r.repeats ? ti('st.repeats', { n: r.repeats }) : ''));
  return got.items.length;
}

/** Осталось ≤ резерва — просим пачку, НЕ блокируя игру. */
function stRefillIfLow() {
  const s = _stSess;
  if (!s || s.waiting || !stSessionLive()) return;
  if (stRemaining() > s.reserveMin) return;
  stRefillNow();
}

/** Пачка прямо сейчас. Возвращает обещание с числом добавленных треков — его
 *  ждёт плеер, когда очередь дошла до края (тишина в колонках дороже). */
function stRefillNow() {
  const s = _stSess;
  if (!s) return Promise.resolve(0);
  if (s.waiting) return s.waiting;
  // Сервер сам сказал, что резерв кончился, — это не «следующая пачка», а
  // новая волна: открываем новую сессию и доклеиваем её сюда же.
  const p = (s.suggest === 'new_session' || s.exhausted) ? stRotate() : stNextBatch();
  s.waiting = p;
  p.then(() => { if (s.waiting === p) s.waiting = null; },
         () => { if (s.waiting === p) s.waiting = null; });
  return p;
}

async function stNextBatch() {
  const s = _stSess;
  if (!s) return 0;
  const events = (typeof _stevTakePending === 'function') ? _stevTakePending() : [];
  let r = null;
  try {
    r = await api('POST', '/api/station/session/' + encodeURIComponent(s.id) + '/next',
                  { events: events, knobs: _stKnobs, batch: ST_BATCH });
  } catch (e) {
    // Сеть подвела — события возвращаются в буфер и уедут с следующей
    // попыткой либо при остановке станции. Молча их потерять = потерять вечер.
    if (typeof _stevReturnPending === 'function') _stevReturnPending(events);
    return 0;
  }
  if (!r) return 0;
  if (r.stale) {
    // Состояние сервера умерло (перезапуск, получасовой простой). Эфир это
    // чинит САМ, тихо: новая сессия доклеивается к играющему. Ошибка на
    // экране посреди музыки — тот самый случай, когда исправное состояние
    // дороже оборванного эфира.
    if (typeof _stevReturnPending === 'function') _stevReturnPending(events);
    return stRotate();
  }
  if (typeof _stevReturnPending === 'function' && !(r.ok && (r.tracks || []).length)) {
    _stevReturnPending(events);          // пачки нет — фидбек ещё не принят
  }
  if (!r.ok || !(r.tracks || []).length) {
    s.exhausted = true; s.suggest = 'new_session';
    return 0;
  }
  return stAdopt(r, false);
}

/** Тихая ротация: новый эфир того же жанра доклеивается к текущему. */
async function stRotate() {
  const s = _stSess;
  if (!s) return 0;
  let r = null;
  try { r = await stSessionRequest(s.stationId); } catch (e) { return 0; }
  if (!r || !r.ok || !(r.tracks || []).length) { s.exhausted = true; return 0; }
  r.session_id = r.session_id || s.id;
  return stAdopt(r, true);
}

/* ── РУЧКИ ЭФИРА ───────────────────────────────────────────────────────────
 *
 * Настроение, разнообразие, язык. Правило одно: контрол, который ничего не
 * меняет, не имеет права выглядеть рабочим. Что каждая ручка делает с ЭТИМ
 * эфиром, решает сервер — `knob_report` приходит с каждой пачкой, и у него есть
 * поле `effect` ("active" | "no-op") на каждое значение. Сегодня живая ровно
 * одна: `diversity`. `energy` и `language` — no-op ВСЕГДА, потому что признаков
 * нет: bpm/energy ни один станционный источник не отдаёт, язык трека определить
 * нечем. Обе ручки показываются ОТКЛЮЧЁННЫМИ с объяснением, а не рабочими
 * кнопками, которые молча делают ничего.
 */

/** эффект ЗНАЧЕНИЯ ручки по докладу сервера; «active», «no-op» или '' (не
 *  известно — до первой сессии доклада нет). */
function stKnobEffect(key, value) {
  const rep = _stReport && _stReport[key];
  if (!rep) return '';
  const vals = rep.values || [];
  for (let i = 0; i < vals.length; i++) {
    if (vals[i].value === value) return vals[i].effect || '';
  }
  // Сервер отчитывается только про текущее значение: спрашивают его — верим
  // докладу; про любое другое честнее сказать «не знаю», чем выдумать.
  return value === rep.value ? (rep.effect || '') : '';
}

/** Мёртвая ручка — та, у которой НЕ РАБОТАЕТ ВООБЩЕ НИ ОДНО значение. */
function stKnobDead(key) {
  const spec = ST_KNOBS.filter(g => g.key === key)[0];
  if (!spec) return true;
  const known = spec.values.map(v => stKnobEffect(key, v[0])).filter(Boolean);
  if (!known.length) return key !== 'diversity';   // доклада нет: жива только та,
                                                   // которую сервер объявил сам
  return known.every(e => e !== 'active');
}

function stKnobNote(key) {
  if (stKnobDead(key)) return t('st.knob_dead_' + key);
  if (!stSessionLive()) return t('st.knob_next_station');
  return key === 'diversity' ? t('st.knob_applies_tail') : '';
}

function stRenderKnobs() {
  // Дом ручки — один: панель «⚙ Настроить» внутри «моей волны», а если карточки
  // нет (своей основы нет вовсе) — прежняя полоса под заголовком. Двух
  // одинаковых наборов кнопок на экране не бывает: пустой host обнуляем.
  const hero = document.getElementById('st-knobs-hero');
  const box = hero || document.getElementById('st-knobs');
  if (!box) return;
  const other = hero ? document.getElementById('st-knobs') : null;
  if (other) other.innerHTML = '';
  // Владелец 25.09: зачёркнутые «Настроение»/«Язык» с «Не работает» — мусор
  // сверху экрана. Мёртвая ручка (ни одно значение не действует) не
  // рисуется совсем; оживёт на сервере — появится сама.
  box.innerHTML = ST_KNOBS.filter(g => !stKnobDead(g.key)).map(g => {
    const dead = false;
    const chips = g.values.map(v => {
      const value = v[0], on = _stKnobs[g.key] === value;
      const noop = !dead && stKnobEffect(g.key, value) === 'no-op';
      const cls = 'st-knob-chip' + (on ? ' on' : '') + (noop ? ' noop' : '');
      const tip = dead ? t('st.knob_dead_' + g.key) : (noop ? t('st.knob_noop') : t(v[1]));
      return `<button class="${cls}"${dead ? ' disabled' : ''} title="${esc(tip)}"
                onclick="stSetKnob('${g.key}','${value}')">${esc(t(v[1]))}</button>`;
    }).join('');
    const note = stKnobNote(g.key);
    return `<div class="st-knob${dead ? ' st-knob-dead' : ''}">
      <span class="st-knob-t">${esc(t(g.title))}</span>
      <span class="st-knob-chips">${chips}</span>
      ${note ? `<span class="st-knob-note">${esc(note)}</span>` : ''}
    </div>`;
  }).join('');
}

async function stSetKnob(key, value) {
  if (stKnobDead(key)) return;                  // мёртвую ручку не сделать живой
  _stKnobs = Object.assign({}, _stKnobs);
  _stKnobs[key] = value;
  stKnobSave();
  stRenderKnobs();
  const s = _stSess;
  if (!s || !stSessionLive()) return;           // применится при следующем открытии
  let r = null;
  try {
    r = await api('POST', '/api/station/session/' + encodeURIComponent(s.id) + '/knobs',
                  { knobs: _stKnobs });
  } catch (e) { return; }
  if (!r || r.stale) return;                    // эфир починит это сам на дозаказе
  if (r.knob_report) _stReport = r.knob_report;
  if (typeof r.reserve === 'number') s.reserve = r.reserve;
  if (r.suggest) s.suggest = r.suggest;
  stRenderKnobs();
  stShowLive(t('st.knob_reordered'));
}

// Смена языка: панель ручек собирается в JS, а `applyLang()` перерисовывает
// только узлы с data-i18n — поэтому следим за признаком языка (атрибут `lang на
// <html>`, который ставит он же) и перерисовываем саму панель. Тот же приём, что
// у динамических панелей плавающего плеера.
(function () {
  if (typeof MutationObserver !== 'function' || typeof document === 'undefined') return;
  try {
    new MutationObserver(function () { stRenderKnobs(); })
      .observe(document.documentElement, { attributes: true, attributeFilter: ['lang'] });
  } catch (_) {}
})();

function stPlayArtist(name) {
  if (!name) return;
  stRun(`/api/station/artist?name=${encodeURIComponent(name)}&limit=25&seed=${Date.now() & 0xffff}`,
        name);
}

/**
 * Эфир ОДНОЙ пачкой — так живут станции вокруг артиста: сессия серверу
 * нужна жанровая (`/api/station/session` берёт id плитки), и для «вокруг
 * человека» её нет. События при этом пишутся честно: у своего эфира есть id,
 * которого хватает и на бан трека, и на долгий профиль; перестройка внутри
 * эфира для одной пачки просто негде жить.
 */
async function stRun(url, label) {
  stStatus(t('st.building').replace('{0}', label));
  let r = null;
  try { r = await api('GET', url); } catch (e) {
    stStatus(t('st.load_failed') + ': ' + String(e)); return;
  }
  if (!r || !r.ok || !(r.tracks || []).length) { stShowEmpty(r); return; }
  const got = stPlayable(r.tracks);
  if (!got.items.length) { stStatus(t('st.nothing_playable')); stTellDropped(got.dropped); return; }
  if (typeof _setupAudioEvents !== 'function' || typeof _playPreviewAt !== 'function') return;
  Preview.queue = got.items;
  Preview.idx = 0;
  _stSess = null;                     // не сессия: дозаказа у этого эфира нет
  _setupAudioEvents();
  if (typeof _stevStationStart === 'function') {
    try { _stevStationStart(label, stUuid(), ''); } catch (_) {}
  }
  const from = r.from_artists ? '  ·  ' + t('st.from_artists').replace('{0}', r.from_artists) : '';
  stStatus(`${t('st.playing').replace('{0}', r.title || label)}  ·  ${got.items.length}${from}`);
  _playPreviewAt(0);
  if (Object.keys(got.dropped).length) stTellDropped(got.dropped);
}
