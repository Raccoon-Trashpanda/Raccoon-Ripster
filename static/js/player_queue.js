// ======================================================================
// Трек-лист плеера — как в AIMP.
//
// ЗАЧЕМ. Очередь у плеера была всегда (`Preview.queue`), но показать её было
// нечем: радио дописывало в неё треки и звало `_ppRenderQueue()`, которой не
// существовало. Со стороны это выглядело так, будто радио не работает — на
// самом деле работало, просто вслепую.
//
// КАК В AIMP. Номер, название, артист, длительность — колонками, текущий трек
// подсвечен, клик по строке переключает на неё. Ничего лишнего: это рабочий
// список, а не витрина.
//
// ДЛИТЕЛЬНОСТЬ. Берётся из данных трека, если она там есть; у играющего —
// из самого аудио. Прочерк вместо выдуманного числа: в очереди лежат треки
// разных сервисов, и не у всех длительность известна заранее.
// ======================================================================


// Элемент, который РЕАЛЬНО играет. Не «первый попавшийся audio»: как только на
// странице оказывается второй (так было с прогревом), громкость и длительность
// начинают читаться с чужого. Предпочитаем непаузированный с ненулевым временем.
function _pqAudio() {
  const list = [...document.querySelectorAll('audio')];
  return list.find(a => !a.paused && a.currentTime > 0)
      || document.getElementById('bbc-audio')
      || list[0] || null;
}

function _pqFmt(sec) {
  if (!sec || !isFinite(sec) || sec <= 0) return '—';
  const s = Math.round(sec);
  const m = Math.floor(s / 60);
  return `${m}:${String(s % 60).padStart(2, '0')}`;
}

function _pqDur(it, i) {
  // У играющего трека длительность знает сам элемент audio — она точнее любой
  // метаданной, особенно у миксов, где заявленное время часто врёт.
  if (typeof Preview !== 'undefined' && i === Preview.idx) {
    const a = _pqAudio();
    if (a && isFinite(a.duration) && a.duration > 0) return a.duration;
  }
  return Number(it.duration || it.dur || it.length || 0);
}

// Качество — ОДНИМ коротким словом, а не строкой характеристик.
//
// В трек-листе главное — что играет и сколько идёт; качество здесь подсказка,
// а не паспорт. «24/96» и «FLAC 1411 kbps» в каждой строке съедают внимание,
// поэтому сводим к одной метке: FLAC / ALAC / Hi-Res / 320. Неизвестно —
// молчим: пустое место читается лучше, чем догадка.
function _pqQuality(it) {
  // ОДИН источник правды с нижним плеером. Своя догадка по битрейту разошлась с
  // ним сразу же: FLAC на 1411 kbps попадал под правило «≥300 → 320», и внизу
  // было написано FLAC, а в списке 320 (замечено 02.08.2026). Берём ту же
  // функцию, которой подписан плеер, — разойтись они больше не могут.
  // ТОЛЬКО если у трека известен сервис: без него функция плеера всё равно
  // вернёт метку по глобальной настройке, и строка получит выдуманное «FLAC».
  // Первый же прогон так и показал FLAC у трека без сервиса — а честнее пусто.
  const svc = it.service || it._streamService || '';
  if (svc && typeof _qualityShortLabel === 'function') {
    const q = (typeof S !== 'undefined' && S.config && S.config['player-stream-quality'])
              || 'lossless';
    const lab = _qualityShortLabel(svc, q);
    if (lab) return lab;
  }
  // Запасной путь — только когда плеер ещё не загружен. Порог lossless высокий
  // намеренно: всё, что выше 700 kbps, mp3 быть не может.
  const raw = String(it.quality || it.fmt || it.format || it.codec || '').toLowerCase();
  const br = Number(it.bitrate || 0);
  const bits = Number(it.bits || it.bit_depth || 0);
  if (bits >= 24 || raw.includes('hires') || raw.includes('hi-res')) return 'Hi-Res';
  if (raw.includes('alac')) return 'ALAC';
  if (raw.includes('flac') || raw.includes('lossless') || br >= 700) return 'FLAC';
  if (raw.includes('aac')) return 'AAC';
  if (raw.includes('ogg') || raw.includes('vorbis')) return 'OGG';
  if (raw.includes('mp3') || (br >= 128 && br < 700)) return '320';
  return '';
}

function togglePlayerQueue() {
  const el = document.getElementById('pp-queue');
  if (el) { el.remove(); return; }
  _ppRenderQueue(true);
}

// Приписка в заголовке, пока микс ещё докачивается: «+5 едут». Без неё панель
// показывает три трека из восемнадцати и выглядит так, будто это весь микс.
//
// `_liveMix` объявлен через `let` на верхнем уровне player_lib.js — на `window`
// его НЕТ, поэтому обращаемся голым именем через typeof (тот же случай, что с
// DigsRadio: проверка через window молча не срабатывала). См. скилл
// ripster-frontend-file-drift.
function _pqLiveNote() {
  try {
    if (typeof _liveMix !== 'object' || !_liveMix || !_liveMix.timer) return '';
    const have = (typeof Preview !== 'undefined' && Preview.queue) ? Preview.queue.length : 0;
    const left = (_liveMix.total || 0) - have;
    if (left <= 0) return '';
    return `<span class="pq-live">${ti('pq.live_more', { n: left })}</span>`;
  } catch (e) { return ''; }
}

// ── Бегущая строка для того, что не влезло ───────────────────────────────────
// Оборачиваем текст во внутренний span САМИ, а не в разметке: тогда весь
// существующий код может продолжать писать `el.textContent = …` как писал, а
// нам достаточно позвать mqScan() после обновления. Иначе пришлось бы править
// каждое место, где меняется название, и одно из них обязательно забылось бы.
//
// Ширину меряем в пикселях и из неё считаем длительность, чтобы длинная строка
// не пролетала за то же время, что короткая: скорость постоянная (~40 px/с).
function mqScan(root) {
  const scope = root || document;
  let els;
  try { els = scope.querySelectorAll('[data-mq]'); } catch (e) { return; }
  els.forEach(el => {
    let inner = el.firstElementChild;
    if (!inner || !inner.classList.contains('mq-i')) {
      inner = document.createElement('span');
      inner.className = 'mq-i';
      while (el.firstChild) inner.appendChild(el.firstChild);
      el.appendChild(inner);
    }
    // clientWidth — видимая ширина, scrollWidth — сколько текст занял бы весь.
    const dx = inner.scrollWidth - el.clientWidth;
    if (dx > 4) {
      el.style.setProperty('--mq-dx', dx + 'px');
      el.style.setProperty('--mq-dur', Math.max(5, dx / 40 + 4).toFixed(1) + 's');
      el.classList.add('mq-on');
    } else {
      el.classList.remove('mq-on');
      el.style.removeProperty('--mq-dx');
      el.style.removeProperty('--mq-dur');
    }
  });
}

function _ppRenderQueue(create) {
  const q = (typeof Preview !== 'undefined' && Preview.queue) || [];
  let box = document.getElementById('pp-queue');
  if (!box) {
    if (!create) return;            // панель закрыта — молча ничего не делаем
    box = document.createElement('div');
    box.id = 'pp-queue';
    document.body.appendChild(box);
  }
  if (!q.length) {
    box.innerHTML = `<div class="pq-head"><span>${t('pq.title')}</span>
        <button class="pq-x" onclick="togglePlayerQueue()" aria-label="${t('pq.close')}">×</button></div>
      <div class="pq-empty">${t('pq.empty')}</div>`;
    return;
  }
  // Альбом трека: явное поле, иначе из подписи «Сервис · Альбом». Нужно, чтобы
  // рисовать границы — где один релиз кончается и начинается следующий (AIMP).
  const albumOf = it => {
    if (it.album) return String(it.album);
    const lbl = String(it.label || '');
    const m = lbl.split('·');
    return m.length > 1 ? m.slice(1).join('·').trim() : '';
  };
  let _prevAlbum = null, _albTrackNo = 0;
  const rows = q.map((it, i) => {
    const on = (typeof Preview !== 'undefined' && i === Preview.idx);
    // Заголовок альбома — когда альбом сменился. Даёт визуальную границу релиза.
    const alb = albumOf(it);
    let header = '';
    if (alb && alb !== _prevAlbum) {
      _prevAlbum = alb; _albTrackNo = 0;
      // Автор АЛЬБОМА, а не первого трека. Для компиляции/DJ-микса артист трека
      // (напр. David Duriez) — не автор сборника (Mount Kimbie). Берём albumArtist,
      // который несёт очередь; на него же откатываемся, только если его нет.
      const gArtist = it.albumArtist || it.artist || '';
      header = `<div class="pq-group"><span class="pq-group-name">${esc(alb)}</span>`
             + (gArtist ? `<span class="pq-group-artist">${esc(gArtist)}</span>` : '') + `</div>`;
    }
    _albTrackNo++;
    const title = esc(it.title || '');
    const artist = esc(it.artist || '');
    // Подпись источника — откуда трек взялся; у радио она объясняет ПОЧЕМУ.
    // Подпись показываем ТОЛЬКО когда она объясняет появление трека (радио).
    // У треков релиза в ней лежит «Сервис · Альбом» — это дубль подстрочника, и
    // на экране он съедал третью строку впустую (замечено по скриншоту 02.08).
    const why = String(it.label || '');
    // …и НЕ показываем, когда она повторяет заголовок альбома, который стоит
    // прямо над этими треками. У локального микса label = «Библиотека · <тот же
    // альбом>», и третья строка в каждой строке очереди дублировала шапку,
    // раздувая список впустую.
    const dupOfHeader = !!alb && why.includes(alb);
    const label = (why && !dupOfHeader && /·/.test(why)
                   && !/^(spotify|apple|deezer|qobuz|tidal|soundcloud)\s/i.test(why))
      ? `<span class="pq-why">${esc(why)}</span>` : '';
    const q = _pqQuality(it);
    // Номер внутри альбома (как в AIMP), если группируем; иначе позиция в очереди.
    const num = _prevAlbum ? _albTrackNo : (i + 1);
    return header + `<div class="pq-row${on ? ' on' : ''}" onclick="_pqJump(${i})"
        title="${title}${artist ? ' — ' + artist : ''}${q ? ' · ' + esc(q) : ''}">
        <span class="pq-n">${on ? '▶' : num}</span>
        <span class="pq-t">
          <span class="pq-title" data-mq>${title}</span>
          <span class="pq-sub" data-mq>${artist}${q ? `<b class="pq-q">·&nbsp;${esc(q)}</b>` : ''}</span>
          ${label}
        </span>
        <span class="pq-d">${_pqFmt(_pqDur(it, i))}</span>
        <button class="pq-rm" onclick="_pqRemove(${i},event)" title="${t('pq.remove')}" aria-label="${t('pq.remove')}">×</button>
      </div>${on ? _pqChapters() : ''}`;
  }).join('');
  box.innerHTML = `
    <div class="pq-head">
      <span>${ti('pq.title_n', { n: q.length })}${_pqLiveNote()}</span>
      <span style="display:flex;gap:6px;align-items:center">
        <button class="pq-clear" onclick="_pqClear(event)" title="${t('pq.clear')}">${t('pq.clear')}</button>
        <button class="pq-x" onclick="togglePlayerQueue()" aria-label="${t('pq.close')}">×</button>
      </span>
    </div>
    <div class="pq-list">${rows}</div>`;
  // Держим текущий трек на виду: очередь радио растёт сама, и без этого
  // играющее уезжает за пределы панели.
  const cur = box.querySelector('.pq-row.on');
  if (cur && cur.scrollIntoView) cur.scrollIntoView({ block: 'nearest' });
  // Длинные названия — посчитать, что не влезло, и запустить бегущую строку.
  try { mqScan(box); } catch (e) {}
}
function _pqJump(i) {
  if (typeof Preview === 'undefined' || !Preview.queue || !Preview.queue[i]) return;
  Preview.idx = i;
  if (typeof _playPreviewAt === 'function') _playPreviewAt(i);
  _ppRenderQueue();
}

// Убрать трек из очереди. Если убираем ИГРАЮЩИЙ — переходим на следующий (или
// закрываем плеер, если очередь опустела). Индекс правим, чтобы подсветка не
// съехала на соседний трек.
function _pqRemove(i, ev) {
  if (ev) ev.stopPropagation();
  if (typeof Preview === 'undefined' || !Preview.queue) return;
  if (i < 0 || i >= Preview.queue.length) return;
  const wasCurrent = (i === Preview.idx);
  Preview.queue.splice(i, 1);
  if (i < Preview.idx) Preview.idx--;
  if (!Preview.queue.length) {
    if (typeof _waStopIfPlaying === 'function') _waStopIfPlaying();
    if (typeof closePreview === 'function') closePreview();
    const box = document.getElementById('pp-queue'); if (box) box.remove();
    return;
  }
  if (wasCurrent) {
    if (Preview.idx >= Preview.queue.length) Preview.idx = Preview.queue.length - 1;
    if (typeof _playPreviewAt === 'function') _playPreviewAt(Preview.idx);
  }
  _ppRenderQueue();
}

// Очистить очередь = убрать всё, КРОМЕ играющего (обрывать музыку неожиданно
// нельзя). Радио тоже глушим, чтобы не набежало заново.
function _pqClear(ev) {
  if (ev) ev.stopPropagation();
  if (typeof Preview === 'undefined' || !Preview.queue) return;
  try { if (typeof DigsRadio === 'object' && DigsRadio) DigsRadio.on = false; } catch (_) {}
  const cur = Preview.queue[Preview.idx] || null;
  Preview.queue = cur ? [cur] : [];
  Preview.idx = cur ? 0 : -1;
  _ppRenderQueue();
  if (typeof toast === 'function') toast(t('pq.cleared'), 'var(--muted)', '', 1500);
}

// Обновляемся на смене трека — номер и подсветка должны совпадать с тем, что
// звучит, иначе список врёт.
document.addEventListener('ripster:track-start', () => _ppRenderQueue());

// «В любимые» прямо из плеера: артист играющего трека. Раньше для этого надо
// было идти в радар или поиск и искать его там же, кого сейчас слушаешь.
async function ppFollowCurrent() {
  const it = (typeof Preview !== 'undefined' && Preview.queue) ? Preview.queue[Preview.idx] : null;
  const name = it && it.artist;
  if (!name) { toast(t('pq.no_artist'), 'var(--muted)'); return; }
  if (typeof followArtist === 'function') await followArtist(name, document.getElementById('pp-fav-btn'));
  _ppSyncFav();
}

// Кнопка должна показывать состояние ТЕКУЩЕГО трека, иначе она врёт при
// переключении: сердце оставалось залитым от прошлого артиста.
function _ppSyncFav() {
  const b = document.getElementById('pp-fav-btn');
  if (!b) return;
  const it = (typeof Preview !== 'undefined' && Preview.queue) ? Preview.queue[Preview.idx] : null;
  const on = it && it.artist && typeof afIsFollowed === 'function' && afIsFollowed(it.artist);
  b.textContent = on ? '♥' : '♡';
  b.style.color = on ? 'var(--red)' : '';
  b.title = on ? t('af.following') : t('af.follow');
}
document.addEventListener('ripster:track-start', _ppSyncFav);

// ── Мягкий вход трека на пути <audio> ────────────────────────────────────────
//
// Движок gapless (Web Audio) СОЗНАТЕЛЬНО не применяется к SoundCloud и Deezer:
// он обязан скачать и раскодировать трек целиком до первого сэмпла, а у
// часового DJ-микса это минуты ожидания. Такие релизы играют через <audio>, где
// переход — резкий обрыв: новый трек начинается сразу на полной громкости.
// Владелец слышит это как «пинок» (02.08.2026).
//
// Полноценная сшивка требует второго <audio> с предзагрузкой — это отдельная
// работа. Но БÓЛЬШУЮ часть резкости даёт не отсутствие нахлёста, а именно
// мгновенный скачок громкости. Короткий подъём убирает её целиком и ничего не
// ломает: длится 420 мс, работает поверх любой громкости, отменяется при
// следующем переключении.
const _PQ_FADE_MS = 420;
let _pqFadeTimer = null;

function _pqFadeIn() {
  const a = _pqAudio();
  if (!a) return;
  // Уважаем системную настройку: кому анимации мешают, тому и звук не «ездит».
  try {
    if (window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches) return;
  } catch (e) {}
  clearInterval(_pqFadeTimer);
  const target = (typeof Preview !== 'undefined' && Preview._vol != null)
    ? Number(Preview._vol) : (a.volume || 1);
  if (!(target > 0)) return;
  const steps = 14, dt = _PQ_FADE_MS / steps;
  let k = 0;
  a.volume = 0;
  _pqFadeTimer = setInterval(() => {
    k++;
    // Кривая, а не прямая: на слух равномерный подъём громкости кажется рывком
    // в начале — ухо логарифмическое.
    a.volume = Math.min(target, target * Math.pow(k / steps, 2));
    if (k >= steps) { clearInterval(_pqFadeTimer); a.volume = target; }
  }, dt);
}
document.addEventListener('ripster:track-start', _pqFadeIn);

// ── Треки внутри микса ───────────────────────────────────────────────────────
//
// Микс — это ОДИН файл на час-два, и в очереди он одна строка. А слушают его как
// список: «что сейчас играет» внутри микса — вопрос не менее частый, чем «что
// в очереди». Метки уже собираются (описание BBC, MixesDB, тайм-коды YouTube) и
// лежат в `Preview._chapters`; здесь они просто показываются там, где человек их
// и ищет — под самим миксом, с временем и переходом по клику.
//
// Раскрываются ТОЛЬКО у играющего: у всех сразу это простыня, в которой не
// найти ничего.
function _pqChapters() {
  const ch = (typeof Preview !== 'undefined' && Preview._chapters) || [];
  if (!ch.length) return '';
  const cur = (typeof Preview !== 'undefined' ? Preview._curChap : -1);
  const rows = ch.map((c, i) => `
    <div class="pq-chap${i === cur ? ' on' : ''}" onclick="event.stopPropagation();previewSeekTo(${c.seconds})"
         title="${esc(c.label || '')}">
      <span class="pq-chap-t">${_pqFmt(c.seconds)}</span>
      <span class="pq-chap-l">${esc(c.label || '')}</span>
    </div>`).join('');
  return `<div class="pq-chaps">${rows}</div>`;
}

// ── Предзагрузка следующего трека ────────────────────────────────────────────
//
// Плеер заранее выясняет АДРЕС следующего трека (`_Pre.url`), но не качает его.
// Поэтому на стыке браузер начинал скачивание с нуля, и владелец слышал паузу.
//
// ПЕРВАЯ ПОПЫТКА БЫЛА ОШИБКОЙ и её надо помнить: я грел кэш скрытым вторым
// <audio preload="auto">. Даже приглушённый, это ВТОРОЙ ЗВУКОВОЙ ЭЛЕМЕНТ на
// странице — при ручной перемотке он оживал, и звук двоился (02.08.2026,
// «крашится звук, он как будто дублируется»). Никакой прогрев не стоит риска
// сломать воспроизведение.
//
// Сейчас греем ЗАПРОСОМ: обычный fetch первого куска файла. Браузер кладёт его
// в HTTP-кэш, и когда плеер откроет тот же адрес по-настоящему, данные уже
// рядом. Запрос не умеет издавать звук — сломать воспроизведение он не может
// физически.
//
// НЕ РАНЬШЕ ВРЕМЕНИ: за 25 секунд до конца. Хватает на буфер и не тратит
// трафик, если трек переключат раньше.
const _PQ_PRELOAD_AT = 25;
let _pqPreUrl = '';
let _pqPreAbort = null;

function _pqPreload() {
  try {
    if (typeof Preview === 'undefined' || typeof _Pre === 'undefined') return;
    const a = _pqAudio();
    if (!a || !isFinite(a.duration) || a.duration <= 0) return;
    if (a.duration - a.currentTime > _PQ_PRELOAD_AT) return;

    // Адрес готовит сам плеер — свою логику разрешения ссылок не дублируем,
    // иначе она разойдётся с настоящей.
    if (_Pre.idx !== Preview.idx + 1 || !_Pre.url) return;
    if (_pqPreUrl === _Pre.url) return;               // уже греем этот

    _pqDropPreload();
    _pqPreUrl = _Pre.url;
    _pqPreAbort = new AbortController();
    // Первый мегабайт: этого достаточно, чтобы старт был мгновенным, и мало,
    // чтобы не тянуть впустую весь трек, который могут не дослушать.
    fetch(_pqPreUrl, { headers: { Range: 'bytes=0-1048575' },
                       signal: _pqPreAbort.signal, cache: 'force-cache' })
      .then(r => r.arrayBuffer())
      .catch(() => {});                               // не вышло — просто без прогрева
  } catch (e) { /* прогрев не обязан работать и не может ничего сломать */ }
}

function _pqDropPreload() {
  try { if (_pqPreAbort) _pqPreAbort.abort(); } catch (e) {}
  _pqPreAbort = null;
  _pqPreUrl = '';
}

setInterval(_pqPreload, 1000);
document.addEventListener('ripster:track-start', _pqDropPreload);

// Док плеера обновляет название через textContent — своей перерисовки у него
// нет, поэтому пересчитываем переполнение по событию смены трека. Небольшая
// задержка: на момент события ширина ещё может меняться (обложка, кнопки).
document.addEventListener('ripster:track-start', () => setTimeout(() => {
  try { mqScan(document.getElementById('pp-bar') || document); } catch (e) {}
}, 120));
// Изменили размер окна — то, что влезало, могло перестать влезать.
let _mqRz = null;
window.addEventListener('resize', () => {
  clearTimeout(_mqRz);
  _mqRz = setTimeout(() => { try { mqScan(); } catch (e) {} }, 200);
});
