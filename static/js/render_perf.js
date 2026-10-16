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

// ======================================================================
// Обложка не доехала — карточка НЕ должна схлопываться.
//
// Раньше в сетках стоял `onerror="this.style.display='none'"`. У одной битой
// ссылки это выглядит терпимо, но обрыв связи (переключение VPN, засыпание
// машины, троттл витрины) гасит ВСЕ картинки сетки разом — и тогда у каждой
// карточки исчезает не только обложка, но и её место: бейдж «Альбом», который
// стоит поверх обложки, падает на название и печатается прямо по нему. Именно
// это и было на экране 23.08: читалось «Albumодняя» вместо «Новогодняя».
//
// Два правила. ПЕРВОЕ: одна повторная попытка через секунду — сетевой обрыв
// разовый, а без ретрая обложки не вернутся до перезагрузки страницы. Второе
// обращение идёт с меткой, иначе браузер отдаёт свой же отрицательный кэш.
// ВТОРОЕ: если и она не доехала — на место картинки встаёт заглушка ТОГО ЖЕ
// размера. Карточка остаётся карточкой, текст остаётся читаемым.
// ======================================================================
window.coverFail = function (img) {
  if (!img || img.dataset.covDead) return;
  if (!img.dataset.covRetry) {
    img.dataset.covRetry = '1';
    const src = img.currentSrc || img.src;
    if (src) {
      setTimeout(() => {
        img.src = src + (src.indexOf('?') < 0 ? '?' : '&') + '_r=1';
      }, 1000);
      return;
    }
  }
  img.dataset.covDead = '1';
  const cs  = getComputedStyle(img);
  const ph  = document.createElement('div');
  ph.textContent = '♪';
  ph.style.cssText = 'display:flex;align-items:center;justify-content:center;'
    + 'background:rgba(255,255,255,.05);color:var(--muted2)';
  // Размер берём из ИНЛАЙНОВОГО стиля картинки, а не из вычисленного: у сеток
  // там `width:100%`, и подстановка пикселей сломала бы резину при ресайзе.
  ph.style.width  = img.style.width  || cs.width;
  if (img.style.height && img.style.height !== 'auto') ph.style.height = img.style.height;
  else ph.style.aspectRatio = '1';
  ph.style.fontSize     = (parseFloat(cs.width) > 160) ? '48px' : '28px';
  ph.style.borderRadius = cs.borderRadius;
  ph.style.flexShrink   = cs.flexShrink;
  img.replaceWith(ph);
};
