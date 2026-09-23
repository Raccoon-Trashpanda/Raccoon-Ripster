// ======================================================================
// Цвет обложки — на карточку.
//
// ЗАЧЕМ. Сетка релизов сейчас читается как таблица: все карточки одинаковой
// серой рамкой, различает их только картинка. Взяв цвет из самой обложки, мы
// не «раскрашиваем», а ДОБАВЛЯЕМ ту же информацию вторым каналом: издание с
// красной обложкой и опознаётся красным. В поиске тем же цветом окрашивается
// панель — так видно, чей результат сейчас на экране.
//
// КАК СЧИТАЕТСЯ. Не «средний цвет» — усреднение всегда даёт грязно-серое.
// Но и не «самый частый НАСЫЩЕННЫЙ тон»: прежняя версия до голосования выбрасывала
// всё с s < 0.22, и на обложке с большим приглушённым полем (бирюзовый фон и
// лососевые пятна, 21.09.2026) это выкинуло 75% картинки — тихое, но главное по
// площади поле не могло набрать голосов, и победила мелкая крикливая деталь.
// Теперь голосует КАЖДЫЙ пиксель с весом √насыщенность: вес сублинеен, поэтому
// площадь поля значит больше, чем крикливость пятна; отсекаются только почти
// чёрное и почти белое (тон там — шум), но не «серое». Корзины по 12°, однако
// побеждает не корзина, а СВЯЗНАЯ ОБЛАСТЬ круга тонов: соседняя корзина,
// набравшая не меньше трети предыдущей, считается частью того же поля —
// плавный градиент не должен раскалывать одно поле на две полупустые корзины.
//
// ПОРОГ «ЦВЕТА НЕТ» — ПО ХРОМЕ C*, А НЕ ПО HSL-S. Прежний пол (s < 0.10 или
// s×доля < 0.045) перевёрнутыми дал обе ошибки сразу: чёрно-белому «Back in
// Black» HSL-насыщенность раздувает в тенях (у тёмного JPEG-зерна d делится на
// mx+mn ≈ 0.1 → s ≈ 0.13 при реальной краске нулевой), и обложка получала
// выдуманный синий тон; а приглушённому небу «Wish You Were Here» не хватало
// как раз произведения s×площадь, и оно теряло цвет вовсе. C* = √(a*²+b*²) в
// Lab — величина «сколько в пикселе цвета» для глаза, от светлоты не раздувается:
// у зерна ч/б фото после усреднения в 24×24 она 2–3, у тихого цветного — 6+.
// Пол 4.5 — середина между 2.6 (Back in Black) и 8.0 (Wish You Were Here).
//
// ЦВЕТ ОДИН, КРАСОК ДВА. Измерение одно, а применения разные, и одним числом их
// не обслуживают (21.09.2026 лососевое кольцо вокруг карточки объяснилось именно
// этим): АКЦЕНТ — тонкая полоса над панелями и кольцо под курсором, он рядом с
// текстом и обязан читаться; СВЕЧЕНИЕ — кольцо и ореол карточки, оно должно быть
// намёком на обложку, а не маркером. Светлоту акцента по-прежнему ведёт коридор
// темы, но насыщенность акцента только ОБРЕЗАЕТСЯ сверху — тусклая обложка даёт
// тусклый акцент, прежнему «поднять до 0.34» конец. Свечение режется сильнее и
// тянется светлотой к фону темы, чтобы растворяться, а не звенеть.
//
// ЦЕНА. Считаем один раз на обложку, в оффскрин-канве 24×24 (это ~576 пикселей,
// доли миллисекунды), результат кладём в память и в localStorage — при
// следующем открытии радара сеть и канва уже не нужны. Храним СЫРОЕ измерение
// {h,s,l}, а цвета вычисляем в момент покраски: коридоры зависят от темы, и
// переключение темы не должно требовать переснятия обложек. Картинка для замера
// берётся ОТДЕЛЬНАЯ, с crossOrigin — видимая <img> не трогается вообще, чтобы
// нельзя было сломать показ обложек. Витрина без CORS просто не даст цвет,
// и это нормально: карточка останется обычной.
// ======================================================================

const _CT_KEY = 'ripster_cover_tints3';   // v3: порог «нет цвета» меряется хромой C*; словарь с прежними решениями невалиден
const _CT_MAX = 600;                  // храним последние — словарь не должен пухнуть
const _ctMem = new Map();
let _ctDisk = null;

function _ctLoad() {
  if (_ctDisk) return _ctDisk;
  try { _ctDisk = JSON.parse(localStorage.getItem(_CT_KEY) || '{}'); }
  catch (e) { _ctDisk = {}; }
  return _ctDisk;
}

let _ctSaveTimer = null;
function _ctSave() {
  clearTimeout(_ctSaveTimer);
  _ctSaveTimer = setTimeout(() => {
    try {
      const keys = Object.keys(_ctDisk || {});
      if (keys.length > _CT_MAX) {
        const drop = keys.slice(0, keys.length - _CT_MAX);
        drop.forEach(k => delete _ctDisk[k]);
      }
      localStorage.setItem(_CT_KEY, JSON.stringify(_ctDisk || {}));
    } catch (e) { /* переполнено — обойдёмся памятью */ }
  }, 800);
}

// rgb → hsl, нужен и для отбора «насыщенных», и для коридора читаемости.
function _ctRgbToHsl(r, g, b) {
  r /= 255; g /= 255; b /= 255;
  const mx = Math.max(r, g, b), mn = Math.min(r, g, b);
  const l = (mx + mn) / 2;
  let h = 0, s = 0;
  if (mx !== mn) {
    const d = mx - mn;
    s = l > 0.5 ? d / (2 - mx - mn) : d / (mx + mn);
    if (mx === r)      h = ((g - b) / d + (g < b ? 6 : 0));
    else if (mx === g) h = ((b - r) / d + 2);
    else               h = ((r - g) / d + 4);
    h /= 6;
  }
  return [h * 360, s, l];
}

// Хрома C* = √(a*²+b*²) в Lab: «сколько пикселю цвета» по-человечески.
// В отличие от HSL-s, не раздувается тёмным шумом (там s = d/(mx+mn) растёт
// при d→0 вместе с mx+mn→0) и не тонет на приглушённом светлом поле.
function _ctChroma(r, g, b) {
  const lin = v => { v /= 255; return v <= 0.04045 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
  const R = lin(r), G = lin(g), B = lin(b);
  const X = (0.4124 * R + 0.3576 * G + 0.1805 * B) / 0.95047;
  const Y = 0.2126 * R + 0.7152 * G + 0.0722 * B;
  const Z = (0.0193 * R + 0.1192 * G + 0.9505 * B) / 1.08883;
  const f = t => t > 0.008856 ? Math.cbrt(t) : 7.787 * t + 16 / 116;
  const fx = f(X), fy = f(Y), fz = f(Z);
  const a = 500 * (fx - fy), bb = 200 * (fy - fz);
  return Math.sqrt(a * a + bb * bb);
}

// Доминирующий тон обложки — голосование ОБЛАСТЕЙ тона: взвешенный вес пикселя
// √насыщенность, а победитель считается не по одной корзине, а по связному
// участку круга тонов. null — цвета нет (серая/монотонная обложка).
// Возвращает СЫРОЕ {h,s,l}.
function _ctDominant(img) {
  const N = 24, BINS = 30;   // корзины по 12°
  const c = document.createElement('canvas');
  c.width = c.height = N;
  const ctx = c.getContext('2d', { willReadFrequently: true });
  ctx.drawImage(img, 0, 0, N, N);
  let data;
  try { data = ctx.getImageData(0, 0, N, N).data; }
  catch (e) { return null; }        // витрина без CORS — канва «запачкана»
  const own = new Map();            // корзина → своя статистика пикселей
  const mass = new Float64Array(BINS);   // свои голоса, без соседей
  for (let i = 0; i < data.length; i += 4) {
    if (data[i + 3] < 128) continue;
    const r = data[i], g = data[i + 1], b = data[i + 2];
    const [h, s, l] = _ctRgbToHsl(r, g, b);
    // Почти чёрное и почти белое: тон там — шум округления, а не намерение.
    // Заметьте: «серое» по НАСЫЩЕННОСТИ сюда не попадает — оно голосует.
    if (l < 0.06 || l > 0.97) continue;
    const w = Math.sqrt(s);         // сублинейный вес: площадь важнее крикливости
    if (w === 0) continue;
    const k = Math.min(BINS - 1, Math.floor(h / (360 / BINS)));
    mass[k] += w;
    const cur = own.get(k) || { n: 0, cs: 0, sn: 0, s: 0, l: 0, c: 0 };
    cur.n++;
    cur.cs += Math.cos(h * Math.PI / 180);   // круговое среднее: 359° и 1° — один тон
    cur.sn += Math.sin(h * Math.PI / 180);
    cur.s += s; cur.l += l;
    cur.c += _ctChroma(r, g, b);
    own.set(k, cur);
  }
  // Плавный градиент одного поля (бирюза 144°+157°) раскалывается на две
  // корзины, и поодиночке они проигрывают компактному пятну. Поэтому за
  // победой смотрим на СВЯЗНУЮ область: от семени расползаемся по кругу тона
  // туда-сюда, пока сосед не меньше трети предыдущей корзины (цепочка
  // обрывается на провале — через чужой тон область не переползает).
  const rel = a => ((a % BINS) + BINS) % BINS;
  let bestSet = null, bestSum = -1;
  for (let seed = 0; seed < BINS; seed++) {
    if (mass[seed] <= 0) continue;
    const set = new Set([seed]);
    let sum = mass[seed];
    for (const dir of [1, -1]) {
      let k = seed;
      for (let g = 0; g < 5; g++) {         // максимум ±60° в каждую сторону
        const nxt = rel(k + dir);
        if (mass[nxt] >= 0.3 * mass[k]) { sum += mass[nxt]; set.add(nxt); k = nxt; }
        else break;
      }
    }
    if (sum > bestSum) { bestSum = sum; bestSet = set; }
  }
  if (!bestSet) return null;
  let n = 0, cs = 0, sn = 0, ss = 0, ll = 0, cc = 0;
  bestSet.forEach(k => {
    const o = own.get(k);
    if (!o) return;
    n += o.n; cs += o.cs; sn += o.sn; ss += o.s; ll += o.l; cc += o.c;
  });
  if (!n) return null;
  const meanS = ss / n, meanC = cc / n;
  // Серая обложка не должна выдумывать цвет, а приглушённая цветовая —
  // терять его: решает средняя хрома C* победившей области (см. шапку).
  if (meanC < 4.5) return null;
  let hm = Math.atan2(sn, cs) * 180 / Math.PI;
  if (hm < 0) hm += 360;
  return { h: hm, s: meanS, l: ll / n };
}

const _ctIsLight = () =>
  document.documentElement.matches('.light, [data-theme="light"], [data-theme="sepia"]');

// Два красителя из одного измерения. СЫРОЕ {h,s,l} на входе — чтобы переключение
// темы не требовало пересъёма обложек (значение считается здесь, на лету).
function _ctPair(t) {
  if (!t) return null;
  const light = _ctIsLight();
  // Акцент: полосу над панелью и кольцо под курсором видно и на чёрном, и на
  // бумаге; насыщенность ТОЛЬКО обрезаем сверху — тусклое остаётся тусклым.
  const sa = Math.min(t.s, 0.62);
  const la = light ? Math.min(0.50, Math.max(0.28, t.l))
                   : Math.min(0.66, Math.max(0.42, t.l));
  // Свечение: 60% насыщенности (пол, чтобы тихая бирюза не исчезла вовсе),
  // светлота на 55% тянется к фону темы — ореол растворяется вокруг карточки,
  // а не рисует вокруг неё тарелку.
  const sg = Math.max(0.10, Math.min(0.30, t.s * 0.60));
  const lg = light ? Math.min(0.92, Math.max(0.72, 0.45 * t.l + 0.55 * 0.92))
                   : Math.min(0.40, Math.max(0.20, 0.45 * t.l + 0.55 * 0.16));
  return {
    a: `hsl(${Math.round(t.h)} ${Math.round(sa * 100)}% ${Math.round(la * 100)}%)`,
    g: `hsl(${Math.round(t.h)} ${Math.round(sg * 100)}% ${Math.round(lg * 100)}%)`,
  };
}

// Сырой тон обложки {h,s,l}. Всегда промис; null — цвета нет и красить нечем.
// Именно его красим в карточки и панели: из {h,s,l} оба красителя (_ctPair)
// вычисляются в момент покраски, и смена темы не требует пересъёма обложек.
//
// `url` — КЛЮЧ словаря (адрес обложки как он есть), `measureUrl` — то, что
// реально качаем ради замера. Разделены намеренно: замер идёт по канве 24×24,
// ему хватает миниатюры, а ключ должен остаться прежним, иначе весь уже
// накопленный словарь цветов обнулится при первом же открытии.
function coverTintRaw(url, measureUrl) {
  if (!url) return Promise.resolve(null);
  if (_ctMem.has(url)) return Promise.resolve(_ctMem.get(url));
  const disk = _ctLoad();
  if (Object.prototype.hasOwnProperty.call(disk, url)) {
    _ctMem.set(url, disk[url]);
    return Promise.resolve(disk[url]);
  }
  return new Promise(resolve => {
    const img = new Image();
    img.crossOrigin = 'anonymous';    // без этого канва «пачкается» и цвет не прочесть
    img.decoding = 'async';
    const done = (val) => {
      _ctMem.set(url, val);
      disk[url] = val;
      _ctSave();
      resolve(val);
    };
    img.onload = () => {
      let t = null;
      try { t = _ctDominant(img); } catch (e) { t = null; }
      done(t);
    };
    img.onerror = () => done(null);   // нет CORS/картинки — карточка остаётся обычной
    img.src = measureUrl || url;
  });
}

// Строковый контракт (hsl-строка акцента) ЖИВ: им пользуется dlorb.js
// (`dominantTint` → parseHsl) для окраски круга загрузки. Строка — акцент,
// самый «читаемый» из двух красителей: круг загрузки рядом с текстом.
function coverTint(url, measureUrl) {
  return coverTintRaw(url, measureUrl).then(t => {
    const p = _ctPair(t);
    return p ? p.a : null;
  });
}

// Покрасить элемент обоими красителями (или снять покраску при t=null).
function _ctApply(el, t) {
  if (!el) return;
  const p = _ctPair(t);
  if (!p) { el.classList.remove('tinted'); el.style.removeProperty('--tint'); el.style.removeProperty('--tint-glow'); return; }
  el.style.setProperty('--tint', p.a);
  el.style.setProperty('--tint-glow', p.g);
  el.classList.add('tinted');
}

// ──────────────────────────────────────────────────────────────────────
// ПОЧЕМУ ЗДЕСЬ НАБЛЮДАТЕЛЬ И ОЧЕРЕДЬ, А НЕ ПРОСТОЙ ЦИКЛ.
//
// Функция называлась «покрасить ВИДИМЫЕ», а брала `.rel-card:not([data-tinted])`
// по всему документу — то есть всю отрисованную страницу радара целиком (120
// карточек за раз, а после «показать ещё» — 240, 360…). На каждую заводился
// ВТОРОЙ `new Image()` с `crossOrigin='anonymous'` и адресом `lightboxSrc`,
// то есть ОРИГИНАЛОМ обложки (640×640 у Spotify).
//
// Цена была двойная и обе половины били ровно туда, на что жаловался владелец:
//   • CORS-запрос — отдельная запись в кэше браузера: тот же файл качался ВТОРОЙ
//     раз, мимо кэша видимой <img>;
//   • на хост живёт ~6 одновременных соединений. 120 запросов по 640×640
//     занимали их все, и настоящие обложки карточек вставали в хвост очереди.
//     Отсюда «обложки грузятся медленно, а иногда не грузятся вовсе»: когда
//     человек пролистывал дальше, соединения всё ещё были заняты замерами
//     карточек, которые он давно проскроллил.
//
// Теперь: замер начинается только когда карточка подошла к экрану, качается
// МИНИАТЮРА (relCover(...,96) — 64×64 у Spotify, ~2 КБ вместо ~40 КБ) и не
// более трёх штук одновременно, чтобы соединения оставались настоящим обложкам.
// ──────────────────────────────────────────────────────────────────────
const _CT_PARALLEL = 3;
const _ctQueue   = [];
let   _ctRunning = 0;

function _ctSmall(src) {
  // relCover живёт в sc_tab.js, который грузится ПОЗЖЕ этого файла. Вызов
  // происходит в рантайме, когда всё уже на месте, но проверка обязательна:
  // публичная сборка может не содержать модуля вовсе.
  try { if (typeof relCover === 'function') return relCover(src, 96); } catch (e) {}
  return src;
}

function _ctPump() {
  while (_ctRunning < _CT_PARALLEL && _ctQueue.length) {
    const card = _ctQueue.shift();
    if (!card.isConnected) continue;
    const src = card.dataset.tintSrc;
    if (!src) continue;
    _ctRunning++;
    coverTintRaw(src, _ctSmall(src)).then(t => {
      _ctRunning--;
      if (t && card.isConnected) _ctApply(card, t);
      _ctPump();
    });
  }
}

const _ctIO = ('IntersectionObserver' in window)
  ? new IntersectionObserver(entries => {
      let queued = false;
      for (const e of entries) {
        if (!e.isIntersecting) continue;
        _ctIO.unobserve(e.target);
        _ctQueue.push(e.target);
        queued = true;
      }
      if (queued) _ctPump();
    }, { rootMargin: '250px 0px' })   // чуть раньше экрана, чтобы цвет успел
  : null;

// Поставить некрашеные карточки под наблюдение. Сам замер откладывается до
// момента, когда карточка подошла к экрану.
function tintVisibleCards(root) {
  const scope = root || document;
  // .bbc-card — сетка BBC Sounds: те же обложки, та же окантовка в тон,
  // что у карточек релизов.
  const cards = scope.querySelectorAll('.rel-card:not([data-tinted]),.bbc-card:not([data-tinted])');
  if (!cards.length) return;
  cards.forEach(card => {
    const img = card.querySelector('img');
    const src = img && (img.dataset.lightboxSrc || img.src);
    // data:-заглушка BBC (нет кадра ни у выпуска, ни у бренда) красить нечем:
    // это серый прямоугольник, а не обложка.
    if (!src || src.startsWith('data:')) { card.dataset.tinted = 'no'; return; }
    card.dataset.tinted  = '1';
    card.dataset.tintSrc = src;
    // Цвет уже посчитан раньше — красим сразу: сети здесь не будет вовсе,
    // откладывать до появления на экране нечего.
    const disk = _ctLoad();
    const cached = _ctMem.has(src) ? _ctMem.get(src)
                 : (Object.prototype.hasOwnProperty.call(disk, src) ? disk[src] : undefined);
    if (cached !== undefined) {
      if (cached) _ctApply(card, cached);
      return;
    }
    if (_ctIO) _ctIO.observe(card);
    else { _ctQueue.push(card); _ctPump(); }
  });
}

// Панель поиска красится цветом того, кого ищут: видно, чей результат на экране.
// Смена мягкая — за неё отвечает переход в CSS, здесь только значение.
function tintSearchPanel(coverUrl) {
  const host = document.getElementById('view-search') || document.querySelector('.view-search');
  if (!host) return;
  if (!coverUrl) { host.classList.remove('tinted'); host.style.removeProperty('--tint'); host.style.removeProperty('--tint-glow'); return; }
  coverTintRaw(coverUrl).then(t => _ctApply(host, t));
}

// Карточки досыпаются пачками («показать ещё») — красим и их, но не на каждый
// чих: одного прохода на кадр достаточно.
(function _ctWatch() {
  let pending = false;
  const kick = () => {
    if (pending) return;
    pending = true;
    requestAnimationFrame(() => { pending = false; tintVisibleCards(); });
  };
  const grids = () => [document.getElementById('releases-grid'),
                       document.getElementById('bbc-grid')];
  const obs = new MutationObserver(kick);
  const attach = () => {
    for (const g of grids()) {
      if (g && !g.dataset.tintWatch) {
        g.dataset.tintWatch = '1';
        obs.observe(g, { childList: true, subtree: false });
        kick();
      }
    }
  };
  document.addEventListener('click', e => {
    if (e.target && e.target.closest && e.target.closest('.nav-item')) setTimeout(attach, 400);
  }, true);
  setTimeout(attach, 1500);
  window.addEventListener('load', () => setTimeout(attach, 800));
})();

// Панель дискографии — в тон артиста. Тот же расчёт, что у карточек: цвет
// берётся из обложки и уходит только в окантовку, не в фон под текстом.
function tintDetailPanel(coverUrl) {
  const host = document.getElementById('sc-detail')
            || document.querySelector('.detail-panel, #detail-panel');
  if (!host) return;
  if (!coverUrl) { host.classList.remove('tinted'); host.style.removeProperty('--tint'); host.style.removeProperty('--tint-glow'); return; }
  coverTintRaw(coverUrl).then(t => _ctApply(host, t));
}
