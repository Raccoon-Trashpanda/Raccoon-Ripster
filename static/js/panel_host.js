/* ============================================================================
   Хост мобильной панели (трекер #37).

   Три транспорта, протокол сообщений ОДИН:
   • «oswin» — отдельное OS-окно, созданное Python-ом (pywebview). Единственный
     способ, которым панель живёт при свёрнутом Ripster: DOM главного окна
     умирает вместе с прятанием в трей, окно — нет. Старый путь `floatPlayer()`
     (Document-PiP + window.open) в WebView2 задушен без предупреждения, поэтому
     окно создаёт лаунчер, а не страница. Обмен: панель → pywebview.api.host →
     Python → rpHostRecv() здесь; обратно — pywebview.api.panel_push →
     rpRecv() в окне панели.
   • «iframe» — док внутри окна. Деградация для запуска в обычном браузере:
     там OS-окно создать нельзя в принципе, и док — честный максимум.
   • «relay» — окно панели (?transport=relay) живёт СВОИМ процессом (standalone
     ripster.player_window или OS-окно лаунчера), а главная страница — в обычной
     вкладке браузера, где pywebview нет. Сообщения перекладывает сервер через
     общий /ws (ripster/rp_relay.py): хост объявляет роль 'host', панель —
     'panel'. Аудио по-прежнему принадлежит только главной странице.

   Собственного плеера панель НЕ имеет: состояние читается из плеера окна,
   команды идут через его же публичные функции. Правок в player.js нет — отказ
   панели физически не может ни остановить звук, ни сломать плеер.
   ========================================================================== */

var RP = {
  open: false, ready: false, mode: 'dock', frame: null, root: null,
  tick: null, watchdog: null, seq: 0, err: '', queueSent: null,
  _extBusy: false,
  transport: 'iframe'          // 'iframe' | 'oswin' | 'relay'
};

var RP_SRC = '/static/panel/index.html?v=4';

/* pywebview-мост есть только в окне лаунчера. */
function rpApi() {
  try {
    var a = window.pywebview && window.pywebview.api;
    return (a && typeof a.mobile_panel === 'function') ? a : null;
  } catch (e) { return null; }
}

/* ── i18n-глушилка: хост грузится позже i18n.js, но раньше всех тостов ───── */
function rpT(key, params) {
  var s;
  try { s = (typeof t === 'function') ? t(key) : key; } catch (e) { s = key; }
  if (params) for (var k in params) s = String(s).split('{' + k + '}').join(params[k]);
  return s;
}

/* ── Открытие / закрытие ─────────────────────────────────────────────────── */
function rpPanelToggle() {
  var api = rpApi();
  if (api) { rpPanelToggleOswin(api); return; }
  if (RP.open) rpPanelClose(); else rpPanelOpen();
}

/* OS-окно: решение «показать/спрятать» принимает Python (он знает видимость).
   Хост лишь заводит цикл рассылки; панель сама пришлёт hello, когда оживёт. */
function rpPanelToggleOswin(api) {
  if (RP.open && RP.ready) {
    try { api.mobile_panel('0'); } catch (e) {}
    RP.open = false; RP.ready = false; RP.transport = 'oswin';
    rpNavMark(false); rpPanelStopTick();
    return;
  }
  var res;
  try { res = api.mobile_panel('1'); } catch (e) { res = 'fail'; }
  if (res && typeof res.then === 'function') {
    res.then(function (r) { rpOswinAdopt(r, api); }, function () { rpOswinAdopt('fail', api); });
    return;
  }
  rpOswinAdopt(res, api);
}

function rpOswinAdopt(res, api) {
  if (res !== 'window') {
    /* Окно не получилось (webview дохлый и т.п.) — честный док поверх. */
    rpPanelOpen('dock');
    try { (window.__rpToast || function () {})(rpT('panel.err.nowin')); } catch (e) {}
    return;
  }
  RP.transport = 'oswin';
  RP.open = true;
  rpNavMark(true);
  rpPanelStartTick();
}

/* Принять хост-сторону при reload главного окна: панель живёт, а RP.* обнулились.
   via — какой канал принёс hello: именно он и становится транспортом (relay
   переживает перезагрузку вкладки так же, как oswin — перезагрузку окна). */
function rpAdoptFromPanel(via) {
  RP.transport = (via === 'relay') ? 'relay' : 'oswin';
  RP.open = true; RP.ready = true; RP.err = ''; RP.queueSent = null;
  rpNavMark(true);
  rpPanelStartTick();
}

function rpPanelStartTick() {
  if (!RP.tick) RP.tick = setInterval(rpPanelPush, 400);
}
function rpPanelStopTick() {
  if (RP.tick) { clearInterval(RP.tick); RP.tick = null; }
}

function rpPanelOpen(mode) {
  if (RP.open) { if (mode) rpPanelSetMode(mode); return; }
  RP.open = true; RP.ready = false; RP.err = ''; RP.queueSent = null;
  RP.transport = 'iframe';
  RP.mode = (mode === 'focus') ? 'focus' : 'dock';

  var root = document.createElement('div');
  root.id = 'rp-panel-root';
  root.className = 'rp-root rp-' + RP.mode;
  var title = rpEsc(rpT('panel.title'));
  var hint = rpEsc(rpT('panel.mode_hint'));
  var ctitle = rpEsc(rpT('panel.close'));
  root.innerHTML =
    '<div class="rp-backdrop" id="rp-backdrop"></div>' +
    '<section class="rp-frame" id="rp-frame" role="dialog" aria-label="' + title + '">' +
      '<header class="rp-bar">' +
        '<span class="rp-name">' + title + '</span>' +
        '<span class="rp-state" id="rp-state">' + rpEsc(rpT('panel.loading')) + '</span>' +
        '<button type="button" class="rp-ib" id="rp-mode" title="' + hint + '">&#8644;</button>' +
        '<button type="button" class="rp-ib rp-close" id="rp-close" title="' + ctitle + '">&#10005;</button>' +
      '</header>' +
      '<div class="rp-body" id="rp-body"></div>' +
    '</section>';
  document.body.appendChild(root);

  root.querySelector('#rp-close').onclick = rpPanelClose;
  root.querySelector('#rp-mode').onclick = function () {
    rpPanelSetMode(RP.mode === 'dock' ? 'focus' : 'dock');
  };
  root.querySelector('#rp-backdrop').onclick = rpPanelClose;

  rpPanelMountFrame();
  rpNavMark(true);
  document.addEventListener('keydown', rpOnKey, true);
  rpPanelStartTick();
  rpPanelPreflight();
}

/* Постоянный вход в сайдбаре подсвечивается, пока панель открыта: кнопка в
   баре плеера видела состояние сама (бар то появлялся, то нет), у сайдбара
   такого индикатора нет — добавляем его здесь, в своих файлах. */
function rpNavMark(on) {
  var b = document.getElementById('nav-panel-btn');
  if (b) b.classList.toggle('rp-on', !!on);
  var e = document.getElementById('pp-ext-btn');
  if (e) e.classList.toggle('rp-on', !!(on && RP.transport === 'relay'));
}

/* ── «Открыть внешний плеер» (шаг 4) ────────────────────────────────────────
   OS-окно панели из ЛЮБОЙ вкладки: сервер поднимает standalone-окно
   (?transport=relay), мост — через /ws. Из окна лаунчера просим своё же
   OS-окно старым путём (там pywebview-мост надёжнее ретранслятора).
   Никакого тихого дока: не вышло — называем причину словами (panel.ext.*). */
function rpOpenExternalPlayer() {
  var api = rpApi();
  if (api) { rpPanelToggleOswin(api); return; }
  if (RP._extBusy) return;
  RP._extBusy = true;
  fetch('/api/player-window/open', {
    method: 'POST', credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ launcher: false })
  }).then(function (r) {
    return r.json().catch(function () { return {}; })
      .then(function (j) { return { st: r.status, j: j }; });
  }).then(function (x) { RP._extBusy = false; rpExtResult(x.st, x.j); })
    .catch(function () { RP._extBusy = false; rpExtSay(rpT('panel.ext.net')); });
}

function rpExtResult(st, j) {
  if (st === 401 || st === 403) { rpExtSay(rpT('panel.ext.auth')); return; }
  if (j && j.ok) {
    /* Окно есть; готова ли связь — скажет hello: ждём 8 с и говорим вслух. */
    RP.transport = 'relay'; RP.open = true; RP.ready = false;
    RP.err = ''; RP.queueSent = null;
    rpNavMark(true);
    rpPanelStartTick();
    clearTimeout(RP.watchdog);
    RP.watchdog = setTimeout(function () {
      if (!RP.ready) rpExtSay(rpT('panel.ext.silent'));
    }, 8000);
    rpExtSay(rpT(j.how === 'focus' ? 'panel.ext.focus' : 'panel.ext.opened'), true);
    return;
  }
  var reason = (j && j.reason) || 'fail';
  var why = rpT('panel.ext.' + reason);
  if (why === 'panel.ext.' + reason) why = rpT('panel.ext.fail');
  rpExtSay(why + (j && j.detail ? ' (' + String(j.detail).slice(0, 120) + ')' : ''));
}

function rpExtSay(msg, ok) {
  try {
    /* --ok/--bad есть только в panel.css; на главной — --green/--red. */
    if (typeof toast === 'function') { toast(msg, ok ? 'var(--green)' : 'var(--red)'); return; }
  } catch (e) {}
  try { (window.__rpToast || function () {})(msg); } catch (e) {}
}

/* Сам iframe (и перевеска при «Повторить»). Watchdog — 6 с: панель обязана
   успеть представиться; если нет — показываем причину, а не пустой экран. */
function rpPanelMountFrame() {
  var body = document.getElementById('rp-body'); if (!body) return;
  var old = body.querySelector('iframe'); if (old) old.remove();
  var f = document.createElement('iframe');
  f.id = 'rp-panel-frame';
  f.className = 'rp-iframe';
  f.setAttribute('title', rpT('panel.title'));
  f.src = RP_SRC;
  body.innerHTML = '';
  body.appendChild(f);
  RP.frame = f;
  RP.ready = false;
  rpPanelStatus(rpT('panel.loading'));
  clearTimeout(RP.watchdog);
  RP.watchdog = setTimeout(function () { if (!RP.ready) rpPanelFail('timeout'); }, 6000);
}

function rpPanelSetMode(mode) {
  RP.mode = mode === 'focus' ? 'focus' : 'dock';
  var root = document.getElementById('rp-panel-root');
  if (root) root.className = 'rp-root rp-' + RP.mode;
}

function rpPanelClose() {
  if (!RP.open) return;
  RP.open = false; RP.ready = false; RP.frame = null;
  rpPanelStopTick();
  clearTimeout(RP.watchdog); RP.watchdog = null;
  document.removeEventListener('keydown', rpOnKey, true);
  rpNavMark(false);
  var root = document.getElementById('rp-panel-root');
  if (root) root.remove();
}

/* Закрыть по Esc — но только если фокус НЕ в самом плеере-хосте: панель живёт
   в iframe и свой Esc обрабатывает у себя. */
function rpOnKey(e) {
  if (e.key === 'Escape' && RP.open) { e.stopPropagation(); rpPanelClose(); }
}

/* ── Диагностика отказа: WHY, а не «что-то пошло не так» ─────────────────── */
function rpPanelStatus(msg) {
  var el = document.getElementById('rp-state'); if (el) el.textContent = msg;
}

function rpPanelPreflight() {
  var head = RP_SRC.split('?')[0];
  fetch(head, { method: 'HEAD', credentials: 'same-origin', cache: 'no-store' })
    .then(function (r) {
      if (r.status === 404) rpPanelFail('404');
      else if (r.status === 401 || r.status === 403) rpPanelFail('auth');
    })
    .catch(function () { rpPanelFail('net'); });
}

function rpPanelFail(kind, detail) {
  if (!RP.open) return;
  RP.err = kind;
  clearTimeout(RP.watchdog); RP.watchdog = null;
  var key = { '404': 'panel.err.404', auth: 'panel.err.auth', net: 'panel.err.net',
              timeout: 'panel.err.timeout', fatal: 'panel.err.fatal' }[kind] || 'panel.err.timeout';
  var why = rpT(key);
  if (kind === 'fatal' && detail) why = rpT('panel.err.fatal', { x: String(detail).slice(0, 160) });
  rpPanelStatus(why);
  var body = document.getElementById('rp-body'); if (!body) return;
  body.innerHTML =
    '<div class="rp-fail">' +
      '<div class="rp-fail-t">' + rpT('panel.err.title') + '</div>' +
      '<div class="rp-fail-r">' + rpT('panel.err.why') + ': ' + rpEsc(why) + '</div>' +
      '<div class="rp-fail-note">' + rpT('panel.err.note') + '</div>' +
      '<div class="rp-fail-btns">' +
        '<button type="button" class="rp-btn primary" id="rp-retry">' + rpT('panel.retry') + '</button>' +
        '<button type="button" class="rp-btn" id="rp-x">' + rpT('panel.close') + '</button>' +
      '</div>' +
    '</div>';
  body.querySelector('#rp-retry').onclick = function () { rpPanelMountFrame(); rpPanelPreflight(); };
  body.querySelector('#rp-x').onclick = rpPanelClose;
}

function rpEsc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
    return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
  });
}

/* ── Состояние плеера окна: читаем то же, что показывает сам интерфейс ───── */
/* `_stevPos`/`_stevLen` — штатные счётчики player.js: позиция из того движка,
   который играет (WebAudio-бесшовность, нативный WASAPI, <audio>, отдельный
   элемент синхронизации с телефоном), длительность — из карточки очереди, а не
   из вчерашнего трека в <audio>. Изобретать свою арифметику здесь нельзя. */
function rpEngine() {
  try {
    if (typeof Preview !== 'undefined' && Preview.mode === 'bbc') return 'bbc';
    if (typeof _NA !== 'undefined' && _NA.active) return 'native';
    if (typeof _WA !== 'undefined' && _WA.curSource) return 'gapless';
    if (typeof Preview !== 'undefined' && Preview._fpsEl) return 'paired';
  } catch (e) {}
  return 'audio';
}

function rpAudioEl() {
  try {
    if (rpEngine() === 'bbc') return document.getElementById('bbc-audio');
    if (typeof Preview !== 'undefined' && Preview._fpsEl) return Preview._fpsEl;
  } catch (e) {}
  return document.getElementById('pp-audio');
}

/* BBC играет ВНЕ Preview.queue (свой <audio id="bbc-audio">, см. bbc.js),
   поэтому штатный item из очереди для него пуст. Собираем карточку прямо из
   объекта BBC — те же поля, что видит развёрнутый плеер (_bbcFpItem), плюс
   live-флаг: у прямого эфира audio.duration === Infinity, у on-demand микса —
   конечная длительность. Панель по нему рисует бейдж «LIVE» и прячет скруббер. */
function rpBbcItem() {
  try {
    if (typeof BBC === 'undefined' || !BBC.pid) return null;
    var el = document.getElementById('bbc-audio');
    var dur = 0, live = false;
    try {
      if (el && isFinite(el.duration) && el.duration > 0) dur = Math.round(el.duration);
      else if (BBC.duration) dur = Math.round(BBC.duration);
      if (el && !isFinite(el.duration)) live = true;
    } catch (e) { live = true; }
    return {
      service: 'bbc', id: String(BBC.pid),
      title: BBC.title || '', artist: BBC.artist || '',
      cover: BBC.art || '', label: 'BBC Sounds',
      duration: dur, live: live, hires: false, local: false
    };
  } catch (e) { return null; }
}

function rpState() {
  var q = [], idx = -1, item = null;
  try { q = (typeof Preview !== 'undefined' && Preview.queue) || []; idx = (Preview.idx | 0); } catch (e) {}
  item = q[idx] || null;
  var eng = rpEngine(), el = rpAudioEl();
  var pos = null, dur = null, paused = true, vol = null, muted = false;
  // BBC вне очереди: карточку и длительность берём из объекта BBC.
  if (eng === 'bbc' && !item) { item = rpBbcItem(); }
  try { if (typeof _stevLen === 'function' && item && eng !== 'bbc') dur = _stevLen(item); } catch (e) {}
  if (eng === 'bbc' && item) { dur = item.duration || dur; }
  try {
    if (eng === 'native' && typeof _NA !== 'undefined') { pos = _NA.cur; dur = _NA.dur || dur; paused = !!_NA.paused; }
    else {
      if (typeof _stevPos === 'function' && eng !== 'bbc') pos = _stevPos();
      if (pos == null && el && isFinite(el.currentTime)) pos = el.currentTime;
      paused = el ? !!el.paused : true;
    }
  } catch (e) {}
  try { if (el) { vol = el.muted ? 0 : el.volume; muted = !!el.muted; } } catch (e) {}
  return {
    rp: 1, k: 'state', seq: ++RP.seq, engine: eng,
    have: !!item, idx: idx, qlen: q.length,
    item: item ? {
      service: item.service || '', id: String(item.id == null ? '' : item.id),
      title: item.title || '', artist: item.artist || '', cover: item.cover || '',
      label: item.label || '', duration: Number(item.duration) || 0,
      hires: !!item.hires, local: !!item.local, live: !!item.live
    } : null,
    pos: pos, dur: dur, paused: paused, volume: vol, muted: muted,
    queue: rpQueueStamp(q, idx)
  };
}

/* Отпечаток очереди: чтобы панель не перерисовывала список на каждом тике. */
function rpQueueStamp(q, idx) {
  if (!q || !q.length) return '';
  var out = idx + '|';
  for (var i = 0; i < q.length; i++) {
    out += ((q[i] && q[i].service) || '') + ':' + ((q[i] && q[i].id) || '') + ';';
    if (i > 250) break;
  }
  return out;
}

function rpSlimQueue() {
  var q = [];
  try { q = (typeof Preview !== 'undefined' && Preview.queue) || []; } catch (e) {}
  return q.slice(0, 300).map(function (x, i) {
    return {
      i: i, service: x.service || '', id: String(x.id == null ? '' : x.id),
      title: x.title || '', artist: x.artist || '', cover: x.cover || '',
      label: x.label || '', duration: Number(x.duration) || 0,
      album: x.album || '', hires: !!x.hires
    };
  });
}

/* Один пост — три транспорта. В OS-окне адресат живёт по ту сторону Python,
   в relay — по ту сторону сервера: /ws перекладывает {type:'rp', msg}. */
function rpPost(msg) {
  if (RP.transport === 'oswin') {
    var api = rpApi();
    if (!api) return;
    try {
      var p = api.panel_push(JSON.stringify(msg));
      if (p && p.catch) p.catch(function () {});
    } catch (e) {}
    return;
  }
  if (RP.transport === 'relay') {
    rpRelaySend(msg);
    return;
  }
  var f = RP.frame;
  if (!f || !f.contentWindow) return;
  try { f.contentWindow.postMessage(msg, location.origin); } catch (e) {}
}

/* Общий сокет главной страницы живёт в app.js (`let ws`); глобальная лексическая
   связь видна отсюда — но сам сокет может быть в переподключении, и это не беда:
   панель при отсутствии ответа сама пришлёт need-queue, а цикл шлёт состояние
   каждые 400 мс. Регистрируем роль ХОСТА при каждом открытии сокета (ниже). */
function rpRelaySend(msg) {
  try {
    if (typeof ws !== 'undefined' && ws && ws.readyState === 1)
      ws.send(JSON.stringify({ type: 'rp', msg: msg }));
  } catch (e) {}
}

/* app.js зовёт это на каждом ws.onopen (свой сокет — своя роль на сервере). */
function rpHostWsOpen() {
  try {
    if (typeof ws !== 'undefined' && ws && ws.readyState === 1)
      ws.send(JSON.stringify({ type: 'rp-role', role: 'host' }));
  } catch (e) {}
}

/* app.js зовёт это для каждого входящего {type:'rp'} — сервер переложил
   сообщение панели. oswin/iframe живут своими каналами, relay-мошенничество
   между ними невозможно: роль на сервере уже развела потоки. */
function rpHostWsMessage(packet) {
  var d = packet && packet.msg;
  if (!d || d.rp !== 1) return;
  if (RP.open && RP.transport !== 'relay') return;
  rpHostMessage(d, 'relay');
}

/* Спрятать relay-окно по «✕» внутри панели. Окно переживает это (hide, не
   destroy) — сервер при реальном закрытии сам пришлёт bye. */
function rpCloseExternalWindow() {
  try {
    var p = fetch('/api/player-window/close', { method: 'POST', credentials: 'same-origin' });
    if (p && p.catch) p.catch(function () {});
  } catch (e) {}
  RP.ready = false;
}

function rpPanelPush() {
  if (!RP.open || !RP.ready) return;
  var s = rpState();
  rpPost(s);
  if (s.queue !== RP.queueSent) {
    RP.queueSent = s.queue;
    rpPost({ rp: 1, k: 'queue', items: rpSlimQueue(), idx: s.idx, stamp: s.queue });
  }
}

/* ── Команды панели → те же функции, что зовёт сам интерфейс плеера ──────── */
var RP_CMD = {
  toggle: function () { if (typeof previewToggle === 'function') previewToggle(); },
  next: function () { if (typeof previewNext === 'function') previewNext(); },
  prev: function () { if (typeof previewPrev === 'function') previewPrev(); },
  mute: function () { if (typeof previewMute === 'function') previewMute(); },
  volume: function (a) {
    if (typeof previewVolume !== 'function') return;
    previewVolume(Math.max(0, Math.min(1, +a.v || 0)));
  },
  seek: function (a) {
    if (typeof previewSeekTo !== 'function') return;
    var s = +a.sec;
    if (!isFinite(s) || s < 0) return;
    previewSeekTo(s);
  },
  playIndex: function (a) {
    try {
      var i = a.i | 0;
      if (typeof Preview === 'undefined' || !Preview.queue || !Preview.queue[i]) return;
      Preview.idx = i;
      if (typeof _setupAudioEvents === 'function') _setupAudioEvents();
      if (typeof _playPreviewAt === 'function') _playPreviewAt(i);
    } catch (e) {}
  },
  setQueue: function (a) {
    var items = rpNormalize(a.items);
    if (!items.length) return;
    var i = Math.max(0, Math.min(items.length - 1, a.idx | 0));
    try {
      if (typeof _setupAudioEvents === 'function') _setupAudioEvents();
      Preview.queue = items; Preview.idx = i;
      if (typeof _playPreviewAt === 'function') _playPreviewAt(i);
      RP.queueSent = null;
    } catch (e) {}
  },
  enqueue: function (a) {
    var items = rpNormalize(a.items);
    if (!items.length) return;
    try {
      if (typeof pqAddItems === 'function') { pqAddItems(items); RP.queueSent = null; return; }
      Preview.queue = (Preview.queue || []).concat(items); RP.queueSent = null;
    } catch (e) {}
  },
  station: function (a) {
    if (typeof stPlay !== 'function') return false;
    try { stPlay(String(a.id || '')); RP.queueSent = null; } catch (e) {}
  },
  quality: function (a) {
    if (typeof setStreamQuality === 'function') { try { setStreamQuality(a.q); } catch (e) {} }
  },
  removeAt: function (a) {
    if (typeof _pqRemove === 'function') { try { _pqRemove(a.i | 0); RP.queueSent = null; } catch (e) {} }
  },
  playNext: function (a) {
    var items = rpNormalize(a.items);
    if (!items.length) return;
    try {
      if (typeof pqPlayNext === 'function') { pqPlayNext(items); RP.queueSent = null; return; }
      Preview.queue.splice(Preview.idx + 1, 0, items[0]); RP.queueSent = null;
    } catch (e) {}
  },
  fav: function () {
    if (typeof ppFollowCurrent === 'function') { try { ppFollowCurrent(); } catch (e) {} }
  },
  /* «Открыть в Ripster»: view главного окна по-человечески (Раскопки, BBC,
     настройки). Окно может быть спрятано в трей — сначала поднимаем его. */
  openView: function (a) {
    var name = String((a && a.name) || '');
    if (!/^(digs|bbc|settings|search|queue|stations|soundcloud)$/.test(name)) return;
    try {
      var api = rpApi();
      if (api) { var p = api.main_show(); if (p && p.catch) p.catch(function () {}); }
    } catch (e) {}
    try {
      var el = document.querySelector('.nav-item[data-view="' + name + '"]');
      if (typeof showView === 'function') showView(name, el);
    } catch (e) {}
  },
  pip: function () {
    try { floatPlayer(); } catch (e) {}
  },
  close: function () {
    if (RP.transport === 'relay') { rpCloseExternalWindow(); return; }
    if (RP.transport === 'oswin') { rpPanelToggleOswin(rpApi()); return; }
    rpPanelClose();
  }
};

/* Карточки поиска/станций → форма очереди окна ПК. `url` остаётся пустым:
   аудиоссылку разрезолвливает плеер, адрес страницы сервиса там убивает звук.
   Исключение — локальные файлы библиотеки: у них НЕТ сервиса, который можно
   разрезолвить по id; единственная играющая ссылка и есть `/api/library/file`.
   Теряем её — и «Библиотека» панели честна, но нема. */
function rpNormalize(list) {
  return (list || []).slice(0, 400).map(function (x) {
    var local = !!x.local || x.service === 'local';
    var direct = local && /^\/api\/library\/file\?/.test(String(x.url || ''));
    return {
      url: direct ? x.url : '',
      service: x.service || (local ? 'local' : ''),
      local: local || undefined,
      id: String(x.id == null ? '' : x.id),
      title: x.title || '', artist: x.artist || '', cover: x.cover || '',
      album: x.album || '', label: x.label || '',
      duration: Number(x.duration) || 0, full: true,
      hires: !!x.hires, permalink: direct ? '' : (x.permalink || x.url || ''),
      posKey: x.posKey || ((x.service || '') + ':' + (x.id == null ? '' : x.id))
    };
  }).filter(function (x) { return x.id || x.title; });
}

/* Возможности хоста — чтобы панель честнее показывала то, чем нельзя управлять. */
function rpCaps() {
  var has = function (fn) { try { return typeof fn === 'function' ? 1 : 0; } catch (e) { return 0; } };
  var c = {};
  c.toggle = has(previewToggle); c.next = has(previewNext); c.prev = has(previewPrev);
  c.seek = has(previewSeekTo); c.volume = has(previewVolume); c.mute = has(previewMute);
  c.setQueue = has(_playPreviewAt); c.enqueue = has(pqAddItems) || has(_playPreviewAt);
  c.playNext = has(pqPlayNext); c.remove = has(_pqRemove); c.fav = has(ppFollowCurrent);
  c.queue = 1;
  try { c.station = (typeof stPlay === 'function') ? 1 : 0; } catch (e) { c.station = 0; }
  try { c.quality = (typeof setStreamQuality === 'function') ? 1 : 0; } catch (e) { c.quality = 0; }
  /* Document-PiP мёртв в WebView2, а в OS-окне он и вовсе не нужен. */
  c.pip = (RP.transport !== 'iframe' || rpEnv() === 'WebView2') ? 0 : has(floatPlayer);
  c.openView = 1;
  c.oswin = RP.transport === 'oswin' ? 1 : 0;
  c.relay = RP.transport === 'relay' ? 1 : 0;
  c.shuffle = 0; c.repeat = 0;   // в окне ПК их нет — панель это скажет вслух
  return c;
}

/* Обработка одного сообщения от панели (общая для iframe и OS-окна). */
function rpHostMessage(d, via) {
  if (d.k === 'bye') {                       // окно панели спрятали — цикл спит
    RP.ready = false;
    return;
  }
  if (!RP.open && (d.k === 'hello' || d.k === 'cmd' || d.k === 'need-queue')) {
    rpAdoptFromPanel(via);                   // хост перезагрузился, панель живёт
  }
  if (d.k === 'hello') {
    RP.ready = true; RP.err = ''; RP.queueSent = null;
    clearTimeout(RP.watchdog); RP.watchdog = null;
    var s = rpState();
    if (RP.transport === 'iframe') rpPanelStatus(s.have ? ((s.item && s.item.title) || '') : rpT('panel.idle'));
    rpPost({ rp: 1, k: 'welcome', caps: rpCaps(), env: rpEnv() });
    rpPost(s);
    rpPost({ rp: 1, k: 'queue', items: rpSlimQueue(), idx: s.idx, stamp: s.queue });
    RP.queueSent = s.queue;
    return;
  }
  if (d.k === 'ready') { rpPanelStatus(rpT('panel.connected')); return; }
  if (d.k === 'fatal') { rpPanelFail('fatal', d.msg); return; }
  if (d.k === 'need-queue') {
    var s2 = rpState();
    rpPost({ rp: 1, k: 'queue', items: rpSlimQueue(), idx: s2.idx, stamp: s2.queue });
    RP.queueSent = s2.queue;
    return;
  }
  if (d.k === 'cmd') {
    var fn = RP_CMD[d.cmd];
    if (fn) { try { fn(d.arg || {}); } catch (e) { rpPost({ rp: 1, k: 'cmd-fail', cmd: d.cmd, msg: String(e.message || e) }); } }
    else rpPost({ rp: 1, k: 'cmd-fail', cmd: d.cmd, msg: 'no such command' });
  }
}

/* Приём из OS-окна: Python вызывает rpHostRecv(<json-строка>). */
window.rpHostRecv = function (str) {
  var d = null;
  try { d = JSON.parse(str); } catch (e) { return; }
  if (!d || d.rp !== 1) return;
  rpHostMessage(d, 'oswin');
};

window.addEventListener('message', function (ev) {
  var d = ev.data;
  if (!d || d.rp !== 1) return;
  var f = RP.frame;
  if (!f || ev.source !== f.contentWindow) return;          // чужое окно не хозяин
  if (ev.origin !== location.origin) return;                 // и не чужой origin
  rpHostMessage(d, 'iframe');
}, false);

function rpEnv() {
  var ua = navigator.userAgent || '';
  return /webview2|pywebview/i.test(ua) ? 'WebView2' : 'browser';
}
