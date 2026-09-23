/* ============================================================================
   Ripster — мобильная панель (трекер #37).

   Живёт в ОТДЕЛЬНОМ OS-ОКНЕ, которое создаёт Python (launcher_exe.py, адрес
   с ?rpwin=1): только так панель переживает сворачивание Ripster в трей —
   DOM главного окна умирает вместе с прятанием, окно — нет. Старый путь
   floatPlayer()/Document-PiP/window.open в WebView2 задушен молча — сюда
   никогда не возвращаемся.

   ТРИ СРЕДЫ, ОДИН ТРАНСПОРТ (см. rpSend/onHost ниже):
   • OS-окно лаунчера (?rpwin=1) — обмен через pywebview.api.host() ↔
     panel_push; мост к плееру окна ПК.
   • док-iframe внутри окна ПК (обычный браузер, где OS-окно создать нельзя)
     — postMessage тому же origin; тот же мост, тот же протокол.
   • отдельная вкладка/телефон — моста нет; панель играет своим <audio> и
     честно об этом говорит в строке источника.
   Собственного плеера там, где есть мост, панель НЕ имеет: состояние
   читается из плеера окна, команды идут через его же функции
   (static/js/panel_host.js). Данные — живые эндпоинты того же сервера
   (тот же origin → та же сессия и те же куки), никаких заглушек.
   ========================================================================== */
'use strict';

/* ── Язык ─────────────────────────────────────────────────────────────────── */
var LANGS = ['ru', 'en', 'hi', 'ja', 'zh'];
var S = {
  lang: (function () {
    try { return localStorage.getItem('ripster-lang') || 'ru'; } catch (e) { return 'ru'; }
  })(),
  quality: 'mp3',
  queue: [], idx: -1, shuffle: false, repeat: 'off',
  station: null, svc: 'deezer', fav: {},
  api: 0, apiErr: 0, wsEv: 0, wsOpen: false, lastErr: '',
  mirror: null, env: '', kind: 'tracks',
  lib: null, libQ: '', pinned: false
};
if (LANGS.indexOf(S.lang) < 0) S.lang = 'en';

function t(k) {
  var L = (typeof LANG !== 'undefined') ? LANG : {};
  if (L[S.lang] && L[S.lang][k] != null) return L[S.lang][k];
  if (L.en && L.en[k] != null) return L.en[k];
  if (L.ru && L.ru[k] != null) return L.ru[k];
  return k;
}
function ti(k, p) {
  return String(t(k)).replace(/\{(\w+)\}/g, function (_, n) {
    return (p && p[n] != null) ? p[n] : '{' + n + '}';
  });
}
// Число со склонением. Формы в ключе через «|» в порядке категорий
// Intl.PluralRules: ru — one|few|many («1 трек|2 трека|5 треков»), en — one|other.
// Раньше ключ был один на все числа, и плитка радара писала «1 треков».
function tn(k, n) {
  var forms = String(t(k)).split('|');
  var num = Number(n);
  if (!isFinite(num)) return forms[forms.length - 1].replace('{n}', '?');
  var cat = 'other';
  try { cat = new Intl.PluralRules(S.lang || 'ru').select(num); } catch (e) {}
  var order = (forms.length >= 3) ? ['one', 'few', 'many'] : ['one', 'other'];
  var i = order.indexOf(cat);
  if (i < 0) i = forms.length - 1;          // 'other' у дробных в ru и т. п.
  return (forms[i] || forms[forms.length - 1]).replace('{n}', String(num));
}
function applyLang() {
  document.documentElement.lang = S.lang;
  document.title = t('m.app.title');
  document.querySelectorAll('[data-i18n]').forEach(function (el) {
    el.textContent = t(el.getAttribute('data-i18n'));
  });
  document.querySelectorAll('[data-i18n-ph]').forEach(function (el) {
    el.placeholder = t(el.getAttribute('data-i18n-ph'));
  });
  document.querySelectorAll('[data-i18n-title]').forEach(function (el) {
    el.title = t(el.getAttribute('data-i18n-title'));
  });
  renderAll();
}

/* ── Сеть ─────────────────────────────────────────────────────────────────── */
function esc(x) {
  return String(x == null ? '' : x).replace(/[&<>"']/g, function (c) {
    return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
  });
}
async function api(path) {
  S.api++;
  try {
    var r = await fetch(path, { credentials: 'same-origin' });
    if (!r.ok) throw new Error(path.split('?')[0] + ' → HTTP ' + r.status);
    return await r.json();
  } catch (e) {
    S.apiErr++; S.lastErr = String(e.message || e); diag(); throw e;
  }
}
async function apiPost(path, body) {
  S.api++;
  try {
    var r = await fetch(path, {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {})
    });
    if (!r.ok) throw new Error(path.split('?')[0] + ' → HTTP ' + r.status);
    return await r.json();
  } catch (e) {
    S.apiErr++; S.lastErr = String(e.message || e); diag(); throw e;
  }
}

/* ── Форматирование ────────────────────────────────────────────────────────── */
function mmss(sec) {
  sec = Math.max(0, Math.floor(sec || 0));
  var m = Math.floor(sec / 60), s = sec % 60;
  return m + ':' + String(s).padStart(2, '0');
}
function ago(ts) {
  if (!ts) return '—';
  var d = Math.max(0, Date.now() - ts) / 1000;
  if (d < 90) return t('m.ago.now');
  var k = d < 3600 ? 'm.ago.min' : d < 86400 ? 'm.ago.hour' : 'm.ago.day';
  return ti(k, { n: Math.round(d < 3600 ? d / 60 : d < 86400 ? d / 3600 : d / 86400) });
}

/* ── Мост с окном ПК ──────────────────────────────────────────────────────── */
var B = { live: false, caps: {}, state: null, queue: null, stamp: '', up: false };

/* RPWIN — панель в OS-окне лаунчера (Python-хост по ту сторону pywebview).
   IFRAME — доком в окне ПК. Иначе — отдельное окно: моста нет. */
var RPWIN = /[?&]rpwin=1/.test(location.search);
var IFRAME = (function () {
  try { return !!(window.parent && window.parent !== window); } catch (e) { return false; }
})();

function rpApi() {
  if (!RPWIN) return null;
  try {
    var a = window.pywebview && window.pywebview.api;
    return (a && typeof a.host === 'function') ? a : null;
  } catch (e) { return null; }
}
function rpSend(msg) {
  msg.rp = 1;
  var a = rpApi();
  if (a) {
    try {
      var p = a.host(JSON.stringify(msg));
      if (p && p.catch) p.catch(function () {});
      return true;
    } catch (e) { return false; }
  }
  if (RPWIN) return false;          // api ещё не подоспел — не притворяемся
  if (!IFRAME) return false;
  try { window.parent.postMessage(msg, location.origin); return true; } catch (e) { return false; }
}
function cmd(name, arg) { return rpSend({ k: 'cmd', cmd: name, arg: arg || {} }); }
function can(what) { return B.live && B.caps[what]; }

function hostToItem(x) {
  return {
    service: x.service, id: String(x.id || ''), title: x.title, artist: x.artist,
    cover: x.cover, duration: x.duration || 0, label: x.label || '',
    album: x.album || '', hires: !!x.hires, host: true,
    local: !!x.local || x.service === 'local',
    url: x.url || '', resolved: !!x.resolved
  };
}

/* Часы: единственный источник «где мы в треке» для всех сред. */
function clock() {
  if (B.live && B.state) {
    var s = B.state;
    return { pos: s.pos || 0, dur: s.dur || 0, paused: !!s.paused,
             vol: (s.volume == null ? vol() : s.volume), have: !!s.have, engine: s.engine };
  }
  var a = T.el;
  return { pos: a.currentTime || 0, dur: (a.duration && isFinite(a.duration)) ? a.duration : 0,
           paused: a.paused, vol: a.volume, have: !!S.queue[S.idx], engine: 'local' };
}

/* Одно сообщение — два входа: pywebview зовёт rpRecv(строка), док шлёт
   postMessage. Origin и rp-метка проверяются на обоих. */
function onHost(d) {
  if (d.k === 'welcome') {
    B.live = true; B.caps = d.caps || {}; B.up = true; S.env = d.env || 'WebView2?';
    if (B.caps.oswin) S.env = 'WebView2/OS-window';
    readMirror();
    renderAll();
    if (currentScreen() === 'scr-home') renderHome();
    rpSend({ k: 'ready' });
    return;
  }
  if (d.k === 'state') {
    B.live = true; B.state = d;
    if (d.queue !== B.stamp && !B.queue) rpSend({ k: 'need-queue' });
    renderTransport(); renderMini(); diag();
    return;
  }
  if (d.k === 'queue') {
    B.queue = d.items || []; B.stamp = d.stamp || '';
    S.queue = B.queue.map(hostToItem);
    S.idx = (B.state && B.state.have) ? B.state.idx : -1;
    S.station = '';
    renderAll();
    return;
  }
  if (d.k === 'cmd-fail') failToast(ti('m.err.cmd', { x: String(d.cmd) + ': ' + String(d.msg || '') }));
}
window.rpRecv = function (str) {
  var d = null;
  try { d = JSON.parse(str); } catch (e) { return; }
  if (d && d.rp === 1) onHost(d);
};
window.addEventListener('message', function (ev) {
  var d = ev.data;
  if (!d || d.rp !== 1) return;
  try { if (ev.origin !== location.origin) return; } catch (e) { return; }
  if (IFRAME && ev.source !== window.parent) return;
  onHost(d);
});

/* ── Транспорт. Один интерфейс, две реализации: мост (окно ПК) и local
   (панель открыта сама по себе — вкладка браузера, телефон). ────────────── */
var T = {
  el: new Audio(),
  ready: false,

  async resolve(item) {
    if (item.local && item.url) return item.url;   // файл библиотеки уже прямая ссылка
    if (item.url && /^(\/api\/|https?:)/.test(item.url) && item.resolved) return item.url;
    var svc = item.service || 'deezer', id = item.id;
    if (!svc || !id) throw new Error('no ids');
    var name = encodeURIComponent(item.title || ''), art = encodeURIComponent(item.artist || '');
    if (svc === 'deezer') {
      return '/api/stream/deezer/' + id + '?name=' + name + '&artist=' + art +
             '&quality=' + (S.quality === 'mp3' ? '3' : '9');
    }
    var qp = svc === 'qobuz' ? 'format_id=' + (S.quality === 'hires' ? 27 : S.quality === 'lossless' ? 6 : 5)
           : svc === 'tidal'  ? 'quality=' + (S.quality === 'hires' ? 'HI_RES' : S.quality === 'lossless' ? 'LOSSLESS' : 'HIGH')
           : svc === 'soundcloud' ? 'prefer=ctr' : '';
    var d = await api('/api/stream/' + svc + '/' + id + '?name=' + name + '&artist=' + art + (qp ? '&' + qp : ''));
    if (!d.url) throw new Error(d.error || d.detail || 'no url');
    return d.url;
  },

  async play(index) {
    if (can('setQueue')) { cmd('playIndex', { i: index }); return; }
    if (index < 0 || index >= S.queue.length) return;
    S.idx = index;
    this._want = index;
    var it = S.queue[index];
    renderPlayer(); renderQueueSheets();
    try {
      var url = await this.resolve(it);
      if (this._want !== index) return;                 // за время сети кликнули другой
      this.el.src = url;
      this.el.volume = vol();
      await this.el.play();
      this.ready = true;
    } catch (e) {
      S.lastErr = String(e.message || e);
      failToast(ti('m.err.stream', { x: String(e.message || e) }));
    }
    diag();
  },

  /* Запустить В ОКНЕ ПК то, что панель насобирала сама (жанр, поиск, библиотека). */
  launch(items, index) {
    if (can('setQueue')) { cmd('setQueue', { items: items, idx: index || 0 }); openPlayer(); return; }
    S.queue = items.map(hostToItem); S.idx = -1; S.station = '';
    openPlayer(); renderAll(); this.play(index || 0);
  },
  append(items) {
    if (can('enqueue')) { cmd('enqueue', { items: items }); return; }
    S.queue = S.queue.concat(items.map(hostToItem));
    renderQueueSheets(); renderMini();
    if (S.idx < 0) this.play(0);
  },
  insertNext(items) {
    if (can('playNext')) { cmd('playNext', { items: items }); return; }
    failToast(t('m.err.nocmd'));
  },

  toggle() {
    if (can('toggle')) { cmd('toggle'); return; }
    if (!this.el.src) { if (S.queue.length) this.play(Math.max(0, S.idx)); return; }
    if (this.el.paused) this.el.play().catch(function (e) { S.lastErr = String(e.message); diag(); });
    else this.el.pause();
  },
  next() {
    if (can('next')) { cmd('next'); return; }
    if (!S.queue.length) return;
    var i;
    if (S.repeat === 'one') { i = S.idx; }
    else if (S.shuffle) { i = S.queue.length > 1 ? (S.idx + 1 + Math.floor(Math.random() * (S.queue.length - 1))) % S.queue.length : 0; }
    else i = S.idx + 1;
    if (i >= S.queue.length) { if (S.repeat === 'all') i = 0; else { this.el.pause(); this.el.currentTime = 0; return; } }
    this.play(i);
  },
  prev() {
    if (can('prev')) { cmd('prev'); return; }
    if (!S.queue.length) return;
    if (this.el.currentTime > 3) { this.el.currentTime = 0; return; }
    this.play(S.idx > 0 ? S.idx - 1 : S.queue.length - 1);
  },
  seekTo(frac) {
    frac = Math.min(0.999, Math.max(0, frac));
    if (B.live) {
      var dur = (B.state && B.state.dur) || 0;
      if (!dur) { failToast(t('m.err.seek_nodur')); return; }
      cmd('seek', { sec: frac * dur });
      return;
    }
    if (!this.el.duration) return;
    this.el.currentTime = frac * this.el.duration;
    syncTime();
  },
  volume(v) {
    if (B.live) { cmd('volume', { v: v }); return; }
    vol(v); this.el.volume = vol();
  },
  remove(i) {
    if (can('remove')) { cmd('removeAt', { i: i }); return; }
    if (i === S.idx) { this.el.pause(); this.el.removeAttribute('src'); S.queue.splice(i, 1); this.play(Math.min(i, S.queue.length - 1)); }
    else { S.queue.splice(i, 1); if (i < S.idx) S.idx--; }
    renderPlayer(); renderQueueSheets(); renderMini();
  },
  /* Что реально играет: ссылка локального <audio> или путь движка окна ПК. */
  srcLine() {
    if (B.live && B.state) return (B.state.engine || '') + ' · #' + (B.state.idx + 1) + '/' + B.state.qlen;
    return this.el.currentSrc || this.el.src || '';
  }
};
T.el.preload = 'metadata';
T.el.addEventListener('ended', function () { T.next(); });
T.el.addEventListener('timeupdate', syncTime);
T.el.addEventListener('durationchange', syncTime);
T.el.addEventListener('loadedmetadata', function () { renderPlayer(); diag(); });
T.el.addEventListener('play', function () { renderTransport(); diag(); });
T.el.addEventListener('pause', function () { renderTransport(); diag(); });

function vol(v) {
  if (B.live) return (B.state && B.state.volume != null) ? B.state.volume : 0.8;
  if (v != null) {
    try { localStorage.setItem('ripster-panel-volume', String(v)); } catch (e) {}
  }
  var cur;
  try { cur = parseFloat(localStorage.getItem('ripster-panel-volume')); } catch (e) { cur = NaN; }
  return isNaN(cur) ? 0.8 : Math.min(1, Math.max(0, cur));
}

/* ── Источник очереди без моста: живой снимок окна ПК ──────────────────────── */
/* Основной плеер пишет в localStorage последний трек вместе со slim-очередью
   (player.js). Панель — тот же origin, значит читает это без правок в чужом
   файле. Это реальное состояние ПК, а не фиктивные данные. */
function readMirror() {
  try {
    var raw = localStorage.getItem('ripster_last_track');
    S.mirror = raw ? JSON.parse(raw) : null;
  } catch (e) { S.mirror = null; }
  if (S.mirror && S.mirror.ts && Date.now() - S.mirror.ts > 7 * 86400000) S.mirror = null;
}

/* ── Данные ───────────────────────────────────────────────────────────────── */
async function loadConfig() {
  try {
    var c = await api('/api/config');
    S.quality = c['player-stream-quality'] || 'mp3';
    S.engine = c.engine || '?';
    S.lossless = c.quality || '?';
  } catch (e) { /* конфиг не критичен: панель живёт и без него */ }
}

/* ── Главная: визуальный язык мобильного Ripster (03_home.png) ────────────── */
/* Жанры — те же имена и запросы, что в android_app (WaveStations.kt); градиенты
   плитки подобраны под сливово-розовую палитру. */
var GENRES = [
  ['Minimal',      'minimal techno', '#3B2A55', '#6C4E96'],
  ['Blues',        'blues',          '#1E3A5F', '#2F6DA8'],
  ['New Wave',     'new wave',       '#5A1E46', '#B0306E'],
  ['Synthwave',    'synthwave',      '#43125E', '#D91A8C'],
  ['Lo-Fi',        'lofi hip hop',   '#4A2C1A', '#A86A3B'],
  ['Ambient',      'ambient',        '#16403F', '#2F8F7A'],
  ['Techno',       'techno',         '#26262E', '#5C5C6E'],
  ['Disco',        'disco',          '#5E1A3A', '#E0559B'],
  ['Soul',         'soul',           '#4A1F1F', '#9E4A3B'],
  ['Indie',        'indie rock',     '#2E4423', '#7A9E4F'],
  ['Drum & Bass',  'drum and bass',  '#123A4A', '#2E8FA8'],
  ['Deep House',   'deep house',     '#2A1B4A', '#7A4FB0']
];

function homeShell() {
  return '<div class="greet">' + esc(t('m.greet')) + '</div>' +
    '<div class="greet-sub">' + esc(t('m.greet_sub')) + '</div>' +
    '<div id="home-now"></div>' +
    '<div class="sect-label">' + esc(t('m.home.genres')) + '</div>' +
    '<div class="sect-sub">' + esc(t('m.home.genres_sub')) + '</div>' +
    '<div class="gtiles" id="home-genres"></div>' +
    '<div id="home-digs"></div>' +
    '<div class="sect-label">' + esc(t('m.home.bbc')) + '</div>' +
    '<div id="home-bbc"></div>' +
    '<div id="home-find"></div>';
}

function renderHome() {
  var el = document.getElementById('home-body');
  if (!el.querySelector('.greet')) el.innerHTML = homeShell();
  renderHomeNow();
  renderHomeGenres();
  renderHomeDigs();
  renderHomeFind();
  loadHomeBbc();
}

/* «Играет сейчас» — живое состояние окна ПК, когда мост есть. Когда его нет —
   снимок очереди плеера из localStorage того же origin. Ключ-дедуп: состояние
   прилетает каждые 400 мс, перерисовывать карточку на каждый тик нельзя. */
var _homeKey = '';
function renderHomeNow() {
  var box = document.getElementById('home-now');
  if (!box) { _homeKey = ''; return; }
  var key;
  if (B.live && B.state && B.state.have && B.state.item) {
    var it = B.state.item;
    key = 'now:' + it.service + ':' + it.id + ':' + (B.state.paused ? 'p' : 'r') + ':' + B.state.qlen;
  } else {
    readMirror();
    key = (S.mirror && Array.isArray(S.mirror.queue) && S.mirror.queue.length)
      ? 'res:' + S.mirror.ts + ':' + S.mirror.queue.length : 'none';
  }
  if (key === _homeKey) return;
  _homeKey = key;
  box.innerHTML = rpNowCard();
  var rb = document.getElementById('resume-btn');
  if (rb) rb.onclick = resumeFromMirror;
  var nb = document.getElementById('now-btn');
  if (nb) nb.onclick = function () { openPlayer(); renderPlayer(); };
}

function rpNowCard() {
  if (B.live && B.state && B.state.have && B.state.item) {
    var it = B.state.item, c = clock();
    return '<div class="sect-label">' + esc(t('m.home.now')) + '</div>' +
      '<button class="resume now" id="now-btn">' +
      '<img src="' + esc(it.cover || '') + '" alt="">' +
      '<span><span class="rt">' + esc(it.title || '') + '</span>' +
      '<span class="rs">' + esc(ti('m.home.now_sub', {
        artist: it.artist || '', n: B.state.qlen, pos: c.dur ? mmss(c.pos) + ' / ' + mmss(c.dur) : mmss(0),
        state: c.paused ? t('m.home.now_paused') : t('m.home.now_playing')
      })) + '</span></span></button>';
  }
  if (!B.live && S.mirror && Array.isArray(S.mirror.queue) && S.mirror.queue.length) {
    var m = S.mirror;
    return '<div class="sect-label">' + esc(t('m.home.continue')) + '</div>' +
      '<button class="resume" id="resume-btn">' +
      '<img src="' + esc(m.cover || '') + '" alt="">' +
      '<span><span class="rt">' + esc(m.title || '') + '</span>' +
      '<span class="rs">' + esc(ti('m.home.continue_sub', { n: m.queue.length, when: ago(m.ts) })) + '</span></span></button>';
  }
  return '';
}

function resumeFromMirror() {
  var m = S.mirror; if (!m) return;
  S.queue = (m.queue || []).map(function (x) {
    return {
      service: x.service, id: String(x.id || ''), title: x.title, artist: x.artist,
      cover: x.cover, posKey: x.posKey, full: x.full !== false,
      local: !!x.local || x.service === 'local',
      url: (x.url && /^\/api\/library\/file\?/.test(x.url)) ? x.url : '',
      resolved: !!x.resolved
    };
  }).filter(function (x) { return x.id; });
  S.station = m.ctx || t('m.home.continue');
  openPlayer();
  T.play(Math.min(m.idx | 0, S.queue.length - 1));
}

function renderHomeGenres() {
  var box = document.getElementById('home-genres'); if (!box) return;
  box.innerHTML = GENRES.map(function (g, i) {
    return '<button class="gtile" data-g="' + i + '" style="--g1:' + g[2] + ';--g2:' + g[3] + '">' +
      '<span class="gplay">▶</span><span class="gname">' + esc(g[0]) + '</span></button>';
  }).join('');
  box.querySelectorAll('[data-g]').forEach(function (b) {
    b.onclick = function () { playGenre(+b.getAttribute('data-g'), b); };
  });
}

/* Тап по жанру собирает поток живым /api/search (тот же сервис, что выбран в
   поиске; SoundCloud у /api/search не спрашиваем — он там не живёт). */
async function playGenre(i, tile) {
  var g = GENRES[i]; if (!g) return;
  var mark = tile.querySelector('.gplay') || tile.querySelector('.gspin');
  if (mark) mark.outerHTML = '<span class="gspin"></span>';
  try {
    var svc = (S.svc === 'soundcloud') ? 'deezer' : S.svc;
    var d = await api('/api/search?q=' + encodeURIComponent(g[1]) +
                      '&service=' + encodeURIComponent(svc) + '&type=track&limit=30');
    var items = (d.results || []).map(toQueueItem);
    if (!items.length) failToast(t('m.search.none'));
    else { S.station = g[0]; T.launch(items, 0); }
  } catch (e) {
    failToast(ti('m.err.stream', { x: String(e.message || e) }));
  }
  if (tile.isConnected) renderHomeGenres();
}

function renderHomeDigs() {
  var box = document.getElementById('home-digs'); if (!box) return;
  box.innerHTML = '<button class="digsrow" id="digs-btn">' +
    '<span class="chip-pink"></span>' +
    '<span class="dtext">' + esc(t('m.home.digs')) + '</span>' +
    '<span class="darr">→</span></button>';
  document.getElementById('digs-btn').onclick = function () { openInView('digs'); };
}

/* «Открыть в Ripster» делает само окно ПК: у него сессия, история и настройки
   вью. Без моста кнопка не притворяется рабочей — говорит об этом прямо. */
function openInView(name) {
  if (B.live && can('openView')) { cmd('openView', { name: name }); return; }
  failToast(t('m.err.nocmd'));
}

function renderHomeFind() {
  var box = document.getElementById('home-find'); if (!box) return;
  box.innerHTML = '<div class="findwrap"><button class="findpill" id="find-btn">⌕ ' +
    esc(t('m.home.find')) + '</button></div>';
  document.getElementById('find-btn').onclick = function () {
    showScreen('scr-search');
    var q = document.getElementById('sq'); if (q) q.focus();
  };
}

var BBC_BRAND = 'b006wkfp';   // тот же бренд, что крутит вкладка BBC окна ПК
async function loadHomeBbc() {
  var box = document.getElementById('home-bbc'); if (!box) return;
  var d;
  try { d = await api('/api/bbc/episodes?brand_id=' + BBC_BRAND + '&limit=12'); }
  catch (e) { box.innerHTML = '<div class="hint">' + esc(S.lastErr) + '</div>'; return; }
  var items = d.items || [];
  if (!items.length) { box.innerHTML = '<div class="empty">' + esc(t('m.search.none')) + '</div>'; return; }
  var week = 7 * 86400000;
  box.innerHTML = '<div class="bbcrow">' + items.map(function (x, i) {
    var fresh = false;
    var dt = Date.parse(String(x.date || '').slice(0, 10) + 'T00:00:00Z');
    if (!isNaN(dt) && dt >= Date.now() - week) fresh = true;
    return '<div class="bbccard">' +
      '<div class="bart" data-bbc="' + i + '">' +
        (x.image ? '<img src="' + esc(x.image) + '" alt="" loading="lazy">' : '') +
        (fresh ? '<span class="bnew">NEW</span>' : '') +
        '<button class="bdl" data-dl="' + i + '" title="' + esc(t('m.dl.add')) + '">↓</button>' +
      '</div>' +
      '<div class="btitle" data-bbc="' + i + '">' + esc(x.title || '') + '</div>' +
      '<div class="bsub">' + esc(x.channel || x.subtitle || '') + '</div>' +
      '<div class="bdate">' + esc(String(x.date || '').slice(0, 10)) + '</div>' +
    '</div>';
  }).join('') + '</div>';
  box.querySelectorAll('[data-bbc]').forEach(function (b) {
    b.onclick = function (ev) {
      if (ev.target && ev.target.classList.contains('bdl')) return;
      openInView('bbc');
    };
  });
  box.querySelectorAll('[data-dl]').forEach(function (b) {
    b.onclick = function (ev) {
      ev.stopPropagation();
      bbcDownload(items[+b.getAttribute('data-dl')], b);
    };
  });
}

/* Загрузка выпуска — тот же живой POST, что жмёт вкладка BBC окна ПК. */
async function bbcDownload(x, btn) {
  if (!x || btn.classList.contains('done')) return;
  btn.classList.add('busy');
  try {
    await apiPost('/api/bbc/download', { pid: x.pid, vpid: x.vpid, title: x.title, artist: 'BBC Radio' });
    btn.classList.remove('busy');
    btn.classList.add('done');
    btn.textContent = '✓';
    toast(t('m.dl.added'), true);
  } catch (e) {
    btn.classList.remove('busy');
    failToast(ti('m.err.stream', { x: String(e.message || e) }));
  }
}

var _searchTimer = null;
function onSearch() {
  clearTimeout(_searchTimer);
  var q = document.getElementById('sq').value.trim();
  var el = document.getElementById('search-body');
  if (q.length < 2) { el.innerHTML = '<div class="empty">' + esc(t('m.search.hint')) + '</div>'; return; }
  el.innerHTML = '<div class="hint"><span class="spin"></span> ' + esc(t('m.search.loading')) + '</div>';
  _searchTimer = setTimeout(function () { doSearch(q); }, 450);
}

/* /api/search знает apple|deezer|qobuz|tidal|spotify|yandex|beatport|jiosaavn,
   но НЕ soundcloud: для SoundCloud свой поиск со своими карточками (трек там
   может быть миксом с трек-листом в описании). Молча отдать ему «soundcloud»
   значит показать «ничего не нашлось» там, где ответы есть. */
async function doSearch(q) {
  var el = document.getElementById('search-body');
  var mixes = S.kind === 'mixes';
  try {
    var d, rows;
    if (S.svc === 'soundcloud') {
      d = await api('/api/soundcloud/search?q=' + encodeURIComponent(q) +
                    '&kind=' + (mixes ? 'playlists' : 'tracks') + '&limit=30');
      if (d.ok === false) throw new Error(d.error || 'soundcloud');
      rows = (d.results || []).filter(function (x) { return mixes ? x.kind === 'playlist' : x.kind !== 'playlist'; });
    } else {
      d = await api('/api/search?q=' + encodeURIComponent(q) + '&service=' + S.svc +
                    '&type=' + (mixes ? 'album' : 'track') + '&limit=20');
      rows = d.results || [];
    }
    el._rows = rows;
    if (!rows.length) { el.innerHTML = '<div class="empty">' + esc(t('m.search.none')) + '</div>'; return; }
    el.innerHTML = rows.map(function (x, i) {
      var isMix = mixes || x.kind === 'playlist';
      return rowHtml({
        r1: x.title, r2: x.artist,
        r3: isMix ? tn('m.search.ntracks', x.tracks || x.trackCount) : (x.year || x.album || ''),
        cover: x.cover || x.artwork_sm || x.artwork,
        act: isMix
          ? '<button class="ract open" data-open="' + i + '" title="' + esc(t('m.search.open')) + '">›</button>'
          : '<button class="ract play" data-i="' + i + '" title="' + esc(t('m.search.play')) + '">▶</button>' +
            '<button class="ract" data-add="' + i + '" title="' + esc(t('m.search.add')) + '">＋</button>' +
            (x.has_tracklist ? '<button class="ract" data-tl="' + i + '" title="' + esc(t('m.mix.title')) + '">≡</button>' : '')
      });
    }).join('');
    bindSearchRows(el, rows, mixes);
  } catch (e) {
    el.innerHTML = '<div class="empty">' + esc(t('m.err.net')) + '<div class="hint">' + esc(S.lastErr) + '</div></div>';
  }
}

function bindSearchRows(el, rows, mixes) {
  el.querySelectorAll('[data-i]').forEach(function (b) {
    b.onclick = function (ev) { ev.stopPropagation(); playFound(rows[+b.getAttribute('data-i')], false); };
  });
  el.querySelectorAll('[data-add]').forEach(function (b) {
    b.onclick = function (ev) { ev.stopPropagation(); playFound(rows[+b.getAttribute('data-add')], true); };
  });
  el.querySelectorAll('[data-open], [data-tl]').forEach(function (b) {
    b.onclick = function (ev) {
      ev.stopPropagation();
      openCollection(rows[+(b.getAttribute('data-open') != null ? b.getAttribute('data-open') : b.getAttribute('data-tl'))]);
    };
  });
  el.querySelectorAll('.row').forEach(function (r, i) {
    var x = rows[i]; if (!x) return;
    r.onclick = function () { (mixes || x.kind === 'playlist') ? openCollection(x) : playFound(x, false); };
    r.oncontextmenu = function (ev) {
      ev.preventDefault();
      var acts = [];
      if (mixes || x.kind === 'playlist') acts.push([t('m.search.open'), function () { openCollection(x); }]);
      else {
        acts.push([t('m.search.play'), function () { playFound(x, false); }]);
        acts.push([t('m.search.add'), function () { playFound(x, true); }]);
        acts.push([t('m.mix.next'), function () { T.insertNext([toQueueItem(x)]); }]);
      }
      if (x.has_tracklist) acts.push([t('m.mix.title'), function () { openCollection(x); }]);
      menuAt(ev, acts);
    };
  });
}

function toQueueItem(x) {
  return {
    service: x.service || (S.svc === 'soundcloud' ? 'soundcloud' : S.svc),
    id: String(x.id), title: x.title, artist: x.artist,
    cover: x.cover || x.artwork_sm || x.artwork || '',
    duration: x.duration || 0, album: x.album || '', label: x.label || '',
    permalink: x.url || '', url: '', resolved: false
  };
}
function playFound(x, append) {
  if (!x) return;
  var it = toQueueItem(x);
  if (append) { T.append([it]); return; }
  T.launch([it], 0);
}

/* Трек-лист микса/альбома. Три живых источника, никаких заглушек:
   • SoundCloud-плейлист  → /api/soundcloud/playlist/{id}
   • трек-микс с таймкодами в описании → трек-лист уже приехал в карточке
     поиска (`tracklist`), при необходимости /api/soundcloud/tracklist/{id}
   • релиз любого другого сервиса → /api/album/{service}/{id} */
async function openCollection(x) {
  var svc = x.service || S.svc;
  var title = x.title || '';
  openSheet('collection', title);
  var body = document.getElementById('sheet-body');
  body.innerHTML = '<div class="hint"><span class="spin"></span> ' + esc(t('m.mix.loading')) + '</div>';
  try {
    var tracks = [], timed = null;
    if (svc === 'soundcloud' && x.kind !== 'playlist') {
      var tl = x.tracklist;
      if (!tl || !tl.length) {
        var dd = await api('/api/soundcloud/tracklist/' + encodeURIComponent(x.id));
        tl = (dd && dd.tracklist) || [];
      }
      if (tl && tl.length) {
        timed = tl;
      } else {
        var p = await api('/api/soundcloud/playlist/' + encodeURIComponent(x.id));
        tracks = (p.tracks || []).map(function (tr) {
          return { service: 'soundcloud', id: String(tr.id), title: tr.title, artist: tr.artist,
                   cover: tr.artwork_sm || tr.artwork || x.cover || x.artwork_sm || '', duration: tr.duration || 0 };
        });
      }
    } else if (svc === 'soundcloud') {
      var pl = await api('/api/soundcloud/playlist/' + encodeURIComponent(x.id));
      tracks = (pl.tracks || []).map(function (tr) {
        return { service: 'soundcloud', id: String(tr.id), title: tr.title, artist: tr.artist,
                 cover: tr.artwork_sm || tr.artwork || x.cover || x.artwork_sm || '', duration: tr.duration || 0 };
      });
    } else {
      var a = await api('/api/album/' + encodeURIComponent(svc) + '/' + encodeURIComponent(x.id));
      var ainfo = a.album || a.release || {};
      tracks = (a.tracks || []).map(function (tr) {
        return { service: svc, id: String(tr.id), title: tr.title, artist: tr.artist || ainfo.artist,
                 cover: tr.cover || ainfo.cover || x.cover || '', duration: tr.duration || 0,
                 album: ainfo.title || title };
      });
    }
    if (timed) { renderMixTimeline(body, timed, x); return; }
    if (!tracks.length) { body.innerHTML = '<div class="empty">' + esc(t('m.mix.empty')) + '</div>'; return; }
    body.innerHTML =
      '<div class="collbar">' +
        '<button class="cta" id="c-play">' + esc(ti('m.mix.play_all', { n: tracks.length })) + '</button>' +
        '<button class="ctb" id="c-next">' + esc(t('m.mix.next')) + '</button>' +
      '</div>' +
      tracks.map(function (tr, i) {
        return rowHtml({
          idx: tr.track_no || (i + 1), cover: tr.cover, r1: tr.title,
          r2: [tr.artist, tr.album].filter(Boolean).join(' · '),
          r3: String(tr.service || svc).toUpperCase(),
          dur: tr.duration ? mmss(tr.duration) : ''
        });
      }).join('');
    body.querySelector('#c-play').onclick = function () { T.launch(tracks.map(toQueueItem), 0); };
    body.querySelector('#c-next').onclick = function () { T.insertNext(tracks.map(toQueueItem)); };
    body.querySelectorAll('.row').forEach(function (r, i) {
      r.onclick = function () { T.launch(tracks.map(toQueueItem), i); };
      r.oncontextmenu = function (ev) {
        ev.preventDefault();
        menuAt(ev, [[t('m.search.play'), function () { T.launch(tracks.map(toQueueItem), i); }],
                   [t('m.mix.play_all'), function () { T.launch(tracks.map(toQueueItem), 0); }]]);
      };
    });
  } catch (e) {
    body.innerHTML = '<div class="empty">' + esc(t('m.mix.fail')) + '<div class="hint">' + esc(String(e.message || e)) + '</div></div>';
  }
}
/* Таймлайн диджейского микса: сами треки не стримятся (они внутри одного
   файла), но перемотать окно ПК на отметку — можно. Кнопка есть только там,
   где перемотка доступна: лайв-эфир и BBC без длительности отматываются
   отказом, а не молчанием. */
function renderMixTimeline(body, tl, x) {
  body.innerHTML =
    '<div class="mixnote">' + esc(ti('m.mix.note', { n: tl.length })) + '</div>' +
    tl.map(function (r, i) {
      return rowHtml({
        idx: i + 1, r1: [r.artist, r.title].filter(Boolean).join(' — ') || r.title || '—',
        r3: r.timestamp ? '' : t('m.mix.no_ts'), dur: r.timestamp || ''
      });
    }).join('');
  body.querySelectorAll('.row').forEach(function (r, i) {
    var ts = tl[i] && tl[i].timestamp;
    if (!ts) { r.classList.add('dim'); return; }
    r.onclick = function () { seekToLabel(ts); };
    r.title = t('m.mix.seek_hint');
  });
  void x;
}

function tsToSec(ts) {
  var p = String(ts || '').split(':').map(function (n) { return parseInt(n, 10) || 0; });
  if (p.length === 3) return p[0] * 3600 + p[1] * 60 + p[2];
  if (p.length === 2) return p[0] * 60 + p[1];
  return p[0] || 0;
}
function seekToLabel(ts) {
  var sec = tsToSec(ts), dur = (B.state && B.state.dur) || 0;
  if (!B.live) { failToast(t('m.mix.seek_nobridge')); return; }
  if (!dur) { failToast(t('m.err.seek_nodur')); return; }
  if (sec >= dur) { failToast(ti('m.mix.seek_over', { dur: mmss(dur) })); return; }
  cmd('seek', { sec: sec });
}

/* ── Библиотека: файлы с дисков, тех же, что сканирует окно ПК ────────────── */
async function loadLibrary() {
  var el = document.getElementById('lib-body');
  if (!S.lib) {
    el.innerHTML = '<div class="hint"><span class="spin"></span> ' + esc(t('m.lib.load')) + '</div>';
    try {
      var d = await api('/api/library/scan');
      S.lib = d.items || [];
    } catch (e) {
      el.innerHTML = '<div class="empty">' + esc(t('m.err.net')) + '<div class="hint">' + esc(S.lastErr) + '</div></div>';
      return;
    }
  }
  renderLibrary();
}

/* Файл библиотеки — самодостаточный элемент очереди: url уже прямая ссылка на
   раздатчик, резолвить его не у кого (у сервиса его нет). */
function libQueueItem(x) {
  return {
    service: 'local', local: true, id: String(x.id),
    title: x.title, artist: x.artist, album: x.album || '',
    duration: Number(x.duration) || 0,
    cover: x.has_cover ? '/api/library/cover/' + encodeURIComponent(x.id) : '',
    url: '/api/library/file?p=' + encodeURIComponent(x.path),
    posKey: 'lib:' + x.id, resolved: true
  };
}

function renderLibrary() {
  var el = document.getElementById('lib-body'); if (!el) return;
  var q = S.libQ.toLowerCase();
  var rows = (S.lib || []).filter(function (x) {
    return !q || ((x.title || '') + ' ' + (x.artist || '') + ' ' + (x.album || '') + ' ' + (x.rel || '')).toLowerCase().indexOf(q) >= 0;
  });
  document.getElementById('lib-sum').textContent = ti('m.lib.sum', { n: (S.lib || []).length });
  if (!rows.length) { el.innerHTML = '<div class="empty">' + esc(t('m.lib.none')) + '</div>'; return; }
  el.innerHTML = rows.slice(0, 150).map(function (x) {
    return rowHtml({
      cover: x.has_cover ? '/api/library/cover/' + encodeURIComponent(x.id) : '',
      r1: x.title, r2: [x.artist, x.album].filter(Boolean).join(' · '),
      r3: String(x.ext || 'file').toUpperCase().replace(/^\./, '') + (x.year ? ' · ' + x.year : ''),
      dur: x.duration ? mmss(x.duration) : ''
    });
  }).join('') +
    (rows.length > 150 ? '<div class="hint">' + esc(ti('m.lib.more', { n: rows.length - 150 })) + '</div>' : '');
  el.querySelectorAll('.row').forEach(function (r, i) {
    var x = rows[i]; if (!x) return;
    r.onclick = function () { T.launch([libQueueItem(x)], 0); };
    r.title = t('m.search.play');
  });
}

/* ── Радар: анонсы и подписки окна ПК (только чтение — играть ещё нечего) ─── */
async function loadRadar() {
  var el = document.getElementById('radar-body');
  el.innerHTML = '<div class="hint"><span class="spin"></span> ' + esc(t('m.loading')) + '</div>';
  var up = null, wl = null;
  try { up = await api('/api/releases/upcoming'); } catch (e) {}
  try { wl = await api('/api/watchlist'); } catch (e) {}
  var rels = (up && up.releases) || [];
  var watch = (wl && wl.items) || [];
  document.getElementById('radar-sum').textContent = ti('m.radar.sum', { n: rels.length, m: watch.length });
  var html = '<div class="sect-label">' + esc(t('m.radar.upcoming')) + '</div>';
  if (up && up.hint === 'upcoming_off') {
    html += '<div class="empty">' + esc(t('m.radar.off')) + '</div>';
  } else if (!rels.length) {
    html += '<div class="empty">' + esc(t('m.radar.none')) + '</div>';
  } else {
    html += rels.slice().sort(function (a, b) {
      return String(a.date || '') < String(b.date || '') ? -1 : 1;
    }).slice(0, 60).map(function (x) {
      return rowHtml({
        cover: x.cover, r1: x.title,
        r2: [x.artist, x.type || x.label].filter(Boolean).join(' · '),
        r3: String(x.service || '').toUpperCase(),
        dur: String(x.date || '').slice(0, 10)
      });
    }).join('');
  }
  html += '<div class="sect-label">' + esc(t('m.radar.watch')) + '</div>';
  html += watch.length
    ? '<div class="chips">' + watch.map(function (w) {
        return '<span class="chip">' + esc(w.name || '') + ' · ' + esc(svcName(w.service || '')) + '</span>';
      }).join('') + '</div>'
    : '<div class="empty">' + esc(t('m.radar.none')) + '</div>';
  el.innerHTML = html;
}

/* svc.* есть не у всех сервисов (watchlist знает и youtube, и bandcamp):
   без перевода называем сервис как он назван в данных, а не ключом словаря. */
function svcName(sv) {
  var k = 'svc.' + sv;
  var tr = t(k);
  return tr === k ? sv : tr;
}

async function loadDownloads() {
  var el = document.getElementById('dl-body');
  try {
    var q = await api('/api/queue');
    if (!q.length) { el.innerHTML = '<div class="empty">' + esc(t('m.dl.none')) + '</div>'; document.getElementById('dl-sum').textContent = ''; return; }
    var run = 0, done = 0, queued = 0;
    q.forEach(function (x) {
      if (x.status === 'done') done++;
      else if (x.status === 'queued' || x.status === 'pending') queued++;
      else run++;
    });
    document.getElementById('dl-sum').textContent = ti('m.dl.sum', { running: run, done: done, queued: queued });
    el.innerHTML = q.slice(0, 40).map(function (x) {
      var m = x.meta || {};
      var p = Math.max(0, Math.min(100, x.progress || 0));
      return '<div class="row" style="cursor:default">' +
        '<img class="rcov" src="' + esc(m.artworkUrl || m.cover || '') + '" alt="">' +
        '<div class="rtext"><div class="r1">' + esc(m.title || m.album || x.url) + '</div>' +
        '<div class="r2">' + esc((m.artist || '') + ' · ' + (x.service || '')) + '</div>' +
        '<div class="r3"><span class="stat ' + esc(x.status) + '">' + esc(dlStatus(x.status)) + '</span> ' +
        '<span class="bar" style="display:inline-block;width:70px;vertical-align:2px"><i style="width:' + p + '%"></i></span></div></div>' +
        '<span class="rdur">' + esc(x.quality || '') + '</span></div>';
    }).join('');
  } catch (e) {
    el.innerHTML = '<div class="empty">' + esc(t('m.err.net')) + '<div class="hint">' + esc(S.lastErr) + '</div></div>';
  }
}
function dlStatus(s) {
  var k = { queued: 'm.dl.queued', pending: 'm.dl.queued', downloading: 'm.dl.downloading',
            done: 'm.dl.done', error: 'm.dl.error', canceled: 'm.dl.canceled', cancelled: 'm.dl.canceled' }[s];
  return k ? t(k) : String(s);
}

/* ── Живые события сервера (WebSocket — в WebView2 работает, в отличие от
   window.open) ──────────────────────────────────────────────────────────── */
var _ws = null, _wsRetry = 0;
function connectWS() {
  var proto = location.protocol === 'https:' ? 'wss://' : 'ws://';
  try { _ws = new WebSocket(proto + location.host + '/ws'); }
  catch (e) { return; }
  _ws.onopen = function () { S.wsOpen = true; _wsRetry = 0; diag(); };
  _ws.onmessage = function (ev) {
    S.wsEv++;
    try {
      var d = JSON.parse(ev.data);
      if (d.type === 'queue_update' || d.type === 'progress') {
        if (document.getElementById('scr-downloads').classList.contains('on')) loadDownloads();
      }
    } catch (e) {}
    diag();
  };
  _ws.onclose = function () {
    S.wsOpen = false;
    _wsRetry = Math.min(30, (_wsRetry || 1) * 2);
    setTimeout(connectWS, _wsRetry * 1000);
    diag();
  };
  _ws.onerror = function () { S.apiErr++; diag(); };
}

/* ── Рендер ───────────────────────────────────────────────────────────────── */
function rowHtml(o) {
  return '<div class="row' + (o.now ? ' now' : '') + '">' +
    (o.idx != null ? '<div class="ridx">' + (o.now ? '<span class="rdot"></span>' : esc(o.idx)) + '</div>' : '') +
    (o.cover ? '<img class="rcov" src="' + esc(o.cover) + '" alt="" loading="lazy">' : '<div class="rcov"></div>') +
    '<div class="rtext"><div class="r1">' + esc(o.r1) + '</div>' +
    '<div class="r2">' + esc(o.r2 || '') + '</div>' +
    (o.r3 ? '<div class="r3 ' + (o.lossless ? 'lossless' : '') + '">' + esc(o.r3) + '</div>' : '') + '</div>' +
    (o.dur ? '<div class="rdur">' + esc(o.dur) + '</div>' : '') +
    (o.act || '') + '</div>';
}

function renderAll() {
  renderTabs(); renderPlayer(); renderMini(); renderQueueSheets(); diag();
}

/* Пять слотов, как в мобильном: Главная · Поиск · Библиотека · Загрузки ·
   Радар. Иконки — инлайн-SVG: эмодзи в WebView2 рендерятся через раз. */
var TAB_ICONS = {
  home: '<svg viewBox="0 0 24 24"><path d="M3 11.5 12 4l9 7.5V20a1 1 0 0 1-1 1h-5v-6h-6v6H4a1 1 0 0 1-1-1z"/></svg>',
  search: '<svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="m16.5 16.5 4.5 4.5"/></svg>',
  library: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="8.5"/><circle cx="12" cy="12" r="2.5"/></svg>',
  downloads: '<svg viewBox="0 0 24 24"><path d="M12 4v11m0 0 4.5-4.5M12 15 7.5 10.5M4 20h16"/></svg>',
  radar: '<svg viewBox="0 0 24 24"><path d="M12 12 19 5"/><path d="M4.5 16a9 9 0 1 1 15-4.5"/><path d="M8 19a9 9 0 0 1-3.5-3"/></svg>'
};
var TABS = [['home', 'm.tab.home'], ['search', 'm.tab.search'], ['library', 'm.tab.library'],
            ['downloads', 'm.tab.downloads'], ['radar', 'm.tab.radar']];

function renderTabs() {
  var nav = document.getElementById('tabbar');
  var active = currentScreen();
  nav.innerHTML = TABS.map(function (d) {
    return '<button class="tab' + (active === 'scr-' + d[0] ? ' on' : '') + '" data-tab="' + d[0] + '">' +
      '<span class="g">' + TAB_ICONS[d[0]] + '</span><span>' + esc(t(d[1])) + '</span></button>';
  }).join('');
  nav.querySelectorAll('[data-tab]').forEach(function (b) {
    b.onclick = function () { showScreen('scr-' + b.getAttribute('data-tab')); };
  });
}

function currentScreen() {
  var on = document.querySelector('.screen.on:not(.player)');
  return on ? on.id : 'scr-home';
}
function showScreen(id) {
  document.querySelectorAll('.screen').forEach(function (s) {
    if (s.id !== 'scr-player' && s.id !== 'sheet') s.classList.toggle('on', s.id === id);
  });
  renderTabs();
  if (id === 'scr-home') renderHome();
  if (id === 'scr-downloads') loadDownloads();
  if (id === 'scr-library') { if (S.lib) renderLibrary(); else loadLibrary(); }
  if (id === 'scr-radar') radarShow();
  if (id === 'scr-search' && !document.getElementById('search-body').innerHTML) {
    document.getElementById('search-body').innerHTML =
      '<div class="empty">' + esc(t('m.search.hint')) + '</div>';
  }
}
function openPlayer() { document.getElementById('scr-player').classList.add('on'); }
function closePlayer() { document.getElementById('scr-player').classList.remove('on'); }

function specLine(it) {
  if (!it) return '';
  var q = S.quality === 'hires' ? 'Hi-Res' : S.quality === 'lossless' ? 'FLAC' : 'MP3 320';
  var bits = [(it.service || ''), q];
  if (it.hires) bits.push(t('m.p.hires'));
  if (it.local) bits.push('LOCAL');
  return bits.filter(Boolean).join(' · ').toUpperCase();
}

var ENGINE_KEY = { native: 'm.eng.native', gapless: 'm.eng.gapless', bbc: 'm.eng.bbc',
                   paired: 'm.eng.paired', audio: 'm.eng.audio', local: 'm.eng.local' };

function renderPlayer() {
  var it = S.queue[S.idx];
  var body = document.getElementById('player-body');
  if (!it) {
    body.innerHTML = '<button class="pgrab" id="p-close" title="' + esc(t('m.close')) + '"></button>' +
      '<div class="pcoverwrap"><div class="pcover" style="display:grid;place-items:center;color:var(--t4);font-size:30px">♪</div></div>' +
      '<div class="pmeta"><div class="ptitle"><span>' + esc(t('m.p.empty')) + '</span></div>' +
      '<div class="partist">' + esc(t(B.live ? 'm.p.empty_pc' : 'm.p.empty_hint')) + '</div></div>';
    var cc = document.getElementById('p-close');
    if (cc) cc.onclick = closePlayer;
    return;
  }
  var key = it.service + ':' + it.id;
  var navOk = !B.live || can('next');
  body.innerHTML =
    '<button class="pgrab" id="p-close" title="' + esc(t('m.close')) + '"></button>' +
    '<div class="pcoverwrap"><img class="pcover" id="p-cover" src="' + esc(it.cover || '') + '" alt="" title="' + esc(t('m.p.zoom')) + '"></div>' +
    '<div class="pmeta">' +
      '<div class="ptitle"><span>' + esc(it.title || '') + '</span>' +
        '<button class="pfav' + (S.fav[key] ? ' on' : '') + '" id="p-fav" title="' +
          esc(t(B.live && can('fav') ? 'm.p.fav_pc' : 'm.p.favorite')) + '">' + (S.fav[key] ? '♥' : '♡') + '</button></div>' +
      '<div class="partist">' + esc([it.artist, it.album].filter(Boolean).join(' · ')) + '</div>' +
      '<div class="pfmt"><span class="svc">' + esc(it.service || '') + '</span> · ' + esc(specLine(it)) +
        (S.station ? ' · <span>' + esc(S.station) + '</span>' : '') +
        ' <button class="i" id="p-pass" title="' + esc(t('m.pass.title')) + '">ⓘ</button></div>' +
    '</div>' +
    '<div class="seek" id="p-seek" role="slider" tabindex="0" aria-label="' + esc(t('m.p.seek')) + '">' +
      '<div class="seekrail"><div class="seekbuf" id="p-buf"></div><div class="seekfill" id="p-fill"></div><div class="seekknob" id="p-knob"></div></div>' +
      '<div class="seektimes"><span id="p-cur">0:00</span><span id="p-dur">–:––</span></div>' +
    '</div>' +
    '<div class="transport">' +
      '<button class="tbtn' + (S.shuffle ? ' on' : ' off') + '" id="p-shuf" title="' + esc(shuffleHint()) + '">⤨</button>' +
      '<button class="tbtn" id="p-prev" title="' + esc(t('m.p.prev')) + '">⏮</button>' +
      '<button class="pplay" id="p-play" title="' + esc(t('m.p.play_pause')) + '">⏸</button>' +
      '<button class="tbtn" id="p-next" title="' + esc(t('m.p.next')) + '"' + (navOk ? '' : ' data-na="1"') + '>⏭</button>' +
      '<button class="tbtn' + (S.repeat !== 'off' ? ' on' : ' off') + '" id="p-rep" title="' + esc(repeatHint()) + '">⤁</button>' +
    '</div>' +
    '<div class="tiles">' +
      '<button class="tile" id="t-queue"><span class="g">≡</span>' + esc(t('m.p.queue')) + '</button>' +
      '<button class="tile" id="t-lyrics"><span class="g">🗎</span>' + esc(t('m.p.lyrics')) + '</button>' +
      '<button class="tile" id="t-pass"><span class="g">ⓘ</span>' + esc(t('m.p.passport')) + '</button>' +
    '</div>' +
    '<div class="volrow"><span title="' + esc(t('m.p.vol')) + '">🔈</span>' +
      '<input type="range" id="p-vol" min="0" max="100" value="' + Math.round(vol() * 100) + '" aria-label="' + esc(t('m.p.vol')) + '">' +
      '<span class="pct" id="p-volpct">' + Math.round(vol() * 100) + '%</span></div>' +
    '<div class="srcnote">' + esc(srcNote()) + '</div>';

  body.querySelector('#p-play').textContent = clock().paused ? '▶' : '⏸';
  body.querySelector('#p-cover').onclick = function () {
    var cs = document.getElementById('coverstage');
    cs.innerHTML = '<img src="' + esc(it.cover || '') + '" alt="">';
    cs.classList.add('on');
  };
  body.querySelector('#p-fav').onclick = function () {
    if (B.live && can('fav')) { cmd('fav'); S.fav[key] = true; }
    else S.fav[key] = !S.fav[key];
    try { localStorage.setItem('ripster-panel-fav', JSON.stringify(S.fav)); } catch (e) {}
    renderPlayer();
  };
  bindTransport();
  bindSeek();
  bindVolume();
  syncTime();
}

/* Управление, которого у окна ПК нет, не притворяется рабочим: кнопка гаснет,
   а подсказка говорит почему (правило «настройка, которая ничего не меняет,
   хуже отсутствующей»). */
function shuffleHint() {
  return B.live && !can('shuffle') ? t('m.p.na_pc') : t('m.p.shuffle');
}
function repeatHint() {
  return B.live && !can('repeat') ? t('m.p.na_pc') : t('m.p.repeat');
}
function srcNote() {
  if (B.live) {
    var eng = (B.state && B.state.engine) || 'audio';
    return ti('m.p.src_pc', { x: t(ENGINE_KEY[eng] || 'm.eng.audio') });
  }
  return t('m.p.src.local_note');
}

function bindTransport() {
  var g = function (id) { return document.getElementById(id); };
  var cl = g('p-close'); if (cl) cl.onclick = closePlayer;
  var play = g('p-play'); if (play) play.onclick = function () { T.toggle(); };
  var prev = g('p-prev'); if (prev) prev.onclick = function () { T.prev(); };
  var next = g('p-next'); if (next) next.onclick = function () { T.next(); };
  var sh = g('p-shuf'); if (sh) sh.onclick = function () {
    if (B.live && !can('shuffle')) { failToast(t('m.p.na_pc')); return; }
    S.shuffle = !S.shuffle; renderPlayer();
  };
  var rp = g('p-rep'); if (rp) rp.onclick = function () {
    if (B.live && !can('repeat')) { failToast(t('m.p.na_pc')); return; }
    S.repeat = S.repeat === 'off' ? 'all' : S.repeat === 'all' ? 'one' : 'off'; renderPlayer();
  };
  if (sh && B.live && !can('shuffle')) sh.classList.add('na');
  if (rp && B.live && !can('repeat')) rp.classList.add('na');
  var q = g('t-queue'); if (q) q.onclick = function () { openSheet('queue'); };
  var l = g('t-lyrics'); if (l) l.onclick = function () { openSheet('lyrics'); };
  var ps = g('t-pass'), pp = g('p-pass');
  if (ps) ps.onclick = function () { openSheet('passport'); };
  if (pp) pp.onclick = function () { openSheet('passport'); };
}

function renderTransport() {
  var c = clock();
  var b = document.getElementById('p-play');
  if (b) b.textContent = c.paused ? '▶' : '⏸';
  var mb = document.getElementById('mini-play');
  if (mb) { mb.innerHTML = c.paused ? '&#9654;' : '&#10074;&#10074;'; mb.title = t(c.paused ? 'm.p.play' : 'm.p.pause'); }
  syncTime();
}

function syncTime() {
  var c = clock();
  var cur = c.pos || 0, dur = c.dur || 0;
  var f = document.getElementById('p-fill'), k = document.getElementById('p-knob');
  var cu = document.getElementById('p-cur'), du = document.getElementById('p-dur');
  var pct = dur ? (cur / dur * 100) : 0;
  if (f) f.style.width = pct + '%';
  if (k) k.style.left = pct + '%';
  if (cu) cu.textContent = mmss(cur);
  if (du) du.textContent = dur ? mmss(dur) : '–:––';
  var seek = document.getElementById('p-seek');
  if (seek) { seek.setAttribute('aria-valuemin', '0'); seek.setAttribute('aria-valuemax', String(Math.round(dur)));
              seek.setAttribute('aria-valuenow', String(Math.round(cur))); }
  var buf = document.getElementById('p-buf');
  var a = T.el;
  if (buf && !B.live && a.buffered.length) {
    try { buf.style.width = (a.buffered.end(a.buffered.length - 1) / (dur || 1) * 100) + '%'; } catch (e) {}
  }
  var mp = document.getElementById('mini-prog');
  if (mp) mp.style.width = pct + '%';
}

function bindSeek() {
  var s = document.getElementById('p-seek'); if (!s) return;
  var drag = false;
  function frac(ev) {
    var r = s.querySelector('.seekrail').getBoundingClientRect();
    return Math.min(1, Math.max(0, (ev.clientX - r.left) / r.width));
  }
  function paint(f) {
    var fill = document.getElementById('p-fill'), knob = document.getElementById('p-knob');
    if (fill) fill.style.width = (f * 100) + '%';
    if (knob) knob.style.left = (f * 100) + '%';
  }
  /* pointerdown/pointermove/pointerup — один код и на мышь, и на палец;
     но держим мышь за руку: колесо на полосе = ±15 с, клавиши = ±5 с. */
  s.addEventListener('pointerdown', function (ev) {
    if (ev.button != null && ev.button !== 0) return;   // средняя/правая кнопка не «перемотка»
    drag = true; s.classList.add('drag');
    try { s.setPointerCapture(ev.pointerId); } catch (e) {}
    paint(frac(ev));
    ev.preventDefault();
  });
  s.addEventListener('pointermove', function (ev) { if (drag) paint(frac(ev)); });
  s.addEventListener('pointerup', function (ev) {
    if (!drag) return;
    drag = false; s.classList.remove('drag');
    T.seekTo(frac(ev));
  });
  s.addEventListener('pointercancel', function () { drag = false; s.classList.remove('drag'); });
  s.addEventListener('wheel', function (ev) {
    ev.preventDefault();
    var dur = clock().dur || 0; if (!dur) return;
    var step = (ev.deltaY < 0 ? 15 : -15);
    var now = Math.min(dur - 0.5, Math.max(0, clock().pos + step));
    if (B.live) cmd('seek', { sec: now }); else T.seekTo(now / dur);
  }, { passive: false });
  s.addEventListener('keydown', function (ev) {
    var dur = clock().dur || 0; if (!dur) return;
    var step = ev.key === 'ArrowRight' ? 5 : ev.key === 'ArrowLeft' ? -5 : 0;
    if (!step) return;
    ev.preventDefault();
    var now = Math.min(dur - 0.5, Math.max(0, clock().pos + step));
    if (B.live) cmd('seek', { sec: now }); else T.seekTo(now / dur);
  });
  s.oncontextmenu = function (ev) { ev.preventDefault(); menuAt(ev, [[t('m.seek.restart'), function () { T.seekTo(0); }]]); };
}

function bindVolume() {
  var r = document.getElementById('p-vol'); if (!r) return;
  r.oninput = function () {
    var v = (+r.value) / 100;
    T.volume(v);
    var pct = document.getElementById('p-volpct');
    if (pct) pct.textContent = Math.round(v * 100) + '%';
    diag();
  };
  r.title = t('m.p.vol');
}

function renderMini() {
  var m = document.getElementById('mini'), it = S.queue[S.idx];
  if (!it) {
    m.classList.remove('on');
    if (!B.live && !S.mirror) _homeKey = '';   // снимок обновился — карточку можно заново
    renderHomeNow();
    return;
  }
  m.classList.add('on');
  var c = clock();
  document.getElementById('mini-cover').src = it.cover || '';
  document.getElementById('mini-title').textContent = it.title || '';
  document.getElementById('mini-artist').textContent = [it.artist, it.service].filter(Boolean).join(' · ');
  var mb = document.getElementById('mini-play');
  mb.innerHTML = c.paused ? '&#9654;' : '&#10074;&#10074;';
  mb.title = t(c.paused ? 'm.p.play' : 'm.p.pause');
  mb.onclick = function (ev) { ev.stopPropagation(); T.toggle(); };
  m.onclick = function () { openPlayer(); renderPlayer(); };
  m.title = t('m.mini.open');
  renderHomeNow();                             // «играет сейчас» на главной — живое
}

function renderQueueSheets() {
  if (_sheetKind === 'queue') fillQueueSheet();
}

var _sheetKind = '';
function openSheet(kind, title) {
  _sheetKind = kind;
  var sh = document.getElementById('sheet');
  sh.classList.add('on');
  document.getElementById('sheet-title').textContent =
    title || t(kind === 'queue' ? 'm.q.title' : kind === 'lyrics' ? 'm.p.lyrics'
             : kind === 'collection' ? 'm.mix.title' : kind === 'settings' ? 'm.settings.title'
             : 'm.pass.title');
  if (kind === 'queue') fillQueueSheet();
  else if (kind === 'lyrics') fillLyrics();
  else if (kind === 'settings') fillSettings();
  else if (kind === 'collection') { /* наполнит openCollection */ }
  else fillPassport();
}
function closeSheet() { document.getElementById('sheet').classList.remove('on'); _sheetKind = ''; }

function fillQueueSheet() {
  var body = document.getElementById('sheet-body');
  if (!S.queue.length) { body.innerHTML = '<div class="empty">' + esc(t('m.q.empty')) + '</div>'; return; }
  var removable = !B.live || can('remove');
  body.innerHTML = S.queue.map(function (x, i) {
    return rowHtml({
      now: i === S.idx, idx: i + 1, cover: x.cover, r1: x.title,
      r2: [x.artist, x.label].filter(Boolean).join(' · '),
      r3: String(x.service || '').toUpperCase() + (x.duration ? ' · ' + mmss(x.duration) : ''),
      act: removable ? '<button class="ract" data-rm="' + i + '" title="' + esc(t('m.q.remove')) + '">✕</button>' : ''
    });
  }).join('');
  body.querySelectorAll('[data-rm]').forEach(function (b) {
    b.onclick = function (ev) { ev.stopPropagation(); T.remove(+b.getAttribute('data-rm')); };
  });
  body.querySelectorAll('.row').forEach(function (r, i) {
    r.onclick = function () { T.play(i); };
    r.title = t('m.q.play');
    r.oncontextmenu = function (ev) {
      ev.preventDefault();
      menuAt(ev, [[t('m.q.play'), function () { T.play(i); }],
                  [t('m.q.remove'), function () { T.remove(i); }]]);
    };
  });
}

/* Настройки панели — только то, что реально работает: качество стримидёт в
   окно ПК, «поверх всех окон» есть лишь в OS-окне, вью открывает само окно. */
function fillSettings() {
  var body = document.getElementById('sheet-body');
  var html =
    '<div class="setrow"><div><div class="sl">' + esc(t('m.set.quality')) + '</div>' +
    '<div class="sh">' + esc(t('m.set.quality_hint')) + '</div></div></div>' +
    '<div class="chips" id="set-qual"></div>';
  if (RPWIN && rpApi()) {
    html += '<div class="setrow"><div><div class="sl">' + esc(t('m.set.pin')) + '</div>' +
      '<div class="sh">' + esc(t('m.set.pin_hint')) + '</div></div>' +
      '<button class="sw' + (S.pinned ? ' on' : '') + '" id="set-pin" title="' + esc(t('m.set.pin')) + '"><i></i></button></div>';
  }
  if (can('openView')) {
    html += '<button class="setrow" id="go-digs"><span class="sl">' + esc(t('m.set.digs')) + '</span><span aria-hidden="true">›</span></button>' +
      '<button class="setrow" id="go-bbc"><span class="sl">' + esc(t('m.set.bbc')) + '</span><span aria-hidden="true">›</span></button>' +
      '<button class="setrow" id="go-pc"><span class="sl">' + esc(t('m.set.pc')) + '</span><span aria-hidden="true">›</span></button>';
  }
  html += '<div class="setrow"><div><div class="sl">' + esc(t('m.set.lang')) + '</div></div></div>' +
    '<div class="chips" id="set-lang"></div>' +
    '<div class="hint">' + esc(t('m.set.note')) + '</div>';
  body.innerHTML = html;
  renderChips('set-qual', ['mp3', 'lossless', 'hires'], S.quality, function (v) {
    S.quality = v;
    cmd('quality', { q: v });
  }, 'm.qual.');
  renderChips('set-lang', ['ru', 'en'], S.lang, function (v) {
    try { localStorage.setItem('ripster-lang', v); } catch (e) {}
    S.lang = v;
    applyLang();
  });
  var pin = document.getElementById('set-pin');
  if (pin) pin.onclick = function () {
    S.pinned = !S.pinned;
    pin.classList.toggle('on', S.pinned);
    var a = rpApi();
    try { if (a) a.pin(S.pinned); } catch (e) {}
  };
  var b1 = document.getElementById('go-digs'); if (b1) b1.onclick = function () { openInView('digs'); };
  var b2 = document.getElementById('go-bbc'); if (b2) b2.onclick = function () { openInView('bbc'); };
  var b3 = document.getElementById('go-pc'); if (b3) b3.onclick = function () { openInView('settings'); };
}

/* Контекстное меню по ПРАВОЙ кнопке: на мыши это главный способ «сделать с
   строкой что-то ещё», и он не должен прятаться за долгим нажатием. */
function menuAt(ev, acts) {
  var m = document.getElementById('ctxmenu');
  m.innerHTML = acts.map(function (a, i) {
    return '<button data-mi="' + i + '">' + esc(a[0]) + '</button>';
  }).join('');
  var x = ev.clientX != null ? ev.clientX : ev.pageX;
  var y = ev.clientY != null ? ev.clientY : ev.pageY;
  m.style.left = Math.min(x, window.innerWidth - 210) + 'px';
  m.style.top = Math.min(y, window.innerHeight - 30 - acts.length * 34) + 'px';
  m.classList.add('on');
  m.querySelectorAll('[data-mi]').forEach(function (b) {
    b.onclick = function () {
      m.classList.remove('on');
      var fn = acts[+b.getAttribute('data-mi')] && acts[+b.getAttribute('data-mi')][1];
      if (fn) fn();
    };
  });
}
document.addEventListener('mousedown', function (ev) {
  var m = document.getElementById('ctxmenu');
  if (m && m.classList.contains('on') && !m.contains(ev.target)) m.classList.remove('on');
});

async function fillLyrics() {
  var it = S.queue[S.idx], body = document.getElementById('sheet-body');
  if (!it) { closeSheet(); return; }
  body.innerHTML = '<div class="hint"><span class="spin"></span> ' + esc(t('m.l.loading')) + '</div>';
  try {
    var p = new URLSearchParams({ artist: it.artist || '', track: it.title || '' });
    if (it.duration) p.set('duration', String(Math.round(it.duration)));
    if (it.service === 'deezer') p.set('deezer_id', String(it.id));
    if (it.service === 'tidal') p.set('tidal_id', String(it.id));
    var d = await api('/api/lyrics?' + p.toString());
    var txt = (d.plain && d.plain.trim()) || (d.synced && d.synced.replace(/\[[^\]]*\]/g, '').trim());
    body.innerHTML = txt
      ? '<div class="lyric">' + esc(txt) + '</div>' +
        (d.source ? '<div class="hint">' + esc(String(d.source)) + '</div>' : '')
      : '<div class="empty">' + esc(t('m.l.none')) + '</div>';
  } catch (e) {
    body.innerHTML = '<div class="empty">' + esc(t('m.err.net')) + '</div>';
  }
}

async function fillPassport() {
  var it = S.queue[S.idx], body = document.getElementById('sheet-body');
  if (!it) { closeSheet(); return; }
  var rows = [
    [t('m.pass.service'), String(it.service || '—').toUpperCase()],
    [t('m.pass.quality'), specLine(it)],
    [t('m.pass.id'), String(it.id || '—')],
    [t('m.pass.dur'), mmss(clock().dur || it.duration || 0)],
    [t('m.pass.src'), T.srcLine().slice(0, 72) || '—']
  ];
  var a = null;
  try { a = await api('/api/audio/state'); } catch (e) {}
  var eng = a && a.available
    ? (a.playing ? t('m.pass.playing') : t('m.pass.idle')) +
      (a.bit_perfect ? ' · bit-perfect ' + a.bits + '/' + a.file_rate : '')
    : 'n/a';
  body.innerHTML = '<div class="pass">' +
    rows.map(function (r) { return '<div><b>' + esc(r[0]) + ':</b> ' + esc(r[1]) + '</div>'; }).join('') +
    '<div style="margin-top:10px"><b>' + esc(t('m.pass.engine')) + ':</b> ' + esc(eng) + '</div>' +
    '<div style="margin-top:6px"><b>' + esc(t('m.pass.host')) + ':</b> ' +
      esc(B.live ? t('m.pass.host.bridge') : t('m.pass.host.local')) + ' · ' + esc(S.env || '?') + '</div>' +
    '</div>';
}

/* ── Мини-строка диагностики: чем её заполнять честнее, тем меньше споров ─── */
var _diagOn = false;
function diag() {
  var el = document.getElementById('dipline'); if (!el || !_diagOn) return;
  var c = clock();
  var env = RPWIN ? 'WebView2/OS-window' : IFRAME ? 'dock' : 'browser';
  var bridge = B.live ? 'live (' + c.engine + ')'
             : (RPWIN || IFRAME) ? ((RPWIN ? 'oswin' : 'dock') + ', no answer')
             : 'standalone';
  el.innerHTML = '<b>' + esc(t('m.dip.title')) + '</b>\n' +
    t('m.dip.env') + ': ' + env + '\n' +
    t('m.dip.bridge') + ': ' + esc(bridge) + '\n' +
    t('m.dip.caps') + ': ' + Object.keys(B.caps).filter(function (k) { return B.caps[k]; }).join(' ') + '\n' +
    t('m.dip.api') + ': ' + S.api + (S.apiErr ? '  ' + t('m.dip.err') + ': ' + S.apiErr : '') + '\n' +
    t('m.dip.ws') + ': ' + (S.wsOpen ? 'open' : 'closed') + ' · ' + S.wsEv + '\n' +
    t('m.dip.audio') + ': ' + (c.have ? (c.paused ? 'paused' : 'playing') : 'idle') +
    ' @ ' + (c.pos || 0).toFixed(2) + 's/' + (c.dur || 0).toFixed(0) + 's' +
    ' · vol ' + Math.round((c.vol || 0) * 100) + '%\n' +
    (S.queue.length ? 'queue: ' + S.queue.length + ' · idx ' + S.idx + '\n' : '') +
    (S.lastErr ? '<span class="bad">' + esc(S.lastErr.slice(0, 120)) + '</span>' : '');
}
function toast(msg, ok) {
  var el = document.createElement('div');
  el.setAttribute('role', ok ? 'status' : 'alert');
  el.textContent = String(msg).replace(/<[^>]*>/g, '');
  el.style.cssText = 'position:absolute;left:24px;right:24px;bottom:120px;z-index:80;padding:10px 12px;' +
    'border-radius:var(--r-ctl);background:var(--overlay);border:1px solid ' + (ok ? 'var(--ok)' : 'var(--bad)') +
    ';color:var(--t1);font:var(--caption)/1.4 var(--font)';
  document.getElementById('phone').appendChild(el);
  setTimeout(function () { el.remove(); }, 4200);
}
function failToast(msg) {
  S.lastErr = String(msg);
  toast(msg, false);
  diag();
}

/* Падение панели не должно выглядеть как «окно ПК сломался»: говорим хосту,
   что умерли МЫ, и он показывает причину рядом с живым плеером. */
function reportFatal(msg) {
  S.lastErr = String(msg);
  rpSend({ k: 'fatal', msg: String(msg).slice(0, 240) });
}
window.addEventListener('error', function (ev) { reportFatal((ev.message || 'error') + ' @' + String(ev.filename || '').split('/').pop() + ':' + ev.lineno); });
window.addEventListener('unhandledrejection', function (ev) {
  var r = ev.reason;
  reportFatal('promise: ' + String((r && r.message) || r || ''));
});

/* ── Перезапуск языка по изменению в окне ПК (тот же origin) ──────────────── */
window.addEventListener('storage', function (ev) {
  if (ev.key === 'ripster-lang') {
    try { S.lang = localStorage.getItem('ripster-lang') || S.lang; } catch (e) {}
    applyLang();
  }
  if (ev.key === 'ripster_last_track' && !B.live) {
    readMirror(); _homeKey = '';
    if (currentScreen() === 'scr-home') renderHomeNow();
  }
});

/* ── Клавиши: панель живёт под мышью, и с клавиатуры тоже ────────────────── */
function onKey(ev) {
  if (ev.target && /INPUT|TEXTAREA/.test(ev.target.tagName)) {
    if (ev.key === 'Escape') {
      var sq = document.getElementById('sq'), lq = document.getElementById('lq');
      if (ev.target === sq || ev.target === lq) ev.target.blur();
    }
    return;
  }
  var c = clock();
  if (ev.code === 'Space') { ev.preventDefault(); T.toggle(); }
  else if (ev.key === 'ArrowRight') { nudge(5, c); }
  else if (ev.key === 'ArrowLeft') { nudge(-5, c); }
  else if (ev.key === 'ArrowUp') { ev.preventDefault(); volStep(0.05, c); }
  else if (ev.key === 'ArrowDown') { ev.preventDefault(); volStep(-0.05, c); }
  else if (ev.key === '+' || ev.key === '=') { volStep(0.05, c); }
  else if (ev.key === '-' || ev.key === '_') { volStep(-0.05, c); }
  else if (ev.key === 'Escape') { closeSheet(); closePlayer(); }
  else if (ev.key === 'd' || ev.key === 'D' || ev.key === 'в' || ev.key === 'В') {
    _diagOn = !_diagOn;
    document.documentElement.classList.toggle('diag', _diagOn);
    diag();
  }
}
function nudge(step, c) {
  if (!c.dur) { failToast(t('m.err.seek_nodur')); return; }
  var to = Math.min(c.dur - 0.5, Math.max(0, c.pos + step));
  if (B.live) cmd('seek', { sec: to }); else T.seekTo(to / c.dur);
}
function volStep(step, c) {
  var v = Math.min(1, Math.max(0, (c.vol != null ? c.vol : 0.8) + step));
  T.volume(v);
  var r = document.getElementById('p-vol'), pct = document.getElementById('p-volpct');
  if (r) r.value = String(Math.round(v * 100));
  if (pct) pct.textContent = Math.round(v * 100) + '%';
}

/* ── Рукопожатие с окном ПК ───────────────────────────────────────────────── */
/* В OS-окне pywebview.api подоспевает асинхронно: опрос 250 мс (до 20 с) плюс
   событие pywebviewready. Если хост не ответил за 5 с — панели нечего выдавать
   за «мост»: остаёмся на локальном <audio> и говорим об этом. Спрятанное окно
   шлёт bye (хост перестает слать состояние), показанное — hello заново. */
var _hs = false;
function rpHandshake() {
  var sent = rpSend({ k: 'hello' });
  if (!sent) return false;
  if (!_hs) {
    _hs = true;
    setTimeout(function () {
      if (!B.live) { S.lastErr = 'host silent'; renderAll(); diag(); }
    }, 5000);
  }
  return true;
}
document.addEventListener('visibilitychange', function () {
  if (!RPWIN) return;
  if (document.hidden) rpSend({ k: 'bye' });
  else if (_hs) rpSend({ k: 'hello' });
});

/* ── Старт ────────────────────────────────────────────────────────────────── */
document.addEventListener('DOMContentLoaded', async function () {
  var embedded = RPWIN || IFRAME;
  _diagOn = !embedded;                       // в окне ПК диагностика по «d»
  document.documentElement.classList.add(embedded ? 'embedded' : 'standalone');
  if (RPWIN) document.body.classList.add('rpwin');
  try { S.fav = JSON.parse(localStorage.getItem('ripster-panel-fav') || '{}') || {}; } catch (e) { S.fav = {}; }
  document.getElementById('sq').addEventListener('input', onSearch);
  document.getElementById('lq').addEventListener('input', function () {
    S.libQ = this.value.trim();
    renderLibrary();
  });
  document.getElementById('ab-search').onclick = function () {
    showScreen('scr-search');
    document.getElementById('sq').focus();
  };
  document.getElementById('ab-settings').onclick = function () { openSheet('settings'); };
  document.getElementById('sheet-back').onclick = closeSheet;
  document.getElementById('coverstage').onclick = function () { this.classList.remove('on'); };
  document.getElementById('coverstage').title = t('m.cover_close');

  renderChips('svc-chips', ['deezer', 'qobuz', 'tidal', 'spotify', 'apple', 'soundcloud'], S.svc, function (v) {
    S.svc = v;
    if (document.getElementById('sq').value.trim().length > 1) doSearch(document.getElementById('sq').value.trim());
  });
  renderChips('kind-chips', ['tracks', 'mixes'], S.kind, function (v) {
    S.kind = v;
    if (document.getElementById('sq').value.trim().length > 1) doSearch(document.getElementById('sq').value.trim());
  }, 'm.kind.');

  document.addEventListener('keydown', onKey);
  setInterval(function () { if (!B.live && !T.el.paused) syncTime(); }, 500);
  setInterval(diag, 2000);

  applyLang();
  await loadConfig();
  showScreen('scr-home');
  connectWS();
  setInterval(function () { if (document.getElementById('scr-downloads').classList.contains('on')) loadDownloads(); }, 5000);

  if (IFRAME) rpHandshake();
  if (RPWIN) {
    var tries = 0;
    var poll = setInterval(function () {
      if (rpApi() && rpHandshake()) { clearInterval(poll); return; }
      if (++tries > 80) clearInterval(poll);         // 20 с без api — простоиваем честно
    }, 250);
    window.addEventListener('pywebviewready', function () {
      clearInterval(poll); tries = 1e9;
      rpHandshake();
    });
  }
});

function renderChips(id, values, active, onPick, prefix) {
  var box = document.getElementById(id); if (!box) return;
  box.innerHTML = values.map(function (v) {
    return '<button class="chip' + (v === active ? ' on' : '') + '" data-v="' + esc(v) + '">' +
      esc(t((prefix || '') + v)) + '</button>';
  }).join('');
  box.querySelectorAll('[data-v]').forEach(function (b) {
    b.onclick = function () {
      var v = b.getAttribute('data-v');
      box.querySelectorAll('.chip').forEach(function (x) { x.classList.toggle('on', x === b); });
      onPick(v);
    };
  });
}
