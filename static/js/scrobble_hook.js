/* Scrobble hook (093 п.2): тонкий мост между плеером и бэкендом ListenBrainz.
 * Отправляет POST /api/scrobble после ≥50% или ≥4 минут проигрывания трека.
 * Только владелец (гость не скробблит). Ошибка отправки не ломает плеер.
 * По умолчанию выключено — требуется токен в tokens/listenbrainz.token. */
(function () {
  'use strict';

  var _sent = {};          // {trackKey: true} — уже отправленные в эту сессию
  var _cur = null;         // {key, artist, title, album, durMs, playedMs}
  var _THRESHOLD_PCT = 50;
  var _THRESHOLD_SEC = 240; // 4 минуты

  function _owner() {
    return !document.body.classList.contains('guest-mode');
  }

  function _trackKey(item) {
    if (!item) return '';
    return (item.artist || '') + '\x00' + (item.title || '') + '\x00' + (item.url || item.id || '');
  }

  function _onTrackStart(e) {
    var item = (e && e.detail && e.detail.item) || null;
    if (!item || !item.artist || !item.title) { _cur = null; return; }
    var key = _trackKey(item);
    _cur = {
      key: key,
      artist: item.artist || '',
      title: item.title || '',
      album: item.album || '',
      durMs: Number(item.duration || item.dur || item.length || 0),
      playedMs: 0
    };
  }

  function _onTimeUpdate() {
    if (!_cur || _sent[_cur.key] || !_owner()) return;
    var audio = document.getElementById('pp-audio');
    if (!audio) return;
    var curSec = audio.currentTime || 0;
    var durSec = audio.duration || 0;
    _cur.playedMs = Math.max(_cur.playedMs, curSec * 1000);

    var pctPlayed = durSec > 0 ? (curSec / durSec * 100) : 0;
    var secPlayed = curSec;
    if (pctPlayed >= _THRESHOLD_PCT || secPlayed >= _THRESHOLD_SEC) {
      _send();
    }
  }

  function _send() {
    if (!_cur || _sent[_cur.key]) return;
    _sent[_cur.key] = true;
    var body = {
      artist: _cur.artist,
      title: _cur.title,
      album: _cur.album,
      duration_ms: Math.round(_cur.durMs || _cur.playedMs)
    };
    try {
      fetch('/api/scrobble', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
        keepalive: true
      }).catch(function () { /* ошибка не мешает плееру */ });
    } catch (_) { /* ignore */ }
  }

  document.addEventListener('ripster:track-start', _onTrackStart);

  // Подписываемся на timeupdate основного аудиоэлемента.
  // _setupAudioEvents в player.js вызывается один раз; мы вешаемся отдельно,
  // чтобы не трогать player.js (он и так огромный).
  function _attach() {
    var audio = document.getElementById('pp-audio');
    if (audio) audio.addEventListener('timeupdate', _onTimeUpdate);
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', _attach);
  } else {
    _attach();
  }

  // При уходе со страницы — отправить то, что уже на пороге.
  window.addEventListener('pagehide', function () {
    if (_cur && !_sent[_cur.key] && _owner()) {
      var pct = _cur.durMs > 0 ? (_cur.playedMs / _cur.durMs * 100) : 0;
      if (pct >= _THRESHOLD_PCT || _cur.playedMs >= _THRESHOLD_SEC * 1000) _send();
    }
  });
})();

// Глобальная функция для сохранения токена из настроек.
// Вызывается из oninput поля s-lb-token.
function _saveLbToken(value) {
  if (document.body.classList.contains('guest-mode')) return;
  try {
    fetch('/api/scrobble/token', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token: value || '' })
    }).catch(function () { /* ignore */ });
  } catch (_) { /* ignore */ }
}
