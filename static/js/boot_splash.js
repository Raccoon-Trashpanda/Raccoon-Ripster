// ======================================================================
// Заставка запуска.
//
// ЗАЧЕМ. Между открытием окна и первым кадром интерфейса проходит заметная
// пауза: тянутся фрагменты вкладок, поднимается вебсокет, приходит конфиг. До
// сих пор в этой паузе был пустой тёмный прямоугольник, и он читается как
// «не запустилось» — особенно на холодном старте, где ждать дольше всего.
//
// ЧТО ПОКАЗЫВАЕМ. Пластинку с иглой и звуковую волну — то, чем программа
// занимается. Не логотип-заглушку: анимация ЖИВАЯ, поэтому по ней видно, что
// процесс идёт, а не завис.
//
// КОГДА УХОДИТ. По приходу первого кадра данных (WS-init) — то есть ровно
// тогда, когда показывать уже есть что. Плюс жёсткий предел: если инициализация
// не пришла за 12 секунд, заставка всё равно уходит. Экран, с которого нельзя
// уйти, — худшее, чем может кончиться «красивая анимация».
//
// УВАЖАЕМ НАСТРОЙКИ. При `prefers-reduced-motion` ничего не крутится: остаётся
// та же карточка со статичной пластинкой.
// ======================================================================

(function () {
  const MAX_MS = 12000;              // дольше не держим ни при каких условиях
  let gone = false;

  function build() {
    if (document.getElementById('boot-splash')) return;
    const el = document.createElement('div');
    el.id = 'boot-splash';
    el.setAttribute('role', 'status');
    el.setAttribute('aria-live', 'polite');
    el.innerHTML = `
      <div class="bs-card">
        <div class="bs-disc" aria-hidden="true">
          <div class="bs-vinyl">
            <span class="bs-groove g1"></span>
            <span class="bs-groove g2"></span>
            <span class="bs-groove g3"></span>
            <span class="bs-label"></span>
          </div>
          <div class="bs-arm"></div>
        </div>
        <div class="bs-wave" aria-hidden="true">
          <i></i><i></i><i></i><i></i><i></i><i></i><i></i><i></i><i></i>
        </div>
        <div class="bs-name">Ripster</div>
        <div class="bs-sub" id="bs-sub"></div>
      </div>`;
    document.body.appendChild(el);
    // Подпись — через словарь: заставка тоже часть интерфейса.
    const sub = document.getElementById('bs-sub');
    if (sub) sub.textContent = (typeof t === 'function') ? t('boot.starting') : '';
  }

  function hide(why) {
    if (gone) return;
    gone = true;
    const el = document.getElementById('boot-splash');
    if (!el) return;
    el.classList.add('off');
    setTimeout(() => { try { el.remove(); } catch (e) {} }, 420);
    if (why) console.log('[splash] закрыта:', why);
  }

  // Наружу — чтобы обработчик WS-init мог закрыть заставку в тот же момент,
  // когда показывать становится что.
  window.bootSplashHide = () => hide('init');

  build();
  setTimeout(() => hide('таймаут'), MAX_MS);
  // Если данные уже пришли до подключения этого файла — не висим зря.
  document.addEventListener('ripster:init', () => hide('init'), { once: true });

  // Отчёт самопроверки — в консоль приложения. В стандартный вывод он уже ушёл,
  // но его видит только тот, кто запускал из терминала; в окне программы такого
  // вывода нет вовсе.
  setTimeout(async () => {
    try {
      const r = await api('GET', '/api/selfcheck');
      if (!r || !r.checks) return;
      const bad = r.checks.filter(c => !c.ok);
      const line = bad.length
        ? ti('boot.check_bad', { n: bad.length })
        : ti('boot.check_ok', { n: r.checks.length });
      if (typeof addLog === 'function') {
        addLog(line, bad.length ? 'warn' : 'info');
        bad.forEach(c => addLog(`  ${c.name}: ${c.detail}`, 'warn'));
      } else {
        console.log('[самопроверка]', line, bad);
      }
    } catch (e) { /* не ответила — молчим, это не повод шуметь при запуске */ }
  }, 3000);
})();
