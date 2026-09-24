/* ============================================================================
   Низ плеера: одна кнопка «Плеер-окно», сетка с именованными областями и
   предел сужения окна (замечание владельца 24.09.2026).

   Почему это отдельный файл. `panel_host.js` и `rp_relay.py` заняли чужой
   задачей, а правки ниже к ним не имеют отношения: здесь только разметка
   бара, перекладывание кнопок в «⋯» и вызов УЖЕ существующих функций панели
   (rpPanelToggle / rpOpenExternalPlayer / rpPanelOpen). Общего верхнеуровневого
   let/const здесь нет намеренно — имена через var/function, чтобы не столкнуть
   котёл скриптов (tools/check_js_collisions.py).

   1. ОДНА КНОПКА. Раньше на баре стояли ⧉ (док панели внутри окна) и 🖥
      («внешний плеер», отдельное OS-окно) — две кнопки про одно и то же.
      Осталась одна, «Плеер-окно»: клик — окно; окно не вышло — док справа и
      тост с причиной (молчаливого дока, как раньше, нет). Долгое нажатие или
      правая кнопка — меню «в окне / в панели справа».

   2. СЕТКА. Колонки задал CSS; здесь — арифметика: широкая правая группа не
      может влезть в свою колонку на любом окне, и раньше media-query просто
      гасили кнопки. Теперь недостающее переезает в «⋯» ПО ПОРЯДКУ приоритета
      (data-pp-prio: больше = сворачивается раньше). Транспорт ⏮▶⏭, качество,
      «Плеер-окно» и «✕» не сворачиваются никогда: без них плеер перестаёт
      быть плеером.

   3. ПРЕДЕЛ. CSS держит min-width настольного интерфейса; fixed-док —
      position:fixed, поэтому прокрутка уезжает от него. Сдвиг на scrollLeft
      возвращает бар к его же контенту.
       ========================================================================= */

var PPBAR = {
  items: [],          // [{el, prio, home, next, hideOnly, collapsed}]
  stack: [],          // порядок сворачивания — для честного восстановления
  fitted: 0,          // ширина бара на последнем расчёте (анти-дребезг)
  longT: null,
  wm: null
};

/* Кандидаты на сворачивание и их «дом». Порядок в списке = порядок
   сворачивания; не сворачиваемое в списке отсутствует вовсе. */
function ppBarCollect() {
  if (PPBAR.items.length) return PPBAR.items;
  var spec = [
    ['pp-viz',       100, 1],   // декоративный визуализатор: не кнопка → просто гаснет
    ['pp-expand-btn', 90, 0],   // дубль клика по обложке
    ['pp-local-btn',  80, 0],
    ['pp-fav-btn',    70, 0],
    ['pp-queue-btn',  60, 0],
    ['pp-dl-btn',     50, 0],
    ['pp-dis-btn',    40, 0],
    ['pp-vol-wrap',   30, 0],
    ['pp-center',     20, 0]    // заполнитель: время — последнее (см. ниже)
  ];
  for (var i = 0; i < spec.length; i++) {
    var el = document.getElementById(spec[i][0]);
    if (spec[i][0] === 'pp-center') {
      var tb = document.querySelector('#pp-center .pp-time-big');
      if (tb) el = tb;
    }
    if (!el) continue;
    PPBAR.items.push({
      el: el, prio: spec[i][1], hideOnly: !!spec[i][2], collapsed: false,
      home: el.parentNode, next: el.nextSibling
    });
  }
  PPBAR.items.sort(function (a, b) { return b.prio - a.prio; });
  return PPBAR.items;
}

function ppBarGap(zone) {
  var g = parseFloat(getComputedStyle(zone).columnGap || getComputedStyle(zone).gap || '0');
  return isFinite(g) ? g : 0;
}

/* Ширина контента зоны: сумма видимых детей + промежутки. scrollWidth для
   justify-content:flex-end врёт (переполнение уходит влево и в счёт не берётся),
   поэтому считаем сами. */
function ppBarNatural(zone, gap) {
  var sum = 0, n = 0;
  for (var c = zone.firstElementChild; c; c = c.nextElementSibling) {
    var w = c.getBoundingClientRect().width;
    if (!w && getComputedStyle(c).display === 'none') continue;
    sum += w; n++;
  }
  return n > 1 ? sum + gap * (n - 1) : sum;
}

/* Узел «⋯» считается по ширине только когда он нужен. */
function ppBarOfWidth() {
  var of = document.getElementById('pp-of');
  if (!of) return 0;
  return of.classList.contains('pp-of-on') ? of.getBoundingClientRect().width : 0;
}

function ppBarMobile() {
  return window.innerWidth < 700 ||
         !document.getElementById('pp-bar') ||
         window.matchMedia('(max-width:699px)').matches;
}

function ppBarOf() { return document.getElementById('pp-of'); }
function ppBarMenu() { return document.getElementById('pp-of-menu'); }

function ppBarPut(it, intoMenu) {
  if (it.hideOnly) { it.collapsed = !!intoMenu; it.el.classList.toggle('pp-collapsed', intoMenu); return; }
  var menu = ppBarMenu();
  if (intoMenu) {
    if (menu && it.el.parentNode !== menu) { menu.appendChild(it.el); it.collapsed = true; }
  } else if (it.home) {
    it.home.insertBefore(it.el, it.next || null); it.collapsed = false;
  }
}

function ppBarRestoreAll() {
  var items = ppBarCollect();
  for (var i = 0; i < items.length; i++) ppBarPut(items[i], false);
  PPBAR.stack = [];
  var of = document.getElementById('pp-of');
  if (of) of.classList.remove('pp-of-on');
  ppBarCloseMenus();
}

/* Один проход раскладки: что не влезает — уезжает в «⋯», что снова влезает —
   возвращается оттуда (LIFO, иначе кнопка прыгала бы местами на каждом пикселе).

   Узлы НЕ дублируются: в меню живёт та же кнопка, поэтому её обработчики,
   состояние (♡/🚫 «.on») и значение ползунка не разъезжаются с оригиналом. */
function ppBarFit(force) {
  var bar = document.getElementById('pp-bar');
  if (!bar) return;
  if (ppBarMobile()) { ppBarRestoreAll(); return; }
  var w = bar.getBoundingClientRect().width;
  if (!force && Math.abs(w - PPBAR.fitted) < 0.5) return;
  PPBAR.fitted = w;

  var items = ppBarCollect();
  var right = document.querySelector('.pp-zone-right');
  var of = ppBarOf();
  if (!right || !of) return;
  var trackR = right.getBoundingClientRect().width;
  var gapR = ppBarGap(right);
  var need = 0, guard = 0;

  /* Считаем справа и заново на каждом шаге: переложенная в меню кнопка меняет
     ширину и своей колонки, и соседней (транспорт в max-content). */
  while (guard++ < items.length + 2) {
    need = ppBarNatural(right, gapR) + ppBarOfWidth();
    if (need <= trackR + 0.5) break;
    var next = null;
    for (var i = 0; i < items.length; i++) {
      if (!items[i].collapsed) { next = items[i]; break; }
    }
    if (!next) break;                       // сворачивать больше нечего
    ppBarPut(next, true);
    PPBAR.stack.push(next);
    of.classList.add('pp-of-on');           // «⋯» с этого момента занимает место
    trackR = right.getBoundingClientRect().width;
  }

  /* Обратный ход: пробуем вернуть последней уехавшую и меряем ПОСЛЕ возврата.
     Угадывать ширину нельзя — у узла в скрытом меню она нулевая. */
  while (PPBAR.stack.length && guard++ < items.length * 4 + 4) {
    var last = PPBAR.stack[PPBAR.stack.length - 1];
    PPBarPutBack(last);
    trackR = right.getBoundingClientRect().width;
    need = ppBarNatural(right, gapR) + ppBarOfWidth();
    if (need > trackR + 0.5) {              // не влезло — увозим обратно
      ppBarPut(last, true);
      PPBAR.stack.push(last);
      break;
    }
  }

  of.classList.toggle('pp-of-on', PPBAR.stack.length > 0);
  if (!PPBAR.stack.length) ppBarCloseMenus();
}

function PPBarPutBack(it) {
  ppBarPut(it, false);
  for (var i = PPBAR.stack.length - 1; i >= 0; i--) if (PPBAR.stack[i] === it) PPBAR.stack.splice(i, 1);
}

function ppOverflowToggle(ev) {
  if (ev) ev.stopPropagation();
  var menu = ppBarMenu();
  if (!menu) return;
  var open = menu.classList.contains('open');
  ppBarCloseMenus();
  if (!open) menu.classList.add('open');
}

function ppBarCloseMenus() {
  var menu = ppBarMenu();
  if (menu) menu.classList.remove('open');
  if (PPBAR.wm) PPBAR.wm.classList.remove('open');
}

/* ── Кнопка «Плеер-окно» ─────────────────────────────────────────────────── */
function rpBarApi() {
  try { return (typeof rpApi === 'function') ? rpApi() : null; } catch (e) { return null; }
}

/* Окно не вышло → док справа ОБЯЗАН быть подписан: без тоста человек видит
   панель не там, где просил, и решает, что это новый баг. */
function rpPlayerWindowToggle(ev) {
  if (ev && ev.stopPropagation) ev.stopPropagation();
  if (ppBarLongFired()) return;
  if (RP.open && RP.transport === 'iframe') { rpPanelClose(); return; }  // док показан — гасим
  if (rpBarApi()) { rpPanelToggle(); return; }   // лаунчер: окно, а не вышло — подписывает сам
  if (RP._extBusy) return;                       // запрос на окно уже жив — второй только мешает
  rpOpenExternalPlayer();
  ppBarWaitWindow(0);
}

/* rpOpenExternalPlayer() асинхронный: RP._extBusy горит, пока запрос жив, а
   успех выглядит как transport='relay' + panelPresent (RP.open загорается
   позже, когда окно пришлёт hello — ждать его здесь нельзя). Отказ panel_host
   уже назвал словами; здесь — следствие: док справа. */
function ppBarWaitWindow(tries) {
  var busy = true;
  try { busy = !!RP._extBusy; } catch (e) { busy = false; }
  if (!busy) {
    var got = false;
    try { got = (RP.transport === 'relay' && RP.panelPresent) || RP.transport === 'oswin'; } catch (e) {}
    if (!got) ppBarDock();
    return;
  }
  if (tries > 120) ppBarDock();                  // 12 с — дольше ждать отказа незачем
  else setTimeout(function () { ppBarWaitWindow(tries + 1); }, 100);
}

function ppBarDock() {
  try { rpPanelOpen('dock'); } catch (e) {}
  ppBarSay(rpT('player.window.dock_fallback'));
}

function ppBarSay(msg) {
  try { if (typeof toast === 'function') { toast(msg, 'var(--red)'); return; } } catch (e) {}
  try { if (typeof rpExtSay === 'function') rpExtSay(msg); } catch (e) {}
}

/* Долгое нажатие (600 мс) и правая кнопка — одно и то же меню: ordinary click
   остаётся «как договорились», место выбирается здесь. */
function ppBarLongFired() {
  if (!PPBAR.longFired) return false;
  PPBAR.longFired = false;
  return true;
}

function ppBarWireWindowButton() {
  var btn = document.getElementById('pp-ext-btn');
  if (!btn || btn._ppwmWired) return;
  btn._ppwmWired = true;
  btn.setAttribute('aria-haspopup', 'menu');
  btn.addEventListener('pointerdown', function (e) {
    if (e.button !== 0) return;
    PPBAR.longFired = false;
    clearTimeout(PPBAR.longT);
    PPBAR.longT = setTimeout(function () {
      PPBAR.longFired = true;
      ppBarWindowMenu(e.clientX, e.clientY);
    }, 600);
  });
  var cancel = function () { clearTimeout(PPBAR.longT); };
  btn.addEventListener('pointerup', cancel);
  btn.addEventListener('pointerleave', cancel);
  btn.addEventListener('pointercancel', cancel);
  btn.addEventListener('contextmenu', function (e) {
    e.preventDefault(); clearTimeout(PPBAR.longT);
    ppBarWindowMenu(e.clientX, e.clientY);
  });
}

/* Меню места. Узлы собираются DOM-вызовами, а не innerHTML: текст приходит из
   i18n-словаря, и полагаться на экранирование строки — лишний риск. */
function ppBarWindowMenu(x, y) {
  var wm = PPBAR.wm;
  if (!wm) return;
  ppBarCloseMenus();
  while (wm.firstChild) wm.removeChild(wm.firstChild);
  wm.appendChild(ppBarWmItem('win', rpT('player.menu.window')));
  wm.appendChild(ppBarWmItem('dock', rpT('player.menu.dock')));
  var note = document.createElement('div');
  note.className = 'pp-wm-note';
  note.textContent = rpT('player.menu.note');
  wm.appendChild(note);
  wm.classList.add('open');                  // ширина есть только у видимого узла
  wm.style.left = Math.max(8, Math.min(x - 8, window.innerWidth - wm.offsetWidth - 8)) + 'px';
  wm.style.top = 'auto';
  wm.style.bottom = Math.max(8, window.innerHeight - y + 8) + 'px';
}

function ppBarWmItem(act, label) {
  var b = document.createElement('button');
  b.type = 'button';
  b.setAttribute('data-act', act);
  b.textContent = label;
  return b;
}

function ppBarWindowAct(act) {
  if (act === 'win') {
    var api = rpBarApi();
    if (api) rpPanelToggleOswin(api); else rpOpenExternalPlayer();
    return;
  }
  rpPanelOpen('dock');
}

/* ── Прокрутка ниже предела: fixed-док догоняет свой контент ────────────────
   transform дока принадлежит CSS (им панель выезжает: `translate3d(0,100%,0)`
   → `.visible`), поэтому писать туда свой X нельзя — оставался бы либо
   уехавший на высоту панели бар (замер 24.09: +59px по Y), либо украденная
   анимация. Пишем ТОЛЬКО переменную `--pp-sx`, из которой CSS сам собирает
   transform. Так X и Y не воюют друг с другом. */
function ppBarScrollFix() {
  var dock = document.getElementById('preview-player');
  var sx = document.documentElement.scrollLeft || document.body.scrollLeft || 0;
  if (dock) dock.style.setProperty('--pp-sx', (-sx) + 'px');
  /* Тост прижат к правому краю ЭКРАНА (position:fixed), а бар — к правому
     краю ДОКРИТУЛЬНОГО поля: без поправки уведомление в 320px ложится на
     «Плеер-окно», «✕» и «⋯» ровно там, где человек докрутил до них. Развёрнутый
     плеер — правый ящик на 440px, его тоже объезжаем. */
  var ns = document.getElementById('notif-stack');
  if (ns) {
    var side = document.body.classList.contains('pp-side')
             ? Math.min(440, window.innerWidth) : 0;
    ns.style.right = (20 + side + sx) + 'px';
  }
  ppBarCloseMenus();
}

function ppBarInit() {
  if (window._ppBarInited) return;
  window._ppBarInited = true;
  var wm = document.createElement('div');
  wm.className = 'pp-wm'; wm.id = 'pp-window-menu';
  document.body.appendChild(wm);
  PPBAR.wm = wm;
  wm.addEventListener('click', function (e) {
    var b = e.target.closest ? e.target.closest('button[data-act]') : null;
    if (!b) return;
    ppBarCloseMenus();
    ppBarWindowAct(b.getAttribute('data-act'));
  });
  ppBarWireWindowButton();
  ppBarFit(true);
  if (window.ResizeObserver) {
    var ro = new ResizeObserver(function () { ppBarFit(false); });
    var bar = document.getElementById('pp-bar');
    if (bar) ro.observe(bar);
    var ex = document.getElementById('pp-extras');
    if (ex) ro.observe(ex);
  }
  window.addEventListener('resize', function () { ppBarFit(true); }, true);
  window.addEventListener('scroll', ppBarScrollFix, true);
  /* Классы тела (`.visible` у дока, `pp-side` — ящик развёрнутого плеера)
     меняются без событий window; а они решают и за transform дока, и за
     место тостов. */
  if (window.MutationObserver)
    new MutationObserver(function () { ppBarScrollFix(); })
      .observe(document.body, {attributes: true, attributeFilter: ['class']});
  ppBarScrollFix();
  /* Закрываем на всплытии, а не на захвате: клик по переложенной в меню кнопке
     должен успеть сработать сам — на захвате меню сворачивается до цели. */
  document.addEventListener('click', function (e) {
    var of = ppBarOf();
    var btn = document.getElementById('pp-ext-btn');
    if (of && of.contains(e.target)) return;
    if (PPBAR.wm && PPBAR.wm.contains(e.target)) return;
    if (btn && btn.contains(e.target)) return;
    ppBarCloseMenus();
  });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') ppBarCloseMenus();
  }, true);
  /* «Качество» меняет надпись (MP3 → Hi-Res) — вместе с ней меняется и ширина
     группы справа, то есть и решение о сворачивании. */
  var lab = document.getElementById('pp-quality-label');
  if (lab && window.MutationObserver)
    new MutationObserver(function () { ppBarFit(true); }).observe(lab, { childList: true, characterData: true, subtree: true });
  /* Смена языка: событий в проекте нет, но applyLang() пишет lang на <html> —
     за этим и следим. Подписи «⋯» (attr(title)) перекрасятся сами. */
  if (window.MutationObserver)
    new MutationObserver(function () { ppBarFit(true); })
      .observe(document.documentElement, { attributes: true, attributeFilter: ['lang'] });
}

if (document.readyState === 'loading')
  document.addEventListener('DOMContentLoaded', ppBarInit);
else ppBarInit();
