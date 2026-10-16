/* Тур по «Раскопкам» — что здесь можно делать и зачем.
 *
 * Раскопки — единственная вкладка, где Ripster не выполняет команду, а
 * предлагает. Без объяснения это читается как реклама: человек видит список
 * альбомов и не понимает, ни откуда он взялся, ни почему в нём именно эти
 * артисты. Тур проходится один раз, дальше — по кнопке в углу заголовка.
 *
 * ЯЗЫК ДВИЖЕНИЯ. Подсветка не перепрыгивает между шагами, а ПЕРЕТЕКАЕТ: это
 * одна и та же рамка, у которой меняются координаты. Ощущение — снимаемый слой,
 * а не всплывающие окна. Поэтому вырез сделан одной тенью на 9999px вместо
 * четырёх затемняющих панелей: тень анимируется вместе с рамкой, панели бы
 * дёргались. Двигаем только transform/opacity и геометрию рамки — фильтров и
 * масок на движущемся нет (то же правило, что и в digs.html).
 *
 * ЦВЕТ НЕ ВЫДУМАН. Янтарь #c9a227 — уже фирменный цвет находок этой вкладки,
 * зелёный берётся из --green. Всё остальное — переменные темы, потому что тем
 * пять (dark, light, midnight, ember, sepia), и любой хардкод фона сломал бы
 * тур в четырёх из них.
 *
 * СОСТОЯНИЕ — НА СЕРВЕРЕ. Признак «тур пройден» лежит в конфиге, а не в
 * localStorage: у Ripster минимум две оболочки (окно программы на WebView2 и
 * обычный браузер), и в localStorage тур показался бы заново в каждой.
 */

const DG_TOUR_KEY = 'digs-tour-seen';

/* Шаги. `sel` — реальный элемент вкладки; если его на странице нет (например,
 * находок ещё не нашлось), шаг молча пропускается: подсвечивать пустоту и
 * рассказывать про то, чего человек не видит, хуже, чем не рассказать вовсе. */
const DG_TOUR_STEPS = [
  { sel: null,               k: 'dgtour.s1' },   // без подсветки — суть фичи
  { sel: '#view-digs .dg-bar',  k: 'dgtour.s2' },
  { sel: '#digs-seams',      k: 'dgtour.s3' },
  { sel: '#view-digs .dg-item', k: 'dgtour.s4' },
  { sel: '#view-digs .dg-item .dg-acts', k: 'dgtour.s5' },
  { sel: '#digs-profile',    k: 'dgtour.s6' },
];

let _dgTourIdx = 0;
let _dgTourSteps = [];

function _dgTourReduced() {
  return window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
}

function _dgTourEnsureDom() {
  if (document.getElementById('dg-tour')) return;

  const css = document.createElement('style');
  css.id = 'dg-tour-css';
  css.textContent = `
    #dg-tour{position:fixed;inset:0;z-index:9000;display:none}
    #dg-tour.on{display:block}
    /* Затемнение для шагов БЕЗ подсветки. Без него первый шаг висел поверх
       полностью яркого интерфейса и читался хуже всех остальных: тень выреза
       есть только у самого выреза, а на этом шаге вырезать нечего. */
    #dg-tour .dgt-veil{position:absolute;inset:0;background:rgba(8,7,5,.66);
      opacity:0;transition:opacity .3s ease-out;pointer-events:none}
    #dg-tour .dgt-veil.on{opacity:1}
    :root[data-theme="light"] #dg-tour .dgt-veil{background:rgba(20,16,10,.42)}
    /* Вырез: рамка + огромная тень вокруг неё. Одним элементом, поэтому
       перемещение между шагами — это перетекание, а не смена окон. */
    #dg-tour .dgt-hole{position:absolute;border-radius:12px;pointer-events:none;
      box-shadow:0 0 0 9999px rgba(8,7,5,.66), 0 0 0 1px rgba(201,162,39,.55) inset;
      transition:top .42s cubic-bezier(.22,1,.36,1),left .42s cubic-bezier(.22,1,.36,1),
                 width .42s cubic-bezier(.22,1,.36,1),height .42s cubic-bezier(.22,1,.36,1)}
    :root[data-theme="light"] #dg-tour .dgt-hole{box-shadow:0 0 0 9999px rgba(20,16,10,.42), 0 0 0 1px rgba(140,83,20,.5) inset}
    /* Тонкое янтарное дыхание по краю выреза — «здесь копаем». */
    #dg-tour .dgt-ring{position:absolute;border-radius:12px;pointer-events:none;
      border:1px solid rgba(201,162,39,.5);animation:dgtBreath 2.4s ease-in-out infinite;
      transition:top .42s cubic-bezier(.22,1,.36,1),left .42s cubic-bezier(.22,1,.36,1),
                 width .42s cubic-bezier(.22,1,.36,1),height .42s cubic-bezier(.22,1,.36,1)}
    @keyframes dgtBreath{0%,100%{opacity:.28}50%{opacity:.85}}

    #dg-tour .dgt-box{position:absolute;max-width:400px;background:var(--surface,#1c1e2a);
      border:1px solid rgba(201,162,39,.35);border-radius:14px;padding:15px 16px 13px;
      box-shadow:0 18px 44px rgba(0,0,0,.5);color:var(--text);font-family:var(--font);
      opacity:0;transform:translateY(10px);
      transition:opacity .3s ease-out,transform .34s cubic-bezier(.22,1,.36,1),
                 top .42s cubic-bezier(.22,1,.36,1),left .42s cubic-bezier(.22,1,.36,1)}
    #dg-tour .dgt-box.in{opacity:1;transform:translateY(0)}
    #dg-tour .dgt-h{display:flex;align-items:center;gap:8px;margin-bottom:7px}
    #dg-tour .dgt-paw{width:22px;height:22px;flex-shrink:0;transform-origin:60% 90%;
      animation:dgtPaw 1.15s ease-in-out infinite}
    @keyframes dgtPaw{0%,100%{transform:rotate(-6deg)}45%{transform:rotate(8deg) translateY(2px)}}
    #dg-tour .dgt-t{font-family:var(--display);font-size:13.5px;font-weight:800;letter-spacing:-.2px}
    #dg-tour .dgt-b{font-size:12px;line-height:1.65;color:var(--muted)}
    #dg-tour .dgt-b b{color:#c9a227;font-weight:600}
    :root[data-theme="light"] #dg-tour .dgt-b b{color:#8c5314}
    #dg-tour .dgt-f{display:flex;align-items:center;gap:9px;margin-top:13px}
    #dg-tour .dgt-dots{display:flex;gap:5px;margin-right:auto}
    #dg-tour .dgt-dot{width:5px;height:5px;border-radius:50%;background:rgba(255,255,255,.18);
      transition:background .25s ease,transform .25s ease}
    :root[data-theme="light"] #dg-tour .dgt-dot{background:rgba(0,0,0,.16)}
    #dg-tour .dgt-dot.on{background:#c9a227;transform:scale(1.45)}
    #dg-tour .dgt-skip{background:none;border:none;color:var(--muted);font-size:11px;
      cursor:pointer;font-family:var(--font);text-decoration:underline dotted}
    #dg-tour .dgt-next{background:rgba(201,162,39,.16);border:1px solid rgba(201,162,39,.45);
      border-radius:20px;color:#c9a227;font-size:12px;font-weight:700;padding:7px 17px;
      cursor:pointer;font-family:var(--font);
      transition:background .16s ease,transform .18s cubic-bezier(.34,1.56,.64,1)}
    #dg-tour .dgt-next:hover{background:rgba(201,162,39,.26)}
    #dg-tour .dgt-next:active{transform:scale(.95) translateY(1px)}
    @media (prefers-reduced-motion: reduce){
      #dg-tour .dgt-hole,#dg-tour .dgt-ring,#dg-tour .dgt-box{transition:none}
      #dg-tour .dgt-paw,#dg-tour .dgt-ring{animation:none}
      #dg-tour .dgt-box{opacity:1;transform:none}
    }`;
  document.head.appendChild(css);

  const wrap = document.createElement('div');
  wrap.id = 'dg-tour';
  wrap.innerHTML =
    `<div class="dgt-veil"></div><div class="dgt-hole"></div><div class="dgt-ring"></div>
     <div class="dgt-box">
       <div class="dgt-h">
         <svg class="dgt-paw" viewBox="0 0 24 24" fill="#c9a227" aria-hidden="true">
           <ellipse cx="7" cy="8" rx="2.1" ry="2.9"/><ellipse cx="12" cy="6.2" rx="2.1" ry="3"/>
           <ellipse cx="17" cy="8" rx="2.1" ry="2.9"/>
           <path d="M12 10.5c3.4 0 5.6 2.4 5.6 4.8 0 2.2-1.9 3.4-5.6 3.4s-5.6-1.2-5.6-3.4c0-2.4 2.2-4.8 5.6-4.8z"/>
         </svg>
         <div class="dgt-t"></div>
       </div>
       <div class="dgt-b"></div>
       <div class="dgt-f">
         <div class="dgt-dots"></div>
         <button class="dgt-skip" onclick="digsTourStop(true)"></button>
         <button class="dgt-next" onclick="digsTourNext()"></button>
       </div>
     </div>`;
  document.body.appendChild(wrap);

  // Esc закрывает — тур не должен быть ловушкой.
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape' && document.getElementById('dg-tour').classList.contains('on'))
      digsTourStop(true);
  });
  window.addEventListener('resize', () => {
    if (document.getElementById('dg-tour').classList.contains('on')) _dgTourPlace();
  });
}

function _dgTourPlace() {
  const root = document.getElementById('dg-tour');
  const step = _dgTourSteps[_dgTourIdx];
  if (!root || !step) return;

  const hole = root.querySelector('.dgt-hole');
  const ring = root.querySelector('.dgt-ring');
  const veil = root.querySelector('.dgt-veil');
  const box  = root.querySelector('.dgt-box');
  const el   = step.sel ? document.querySelector(step.sel) : null;
  const pad  = 8;
  const vw = window.innerWidth, vh = window.innerHeight;
  const bh = box.offsetHeight || 190, bw = box.offsetWidth || 400;

  if (el) {
    veil.classList.remove('on');
    const r = el.getBoundingClientRect();
    // Блок опор вкуса выше экрана целиком: если не обрезать вырез по видимой
    // области, рамка уходит за нижний край, а подсказка вслед за ней — на шаге
    // 6 от неё была видна одна кромка.
    const top = Math.max(6, r.top - pad);
    const bottom = Math.min(vh - 6, r.bottom + pad);
    const left = Math.max(6, r.left - pad);
    const w = Math.min(r.width + pad * 2, vw - left - 6);
    const h = Math.max(24, bottom - top);
    [hole, ring].forEach(n => {
      n.style.display = ''; n.style.top = top + 'px'; n.style.left = left + 'px';
      n.style.width = w + 'px'; n.style.height = h + 'px';
    });
    // Подсказка под вырезом; не влезла снизу — над ним; не влезла и там —
    // прижимаем к низу экрана. Она обязана остаться видимой целиком.
    const below = top + h + 14;
    let bt = below;
    if (below + bh + 10 > vh) bt = (top - bh - 14 >= 10) ? top - bh - 14 : vh - bh - 10;
    box.style.top  = Math.max(10, bt) + 'px';
    box.style.left = Math.min(Math.max(10, left), vw - bw - 14) + 'px';
  } else {
    // Шаг без цели: подсвечивать нечего, поэтому затемняем весь экран.
    [hole, ring].forEach(n => { n.style.display = 'none'; });
    veil.classList.add('on');
    box.style.top  = Math.max(10, vh / 2 - bh / 2) + 'px';
    box.style.left = Math.max(10, vw / 2 - bw / 2) + 'px';
  }
}

function _dgTourRender() {
  const root = document.getElementById('dg-tour');
  const step = _dgTourSteps[_dgTourIdx];
  if (!root || !step) return;
  const box = root.querySelector('.dgt-box');
  const last = _dgTourIdx === _dgTourSteps.length - 1;

  box.classList.remove('in');
  root.querySelector('.dgt-t').textContent = t(step.k + '_t');
  root.querySelector('.dgt-b').innerHTML   = t(step.k + '_b');
  root.querySelector('.dgt-skip').textContent = t(last ? 'dgtour.close' : 'dgtour.skip');
  root.querySelector('.dgt-next').textContent = t(last ? 'dgtour.done' : 'dgtour.next');
  root.querySelector('.dgt-dots').innerHTML = _dgTourSteps
    .map((_, i) => `<div class="dgt-dot${i === _dgTourIdx ? ' on' : ''}"></div>`).join('');

  const el = step.sel ? document.querySelector(step.sel) : null;
  if (el) {
    // Высокий блок по центру не поставить — центрирование увело бы его начало
    // за верхний край. Такие показываем с начала.
    const tall = el.getBoundingClientRect().height > window.innerHeight * 0.6;
    el.scrollIntoView({ behavior: _dgTourReduced() ? 'auto' : 'smooth',
                        block: tall ? 'start' : 'center' });
  }

  // Ждём, пока прокрутка РЕАЛЬНО остановится, а не фиксированные 260 мс: путь
  // до блока опор длинный, плавная прокрутка на нём не успевает, и рамка
  // вставала по старым координатам — за нижним краем экрана.
  if (!el) { setTimeout(() => { _dgTourPlace(); box.classList.add('in'); }, 30); return; }
  let prevTop = null, still = 0;
  const settle = () => {
    const now = Math.round(el.getBoundingClientRect().top);
    still = (now === prevTop) ? still + 1 : 0;
    prevTop = now;
    _dgTourPlace();                       // рамка едет вместе с прокруткой
    if (still >= 3) { box.classList.add('in'); return; }
    if (settle.n = (settle.n || 0) + 1, settle.n > 40) { box.classList.add('in'); return; }
    requestAnimationFrame(settle);
  };
  requestAnimationFrame(settle);
}

function digsTourNext() {
  if (_dgTourIdx >= _dgTourSteps.length - 1) return digsTourStop(true);
  _dgTourIdx++;
  _dgTourRender();
}

function digsTourStop(remember) {
  const root = document.getElementById('dg-tour');
  if (root) root.classList.remove('on');
  if (!remember) return;
  // Пишем в конфиг, а не в localStorage: оболочек у Ripster две.
  try {
    S.config[DG_TOUR_KEY] = true;
    api('POST', '/api/config', { [DG_TOUR_KEY]: true });
  } catch (e) { /* не смогли запомнить — тур покажется ещё раз, это не поломка */ }
}

function digsTourStart() {
  _dgTourEnsureDom();
  // Шаги без своих элементов выкидываем: рассказывать про то, чего на экране
  // нет, — верный способ, чтобы тур закрыли на втором шаге.
  _dgTourSteps = DG_TOUR_STEPS.filter(s => !s.sel || document.querySelector(s.sel));
  if (!_dgTourSteps.length) return;
  _dgTourIdx = 0;
  document.getElementById('dg-tour').classList.add('on');
  _dgTourRender();
}

/* Автозапуск — только при первом заходе и только когда вкладке есть что
 * показать. Тур на пустом экране бесполезен: человек ещё ничего не скачал,
 * подсвечивать нечего. */
function digsTourMaybeAuto() {
  try {
    if (S.config[DG_TOUR_KEY]) return;
    if (!document.querySelector('#view-digs .dg-item')) return;
    setTimeout(digsTourStart, 420);
  } catch (e) { /* конфиг ещё не пришёл — покажем в следующий заход */ }
}
