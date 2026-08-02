// ======================================================================
// «Раскопки» — радио: очередь, которая дописывается сама.
//
// Без этого копатель остаётся списком: нашёл, забрал, закрыл. Включил трек — и
// тишина. Смысл в том, чтобы копание продолжалось само: под играющий трек
// подкладываются следующие по вкусу, и очередь не кончается.
//
// ВСТРАИВАНИЕ, А НЕ ПЕРЕПИСЫВАНИЕ. У плеера уже есть очередь (Preview.queue) и
// единая точка перехода (previewNext). Радио оборачивает её, а не подменяет
// логику воспроизведения: так оно не ломает ни альбомную очередь, ни BBC, ни
// DRM-путь SoundCloud, у которых свои правила перехода.
//
// Докладываем ЗАРАНЕЕ. Ждать конца последнего трека нельзя — между ним и новым
// куском будет тишина на время запроса. Поэтому добор начинается, когда до конца
// очереди остаётся два трека.
// ======================================================================

const DigsRadio = {
  on: false,
  seed: '',            // от кого пляшем сейчас
  played: [],          // кого уже играли — чтобы радио не ходило по кругу
  loading: false,
};

const _DR_PREFETCH_AT = 2;   // осталось столько треков — пора добирать
const _DR_BATCH = 6;

function digsRadioOn(seedArtist) {
  DigsRadio.on = true;
  DigsRadio.seed = seedArtist || DigsRadio.seed;
  if (seedArtist) DigsRadio.played = [seedArtist];
  _digsRadioBadge();
  toast(ti('digs.radio_on', { name: DigsRadio.seed }), 'var(--green)', '', 4000);
  _digsRadioTopUp();
}

function digsRadioOff() {
  DigsRadio.on = false;
  _digsRadioBadge();
  toast(t('digs.radio_off'), 'var(--muted)');
}

function digsRadioToggle() {
  if (DigsRadio.on) { digsRadioOff(); return; }
  // Заводим от того, что играет прямо сейчас: радио без затравки бессмысленно.
  const cur = (typeof Preview !== 'undefined' && Preview.queue && Preview.idx >= 0)
    ? Preview.queue[Preview.idx] : null;
  if (!cur || !cur.artist) { toast(t('digs.radio_need_track'), 'var(--orange)'); return; }
  digsRadioOn(cur.artist);
}

function _digsRadioBadge() {
  const b = document.getElementById('fp-radio-btn');
  if (b) b.classList.toggle('active', !!DigsRadio.on);
}

// Добрать треков в конец очереди. Ничего не играет и не перематывает — только
// дописывает, поэтому вызывать безопасно в любой момент.
async function _digsRadioTopUp() {
  if (!DigsRadio.on || DigsRadio.loading) return;
  if (typeof Preview === 'undefined' || !Preview.queue) return;
  const left = Preview.queue.length - 1 - Preview.idx;
  if (left > _DR_PREFETCH_AT) return;

  DigsRadio.loading = true;
  try {
    // Затравка — последний прозвучавший артист, а не самый первый: иначе радио
    // всё время возвращается к началу и не уходит вглубь.
    const cur = Preview.queue[Preview.idx];
    const seed = (cur && cur.artist) || DigsRadio.seed;
    const ex = encodeURIComponent(DigsRadio.played.slice(-40).join('|'));
    const r = await api('GET', `/api/digs/radio?seed=${encodeURIComponent(seed)}`
                              + `&exclude=${ex}&limit=${_DR_BATCH}`);
    const items = (r && r.items) || [];
    if (!items.length) {
      // Честно выключаемся, а не молчим: пусть человек видит, что радио кончилось.
      DigsRadio.on = false;
      _digsRadioBadge();
      toast(t('digs.radio_dry'), 'var(--orange)', '', 6000);
      return;
    }
    for (const it of items) {
      const why = it.why_key ? ti(it.why_key, it.why_args || {}) : (it.why || '');
      it.label = `${t('digs.radio_label')} · ${why}`.trim();
      Preview.queue.push(it);
      DigsRadio.played.push(it.artist);
    }
    DigsRadio.seed = items[items.length - 1].artist;
    if (typeof _ppRenderQueue === 'function') _ppRenderQueue();
    toast(ti('digs.radio_added', { n: items.length }), 'var(--muted)', '', 3000);
  } catch (e) {
    // Сеть моргнула — не выключаем радио, попробуем на следующем треке.
  } finally {
    DigsRadio.loading = false;
  }
}

// Оборачиваем переход. Две задачи: заранее добрать, и не дать очереди кончиться,
// если добор ещё в пути.
(function _digsRadioHook() {
  if (typeof window.previewNext !== 'function') return;
  const orig = window.previewNext;
  window.previewNext = function () {
    const res = orig.apply(this, arguments);
    if (DigsRadio.on) _digsRadioTopUp();
    return res;
  };
  // Плюс добор при каждом запуске трека — на случай, если переход случился не
  // через previewNext (обрыв потока, пропуск DRM-трека).
  document.addEventListener('ripster:track-start', () => {
    if (DigsRadio.on) _digsRadioTopUp();
  });
})();

// ── Очередь не должна кончаться сама собой ───────────────────────────────────
//
// Замысел был такой: включил один трек — дальше подкладывается само. На деле
// радио надо было включить вручную, и человек, запустивший одиночный трек,
// получал трек-лист из одной строки и тишину после него (02.08.2026: «трек-лист
// 1 почему-то написано»).
//
// Теперь радио заводится САМО, когда очередь вот-вот кончится: играет трек,
// впереди пусто — берём его за затравку и продолжаем. Ровно один раз на
// иссякание, поэтому лишних запросов нет.
//
// Отключается `radio-autostart: false` — кому нужна ровно та очередь, что он
// собрал, тот её и получит.
document.addEventListener('ripster:track-start', () => {
  if (DigsRadio.on) return;
  try {
    const off = (typeof S !== 'undefined' && S.config
                 && S.config['radio-autostart'] === false);
    if (off) return;
    if (typeof Preview === 'undefined' || !Preview.queue) return;
    const left = Preview.queue.length - 1 - Preview.idx;
    if (left > 0) return;                       // впереди ещё есть что играть
    const cur = Preview.queue[Preview.idx];
    if (!cur || !cur.artist) return;            // без затравки продолжать нечем
    digsRadioOn(cur.artist);
  } catch (e) { /* не смогли — просто не продолжаем, это не повод падать */ }
});
