// ======================================================================
// Кнопка «наверх» — для любого длинного списка.
//
// ПОЧЕМУ БЕЗ СПИСКА СЕЛЕКТОРОВ. Прокручиваемых контейнеров в приложении
// минимум шесть и они разные: `.view-body` у большинства вкладок, свой блок у
// радара, `.queue-list`, `.console`, тело карточки SoundCloud, сама страница.
// Захардкодить их — значит забыть седьмой и получить вкладку, где кнопки нет.
// Поэтому слушаем прокрутку глобально и запоминаем ТОТ элемент, который
// прокрутили последним: кнопка всегда относится к тому, что человек и листает.
//
// Событие `scroll` не всплывает, поэтому слушаем в фазе ПЕРЕХВАТА на document —
// иначе до нас дойдёт только прокрутка самой страницы.
//
// Поводом была долгая прокрутка релиз-радара: карточек сотни, и возвращаться
// наверх колесом — минуты.
// ======================================================================

(function () {
  const SHOW_AT = 420;          // ниже этого возврат наверх и так недолог
  let scroller = null;          // что листали последним
  let btn = null;

  function topOf(el) {
    return el === document || el === document.documentElement || el === window
      ? (window.scrollY || document.documentElement.scrollTop || 0)
      : (el.scrollTop || 0);
  }

  function ensureBtn() {
    if (btn) return btn;
    btn = document.createElement('button');
    btn.id = 'scroll-top-btn';
    btn.type = 'button';
    btn.setAttribute('aria-label', (typeof t === 'function' ? t('ui.to_top') : 'Наверх'));
    btn.title = btn.getAttribute('aria-label');
    // Стрелка рисуется, а не пишется символом: текстовые глифы в этом интерфейсе
    // уже подводили — часть шрифтов подставляет эмодзи-начертание.
    btn.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true">'
      + '<path d="M12 5 L12 19 M12 5 L5 12 M12 5 L19 12"/></svg>';
    btn.addEventListener('click', () => {
      const el = scroller;
      if (!el) return;
      try {
        if (el === document || el === document.documentElement || el === window) {
          window.scrollTo({ top: 0, behavior: 'smooth' });
        } else {
          el.scrollTo({ top: 0, behavior: 'smooth' });
        }
      } catch (e) {
        // Старый движок без плавной прокрутки — прыгаем сразу, это лучше, чем ничего.
        if (el.scrollTop != null) el.scrollTop = 0; else window.scrollTo(0, 0);
      }
    });
    document.body.appendChild(btn);
    return btn;
  }

  function refresh() {
    if (!scroller) return;
    const b = ensureBtn();
    b.classList.toggle('on', topOf(scroller) > SHOW_AT);
  }

  let ticking = false;
  document.addEventListener('scroll', (e) => {
    const el = e.target === document ? document.documentElement : e.target;
    if (!el) return;
    scroller = el;
    if (ticking) return;         // не чаще кадра — прокрутка и так самое горячее место
    ticking = true;
    requestAnimationFrame(() => { ticking = false; refresh(); });
  }, true);

  // Смена вкладки: старая позиция больше не наша, прячем до первой прокрутки.
  document.addEventListener('click', (e) => {
    if (e.target && e.target.closest && e.target.closest('.nav-item')) {
      scroller = null;
      if (btn) btn.classList.remove('on');
    }
  }, true);
})();
