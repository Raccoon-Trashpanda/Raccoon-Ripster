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
// Берём самый ЧАСТЫЙ насыщенный тон: пиксели грубо квантуются, почти серые и
// почти чёрные/белые отбрасываются, побеждает самая населённая корзина. Если
// насыщенных тонов нет вовсе (чёрно-белая обложка) — честно возвращаем null и
// карточка остаётся как была, вместо серой каши.
//
// ЧИТАЕМОСТЬ ВАЖНЕЕ КРАСОТЫ. Цвет уходит только в рамку и еле заметный ореол;
// ни текст, ни фон карточки им не красятся. Насыщенность и светлота
// приводятся в коридор, поэтому кислотная обложка не выжигает глаза, а тёмная
// не сливается с фоном. Цвет здесь — акцент рядом с содержимым, а не заливка
// под ним.
//
// ЦЕНА. Считаем один раз на обложку, в оффскрин-канве 24×24 (это ~576 пикселей,
// доли миллисекунды), результат кладём в память и в localStorage — при
// следующем открытии радара сеть и канва уже не нужны. Картинка для замера
// берётся ОТДЕЛЬНАЯ, с crossOrigin — видимая <img> не трогается вообще, чтобы
// нельзя было сломать показ обложек. Витрина без CORS просто не даст цвет,
// и это нормально: карточка останется обычной.
// ======================================================================

const _CT_KEY = 'ripster_cover_tints';
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

// Самый частый насыщенный тон обложки. null — насыщенных тонов нет.
function _ctDominant(img) {
  const N = 24;
  const c = document.createElement('canvas');
  c.width = c.height = N;
  const ctx = c.getContext('2d', { willReadFrequently: true });
  ctx.drawImage(img, 0, 0, N, N);
  let data;
  try { data = ctx.getImageData(0, 0, N, N).data; }
  catch (e) { return null; }        // витрина без CORS — канва «запачкана»
  const bins = new Map();
  for (let i = 0; i < data.length; i += 4) {
    if (data[i + 3] < 128) continue;
    const r = data[i], g = data[i + 1], b = data[i + 2];
    const [h, s, l] = _ctRgbToHsl(r, g, b);
    // Почти серое и крайние по светлоте не несут тона — они бы и победили,
    // потому что фон обложек чаще всего именно такой.
    if (s < 0.22 || l < 0.12 || l > 0.9) continue;
    const key = Math.round(h / 12);   // корзины по 12° — тон, а не оттенок
    const cur = bins.get(key) || { n: 0, h: 0, s: 0, l: 0 };
    cur.n++; cur.h += h; cur.s += s; cur.l += l;
    bins.set(key, cur);
  }
  let best = null;
  bins.forEach(v => { if (!best || v.n > best.n) best = v; });
  if (!best || best.n < 12) return null;      // тона почти нет — не выдумываем
  return { h: best.h / best.n, s: best.s / best.n, l: best.l / best.n };
}

// Коридор читаемости: акцент должен быть виден и на тёмной, и на светлой теме,
// но не выжигать. Возвращает строку hsl.
function _ctReadable(t) {
  const dark = !document.documentElement.matches('[data-theme="light"]');
  const s = Math.min(0.72, Math.max(0.34, t.s));
  const l = dark ? Math.min(0.68, Math.max(0.46, t.l))
                 : Math.min(0.52, Math.max(0.32, t.l));
  return `hsl(${Math.round(t.h)} ${Math.round(s * 100)}% ${Math.round(l * 100)}%)`;
}

// Цвет обложки. Всегда промис; null — цвета нет и красить нечем.
function coverTint(url) {
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
      done(t ? _ctReadable(t) : null);
    };
    img.onerror = () => done(null);   // нет CORS/картинки — карточка остаётся обычной
    img.src = url;
  });
}

// Покрасить карточки, которые этого ещё не получили. Идёт по видимым — цвет
// нужен там, куда человек смотрит, а не во всей ленте из сотен карточек.
function tintVisibleCards(root) {
  const scope = root || document;
  const cards = scope.querySelectorAll('.rel-card:not([data-tinted])');
  if (!cards.length) return;
  cards.forEach(card => {
    const img = card.querySelector('img');
    const src = img && (img.dataset.lightboxSrc || img.src);
    if (!src) { card.dataset.tinted = 'no'; return; }
    card.dataset.tinted = '1';
    coverTint(src).then(col => {
      if (!col) return;
      card.style.setProperty('--tint', col);
      card.classList.add('tinted');
    });
  });
}

// Панель поиска красится цветом того, кого ищут: видно, чей результат на экране.
// Смена мягкая — за неё отвечает переход в CSS, здесь только значение.
function tintSearchPanel(coverUrl) {
  const host = document.getElementById('view-search') || document.querySelector('.view-search');
  if (!host) return;
  if (!coverUrl) { host.classList.remove('tinted'); host.style.removeProperty('--tint'); return; }
  coverTint(coverUrl).then(col => {
    if (!col) { host.classList.remove('tinted'); return; }
    host.style.setProperty('--tint', col);
    host.classList.add('tinted');
  });
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
  const grid = () => document.getElementById('releases-grid');
  const obs = new MutationObserver(kick);
  const attach = () => {
    const g = grid();
    if (g && !g.dataset.tintWatch) {
      g.dataset.tintWatch = '1';
      obs.observe(g, { childList: true, subtree: false });
      kick();
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
  if (!coverUrl) { host.classList.remove('tinted'); host.style.removeProperty('--tint'); return; }
  coverTint(coverUrl).then(col => {
    if (!col) { host.classList.remove('tinted'); return; }
    host.style.setProperty('--tint', col);
    host.classList.add('tinted');
  });
}
