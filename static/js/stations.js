/* ══ СТАНЦИИ ══════════════════════════════════════════════════════════════════
 *
 * Вкладка «Станции»: жанровые плитки плюс то, что собрано из СОБСТВЕННОЙ
 * истории — кого слушал, что качал, какие жанры при этом звучали.
 *
 * Радар сюда не тянем: там своя тема (грядущее и подписки), и владелец
 * попросил оставить её отдельно.
 *
 * Правило показа одно и то же во всех секциях: **не показывать того, чего не
 * знаем**. Нет прослушиваний — секции нет, а не «рекомендуем популярное».
 * Жанр не лёг ни на одну плитку — он остаётся в списке фактом, но без ссылки:
 * увести человека на соседний жанр хуже, чем честно ничего не предложить.
 */

let _stHome = null;          // ответ /api/stations/home
let _stBusy = false;
let _stSeed = 0;             // растёт на «ещё раз» — тот же жанр, другой эфир

async function stationsInit(force) {
  if (_stBusy) return;
  if (_stHome && !force) { stRender(); return; }
  _stBusy = true;
  const box = document.getElementById('st-body');
  if (box) box.innerHTML = `<div class="st-note">${t('st.loading')}</div>`;
  try {
    _stHome = await api('GET', '/api/stations/home');
    stRender();
  } catch (e) {
    if (box) box.innerHTML = `<div class="st-note st-err">${esc(t('st.load_failed'))}: ${esc(String(e))}</div>`;
  } finally {
    _stBusy = false;
  }
}

function stRender() {
  const box = document.getElementById('st-body');
  if (!box || !_stHome) return;
  const d = _stHome;
  const parts = [];

  // ── Твои артисты ──────────────────────────────────────────────────────────
  // Первым, а не жанры: имя конкретнее жанра, и по нему станция точнее.
  if ((d.artists || []).length) {
    parts.push(stSection(
      t('st.your_artists'),
      t('st.your_artists_hint'),
      (d.artists || []).map(a => {
        // Показываем ОБА счётчика раздельно. Слушал и скачал — разные следы:
        // скачанное и не тронутое говорит о вкусе меньше, чем то, что играется.
        const bits = [];
        if (a.plays) bits.push(`♪ ${a.plays}`);
        if (a.downloads) bits.push(`↓ ${a.downloads}`);
        return `<button class="st-chip" onclick="stPlayArtist(${escJ2(a.name)})" title="${esc(t('st.play_artist'))}">
                  <span class="st-chip-name">${esc(a.name)}</span>
                  <span class="st-chip-num">${bits.join('  ')}</span>
                </button>`;
      }).join('')
    ));
  }

  // ── Твои жанры ────────────────────────────────────────────────────────────
  if ((d.genres || []).length) {
    const rows = (d.genres || []).map(g => g.station
      ? `<button class="st-chip" onclick="stPlay(${escJ2(g.station)})">
           <span class="st-chip-name">${esc(g.genre)}</span>
           <span class="st-chip-num">♪ ${g.plays}</span>
         </button>`
      // Без плитки — не кнопка. Кнопка, которая никуда не ведёт, это враньё.
      : `<span class="st-chip st-chip-dead" title="${esc(t('st.no_station_for'))}">
           <span class="st-chip-name">${esc(g.genre)}</span>
           <span class="st-chip-num">♪ ${g.plays}</span>
         </span>`).join('');
    const note = d.genres_unknown
      ? `<div class="st-note">${esc(t('st.genres_unknown').replace('{0}', d.genres_unknown))}</div>`
      : '';
    parts.push(stSection(t('st.your_genres'), t('st.your_genres_hint'), rows + note));
  }

  // ── Все жанровые станции ──────────────────────────────────────────────────
  parts.push(stSection(
    t('st.all_genres'), t('st.all_genres_hint'),
    (d.tiles || []).map(s =>
      `<button class="st-tile" onclick="stPlay(${escJ2(s.id)})">
         <span class="st-tile-name">${esc(s.title)}</span>
       </button>`).join('')
  ));

  // ── Что играло недавно ────────────────────────────────────────────────────
  if ((d.recent || []).length) {
    parts.push(stSection(
      t('st.recent'), t('st.recent_hint'),
      `<div class="st-list">` + (d.recent || []).slice(0, 12).map(r =>
        `<div class="st-row">
           <div class="st-row-main">
             <div class="st-row-t">${esc(r.title || '')}</div>
             <div class="st-row-a">${esc(r.artist || '')}${r.service ? ' · ' + esc(r.service) : ''}${r.times > 1 ? ' · ♪ ' + r.times : ''}</div>
           </div>
           <button class="st-mini" onclick="stPlayArtist(${escJ2(r.artist || '')})">${esc(t('st.station_of'))}</button>
         </div>`).join('') + `</div>`
    ));
  }

  // ── Откуда качалось ───────────────────────────────────────────────────────
  if ((d.services || []).length) {
    parts.push(stSection(
      t('st.services'), '',
      `<div class="st-svcs">` + (d.services || []).map(([name, n]) =>
        `<span class="st-svc">${esc(name)} <b>${n}</b></span>`).join('') + `</div>`
    ));
  }

  const c = d.counts || {};
  parts.push(`<div class="st-note st-foot">${esc(
    t('st.footer').replace('{0}', c.plays || 0).replace('{1}', c.downloads || 0))}</div>`);

  box.innerHTML = parts.join('');
}

function stSection(title, hint, inner) {
  return `<div class="st-sec">
    <div class="st-sec-h">
      <span class="st-sec-t">${esc(title)}</span>
      ${hint ? `<span class="st-sec-hint">${esc(hint)}</span>` : ''}
    </div>
    <div class="st-sec-b">${inner}</div>
  </div>`;
}

/** Кавычки для onclick — отдельно от escJ, чтобы не зависеть от чужого файла. */
function escJ2(s) {
  return "'" + String(s == null ? '' : s).replace(/\\/g, '\\\\').replace(/'/g, "\\'") + "'";
}

async function stPlay(id) {
  _stSeed = (_stSeed + 1) | 0;
  await stRun(`/api/station?id=${encodeURIComponent(id)}&limit=30&seed=${Date.now() & 0xffff}`,
              id);
}

async function stPlayArtist(name) {
  if (!name) return;
  await stRun(`/api/station/artist?name=${encodeURIComponent(name)}&limit=25&seed=${Date.now() & 0xffff}`,
              name);
}

/**
 * Собрать эфир и поставить его в плеер.
 *
 * Отдельная функция на оба случая, потому что различаются они только адресом,
 * а вот отчёт человеку обязан быть одинаковым: и пустой ответ, и отказ должны
 * ЧТО-ТО сказать. Молчащая кнопка — тот самый врущий контрол.
 */
async function stRun(url, label) {
  const bar = document.getElementById('st-status');
  if (bar) bar.textContent = t('st.building').replace('{0}', label);
  try {
    const r = await api('GET', url);
    if (!r || !r.ok || !(r.tracks || []).length) {
      if (bar) bar.textContent = (r && r.reason) ? r.reason : t('st.empty');
      return;
    }
    stQueue(r.tracks, label);
    const from = r.from_artists ? ` · ${t('st.from_artists').replace('{0}', r.from_artists)}` : '';
    if (bar) bar.textContent = `${t('st.playing').replace('{0}', r.title || label)} · ${r.tracks.length}${from}`;
  } catch (e) {
    if (bar) bar.textContent = `${t('st.load_failed')}: ${String(e)}`;
  }
}

/**
 * Отдать собранное существующему плееру.
 *
 * Играем ТОЛЬКО то, у чего есть сервис и идентификатор: без них стрим-адрес не
 * собрать, и такая строка в очереди была бы кнопкой без действия.
 */
function stQueue(tracks, label) {
  const playable = tracks.filter(x => x.service && x.id &&
                                 ['qobuz', 'tidal', 'deezer'].includes(x.service));
  if (!playable.length) {
    const bar = document.getElementById('st-status');
    if (bar) bar.textContent = t('st.nothing_playable');
    return;
  }
  const first = playable[0];
  playStreamTrack(first.service, first.id, first.title || '', first.artist || '', first.cover || '');
  // Остальное — в очередь предпросмотра, если она есть в этой сборке.
  try {
    if (window.Preview && Array.isArray(Preview.queue)) {
      Preview.queue = playable.map(x => ({
        url: '', service: x.service, id: x.id,
        title: x.title || '', artist: x.artist || '', cover: x.cover || '',
      }));
      Preview.idx = 0;
    }
  } catch (_) {}
}
