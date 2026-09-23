/* ============================================================================
   Радар панели — вертикальная лента широких плиток (эталон: мобильный Ripster
   10_radar.png), а не сетка квадратных карточек, которой пользуется вкладка
   «Релизы» окна ПК.

   ПОТОК, а не ожидание. Обход 287 источников в окне ПК прежде ждал
   Promise.allSettled и рисовал всё разом: пустой экран на полторы минуты.
   Здесь каждый источник попадает на экран в момент своего ответа (тот же
   принцип, что «РАДАР: ПОТОК» в static/js/sc_tab.js), полная пересортировка —
   одна, в конце досмара.

   «РЕЛИЗ ВЫШЕЛ» — НЕ ПРО ДАТУ. Дата — анонс лейбла; витрины наполняются каждая
   .claude/skills/release-availability-matrix). Поэтому play/download
   предлагаются
   ТОЛЬКО когда матрица доступности (/api/availability) отвечает «есть хотя бы
   один сервис, откуда файл отдаётся прямо сейчас». Дата служит грубым
   пред-фильтром: то, что датировано дальше чем сегодня+2, не проверяем вовсе и
   показываем как анонс.

   Данные — живые эндпоинты того же сервера (тот же origin → та же сессия):
   /api/releases/*, /api/releases/upcoming, /api/availability,
   /api/release/expand, /api/queue/add, /api/bbc/download, /api/watchlist.
   Никаких /api/radar/* — их нет.
   ========================================================================== */

var PR = {
  out: [], upcoming: [], byUid: {}, seen: {},
  scan: null, flushTimer: null, sweepTimer: null, pend: [],
  limit: 60, ts: 0, running: false, follows: [],
  cfg: null, avail: {}, availPend: {}, opened: null, bound: false
};

var PR_SWEEP_TIMEOUT_MS = 360000;   // зависший источник не держит «идём по источникам» вечно
var PR_FLUSH_MS = 120;              // соседние ответы склеиваем в одну дописку
var PR_TTL_MS = 5 * 60000;          // повторный вход во вкладку не начинает обход заново
var PR_GRACE_DAYS = 2;              // NZ-опережение: «завтрашний» релиз уже качается

/* ── Мелочи без повторов: дата, период, качество ──────────────────────────── */
function prDay(off) {
  var d = new Date(Date.now() + (off || 0) * 86400000);
  return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') +
         '-' + String(d.getDate()).padStart(2, '0');
}

function prDayOf(rel) { return String((rel && rel.date) || '').slice(0, 10); }

/* «Вышел» или «анонс» — ГРУБЫЙ пред-фильтр по дате, финальное слово за
   матрицей доступности. +2 дня — тот же допуск, что у лейбл-источника в
   ripster/routes/radar.py: релиз, датированный завтра по местному календарю,
   в Новой Зеландии уже вышел. */
function prIsAhead(rel) {
  var d = prDayOf(rel);
  if (!d) return false;
  return d > prDay(PR_GRACE_DAYS);
}

/* Эфир BBC бывает запланирован на будущее: стрима ещё нет, качать нечего. */
function prBbcFuture(rel) {
  var from = String(rel.avail_from || '').slice(0, 10);
  if (from && from > prDay(0)) return true;
  return prIsAhead(rel);
}

function prQuality(svc) {
  var c = PR.cfg || {};
  if (svc === 'spotify') {
    var eng = c['spotify-engine'] || 'convert';
    return eng === 'orpheus_spotify' ? (c['orpheus-quality'] || 'hifi') : (c['quality'] || 'alac');
  }
  var k = { deezer: 'deezer-quality', qobuz: 'qobuz-quality', tidal: 'tidal-quality',
            beatport: 'beatport-quality', yandex: 'yandex-quality', apple: 'quality' }[svc];
  var dflt = { deezer: 'flac', qobuz: '27', tidal: 'lossless', beatport: 'hifi' }[svc];
  if (k) return c[k] || dflt || 'alac';
  if (svc === 'bbc') return 'mp3';
  if (svc === 'soundcloud') return (c['soundcloud-oauth-token'] || '').trim() ? 'hq' : 'mp3';
  return c['quality'] || 'alac';
}

function prJwtExpired(token) {
  try {
    var p = JSON.parse(atob(String(token).split('.')[1].replace(/-/g, '+').replace(/_/g, '/')));
    return p.exp && p.exp < Date.now() / 1000;
  } catch (e) { return false; }
}

/* ── Источники: те же, что опрашивает окно ПК ─────────────────────────────── */
function prActiveSvcs(c) {
  var cfg = String(c['releases-services'] || 'spotify').split(',')
    .map(function (s) { return s.trim(); }).filter(Boolean);
  return cfg.filter(function (svc) {
    if (svc === 'spotify') return true;
    if (svc === 'qobuz') return !!(c['qobuz-auth-token'] || '').trim();
    if (svc === 'tidal') { var tk = (c['tidal-token'] || '').trim(); return !!tk && !prJwtExpired(tk); }
    if (svc === 'deezer') return !!(c['deezer-arl'] || '').trim();
    return true;                      // bbc / soundcloud / apple идут по вишлисту
  });
}

function prRequests() {
  var c = PR.cfg || {};
  var days = parseInt(c['releases-days'], 10) || 30;
  var active = prActiveSvcs(c);
  var reqs = [];
  var types = 'album,single,compilation';
  if (String(c['releases-types'] || '').indexOf('appears_on') >= 0) types += ',appears_on';
  if (active.indexOf('spotify') >= 0)
    reqs.push(['spotify', '/api/spotify/releases?days=' + days + '&types=' + encodeURIComponent(types)]);
  ['qobuz', 'tidal', 'deezer', 'bbc', 'soundcloud', 'apple'].forEach(function (svc) {
    if (active.indexOf(svc) >= 0) reqs.push([svc, '/api/releases/' + svc + '?days=' + days]);
  });
  if (c['show-radar-labels'] === true) reqs.push(['labels', '/api/releases/labels?days=' + days]);
  if (c['show-upcoming'] === true) reqs.push(['upcoming', '/api/releases/upcoming?days=' + Math.min(120, days * 4)]);
  return reqs;
}

function prPeriod() {
  var days = parseInt((PR.cfg || {})['releases-days'], 10) || 30;
  return ti('m.radar.period_d', { n: days });
}

/* ── Ключи: дедуп ленты и идентичность карточки ───────────────────────────── */
function prKey(r) {
  return (String(r.title || '').toLowerCase() + '|' +
          String(r.artist || '').toLowerCase() + '|' +
          String(r.year || prDayOf(r)).slice(0, 4));
}
function prUid(r) {
  return String(r.service || '?') + ':' + String(r.url || r.id || prKey(r));
}

/* ── Обход ────────────────────────────────────────────────────────────────── */
async function radarSweep(force) {
  if (PR.running && !force) return;
  if (!PR.cfg) {
    try { PR.cfg = await api('/api/config'); }
    catch (e) { PR.cfg = {}; }
  }
  var reqs = prRequests();
  if (!reqs.length) {
    prBody().innerHTML = '<div class="empty">' + esc(t('m.radar.no_sources')) + '</div>';
    prSetSummary();
    return;
  }
  if (force) { PR.out = []; PR.upcoming = []; PR.seen = {}; PR.byUid = {}; PR.pend = []; PR.limit = 60; }
  PR.running = true;
  PR.ts = Date.now();
  PR.scan = { total: reqs.length, done: 0, found: 0, errors: [], started: Date.now(), aborted: false };
  prBody().innerHTML = force ? '' : prBody().innerHTML;
  if (!prBody().querySelector('.rtile')) prBody().innerHTML = '';
  prProgress();

  var scan = PR.scan;
  reqs.forEach(function (q) {
    api(q[1]).then(function (d) {
      prMerge(q[0], (d && d.releases) || [], (d && d.ok === false) ? (d.error || '') : '');
    }).catch(function (e) {
      prMerge(q[0], [], String(e && e.message || e));
    });
  });

  clearTimeout(PR.sweepTimer);
  PR.sweepTimer = setTimeout(function () {
    if (PR.scan !== scan || scan.aborted) return;
    scan.aborted = true;
    scan.done = scan.total;
    failToast(ti('m.radar.timeout', { sec: Math.round(PR_SWEEP_TIMEOUT_MS / 1000) }));
    radarFinish();
  }, PR_SWEEP_TIMEOUT_MS);

  prFollows();
}

function radarFinish() {
  var scan = PR.scan;
  if (!scan || scan.finished) return;
  scan.finished = true;
  PR.running = false;
  clearTimeout(PR.flushTimer); PR.flushTimer = null;
  var batch = PR.pend; PR.pend = [];
  if (batch.length) prAppend(batch);
  // Одна пересортировка в конце: дописанное «как пришло» встаёт по датам.
  PR.out.sort(function (a, b) { return prDayOf(b) < prDayOf(a) ? -1 : prDayOf(b) > prDayOf(a) ? 1 : 0; });
  PR.upcoming.sort(function (a, b) { return prDayOf(a) < prDayOf(b) ? -1 : prDayOf(a) > prDayOf(b) ? 1 : 0; });
  prRerender();
  prProgress();
  prSetSummary();
  if (scan.errors.length) failToast(scan.errors.slice(0, 2).join('; '));
}

/* Ответ одного источника: дедуп, раскладка «вышло / анонс», дописка. */
function prMerge(name, list, err) {
  var scan = PR.scan;
  if (!scan || scan.aborted) return;
  scan.done++;
  if (err) scan.errors.push(name + ': ' + err);
  var fresh = [];
  (Array.isArray(list) ? list : []).forEach(function (rel) {
    var k = prKey(rel);
    if (PR.seen[k]) return;
    PR.seen[k] = 1;
    var uid = prUid(rel);
    rel._uid = uid;
    PR.byUid[uid] = rel;
    if (name === 'upcoming' || prIsAhead(rel) || (rel.service === 'bbc' && prBbcFuture(rel))) {
      PR.upcoming.push(rel);
    } else {
      PR.out.push(rel);
      fresh.push(rel);
    }
    scan.found++;
  });
  if (scan.done === scan.total) { scan.finished = false; radarFinish(); return; }
  if (fresh.length) {
    PR.pend = PR.pend.concat(fresh);
    if (!PR.flushTimer) PR.flushTimer = setTimeout(prFlush, PR_FLUSH_MS);
  }
  prProgress();
  prSetSummary();
}

function prFlush() {
  PR.flushTimer = null;
  var batch = PR.pend; PR.pend = [];
  if (batch.length) prAppend(batch);
}

/* ── DOM ──────────────────────────────────────────────────────────────────── */
function prBody() { return document.getElementById('radar-body'); }

function radarShow() {
  var el = prBody(); if (!el) return;
  el.classList.add('radar');
  if (!PR.bound) { PR.bound = true; el.addEventListener('click', prOnClick); }
  if (!PR.out.length && !PR.upcoming.length && !PR.running) {
    el.innerHTML = '<div class="hint"><span class="spin"></span> ' + esc(t('m.loading')) + '</div>';
    radarSweep(true);
    return;
  }
  if (Date.now() - PR.ts > PR_TTL_MS && !PR.running) radarSweep(true);
  else prRerender();
}

function prSetSummary() {
  var line = document.getElementById('radar-sum');
  if (line) line.textContent = ti('m.radar.count', { out: PR.out.length, up: PR.upcoming.length });
}

function prProgress() {
  var bar = document.getElementById('radar-prog');
  var fill = document.getElementById('radar-prog-fill');
  var txt = document.getElementById('radar-prog-txt');
  if (!bar || !PR.scan) return;
  var s = PR.scan;
  var pct = s.total ? Math.round(s.done / s.total * 100) : 0;
  if (fill) fill.style.width = pct + '%';
  if (txt) txt.textContent = s.finished
    ? ti('m.radar.done', { sources: s.done, found: s.found })
    : ti('m.radar.walk', { done: s.done, total: s.total, found: s.found });
  bar.classList.toggle('done', !!s.finished);
  bar.style.display = s.finished && PR.out.length ? 'none' : '';
  if (s.finished && !PR.out.length && bar.style.display === 'none') bar.style.display = '';
}

// Обложка нужного размера и с живого хоста. То же правило, что у десктопного
// sc_tab.js::relCover, — держать их в согласии. i.scdn.co у части провайдеров
// виснет на TLS-рукопожатии (22.09.2026 с этой машины — таймаут 15 с, плитки
// радара стояли с пустыми квадратами); те же пути отдаёт собственный CDN Spotify
// image-cdn-ak.spotifycdn.com. Размер кодируется в пути: 4851 = 64, 1e02 = 300,
// b273 = 640 — плитке в 56 px нечего декодировать 640×640.
function prCover(url, px) {
  if (!url) return url;
  var u = String(url);
  if (u.indexOf('i.scdn.co/image/ab67616d0000') >= 0 || u.indexOf('spotifycdn.com/image/ab67616d0000') >= 0) {
    var code = px <= 96 ? 'ab67616d00004851' : (px <= 400 ? 'ab67616d00001e02' : 'ab67616d0000b273');
    return u.replace(/ab67616d0000(b273|1e02|4851)/, code)
            .replace('://i.scdn.co/', '://image-cdn-ak.spotifycdn.com/');
  }
  if (u.indexOf('mzstatic.com') >= 0) {
    var n = px <= 96 ? 128 : (px <= 320 ? 296 : (px <= 700 ? 632 : 1000));
    return u.replace(/\/\d+x\d+([a-z]{0,2})\.(jpg|png|webp)/i, '/' + n + 'x' + n + '$1.$2');
  }
  return u;
}

function prTileHtml(rel, ahead) {
  var svc = String(rel.service || '').toUpperCase();
  var d = prDayOf(rel);
  var type = rel.type ? String(rel.type).toUpperCase() : '';
  var meta = [type, rel.tracks ? tn('m.search.ntracks', rel.tracks) : '', rel.label || '']
    .filter(Boolean).join(' · ');
  return '<button type="button" class="rtile' + (ahead ? ' ahead' : '') + '" data-uid="' + esc(rel._uid) + '">' +
    '<span class="rt-art">' +
      (rel.cover ? '<img src="' + esc(prCover(rel.cover, 200)) + '" alt="" loading="lazy" decoding="async">' : '<i class="rt-noart">♪</i>') +
      '<b class="rt-svc">' + esc(svc || '?') + '</b>' +
    '</span>' +
    '<span class="rt-body">' +
      '<span class="rt-t">' + esc(rel.title || '—') + '</span>' +
      '<span class="rt-a">' + esc(rel.artist || '') + '</span>' +
      '<span class="rt-m">' + esc(meta) + '</span>' +
    '</span>' +
    '<span class="rt-side">' +
      (ahead ? '<b class="rt-soon">' + esc(ti('m.radar.will', { date: prDateShort(d) })) + '</b>'
             : '<b class="rt-day">' + esc(prDateShort(d)) + '</b>') +
      '<i class="rt-go">›</i>' +
    '</span>' +
  '</button>';
}

function prDateShort(d) {
  if (!d) return '—';
  var mo = [t('m.mon.1'), t('m.mon.2'), t('m.mon.3'), t('m.mon.4'), t('m.mon.5'), t('m.mon.6'),
            t('m.mon.7'), t('m.mon.8'), t('m.mon.9'), t('m.mon.10'), t('m.mon.11'), t('m.mon.12')];
  var n = parseInt(d.slice(5, 7), 10);
  return parseInt(d.slice(8, 10), 10) + ' ' + (mo[n - 1] || '') + ' ' + d.slice(0, 4);
}

function prSection(label, html) {
  return '<div class="rt-sec">' + esc(label) + '</div>' + html;
}

function prRerender() {
  var el = prBody(); if (!el) return;
  var shown = PR.out.slice(0, PR.limit);
  var html = '';
  if (!PR.out.length && !PR.upcoming.length && !(PR.scan && !PR.scan.finished)) {
    html = '<div class="empty">' + esc(ti('m.radar.nothing', { period: prPeriod() })) + '</div>';
  } else if (shown.length) {
    html = prSection(t('m.radar.out_now'), shown.map(function (r) { return prTileHtml(r, false); }).join(''));
    if (PR.out.length > shown.length)
      html += '<button type="button" class="rt-more" id="rt-more">' +
        esc(ti('m.radar.more', { n: PR.out.length - shown.length })) + '</button>';
  } else if (PR.out.length) {
    html = prSection(t('m.radar.out_now'), PR.out.map(function (r) { return prTileHtml(r, false); }).join(''));
  }
  if (!PR.out.length && PR.upcoming.length) {
    html += '<div class="hint">' + esc(t('m.radar.walk_none')) + '</div>';
  }
  if (PR.upcoming.length) {
    html += prSection(t('m.radar.upcoming'),
      PR.upcoming.slice(0, 40).map(function (r) { return prTileHtml(r, true); }).join(''));
  }
  html += prFollowsHtml();
  if (!html) html = '<div class="hint"><span class="spin"></span> ' + esc(t('m.loading')) + '</div>';
  el.innerHTML = html;
  var more = document.getElementById('rt-more');
  if (more) more.onclick = function () { PR.limit += 60; prRerender(); };
  prProgress();
  prSetSummary();
}

/* Дописка приходящих плиток — без перестройки ленты (главное для потока). */
function prAppend(items) {
  var el = prBody(); if (!el || !items.length) return;
  var tiles = el.querySelectorAll('.rtile');
  var anchor = tiles.length ? tiles[tiles.length - 1] : null;
  if (!anchor || !el.querySelector('.rt-sec')) { prRerender(); return; }
  var html = items.map(function (r) { return prTileHtml(r, false); }).join('');
  anchor.insertAdjacentHTML('afterend', html);
  prSetSummary();
}

/* ── Подписки (только чтение, как в прежнем радаре панели) ────────────────── */
async function prFollows() {
  try {
    var d = await api('/api/watchlist');
    PR.follows = (d && d.items) || [];
  } catch (e) { PR.follows = []; }
  if (document.getElementById('radar-body') && prBody().querySelector('.rt-sec')) prRerender();
}

function prFollowsHtml() {
  var w = PR.follows || [];
  var html = '<div class="rt-sec">' + esc(t('m.radar.watch')) + '</div>';
  html += w.length
    ? '<div class="chips">' + w.slice(0, 40).map(function (x) {
        return '<span class="chip">' + esc(x.name || '') + ' · ' + esc(svcName(x.service || '')) + '</span>';
      }).join('') + '</div>'
    : '<div class="empty">' + esc(t('m.radar.no_watch')) + '</div>';
  return html;
}

/* ── Клик по ленте: открыть карточку / «показать ещё» ─────────────────────── */
function prOnClick(ev) {
  var tile = ev.target && ev.target.closest ? ev.target.closest('.rtile') : null;
  if (!tile) return;
  prOpen(tile.getAttribute('data-uid'));
}

/* ── Карточка релиза: четыре действия, и только для того, что ВЫШЛО ────────── */
function prOpen(uid) {
  var rel = PR.byUid[uid]; if (!rel) return;
  PR.opened = rel;
  openSheet('collection', String(rel.title || t('m.radar.title')));
  var body = document.getElementById('sheet-body'); if (!body) return;
  body.dataset.uid = uid;
  body.innerHTML = prCardHtml(rel, true);
  prBindCard(body, rel);
  if (prNeedMatrix(rel) && !PR.avail[uid]) prCheck(rel, body);
}

/* Матрица нужна каталожным релизам. У BBC и SoundCloud витрин «где скачать»
   нет — там всё решает факт публикации (эфир уже в сети / аплоад уже лежит). */
function prNeedMatrix(rel) {
  return ['spotify', 'apple', 'deezer', 'qobuz', 'tidal', 'beatport'].indexOf(rel.service) >= 0;
}

function prActHtml(rel, state) {
  var isBbc = rel.service === 'bbc';
  var acts = [];
  if (state === 'out') {
    if (!isBbc) acts.push('<button type="button" class="rc-act play" data-act="play">' +
      '<span>▶</span>' + esc(t('m.radar.play')) + '</button>');
    acts.push('<button type="button" class="rc-act" data-act="dl">' +
      '<span>↓</span>' + esc(t('m.radar.dl')) + '</button>');
  }
  acts.push('<button type="button" class="rc-act" data-act="ext">' +
    '<span>↗</span>' + esc(t('m.radar.open')) + '</button>');
  acts.push('<button type="button" class="rc-act" data-act="copy">' +
    '<span>⎘</span>' + esc(t('m.radar.copy')) + '</button>');
  return '<div class="rc-acts">' + acts.join('') + '</div>';
}

function prCardHtml(rel, pending) {
  var d = prDayOf(rel);
  var ahead = (rel.service === 'bbc' && prBbcFuture(rel)) || prIsAhead(rel);
  var head =
    '<div class="rcard">' +
      (rel.cover ? '<img class="rc-cover" src="' + esc(prCover(rel.cover, 300)) + '" alt="">' : '<div class="rc-cover rc-noart">♪</div>') +
      '<div class="rc-info"><div class="rc-t">' + esc(rel.title || '—') + '</div>' +
        '<div class="rc-a">' + esc(rel.artist || '') + '</div>' +
        '<div class="rc-m">' + esc([String(rel.service || '').toUpperCase(), rel.type ? String(rel.type).toUpperCase() : '',
                                    prDateShort(d)].filter(Boolean).join(' · ')) + '</div>' +
        (rel.label ? '<div class="rc-l">' + esc(rel.label) + '</div>' : '') +
      '</div>' +
    '</div>';
  var mid;
  if (ahead) {
    mid = '<div class="rc-note">' + esc(ti('m.radar.not_out', { date: prDateShort(d) })) + '</div>';
  } else if (prNeedMatrix(rel)) {
    var m = PR.avail[rel._uid];
    if (pending && !m) mid = '<div class="rc-note"><span class="spin"></span> ' + esc(t('m.radar.checking')) + '</div>';
    else mid = prAvailHtml(rel, m);
  } else {
    mid = '<div class="rc-note ok">' + esc(t('m.radar.published')) + '</div>';
  }
  var state = ahead ? 'ahead' : (prNeedMatrix(rel) ? (PR.avail[rel._uid] && PR.avail[rel._uid].out ? 'out' : 'wait') : 'out');
  var foot = (state === 'out' || state === 'ahead') ? prActHtml(rel, state) : prActHtml(rel, 'wait');
  return head + mid + foot + '<div class="rc-link" id="rc-link"></div>';
}

/* Строка матрицы: не «релиз доступен», а где именно и почему нет. */
function prAvailHtml(rel, m) {
  if (!m) return '<div class="rc-note">' + esc(t('m.radar.checking')) + '</div>';
  if (m.error) {
    return '<div class="rc-note bad">' + esc(ti('m.radar.avail_fail', { x: m.error })) +
      ' <button type="button" class="rc-retry" data-act="retry">' + esc(t('m.radar.retry')) + '</button></div>';
  }
  var g = m.groups;
  var parts = [];
  if (g.ready.length)   parts.push('<div class="rc-ok">' + esc(ti('m.radar.ready_in', { list: g.ready.map(svcName).join(', ') })) + '</div>');
  if (g.wait.length)    parts.push('<div class="rc-warn">' + esc(ti('m.radar.wait_in', { list: g.wait.map(svcName).join(', ') })) + '</div>');
  if (g.region.length)  parts.push('<div class="rc-warn">' + esc(ti('m.radar.region_in', { list: g.region.map(svcName).join(', ') })) + '</div>');
  if (g.rights.length)  parts.push('<div class="rc-warn">' + esc(ti('m.radar.rights_in', { list: g.rights.map(svcName).join(', ') })) + '</div>');
  if (g.token.length)   parts.push('<div class="rc-warn">' + esc(ti('m.radar.token_in', { list: g.token.map(svcName).join(', ') })) + '</div>');
  if (!g.ready.length)  parts.push('<div class="rc-bad">' + esc(t('m.radar.nowhere')) + '</div>');
  var src = g.ready.length
    ? '<div class="rc-src">' + esc(ti('m.radar.from', { svc: svcName(m.recommended || g.ready[0]), q: prQuality(m.recommended || g.ready[0]) })) + '</div>'
    : '';
  return '<div class="rc-avail">' + parts.join('') + src + '</div>';
}

async function prCheck(rel, body) {
  var uid = rel._uid;
  if (PR.availPend[uid]) { await PR.availPend[uid]; return; }
  var p = (async function () {
    var q = 'url=' + encodeURIComponent(rel.url || '') +
            '&title=' + encodeURIComponent(rel.title || '') +
            '&artist=' + encodeURIComponent(rel.artist || '');
    try {
      var d = await api('/api/availability?' + q);
      if (!d || d.ok === false) { PR.avail[uid] = { error: (d && (d.error || d.error_key)) || t('m.radar.nowhere') }; return; }
      var svcs = d.services || {};
      var groups = { ready: [], wait: [], region: [], rights: [], token: [], noid: [] };
      Object.keys(svcs).forEach(function (s) {
        var v = svcs[s] || {};
        if (v.available) groups.ready.push(s);
        else if (v.reason === 'region_locked') groups.region.push(s);
        else if (v.reason === 'no_entitlement') groups.rights.push(s);
        else if (v.reason === 'no_token') groups.token.push(s);
        else if (v.reason === 'no_identifier') groups.noid.push(s);
        else groups.wait.push(s);
      });
      var urls = {};
      groups.ready.forEach(function (s) { if (svcs[s].url) urls[s] = svcs[s].url; });
      PR.avail[uid] = {
        groups: groups, urls: urls, out: groups.ready.length > 0,
        recommended: d.recommended || (groups.ready[0] || '')
      };
    } catch (e) {
      PR.avail[uid] = { error: String(e && e.message || e) };
    } finally {
      delete PR.availPend[uid];
    }
  })();
  PR.availPend[uid] = p;
  await p;
  if (PR.opened === rel && body && body.dataset.uid === uid) {
    body.innerHTML = prCardHtml(rel, false);
    prBindCard(body, rel);
  }
}

function prBindCard(body, rel) {
  body.querySelectorAll('[data-act]').forEach(function (b) {
    b.onclick = function (ev) {
      ev.stopPropagation();
      var a = b.getAttribute('data-act');
      if (a === 'play') prPlay(rel, b);
      else if (a === 'dl') prDownload(rel, b);
      else if (a === 'ext') prExternal(rel.url || '');
      else if (a === 'copy') prCopy(rel.url || '', b);
      else if (a === 'retry') { delete PR.avail[rel._uid]; prCheck(rel, body); }
    };
  });
  var link = body.querySelector('#rc-link');
  if (link) link.textContent = rel.url || '';
  var head = body.querySelector('.rc-cover');
  if (head && rel.cover) head.onclick = function () {
    var cs = document.getElementById('coverstage');
    cs.innerHTML = '<img src="' + esc(prCover(rel.cover, 1000)) + '" alt="">';
    cs.classList.add('on');
  };
}

/* ── Играть: разворот релиза через /api/release/expand (тот же путь, что у
   окна ПК), очередь — штатным T.launch: мост в окно ПК либо свой <audio>. ── */
async function prPlay(rel, btn) {
  if (btn) btn.disabled = true;
  try {
    var q = 'service=' + encodeURIComponent(rel.service || '') +
            '&url=' + encodeURIComponent(rel.url || '') +
            '&title=' + encodeURIComponent(rel.title || '') +
            '&artist=' + encodeURIComponent(rel.artist || '');
    var d = await api('/api/release/expand?' + q);
    var tracks = (d && d.tracks) || [];
    if (rel.service === 'spotify' || rel.service === 'apple') {
      /* эти два сервиса сами не стримятся нашим беком: бэк уже подобрал копию
         по ISRC — без неё трек не играет, и врать про него нельзя */
      tracks = tracks.filter(function (tr) { return tr.playable_service && tr.playable_id != null; });
      if (!tracks.length) { failToast(t('m.radar.no_play_match')); return; }
    }
    if (!tracks.length) { failToast(t('m.mix.empty')); return; }
    var items = tracks.map(function (tr) {
      return {
        service: tr.playable_service || rel.service, id: String(tr.playable_id != null ? tr.playable_id : tr.id),
        title: tr.title, artist: tr.artist || rel.artist,
        cover: prCover(tr.artwork || tr.cover || rel.cover || '', 640), duration: Number(tr.duration || tr.length || 0) || 0,
        album: (d.album && d.album.title) || rel.title, permalink: tr.url || rel.url, url: '', resolved: false
      };
    });
    S.station = t('m.tab.radar') + ' · ' + (rel.title || '');
    T.launch(items, 0);
  } catch (e) {
    failToast(ti('m.radar.play_fail', { x: String(e && e.message || e) }));
  } finally {
    if (btn) btn.disabled = false;
  }
}

/* ── Скачать: обычная очередь, тем же POST, что жмёт вкладка «Релизы» окна ПК */
async function prDownload(rel, btn) {
  if (btn) btn.disabled = true;
  try {
    if (rel.service === 'bbc') {
      var pid = ((rel.url || '').match(/programmes\/([a-z0-9]+)/i) || [])[1] || rel.id || '';
      await apiPost('/api/bbc/download', { pid: pid, vpid: '', title: rel.title, artist: rel.artist || 'BBC Radio',
                                           image_url: prCover(rel.cover || '', 640) });
      toast(t('m.radar.queued_bbc'), true);
      return;
    }
    var a = PR.avail[rel._uid];
    var svc = (a && a.recommended) || rel.service || '';
    var url = (a && a.urls && a.urls[svc]) || rel.url || '';
    if (!url) { failToast(t('m.radar.nowhere')); return; }
    var d = await apiPost('/api/queue/add', {
      url: url, quality: prQuality(svc), title: rel.title, artist: rel.artist,
      meta: { title: rel.title, artist: rel.artist, artworkUrl: prCover(rel.cover || '', 640), album: rel.title }
    });
    if (d && d.ok) toast(ti('m.radar.queued', { svc: svcName(svc) }), true);
    else if (d && d.duplicate) toast(t('m.radar.dup'), true);
    else failToast(ti('m.radar.dl_fail', { x: String((d && (d.msg || d.detail || d.error)) || '?') }));
  } catch (e) {
    failToast(ti('m.radar.dl_fail', { x: String(e && e.message || e) }));
  } finally {
    if (btn) btn.disabled = false;
  }
}

/* ── Ссылка: скопировать там, где navigator.clipboard может не быть вовсе ─── */
async function prCopy(text, btn) {
  var s = String(text || '');
  if (!s) { failToast(t('m.radar.no_link')); return; }
  var ok = false;
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(s);
      ok = true;
    }
  } catch (e) { ok = false; }
  if (!ok) {
    /* WebView2 режет попапы и иногда отдаёт NotAllowedError на async-буфер:
       классический textarea + execCommand работает и без защищённого контекста */
    try {
      var ta = document.createElement('textarea');
      ta.value = s;
      ta.setAttribute('readonly', '');
      ta.style.cssText = 'position:fixed;left:-9999px;top:0;opacity:0';
      document.body.appendChild(ta);
      ta.focus(); ta.select();
      ta.setSelectionRange(0, s.length);
      ok = document.execCommand('copy');
      document.body.removeChild(ta);
    } catch (e) { ok = false; }
  }
  var link = document.getElementById('rc-link');
  if (link && s) { link.textContent = s; link.classList.add('shown'); }
  if (btn) {
    var old = btn.innerHTML;
    btn.innerHTML = '<span>' + (ok ? '✓' : '✗') + '</span>' + esc(t(ok ? 'm.radar.copied' : 'm.radar.copy_fail'));
    setTimeout(function () { if (btn.isConnected) btn.innerHTML = old; }, 1800);
  }
  if (ok) toast(t('m.radar.copied'), true);
  else failToast(t('m.radar.copy_manual'));
}

/* ── Внешняя ссылка: системный браузер. Панель, ушедшая на страницу сервиса,
   теряет плеер — поэтому window.open только как запасной ход. ─────────────── */
async function prExternal(url) {
  var abs = String(url || '');
  if (!/^https?:/i.test(abs)) { failToast(t('m.radar.no_link')); return; }
  /* WebView2 режет window.open на корню, поэтому системный браузер через бэк —
     основной путь, а не запасной ход. window.open остаётся только для case,
     когда панель открыта в обычном браузере отдельной вкладкой и сервер сам
     открыть браузер не может. */
  try {
    var r = await apiPost('/api/open-url', { url: abs });
    if (r && r.ok) { toast(t('m.radar.opened_sys'), true); return; }
  } catch (e) {}
  var w = null;
  try { w = window.open(abs, '_blank', 'noopener'); } catch (e) { w = null; }
  if (w) { toast(t('m.radar.opened_tab'), true); return; }
  await prCopy(abs, null);
  failToast(t('m.radar.open_fail'));
}
