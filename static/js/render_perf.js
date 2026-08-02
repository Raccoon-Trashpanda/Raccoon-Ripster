// ======================================================================
// Обложки: декодирование вне главного потока.
//
// `decoding="async"` разрешает браузеру раскодировать картинку в стороне и не
// держать кадр. Без него декодирование крупной обложки происходит СИНХРОННО при
// отрисовке — и именно оно даёт рывок при листании сетки, где картинок сотня.
// `loading="lazy"` добавляет второе: за экраном картинка вообще не грузится.
//
// ПОЧЕМУ ОДНО МЕСТО, А НЕ ПРАВКА ШАБЛОНОВ. Карточки собираются строками HTML в
// доброй дюжине мест (радар, поиск, история, очередь, Раскопки, SoundCloud,
// Кодер…). Проставить атрибуты в каждом — значит забыть тринадцатое и потом
// гадать, почему «где-то дёргается». Замер 01.08.2026 нашёл ровно такие
// забытые: 7 картинок в радаре шли без `decoding`.
//
// ЦЕНА НАБЛЮДЕНИЯ. Наблюдатель следит за одним узлом (`.main`), работу копит и
// делает раз в кадр, трогает только `img:not([decoding])` — то есть каждую
// картинку ровно один раз за жизнь. Это дешевле, чем один синхронный декод.
// ======================================================================

(function () {
  function boost(root) {
    const imgs = (root || document).querySelectorAll('img:not([decoding])');
    if (!imgs.length) return 0;
    imgs.forEach(im => {
      im.setAttribute('decoding', 'async');
      // Ленивую загрузку не навязываем тому, кто уже над экраном: у обложки
      // играющего трека и у аватара в шапке задержка была бы заметна.
      if (!im.hasAttribute('loading') && !im.closest('#preview-player, .topbar, .sidebar')) {
        im.setAttribute('loading', 'lazy');
      }
    });
    return imgs.length;
  }

  let pending = false;
  const kick = () => {
    if (pending) return;
    pending = true;
    requestAnimationFrame(() => { pending = false; boost(); });
  };

  function start() {
    const host = document.querySelector('.main') || document.body;
    if (!host || host.dataset.perfWatch) return;
    host.dataset.perfWatch = '1';
    new MutationObserver(kick).observe(host, { childList: true, subtree: true });
    boost();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }
  // Вьюхи подгружаются лениво — к моменту первого запуска `.main` может быть
  // пуст, поэтому пробуем ещё раз, когда человек открыл вкладку.
  document.addEventListener('click', (e) => {
    if (e.target && e.target.closest && e.target.closest('.nav-item')) setTimeout(start, 300);
  }, true);

  window.imgBoost = boost;   // для ручной проверки и разовых прогонов
})();
