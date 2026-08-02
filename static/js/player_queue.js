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
      header = `<div class="pq-group"><span class="pq-group-name">${esc(alb)}</span>`
             + (it.artist ? `<span class="pq-group-artist">${esc(it.artist)}</span>` : '') + `</div>`;
    }
    _albTrackNo++;
    const title = esc(it.title || '');
    const artist = esc(it.artist || '');
    // Подпись источника — откуда трек взялся; у радио она объясняет ПОЧЕМУ.
    // Подпись показываем ТОЛЬКО когда она объясняет появление трека (радио).
    // У треков релиза в ней лежит «Сервис · Альбом» — это дубль подстрочника, и
    // на экране он съедал третью строку впустую (замечено по скриншоту 02.08).
    const why = String(it.label || '');
    const label = (why && /·/.test(why) && !/^(spotify|apple|deezer|qobuz|tidal|soundcloud)\s/i.test(why))
      ? `<span class="pq-why">${esc(why)}</span>` : '';
    const q = _pqQuality(it);
    // Номер внутри альбома (как в AIMP), если группируем; иначе позиция в очереди.
    const num = _prevAlbum ? _albTrackNo : (i + 1);
    return header + `<div class="pq-row${on ? ' on' : ''}" onclick="_pqJump(${i})"
        title="${title}${artist ? ' — ' + artist : ''}${q ? ' · ' + esc(q) : ''}">
        <span class="pq-n">${on ? '▶' : num}</span>
        <span class="pq-t">
          <span class="pq-title">${title}</span>
          <span class="pq-sub">${artist}${q ? `<b class="pq-q">·&nbsp;${esc(q)}</b>` : ''}</span>
          ${label}
        </span>
        <span class="pq-d">${_pqFmt(_pqDur(it, i))}</span>
      </div>${on ? _pqChapters() : ''}`;
  }).join('');
  box.innerHTML = `
    <div class="pq-head">
      <span>${ti('pq.title_n', { n: q.length })}</span>
      <button class="pq-x" onclick="togglePlayerQueue()" aria-label="${t('pq.close')}">×</button>
    </div>
    <div class="pq-list">${rows}</div>`;
  // Держим текущий трек на виду: очередь радио растёт сама, и без этого
  // играющее уезжает за пределы панели.
  const cur = box.querySelector('.pq-row.on');
  if (cur && cur.scrollIntoView) cur.scrollIntoView({ block: 'nearest' });
}

function _pqJump(i) {
  if (typeof Preview === 'undefined' || !Preview.queue || !Preview.queue[i]) return;
  Preview.idx = i;
  if (typeof _playPreviewAt === 'function') _playPreviewAt(i);
  _ppRenderQueue();
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
