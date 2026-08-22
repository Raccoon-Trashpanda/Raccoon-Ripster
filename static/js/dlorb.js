/* dlorb.js — круглый индикатор загрузки в ПУСТОМ НИЗУ левой панели.
 *
 * ЗАЧЕМ. Уйдя с вкладки задач, человек теряет прогресс из виду совсем: ни
 * процентов, ни шкалы больше нигде нет. Пустая полоса под пунктом «Бот» —
 * единственное место в панели, которое ничем не занято.
 *
 * ДВИЖЕНИЕ. Круг выкатывается из левого края, ПАРКУЕТСЯ ПОСЕРЕДИНЕ и стоит,
 * пока идёт загрузка; по завершении укатывается направо. На область карточек
 * не вылезает НИКОГДА — правая граница панели это стена, и держит её CSS
 * (.dlorb-dock{overflow:hidden;contain:layout paint}), а не аккуратность
 * арифметики: даже если расчёт X ошибётся, за прямоугольник ничего не выйдет.
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

  const VARIANTS   = ['neon', 'aurora', 'vinyl', 'mono'];
  // Варианты отличаются в том числе СПОСОБОМ брать цвет: доминирующий тон
  // против среднего. Спор решается глазами, а не описанием.
  const COLOR_MODE = { neon: 'dominant', aurora: 'average', vinyl: 'average', mono: 'dominant' };
  const DEF_VARIANT = 'neon';

  const MAX_GHOSTS = 3;      // потолок видимых силуэтов, дальше «+N»
  const ORB   = 44;          // диаметр круга, px (совпадает с main.css)
  const GAP   = 13;          // сдвиг силуэта в стопке
  const OFF_L = -76;         // стартовая точка выката (за левым краем)
  const OFF_R = 76;          // точка уката (за правым краем, но под overflow:hidden)
  const RAD   = 17.5;        // радиус кольца в системе viewBox 0 0 40 40
  const CIRC  = 2 * Math.PI * RAD;
  const EXIT_MS = 720;
  const DOCK_W_FALLBACK = 198;   // ширина панели 220 минус padding 2×10

  const ACTIVE = { queued: 1, running: 1, pending: 1 };

  // Честный запасной цвет: обложки может не быть, она может не догрузиться,
  // и она почти всегда с чужого домена — canvas тогда «пачкается».
  const FALLBACK = mkCol(340, 0.52, 0.58);

  let host    = null;
  let moreEl  = null;
  let enabled = true;
  let variant = DEF_VARIANT;
  let overflow = 0;
  let queueProvider = null;

  const orbs = new Map();          // id → { el, skin, arc, pct, cnt, slot, exiting }
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

  // Один тон → набор производных. Так варианты обходятся без color-mix(),
  // который в старой WebView2 просто не поддерживается и даёт прозрачное ничто.
  function mkCol(h, s, l) {
    const sat = Math.min(0.74, Math.max(0.32, s));
    const lig = Math.min(0.68, Math.max(0.44, l));
    return {
      c:  hsl(h, sat, lig),
      lt: hsl(h, Math.min(0.9, sat + 0.12), Math.min(0.82, lig + 0.16)),
      dk: hsl(h, sat, Math.max(0.22, lig - 0.20)),
      c2: hsl((h + 148) % 360, sat, Math.min(0.72, lig + 0.06))
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

  function isEligible(task) {
    if (!task) return false;
    if ((task.session_id || '') !== '') return false;      // гость — не наш случай
    if (task.source !== 'manual') return false;            // watchlist/batch/retry — тоже
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
  function slotX(i, w) {
    return Math.round((w - ORB) / 2) - i * GAP;
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
    root.appendChild(svg);

    const txt = el('div', 'dlorb-txt');
    const pct = el('div', 'dlorb-pct');
    const cnt = el('div', 'dlorb-cnt');
    txt.appendChild(pct); txt.appendChild(cnt);
    root.appendChild(txt);

    return { el: root, skin: skin, arc: arc, pct: pct, cnt: cnt, slot: -1, exiting: false, colorKey: '' };
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
    }, EXIT_MS);
  }

  function applyColor(o, col) {
    if (!o || !col) return;      // не дать null'у из отказавшего canvas уронить .then
    try {
      o.el.style.setProperty('--orb-c',    col.c);
      o.el.style.setProperty('--orb-c-lt', col.lt);
      o.el.style.setProperty('--orb-c-dk', col.dk);
      o.el.style.setProperty('--orb-c2',   col.c2);
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
      o.pct.textContent = p + '%';
      o.cnt.textContent = cntTxt || '';
    } catch (_) {}
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

  // ── сборка сцены ────────────────────────────────────────────────────────

  function mount() {
    if (host) return host;
    let sb = null;
    try { sb = document.querySelector('.sidebar'); } catch (_) {}
    if (!sb) return null;
    let d = null;
    try { d = sb.querySelector('.dlorb-dock'); } catch (_) {}
    if (!d) {
      d = el('div', 'dlorb-dock');
      d.setAttribute('data-i18n-title', 'dlorb.title');
      try { d.setAttribute('title', _t('dlorb.title')); } catch (_) {}
      try { sb.appendChild(d); } catch (_) {}
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
        } catch (_) {}
        o.colorKey = '';
        setProgress(o, 0, '');
      }
      place(o, i, w);
    }
    if (vis.length > 1) warm(vis[1]);
    renderMore(w);
  }

  function onProgress(msg) {
    if (!msg || !msg.id) return;
    const o = orbs.get(msg.id);
    if (!o || o.exiting || o.slot !== 0) { sync(); return; }   // состав сцены мог измениться
    const task = findTask(msg.id);
    if (!task) { sync(); return; }
    if (!isEligible(task)) { sync(); return; }
    setProgress(o, pctOf(task), countOf(task));
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
      circumference: CIRC
    };
  }

  function attach(node) {
    host = node || null;
    moreEl = null;
    orbs.clear();
    if (host) host.setAttribute('data-orb-variant', variant);
    return host;
  }

  function reset() {
    orbs.forEach(function (o) { try { o.el.remove(); } catch (_) {} });
    orbs.clear();
    if (moreEl) { try { moreEl.remove(); } catch (_) {} moreEl = null; }
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

  function init() {
    mount();
    sync();
    try { window.addEventListener('resize', onResize); } catch (_) {}
  }
  try {
    if (typeof document !== 'undefined') {
      if (document.readyState !== 'loading') init();
      else document.addEventListener('DOMContentLoaded', init);
    }
  } catch (_) {}
})();
