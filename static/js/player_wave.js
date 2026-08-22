// ======================================================================
// Волна трека — ПОДПИСЬ, а не измерение.
//
// В референсе (design/approved/neon-reference-mobile.png) волна — единственный
// элемент, который есть на КАЖДОМ экране: в мини-плеере, в очереди, во всю
// ширину на «Плеере». Это опознавательный знак Ripster, и именно поэтому он
// обязан быть УСТОЙЧИВЫМ.
//
// ЧЕГО ЭТОТ КОД НЕ ДЕЛАЕТ: он не разбирает аудио и не знает настоящих пиков.
// Решение владельца от 15.08.2026 — «рисованная из метаданных», и врать про
// амплитуду мы не будем: форма выводится из стабильного сида
// (артист + название + длительность), поэтому ОДИН И ТОТ ЖЕ ТРЕК ВСЕГДА ДАЁТ
// ОДНУ И ТУ ЖЕ ВОЛНУ. `Math.random()` здесь запрещён: форма, пляшущая между
// открытиями, врёт заметнее статичной — человек считает её измерением ровно до
// того момента, когда заметит, что она другая.
//
// Настоящий разбор аудио (Web Audio -> пики) сюда добавить можно и нужно позже:
// точка входа одна — `rwBars()`. Пока её нет, честнее рисовать знак.
//
// ЗДЕСЬ ТОЛЬКО ФОРМА И ОТРИСОВКА. Связь с плеером (кто играет, где позиция,
// куда перематывать) живёт в player.js — этот файл ничего не играет, не ставит
// на паузу и не создаёт узлов Web Audio.
// ======================================================================

// FNV-1a, 32 бита. Нужен не крипто-хеш, а устойчивое число из строки: одно и то
// же имя обязано давать одно и то же зерно на любой машине и в любом браузере.
function rwSeed(artist, title, durationSec) {
  const s = String(artist || '') + '' + String(title || '')
          + '' + String(Math.round(Number(durationSec) || 0));
  let h = 0x811c9dc5;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 0x01000193) >>> 0;
  }
  return h >>> 0;
}

// mulberry32 — маленький детерминированный ГПСЧ. Своё состояние, ничего
// глобального: два холста на одной странице не влияют друг на друга.
function _rwRand(seed) {
  let a = seed >>> 0;
  return function () {
    a = (a + 0x6D2B79F5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

// Высоты столбиков, 0..1. Форма — не белый шум: у трека есть вступление,
// набор, плато и спад, и именно это делает картинку похожей на музыку, а не на
// щётку. Огибающая складывается из трёх медленных синусов со сдвинутыми фазами,
// поверх — мелкое дрожание. Всё из одного сида, поэтому воспроизводимо.
function rwBars(artist, title, durationSec, count) {
  const n = Math.max(8, Math.min(1200, count | 0 || 64));
  const rnd = _rwRand(rwSeed(artist, title, durationSec));
  // Фазы и веса тянем ДО цикла: порядок вызовов ГПСЧ — часть детерминизма.
  const ph = [rnd() * 6.283, rnd() * 6.283, rnd() * 6.283];
  const w  = [0.55 + rnd() * 0.25, 0.28 + rnd() * 0.18, 0.14 + rnd() * 0.12];
  const fr = [1 + rnd() * 1.5, 3 + rnd() * 3, 7 + rnd() * 6];
  const out = new Array(n);
  for (let i = 0; i < n; i++) {
    const x = i / (n - 1);
    let v = 0;
    for (let k = 0; k < 3; k++) v += w[k] * (0.5 + 0.5 * Math.sin(ph[k] + x * fr[k] * 6.283));
    v /= (w[0] + w[1] + w[2]);
    v = 0.25 + 0.75 * v;                 // не даём столбикам схлопываться в ноль
    v *= 0.82 + rnd() * 0.36;            // мелкое дрожание
    // Заход и уход: у трека края тише середины.
    const fade = Math.min(1, x / 0.06) * Math.min(1, (1 - x) / 0.06);
    v *= 0.35 + 0.65 * fade;
    out[i] = Math.max(0.06, Math.min(1, v));
  }
  return out;
}

// Сколько столбиков влезает в ширину. Вынесено отдельно, потому что этим же
// числом пользуется player.js: он перерисовывает волну только когда МЕНЯЕТСЯ
// НОМЕР столбика под курсором воспроизведения, а не каждый кадр.
function rwBarCount(cssWidth, barWidth, barGap) {
  const bw = Math.max(1, Number(barWidth) || 2);
  const gap = Math.max(0.5, barGap != null ? Number(barGap) : 1.5);
  return Math.max(8, Math.floor((Math.max(1, Number(cssWidth) || 1) + gap) / (bw + gap)));
}

// Палитра — ИЗ ПЕРЕМЕННЫХ ТЕМЫ, не из хексов в коде. `--pp-svc` ставит
// _applyServiceColor() в player.js (цвет сервиса, откуда играет трек), поэтому
// волна красится тем же, чем красились полосы до неё; `--purple` и `--blue`
// дают тот же фирменный переход, что уже описан у .fp-fill в main.css;
// `--rw-idle` — несыгранный остаток, объявлен рядом с --pp-track/--pp-cache.
function rwPalette(el) {
  const root = el || (typeof document !== 'undefined' ? document.documentElement : null);
  if (!root || typeof getComputedStyle !== 'function') {
    return { a: '#c084a0', b: '#9090c8', c: '#0a84ff', idle: 'rgba(255,255,255,.34)' };
  }
  const cs = getComputedStyle(root);
  const v = (name, fb) => ((cs.getPropertyValue(name) || '').trim() || fb);
  const a = v('--pp-svc', v('--red', '#c084a0'));
  return {
    a: a,
    b: v('--purple', '#9090c8'),
    c: v('--blue', '#0a84ff'),
    idle: v('--rw-idle', 'rgba(255,255,255,.34)')
  };
}

function _rwClamp01(x) {
  x = Number(x);
  if (!isFinite(x)) return 0;
  return x < 0 ? 0 : (x > 1 ? 1 : x);
}

function _rwRect(g, x, y, w, h, r) {
  if (g.roundRect) { g.beginPath(); g.roundRect(x, y, w, h, r); g.fill(); }
  else g.fillRect(x, y, w, h);
}

// Отрисовка в canvas. Сыгранная часть — фирменным градиентом со слабым
// свечением, оставшаяся — приглушённым нейтральным цветом; между ними не резкая
// граница, а плавный переход шириной `soft` (доля ширины). Волна и есть полоса
// перемотки, отдельной полосы в референсе нет.
//
// Масштаб: число столбиков считается от ШИРИНЫ, а холст поднимается под
// devicePixelRatio, поэтому на телефоне и на 4K картинка одинаково резкая.
function rwDraw(cv, opt) {
  if (!cv || !cv.getContext) return;
  const o = opt || {};
  const dprRaw = (typeof window !== 'undefined' && window.devicePixelRatio) || 1;
  const dpr = Math.max(1, Math.min(3, o.dpr || dprRaw));
  const cssW = Math.max(1, o.width || cv.clientWidth || cv.width || 300);
  const cssH = Math.max(1, o.height || cv.clientHeight || cv.height || 44);
  const pxW = Math.round(cssW * dpr), pxH = Math.round(cssH * dpr);
  if (cv.width !== pxW) cv.width = pxW;
  if (cv.height !== pxH) cv.height = pxH;

  const g = cv.getContext('2d');
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.clearRect(0, 0, cssW, cssH);

  const bw = Math.max(1, o.barWidth || 2);
  const gap = Math.max(0.5, o.barGap != null ? o.barGap : 1.5);
  const step = bw + gap;
  const n = rwBarCount(cssW, bw, gap);
  const bars = rwBars(o.artist, o.title, o.duration, n);

  const prog = _rwClamp01(o.progress);
  // Буфер не может быть «позади» сыгранного — то, что прозвучало, загружено.
  const buf = Math.max(prog, _rwClamp01(o.buffered));
  const pal = o.palette || rwPalette();
  const soft = Math.max(1 / n, o.soft != null ? Number(o.soft) : 0.05);
  const pad = o.pad != null ? Number(o.pad) : 2;
  // centerY — где ось волны. Мини-док растянут на всю высоту плеера (вместе с
  // раскрытой панелью), а волна должна сидеть на строке кнопок, а не в
  // геометрическом центре чего-то, чего пользователь центром не считает.
  const mid = o.centerY != null ? Number(o.centerY) : cssH / 2;
  const room = Math.min(mid, cssH - mid) * 2 - pad * 2;
  const maxH = Math.max(2, Math.min(room, o.band != null ? Number(o.band) : room));

  const grad = g.createLinearGradient(0, 0, cssW, 0);
  grad.addColorStop(0, pal.a);
  grad.addColorStop(0.55, pal.b);
  grad.addColorStop(1, pal.c);

  const glow = o.glow != null ? Number(o.glow) : 5;
  const r = Math.min(bw / 2, 2);

  for (let i = 0; i < n; i++) {
    const x = i * step;
    const xc = (i + 0.5) / n;
    const h = Math.max(2, bars[i] * maxH);
    const y = mid - h / 2;
    // Плавная граница: доля «сыгранности» этого столбика. Ровно на позиции
    // курсора — 0.5, дальше плавно к 0/1 на ширине soft.
    const mixed = _rwClamp01((prog - xc) / soft + 0.5);

    if (mixed < 0.998) {
      // Несыгранное. Загруженное в буфер — заметно, ещё не загруженное — бледнее:
      // старая полоса показывала кэш отдельной планкой, и эта информация не
      // должна была пропасть вместе с планкой.
      g.globalAlpha = (1 - mixed) * (xc <= buf ? 1 : 0.5);
      g.fillStyle = pal.idle;
      _rwRect(g, x, y, bw, h, r);
    }
    if (mixed > 0.002) {
      g.globalAlpha = mixed;
      g.fillStyle = grad;
      if (glow > 0) { g.shadowColor = pal.a; g.shadowBlur = glow; }
      _rwRect(g, x, y, bw, h, r);
      g.shadowBlur = 0;
    }
  }
  g.globalAlpha = 1;
  g.shadowBlur = 0;
}

// Клик и перетаскивание по волне = перемотка. Нужно тем, кто рисует волну на
// СВОЁМ холсте (демо design/v003). В живом приложении холст лежит ВНУТРИ уже
// существующей полосы #fp-progress / #pp-progress под pointer-events:none, и
// перемотку там делает штатный _wireSeekBar в player.js — второй перемотки
// рядом со штатной заводить нельзя.
function rwAttachSeek(cv, onSeek) {
  if (!cv || typeof onSeek !== 'function') return;
  const pos = (ev) => {
    const rect = cv.getBoundingClientRect();
    const x = (ev.touches ? ev.touches[0].clientX : ev.clientX) - rect.left;
    return Math.max(0, Math.min(1, x / (rect.width || 1)));
  };
  let down = false;
  cv.addEventListener('pointerdown', (e) => {
    down = true;
    // Перемотка ПЕРВОЙ, захват указателя — после и под try. Порядок не
    // косметический: `setPointerCapture` бросает NotFoundError, если pointerId
    // не принадлежит живому указателю, и тогда исключение убивало обработчик
    // ДО самой перемотки. Клик по волне молча не работал, а причина выглядела
    // как «волна не реагирует» — то есть не там, где она была.
    onSeek(pos(e));
    try { cv.setPointerCapture?.(e.pointerId); } catch (_) {}
  });
  cv.addEventListener('pointermove', (e) => { if (down) onSeek(pos(e)); });
  cv.addEventListener('pointerup', (e) => {
    down = false;
    try { cv.releasePointerCapture?.(e.pointerId); } catch (_) {}
  });
  cv.addEventListener('pointercancel', () => { down = false; });
}

// Явный экспорт в window: файл подключается обычным <script>, а объявления
// через function на верхнем уровне в браузере на window ПОПАДАЮТ, но полагаться
// на это в коде, который читают люди, не стоит.
if (typeof window !== 'undefined') {
  window.rwSeed = rwSeed;
  window.rwBars = rwBars;
  window.rwBarCount = rwBarCount;
  window.rwPalette = rwPalette;
  window.rwDraw = rwDraw;
  window.rwAttachSeek = rwAttachSeek;
}
// Для харнесса в node: файл читается и исполняется как есть, без сборщика.
if (typeof module !== 'undefined' && module.exports) {
  module.exports = { rwSeed, rwBars, rwBarCount, rwPalette, rwDraw, rwAttachSeek };
}
