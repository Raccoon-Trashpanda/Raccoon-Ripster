/* dlorb.js — круглый индикатор загрузки в ОТДЕЛЬНОМ ЭТАЖЕ левой панели.
 *
 * ЗАЧЕМ. Уйдя с вкладки задач, человек теряет прогресс из виду совсем: ни
 * процентов, ни шкалы больше нигде нет.
 *
 * ГДЕ СТОИТ. Панель разделена на два этажа: меню прокручивается в своей
 * области (.nav-scroll), а док с кругами и стек карточек занимают СВОЁ МЕСТО
 * в потоке под ним (.dlside-foot). Раньше док был прилеплен к низу прокрутки
 * (sticky), а карточки — вообще листом на <body>, и при низком окне оба
 * ложились на пункты меню: «Настройки», «Console», «Admin» и «Бот» читались
 * сквозь них (замечание владельца 24.09.2026). Теперь наезжать нечем: у
 * виджетов прямоугольник свой, и в него чужому контенту не попасть.
 *
 * ДВИЖЕНИЕ. Круг выкатывается из левого края, ПАРКУЕТСЯ У ПРАВОГО КРАЯ дока и
 * стоит, пока идёт загрузка; по завершении укатывается направо. На область
 * карточек не вылезает НИКОГДА — правая граница панели это стена, и держит её
 * CSS (.dlorb-dock{overflow:hidden;contain:layout paint}), а не аккуратность
 * арифметики: даже если расчёт X ошибётся, за прямоугольник ничего не выйдет.
 *
 * ПОЧЕМУ НЕ ПОСЕРЕДИНЕ (было так до 06.09.2026). Слева от круга появилось поле
 * метаданных, а при центральной парковке слева оставалось ~77px, из которых
 * стопка силуэтов съедала ещё 39px: в оставшиеся 30px не влезает ни одно
 * название. Круг сдвинут вправо, и левая часть дока отдана тексту.
 *
 * ПОЛЕ МЕТАДАННЫХ (слева от круга). Отвечает на «что качается прямо сейчас»
 * и живёт даже когда не качается ничего: тогда показывает ПОСЛЕДНЮЮ задачу из
 * очереди, а если и её нет — честную нейтральную строку. Ничего не додумывает:
 * нет названия — так и написано «название неизвестно», а не выдуманный текст.
 * Область отбора у поля ТА ЖЕ, что у круга (свои ручные), чтобы текст и круг
 * физически не могли говорить о разных задачах.
 *
 * ОДНА ЗАДАЧА — ОДИН ВИДЖЕТ. Поле метаданных показывает ТЕКУЩУЮ загрузку, а
 * карточки стека — только те, что стоят СЛЕДИ за ней. Раньше и карточка, и
 * поле одновременно говорили об одной и той же задаче, и на низком окне эти
 * два текста ещё и лежали друг на друге поверх меню.
 *
 * ОЧЕРЕДЬ. Цветной только тот круг, чья задача ТЕКУЩАЯ. Следующие стоят
 * стопкой позади СИЛУЭТАМИ — без цвета и оттенков. Силуэту обложка не нужна,
 * поэтому цвет считается только для текущей и предзагружается только для
 * следующей: ни одной лишней картинки. Видимых силуэтов не больше MAX_GHOSTS,
 * дальше «+N» — у владельца в радаре бывает больше двух тысяч релизов.
 *
 * ТОЛЬКО СВОИ РУЧНЫЕ. session_id === "" И source === "manual". Гостей много,
 * автоматика (watchlist/batch/retry) идёт постоянно — круг не должен молотить
 * на чужих и фоновых загрузках.
 *
 * КАНАЛ ДАННЫХ. Свой не заводим: app.js уже принимает WS-событие 'progress' и
 * кладёт значения в S.queue, оттуда и читаем (см. вызовы DLOrb.* в app.js).
 *
 * ЦВЕТ. Из обложки через canvas — а canvas на чужом домене падает на CORS.
 * Поэтому цвет ставится ДВУМЯ ходами: сразу честный запасной (FALLBACK), и
 * потом, если получилось, настоящий. Ни одно исключение отсюда не выходит.
 */
(function () {
  'use strict';

  const VARIANTS   = ['neon', 'aurora', 'vinyl', 'mono', 'ember', 'halo', 'pulse'];
  // Варианты отличаются в том числе СПОСОБОМ брать цвет: доминирующий тон
  // против среднего. Спор решается глазами, а не описанием.
  const COLOR_MODE = { neon: 'dominant', aurora: 'average', vinyl: 'average', mono: 'dominant',
                       ember: 'dominant', halo: 'average', pulse: 'dominant' };
  const DEF_VARIANT = 'neon';

  const MAX_GHOSTS = 3;      // потолок видимых силуэтов, дальше «+N»
  const MAX_CALLOUTS = 3;    // сколько выносных карточек показываем одновременно
  const ORB   = 56;          // диаметр круга, px (совпадает с main.css)
  const GAP   = 13;          // сдвиг силуэта в стопке
  const OFF_L = -92;         // стартовая точка выката (за левым краем)
  const OFF_R = 92;          // точка уката (за правым краем, но под overflow:hidden)
  const RAD   = 17.5;        // радиус кольца в системе viewBox 0 0 40 40
  const CIRC  = 2 * Math.PI * RAD;
  const EXIT_MS = 720;
  const DOCK_W_FALLBACK = 198;   // ширина панели 220 минус padding 2×10
  const PAD_R    = 22;       // отступ припаркованного круга от правого края дока:
                             // >= видимого радиуса света, чтобы ореол не срезался стеной (#9012)
  const META_GAP = 10;       // зазор между полем метаданных и стопкой кругов
  const META_MIN = 56;       // уже этого поле не сжимаем — читать станет нечего
  const CALL_VGAP = 6;       // вертикальный зазор между карточками в стеке (см. .dlcallouts{gap})
  const ORB_BAND = 132;      // высота дока с кругами = .dlorb-dock:has(.dlorb){height:132px}
  const NAV_RESERVE = 140;   // сколько высоты панели всегда остаётся меню:
                             // стек не должен съедать список — пункты и так уходят
                             // в свою прокрутку, но без видимых строк это бесполезно

  const ACTIVE   = { queued: 1, running: 1, pending: 1 };
  const TERMINAL = { done: 1, error: 1, cancelled: 1 };

  // Честный запасной цвет: обложки может не быть, она может не догрузиться,
  // и она почти всегда с чужого домена — canvas тогда «пачкается».
  const FALLBACK = mkCol(340, 0.52, 0.58);

  let host    = null;
  let footEl  = null;        // второй этаж панели (.dlside-foot): стек карточек + док
  let moreEl  = null;
  let metaEl  = null;        // { root, label, title, sub } — поле слева от круга
  let calloutHost = null;    // стек карточек: живёт В ЭТАЖЕ, прямоугольник = колонка панели
  let enabled = true;
  let variant = DEF_VARIANT;
  let overflow = 0;
  let queueProvider = null;

  const orbs = new Map();          // id → { el, skin, arc, pct, cnt, slot, exiting }
  const callouts = new Map();      // id → { el, title, artist, meta, pct, exiting }
  const colorCache = new Map();    // 'mode|url' → colour bag

  // ── мелкие утилиты, все с глушителями: индикатор не имеет права ронять UI ──

  function _t(key, params) {
    try {
      if (params && typeof ti === 'function') return ti(key, params);
      if (!params && typeof t === 'function') return t(key);
    } catch (_) {}
    return params && params.n != null ? '+' + params.n : '';
  }

  function el(tag, cls) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    return n;
  }

  function svgEl(tag) {
    return document.createElementNS('http://www.w3.org/2000/svg', tag);
  }

  function nextFrame(fn) {
    try {
      if (typeof requestAnimationFrame === 'function') { requestAnimationFrame(fn); return; }
    } catch (_) {}
    fn();
  }

  // ── цвет ────────────────────────────────────────────────────────────────

  function hsl(h, s, l) {
    return 'hsl(' + Math.round(h) + ' ' + Math.round(s * 100) + '% ' + Math.round(l * 100) + '%)';
  }

  // HSL → [r,g,b] 0..255. Нужен, чтобы собрать rgba() для света: color-mix()/
  // hsl(... / a) на старой WebView2 не тянутся, а rgba() — самый широкий консенсус.
  function hslToRgb(h, s, l) {
    h = ((h % 360) + 360) % 360 / 360;
    if (s <= 0) { const v = Math.round(l * 255); return [v, v, v]; }
    const q = l < 0.5 ? l * (1 + s) : l + s - l * s;
    const p = 2 * l - q;
    const f = (t) => {
      if (t < 0) t += 1; if (t > 1) t -= 1;
      if (t < 1 / 6) return p + (q - p) * 6 * t;
      if (t < 1 / 2) return q;
      if (t < 2 / 3) return p + (q - p) * (2 / 3 - t) * 6;
      return p;
    };
    return [Math.round(f(h + 1 / 3) * 255), Math.round(f(h) * 255), Math.round(f(h - 1 / 3) * 255)];
  }

  function rgba(h, s, l, a) {
    const c = hslToRgb(h, s, l);
    return 'rgba(' + c[0] + ',' + c[1] + ',' + c[2] + ',' + a + ')';
  }

  // Один тон → набор производных. Так варианты обходятся без color-mix(),
  // который в старой WebView2 просто не поддерживается и даёт прозрачное ничто.
  function mkCol(h, s, l) {
    const sat = Math.min(0.74, Math.max(0.32, s));
    const lig = Math.min(0.68, Math.max(0.44, l));
    const h2 = (h + 148) % 360;
    return {
      c:  hsl(h, sat, lig),
      lt: hsl(h, Math.min(0.9, sat + 0.12), Math.min(0.82, lig + 0.16)),
      dk: hsl(h, sat, Math.max(0.22, lig - 0.20)),
      c2: hsl(h2, sat, Math.min(0.72, lig + 0.06)),
      // Свет ( ореол ): ДВЕ стопы одного тона с сильной завязкой на прозрачность —
      // ядро поярче, середина глуше; наружу градиент в CSS доводит до transparent.
      glow:     rgba(h, sat, Math.min(0.72, lig + 0.08), 0.50),
      glowMid:  rgba(h, sat, lig, 0.22),
      glow2:    rgba(h2, sat, Math.min(0.72, lig + 0.08), 0.46),
      glow2Mid: rgba(h2, sat, lig, 0.20)
    };
  }

  function rgbToHsl(r, g, b) {
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

  // 'hsl(340 52% 58%)' → {h,s,l}. Нужен, чтобы принять готовую строку от
  // coverTint() из cover_tint.js и не считать доминирующий тон второй раз.
  function parseHsl(str) {
    if (typeof str !== 'string') return null;
    const m = str.match(/hsl\(\s*([\d.]+)[,\s]+([\d.]+)%[,\s]+([\d.]+)%/i);
    if (!m) return null;
    return { h: parseFloat(m[1]), s: parseFloat(m[2]) / 100, l: parseFloat(m[3]) / 100 };
  }

  // Средний цвет обложки — второй способ, намеренно НЕ такой, как доминирующий.
  // Всегда промис, никогда не отклоняется: нет canvas / нет CORS / нет картинки
  // → null, и наверху подставится запасной цвет.
  function averageTint(url) {
    return new Promise(function (resolve) {
      if (!url || typeof Image !== 'function' || typeof document === 'undefined') return resolve(null);
      let img;
      try { img = new Image(); } catch (_) { return resolve(null); }
      let done = false;
      const fin = function (v) { if (!done) { done = true; resolve(v); } };
      try { img.crossOrigin = 'anonymous'; img.decoding = 'async'; } catch (_) {}
      img.onerror = function () { fin(null); };
      img.onload = function () {
        try {
          const N = 16;
          const cv = document.createElement('canvas');
          cv.width = cv.height = N;
          const ctx = cv.getContext && cv.getContext('2d', { willReadFrequently: true });
          if (!ctx) return fin(null);
          ctx.drawImage(img, 0, 0, N, N);
          const d = ctx.getImageData(0, 0, N, N).data;   // чужой домен → SecurityError
          let r = 0, g = 0, b = 0, n = 0;
          for (let i = 0; i < d.length; i += 4) {
            if (d[i + 3] < 128) continue;
            r += d[i]; g += d[i + 1]; b += d[i + 2]; n++;
          }
          if (!n) return fin(null);
          const p = rgbToHsl(r / n, g / n, b / n);
          fin(mkCol(p[0], p[1], p[2]));
        } catch (_) { fin(null); }
      };
      try { img.src = url; } catch (_) { fin(null); }
      setTimeout(function () { fin(null); }, 6000);   // застрявшая картинка не держит круг серым вечно
    });
  }

  function dominantTint(url) {
    return new Promise(function (resolve) {
      if (!url || typeof coverTint !== 'function') return resolve(null);
      let p;
      try { p = coverTint(url); } catch (_) { return resolve(null); }
      if (!p || typeof p.then !== 'function') return resolve(null);
      p.then(function (str) {
        const h = parseHsl(str);
        resolve(h ? mkCol(h.h, h.s, h.l) : null);
      }, function () { resolve(null); });
    });
  }

  function resolveColor(url) {
    const mode = COLOR_MODE[variant] || 'dominant';
    const p = mode === 'average' ? averageTint(url) : dominantTint(url);
    return p.then(function (c) { return c || FALLBACK; }, function () { return FALLBACK; });
  }

  // ── чтение очереди ──────────────────────────────────────────────────────

  // «Задача этого человека, добавленная руками». Вынесено из isEligible отдельно,
  // потому что поле метаданных показывает и ЗАВЕРШЁННЫЕ задачи — а им статус из
  // ACTIVE по определению не подходит. Оба отбора обязаны совпадать по владельцу
  // и источнику, иначе текст и круг заговорят о разных задачах.
  function isMine(task) {
    if (!task) return false;
    if ((task.session_id || '') !== '') return false;      // гость — не наш случай
    return task.source === 'manual';                       // watchlist/batch/retry — тоже
  }

  function isEligible(task) {
    if (!isMine(task)) return false;
    return !!ACTIVE[task.status || 'queued'];
  }

  function readQueue() {
    if (typeof queueProvider === 'function') {
      try { const q = queueProvider(); return Array.isArray(q) ? q : []; } catch (_) { return []; }
    }
    try {
      if (typeof S !== 'undefined' && S && Array.isArray(S.queue)) return S.queue;
    } catch (_) {}
    return [];
  }

  function findTask(id) {
    const q = readQueue();
    for (let i = 0; i < q.length; i++) if (q[i] && q[i].id === id) return q[i];
    return null;
  }

  function queueTabActive() {
    try {
      const v = document.getElementById('view-queue');
      return !!(v && v.classList && v.classList.contains('active'));
    } catch (_) { return false; }
  }

  function coverOf(task) {
    try { return (task && task.meta && task.meta.artworkUrl) || ''; } catch (_) { return ''; }
  }

  // Название задачи. Порядок источников — от достоверного к грубому:
  // meta.title (пришёл от сервиса) → разбор ссылки → сама ссылка. Если и её нет,
  // возвращаем ПУСТО, и наверху это превращается в честное «название неизвестно»:
  // подставлять сюда правдоподобный текст нельзя, пустая строка значит «не знаю».
  function titleOf(task) {
    try {
      const m  = task && task.meta;
      const tt = (m && typeof m.title === 'string') ? m.title.trim() : '';
      if (tt) return tt;
      const url = (task && typeof task.url === 'string') ? task.url.trim() : '';
      if (!url) return '';
      if (typeof _titleFromUrl === 'function') {
        const guess = String(_titleFromUrl(url) || '').trim();
        if (guess) return guess;
      }
      return url;
    } catch (_) { return ''; }
  }

  function artistOf(task) {
    try {
      const a = task && task.meta && task.meta.artist;
      return (typeof a === 'string') ? a.trim() : '';
    } catch (_) { return ''; }
  }

  // Год и лейбл — из того же enriched-meta (ripster/metadata/__init__.py), что и
  // title/artist. Ни додумывать, ни подставлять нечем: нет значения — пустая
  // строка, и наверху строка «год · лейбл» честно сожмётся до того, что есть.
  function yearOf(task) {
    try {
      const y = task && task.meta && task.meta.year;
      return (typeof y === 'string' ? y : String(y || '')).trim();
    } catch (_) { return ''; }
  }

  function labelOf(task) {
    try {
      const l = task && task.meta && task.meta.label;
      return (typeof l === 'string') ? l.trim() : '';
    } catch (_) { return ''; }
  }

  function pctOf(task) {
    const p = Number(task && task.progress) || 0;
    return Math.max(0, Math.min(100, Math.round(p)));
  }

  // «Сколько загружено». Счётчик треков достовернее шкалы: yt-dlp шлёт
  // total=100 на фрагментах, и «37/100» не значит ничего.
  function countOf(task) {
    if (!task) return '';
    const mt  = (task.meta && (task.meta.trackCount || task.meta.totalTracks)) || 0;
    const tc  = Number(task._tracksCompleted) || 0;
    const tot = Number(task._progTotal) || 0;
    const cur = Number(task._progCurrent) || 0;
    if (tc > 0 && mt > 1) return tc + '/' + mt;
    if (tc > 0) return String(tc);
    if (tot > 1 && tot !== 100 && cur > 0) return cur + '/' + tot;
    return '';
  }

  // ── раскладка ───────────────────────────────────────────────────────────

  // Чистая функция: слот i (0 — текущий) → X в пикселях внутри дока.
  // Парковка у ПРАВОГО края, а не по центру: левая половина дока отдана полю
  // метаданных (см. шапку файла). При центре тексту оставалось ~30px.
  function slotX(i, w) {
    return Math.round(w - ORB - PAD_R) - i * GAP;
  }

  // Ширина поля метаданных = всё, что осталось слева от самого левого видимого
  // элемента стопки. Кругов нет вовсе → поле занимает док целиком: именно в этом
  // состоянии («ничего не качается») ему и нужнее всего место под текст.
  function metaWidth(w, visCount, hasOverflow) {
    if (visCount <= 0) return Math.max(META_MIN, w);
    let x = slotX(Math.max(0, visCount - 1), w);
    if (hasOverflow) x = slotX(MAX_GHOSTS, w) - 20;   // «+N» стоит левее стопки
    return Math.max(META_MIN, x - META_GAP);
  }

  function dockWidth() {
    let w = 0;
    try {
      if (host) {
        w = host.clientWidth || 0;
        if (!w && typeof host.getBoundingClientRect === 'function') w = host.getBoundingClientRect().width || 0;
      }
    } catch (_) { w = 0; }
    return w > 40 ? w : DOCK_W_FALLBACK;
  }

  // ── DOM ─────────────────────────────────────────────────────────────────

  function buildOrb() {
    const root = el('div', 'dlorb');
    root.appendChild(el('div', 'dlorb-glow'));   // рассеивающийся свет — ПЕРВЫМ, под всем
    root.appendChild(el('div', 'dlorb-body'));
    const skin = el('div', 'dlorb-skin');
    root.appendChild(skin);

    const svg = svgEl('svg');
    svg.setAttribute('class', 'dlorb-ring');
    svg.setAttribute('viewBox', '0 0 40 40');
    const trk = svgEl('circle');
    trk.setAttribute('class', 'dlorb-track');
    trk.setAttribute('cx', '20'); trk.setAttribute('cy', '20'); trk.setAttribute('r', String(RAD));
    const arc = svgEl('circle');
    arc.setAttribute('class', 'dlorb-arc');
    arc.setAttribute('cx', '20'); arc.setAttribute('cy', '20'); arc.setAttribute('r', String(RAD));
    arc.setAttribute('stroke-dasharray', CIRC.toFixed(2));
    arc.setAttribute('stroke-dashoffset', CIRC.toFixed(2));
    svg.appendChild(trk); svg.appendChild(arc);
    // Бегущая точка на конце дуги (варианты ember/halo). Стоим на «12 часах»
    // системы 0 0 40 40, вращаем атрибутом rotate(angle 20 20) — svg уже повёрнут
    // на -90, так что нулевой прогресс = верх, и точка совпадает с концом дуги.
    const head = svgEl('circle');
    head.setAttribute('class', 'dlorb-head');
    head.setAttribute('cx', '20'); head.setAttribute('cy', String(20 - RAD)); head.setAttribute('r', '2.6');
    svg.appendChild(head);
    root.appendChild(svg);

    const txt = el('div', 'dlorb-txt');
    const pct = el('div', 'dlorb-pct');
    const cnt = el('div', 'dlorb-cnt');
    txt.appendChild(pct); txt.appendChild(cnt);
    root.appendChild(txt);

    return { el: root, skin: skin, arc: arc, head: head, pct: pct, cnt: cnt, slot: -1, exiting: false, colorKey: '' };
  }

  function ensureOrb(id) {
    let o = orbs.get(id);
    if (o && !o.exiting) return o;
    if (o) { try { o.el.remove(); } catch (_) {} orbs.delete(id); }
    o = buildOrb();
    o.el.setAttribute('data-orb-id', id);
    o.el.style.transform = 'translate3d(' + OFF_L + 'px,-50%,0)';   // за левым краем
    o.fresh = true;
    try { host.appendChild(o.el); } catch (_) {}
    orbs.set(id, o);
    return o;
  }

  function place(o, i, w) {
    const scale = (1 - i * 0.09).toFixed(3);
    const apply = function () {
      o.el.style.transform = 'translate3d(' + slotX(i, w) + 'px,-50%,0) scale(' + scale + ')';
      o.el.classList.add('is-in');
    };
    // Свежему кругу нужен кадр в стартовой точке, иначе браузеру нечего
    // анимировать и «выката слева» не будет — он просто появится посередине.
    if (o.fresh) { o.fresh = false; nextFrame(apply); } else apply();
    o.el.style.zIndex = String(60 - i);
    o.slot = i;
  }

  function rollOut(id) {
    const o = orbs.get(id);
    if (!o || o.exiting) return;
    o.exiting = true;
    o.slot = -1;
    const w = dockWidth();
    try {
      o.el.classList.remove('is-current');
      o.el.style.transform = 'translate3d(' + (w + OFF_R) + 'px,-50%,0) scale(.82)';
      o.el.classList.remove('is-in');
    } catch (_) {}
    setTimeout(function () {
      try { o.el.remove(); } catch (_) {}
      if (orbs.get(id) === o) orbs.delete(id);
      // Пересчёт ПОСЛЕ уката. Поле метаданных резервирует место под живые круги,
      // а sync зовётся только на события очереди: последний укатившийся круг не
      // порождает ни одного события, и поле навсегда оставалось ужатым под уже
      // исчезнувшую стопку — снаружи это «текст почему-то в половину дока».
      try { sync(); } catch (_) {}
    }, EXIT_MS);
  }

  function applyColor(o, col) {
    if (!o || !col) return;      // не дать null'у из отказавшего canvas уронить .then
    try {
      o.el.style.setProperty('--orb-c',    col.c);
      o.el.style.setProperty('--orb-c-lt', col.lt);
      o.el.style.setProperty('--orb-c-dk', col.dk);
      o.el.style.setProperty('--orb-c2',   col.c2);
      o.el.style.setProperty('--orb-glow',     col.glow);
      o.el.style.setProperty('--orb-glow-mid', col.glowMid);
      o.el.style.setProperty('--orb-glow2',     col.glow2);
      o.el.style.setProperty('--orb-glow2-mid', col.glow2Mid);
    } catch (_) {}
    o.colorKey = col.c;
  }

  function paint(task, o) {
    const url = coverOf(task);
    const key = (COLOR_MODE[variant] || 'dominant') + '|' + url;
    const hit = colorCache.get(key);
    if (hit) { applyColor(o, hit); return; }
    applyColor(o, FALLBACK);          // сразу видимый честный цвет, без ожидания сети
    resolveColor(url).then(function (col) {
      if (!col) return;                       // остаёмся на запасном цвете
      colorCache.set(key, col);
      const cur = orbs.get(task.id);
      if (cur && !cur.exiting) applyColor(cur, col);
    }, function () {});
  }

  // Предзагрузка ТОЛЬКО следующей задачи: иначе, став текущей, она нальётся
  // цветом рывком — в тот момент, когда картинка наконец доедет.
  function warm(task) {
    const url = coverOf(task);
    const key = (COLOR_MODE[variant] || 'dominant') + '|' + url;
    if (colorCache.has(key)) return;
    colorCache.set(key, FALLBACK);    // занять место, чтобы не запустить второй раз
    resolveColor(url).then(function (col) { if (col) colorCache.set(key, col); }, function () {});
  }

  function setProgress(o, p, cntTxt) {
    try {
      o.arc.setAttribute('stroke-dashoffset', (CIRC * (1 - p / 100)).toFixed(2));
      if (o.head) o.head.setAttribute('transform', 'rotate(' + (p * 3.6).toFixed(1) + ' 20 20)');
      o.pct.textContent = p + '%';
      o.cnt.textContent = cntTxt || '';
    } catch (_) {}
  }

  // ── поле метаданных слева от круга ──────────────────────────────────────
  //
  // Стили заданы прямо здесь, а не в main.css, СОЗНАТЕЛЬНО: узел целиком
  // создаётся этим файлом, и держать его вид в другом файле — значит завести
  // ещё одну пару, которую надо не забыть обновить вместе (и ещё один ?v=).
  function buildMeta() {
    const root = el('div', 'dlmeta');
    root.style.cssText =
      'position:absolute;left:0;top:50%;transform:translateY(-50%);' +
      'box-sizing:border-box;padding:0 2px;overflow:hidden;z-index:1;' +
      'opacity:0;transition:opacity .3s ease';
    const label = el('div', 'dlmeta-label');
    label.style.cssText =
      'font-family:var(--display);font-size:8px;font-weight:800;letter-spacing:.6px;' +
      'text-transform:uppercase;color:var(--muted);' +
      'white-space:nowrap;overflow:hidden;text-overflow:ellipsis';
    const title = el('div', 'dlmeta-title');
    title.style.cssText =
      'font-family:var(--display);font-size:11px;font-weight:700;line-height:1.22;' +
      'color:var(--text);margin-top:2px;word-break:break-word;overflow:hidden;' +
      'display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical';
    const sub = el('div', 'dlmeta-sub');
    sub.style.cssText =
      'font-size:9px;line-height:1.25;color:var(--muted);margin-top:2px;' +
      'white-space:nowrap;overflow:hidden;text-overflow:ellipsis';
    root.appendChild(label); root.appendChild(title); root.appendChild(sub);
    return { root: root, label: label, title: title, sub: sub };
  }

  // Что показать. Возвращает {task, state, active}:
  //   running/queued — есть незавершённая задача (running важнее просто стоящей);
  //   last/failed    — активных нет, показываем последнюю ЗАВЕРШЁННУЮ, причём
  //                    'failed' отдельно: назвать «загрузкой» то, что упало с
  //                    ошибкой или было отменено, — это соврать об успехе;
  //   idle           — своих задач в очереди нет вообще.
  // Времени завершения у задачи нет (в _make_task только "added"), поэтому
  // «последняя» = последняя по порядку в массиве очереди. Другого источника нет,
  // и придумывать порядок по прогрессу/логу было бы догадкой, а не фактом.
  function pickMeta() {
    const q = readQueue();
    const active = [], finished = [];
    for (let i = 0; i < q.length; i++) {
      const tk = q[i];
      if (!isMine(tk)) continue;
      if (ACTIVE[tk.status || 'queued']) active.push(tk);
      else if (TERMINAL[tk.status]) finished.push(tk);
    }
    if (active.length) {
      let cur = null;
      for (let i = 0; i < active.length; i++) if (active[i].status === 'running') { cur = active[i]; break; }
      return { task: cur || active[0], state: cur ? 'running' : 'queued', active: active.length };
    }
    if (finished.length) {
      const last = finished[finished.length - 1];
      return { task: last, state: last.status === 'done' ? 'last' : 'failed', active: 0 };
    }
    return { task: null, state: 'idle', active: 0 };
  }

  function renderMeta(w, visCount, hasOverflow) {
    if (!host) return;
    if (!metaEl) {
      metaEl = buildMeta();
      // Вставляем ПЕРВЫМ, чтобы круги в потоке шли после и перекрывали текст,
      // а не наоборот, если ширина вдруг посчиталась с запасом.
      try { host.insertBefore(metaEl.root, host.firstChild); } catch (_) { metaEl = null; return; }
    }
    const show = enabled && !queueTabActive();
    metaEl.root.style.opacity = show ? '1' : '0';
    if (!show) return;
    metaEl.root.style.width = metaWidth(w, visCount, hasOverflow) + 'px';

    const pick = pickMeta();

    if (!pick.task) {
      // Ничего своего в очереди. Строку кладём в крупный узел, а не в мелкую
      // подпись: это единственное, что здесь есть, и оно должно читаться.
      metaEl.label.textContent = '';
      metaEl.title.textContent = _t('dlmeta.idle');
      metaEl.title.style.color = 'var(--muted)';
      metaEl.sub.textContent   = '';
      return;
    }

    // 100% при живом статусе «running» — это НЕ «скачивается»: файлы сошлись,
    // осталась упаковка/запись. Прежняя подпись «Скачивается» у полностью
    // докачанного релиза (замечание владельца 24.09) честно меняется на
    // «завершается» — раньше «последняя загрузка», потому что задача ещё
    // не завершилась: врать о finished нельзя симметрично в обе стороны.
    const label = pick.state === 'running' && pctOf(pick.task) >= 100 ? 'finishing' : pick.state;
    metaEl.label.textContent = _t('dlmeta.' + label);
    metaEl.title.style.color = 'var(--text)';
    const name = titleOf(pick.task);
    metaEl.title.textContent = name || _t('dlmeta.unknown_title');

    const parts = [];
    const art = artistOf(pick.task);
    if (art) parts.push(art);
    else {
      // Исполнитель неизвестен — говорим ПОЧЕМУ, если сервер это сообщил.
      const m = pick.task.meta || {};
      if (m.meta_error) parts.push('⚠ ' + m.meta_error);
      else if (!m.enriched && (pick.state === 'running' || pick.state === 'queued')) parts.push(_t('q.meta_loading'));
    }
    if (pick.active > 1) parts.push(_t('dlmeta.more_queued', { n: pick.active - 1 }));
    metaEl.sub.textContent = parts.join(' · ');
  }

  function renderMore(w) {
    if (!host) return;
    if (!moreEl) {
      moreEl = el('div', 'dlorb-more');
      try { host.appendChild(moreEl); } catch (_) {}
    }
    if (overflow > 0) {
      moreEl.textContent = _t('dlorb.more', { n: overflow });
      moreEl.style.transform = 'translate3d(' + (slotX(MAX_GHOSTS, w) - 20) + 'px,-50%,0)';
      moreEl.classList.add('is-in');
    } else {
      moreEl.textContent = '';
      moreEl.classList.remove('is-in');
    }
  }

  // ── карточки о загрузке над кругом (#9012) ──────────────────────────────
  //
  // Живут В ТОМ ЖЕ ЭТАЖЕ, что и док (.dlside-foot), перед ним: стек и круги —
  // соседи по flex-колонке, а не листы поверх меню. До 24.09.2026 стек был
  // прибит к <body> поверх колонки панели, и на низком окне карточки ложились
  // на пункты меню («нихера не видно» — замечание владельца). Прямоугольник
  // стека — ширина колонки; что не влезло по высоте — не показывается вовсе,
  // а не обрезается пополам. ОДНА ЗАДАЧА — ОДИН ВИДЖЕТ: в стек попадают только
  // задачи, стоящие СЛЕДИ за текущей (см. sync), текущую рассказывает поле
  // метаданных, и карточка с тем же релизом — это дубль.

  function buildCallout() {
    const root = el('div', 'dlcall');
    const title = el('div', 'dlcall-title');
    const artist = el('div', 'dlcall-artist');
    const meta = el('div', 'dlcall-meta');
    const pct = el('div', 'dlcall-pct');
    root.appendChild(title); root.appendChild(artist); root.appendChild(meta); root.appendChild(pct);
    return { el: root, title: title, artist: artist, meta: meta, pct: pct, exiting: false };
  }

  function ensureCallout(id) {
    let c = callouts.get(id);
    if (c && !c.exiting) return c;
    if (c) { try { c.el.remove(); } catch (_) {} callouts.delete(id); }
    c = buildCallout();
    c.el.setAttribute('data-call-id', id);
    try { calloutHost.appendChild(c.el); } catch (_) {}
    callouts.set(id, c);
    return c;
  }

  function fillCallout(c, task, current) {
    const name = titleOf(task);
    c.title.textContent = name || _t('dlmeta.unknown_title');
    const art = artistOf(task);
    c.artist.textContent = art;                       // нет артиста — строки нет (не выдумываем)
    c.artist.style.display = art ? '' : 'none';
    const y = yearOf(task), lb = labelOf(task);
    const bits = []; if (y) bits.push(y); if (lb) bits.push(lb);
    c.meta.textContent = bits.join(' · ');            // нет года и лейбла — строки нет вовсе
    c.meta.style.display = bits.length ? '' : 'none';
    if (current) { c.pct.textContent = pctOf(task) + '%'; c.pct.style.display = ''; }
    else { c.pct.textContent = ''; c.pct.style.display = 'none'; }
  }

  function exitCallout(id) {
    const c = callouts.get(id);
    if (!c || c.exiting) return;
    c.exiting = true;
    try { c.el.classList.remove('is-in'); } catch (_) {}
    setTimeout(function () {
      try { c.el.remove(); } catch (_) {}
      if (callouts.get(id) === c) callouts.delete(id);
    }, EXIT_MS);
  }

  function rectOf(node) {
    try { return node && node.getBoundingClientRect ? node.getBoundingClientRect() : null; } catch (_) { return null; }
  }

  // Высота, доступная карточкам, при двух этажах: рост стека ограничен НЕ
  // геометрией дока (он теперь сосед по потоку), а резервом меню — панель
  // не должна превращаться в один сплошной стек: пункты и так уходят в свою
  // прокрутку, но без видимых строк прокрутка бесполезна. Полоска кругов
  // стабильна по высоте: док анимирует её от 0 до 132, и брать текущее
  // dr.height значило бы, что на раскате стек карточек едет следом.
  const FOOT_CHROME = 12;    // отступ подложки сверху + зазор до дока (см. .dlside-foot)

  function layoutCallouts() {
    if (!calloutHost) return 0;
    const dr = rectOf(host);
    const sr = rectOf(document.querySelector('.sidebar'));
    // Док скрыт медиазапросом (узкое окно) или панель сложена — стеку не от
    // чего считаться и незачем жить: этаж на телефоне скрыт целиком.
    if (!dr || dr.width < 40 || !sr || sr.height < 8) { calloutHost.style.display = 'none'; return 0; }
    const band = dr.height > 4 ? Math.max(dr.height, ORB_BAND) : 0;
    const avail = Math.max(0, Math.round(sr.height - NAV_RESERVE - band - FOOT_CHROME));
    calloutHost.style.display = '';
    calloutHost.style.maxHeight = avail + 'px';
    return avail;
  }

  // Сколько низовых карточек помещается в доступную высоту. Стек растёт ВВЕРХ от
  // круга, поэтому считаем снизу вверх: обрезанная пополам карточка читается
  // дефектом, а не показанная — честное «не влезла». В «+N» над кругами она при
  // этом не учитывается: там счёт только силуэтов кругов.
  function fitCallouts(list, avail) {
    let used = 0, shown = 0;
    for (let i = 0; i < list.length; i++) {
      const c = callouts.get(list[i].id);
      if (!c || c.exiting) continue;
      try { c.el.style.display = ''; } catch (_) {}
      const h = Math.round(c.el.offsetHeight || 0);
      const need = used + (shown ? CALL_VGAP : 0) + h;
      if (need > avail) {
        try { c.el.style.display = 'none'; c.el.classList.remove('is-in'); } catch (_) {}
        continue;
      }
      used = need; shown++;
    }
    return shown;
  }

  function renderCallouts(vis) {
    if (typeof document === 'undefined') return;
    if (!calloutHost) {
      // Стек — перворождённый этажа: он встаёт НАД полоской кругов и растёт
      // вверх от неё (column-reverse), а не накрывает что-либо.
      const sb = footEl || (host && host.parentElement);
      if (!sb) return;
      calloutHost = el('div', 'dlcallouts');
      try { sb.insertBefore(calloutHost, host); } catch (_) { calloutHost = null; return; }
    }
    const avail = layoutCallouts();
    const show = enabled && !queueTabActive();
    const list = show ? (vis || []).slice(0, MAX_CALLOUTS) : [];
    const live = {};
    for (let i = 0; i < list.length; i++) live[list[i].id] = i;
    callouts.forEach(function (c, id) { if (live[id] == null) exitCallout(id); });
    for (let i = 0; i < list.length; i++) {
      const task = list[i], current = i === 0;
      const c = ensureCallout(task.id);
      fillCallout(c, task, current);
      const o = orbs.get(task.id);
      if (current && o && o.colorKey) { try { c.el.style.setProperty('--call-c', o.colorKey); } catch (_) {} }
      else { try { c.el.style.removeProperty('--call-c'); } catch (_) {} }
    }
    fitCallouts(list, avail);
    for (let i = 0; i < list.length; i++) {
      const c = callouts.get(list[i].id);
      if (!c || c.el.style.display === 'none') continue;   // не влезла — не показываем
      nextFrame(function () { try { c.el.classList.add('is-in'); } catch (_) {} });
    }
  }

  // ── сборка сцены ────────────────────────────────────────────────────────

  function mount() {
    if (host) return host;
    let sb = null;
    try { sb = document.querySelector('.sidebar'); } catch (_) {}
    if (!sb) return null;
    // Второй этаж панели. Без него док встал бы третьим ребёнком в flex-колонку
    // к меню — ровно тот оверлей, из- которого вырос дефект 24.09.
    let foot = null;
    try { foot = sb.querySelector('.dlside-foot'); } catch (_) {}
    if (!foot) {
      foot = el('div', 'dlside-foot');
      try { sb.appendChild(foot); } catch (_) { return null; }
    }
    footEl = foot;
    let d = null;
    try { d = foot.querySelector('.dlorb-dock'); } catch (_) {}
    if (!d) {
      d = el('div', 'dlorb-dock');
      d.setAttribute('data-i18n-title', 'dlorb.title');
      try { d.setAttribute('title', _t('dlorb.title')); } catch (_) {}
      try { foot.appendChild(d); } catch (_) {}
    }
    d.setAttribute('data-orb-variant', variant);
    host = d;
    return host;
  }

  function sync() {
    if (!host) mount();
    if (!host) return;
    const all = readQueue().filter(isEligible);
    // Текущая — та, что реально бежит; если ни одна ещё не стартовала, первая в очереди.
    const running = [], waiting = [];
    for (let i = 0; i < all.length; i++) (all[i].status === 'running' ? running : waiting).push(all[i]);
    const ordered = running.concat(waiting);
    const show = (enabled && !queueTabActive()) ? ordered : [];
    const vis  = show.slice(0, 1 + MAX_GHOSTS);
    overflow   = Math.max(0, show.length - vis.length);

    const live = {};
    for (let i = 0; i < vis.length; i++) live[vis[i].id] = 1;
    orbs.forEach(function (o, id) { if (!live[id]) rollOut(id); });

    const w = dockWidth();
    for (let i = 0; i < vis.length; i++) {
      const task = vis[i];
      const o = ensureOrb(task.id);
      const cur = i === 0;
      o.el.classList.toggle('is-current', cur);
      o.el.classList.toggle('is-ghost', !cur);
      if (cur) {
        paint(task, o);
        setProgress(o, pctOf(task), countOf(task));
      } else {
        // Силуэт: ни цвета, ни оттенков, ни чисел. И обложка ему не нужна.
        try {
          o.el.style.removeProperty('--orb-c');
          o.el.style.removeProperty('--orb-c-lt');
          o.el.style.removeProperty('--orb-c-dk');
          o.el.style.removeProperty('--orb-c2');
          o.el.style.removeProperty('--orb-glow');
          o.el.style.removeProperty('--orb-glow-mid');
          o.el.style.removeProperty('--orb-glow2');
          o.el.style.removeProperty('--orb-glow2-mid');
        } catch (_) {}
        o.colorKey = '';
        setProgress(o, 0, '');
      }
      place(o, i, w);
    }
    if (vis.length > 1) warm(vis[1]);
    renderMore(w);
    // Стек — только про те, что СЛЕДИ за текущей. Карточка текущей задачи —
    // это дубль поля метаданных: про одну загрузку говорит один виджет.
    renderCallouts(vis.slice(1));
    // Ширину считаем по ЧИСЛУ ЖИВЫХ УЗЛОВ, а не по vis: укатывающийся круг ещё
    // 0.72s едет по доку, и поле, успевшее развернуться на всю ширину, оказывалось
    // под ним — текст на эти доли секунды перечёркивало кругом.
    renderMeta(w, Math.max(vis.length, orbs.size), overflow > 0);
  }

  function onProgress(msg) {
    if (!msg || !msg.id) return;
    const o = orbs.get(msg.id);
    if (!o || o.exiting || o.slot !== 0) { sync(); return; }   // состав сцены мог измениться
    const task = findTask(msg.id);
    if (!task) { sync(); return; }
    if (!isEligible(task)) { sync(); return; }
    setProgress(o, pctOf(task), countOf(task));
    const c = callouts.get(msg.id);
    if (c && !c.exiting) { try { c.pct.textContent = pctOf(task) + '%'; } catch (_) {} }
  }

  // ── настройки ───────────────────────────────────────────────────────────

  function cfg() {
    try { return (typeof S !== 'undefined' && S && S.config) || null; } catch (_) { return null; }
  }

  function applyConfig() {
    const c = cfg();
    if (c) {
      enabled = c['show-dlorb'] !== false;                       // по умолчанию включён
      if (VARIANTS.indexOf(c['show-dlorb-variant']) >= 0) variant = c['show-dlorb-variant'];
    }
    if (host) host.setAttribute('data-orb-variant', variant);
    syncSettingsUI();
    renderPreviews('dlorb-variants');
    sync();
  }

  function syncSettingsUI() {
    try {
      const chk = document.getElementById('s-dlorb-enabled');
      if (chk) chk.checked = !!enabled;
    } catch (_) {}
  }

  function save(key, value) {
    try { if (typeof saveSetting === 'function') saveSetting(key, value); } catch (_) {}
  }

  function setVariant(v) {
    if (VARIANTS.indexOf(v) < 0) return;
    variant = v;
    if (host) host.setAttribute('data-orb-variant', v);
    colorCache.clear();                       // у вариантов разный способ брать цвет
    orbs.forEach(function (o) { o.colorKey = ''; });
    save('show-dlorb-variant', v);
    renderPreviews('dlorb-variants');
    sync();
  }

  function setEnabled(on) {
    enabled = !!on;
    save('show-dlorb', !!on);
    sync();
  }

  // Четыре образца в настройках: выбор делается глазами, а не по описанию.
  function renderPreviews(node) {
    let box = null;
    try { box = typeof node === 'string' ? document.getElementById(node) : node; } catch (_) {}
    if (!box) return;
    try { box.innerHTML = ''; } catch (_) { return; }
    VARIANTS.forEach(function (v) {
      const card = el('div', 'dlorb-vcard' + (v === variant ? ' active' : ''));
      card.setAttribute('data-orb-variant', v);
      card.onclick = function () { setVariant(v); };

      const stage = el('div', 'dlorb-vstage');
      const o = buildOrb();
      o.el.classList.add('is-in', 'is-current', 'is-demo');
      applyColor(o, FALLBACK);
      setProgress(o, 62, '7/12');
      stage.appendChild(o.el);

      const nm = el('div', 'dlorb-vname');
      nm.setAttribute('data-i18n', 'dlorb.v_' + v);
      nm.textContent = _t('dlorb.v_' + v);

      const md = el('div', 'dlorb-vmode');
      md.setAttribute('data-i18n', 'dlorb.mode_' + COLOR_MODE[v]);
      md.textContent = _t('dlorb.mode_' + COLOR_MODE[v]);

      card.appendChild(stage); card.appendChild(nm); card.appendChild(md);
      box.appendChild(card);
    });
  }

  // ── состояние наружу (для проверок) ─────────────────────────────────────

  function state() {
    const on = [];
    orbs.forEach(function (o, id) { if (!o.exiting && o.slot >= 0) on.push({ id: id, slot: o.slot, o: o }); });
    on.sort(function (a, b) { return a.slot - b.slot; });
    const cur = on.length ? on[0] : null;
    return {
      visible:  on.length > 0,
      variant:  variant,
      enabled:  enabled,
      ids:      on.map(function (x) { return x.id; }),
      current:  cur ? cur.id : null,
      ghosts:   on.slice(1).map(function (x) { return x.id; }),
      overflow: overflow,
      pct:      cur ? cur.o.pct.textContent : '',
      count:    cur ? cur.o.cnt.textContent : '',
      colored:  on.map(function (x) { return !!x.o.colorKey; }),
      dashoffset: cur ? Number(cur.o.arc.getAttribute('stroke-dashoffset')) : null,
      circumference: CIRC,
      meta: metaEl ? {
        visible: metaEl.root.style.opacity === '1',
        width:   metaEl.root.style.width,
        label:   metaEl.label.textContent,
        title:   metaEl.title.textContent,
        sub:     metaEl.sub.textContent,
        state:   pickMeta().state
      } : null,
      callouts: (function () {
        const out = [];
        callouts.forEach(function (c, id) {
          if (c.exiting) return;
          out.push({
            id: id, shown: c.el.classList.contains('is-in'),
            title: c.title.textContent, artist: c.artist.textContent,
            meta: c.meta.textContent, pct: c.pct.textContent
          });
        });
        out.sort(function (a, b) { return on.map(function (x) { return x.id; }).indexOf(a.id) - on.map(function (x) { return x.id; }).indexOf(b.id); });
        return out;
      })()
    };
  }

  function attach(node) {
    host = node || null;
    moreEl = null;
    metaEl = null;
    orbs.clear();
    callouts.clear();
    if (calloutHost) { try { calloutHost.remove(); } catch (_) {} calloutHost = null; }
    if (host) host.setAttribute('data-orb-variant', variant);
    return host;
  }

  function reset() {
    orbs.forEach(function (o) { try { o.el.remove(); } catch (_) {} });
    orbs.clear();
    callouts.forEach(function (c) { try { c.el.remove(); } catch (_) {} });
    callouts.clear();
    if (calloutHost) { try { calloutHost.remove(); } catch (_) {} calloutHost = null; }
    if (moreEl) { try { moreEl.remove(); } catch (_) {} moreEl = null; }
    if (metaEl) { try { metaEl.root.remove(); } catch (_) {} metaEl = null; }
    colorCache.clear();
    overflow = 0;
  }

  window.DLOrb = {
    sync: sync,
    onProgress: onProgress,
    applyConfig: applyConfig,
    setVariant: setVariant,
    setEnabled: setEnabled,
    renderPreviews: renderPreviews,
    isEligible: isEligible,
    slotX: slotX,
    state: state,
    attach: attach,
    reset: reset,
    setQueueProvider: function (fn) { queueProvider = fn; },
    VARIANTS: VARIANTS,
    COLOR_MODE: COLOR_MODE,
    MAX_GHOSTS: MAX_GHOSTS,
    MAX_CALLOUTS: MAX_CALLOUTS,
    ORB: ORB,
    PAD_R: PAD_R,
    FALLBACK: FALLBACK
  };

  // Пересчёт при изменении размера окна. Позиции кругов считаются от ШИРИНЫ дока
  // в момент sync(), а sync зовётся только на события очереди и смену вкладки —
  // то есть после ресайза круги оставались стоять по старой арифметике до
  // ближайшего события, а при переходе через 700px (там док скрыт медиазапросом)
  // и вовсе не возвращались, пока не сменится состав очереди.
  let resizeT = null;
  function onResize() {
    clearTimeout(resizeT);
    resizeT = setTimeout(sync, 150);   // ресайз сыплет десятками событий подряд
  }

  // Смена языка. applyLang() перерисовывает ТОЛЬКО узлы с data-i18n, а поле
  // метаданных собирается в момент отрисовки, и вешать на него data-i18n нельзя —
  // там динамический текст, applyLang затёр бы название трека переводом ключа.
  // Признак смены языка — атрибут lang на <html>, его ставит тот же applyLang.
  function watchLang() {
    try {
      if (typeof MutationObserver !== 'function' || typeof document === 'undefined') return;
      new MutationObserver(function () { sync(); })
        .observe(document.documentElement, { attributes: true, attributeFilter: ['lang'] });
    } catch (_) {}
  }

  function init() {
    mount();
    sync();
    watchLang();
    try { window.addEventListener('resize', onResize); } catch (_) {}
  }
  try {
    if (typeof document !== 'undefined') {
      if (document.readyState !== 'loading') init();
      else document.addEventListener('DOMContentLoaded', init);
    }
  } catch (_) {}
})();
