// ======================================================================
// «В любимые артисты» — прямо с карточки.
//
// Раньше отметить артиста можно было только в Раскопках. А натыкаешься на него
// чаще всего там, где он и попался: в карточке релиза или в выдаче поиска — и
// приходилось идти в другую вкладку и набирать имя руками.
//
// ОТМЕЧАЕМ ЧЕРЕЗ ВИШЛИСТ, а не заводим третий список. У вишлиста уже есть
// проверка новых релизов, автоскачка и вся обвязка; параллельный «список
// любимых» означал бы две судьбы одного намерения. Ровно по этой причине так же
// поступают Раскопки (digs.js::digsWatch).
//
// СЛЕДИМ ВСЕГДА ЧЕРЕЗ APPLE — это единственный полный каталог по артисту,
// доступный без подписки; поле `service` говорит лишь КУДА качать, а не где
// следить. Иначе артист, найденный в Qobuz, не отслеживался бы вовсе.
// ======================================================================

const _afFollowed = new Set();   // нормализованные имена — чтобы не жать дважды
let _afLoaded = false;

function _afNorm(s) {
  return String(s || '').trim().toLowerCase().replace(/\s+/g, ' ');
}

// Кого уже отслеживаем — чтобы кнопка сразу рисовалась в правильном состоянии,
// а не «предлагала добавить» того, кто добавлен.
async function afLoadFollowed() {
  if (_afLoaded) return _afFollowed;
  _afLoaded = true;
  try {
    const r = await api('GET', '/api/watchlist');
    const items = (r && (r.items || r.watchlist)) || (Array.isArray(r) ? r : []);
    items.forEach(it => {
      const n = it && (it.name || it.artist || it.query);
      if (n) _afFollowed.add(_afNorm(n));
    });
  } catch (e) { /* не ответил — состояние узнаем по факту нажатия */ }
  return _afFollowed;
}

function afIsFollowed(name) { return _afFollowed.has(_afNorm(name)); }

// Разметка кнопки. Одна и та же в радаре и в поиске — чтобы жест был один.
function afButton(name, extraStyle) {
  const on = afIsFollowed(name);
  const enc = encodeURIComponent(name || '');
  return `<button class="af-btn${on ? ' on' : ''}" data-af="${esc(name || '')}"
    onclick="event.stopPropagation();followArtist(decodeURIComponent('${enc}'),this)"
    title="${on ? t('af.following') : t('af.follow')}"
    aria-label="${on ? t('af.following') : t('af.follow')}"
    style="${extraStyle || ''}">${on ? '♥' : '♡'}</button>`;
}

async function followArtist(name, btn) {
  if (!name) return;
  if (afIsFollowed(name)) { toast(ti('af.already', { name }), 'var(--muted)'); return; }
  if (btn) btn.disabled = true;
  try {
    await api('POST', '/api/watchlist', { name, service: 'apple', kind: 'artist' });
    _afFollowed.add(_afNorm(name));
    // Обновляем ВСЕ кнопки этого артиста на экране: один и тот же человек
    // попадается в нескольких карточках сразу, и рассинхрон читается как сбой.
    document.querySelectorAll('.af-btn[data-af]').forEach(b => {
      if (_afNorm(b.dataset.af) === _afNorm(name)) {
        b.classList.add('on');
        b.textContent = '♥';
        b.title = b.ariaLabel = t('af.following');
      }
    });
    toast(ti('af.added', { name }), 'var(--green)');
  } catch (e) {
    toast('✗ ' + (e && e.message || e), 'var(--red)');
  } finally {
    if (btn) btn.disabled = false;
  }
}

// Список подтягиваем один раз при первом показе карточек — не на старте
// приложения, чтобы не добавлять запрос в и без того плотный запуск.
document.addEventListener('click', (e) => {
  if (e.target && e.target.closest && e.target.closest('.nav-item')) afLoadFollowed();
}, true);
setTimeout(afLoadFollowed, 2500);
