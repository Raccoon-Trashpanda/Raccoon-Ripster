// ══ СЛОВО ВЛАДЕЛЯ: «это не мой артист» / «это мой» ══════════════════
//
// Якорь v5 выводит чужого однофамильца из косвенных дел владельца (что качал,
// что звучало в станции). Одно нажатие на карточке — единственное доказательство
// ПРЯМОЕ, и оно сильнее любого правила. Живёт на сервере (`owner_feedback.json`,
// POST /api/identity/feedback), а не в localStorage: отзыв обязан переживать
// смену браузера, перепривязку подписки и перезапуск, и уезжать в резервную
// копию настроек.
//
// Жест лечит НЕ одну карточку: сервер запоминает, чем карточка выдала себя (id
// артиста в чужой витрине, лейбл, функциональный узор заголовка), и прячет всё
// похожее без новых жалоб. Ответ говорит, сколько ЕЩЁ карточек задето, — иначе
// «вылечено навсегда» было бы просто словами.
//
// Кнопка вешается обработчиком на весь документ (делегирование), а не
// инлайн-onclick с заэкранированным названием: релизы зовутся «Fat», «L'Amour»,
// «Q&A» — любая кавычка ломала бы разметку. Данные лежат в `data-*` атрибутах.
var _nmaHidden = [];
var _nmaHiddenTs = 0;
var _nmaHiddenTTL = 60 * 1000;
var _nmaLoading = false;

function nmaIsHiddenView() {
  return (typeof _relView !== 'undefined') && _relView === 'hidden';
}

// ── Кнопка ───────────────────────────────────────────────────────────────────
// Одна и та же в радаре и на экране «Скрытые»: там — «вернуть», тут — «не мой».
function nmaButton(rel, style) {
  if (!rel || !(rel.artist || rel.alb_artist)) return '';
  const mine = nmaIsHiddenView();
  const label = mine ? t('nma.mine') : t('nma.not_mine');
  const clr = mine ? 'var(--green)' : 'var(--orange)';
  return `<button class="nma-btn${mine ? ' nma-mine' : ''}" data-nma="${mine ? 'mine' : 'not_mine'}"
    data-artist="${esc(rel.artist || rel.alb_artist || '')}"
    data-title="${esc(rel.title || rel.name || '')}"
    data-service="${esc(rel.service || '')}"
    data-id="${esc(rel.id || '')}"
    data-artistid="${esc(rel.artist_id || '')}"
    data-url="${esc(rel.url || '')}"
    data-label="${esc(rel.label || '')}"
    data-date="${esc(rel.date || '')}"
    data-genres="${esc((rel.genres || rel.genre || []).toString())}"
    data-uid="${esc(_relUID(rel))}"
    title="${esc(label)}" aria-label="${esc(label)}"
    style="background:transparent;border:1px solid var(--border);border-radius:7px;font-size:11px;color:${clr};cursor:pointer;font-family:var(--font)">${mine ? '↩' : '⤫'}</button>`;
}

async function nmaSend(btn) {
  const d = btn.dataset;
  const body = {
    verdict: d.nma,
    item: {
      artist: d.artist || '', title: d.title || '', service: d.service || '',
      id: d.id || '', artist_id: d.artistid || '', url: d.url || '',
      label: d.label || '', date: d.date || '',
      genres: d.genres ? d.genres.split(',').filter(Boolean) : []
    }
  };
  btn.disabled = true;
  let r = null;
  try {
    r = await api('POST', '/api/identity/feedback', body);
  } catch (e) {
    r = { ok: false, error: String(e && e.message || e) };
  } finally {
    btn.disabled = false;
  }
  if (!r || !r.ok) {
    toast(t('t.error_c') + ((r && (r.error || r.detail)) || '?'), 'var(--red)');
    return;
  }
  if (d.nma === 'not_mine') {
    // Карточка уходит из ленты сразу: ждать перечитывания — значит дать
    // человеку нажать «не мой» второй раз.
    nmaDropCard(d.uid);
    nmaInvalidateHidden();
    toast(ti('nma.hidden_toast', { title: d.title })
      + (r.generalised ? ' · ' + ti('nma.also_hidden', { n: r.generalised }) : ''),
      'var(--green)');
  } else {
    nmaDropCard(d.uid);
    nmaInvalidateHidden();
    toast(ti('nma.returned_toast', { title: d.title })
      + (r.generalised ? ' · ' + ti('nma.also_returned', { n: r.generalised }) : ''),
      'var(--green)');
  }
}

function nmaDropCard(uid) {
  if (nmaIsHiddenView()) {
    _nmaHidden = _nmaHidden.filter(h => _relUID(h) !== uid);
    if (typeof _applyRelFilter === 'function') _applyRelFilter(false);
    return;
  }
  const card = document.querySelector('.rel-card[data-uid="' + (window.CSS && CSS.escape ? CSS.escape(uid) : uid) + '"]');
  if (card) card.remove();
}

// ── Экран «Скрытые» ──────────────────────────────────────────────────────────
// Всё, что дверь не выпустила в ленту, обязано быть видимым и возвратным:
// скрытое без экрана — это молчаливая цензура чужой находки.
function nmaHiddenItems() { return _nmaHidden; }
function nmaHiddenCount() { return _nmaHidden.length; }
function nmaInvalidateHidden() { _nmaHiddenTs = 0; }

async function nmaLoadHidden(force) {
  if (!force && _nmaHidden.length && (Date.now() - _nmaHiddenTs) < _nmaHiddenTTL) {
    return _nmaHidden;
  }
  if (_nmaLoading) return _nmaHidden;
  _nmaLoading = true;
  try {
    const r = await api('GET', '/api/identity/hidden');
    _nmaHidden = (r && r.items) || [];
    _nmaHiddenTs = Date.now();
  } catch (e) {
    _nmaHidden = [];
  } finally {
    _nmaLoading = false;
  }
  return _nmaHidden;
}

// Открытие вида «Скрытые»: список добирается с сервера один раз и на ту же
// минуту переиспользуется (дешевле, чем гонять /api/identity при каждом чипе).
function nmaOpenHidden() {
  nmaLoadHidden().then(list => {
    _nmaHidden = list;
    if (typeof _applyRelFilter === 'function') _applyRelFilter(true);
  });
}

document.addEventListener('click', function (ev) {
  const btn = ev.target && ev.target.closest ? ev.target.closest('.nma-btn') : null;
  if (!btn) return;
  ev.preventDefault();
  ev.stopPropagation();
  nmaSend(btn);
});
