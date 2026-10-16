/* release_card.js — карточка релиза в радаре (задача 065).
   Точка входа: window.RpReleaseCard.render(rel, attrs, opts).
   Все действия — через data-act + один делегированный обработчик. */
(function () {
  'use strict';

  /* ── Делегированный обработчик ────────────────────────────────────────────
     Один слушатель на #releases-grid ловит все клики по [data-act].
     Старые inline onclick каждый раз вызывали event.stopPropagation();
     делегат делает то же самое, чтобы клики не всплывали до карточки. */
  var _bound = false;
  function _ensureBound() {
    if (_bound) return;
    _bound = true;
    document.addEventListener('click', function (e) {
      var el = e.target.closest('[data-act]');
      if (!el) return;
      e.preventDefault();
      e.stopPropagation();
      var act = el.dataset.act;
      var h = _actions[act];
      if (h) h(el, e);
    });
    document.addEventListener('change', function (e) {
      if (e.target.matches('.rel-q-select')) {
        e.stopPropagation();
        if (typeof _relSetQuality === 'function') _relSetQuality(e.target);
      }
    });
  }

  /* ── Действия ───────────────────────────────────────────────────────────── */
  var _actions = {};

  _actions.play = function (el) {
    if (typeof playRelease === 'function')
      playRelease(el.dataset.service, el.dataset.url, el.dataset.title, el.dataset.artist, el.dataset.cover || '');
  };
  _actions['play-bbc'] = function (el) {
    if (typeof playRelease === 'function')
      playRelease('bbc', el.dataset.url, el.dataset.title, el.dataset.artist, el.dataset.cover || '');
  };
  _actions['play-sc'] = function (el) {
    if (typeof playStreamTrack === 'function')
      playStreamTrack('soundcloud', el.dataset.trackId, el.dataset.title, el.dataset.artist, el.dataset.cover || '');
  };
  _actions['open-release'] = function (el) {
    var fn = el.dataset.openFn;
    if (fn === 'album' && typeof openAlbumPage === 'function')
      openAlbumPage(el.dataset.service, el.dataset.albumId);
    else if (fn === 'comp' && typeof relOpenRelease === 'function')
      relOpenRelease(el.dataset.service, el.dataset.albumId, el.dataset.namesCsv || '');
    else if (typeof playRelease === 'function')
      playRelease(el.dataset.service, el.dataset.url, el.dataset.title, el.dataset.artist, el.dataset.cover || '');
  };
  _actions['open-artist'] = function (el) {
    if (typeof openArtistPage === 'function')
      openArtistPage(el.dataset.service, el.dataset.artistId);
  };
  _actions['sc-browse'] = function (el) {
    if (typeof scBrowseUser === 'function' && typeof showView === 'function') {
      showView('soundcloud', document.querySelector('.nav-item[data-view=soundcloud]'));
      setTimeout(function () { scBrowseUser(el.dataset.artistId, el.dataset.artist); }, 60);
    }
  };
  _actions['open-label'] = function (el) {
    if (typeof openLabelPage === 'function') openLabelPage(el.dataset.label);
  };
  _actions.download = function (el) {
    var btn = el.closest('.rel-dl-btn') || el;
    if (typeof downloadRelease === 'function')
      downloadRelease(btn, el.dataset.service, el.dataset.url, el.dataset.title, el.dataset.artist);
  };
  _actions['dl-bbc'] = function (el) {
    if (typeof bbcDownload === 'function')
      bbcDownload(el.dataset.pid, '', el.dataset.title, el.dataset.artist, el.dataset.cover || '');
  };
  _actions['dl-sc'] = function (el) {
    if (typeof _relScDownload === 'function')
      _relScDownload(el, el.dataset.url, el.dataset.title, el.dataset.artist, el.dataset.cover || '', Number(el.dataset.duration) || 0);
  };
  _actions['smart-download'] = function (el) {
    if (typeof smartDownloadRelease === 'function')
      smartDownloadRelease(el, el.dataset.url, el.dataset.title, el.dataset.artist);
  };
  _actions['toggle-fav'] = function (el) {
    if (typeof toggleRelFav === 'function') toggleRelFav(el.dataset.uid);
  };
  _actions['copy-link'] = function (el) {
    navigator.clipboard.writeText(el.dataset.url);
    if (typeof toast === 'function') toast(t('toast.link_copied'));
  };
  _actions['open-external'] = function (el) {
    if (typeof openExternal === 'function') openExternal(el.dataset.url);
  };
  _actions['check-avail'] = function (el) {
    if (typeof relCheckAvail === 'function')
      relCheckAvail(el, el.dataset.url, el.dataset.title, el.dataset.artist);
  };
  _actions['bbc-cue'] = function (el) {
    if (typeof bbcGetCue === 'function') bbcGetCue(el.dataset.pid, el.dataset.title, el.dataset.artist);
  };
  _actions['bbc-schedule'] = function (el) {
    if (typeof bbcScheduleToggle === 'function')
      bbcScheduleToggle(el, el.dataset.channel || '', el.dataset.start || '',
        Number(el.dataset.duration) || 0, el.dataset.title, el.dataset.artist, el.dataset.cover || '');
  };

  /* ── Утилиты ────────────────────────────────────────────────────────────── */
  function _d(el, k, fb) { return el.dataset[k] !== undefined ? el.dataset[k] : (fb || ''); }

  /* ── Рендер: обычная карточка ───────────────────────────────────────────── */
  function render(rel, attrs, opts) {
    _ensureBound();
    if (rel && (rel.service === 'bbc' || rel.service === 'soundcloud')) return renderMix(rel, attrs, opts);

    var view = (opts && opts.view) || 'all';
    var dt = rel.date ? new Date(rel.date + 'T00:00:00').toLocaleDateString(_dateLoc(), {day:'numeric',month:'short',year:'numeric'}) : '';
    var head = (typeof relHeadArtist === 'function') ? relHeadArtist(rel) : (rel.alb_artist || rel.artist || '');
    var feat = (typeof relFeaturedArtists === 'function') ? relFeaturedArtists(rel) : [];
    var featCsv = feat.join(', ');
    var isWatched = !rel.alb_artist || rel.alb_artist === rel.artist;
    var canOpen = !!rel.id && ['apple','spotify','deezer','qobuz','tidal'].indexOf(rel.service) >= 0;
    var isComp = !!(feat.length || (rel.alb_artist && rel.alb_artist !== rel.artist));
    var openFn = !canOpen ? 'play' : (isComp ? 'comp' : 'album');

    var svcClr = _svcColor(rel.service);
    var typeMap = {album:'ALBUM', single:'SINGLE', ep:'EP', compilation:t('rl.comp_badge'), mix:t('rl.mix_badge'), appears_on:t('rl.appears_badge'), live:'LIVE'};
    var typeClr = rel.type === 'single' ? 'var(--orange)' : (rel.type === 'album' ? '#1db954' : 'var(--muted2)');
    var typeTag = typeMap[rel.type] || escapeHtml((rel.type || '').toUpperCase());
    var uid = (typeof _relUID === 'function') ? _relUID(rel) : (rel.service + '|' + (rel.id || rel.url || ''));
    var isNew = (typeof _relIsNew === 'function') ? _relIsNew(rel) : false;
    var isFav = (typeof _relIsFav === 'function') ? _relIsFav(rel) : false;
    var isLive = !!rel.live;
    var baseBorder = isNew ? 'rgba(62,207,170,.55)' : 'var(--border)';
    var isSpotify = rel.service === 'spotify';

    var hiresBadge = rel.hires ? '<span class="rel-badge--hires">HI-RES</span>' : '';

    var qualSelect = isSpotify ? '' :
      '<select class="rel-q-select" data-svc="' + esc(rel.service) + '" title="' + esc(t('rl.quality_label')) + '">' +
        '<option value="' + esc((typeof resolveQuality === 'function') ? resolveQuality(rel.service) : '') + '">' +
          esc((typeof resolveQuality === 'function') ? resolveQuality(rel.service) : '') +
        '</option></select>';

    var _lyricsSvcs = {apple: true, deezer: true};
    var lyricsDefault = rel.service === 'apple'
      ? (typeof S !== 'undefined' && (S.config['embed-lrc'] !== false || !!S.config['save-lrc-file']))
      : false;
    var lyricsToggle = _lyricsSvcs[rel.service] ?
      '<label class="rel-lyrics-toggle">' +
        '<input type="checkbox" class="rel-lyrics-chk"' + (lyricsDefault ? ' checked' : '') + '/>' +
        esc(t('rl.lyrics_toggle')) +
      '</label>' : '';

    var titleTitle = esc(rel.title) + ' — ' + t(canOpen ? 'rl.open_release' : 'rl.listen');

    return '<div class="rel-card' + (isNew ? ' rel-card-new' : '') + '"' +
      ' data-uid="' + esc(uid) + '"' + (attrs || '') +
      ' style="--svc-clr:' + svcClr + ';--base-border:' + baseBorder + '"' +
      '>' +
      '<div class="rel-cover-wrap">' +
        (rel.cover
          ? '<img class="rel-cover" src="' + esc((typeof relCover === 'function') ? relCover(rel.cover, 300) : rel.cover) + '"' +
            ' data-lightbox-src="' + esc((typeof relCover === 'function') ? relCover(rel.cover, 1000) : rel.cover) + '"' +
            ' onerror="if(this.src!==this.dataset.lightboxSrc){this.src=this.dataset.lightboxSrc}"' +
            ' data-lightbox loading="lazy" decoding="async"/>'
          : '<div class="rel-cover-fallback">\u266A</div>') +
        '<button data-act="play" data-service="' + esc(rel.service) + '" data-url="' + esc(rel.url) + '"' +
          ' data-title="' + esc(rel.title) + '" data-artist="' + esc(rel.artist) + '"' +
          ' data-cover="' + esc(rel.cover || '') + '"' +
          ' title="' + esc(t('rl.listen')) + '" class="rel-play" aria-label="' + esc(t('rl.listen')) + '">' +
          '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 6.5 L18 12 L9 17.5 Z"/></svg></button>' +
        '<div class="rel-badge-corner rel-badge-corner--tl"><span class="rel-badge rel-badge--svc">' + escapeHtml((rel.service || '?').toUpperCase()) + '</span></div>' +
        '<div class="rel-badge-corner rel-badge-corner--tr"><span class="rel-badge rel-badge--type" style="--type-clr:' + typeClr + '">' + typeTag + '</span></div>' +
        (isNew ? '<div class="rel-badge-corner rel-badge-corner--bl"><span class="rel-badge--new">' + esc(t('rl.new_badge')) + '</span></div>' : '') +
        (isLive ? '<div class="rel-badge-corner rel-badge-corner--br" title="' + esc(t('rl.live_title')) + '"><span class="rel-live-badge"><span class="rel-live-dot"></span>' + esc(t('rl.live_badge')) + '</span></div>' : '') +
      '</div>' +
      '<div class="rel-body">' +
        '<div class="rel-title" data-act="open-release" data-open-fn="' + openFn + '"' +
          ' data-service="' + esc(rel.service) + '"' +
          (canOpen ? ' data-album-id="' + esc(rel.id) + '"' : '') +
          (!canOpen ? ' data-url="' + esc(rel.url) + '"' : '') +
          (!canOpen ? ' data-cover="' + esc(rel.cover || '') + '"' : '') +
          ' data-title="' + esc(rel.title) + '" data-artist="' + esc(rel.artist) + '"' +
          (isComp ? ' data-names-csv="' + esc(featCsv) + '"' : '') +
          ' title="' + titleTitle + '">' + esc(rel.title) + hiresBadge + '</div>' +
        '<div class="rel-artist' + ((rel.artist_id && isWatched) ? ' rel-artist--clickable' : '') + '"' +
          ' title="' + esc(head) + '"' +
          ((rel.artist_id && isWatched) ? ' data-act="open-artist" data-service="' + esc(rel.service) + '" data-artist-id="' + esc(rel.artist_id) + '"' : '') +
          '>' + esc(head) + '</div>' +
        (feat.length ? '<div class="rel-featured" title="' + esc(featCsv) + '">\u2728 ' + esc(t('rl.featured')) + ': ' + esc(featCsv) + '</div>' : '') +
        (rel.label ? '<div class="rel-label' + (rel.via_label ? ' rel-label--via' : '') + '"' +
          ' data-act="open-label" data-label="' + esc(rel.label) + '"' +
          ' title="' + esc(rel.label) + ' — ' + esc(t('lbl.open_page')) + '">' +
          (rel.via_label ? '\uD83C\uDFF7 ' : '') + esc(rel.label) + '</div>' : '') +
        '<div class="rel-meta">' + dt + (rel.tracks ? ' \u00B7 ' + rel.tracks + ' ' + t('p.trk_abbr') : '') + '</div>' +
        (view === 'hidden' && rel.reason ? '<div class="rel-reason" title="' + esc(rel.reason) + '">' + esc(rel.reason) + '</div>' : '') +
        qualSelect +
        lyricsToggle +
        '<div class="rel-avail"><button class="rel-avail-btn" data-act="check-avail"' +
          ' data-url="' + esc(rel.url) + '" data-title="' + esc(rel.title) + '" data-artist="' + esc(rel.artist) + '"' +
          ' title="' + esc(t('rl.avail_hint')) + '">' + esc(t('rl.avail_check')) + '</button></div>' +
        '<button class="rel-dl-btn" data-act="download"' +
          ' data-service="' + esc(rel.service) + '" data-url="' + esc(rel.url) + '"' +
          ' data-title="' + esc(rel.title) + '" data-artist="' + esc(rel.artist) + '">' +
          esc(t('btn.download')) + '</button>' +
        '<div class="rel-act-row">' +
          (canOpen ? '<button data-act="open-release" data-open-fn="' + openFn + '"' +
            ' data-service="' + esc(rel.service) + '" data-album-id="' + esc(rel.id) + '"' +
            (isComp ? ' data-names-csv="' + esc(featCsv) + '"' : '') +
            ' title="' + esc(t('rl.open_release')) + '" aria-label="' + esc(t('rl.open_release')) + '"' +
            ' class="rel-act-btn">\u2630</button>' : '') +
          '<button data-act="smart-download" class="rel-act-btn rel-act-btn--smart"' +
            ' data-url="' + esc(rel.url) + '" data-title="' + esc(rel.title) + '" data-artist="' + esc(rel.artist) + '"' +
            ' title="' + esc(t('rl.auto_src')) + '">\u26A1</button>' +
          (typeof afButton === 'function' ? afButton(rel.artist, 'background:transparent;border:1px solid var(--border);border-radius:7px;font-size:11px;cursor:pointer;font-family:var(--font)') : '') +
          '<button data-act="toggle-fav" data-uid="' + esc(uid) + '"' +
            ' class="rel-act-btn' + (isFav ? ' rel-act-btn--fav-on' : '') + '"' +
            ' title="' + esc(isFav ? t('sc2.unfav') : t('sc2.fav')) + '">' + (isFav ? '\u2605' : '\u2606') + '</button>' +
          '<button data-act="copy-link" data-url="' + esc(rel.url) + '"' +
            ' class="rel-act-btn" title="' + esc(t('ck.copy_link')) + '">\u2398</button>' +
          (typeof nmaButton === 'function' ? nmaButton(rel) : '') +
          '<a href="' + esc(rel.url) + '" data-act="open-external" data-url="' + esc(rel.url) + '"' +
            ' class="rel-act-btn" title="' + esc(t('ck.open_on')) + ' ' + escapeHtml(rel.service) + '">\u2197</a>' +
        '</div>' +
      '</div>' +
    '</div>';
  }

  /* ── Рендер: микс/эфир (BBC, SoundCloud) ────────────────────────────────── */
  function renderMix(rel, attrs, opts) {
    _ensureBound();
    var isBbc = rel.service === 'bbc';
    var clr = _svcColor(rel.service);
    var uid = (typeof _relUID === 'function') ? _relUID(rel) : (rel.service + '|' + (rel.url || rel.id || ''));
    var isNew = (typeof _relIsNew === 'function') ? _relIsNew(rel) : false;
    var isFav = (typeof _relIsFav === 'function') ? _relIsFav(rel) : false;
    var baseBorder = isNew ? 'rgba(62,207,170,.55)' : 'var(--border)';
    var dt = rel.date ? new Date(rel.date + 'T00:00:00').toLocaleDateString(_dateLoc(), {day:'numeric',month:'short',year:'numeric'}) : '';
    var dur = (typeof _relFmtDur === 'function') ? _relFmtDur(rel.duration) : '';
    var pid = isBbc ? ((rel.url || '').match(/programmes\/([a-z0-9]+)/i) || [])[1] || rel.id || '' : '';
    var liveStart = isBbc && typeof _bbcLiveFuture === 'function' ? _bbcLiveFuture(rel) : '';
    var badge = isBbc ? t('rl.episode_badge') : t('rl.upload_badge');

    var playAct = isBbc ? 'play-bbc' : 'play-sc';
    var playData = isBbc
      ? 'data-url="' + esc(rel.url) + '"'
      : 'data-track-id="' + esc(rel.id) + '"';
    var commonData = 'data-title="' + esc(rel.title) + '" data-artist="' + esc(rel.artist) + '" data-cover="' + esc(rel.cover || '') + '"';

    var dlBtn;
    if (liveStart) {
      dlBtn = '<button class="rel-dl-btn rel-dl-btn--mix bbc-sched-btn" data-act="bbc-schedule"' +
        ' data-key="' + esc((rel.channel || '') + '|' + liveStart) + '"' +
        ' data-channel="' + esc(rel.channel || '') + '" data-start="' + esc(liveStart) + '"' +
        ' data-duration="' + (Number(rel.duration) || 0) + '"' +
        ' ' + commonData + '></button>';
    } else if (isBbc) {
      dlBtn = '<button class="rel-dl-btn rel-dl-btn--mix" data-act="dl-bbc"' +
        ' data-pid="' + esc(pid) + '" ' + commonData + '>\u2B07 MP3</button>';
    } else {
      dlBtn = '<button class="rel-dl-btn rel-dl-btn--mix" data-act="dl-sc"' +
        ' data-url="' + esc(rel.url) + '" data-duration="' + (Number(rel.duration) || 0) + '"' +
        ' ' + commonData + '>' + esc(t('btn.download')) + '</button>';
    }

    var artistClick = '';
    if (!isBbc && rel.artist_id && typeof scBrowseUser === 'function') {
      artistClick = ' data-act="sc-browse" data-artist-id="' + esc(rel.artist_id) + '" data-artist="' + esc(rel.artist) + '"';
    }

    return '<div class="rel-card rel-card-mix' + (isNew ? ' rel-card-new' : '') + '"' +
      ' data-uid="' + esc(uid) + '"' + (attrs || '') +
      ' style="--svc-clr:' + clr + ';--base-border:' + baseBorder + '"' +
      '>' +
      '<div class="rel-cover-wrap">' +
        (rel.cover
          ? '<img class="rel-cover" src="' + esc(rel.cover) + '"' +
            ' data-lightbox-src="' + esc(rel.cover) + '" data-lightbox' +
            ' style="cursor:zoom-in" loading="lazy" decoding="async"' +
            ' onerror="this.removeAttribute(\'src\')"/>'
          : '<div class="rel-cover-fallback">' + (isBbc ? '\uD83D\uDCFB' : '\u2601') + '</div>') +
        (liveStart ? '' :
          '<button data-act="' + playAct + '" ' + playData + ' ' + commonData +
          ' title="' + esc(t('btn.play')) + '" class="rel-play" aria-label="' + esc(t('btn.play')) + '">' +
          '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 6.5 L18 12 L9 17.5 Z"/></svg></button>') +
        '<div class="rel-badge-corner rel-badge-corner--tl"><span class="rel-badge rel-badge--svc">' + (isBbc ? 'BBC' : 'SOUNDCLOUD') + '</span></div>' +
        '<div class="rel-badge-corner rel-badge-corner--tr"><span class="rel-badge rel-badge--type">' + esc(badge) + '</span></div>' +
        (isNew ? '<div class="rel-badge-corner rel-badge-corner--bl"><span class="rel-badge--new">' + esc(t('rl.new_badge')) + '</span></div>' : '') +
        (dur ? '<div class="rel-badge-corner rel-badge-corner--br"><span class="rel-badge--dur">' + esc(dur) + '</span></div>' : '') +
      '</div>' +
      '<div class="rel-body">' +
        '<div class="rel-title" data-act="' + playAct + '" ' + playData + ' ' + commonData +
          ' title="' + esc(rel.title) + '">' + esc(rel.title) + '</div>' +
        '<div class="rel-artist' + (!isBbc && rel.artist_id ? ' rel-artist--clickable' : '') + '"' +
          ' title="' + esc(rel.artist) + '"' + artistClick + '>' +
          (isBbc ? '\uD83D\uDCFB ' : '') + esc(rel.artist) + '</div>' +
        '<div class="rel-meta">' + dt + '</div>' +
        dlBtn +
        '<div class="rel-act-row">' +
          (isBbc && !liveStart ? '<button data-act="bbc-cue" class="rel-act-btn"' +
            ' data-pid="' + esc(pid) + '" data-title="' + esc(rel.title) + '" data-artist="' + esc(rel.artist) + '"' +
            ' title="' + esc(t('b.dl_cue')) + '">CUE</button>' : '') +
          '<button data-act="toggle-fav" data-uid="' + esc(uid) + '"' +
            ' class="rel-act-btn' + (isFav ? ' rel-act-btn--fav-on' : '') + '"' +
            ' title="' + esc(isFav ? t('sc2.unfav') : t('sc2.fav')) + '">' + (isFav ? '\u2605' : '\u2606') + '</button>' +
          '<button data-act="copy-link" data-url="' + esc(rel.url) + '"' +
            ' class="rel-act-btn" title="' + esc(t('ck.copy_link')) + '">\u2398</button>' +
          (typeof nmaButton === 'function' ? nmaButton(rel) : '') +
          '<a href="' + esc(rel.url) + '" data-act="open-external" data-url="' + esc(rel.url) + '"' +
            ' class="rel-act-btn" title="' + esc(t('ck.open_on')) + ' ' + (isBbc ? 'BBC' : 'SoundCloud') + '">\u2197</a>' +
        '</div>' +
      '</div>' +
    '</div>';
  }

  /* ── Цвет сервиса ───────────────────────────────────────────────────────── */
  var _SVC_CLR = {spotify:'#1db954', qobuz:'#1870f5', tidal:'#00d4b3', apple:'var(--red)', deezer:'#a238ff',
                  bbc:'#ff4d4d', soundcloud:'#ff5500'};
  function _svcColor(svc) { return _SVC_CLR[svc] || 'var(--muted)'; }

  /* ── Экспорт ────────────────────────────────────────────────────────────── */
  window.RpReleaseCard = { render: render, renderMix: renderMix };
})();
