// ======================================================================
// Подсказки в простое: «мы знакомы» и «послушай».
//
// ЗАЧЕМ. Когда ничего не играет, фонотека молчит — хотя в ней лежит то, что
// человек любил и забыл, а в его жанрах выходят релизы. Подсказка занимает эту
// паузу, а не отвлекает от дела.
//
// КОГДА ПОЯВЛЯЕТСЯ. Три условия ОДНОВРЕМЕННО, и каждое обязательно:
//   · ничего не играет — перебивать музыку предложением музыки бессмысленно;
//   · человек не трогал приложение N минут — иначе это помеха работе;
//   · вкладка на экране — фоновой вкладке подсказка не нужна вовсе.
//
// ПЕРИОДИЧНОСТЬ РАСТЁТ. Первая пауза короткая, каждая следующая длиннее: если
// подсказки не пригождаются, они сами становятся реже, а не долбят в одном
// ритме. Закрыл вручную — интервал удваивается сразу.
//
// ВСЕГДА ЕСТЬ ДЕЙСТВИЕ. Артист — клик открывает дискографию. Релиз — кнопка
// воспроизведения прямо в подсказке, чтобы согласие стоило одного движения.
// Подсказка без действия — просто шум.
//
// ВЫКЛЮЧАЕТСЯ. `nudges-enabled` в настройках; по умолчанию включено, но одно
// нажатие — и тишина.
// ======================================================================

const _NG = {
  idleMs: 4 * 60 * 1000,      // столько тишины до первой подсказки
  maxMs: 40 * 60 * 1000,      // дальше не растягиваем — иначе это уже никогда
  lastAct: Date.now(),
  nextAt: 0,
  shown: 0,
  timer: null,
  open: false,
};

function _ngEnabled() {
  const v = (typeof S !== 'undefined' && S.config) ? S.config['nudges-enabled'] : true;
  return v === undefined || v === null ? true : !!v;
}

function _ngPlaying() {
  // Играет что угодно: превью-плеер, BBC-поток, аудио-элемент вкладки.
  try {
    const els = document.querySelectorAll('audio, video');
    for (const a of els) if (!a.paused && !a.ended && a.currentTime > 0) return true;
  } catch (e) {}
  return false;
}

function _ngTouch() { _NG.lastAct = Date.now(); }

function _ngClose() {
  const el = document.getElementById('ng-pop');
  if (el) el.remove();
  _NG.open = false;
}

// Закрыли руками — считаем это «не сейчас» и отступаем вдвое дальше.
function ngDismiss() {
  _NG.idleMs = Math.min(_NG.maxMs, _NG.idleMs * 2);
  _NG.nextAt = Date.now() + _NG.idleMs;
  _ngClose();
}

function ngDisable() {
  _ngClose();
  if (typeof saveSetting === 'function') saveSetting('nudges-enabled', false);
  if (typeof S !== 'undefined' && S.config) S.config['nudges-enabled'] = false;
  if (typeof toast === 'function') toast(t('nudge.off_toast'), 'var(--muted)');
}

function ngOpenArtist(name) {
  _ngClose();
  // Дискография — через поиск по артисту: он работает для любого сервиса, а
  // прямая страница артиста требует id, которого у подсказки нет.
  const nav = document.querySelector('.nav-item[data-view="search"]');
  if (typeof showView === 'function') showView('search', nav);
  setTimeout(() => {
    const q = document.getElementById('search-q');
    const ty = document.getElementById('search-type');
    if (q) q.value = name;
    if (ty) ty.value = 'album';
    if (typeof doSearch === 'function') doSearch();
  }, 400);
}

function ngPlay(service, url, title, artist, cover) {
  _ngClose();
  if (typeof playRelease === 'function') playRelease(service, url, title, artist, cover);
}

function _ngRender(d) {
  _ngClose();
  const why = d.why_key && typeof ti === 'function' ? ti(d.why_key, d.why_args || {}) : '';
  const el = document.createElement('div');
  el.id = 'ng-pop';
  el.setAttribute('role', 'status');

  if (d.kind === 'artist') {
    const enc = encodeURIComponent(d.name || '');
    el.innerHTML = `
      <div class="ng-head">${t('nudge.known_title')}</div>
      <div class="ng-body" onclick="ngOpenArtist(decodeURIComponent('${enc}'))" title="${t('nudge.open_disco')}">
        ${d.cover ? `<img class="ng-art" src="${esc(d.cover)}" loading="lazy" decoding="async" alt="">`
                  : '<div class="ng-art">♪</div>'}
        <div class="ng-txt">
          <div class="ng-name">${esc(d.name || '')}</div>
          <div class="ng-why">${esc(why)}</div>
        </div>
      </div>
      <div class="ng-acts">
        <button class="ng-go" onclick="ngOpenArtist(decodeURIComponent('${enc}'))">${t('nudge.open_disco')}</button>
        <button class="ng-x" onclick="ngDismiss()" title="${t('nudge.later')}">×</button>
        <button class="ng-off" onclick="ngDisable()" title="${t('nudge.off')}">${t('nudge.off')}</button>
      </div>`;
  } else {
    const a = ['service', 'url', 'title', 'artist', 'cover']
      .map(k => `decodeURIComponent('${encodeURIComponent(d[k] || '')}')`).join(',');
    el.innerHTML = `
      <div class="ng-head">${t('nudge.listen_title')}</div>
      <div class="ng-body">
        ${d.cover ? `<img class="ng-art" src="${esc(d.cover)}" loading="lazy" decoding="async" alt="">`
                  : '<div class="ng-art">♪</div>'}
        <div class="ng-txt">
          <div class="ng-name">${esc(d.artist || '')} — ${esc(d.title || '')}</div>
          <div class="ng-why">${esc(why)}</div>
        </div>
      </div>
      <div class="ng-acts">
        <button class="ng-go" onclick="ngPlay(${a})">${t('nudge.play')}</button>
        <button class="ng-x" onclick="ngDismiss()" title="${t('nudge.later')}">×</button>
        <button class="ng-off" onclick="ngDisable()" title="${t('nudge.off')}">${t('nudge.off')}</button>
      </div>`;
  }
  document.body.appendChild(el);
  requestAnimationFrame(() => el.classList.add('on'));
  _NG.open = true;
  _NG.shown++;
  // Сама уезжает: подсказка, которую не заметили, не должна висеть вечно.
  setTimeout(() => { if (_NG.open) _ngClose(); }, 30000);
}

async function _ngTick() {
  if (!_ngEnabled() || _NG.open) return;
  if (document.hidden) return;
  if (_ngPlaying()) { _NG.lastAct = Date.now(); return; }
  const now = Date.now();
  if (now - _NG.lastAct < _NG.idleMs) return;
  if (now < _NG.nextAt) return;
  try {
    const d = await api('GET', '/api/nudge');
    if (d && d.ok) _ngRender(d);
  } catch (e) { /* нечего предложить — молчим, это нормальный ответ */ }
  // Следующая — позже предыдущей: не пригодилось, значит реже.
  _NG.idleMs = Math.min(_NG.maxMs, Math.round(_NG.idleMs * 1.6));
  _NG.nextAt = Date.now() + _NG.idleMs;
}

(function _ngStart() {
  ['pointerdown', 'keydown', 'wheel', 'touchstart'].forEach(ev =>
    document.addEventListener(ev, _ngTouch, { passive: true, capture: true }));
  document.addEventListener('visibilitychange', () => { if (!document.hidden) _ngTouch(); });
  _NG.timer = setInterval(_ngTick, 20000);
})();
