/* ============================================================================
   Ряды плиток панели: мышью как пальцем (25.09.2026, жалоба владельца на ПК).

   Диагноз по замерам (headless, панель на 1440 и на 430 px, ряды жанров и
   BBC): конец строки ДОСТИЖИМ — scrollLeft_max = scrollWidth − clientWidth,
   последний тайл встаёт в 20 px от правого края ряда (это же padding-right
   ряда, в scrollWidth оно входит). Недоступен он был по другой причине:
   полоса прокрутки ряда спрятана намеренно (scrollbar-width:none), мышью ряд
   не тянется, а колесо мыши над рядом листает ВЕРТИКАЛЬНЫЙ скроллер-родитель
   — сам ряд не двигался вообще, и до хвоста дойти было нечем.

   Поэтому: • перетаскивание мышью (pointer events) с гашением клика после
   драга больше 5 px — обычный клик по-прежнему открывает плитку;
   • колесо мыши над рядом → горизонтальный скролл без Shift; когда ряд
   упёрся в свой край, колесо работает как раньше (листает панель);
   • курсор grab/grabbing, выделение текста и нативный image-drag подавлены.
   Пальцев (touch/pen) файл не касается: нативный прокруточный жест не меняем.

   Делегирование на document + closest(): ряды, отрисованные ПОЗЖЕ (gtiles и
   bbcrow перерисовываются на каждую загрузку данных), подхватываются сами —
   новый ряд достаточно пометить атрибутом data-mxrow.
   ========================================================================= */
(function () {
  'use strict';

  var ROWS = '.gtiles, .bbcrow, [data-mxrow]';
  var DRAG_SLOP = 5;   // px: больше — это перетаскивание, а не клик по плитке

  function rowOf(el) {
    var r = el && el.closest ? el.closest(ROWS) : null;
    // Тянуть некуда — ряд целиком влезает: не мешаем обычному клику.
    if (!r || r.scrollWidth <= r.clientWidth + 1) return null;
    return r;
  }

  var drag = null;   // {row, id, x0, y0, sl0, moved}

  document.addEventListener('pointerdown', function (e) {
    if (e.pointerType !== 'mouse' || e.button !== 0) return;
    var row = rowOf(e.target);
    if (!row) return;
    drag = { row: row, id: e.pointerId, x0: e.clientX, y0: e.clientY,
             sl0: row.scrollLeft, moved: false };
  });

  document.addEventListener('pointermove', function (e) {
    if (!drag || e.pointerId !== drag.id) return;
    var dx = e.clientX - drag.x0, dy = e.clientY - drag.y0;
    if (!drag.moved) {
      if (Math.abs(dx) < DRAG_SLOP && Math.abs(dy) < DRAG_SLOP) return;
      drag.moved = true;
      drag.row.classList.add('mxDragging');
      try { drag.row.setPointerCapture(drag.id); } catch (err) {}
    }
    drag.row.scrollLeft = drag.sl0 - dx;
    e.preventDefault();
  }, { passive: false });

  function finish(e) {
    if (!drag || (e && e.pointerId !== drag.id)) return;
    var row = drag.row, moved = drag.moved;
    row.classList.remove('mxDragging');
    drag = null;
    if (!moved) return;
    // Клик по итогам перетаскивания глушится на capture-обработчике ниже;
    // отметка снимается сама, если клик почему-то не случится (например,
    // указатель отпустили вне окна).
    row.classList.add('mxSwallow');
    setTimeout(function () { row.classList.remove('mxSwallow'); }, 120);
  }
  document.addEventListener('pointerup', finish);
  document.addEventListener('pointercancel', function (e) {
    if (drag && (!e || e.pointerId === drag.id)) {
      drag.row.classList.remove('mxDragging');
      drag = null;
    }
  });

  document.addEventListener('click', function (e) {
    var row = e.target && e.target.closest ? e.target.closest('.mxSwallow') : null;
    if (!row) return;
    e.stopPropagation();
    e.preventDefault();
    row.classList.remove('mxSwallow');
  }, true);

  // Нативный перенос картинки/текста внутрь ряда не запускаем.
  document.addEventListener('dragstart', function (e) {
    if (rowOf(e.target)) e.preventDefault();
  });

  /* Колесо мыши над рядом → горизонтальный скролл ряда. Только вертикальное
     колесо: тачпад с deltaX ряд листает и без нас. deltaMode — строки/страницы
     (Firefox, старая сборка WebView2), иначе 3 «строки» прокрутят 3 px. */
  document.addEventListener('wheel', function (e) {
    if (Math.abs(e.deltaY) <= Math.abs(e.deltaX)) return;
    var row = rowOf(e.target);
    if (!row) return;
    var k = e.deltaMode === 1 ? 33 : (e.deltaMode === 2 ? row.clientWidth : 1);
    var max = row.scrollWidth - row.clientWidth, before = row.scrollLeft;
    if ((before <= 0 && e.deltaY < 0) || (before >= max && e.deltaY > 0)) return;
    row.scrollLeft = Math.max(0, Math.min(max, before + e.deltaY * k));
    if (row.scrollLeft !== before) e.preventDefault();
  }, { passive: false });
})();
