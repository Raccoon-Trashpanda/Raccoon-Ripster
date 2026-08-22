// ======================================================================
// «Раскопки» (Digs) — вкладка.
//
// Порядок на экране не косметический: СНАЧАЛА видно, на чём построен подбор,
// и только потом сам подбор. Рекомендация без видимого происхождения читается
// как реклама.
//
// Пласты жанров — это диаграмма долей, а не украшение, поэтому она сделана по
// правилам визуализации, а не на глаз:
//   • задача данных — величина (доля жанра), значит ОДИН тон, светлее→темнее
//     (порядковая шкала), а не радуга из категориальных цветов;
//   • тёмная и светлая темы имеют СВОИ шкалы: автоматический переворот тёмной
//     в светлую не работает — бледный конец перестаёт читаться на белом
//     (проверено: #f3d08a даёт 1.44:1 при пороге 2:1);
//   • обе шкалы прогнаны валидатором палитры (монотонность, зазор между
//     ступенями, контраст к поверхности, единство тона) — не «подобраны глазом»;
//   • подписи носят текстовые цвета, а не цвет своей полосы;
//   • у каждой полосы прямая подпись, поэтому легенда не нужна.
// ======================================================================

let _digsData = null;

// Вьюха и её скрипт версионируются РАЗДЕЛЬНО, поэтому у человека может оказаться
// новый HTML со старым JS (или наоборот) — и тогда любой getElementById по
// переименованному узлу возвращает null, а присваивание innerHTML роняет всю
// отрисовку целиком. Ровно это и случилось при перестройке вкладки 01.08.2026:
// «Cannot set properties of null». Пишем через заглушку: несовпадение версий
// теперь означает «часть блоков не отрисовалась», а не «вкладка мертва».
const _DG_SINK = { style: {}, innerHTML: '', textContent: '', firstChild: { nodeValue: '' } };
function _dg(id) { return document.getElementById(id) || _DG_SINK; }


// Порядковая янтарная шкала. Янтарь — не вкусовщина: вкладка про раскопки,
// и слои породы читаются этим тоном естественно. Значения ниже — те, что
// прошли проверку; менять их на глаз нельзя, только заново через валидатор.
const _DIGS_RAMP_DARK  = ['#f3d08a', '#e8b45c', '#d9962e', '#c07a1c', '#9c6015', '#75470f'];
const _DIGS_RAMP_LIGHT = ['#e0a746', '#c9862a', '#ab6b1b', '#8c5314', '#6d3f0f', '#4e2c0a'];

// Здесь лежал справочник разделов `_DIGS_SECTIONS`, на который никто не
// ссылался: он перечислял пять видов находок, а вкладка строила свой список
// прямо в digsRender — и именно поэтому «В этот день» годами считалось и не
// рисовалось. Мёртвый справочник не безобиден: он выглядит как источник правды
// и прячет то, что настоящий источник другой.

// Готовые направления для первого запуска. Список — не «жанры вообще», а то,
// что вообще имеет смысл искать этой качалкой; человек отмечает своё.
const _DIGS_GENRE_PICKS = [
  'deep house', 'progressive house', 'melodic techno', 'techno', 'trance',
  'balearic', 'chillout', 'ambient', 'downtempo', 'liquid funk',
  'drum & bass', 'jungle', 'breakbeat', 'synthwave', 'indie', 'idm',
  'dub techno', 'lo-fi', 'jazz', 'classical',
];

function _digsRampStep(i, total) {
  const dark = !document.documentElement.matches('[data-theme="light"]');
  const ramp = dark ? _DIGS_RAMP_DARK : _DIGS_RAMP_LIGHT;
  // Самая крупная доля — самый насыщенный конец шкалы.
  const idx = Math.min(ramp.length - 1, Math.round((i / Math.max(1, total - 1)) * (ramp.length - 1)));
  return ramp[idx];
}

// Пласт породы. Находки этого жанра лежат ВНУТРИ него — от этого метафора
// становится структурной, а не наклеенной: прокрутка вниз и есть углубление.
function _digsSeam(g, i, total, items) {
  const color = _digsRampStep(i, total);
  const w = Math.max(3, Math.min(100, g.share));
  const key = 'seam:' + g.genre;
  // Кликабельна ТОЛЬКО шапка, и это не мелочь реализации: карточки находок лежат
  // ниже, отдельным узлом, у каждой свои кнопки («Забрать», «Похожие», «Скрыть»).
  // Повесь обработчик на весь пласт — и любой промах по кнопке молча сузил бы
  // страницу вместо ожидаемого действия. Шапка — сосед сетки, а не её предок,
  // поэтому всплытие из карточек сюда не доходит физически, а не «отменяется».
  const on = _dgGenre === g.genre;
  const pick = `digsSeamPick(decodeURIComponent('${encodeURIComponent(g.genre)}'))`;
  // Подпись действия — СЛОВОМ и всегда на виду. Курсор и подсветка при наведении
  // сообщают «сюда можно нажать» только тому, кто уже навёл; на вкладке, где
  // шапка годами была картинкой, наводить причины нет.
  return `<div class="dg-seam${on ? ' on' : ''}" data-dg-block="${esc(key)}" style="background:linear-gradient(90deg,${color}0d,transparent 62%)">
    <div class="dg-seam-bar" style="width:${w}%;background:${color}"
         role="img" aria-label="${esc(g.genre)}: ${g.share}%"></div>
    <div class="dg-seam-head" role="button" tabindex="0" aria-pressed="${on ? 'true' : 'false'}"
         title="${esc(on ? t('digs.f_pick_off') : t('digs.f_pick'))}"
         onclick="${pick}"
         onkeydown="if(event.key===\'Enter\'||event.key===\' \'){event.preventDefault();${pick}}">
      <span class="dg-seam-name">${esc(g.genre)}</span>
      <span class="dg-seam-share">${g.share}%</span>
      <span class="dg-seam-pick">${on ? t('digs.f_pick_lbl_off') : t('digs.f_pick_lbl')}</span>
      <span class="dg-seam-cnt" data-dg-cnt="${esc(key)}">${_dgCountText(key, items.length)}</span>
      <span class="dg-seam-who" title="${esc((g.artists || []).join(' · '))}">${esc((g.artists || []).slice(0, 3).join(' · '))}</span>
    </div>
    ${_dgGrid(items, key, 2)}
  </div>`;
}

// Пласты БЕЗ находок. Раньше каждый рисовался таким же блоком во всю ширину:
// пять слоёв подряд — полоска, название, одно имя справа — и ни одной находки,
// то есть четверть экрана, на которой нечего делать. Доля жанра при этом факт
// полезный, поэтому она никуда не девается — сворачивается в одну строку.
function _digsQuietSeams(genres, total) {
  if (!genres.length) return '';
  const row = genres.map(g => {
    const i = total.indexOf(g);
    const color = _digsRampStep(i < 0 ? total.length - 1 : i, total.length);
    return `<span class="dg-quiet" title="${esc((g.artists || []).join(' · '))}">
      <i style="background:${color}"></i>${esc(g.genre)}<b>${g.share}%</b></span>`;
  }).join('');
  return `<div class="dg-vein"><h3>${t('digs.quiet')}</h3>
      <span>${t('digs.quiet_hint')}</span></div>
    <div class="dg-quiet-row">${row}</div>`;
}

// Разложить находки по пластам: у каждой находки есть артист, у артиста — жанр.
// Что не легло ни в один пласт (жанр неизвестен) — уходит в общий список ниже,
// а не выбрасывается: «не знаю жанр» не повод прятать находку.
// Карта «имя артиста → жанр». Вынесена из _digsByGenre, потому что тот же
// справочник нужен фильтру по пласту: два независимых обхода профиля разошлись
// бы при первом же расхождении в регистре, и пласт показывал бы одно, а фильтр
// по нему — другое.
function _dgGenreMap(profile) {
  const g = {};
  ((profile || {}).artists || []).forEach(a => {
    if (a && a.name && a.genre) g[a.name.toLowerCase()] = a.genre;
  });
  return g;
}

function _digsByGenre(profile, items) {
  const g = _dgGenreMap(profile);
  const out = {}, rest = [];
  items.forEach(it => {
    const gen = g[(it.artist || '').toLowerCase()];
    if (gen) (out[gen] = out[gen] || []).push(it); else rest.push(it);
  });
  return { out, rest };
}

function _digsChip(text, sub, cls, onx) {
  // Имя лежит атрибутом: так чип опоры можно убрать точечно (оптимистичный
  // отклик «Не предлагать»), не перерисовывая весь список опор заново.
  return `<span class="dg-chip ${cls || ''}" data-artist="${esc(text)}">${esc(text)}`
    + (sub ? `<span style="color:var(--muted);font-size:10px">${esc(sub)}</span>` : '')
    + (onx ? `<button onclick="${onx}" title="${t('digs.not_mine')}">×</button>` : '')
    + `</span>`;
}

// Находка — предмет, откопанный в слое: круглая обложка, имя, ПРИЧИНА. Причина
// обязательна: подбор без видимого происхождения читается как реклама.
//
// Артист и название стоят РАЗНЫМИ строками, и название переносится, а не режется.
// Одной строкой «Артист — Название» шесть находок одного артиста выглядели так:
// «Lane 8 — JCM 20…», «Lane 8 — Dance …», «Lane 8 — Group …» — отличить нельзя
// ни одну, а в музыке по обрезку не понять, ремикс это или оригинал (скилл
// ripster-long-text).
function _digsItem(it) {
  const title = it.title ? `${it.artist} — ${it.title}` : it.artist;
  // Заглушка — ИНИЦИАЛ, а не нота: тридцать одинаковых «♪» подряд читались как
  // шум, а инициал хотя бы различает соседние карточки.
  const cover = it.cover
    ? `<img class="dg-cover" src="${esc(it.cover)}" loading="lazy" decoding="async" alt=""
         onerror="this.classList.add('dg-cover-off')">`
    : `<div class="dg-blank">${esc((it.artist || '?').trim()[0] || '?')}</div>`;
  // Причина строится как «что ты делал, ЗАПЯТАЯ, чего не хватает». Вторая
  // половина и есть ответ на «почему мне это показывают» — её и подсвечиваем.
  // Причина приходит КЛЮЧОМ и числами — иначе английский пользователь получал
  // бы русскую фразу на каждой карточке, и перевод интерфейса её бы не достал.
  const why = it.reason_key ? ti(it.reason_key, it.reason_args || {}) : String(it.reason || '');
  const cut = why.indexOf(', ');
  const whyHtml = cut > 0
    ? esc(why.slice(0, cut + 2)) + '<b>' + esc(why.slice(cut + 2)) + '</b>'
    : esc(why);
  const enc = encodeURIComponent(it.artist || '');
  // Действия названы словами: иконка лупы не читалась совсем. Но ТРИ одинаковых
  // кнопки-пилюли на каждой из тридцати карточек весили больше самих находок —
  // шестьдесят четыре рамки спорили за внимание с тем, ради чего вкладка нужна.
  // Прятать их нельзя (проверено: невидимого элемента управления для человека не
  // существует), поэтому разведены по ВЕСУ: главное действие — кнопка, два
  // вспомогательных — тихие текстовые, всегда на виду.
  const primary = it.url
    ? `<button class="dg-act primary" onclick="event.stopPropagation();digsQueue(decodeURIComponent('${encodeURIComponent(it.url)}'))">${t('digs.a_get')}</button>`
    : `<button class="dg-act" onclick="event.stopPropagation();digsSearch(decodeURIComponent('${enc}'))">${t('digs.a_find')}</button>`;
  const links =
      `<button class="dg-link" onclick="event.stopPropagation();digsBubbles(decodeURIComponent('${enc}'))">${t('digs.a_similar')}</button>`
    + `<button class="dg-link" onclick="event.stopPropagation();digsExclude(decodeURIComponent('${enc}'))">${t('digs.a_hide')}</button>`;
  const meta = ` data-artist="${esc(it.artist || '')}" data-title="${esc(it.title || '')}"`
    + ` data-url="${esc(it.url || '')}" data-svc="${esc(it.service || '')}"`
    + ` data-cover="${esc(it.cover || '')}"`;
  return `<div class="dg-item" tabindex="0"${meta} title="${esc(title)}">${cover}
    <div class="dg-body">
      <div class="dg-art">${esc(it.artist || '')}</div>
      ${it.title ? `<div class="dg-ttl">${esc(it.title)}</div>` : ''}
      <div class="dg-why">${whyHtml}</div>
      <div class="dg-links">${links}</div>
    </div>
    <div class="dg-acts">${primary}</div></div>`;
}

// Один артист не должен заливать собой весь пласт. Шесть находок Lane 8 подряд —
// это не раскопки, а «ещё Lane 8»: смысл вкладки в РАЗНООБРАЗИИ того, что можно
// забрать. Лишнее не выбрасываем — прячем под разворот, потому что «пропустил
// шесть релизов своего артиста» само по себе ценный факт.
function _dgDiverse(items, perArtist) {
  const seen = {}, keep = [], more = [];
  items.forEach(it => {
    const a = (it.artist || '').toLowerCase();
    seen[a] = (seen[a] || 0) + 1;
    (seen[a] <= perArtist ? keep : more).push(it);
  });
  return { keep, more };
}

// Какие пласты и жилы развёрнуты целиком. Состояние держим здесь, а не в разметке:
// разметка сеток переписывается, а разворот обязан пережить ЛЮБОЕ обновление —
// и частичное, и полное. Модульная переменная это и обеспечивает.
let _dgOpen = new Set();

// Реестр сеток: ключ → находки, из которых сетка нарисована. Без него частичная
// перерисовка невозможна: digsToggle получает только ключ, а items остались
// внутри digsRender и наружу никогда не выходили.
const _dgGrids = new Map();

// Хозяин сетки по ключу. Ключи содержат имена жанров — с пробелами, юникодом,
// кавычками и апострофами, — поэтому ищем СРАВНЕНИЕМ dataset, а не подстановкой
// в CSS-селектор: экранирование селектора здесь было бы ошибкой, ждущей своего
// жанра (ровно так уже рвались onclick с апострофами, см. digsRender).
function _dgFind(attr, prop, key) {
  const list = document.querySelectorAll('#view-digs [' + attr + ']');
  for (let i = 0; i < list.length; i++) if (list[i].dataset[prop] === key) return list[i];
  return null;
}
function _dgGridHost(key) { return _dgFind('data-dg-key', 'dgKey', key); }
function _dgCntHost(key)  { return _dgFind('data-dg-cnt', 'dgCnt', key); }

// Число находок в шапке. Пласт подписывает его словом («12 находок»), жила —
// голой цифрой; развилка по префиксу ключа — другого признака у ключа нет.
function _dgCountText(key, n) {
  return key.indexOf('seam:') === 0 ? ti('digs.seam_n', { n: n }) : String(n);
}

// Разворот — самое частое действие на вкладке, и раньше он стоил перерисовки
// ВСЕЙ страницы: шапка, пласты, чипы опор, подвал. Теперь переписывается ровно
// одна сетка и её кнопка «Ещё N»; всё остальное DOM не трогается.
function digsToggle(key) {
  if (_dgOpen.has(key)) _dgOpen.delete(key); else _dgOpen.add(key);
  const host = _dgGridHost(key);
  // Полная отрисовка остаётся запасным путём: при рассогласовании разметки и
  // скрипта (разные ?v=) контейнера может не оказаться, и молча неработающий
  // разворот хуже лишней перерисовки.
  if (host) host.innerHTML = _dgGridInner(key);
  else digsRender();
}

// Ряд находок с ограничением по одному артисту и разворотом остатка.
function _dgGridInner(key) {
  const rec = _dgGrids.get(key);
  if (!rec) return '';
  const { keep, more } = _dgDiverse(rec.items, rec.per);
  const open = _dgOpen.has(key);
  const shown = open ? keep.concat(more) : keep;
  return `<div class="dg-grid">${shown.map(_digsItem).join('')}</div>`
    + (more.length
        ? `<button class="dg-more" onclick="digsToggle(decodeURIComponent('${encodeURIComponent(key)}'))">`
          + (open ? t('digs.less') : ti('digs.more', { n: more.length })) + `</button>`
        : '');
}

// Обёртка нужна как АДРЕС: сетка и её кнопка «Ещё N» — два соседних узла, и
// заменить их парой без общего родителя нельзя.
function _dgGrid(items, key, perArtist) {
  _dgGrids.set(key, { items: items, per: perArtist || 2 });
  return `<div class="dg-gridwrap" data-dg-key="${esc(key)}">${_dgGridInner(key)}</div>`;
}

// Реконсиляция: состояние экрана выводится ИЗ ДАННЫХ, а не восстанавливается
// хирургией по узлам. Благодаря этому удаление и откат — одна и та же операция,
// отличающаяся только тем, какие данные ей дали.
//
// Опустевший пласт нельзя оставить просто без карточек: получится заголовок,
// полоска доли, счётчик и пустота под ними — ровно тот дефект, против которого
// заведён _digsQuietSeams, только теперь возникающий в ответ на нажатие, то есть
// читающийся как поломка. Жанр обязан уехать в строку «остальные пласты»: его
// доля во вкусе остаётся фактом и без находок.
//
// Точечно такое не собрать — меняется состав live и содержимое строки «остальных»,
// — поэтому структурный случай пересобирает ТЕЛО (пласты и жилы) из данных. Шапка,
// чипы опор, шоу и подвал не трогаются и здесь.
function _dgReconcile(keys) {
  const dta = _digsData;
  if (!dta) return;
  const structural = keys.some(k => {
    const rec = _dgGrids.get(k);
    return !rec || !rec.items.length || !_dgGridHost(k);
  });
  if (structural) { _digsRenderBody(dta); return; }
  keys.forEach(k => {
    const rec = _dgGrids.get(k);
    _dgGridHost(k).innerHTML = _dgGridInner(k);   // сетка вместе с кнопкой «Ещё N»
    // Счётчик в шапке нарисован при полной отрисовке и без этого показывал бы
    // прежнее число — врал бы ровно про то, что человек только что изменил.
    const cnt = _dgCntHost(k);
    if (cnt) cnt.textContent = _dgCountText(k, rec.items.length);
  });
}

// ── Ручка «глубина копа» ──────────────────────────────────────────────────
// Наш ответ чужим «настроениям» по оси, которой у них нет: не «какая музыка
// нравится», а «насколько далеко от уже знакомого копать». Никакой сервис так
// не умеет — он не знает, что у человека уже на диске.
//
// Три положения, а не ползунок: промежуточные деления здесь нечем подписать, а
// неподписанное деление шкалы означает «крути и гадай».
//
// Положения НАКОПИТЕЛЬНЫЕ — это шкала глубины, а не три независимых фильтра:
// «Рядом» = ядро + рядом, «Далеко» = всё. Умолчание — «Далеко», то есть ровно
// сегодняшнее поведение: ручка умеет только СУЖАТЬ. Молча менять то, что
// человек видит при первом заходе, элемент управления не вправе.
//
// Состояние — модульная переменная, а не localStorage (скилл
// ripster-offline-and-state): выбор живёт ровно столько, сколько сессия.
const _DG_DEPTHS = ['core', 'near', 'far'];
let _dgDepth = 'far';

// Слой артиста выводим из его следов в фонотеке, а не из «похожести»:
//   качал                → ядро   (копаем там, где уже копали);
//   слышал или слежу     → рядом  (слышал, но не забрал);
//   ни того, ни другого  → далеко (пришёл по связям: похожие, гости шоу).
// Порядок проверок важен: у скачанного артиста бывают и прослушивания, и
// слежка — «качал» перекрывает всё остальное.
function _dgTier(a) {
  if (!a) return 'far';
  if ((a.downloads || 0) > 0) return 'core';
  if ((a.plays || 0) > 0 || a.watched) return 'near';
  return 'far';
}

// Карта «имя артиста → слой». Строится заново на каждый просев, а не кэшируется:
// профиль меняется под ногами (digsExclude убирает артиста оптимистично), и
// пережившая своего хозяина карта врала бы молча.
function _dgTierMap(d) {
  const m = new Map();
  (((d || {}).profile || {}).artists || []).forEach(a => {
    if (a && a.name) m.set(a.name.toLowerCase(), _dgTier(a));
  });
  return m;
}

// Артиста, которого нет в профиле вовсе, относим к «далеко»: он и попал в
// находки только по связям — будь у него загрузки или прослушивания, он был бы
// в профиле.
function _dgDepthKeep(items, map) {
  if (_dgDepth === 'far') return items;
  const ok = _dgDepth === 'core' ? { core: 1 } : { core: 1, near: 1 };
  return (items || []).filter(it => ok[map.get((it.artist || '').toLowerCase()) || 'far']);
}

// ПРЕДСТАВЛЕНИЕ данных под выбранную глубину. Исходные digs не трогаем ни в
// каком виде: фильтр — это то, что ВИДНО, а не то, что есть. Иначе возврат на
// «Далеко» было бы неоткуда взять, а откат digsExclude писался бы в уже урезанный
// массив и терял находки безвозвратно.
//
// На «Далеко» возвращается тот же объект — умолчание не стоит ни одного прохода.
function _dgDepthView(d) {
  if (_dgDepth === 'far' || !d) return d;
  const map = _dgTierMap(d);
  const digs = {};
  Object.keys(d.digs || {}).forEach(k => {
    digs[k] = Array.isArray(d.digs[k]) ? _dgDepthKeep(d.digs[k], map) : d.digs[k];
  });
  return Object.assign({}, d, { digs: digs });
}

// ── Фильтр по пласту ──────────────────────────────────────────────────────
// Клик по шапке пласта сужает страницу до его жанра, повторный — снимает.
//
// Это ВТОРАЯ ось сужения, независимая от глубины: глубина отвечает на «насколько
// далеко от знакомого», пласт — на «в какой породе». Они складываются, а не
// вытесняют друг друга, поэтому и живут раздельными переменными, и обе
// применяются в одном месте (_dgView) — иначе переключение одной сбрасывало бы
// другую, и человек терял бы половину набранного состояния на каждое движение.
//
// Состояние — модульная переменная, как _dgOpen и _dgDepth: разметка тела
// переписывается при каждом обновлении, и фильтр, живущий в разметке, умирал бы
// от любого «не предлагать». В localStorage не кладём (скилл
// ripster-offline-and-state): выбор живёт ровно столько, сколько сессия.
let _dgGenre = null;

// ПРЕДСТАВЛЕНИЕ под выбранный пласт — как _dgDepthView, копией. Исходные digs не
// трогаем: снятие фильтра обязано вернуть ровно то, что было, а откат
// digsExclude пишет в исходный массив.
//
// Находка без известного жанра под фильтром НЕ проходит. Это осознанная потеря,
// а не недосмотр: «сузить до пласта» — утверждение о составе экрана, и находка,
// про которую мы не знаем, из этой ли она породы, такое утверждение ломает.
// Взамен человеку прямо сказано в шапке фильтра, что жилы тоже сужены и что
// находки без жанра скрыты.
function _dgGenreView(d) {
  if (!_dgGenre || !d) return d;
  const map = _dgGenreMap(d.profile);
  const keep = it => map[((it || {}).artist || '').toLowerCase()] === _dgGenre;
  const digs = {};
  Object.keys(d.digs || {}).forEach(k => {
    digs[k] = Array.isArray(d.digs[k]) ? d.digs[k].filter(keep) : d.digs[k];
  });
  return Object.assign({}, d, { digs: digs });
}

// Оба сужения одной функцией. Всё, что показывает находки или считает их,
// обязано ходить сюда, а не в _dgDepthView: счётчик, разошедшийся с экраном на
// одно из двух сужений, читается как поломка, а не как «дополнительный факт».
function _dgView(d) { return _dgGenreView(_dgDepthView(d)); }

// Пересборка после смены сужения. Ровно та же пара вызовов, что и в
// digsSetDepth: тело из данных плюс счётчик находок. Точечная ветка _dgReconcile
// здесь не годится — меняется СОСТАВ пластов и жил, а реестр сеток как раз устарел.
function _dgApplyNarrow() {
  if (!_digsData) return;
  _digsRenderBody(_digsData);
  _dgSetFinds(_digsData);
}

function digsSeamPick(genre) {
  const g = (genre || '').trim();
  if (!g) return;
  _dgGenre = (_dgGenre === g) ? null : g;
  _dgApplyNarrow();
}

function digsClearGenre() {
  if (!_dgGenre) return;
  _dgGenre = null;
  _dgApplyNarrow();
}

// Шапка включённого фильтра. Повторный клик по пласту — способ снятия «вслепую»:
// он требует помнить, что именно ты нажал, и найти тот же пласт глазами. Явная
// строка с названием и кнопкой снятия стоит над результатом всегда.
function _dgFilterBar() {
  if (!_dgGenre) return '';
  return `<div class="dg-filter">
    <span class="dg-filter-t">${t('digs.f_on')}</span>
    <b class="dg-filter-g">${esc(_dgGenre)}</b>
    <button type="button" class="dg-filter-x" onclick="digsClearGenre()">${t('digs.f_clear')}</button>
    <span class="dg-filter-note">${t('digs.f_veins')}</span>
  </div>`;
}

// Пустой результат сужения. Пустая страница молча — худший исход: она
// неотличима от «раскопки сломались», и человек уходит чинить не то. Поэтому
// перечисляем ОБА сужения поимённо и даём снять каждое отдельно.
function _dgNarrowEmpty() {
  const why = [];
  if (_dgGenre) why.push(ti('digs.f_by_genre', { genre: _dgGenre }));
  if (_dgDepth !== 'far') why.push(ti('digs.f_by_depth', { depth: _dgDepthLabel() }));
  const acts =
      (_dgGenre ? `<button type="button" class="dg-filter-x" onclick="digsClearGenre()">${t('digs.f_clear')}</button>` : '')
    + (_dgDepth !== 'far' ? `<button type="button" class="dg-filter-x" onclick="digsSetDepth('far')">${t('digs.f_alldepth')}</button>` : '');
  return `<div class="dg-empty">
    <div class="dg-empty-t">${t('digs.f_empty')}</div>
    <div class="dg-empty-s">${esc(why.join(' · '))}</div>
    <div class="dg-empty-a">${acts}</div>
  </div>`;
}

// Подпись текущей глубины теми же словами, что на самой ручке: пересказ своими
// («только скачанные») заставил бы человека догадываться, ту же ли ручку имеют в виду.
function _dgDepthLabel() {
  if (_dgDepth === 'core') return t('digs.depth_core');
  if (_dgDepth === 'near') return t('digs.depth_near');
  return t('digs.depth_far');
}

// Текущее положение видно ЯВНО: невидимого состояния у органа управления не
// бывает — человек должен читать глубину, а не вспоминать её.
function _dgSyncDepthUI() {
  document.querySelectorAll('#view-digs .dg-depth-b').forEach(b => {
    const on = b.dataset.depth === _dgDepth;
    b.classList.toggle('on', on);
    b.setAttribute('aria-checked', on ? 'true' : 'false');
  });
}

// Переключение — целиком на клиенте, из уже загруженных _digsData. Ни одного
// запроса: смысл ручки в мгновенном отклике, а поход на сервер здесь стоил бы
// полного пересчёта профиля.
//
// Пересборку отдаём _digsRenderBody, а не собираем свою: глубина меняет СОСТАВ
// пластов (пласт без находок обязан исчезнуть целиком, а его жанр — уехать в
// «остальные пласты»), и это ровно тот структурный случай, ради которого
// _digsRenderBody и выделен. Точечная ветка _dgReconcile здесь не годится —
// она перерисовывает сетки по реестру, а реестр как раз и устарел.
function digsSetDepth(mode) {
  if (_DG_DEPTHS.indexOf(mode) < 0 || mode === _dgDepth) return;
  _dgDepth = mode;
  _dgSyncDepthUI();
  if (!_digsData) return;
  _digsRenderBody(_digsData);
  _dgSetFinds(_digsData);
}

// Счётчик находок в шапке — считаем локально по _digsData. Ходить за одним
// числом на сервер (а там это полный пересчёт профиля) незачем.
function _dgSetFinds(d) {
  const el = document.getElementById('dg-n-finds');
  if (!el || !el.firstChild) return;
  // Считаем по ТЕКУЩЕЙ глубине: общее число рядом с урезанным экраном — не
  // «дополнительный факт», а расхождение, которое читается как поломка.
  el.firstChild.nodeValue = String(Object.values((_dgView(d) || {}).digs || {})
    .reduce((n, a) => n + (Array.isArray(a) ? a.length : 0), 0));
}

// Пласты, которые РЕАЛЬНО нарисованы: жанры, под которыми после обоих сужений
// (глубина + фильтр по пласту) осталась хотя бы одна находка. Считается тем же
// способом, что и в _digsRenderBody, и читается шапкой — иначе счётчик разойдётся
// с экраном. Это ровно тот класс «посчитано и не показано», который чинили 15.08:
// «находок» уже следовало за выборкой, а «пластов» показывало все восемь при одном
// пласте на экране.
function _dgLiveSeamCount(dta) {
  const d = _dgView(dta) || dta;
  const p = d.profile || {};
  const inSeams = [].concat((d.digs || {}).missing_release || [],
                            (d.digs || {}).played_not_owned || []);
  const { out } = _digsByGenre(p, inSeams);
  return (p.genres || []).filter(g => (out[g.genre] || []).length).slice(0, 8).length;
}

function digsRenderHero(d) {
  const p = d.profile || {};
  // Находки и пласты — по текущей выборке (глубина + фильтр): счётчик обязан
  // описывать то, что на экране. «Уже у тебя» остаётся общим намеренно — он
  // прямо подписан как «уже у тебя» и честно говорит про фонотеку целиком.
  const all = Object.values((_dgView(d) || {}).digs || {})
    .reduce((n, a) => n + (Array.isArray(a) ? a.length : 0), 0);
  const set = (id, v) => {
    const el = document.getElementById(id);
    if (el) el.firstChild.nodeValue = String(v);
  };
  set('dg-n-finds', all);
  set('dg-n-seams', _dgLiveSeamCount(d));
  set('dg-n-owned', d.owned_albums != null ? d.owned_albums : ((p.totals || {}).download_events || 0));
  // Объяснение — в подсказку на счётчиках: оно нужно один раз, а место занимало
  // постоянно (замечание владельца: «мелкие буквы с мануалом прямо на странице»).
  const cap = _dg('dg-hero-cap-host');
  const top = (p.genres || [])[0];
  if (cap && cap.setAttribute) cap.setAttribute('title', (top
    ? ti('digs.hero', { genre: top.genre, share: top.share,
                        dl: (p.totals || {}).download_events || 0,
                        art: (p.totals || {}).artists_known || 0 })
    : t('digs.hero_empty')).replace(/<[^>]+>/g, ''));

}

// Тело вкладки — пласты и жилы. Вынесено из digsRender отдельной функцией, чтобы
// его можно было пересобрать из данных, не трогая шапку, чипы опор и подвал.
function _digsRenderBody(dta) {
  // Единственная точка, где применяются ОБА сужения — глубина и пласт: сюда
  // сходятся ВСЕ пути перерисовки тела (первая отрисовка, _dgReconcile,
  // digsSetDepth, digsSeamPick), поэтому фильтрам негде разойтись с экраном.
  dta = _dgView(dta) || dta;
  const p = dta.profile || {};
  // Реестр сеток перестраивается вместе с разметкой: иначе в нём остались бы
  // ключи исчезнувших пластов и старые массивы находок.
  _dgGrids.clear();

  // В пласты идут находки, привязанные к релизу или треку. Гости шоу и забытое
  // жанра не имеют — им отдельные жилы ниже.
  const inSeams = [].concat(dta.digs.missing_release || [], dta.digs.played_not_owned || []);
  const { out, rest } = _digsByGenre(p, inSeams);
  const all = p.genres || [];
  // Пласт заводим ПОД НАХОДКИ, а не под верхние семь жанров. Прежний срез
  // `.slice(0, 7)` делал две вещи разом: рисовал пустые слои и МОЛЧА ТЕРЯЛ
  // находки жанров с восьмого места — они не попадали ни в пласт, ни в «прочие».
  const live = all.filter(g => (out[g.genre] || []).length).slice(0, 8);
  const liveSet = new Set(live.map(g => g.genre));
  Object.keys(out).forEach(k => { if (!liveSet.has(k)) rest.push(...out[k]); });

  // «В этот день» СЧИТАЛОСЬ, но не рисовалось нигде: двенадцать находок уходило
  // в счётчик шапки и пропадало. Узнавание сильнее рекомендации — жила идёт первой.
  // Список собран ДО отрисовки пластов: без него не сказать, пусто ли на странице
  // целиком, а пустоту надо объяснить в самом верху, а не под всеми жилами.
  const extra = [
    { t: 'digs.s_anniv',  items: dta.digs.anniversary || [], per: 1 },
    // Отмеченное в дереве похожих — единственная жила, которую человек набрал
    // руками, и стоит она сразу за узнаванием: он только что сам её и наполнил.
    // Отдельного пути отрисовки у неё нет намеренно — обычная жила общего
    // списка, поэтому оба сужения, счётчик находок и «не предлагать» работают
    // по ней тем же кодом, что и по всему остальному.
    { t: 'digs.s_tree',   items: dta.digs.from_tree || [],   per: 2 },
    { t: 'digs.s_other',  items: rest,                       per: 2 },
    { t: 'digs.s_guest',  items: dta.digs.show_guest || [],  per: 2 },
    { t: 'digs.s_forgot', items: dta.digs.forgotten || [],   per: 2 },
  ];
  const shown = live.reduce((n, g) => n + (out[g.genre] || []).length, 0)
    + extra.reduce((n, s2) => n + s2.items.length, 0);

  // «Остальные пласты» под фильтром не рисуем. Строка утверждает «находок пока
  // нет — только доля во вкусе»; под сужением находки у этих жанров есть, просто
  // спрятаны — то есть утверждение стало бы ложным, и ложным ровно в тот момент,
  // когда человек пытается понять, что он сделал со страницей.
  const quiet = _dgGenre ? '' : _digsQuietSeams(all.filter(g => !liveSet.has(g.genre)), all);
  // Ничего не осталось И при этом что-то сужено — объясняем ЧТО и даём снять.
  // Иначе пустая страница неотличима от поломки.
  const seams = (!shown && (_dgGenre || _dgDepth !== 'far'))
    ? _dgNarrowEmpty()
    : (live.map((g, i) => _digsSeam(g, i, live.length, out[g.genre])).join('')
        || `<div style="color:var(--muted);font-size:11px;padding:14px 0">${t('digs.no_genres')}</div>`);
  _dg('digs-seams').innerHTML = _dgFilterBar() + seams + quiet;
  _dg('digs-sections').innerHTML = extra.map(s2 => {
    if (!s2.items.length) return '';
    // Шапка жилы и её сетка — два соседних узла; общая обёртка нужна затем, чтобы
    // опустевшую жилу можно было убрать целиком, а не оставить голый заголовок.
    const key = 'vein:' + s2.t;
    return `<div class="dg-vein-block" data-dg-block="${esc(key)}">`
      + `<div class="dg-vein"><h3>${t(s2.t)}</h3>`
      + `<span data-dg-cnt="${esc(key)}">${_dgCountText(key, s2.items.length)}</span></div>`
      + _dgGrid(s2.items, key, s2.per)
      + `</div>`;
  }).join('');
}

function digsRender() {
  const dta = _digsData;
  if (!dta) return;
  const p = dta.profile || {};
  _dg('digs-loading').style.display = 'none';
  try { digsApplyLook(); } catch (e) {}   // вид из настроек — до отрисовки
  digsRenderHero(dta);
  _dgSyncDepthUI();   // вьюха могла подгрузиться позже скрипта
  if (!p.seeded) digsShowOnboard();
  _dg('digs-profile').style.display = '';
  _digsRenderBody(dta);

  _dg('digs-artists').innerHTML =
    (p.artists || []).filter(a => !a.is_show).slice(0, 20)
      // Имя кодируем, а не экранируем кавычки руками: у артистов бывают
      // апострофы («Sam Feldt's…»), и наивная подстановка в onclick рвёт
      // разметку. encodeURIComponent снимает вопрос целиком.
      .map(a => _digsChip(a.name, a.downloads ? String(a.downloads) : '',
                          a.favorite ? 'fav' : '',
                          `digsExclude(decodeURIComponent('${encodeURIComponent(a.name)}'))`)).join('');

  const shows = (p.shows || []);
  if (shows.length) {
    _dg('digs-shows-wrap').style.display = '';
    _dg('digs-shows').innerHTML =
      shows.slice(0, 10).map(s2 => _digsChip(s2.name, String(s2.episodes), '', '')).join('');
  }
  const tot = p.totals || {};
  _dg('digs-foot').innerHTML =
    ti('digs.foot', { dl: tot.download_events || 0, pl: tot.play_events || 0,
                      art: tot.artists_known || 0, foreign: dta.foreign_filtered || 0 });
}

async function digsLoad(force) {
  const btn = _dg('digs-refresh');
  if (btn) btn.disabled = true;
  try {
    _digsData = await api('GET', '/api/digs/finds' + (force ? '?force=1' : ''));
    // Ответ сервера про отмеченное в дереве не знает и знать не должен — это
    // выбор, сделанный на клиенте. Возвращаем его в данные ДО отрисовки, иначе
    // любое обновление стирало бы жилу с экрана.
    _dgTreeInject(_digsData);
    digsRender();
    // Тур при первом заходе — после отрисовки: подсвечивать нечего, пока
    // находки не на экране.
    try { digsTourMaybeAuto(); } catch (e) {}
  } catch (e) {
    const el = _dg('digs-loading');
    if (el) { el.style.display = ''; el.textContent = '✗ ' + (e.message || e); }
  } finally {
    if (btn) btn.disabled = false;
  }
}

function digsInit() {
  if (!_digsData) digsLoad(false);
}

function digsShowOnboard() {
  const box = _dg('digs-onboard');
  if (!box) return;
  box.style.display = '';
  const wrap = _dg('digs-ob-genres');
  const chosen = new Set(((_digsData || {}).profile || {}).favorite_genres || []);
  wrap.innerHTML = _DIGS_GENRE_PICKS.map(g =>
    `<span class="dg-chip dg-pick${chosen.has(g) ? ' on' : ''}" data-g="${esc(g)}"
       onclick="this.classList.toggle('on')">${esc(g)}</span>`).join('');
  const ta = _dg('digs-ob-artists');
  if (ta && !ta.value) {
    const favs = ((_digsData || {}).profile || {}).artists || [];
    ta.value = favs.filter(a => a.favorite).map(a => a.name).join(', ');
  }
  box.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

async function digsSaveFavorites() {
  const st = _dg('digs-ob-status');
  const genres = Array.from(document.querySelectorAll('#digs-ob-genres .dg-pick.on'))
    .map(e => e.dataset.g);
  const artists = (_dg('digs-ob-artists').value || '')
    .split(',').map(s => s.trim()).filter(Boolean);
  if (st) { st.textContent = t('digs.saving'); st.style.color = 'var(--muted)'; }
  try {
    await api('POST', '/api/digs/favorites', { artists, genres });
    if (st) { st.textContent = t('digs.saved'); st.style.color = 'var(--green)'; }
    _dg('digs-onboard').style.display = 'none';
    await digsLoad(true);
  } catch (e) {
    if (st) { st.textContent = '✗ ' + (e.message || e); st.style.color = 'var(--red)'; }
  }
}

// «Это не моё» — слово владельца выше любых улик из статистики: надёжно вывести
// принадлежность из данных нельзя, а в базе лежат ещё и загрузки гостей бота.
//
// Отклик ОПТИМИСТИЧНЫЙ: экран меняется до запроса, запрос идёт фоном. Раньше
// здесь стоял `await digsLoad(true)` — полный пересчёт профиля на сервере плюс
// перерисовка всей страницы ради того, чтобы одна карточка исчезла.
//
// Оптимизм честен ровно настолько, насколько честен откат. Поэтому узлы карточек
// НЕ запоминаются: снимаются только данные, а экран оба раза собирается из них
// через _dgReconcile. Порядок находок задаёт _dgDiverse детерминированно от
// данных, поэтому после отката он совпадает с исходным по построению — и заодно
// сходятся счётчики в шапках, кнопки «Ещё N» и состав пластов.
async function digsExclude(name) {
  const key = (name || '').trim().toLowerCase();
  if (!key) { toast('✗ ' + t('digs.e_empty_name'), 'var(--red)'); return; }
  // Сравнение регистронезависимое — как в _dgDiverse: имя приходит и из карточки,
  // и из чипа опоры, и написание там не обязано совпадать.
  const other = v => (v || '').toLowerCase() !== key;

  // Чипы опор — плоский список без шапок и счётчиков, реконсилировать в нём
  // нечего: узел убирается и возвращается на место соседом справа.
  const chips = [];
  document.querySelectorAll('#digs-artists .dg-chip[data-artist]').forEach(el => {
    if (!other(el.dataset.artist)) chips.push({ el: el, at: el.parentNode, before: el.nextSibling });
  });

  const dta = _digsData || {};
  const digsSnap = {};
  Object.keys(dta.digs || {}).forEach(k => {
    if (Array.isArray(dta.digs[k])) digsSnap[k] = dta.digs[k];
  });
  // Реестр сеток хранит СВОИ массивы (пласты нарезаны по жанрам, «прочие»
  // собраны заново) — их чистить и восстанавливать надо отдельно от dta.digs.
  const gridSnap = new Map();
  const touched = [];
  _dgGrids.forEach((rec, k) => {
    gridSnap.set(k, rec.items);
    if (rec.items.some(it => !other(it.artist))) touched.push(k);
  });
  const artSnap = (dta.profile || {}).artists;

  // Одна операция на оба направления: drop=true убирает артиста, drop=false
  // возвращает снимок. Экран после неё в обоих случаях выведен из данных.
  const apply = drop => {
    Object.keys(digsSnap).forEach(k => {
      dta.digs[k] = drop ? digsSnap[k].filter(it => other(it.artist)) : digsSnap[k];
    });
    // Отмеченное в дереве живёт ДОЛЬШЕ страницы: его хозяин — модульная
    // переменная, а не _digsData. Не перенеси сюда результат — и «не
    // предлагать» убрало бы карточку ровно до следующего обновления, после
    // чего она вернулась бы сама. Направление одно на оба случая: что вышло
    // из apply, то и есть правда.
    _dgTreeFinds = (dta.digs.from_tree || []).slice();
    _dgGrids.forEach((rec, k) => {
      if (!gridSnap.has(k)) return;
      rec.items = drop ? gridSnap.get(k).filter(it => other(it.artist)) : gridSnap.get(k);
    });
    if (Array.isArray(artSnap)) {
      dta.profile.artists = drop ? artSnap.filter(a => other(a.name)) : artSnap;
    }
    _dgReconcile(touched);
    _dgSetFinds(dta);
  };

  apply(true);
  chips.forEach(c => { if (c.el.parentNode) c.el.parentNode.removeChild(c.el); });

  try {
    await api('POST', '/api/digs/exclude', { artist: name });
    toast(ti('digs.excluded', { name }), 'var(--muted)');
  } catch (e) {
    apply(false);
    // Чипы возвращаем С КОНЦА: сосед справа у убранного чипа сам мог быть убран,
    // и при прямом порядке он ещё не стоит в дереве — порядок бы поехал.
    for (let i = chips.length - 1; i >= 0; i--) {
      const c = chips[i];
      if (!c.at) continue;
      c.at.insertBefore(c.el, c.before && c.before.parentNode === c.at ? c.before : null);
    }
    toast('✗ ' + ti('digs.exclude_failed', { name }) + ': ' + (e.message || e), 'var(--red)');
  }
}

// Найденное отправляем обычным путём очереди — отдельной «загрузки
// рекомендации» не заводим, иначе у неё будет своя судьба и свои баги.
async function digsQueue(url) {
  try {
    await api('POST', '/api/download', { url });
    toast(t('digs.queued'), 'var(--green)');
  } catch (e) {
    toast('✗ ' + (e.message || e), 'var(--red)');
  }
}

function digsSearch(artist) {
  if (!artist) return;
  const nav = document.querySelector('.nav-item[data-view="search"]');
  if (typeof showView === 'function') showView('search', nav);
  // Поле поиска называется search-q. Раньше здесь стояло search-input — такого
  // элемента нет вовсе, поэтому запрос никуда не подставлялся и «Искать»
  // открывало пустой поиск. Ищем по АРТИСТУ, а не по альбому: мы пришли сюда с
  // именем артиста, и искать его в альбомах — не то, чего человек ждёт.
  const inp = document.getElementById('search-q');
  if (!inp) return;
  inp.value = artist;
  const ty = document.getElementById('search-type');
  if (ty && Array.from(ty.options).some(o => o.value === 'artist')) ty.value = 'artist';
  // Вьюха поиска подгружается лениво — если её ещё нет, ждём появления поля.
  if (typeof doSearch === 'function') setTimeout(doSearch, 60);
}


// ── Оформление из настроек ────────────────────────────────────────────────
// Настройки живут в Настройках → Раскопки, а применяются здесь через CSS-
// переменные: так вид меняется без перерисовки данных и без второго запроса.
const _DG_SHAPES = { circle: '50%', squircle: '14px', square: '4px', hex: '0' };
const _DG_DENSITY = { tight: '4px', normal: '8px', airy: '15px' };

function digsApplyLook() {
  const c = (window.S && S.config) || {};
  const root = document.getElementById('view-digs');
  if (!root) return;
  // Умолчание держим тем же, что в CSS вьюхи (56): иначе JS перебивает
  // стиль и обложка тихо остаётся мелкой.
  const size = parseInt(c['digs-size'] || 56, 10);
  root.style.setProperty('--dg-size', size + 'px');
  root.style.setProperty('--dg-radius', _DG_SHAPES[c['digs-shape'] || 'circle'] || '50%');
  root.style.setProperty('--dg-gap', _DG_DENSITY[c['digs-density'] || 'normal'] || '8px');
  // Шестиугольник — не радиус, а обрезка формы; поэтому отдельным свойством.
  root.style.setProperty('--dg-clip', (c['digs-shape'] === 'hex')
    ? 'polygon(25% 5%,75% 5%,100% 50%,75% 95%,25% 95%,0% 50%)' : 'none');
  root.dataset.bg = c['digs-bg'] || 'earth';
  root.dataset.motion = c['digs-motion'] || 'full';
  root.dataset.covers = (c['digs-covers'] !== false) ? '1' : '0';
  root.dataset.coon = (c['digs-coon'] !== false) ? '1' : '0';
}

// Чипы «сервисы» и «разделы» для вкладки настроек. Строим из кода, а не руками
// в разметке: список сервисов должен совпадать с тем, что реально умеет качалка.
const _DG_SVCS = ['apple', 'spotify', 'deezer', 'tidal', 'qobuz', 'soundcloud', 'beatport', 'yandex'];
const _DG_SECS = [
  ['played_not_owned', 'digs.s_played'], ['missing_release', 'digs.s_missing'],
  ['show_guest', 'digs.s_guest'], ['forgotten', 'digs.s_forgot'],
];

function _dgChipList(hostId, items, cfgKey, labelFn, defaultAll) {
  const host = document.getElementById(hostId);
  if (!host) return;
  const c = (window.S && S.config) || {};
  const cur = c[cfgKey];
  const on = new Set(Array.isArray(cur) && cur.length ? cur : (defaultAll ? items.map(i => i[0] || i) : []));
  host.innerHTML = items.map(it => {
    const key = it[0] || it;
    return `<span class="dg-chip dg-pick${on.has(key) ? ' on' : ''}" data-k="${esc(key)}"
      onclick="this.classList.toggle('on');_dgSaveChips('${hostId}','${cfgKey}')">${esc(labelFn(it))}</span>`;
  }).join('');
}

function _dgSaveChips(hostId, cfgKey) {
  const on = Array.from(document.querySelectorAll('#' + hostId + ' .dg-pick.on')).map(e => e.dataset.k);
  saveSetting(cfgKey, on);
}

function digsBuildSettingChips() {
  _dgChipList('dg-svc-list', _DG_SVCS, 'digs-services', s => s, false);
  _dgChipList('dg-sec-list', _DG_SECS, 'digs-sections', it => t(it[1]), true);
}


// ── Клики по находкам ─────────────────────────────────────────────────────
// Одинарный и двойной клик висят на ОДНОМ элементе, поэтому одинарный
// откладывается: браузер всегда выдаёт click первым, и без задержки двойной
// срабатывал бы ещё и как одинарный. 260 мс — обычный порог двойного клика.
let _dgClickTimer = null;

function _dgItemData(el) {
  return {
    artist: el.dataset.artist || '', title: el.dataset.title || '',
    url: el.dataset.url || '', service: el.dataset.svc || '',
    cover: el.dataset.cover || '',
  };
}

// Сервис выводим из ссылки: у находок из стора радара поля service нет, оно
// есть только у прослушиваний. Своего перечня здесь больше нет — он знал шесть
// сервисов из десяти, и находка с Beatport или Яндекса уходила в плеер без
// имени сервиса. Общая функция живёт в urlbar_detect.js.
function _dgService(d) {
  if (d.service) return d.service;
  return (typeof svcFromUrl === 'function') ? svcFromUrl(d.url || '') : '';
}

function digsItemClick(el) {
  const d = _dgItemData(el);
  const mode = ((window.S && S.config) || {})['digs-click'] || 'play';
  if (mode === 'none') return;
  if (!d.url) {
    // Играть нечего: у «гостей шоу» и «забытого» конкретного релиза нет —
    // предлагается артист, и правильное действие это поиск, а не тишина.
    digsSearch(d.artist);
    return;
  }
  const svc = _dgService(d);
  if (mode === 'open') { openExternal(d.url); return; }
  // Играем НАШИМ плеером и нашими токенами — никаких чужих превью.
  if (typeof playRelease === 'function') {
    playRelease(svc, d.url, d.title || d.artist, d.artist, d.cover);
  } else {
    digsSearch(d.artist);
  }
}

function digsItemDbl(el) {
  if (((window.S && S.config) || {})['digs-dblclick'] === 'off') return;
  digsBubbles(_dgItemData(el).artist);
}

// Делегирование: находки перерисовываются целиком, и вешать слушатели на
// каждую заново — лишняя работа и источник утечек.
document.addEventListener('click', (e) => {
  const el = e.target.closest && e.target.closest('#view-digs .dg-item');
  if (!el || e.target.closest('.dg-act')) return;   // кнопка действия — своя логика
  clearTimeout(_dgClickTimer);
  _dgClickTimer = setTimeout(() => digsItemClick(el), 260);
});
document.addEventListener('dblclick', (e) => {
  const el = e.target.closest && e.target.closest('#view-digs .dg-item');
  if (!el) return;
  clearTimeout(_dgClickTimer);                      // отменяем отложенный одинарный
  digsItemDbl(el);
});
// Клавиатура: находка — это tabindex-элемент, значит должна открываться Enter.
document.addEventListener('keydown', (e) => {
  if (e.key !== 'Enter') return;
  const el = e.target.closest && e.target.closest('#view-digs .dg-item');
  if (el) digsItemClick(el);
});

// ── Дерево похожих артистов ───────────────────────────────────────────────
// Пузыри по кругу вокруг центрального. Клик по пузырю уходит глубже — так и
// получается дерево: каждый шаг это новый круг вокруг выбранного.
// Выбранные в дереве. Множественный выбор нужен затем, что находки приходят
// пачкой: отметить пятерых и разом взять их в вишлист — обычный сценарий, а
// по одному это пять кругов туда-обратно.
let _dgPicked = new Set();

// Дерево было ТУПИКОМ: человек ходил по кругам, отмечал интересное — и всё
// исчезало при закрытии, потому что отмеченное умело ровно два действия
// («в вишлист» и «это моё»), а в саму страницу не возвращалось ничего.
//
// Возвращаем НАХОДКАМИ, обычной жилой общего списка, а не своим списком с
// собственной отрисовкой: тогда к ним сами собой применяются оба сужения, их
// считает счётчик в шапке и по ним работает «не предлагать» с откатом —
// ничего из этого не пришлось бы писать во второй раз.
//
// Релиза у такого артиста нет и искать его нечем: /api/digs/similar отдаёт имя
// и лицо, больше ничего. Карточка строится без `url`, и главная кнопка сама
// становится «Искать» — новой серверной работы не появляется ни на байт.
//
// Хозяин набора — модульная переменная, а НЕ _digsData: данные страницы
// заменяются целиком при каждом digsLoad, и отмеченное умирало бы от любого
// обновления. И не localStorage (скилл ripster-offline-and-state): выбор живёт
// ровно столько, сколько сессия.
let _dgTreeFinds = [];

// Лицо и «от кого пришёл» — из того круга, где артиста отметили: digsPick
// получает только имя, а причина обязана назвать источник.
const _dgTreeMeta = new Map();

// Сравнение регистронезависимое — как везде на вкладке (_dgDiverse, digsExclude):
// имя приходит из ответа сервера, из карточки и из чипа, и написание там не
// обязано совпадать.
function _dgTreeHas(name) {
  const k = (name || '').toLowerCase();
  return _dgTreeFinds.some(it => (it.artist || '').toLowerCase() === k);
}

// Отметка «включена», если артист уже вернулся находкой, даже когда набор для
// пакетных кнопок сброшен закрытием дерева. Иначе при повторном открытии
// галочка была бы снята у того, кто на странице уже лежит, и второй клик
// добавил бы его вторым — состояние экрана разошлось бы с данными.
function _dgPickedOn(name) { return _dgPicked.has(name) || _dgTreeHas(name); }

// Причина — КЛЮЧОМ и аргументами, а не готовой строкой: собранная здесь русская
// фраза осталась бы русской и в английском интерфейсе, потому что перевод
// карточки идёт через reason_key, а не через сам текст.
function _dgTreeAdd(name) {
  if (!name || _dgTreeHas(name)) return;
  const meta = _dgTreeMeta.get(name) || {};
  _dgTreeFinds.push({
    kind: 'from_tree', artist: name, title: '', url: '', service: '',
    cover: meta.pic || '',
    reason_key: 'digs.r_tree', reason_args: { name: meta.from || name },
  });
}

function _dgTreeDrop(name) {
  const k = (name || '').toLowerCase();
  _dgTreeFinds = _dgTreeFinds.filter(it => (it.artist || '').toLowerCase() !== k);
}

// Единственная стыковка с данными страницы. Жилу рисует _digsRenderBody по
// общему списку, поэтому достаточно положить массив в digs — сужения, счётчик
// и «не предлагать» подхватят его сами, без единой развилки «а это из дерева».
function _dgTreeInject(d) {
  if (d && d.digs) d.digs.from_tree = _dgTreeFinds;
  return d;
}

// Пересобрать страницу под деревом. Дерево лежит ПОВЕРХ неё, и перерисовки не
// видно — зато при закрытии человек находит отмеченное на месте, а не пустоту.
// Счётчик находок обновляем тем же вызовом, что и все прочие пути: число рядом
// с изменившимся экраном читается как поломка, а не как «дополнительный факт».
function _dgTreeRefresh() {
  if (!_digsData) return;
  _dgTreeInject(_digsData);
  _digsRenderBody(_digsData);
  _dgSetFinds(_digsData);
}

function _dgBubbleFace(name, pic, size) {
  // Фото, а если его нет — инициал. Пустой кружок с одним текстом читается
  // хуже, а подставлять чужое лицо нельзя, поэтому имя в ответе сверяется.
  return pic
    ? `<img src="${esc(pic)}" alt="" loading="lazy" decoding="async"
         style="width:100%;height:100%;object-fit:cover;border-radius:50%">`
    : `<span class="dg-bb-ini">${esc((name || '?').trim()[0] || '?')}</span>`;
}

// Отметка теперь делает ТРИ вещи, а не одну: набирает пачку для «в вишлист» и
// «это моё» (обе кнопки остались) и возвращает артиста в страницу находкой.
// Снятие отметки убирает находку — иначе «отметил по ошибке» лечилось бы
// только через «не предлагать», то есть запретом артиста навсегда.
function digsPick(name, el) {
  const on = !_dgPickedOn(name);
  if (on) { _dgPicked.add(name); _dgTreeAdd(name); }
  else { _dgPicked.delete(name); _dgTreeDrop(name); }
  if (el) el.classList.toggle('on', on);
  const bar = document.getElementById('dg-bb-picked');
  if (bar) {
    bar.style.display = _dgPicked.size ? '' : 'none';
    const cnt = document.getElementById('dg-bb-cnt');
    if (cnt) cnt.textContent = String(_dgPicked.size);
  }
  // Страницу под деревом пересобираем СРАЗУ, а не при закрытии: закрытие —
  // не единственный выход отсюда (уход на другую вкладку, перезагрузка вьюхи),
  // и отложенная сборка теряла бы отмеченное на каждом таком пути.
  _dgTreeRefresh();
}

async function digsPickedWatch(btn) {
  const names = Array.from(_dgPicked);
  if (!names.length) return;
  if (btn) btn.disabled = true;
  let ok = 0;
  for (const n of names) {
    try { await api('POST', '/api/watchlist', { name: n, service: 'apple', kind: 'artist' }); ok++; }
    catch (e) { /* по одному: одна неудача не должна ронять всю пачку */ }
  }
  toast(ti('digs.picked_watched', { n: ok }), 'var(--green)');
  if (btn) btn.disabled = false;
}

async function digsPickedMine(btn) {
  const names = Array.from(_dgPicked);
  if (!names.length) return;
  if (btn) btn.disabled = true;
  const cur = ((window.S && S.config) || {})['digs-favorite-artists'] || [];
  const next = Array.from(new Set([...cur, ...names]));
  try {
    await api('POST', '/api/digs/favorites',
              { artists: next, genres: (S.config || {})['digs-favorite-genres'] || [] });
    if (S.config) S.config['digs-favorite-artists'] = next;
    _digsData = null;                     // профиль изменился — пересчитать
    toast(ti('digs.picked_mined', { n: names.length }), 'var(--green)');
  } catch (e) { toast('✗ ' + (e.message || e), 'var(--red)'); }
  if (btn) btn.disabled = false;
}

// Путь по дереву: артисты, чьи пузыри уже раскрывали. Без этого второй уровень
// повторяет первый, и «копание» ходит по кругу из тех же имён.
let _dgTrail = [];

async function digsBubbles(artist) {
  if (!artist) return;
  if (!_dgTrail.includes(artist)) _dgTrail.push(artist);
  let ov = document.getElementById('dg-bubbles');
  if (!ov) {
    ov = document.createElement('div');
    ov.id = 'dg-bubbles';
    ov.onclick = (e) => { if (e.target === ov) digsBubblesClose(); };
    document.body.appendChild(ov);
  }
  ov.innerHTML = `<div class="dg-bb-wrap"><div class="dg-bb-load">${t('digs.bb_loading')}</div></div>`;
  let items = [], corePic = '', freshN = 0;
  try {
    // Уже пройденные — в исключения; знакомые сервер сам уводит в конец.
    const ex = encodeURIComponent(_dgTrail.slice(-30).join('|'));
    const r = await api('GET', '/api/digs/similar?artist=' + encodeURIComponent(artist)
                              + '&limit=12&exclude=' + ex);
    items = (r && r.items) || [];
    // Лицо и источник запоминаем на весь круг: отметить могут любого, а
    // причина находки обязана назвать, ОТ КОГО он пришёл.
    items.forEach(it => {
      if (it && it.name) _dgTreeMeta.set(it.name, { pic: it.pic || '', from: artist });
    });
    corePic = (r && r.pic) || '';
    freshN = (r && r.fresh_count) || 0;
  } catch (e) { /* пусто — покажем честное «не нашёл» */ }

  if (!items.length) {
    ov.innerHTML = `<div class="dg-bb-wrap"><div class="dg-bb-load">`
      + ti('digs.bb_empty', { name: esc(artist) }) + `</div></div>`;
    return;
  }
  const max = Math.max(...items.map(i => i.score || 1)) || 1;
  const R = 195;
  const nodes = items.map((it, i) => {
    const ang = (i / items.length) * Math.PI * 2 - Math.PI / 2;
    // Размер несёт похожесть — пузырь остаётся данными, а не украшением.
    const sz = 54 + Math.round(34 * ((it.score || 1) / max));
    const enc = encodeURIComponent(it.name);
    // Знакомых не прячем, но и не выдаём за находку — приглушаем и подписываем.
    const kn = it.known ? ' dg-bb-known' : '';
    const ttl = esc(it.name) + (it.known ? ' — ' + t('digs.bb_known') : '');
    return `<div class="dg-bb${_dgPickedOn(it.name) ? ' on' : ''}${kn}"
      style="left:calc(50% + ${Math.cos(ang) * R}px);top:calc(50% + ${Math.sin(ang) * R}px);
      width:${sz}px;height:${sz}px;animation-delay:${(i * 0.045).toFixed(2)}s"
      title="${ttl}"
      ondblclick="digsBubbles(decodeURIComponent('${enc}'))">
      ${_dgBubbleFace(it.name, it.pic, sz)}
      <span class="dg-bb-cap">${esc(it.name)}</span>
      <button class="dg-bb-tick" title="${t('digs.pick')}"
        onclick="event.stopPropagation();digsPick(decodeURIComponent('${enc}'),this.parentNode)">✓</button>
    </div>`;
  }).join('');
  const encA = encodeURIComponent(artist);
  ov.innerHTML = `<div class="dg-bb-wrap">
      <button class="dg-bb-close" onclick="digsBubblesClose()">×</button>
      <div class="dg-bb dg-bb-core" style="left:50%;top:50%;width:112px;height:112px">
        ${_dgBubbleFace(artist, corePic, 112)}
        <span class="dg-bb-cap">${esc(artist)}</span>
      </div>
      ${nodes}
      <div class="dg-bb-acts">
        <button class="dg-bb-act" onclick="digsWatch(decodeURIComponent('${encA}'),this)">${t('digs.bb_watch')}</button>
        <button class="dg-bb-act" onclick="digsMarkMine(decodeURIComponent('${encA}'),this)">${t('digs.bb_mine')}</button>
        <button class="dg-bb-act" onclick="digsSearch(decodeURIComponent('${encA}'))">${t('digs.bb_find')}</button>
      </div>
      <div class="dg-bb-picked" id="dg-bb-picked" style="display:${_dgPicked.size ? '' : 'none'}">
        <span>${t('digs.picked')} <b id="dg-bb-cnt">${_dgPicked.size}</b></span>
        <button class="dg-bb-act" onclick="digsPickedWatch(this)">${t('digs.bb_watch')}</button>
        <button class="dg-bb-act" onclick="digsPickedMine(this)">${t('digs.bb_mine')}</button>
        <button class="dg-bb-act" onclick="_dgPicked.clear();digsBubbles(decodeURIComponent('${encA}'))">${t('digs.picked_clear')}</button>
      </div>
      <div class="dg-bb-hint">${ti('digs.bb_fresh', { n: freshN })} · ${t('digs.bb_hint')}</div>
    </div>`;
}

// Закрыли дерево — путь пройден заново. Иначе следующее открытие сразу
// исключает всех, кого смотрели полчаса назад, и выдаёт пустоту.
//
// Стирается ПУТЬ и ПАЧКА для кнопок «в вишлист»/«это моё», но НЕ находки:
// _dgTreeFinds живёт дальше, иначе ради чего было ходить по дереву. Ровно на
// этом месте вкладка и была тупиком до 16.08.
function digsBubblesClose() {
  const ov = document.getElementById('dg-bubbles');
  if (ov) ov.remove();
  _dgTrail = [];
  _dgPicked.clear();
}

// ── Действия над найденным артистом ───────────────────────────────────────
// Следить через ВИШЛИСТ, а не через свою сущность: у вишлиста уже есть проверка
// новых релизов, автоскачка и вся обвязка. Заводить рядом второй список
// «избранное раскопок» значило бы поддерживать две судьбы одного намерения.
async function digsWatch(name, btn) {
  if (!name) return;
  try {
    // Следим ВСЕГДА через Apple: это единственный бесплатный полный каталог по
    // артисту (см. правило вишлиста); service говорит лишь куда качать.
    await api('POST', '/api/watchlist', { name, service: 'apple', kind: 'artist' });
    if (btn) { btn.classList.add('done'); btn.textContent = t('digs.bb_watching'); }
    toast(ti('digs.watched', { name }), 'var(--green)');
  } catch (e) {
    toast('✗ ' + (e.message || e), 'var(--red)');
  }
}

// «Это моё» — дописывает артиста в профиль вкуса. Именно здесь и замыкается
// самообучение: откопал → отметил → следующий подбор считается уже с ним.
async function digsMarkMine(name, btn) {
  if (!name) return;
  const cur = ((window.S && S.config) || {})['digs-favorite-artists'] || [];
  const next = Array.from(new Set([...cur, name]));
  try {
    await api('POST', '/api/digs/favorites', { artists: next, genres: (S.config || {})['digs-favorite-genres'] || [] });
    if (S.config) S.config['digs-favorite-artists'] = next;
    if (btn) { btn.classList.add('done'); btn.textContent = t('digs.bb_mine_done'); }
    toast(ti('digs.mined', { name }), 'var(--green)');
    _digsData = null;                     // профиль изменился — пересчитать при возврате
  } catch (e) {
    toast('✗ ' + (e.message || e), 'var(--red)');
  }
}
