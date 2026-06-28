// Ripster player — Preview, Web Audio (gapless + EQ + visualizer),
// fullscreen player, floating PiP, seek-bar drag.
// Loaded AFTER app.js — relies on its globals (esc, toast, t, S, _svcColor, etc).
fetch('/api/sc_fps_log', { method: 'POST', headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ msg: '[player.js] v8 init', ua: navigator.userAgent.slice(0, 150) }) }).catch(() => {});

const Preview = { queue: [], idx: -1, _bound: false, mode: 'spotify' };
// Pre-resolved next-track state for near-gapless transitions.
// _Pre.idx is the queue index whose URL is ready in _Pre.url.
const _Pre = { idx: -1, url: '', resolving: false };

// ── Web Audio gapless engine (optional, sample-accurate) ──────────────────
// When `player-gapless` setting is on, playback bypasses <audio> and uses
// pre-decoded AudioBuffers fed into a BufferSource. Adjacent tracks share
// the same destination — no element swap, no fetch latency at handoff,
// sample-accurate boundary like a glued FLAC.
// Cost: each buffer is ~5-15 MB decoded; we keep at most current+next.
const _WA = {
  ctx: null,        // AudioContext
  gain: null,       // GainNode → destination (volume control)
  curSource: null,  // currently playing BufferSource
  curBuffer: null,  // its AudioBuffer
  curItem:   null,  // queue item it belongs to
  curStartT: 0,     // AC.currentTime when source.start() was called
  curOffset: 0,     // playback offset (for seek+resume)
  nextBuffer: null,
  nextItem:   null,
  suspendedAt: 0,   // ctx.currentTime at pause
  loading: false,
  _preloading: false, // guard against concurrent preload fetches
};
function _waEnabled() {
  // Mobile: force the plain <audio> path (no Web Audio gapless). The WA
  // buffer-source graph runs on an AudioContext that the OS suspends in the
  // background, killing playback on a locked screen. Bare <audio> survives.
  if (_isMobileDevice()) return false;
  return !!(S.config && S.config['player-gapless']);
}

// ── iOS background-audio keepalive ────────────────────────────────────────────
// iOS Safari suspends AudioContext when the app goes to background. A silent
// <audio> loop keeps the audio session alive so AudioContext.resume() works
// immediately when the user returns. Must be started from a user gesture.
let _waKeepalive = null;
function _waKeepaliveUrl() {
  if (_waKeepaliveUrl._url) return _waKeepaliveUrl._url;
  const sr = 8000, n = sr; // 1s silent mono 16-bit WAV
  const buf = new Uint8Array(44 + n * 2);
  const v = new DataView(buf.buffer);
  buf.set([0x52,0x49,0x46,0x46],  0); v.setUint32( 4, 36+n*2, true);
  buf.set([0x57,0x41,0x56,0x45],  8);
  buf.set([0x66,0x6d,0x74,0x20], 12); v.setUint32(16, 16,     true);
  v.setUint16(20,1,true); v.setUint16(22,1,true);
  v.setUint32(24, sr, true); v.setUint32(28, sr*2, true);
  v.setUint16(32,2,true); v.setUint16(34,16,true);
  buf.set([0x64,0x61,0x74,0x61], 36); v.setUint32(40, n*2, true);
  _waKeepaliveUrl._url = URL.createObjectURL(new Blob([buf], {type:'audio/wav'}));
  return _waKeepaliveUrl._url;
}
function _waStartKeepalive() {
  if (_waKeepalive) return;
  const a = new Audio(_waKeepaliveUrl());
  a.loop = true; a.volume = 0;
  a.play().catch(() => {});
  _waKeepalive = a;
}
function _waStopKeepalive() {
  if (!_waKeepalive) return;
  _waKeepalive.pause();
  _waKeepalive = null;
}

// ── <audio> unlock on first gesture (SC first-tap fix) ────────────────────────
// On touch devices there's no hover, so the SC stream cache isn't pre-warmed →
// the FIRST tap resolves the stream URL via `await fetch`, and by the time
// `audio.play()` runs the user-gesture activation has expired → the play is
// rejected and the first tap silently fails (only the second — a synchronous
// cache hit — works). Priming pp-audio with 1s of real silence on the very first
// user gesture "blesses" the element so every later programmatic play() — even
// after an async resolve — is allowed. Plain <audio> only; never AudioContext.
let _audioUnlocked = false;
function _installAudioUnlock() {
  const unlock = () => {
    if (_audioUnlocked) return;
    _audioUnlocked = true;
    try {
      const a = document.getElementById('pp-audio');
      if (a && (!a.src || a.paused)) {        // never interrupt active playback
        a.src = _waKeepaliveUrl();             // silent 1s WAV — inaudible
        const p = a.play();
        if (p && p.catch) p.catch(() => {});
      }
    } catch (_) {}
  };
  ['pointerdown', 'touchstart', 'mousedown', 'keydown'].forEach(ev =>
    document.addEventListener(ev, unlock, { capture: true, once: true }));
}
_installAudioUnlock();

// Resume playback when user returns from background (iOS suspends AudioContext).
let _bgWasPlaying = false;
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'hidden') {
    _bgWasPlaying = (_waEnabled() && _WA.curSource && !_waIsPaused?.()) ||
      (() => { const a = document.getElementById('pp-audio'); return !!(a && a.src && !a.paused); })();
    return;
  }
  if (!_bgWasPlaying) return;
  _bgWasPlaying = false;
  // Always resume AudioContext — <audio> is wired through it for EQ even when gapless is off.
  // Without this, currentTime advances but no sound reaches the speaker (suspended destination).
  if (_WA.ctx?.state === 'suspended') {
    _WA.ctx.resume().then(() => {
      if ('mediaSession' in navigator) navigator.mediaSession.playbackState = 'playing';
    }).catch(() => {});
  }
  if (_waEnabled() && _WA.curSource) {
    // gapless WA path: ctx.resume() above is sufficient
  } else {
    const a = document.getElementById('pp-audio');
    if (a && a.paused && a.src) a.play().catch(() => {});
  }
});
// Web Audio API needs CORS-readable audio bytes. SC/Qobuz/Tidal CDNs don't send
// Access-Control-Allow-Origin, so decodeAudioData fails for cross-origin URLs.
// We can still use WA for SAME-ORIGIN streams (our /api/stream/deezer proxy,
// /api/library/file). For others — silently fall back to the normal <audio>
// path, which doesn't need CORS.
// ── SoundCloud DRM (encrypted HLS) ───────────────────────────────────────────
// SC uses two encrypted-HLS variants:
//   drm-hls-ctr  — CENC/Widevine.  Works in Chrome, Edge, Firefox.
//                  HLS.js with emeEnabled + Widevine CDM.
//   drm-hls-cbc  — CBCS/FairPlay.  Works in Safari only.
//                  Uses native <audio> src — Safari handles FairPlay transparently.
function _scDrmSkip(reason) {
  _FP._drmHits = (_FP._drmHits || 0) + 1;
  const cur = Preview.idx;
  if (cur >= 0 && cur < Preview.queue.length - 1) {
    Preview.idx = cur + 1;
    _playPreviewAt(cur + 1);
  } else {
    const n = _FP._drmHits; _FP._drmHits = 0;
    toast(`SC: ${n} тр. пропущено — ${reason}. Попробуй «⬇ Скачать».`, 'var(--orange)', '', 7000);
  }
}

async function _scDrmHls(audioEl, item, playBtn, playBtnB) {
  const HLS_JS_CDN = 'https://cdn.jsdelivr.net/npm/hls.js@1.5.15/dist/hls.min.js';
  const licToken   = item.license_token || '';

  // ── CBC = FairPlay (Safari / iOS only) ───────────────────────────────────
  if (item.format === 'drm-hls-cbc') {
    // FairPlay: Safari's <audio> does NOT support EME/FPS — must use a hidden <video> element.
    // We create one persistent pp-fps-video, route FPS through it, and mirror UI events manually.
    const _fpsLog = (msg) => {
      console.log(msg);
      fetch('/api/sc_fps_log', { method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ msg, ua: navigator.userAgent.slice(0, 200) }),
      }).catch(() => {});
    };

    // Get or create hidden <video> — FairPlay only works on <video> in Safari/iOS.
    let fpsEl = document.getElementById('pp-fps-video');
    if (!fpsEl) {
      fpsEl = document.createElement('video');
      fpsEl.id = 'pp-fps-video';
      fpsEl.setAttribute('playsinline', '');
      fpsEl.setAttribute('webkit-playsinline', '');
      Object.assign(fpsEl.style, {
        position: 'fixed', top: '-9999px', left: '-9999px',
        width: '1px', height: '1px', opacity: '0', pointerEvents: 'none',
      });
      document.body.appendChild(fpsEl);
    }
    // Reset state from any previous FPS track.
    fpsEl.pause();
    try { await fpsEl.setMediaKeys(null); } catch (_) {}
    fpsEl.removeAttribute('src'); fpsEl.load();
    // Silence real audio element so it doesn't interfere.
    audioEl.pause(); audioEl.src = '';

    // Expose for previewToggle / previewSeek / previewVolume.
    Preview._fpsEl = fpsEl;

    // ── CDM availability check ────────────────────────────────────────────
    // iOS always fires webkitneedkey (native HLS), never encrypted — don't set up both paths.
    const hasWebKitKeys = 'WebKitMediaKeys' in window;
    let hasStdFps = false;
    if (!hasWebKitKeys) {
      try {
        await navigator.requestMediaKeySystemAccess('com.apple.fps', [{
          initDataTypes: ['skd'], videoCapabilities: [{ contentType: 'video/mp4' }],
        }]);
        hasStdFps = true;
      } catch (_) {}
    }

    if (!hasWebKitKeys && !hasStdFps) {
      Preview._fpsEl = null;
      _scDrmSkip('FairPlay DRM — трек только для Safari');
      return;
    }
    _fpsLog(`[FPS] v8 video elm, wk=${hasWebKitKeys} std=${hasStdFps}`);

    // ── Fetch server certificate ──────────────────────────────────────────
    let serverCert;
    try {
      const certResp = await fetch('/api/sc_fps_cert');
      serverCert = new Uint8Array(await certResp.arrayBuffer());
      _fpsLog(`[FPS] cert ${serverCert.length}B`);
    } catch (e) {
      _fpsLog('[FPS] cert fail: ' + e.message);
      Preview._fpsEl = null; _scDrmSkip('FairPlay: сертификат недоступен'); return;
    }

    const fpsLicUrl = `https://license.media-streaming.soundcloud.cloud/playback/fairplay?license_token=${encodeURIComponent(licToken)}`;
    let _keyInstalled = false;
    let _fpsPending = false;
    const _doLicense = async (spc) => {
      const r = await fetch(fpsLicUrl, {
        method: 'POST', body: spc,
        headers: { 'Content-Type': 'application/octet-stream' },
      });
      if (!r.ok) throw new Error(`lic ${r.status}`);
      return new Uint8Array(await r.arrayBuffer());
    };

    // ── Standard EME on <video> (Safari 12.1+ / iOS 12.2+) ───────────────
    if (hasStdFps) {
      let mediaKeys = null;
      try {
        const acc = await navigator.requestMediaKeySystemAccess('com.apple.fps', [{
          initDataTypes: ['skd'], videoCapabilities: [{ contentType: 'video/mp4' }],
        }]);
        mediaKeys = await acc.createMediaKeys();
        await mediaKeys.setServerCertificate(serverCert);
        await fpsEl.setMediaKeys(mediaKeys);
        _fpsLog('[FPS] EME ok');
      } catch (e) {
        _fpsLog('[FPS] EME err: ' + e.message); mediaKeys = null;
      }
      if (mediaKeys) {
        fpsEl.addEventListener('encrypted', async (e) => {
          _fpsLog('[FPS] encrypted: ' + e.initDataType);
          if (_keyInstalled || _fpsPending) return;
          _fpsPending = true;
          try {
            const sess = mediaKeys.createSession();
            sess.addEventListener('message', async (msg) => {
              try {
                _fpsLog('[FPS] SPC → CKC...');
                const ckc = await _doLicense(msg.message);
                await sess.update(ckc);
                _keyInstalled = true;
                _fpsLog('[FPS] key OK');
              } catch (err) { _fpsPending = false; _fpsLog('[FPS] lic: ' + err.message); }
            });
            await sess.generateRequest(e.initDataType, e.initData);
          } catch (err) { _fpsPending = false; _fpsLog('[FPS] sess: ' + err.message); }
        }, { once: false });
      }
    }

    // ── Legacy WebKit FPS on <video> (iOS < 12.2) ─────────────────────────
    if (hasWebKitKeys) {
      try {
        const wkKeys = new WebKitMediaKeys('com.apple.fps.1_0');
        fpsEl.webkitSetMediaKeys(wkKeys);
        _fpsLog('[FPS] WK set');
        fpsEl.addEventListener('webkitneedkey', async (e) => {
          if (_keyInstalled || _fpsPending) return;
          _fpsPending = true;
          _fpsLog('[FPS] webkitneedkey');
          try {
            const sess = wkKeys.createSession('video/mp4');
            sess.addEventListener('webkitkeymessage', async (msg) => {
              try {
                _fpsLog('[FPS] WK SPC → CKC...');
                const ckc = await _doLicense(msg.message);
                sess.update(ckc);
                _keyInstalled = true;
                _fpsLog('[FPS] WK key OK');
              } catch (err) { _fpsPending = false; _fpsLog('[FPS] WK lic: ' + err.message); }
            });
            sess.addEventListener('webkitkeyerror', (ev) => {
              _fpsPending = false;
              _fpsLog(`[FPS] WK keyerr ${ev.errorCode?.code || '?'}`);
            });
            // Apple FPS spec: [4B BE keyId len][keyId][4B BE cert len][cert]
            // keyId = content ID extracted from skd:// URI (strip the scheme prefix)
            const uri = new TextDecoder('utf-8').decode(new Uint8Array(e.initData));
            const keyId = uri.startsWith('skd://') ? uri.slice(6) : uri;
            const keyIdBytes = new TextEncoder().encode(keyId);
            const payload = new Uint8Array(4 + keyIdBytes.length + 4 + serverCert.length);
            const dv = new DataView(payload.buffer);
            dv.setUint32(0, keyIdBytes.length, false);
            payload.set(keyIdBytes, 4);
            dv.setUint32(4 + keyIdBytes.length, serverCert.length, false);
            payload.set(serverCert, 4 + keyIdBytes.length + 4);
            sess.generateKeyRequest('video/mp4', payload);
            _fpsLog(`[FPS] WK req sent, keyId=${keyId.slice(0, 32)}`);
          } catch (err) { _fpsPending = false; _fpsLog('[FPS] WK req: ' + err.message); }
        }, { once: false });
      } catch (e) { _fpsLog('[FPS] WK init: ' + e.message); }
    }

    // ── Mirror <video> events → player UI ────────────────────────────────
    fpsEl.addEventListener('timeupdate', () => {
      const cur = fpsEl.currentTime, dur = fpsEl.duration;
      if (!dur || !isFinite(dur)) return;
      if (_seekDragging) return;
      const pct = (cur / dur * 100) + '%';
      const t = fmtDur(Math.floor(cur));
      ['pp-fill','pp-fill-big','fp-fill'].forEach(id => { const e = document.getElementById(id); if(e) e.style.width = pct; });
      ['pp-cur','pp-cur-big','fp-cur'].forEach(id => { const e = document.getElementById(id); if(e) e.textContent = t; });
      // Thumb uses --x-pct on transform (GPU, no layout). Older code set
      // `style.left` directly which forced a reflow every timeupdate tick.
      const thumb = document.getElementById('fp-thumb'); if (thumb) thumb.style.setProperty('--x-pct', pct);
      try { _mixPosSave?.(item.posKey, cur, dur); } catch {}
      try { _lrcSyncTick?.(cur); } catch {}
      if ('mediaSession' in navigator && dur > 0) {
        try { navigator.mediaSession.setPositionState({duration: dur, playbackRate: 1, position: Math.min(cur, dur)}); } catch {}
      }
    });
    fpsEl.addEventListener('durationchange', () => {
      if (!fpsEl.duration || !isFinite(fpsEl.duration)) return;
      const d = fmtDur(Math.floor(fpsEl.duration));
      ['pp-dur','pp-dur-big','fp-dur'].forEach(id => { const e = document.getElementById(id); if(e) e.textContent = d; });
    });
    fpsEl.addEventListener('play', () => {
      ['pp-play','pp-play-big','fp-play'].forEach(id => { const e = document.getElementById(id); if(e) e.textContent = '⏸'; });
      if ('mediaSession' in navigator) navigator.mediaSession.playbackState = 'playing';
    });
    fpsEl.addEventListener('pause', () => {
      ['pp-play','pp-play-big','fp-play'].forEach(id => { const e = document.getElementById(id); if(e) e.textContent = '▶'; });
      if ('mediaSession' in navigator) navigator.mediaSession.playbackState = 'paused';
    });
    fpsEl.addEventListener('ended', () => {
      Preview._fpsEl = null;
      if (!Preview._suppressEnded) {
        if (Preview.idx >= 0 && Preview.idx < Preview.queue.length - 1) previewNext();
        else closePreview?.();
      }
    }, { once: true });
    fpsEl.addEventListener('error', () => {
      const c = fpsEl.error?.code, m = fpsEl.error?.message || '';
      _fpsLog(`[FPS] video err ${c}: ${m}`);
    }, { once: true });

    // Diagnostic: no key event within 6 s
    const _diagT = setTimeout(() => {
      if (_keyInstalled) return;
      _fpsLog(`[FPS] no event 6s rs=${fpsEl.readyState} err=${fpsEl.error?.code || 0}`);
    }, 6000);
    fpsEl.addEventListener('playing', () => clearTimeout(_diagT), { once: true });

    fpsEl.volume = audioEl.volume || 1;
    fpsEl.src = item.url;
    fpsEl.load();
    if (_WA.ctx && _WA.ctx.state === 'suspended') _WA.ctx.resume().catch(() => {});
    try {
      await fpsEl.play();
    } catch (e) {
      _fpsLog('[FPS] play blocked: ' + e.message);
      if (playBtn)  playBtn.textContent  = '▶';
      if (playBtnB) playBtnB.textContent = '▶';
    }
    return;
  }

  // ── CTR = CENC/Widevine (Chrome, Edge, Firefox) ───────────────────────────
  // Widevine pre-flight — fail fast before loading HLS.js.
  try {
    await navigator.requestMediaKeySystemAccess('com.widevine.alpha', [{
      initDataTypes: ['cenc'],
      audioCapabilities: [{ contentType: 'audio/mp4; codecs="mp4a.40.2"',
                            robustness: 'SW_SECURE_CRYPTO' }],
    }]);
  } catch (_) {
    _scDrmSkip('DRM (Widevine CDM недоступен — попробуй Chrome)');
    return;
  }

  // ── Load HLS.js ──────────────────────────────────────────────────────────
  if (!window.Hls) {
    await new Promise((res, rej) => {
      const s = document.createElement('script');
      s.src = HLS_JS_CDN; s.onload = res; s.onerror = rej;
      document.head.appendChild(s);
    }).catch(() => {});
  }
  if (!window.Hls || !Hls.isSupported()) {
    _scDrmSkip('DRM (браузер не поддерживает MSE/HLS.js)');
    return;
  }

  const licenseProxyUrl = `/api/sc_license?token=${encodeURIComponent(licToken)}`;
  const hlsCfg = {
    emeEnabled: true,
    drmSystems: { 'com.widevine.alpha': { licenseUrl: licenseProxyUrl } },
    drmSystemOptions: { 'com.widevine.alpha': {
      audioRobustness: 'SW_SECURE_CRYPTO', videoRobustness: 'SW_SECURE_CRYPTO' } },
    licenseXhrSetup(xhr) {
      xhr.setRequestHeader('Content-Type', 'application/octet-stream');
    },
  };

  let hls;
  try {
    hls = new Hls(hlsCfg);
  } catch (e) {
    console.error('[SC DRM] HLS init failed:', e);
    _scDrmSkip('DRM (HLS.js ошибка инициализации)');
    return;
  }
  Preview._hls = hls;

  hls.on(Hls.Events.ERROR, (_, data) => {
    const details = (typeof data.details === 'string') ? data.details.toLowerCase() : '';
    const isKeyErr = details.includes('keysystem') || data.type === 'keySystemError';
    console.warn('[SC DRM CTR]', data.type, details, 'fatal:', data.fatal);

    if (!data.fatal && !isKeyErr) return;

    hls.destroy(); Preview._hls = null;
    if (item.id && typeof _scStreamCache !== 'undefined') {
      _scStreamCache.delete(String(item.id));
    }
    const reason = isKeyErr
      ? (details.includes('license') ? 'лиц. сервер SC — токен истёк' : 'DRM ключ недоступен')
      : data.type === 'networkError'
        ? `SC сеть: ${data.details||'?'} (CORS?)`
        : `DRM: ${data.details||data.type||'?'}`;
    _scDrmSkip(reason);
  });

  hls.on(Hls.Events.MANIFEST_PARSED, () => {
    if (_WA.ctx && _WA.ctx.state === 'suspended') _WA.ctx.resume().catch(() => {});
    const p = audioEl.play();
    if (p) p.catch(e => {
      console.warn('[SC DRM CTR] autoplay blocked:', e.message);
      if (playBtn)  playBtn.textContent  = '▶';
      if (playBtnB) playBtnB.textContent = '▶';
    });
  });

  try {
    hls.loadSource(item.url);
    hls.attachMedia(audioEl);
  } catch (e) {
    console.error('[SC DRM] loadSource/attachMedia failed:', e);
    hls.destroy(); Preview._hls = null;
    _scDrmSkip('DRM (HLS.js ошибка загрузки)');
  }
}

function _waCanPlay(item) {
  if (!item) return false;
  // SC streams are large (mixes 1-2h) — buffer-decode-before-play takes forever.
  // <audio> path via same-origin /api/proxy still gets EQ + visualizer.
  if (item.service === 'soundcloud') return false;
  if ((item.posKey || '').startsWith('soundcloud:')) return false;
  // Deezer too: the Web Audio buffer path must download AND decode the WHOLE
  // track before a single sample plays — many seconds of "hang" on long DJ-mix
  // tracks, and arbitrary track jumps re-decode from scratch. Stream it via
  // <audio> instead: instant start, Range-seek, gapless-enough auto-advance.
  if (item.service === 'deezer') return false;
  if (!item.url) return true;       // lazy resolve → will hit our same-origin /api/stream/<svc>
  try {
    const u = new URL(item.url, location.origin);
    return u.origin === location.origin;
  } catch { return false; }
}
async function _waInit() {
  if (_WA.ctx) {
    if (_WA.ctx.state === 'suspended') await _WA.ctx.resume();
    return;
  }
  const Ctx = window.AudioContext || window.webkitAudioContext;
  _WA.ctx = new Ctx();
  _WA.gain = _WA.ctx.createGain();
  _WA.gain.gain.value = (S.config?.['player-volume'] ?? 1);
  // 3-band EQ — gain → bass → mid → treble → destination. Same chain serves
  // both gapless (buffer source → gain) and standard <audio> (mediaElement
  // source → gain) playback.
  const bass = _WA.ctx.createBiquadFilter();
  bass.type = 'lowshelf';  bass.frequency.value = 320;
  const mid  = _WA.ctx.createBiquadFilter();
  mid.type  = 'peaking';   mid.frequency.value  = 1000;  mid.Q.value = 1;
  const treb = _WA.ctx.createBiquadFilter();
  treb.type = 'highshelf'; treb.frequency.value = 3200;
  bass.gain.value = parseFloat(S.config?.['player-eq-bass']   ?? 0);
  mid.gain.value  = parseFloat(S.config?.['player-eq-mid']    ?? 0);
  treb.gain.value = parseFloat(S.config?.['player-eq-treble'] ?? 0);
  _WA.eq = {bass, mid, treble: treb};
  // AnalyserNode for the fullscreen-player visualizer. Connected in parallel
  // to destination so it taps the post-EQ signal without re-routing audio.
  _WA.analyser = _WA.ctx.createAnalyser();
  _WA.analyser.fftSize = 128;            // 64 freq bins — plenty for bars
  _WA.analyser.smoothingTimeConstant = 0.75;
  _WA.gain.connect(bass).connect(mid).connect(treb);
  treb.connect(_WA.ctx.destination);
  treb.connect(_WA.analyser);            // tap, doesn't double the output
}

// Pipe the <audio> element through the WA EQ chain. Same-origin only (CORS):
// our /api/proxy + /api/stream/deezer + /api/library/file qualify.
function _isMobileDevice() {
  const ua = navigator.userAgent || '';
  return /Mobi|Android|iPhone|iPad|iPod/i.test(ua) ||
         (navigator.maxTouchPoints > 1 && /Macintosh/.test(ua));  // iPadOS reports as Mac
}

async function _wireAudioToEQ() {
  // MOBILE: never route <audio> through the Web Audio EQ graph. A
  // MediaElementSource pins the element's output to the AudioContext, which
  // iOS/Android SUSPEND when the page is backgrounded → the track goes silent on
  // the lock screen (currentTime keeps ticking but no sound). A bare <audio>
  // element keeps playing in the background under OS control + the Media Session
  // lock-screen controls. We trade the (rarely-used-on-mobile) EQ for working
  // background playback. Desktop keeps the EQ chain.
  if (_isMobileDevice()) return false;
  if (_WA._audioSourceNode) {
    // Already wired; make sure context is running (mobile may have suspended it)
    if (_WA.ctx?.state === 'suspended') _WA.ctx.resume().catch(() => {});
    return true;
  }
  await _waInit();
  const audio = document.getElementById('pp-audio');
  if (!audio) return false;
  try {
    _WA._audioSourceNode = _WA.ctx.createMediaElementSource(audio);
    _WA._audioSourceNode.connect(_WA.gain);
    // Mobile browsers (iOS Safari, Chrome Android) often start AudioContext suspended.
    // After wiring, audio plays (currentTime ticks) but is silent until context resumes.
    if (_WA.ctx.state === 'suspended') _WA.ctx.resume().catch(() => {});
    return true;
  } catch (e) {
    console.warn('[eq] cannot wire <audio> to WA (probably cross-origin):', e.message);
    return false;
  }
}

// Setter — called by Settings sliders. dB in [-12..+12].
function setEQ(band, db) {
  const v = Math.max(-12, Math.min(12, parseFloat(db) || 0));
  if (_WA.eq && _WA.eq[band]) _WA.eq[band].gain.value = v;
  saveSetting('player-eq-' + band, v);
  const lbl = document.getElementById('s-eq-' + band + '-val');
  if (lbl) lbl.textContent = v > 0 ? `+${v}` : String(v);
}
function resetEQ() {
  ['bass','mid','treble'].forEach(b => {
    setEQ(b, 0);
    const el = document.getElementById('s-eq-' + b); if (el) el.value = 0;
  });
}

// ── Visualiser (FFT frequency bars on fullscreen-player background) ─────
let _vizRAF = null;
function _vizStart() {
  if (!(S.config?.['player-viz']) || !_WA.analyser) return;
  const canvas = document.getElementById('fp-viz');
  if (!canvas) return;
  canvas.style.opacity = '0.55';
  const ctx2d = canvas.getContext('2d');
  const N = _WA.analyser.frequencyBinCount;
  const data = new Uint8Array(N);
  // Re-size canvas DPI-aware on each tick (cheap, handles rotation)
  const draw = () => {
    if (!_FP?.open) { _vizStop(); return; }
    const dpr = window.devicePixelRatio || 1;
    if (canvas.width !== canvas.clientWidth * dpr) {
      canvas.width  = canvas.clientWidth  * dpr;
      canvas.height = canvas.clientHeight * dpr;
    }
    _WA.analyser.getByteFrequencyData(data);
    const w = canvas.width, h = canvas.height;
    ctx2d.clearRect(0, 0, w, h);
    // Pick the dominant service-brand color from current track (fallback pink).
    const item  = Preview.queue[Preview.idx];
    const tint  = (typeof _svcColor === 'function' && item?.service)
                    ? _svcColor(item.service) : '#c084a0';
    const grad  = ctx2d.createLinearGradient(0, h, 0, 0);
    grad.addColorStop(0, tint);
    grad.addColorStop(1, tint + '00');
    ctx2d.fillStyle = grad;
    const bars = Math.min(N, 48);
    const step = Math.floor(N / bars);
    const bw   = w / bars;
    for (let i = 0; i < bars; i++) {
      const v = data[i * step] / 255;
      const bh = v * h;
      ctx2d.fillRect(i * bw + bw * 0.15, h - bh, bw * 0.7, bh);
    }
    _vizRAF = requestAnimationFrame(draw);
  };
  _vizRAF = requestAnimationFrame(draw);
}
function _vizStop() {
  if (_vizRAF) { cancelAnimationFrame(_vizRAF); _vizRAF = null; }
  const canvas = document.getElementById('fp-viz');
  if (canvas) {
    canvas.style.opacity = '0';
    canvas.getContext('2d')?.clearRect(0, 0, canvas.width, canvas.height);
  }
}
// Toggle (called by Settings):
function _vizConfigChanged() {
  if (S.config?.['player-viz'] && _FP?.open) _vizStart();
  else _vizStop();
}
async function _waLoadBuffer(url) {
  const r   = await fetch(url, {credentials: 'same-origin'});
  const ab  = await r.arrayBuffer();
  return await _WA.ctx.decodeAudioData(ab);
}
// DRM format preference for SoundCloud — detected once at startup.
// 'ctr' = Widevine (Chrome/Edge/Firefox), 'cbc' = FairPlay (Safari/iOS).
// Exposed as window._scDrmPrefer so sc.js can also read it.
window._scDrmPrefer = 'ctr';
(async () => {
  // iOS uses legacy WebKitMediaKeys for FairPlay — detect this synchronously first
  // before any async EME calls. Standard EME requestMediaKeySystemAccess with
  // audioCapabilities/com.apple.fps (no .1_0) fails on iOS even though FPS works.
  if ('WebKitMediaKeys' in window) {
    window._scDrmPrefer = 'cbc';
    fetch('/api/sc_fps_log', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ msg: '[DRM] iOS WebKitMediaKeys → prefer=cbc', ua: navigator.userAgent.slice(0, 150) }) }).catch(() => {});
    return;
  }
  try {
    await navigator.requestMediaKeySystemAccess('com.widevine.alpha', [{
      initDataTypes: ['cenc'],
      audioCapabilities: [{ contentType: 'audio/mp4; codecs="mp4a.40.2"' }],
    }]);
    window._scDrmPrefer = 'ctr';
  } catch (_) {
    const hasFps = await navigator.requestMediaKeySystemAccess(
      'com.apple.fps.1_0', [{ initDataTypes: ['skd'],
        videoCapabilities: [{ contentType: 'video/mp4' }] }])
      .then(() => true).catch(() => false);
    window._scDrmPrefer = hasFps ? 'cbc' : 'ctr';
  }
  console.log('[DRM] prefer:', window._scDrmPrefer);
})();

// Returns per-service quality query param based on player-stream-quality setting.
function _streamQp(service) {
  const q = S.config?.['player-stream-quality'] || 'mp3';
  if (service === 'deezer') {
    return q === 'mp3' ? 'quality=3' : 'quality=9';           // 320 or FLAC
  }
  if (service === 'qobuz') {
    const fid = q === 'hires' ? 27 : q === 'lossless' ? 6 : 5;
    return `format_id=${fid}`;                                 // HiRes/FLAC/MP3-320
  }
  if (service === 'tidal') {
    const tq = q === 'hires' ? 'HI_RES' : q === 'lossless' ? 'LOSSLESS' : 'HIGH';
    return `quality=${tq}`;
  }
  if (service === 'soundcloud') {
    return `prefer=${window._scDrmPrefer || 'ctr'}`;
  }
  return '';
}

// ── In-player quality switcher ────────────────────────────────────────────
// The global `player-stream-quality` setting (mp3 | lossless | hires) drives
// _streamQp(); the pill in the bar exposes only the tiers that make sense for
// the current track's service. Services with a fixed source quality (SC /
// Spotify / Apple) show a static, non-interactive label.
function _qualityOptionsFor(service) {
  switch (service) {
    case 'deezer': return [{v:'mp3',l:'MP3',s:'320 kbps'}, {v:'lossless',l:'FLAC',s:'16/44'}];
    case 'qobuz':  return [{v:'mp3',l:'MP3',s:'320 kbps'}, {v:'lossless',l:'FLAC',s:'16/44'}, {v:'hires',l:'Hi-Res',s:'24-bit'}];
    case 'tidal':  return [{v:'mp3',l:'High',s:'AAC'}, {v:'lossless',l:'FLAC',s:'16/44'}, {v:'hires',l:'Hi-Res',s:'24-bit'}];
    default:       return [];  // soundcloud / spotify / apple — fixed source quality
  }
}
function _qualityShortLabel(service, q) {
  if (service === 'soundcloud') return 'AAC';
  const hit = _qualityOptionsFor(service).find(o => o.v === q);
  return hit ? hit.l : (q === 'lossless' ? 'FLAC' : q === 'hires' ? 'Hi-Res' : 'MP3');
}
function _curPlayerItem() {
  try { return Preview?.queue?.[Preview.idx] || null; } catch { return null; }
}
function _updateQualityPill(item) {
  const btn = document.getElementById('pp-quality-btn');
  const lab = document.getElementById('pp-quality-label');
  if (!btn || !lab) return;
  const svc = item?.service || '';
  const q = S.config?.['player-stream-quality'] || 'mp3';
  lab.textContent = _qualityShortLabel(svc, q);
  const fixed = _qualityOptionsFor(svc).length === 0;
  btn.style.opacity = fixed ? '.55' : '1';
  btn.style.pointerEvents = fixed ? 'none' : 'auto';
  btn.title = fixed
    ? (svc === 'soundcloud' ? 'SoundCloud: качество источника фиксировано' : 'Качество фиксировано для этого источника')
    : 'Качество стрима';
}
function _closeQualityMenu() {
  const m = document.getElementById('pp-quality-menu');
  if (m) m.style.display = 'none';
}
function toggleQualityMenu(event) {
  if (event) event.stopPropagation();
  const m = document.getElementById('pp-quality-menu');
  if (!m) return;
  if (m.style.display === 'block') { m.style.display = 'none'; return; }
  const svc = (_curPlayerItem()?.service) || '';
  const opts = _qualityOptionsFor(svc);
  if (!opts.length) return;
  const cur = S.config?.['player-stream-quality'] || 'mp3';
  m.innerHTML = opts.map(o =>
    `<div class="pp-q-opt${o.v===cur?' active':''}" onclick="setStreamQuality('${o.v}')">` +
    `<span>${o.l}</span><span class="pp-q-sub">${o.s}</span></div>`
  ).join('');
  m.style.display = 'block';
}
async function setStreamQuality(q) {
  _closeQualityMenu();
  try {
    if (typeof saveSetting === 'function') saveSetting('player-stream-quality', q);
    else if (S.config) S.config['player-stream-quality'] = q;
  } catch (_) {}
  const item = _curPlayerItem();
  _updateQualityPill(item);
  if (!item) return;
  // Re-resolve the current track at the new quality, resuming from the current position.
  try {
    const audio = document.getElementById('pp-audio');
    const onWA = (typeof _waEnabled === 'function' && _waEnabled() && _WA?.curSource);
    const pos  = onWA ? _waCurrentTime() : (audio?.currentTime || 0);
    item.url = '';  // force re-fetch with the new quality param
    if (onWA && typeof _waCanPlay === 'function' && _waCanPlay(item)) {
      await _waPlay(Preview.idx, pos);
    } else if (audio) {
      const seekOnce = () => {
        try { if (pos > 0) audio.currentTime = pos; } catch (_) {}
        audio.removeEventListener('loadedmetadata', seekOnce);
      };
      audio.addEventListener('loadedmetadata', seekOnce);
      await _playPreviewAt(Preview.idx);
    }
    if (typeof toast === 'function') toast(`Качество: ${_qualityShortLabel(item.service, q)}`, 'var(--green)', '', 2500);
  } catch (e) {
    console.warn('[quality] switch failed:', e?.message);
    if (typeof toast === 'function') toast('Не удалось переключить качество', 'var(--red)');
  }
}
// Close the quality menu on any outside click.
document.addEventListener('click', (e) => {
  const wrap = document.getElementById('pp-quality');
  const m = document.getElementById('pp-quality-menu');
  if (m && m.style.display === 'block' && wrap && !wrap.contains(e.target)) m.style.display = 'none';
});

// Returns {url, status} so caller can distinguish 451 DRM from other failures.
async function _waResolveUrl(item) {
  if (item.url) return {url: item.url, status: 200};
  if (!item.service || !item.id) return {url: null, status: 0};
  const qp = _streamQp(item.service);
  // Deezer streams raw audio bytes from our same-origin proxy and the URL is
  // deterministic, so build it directly — DON'T pre-fetch. The old code fetched
  // the whole (decrypted) track here just to read resp.url, then _waLoadBuffer
  // downloaded it a SECOND time to decode: double the server work + a multi-second
  // stall on track switch/seek for long mixes.
  if (item.service === 'deezer') {
    item.url = `/api/stream/deezer/${item.id}` +
      `?name=${encodeURIComponent(item.title || '')}` +
      `&artist=${encodeURIComponent(item.artist || '')}` +
      (qp ? `&${qp}` : '');
    return {url: item.url, status: 200};
  }
  const resp = await fetch(
    `/api/stream/${item.service}/${item.id}` +
    `?name=${encodeURIComponent(item.title || '')}` +
    `&artist=${encodeURIComponent(item.artist || '')}` +
    (qp ? `&${qp}` : ''));
  if (!resp.ok) return {url: null, status: resp.status};
  const ct = (resp.headers.get('content-type') || '').toLowerCase();
  if (ct.includes('application/json')) {
    const j = await resp.json();
    item.url = j?.url || '';
    if (j?.artwork && !item.cover) item.cover = j.artwork;
    return {url: item.url, status: 200};
  }
  item.url = resp.url;
  return {url: item.url, status: 200};
}
async function _waPlay(idx, startAtSec = 0) {
  await _waInit();
  const item = Preview.queue[idx];
  if (!item) return false;
  _WA.loading = true;
  let buffer = null;
  // Reuse pre-decoded next buffer if available (seamless gapless auto-advance).
  if (_WA.nextItem === item && _WA.nextBuffer) {
    buffer = _WA.nextBuffer;
    _WA.nextItem = null;
    _WA.nextBuffer = null;
  } else {
    // Explicit switch/seek to a track we DON'T have preloaded → stop the current
    // source NOW, so the user gets instant feedback instead of the old track
    // playing on through the network load+decode of the new one.
    if (_WA.curSource) { try { _WA.curSource.onended = null; _WA.curSource.stop(0); } catch {} _WA.curSource = null; }
    const r = await _waResolveUrl(item);
    if (!r.url) {
      _WA.loading = false;
      // 451 = DRM-only (paid SC release w/o Go+). 404/451 → silently skip to
      // next track; show ONE summary toast when we exhaust the whole queue.
      if (r.status === 451 || r.status === 404) {
        _WA._drmHits = (_WA._drmHits || 0) + 1;
        if (idx + 1 < Preview.queue.length) return _waPlay(idx + 1, 0);
        const n = _WA._drmHits;
        _WA._drmHits = 0;
        if (item.service === 'soundcloud') {
          toast(`SC: ${n} тр. пропущено — платный контент (нет Go+). Попробуй «⬇ Скачать».`, 'var(--orange)', '', 6000);
        } else {
          toast(`Треков с DRM пропущено: ${n}. «⬇ Скачать» обходит это.`, 'var(--orange)', '', 6000);
        }
        return false;
      }
      // Other failures — generic toast
      toast(`Стрим ${item.service}: ${r.status || 'нет URL'}`, 'var(--red)');
      if (idx + 1 < Preview.queue.length) return _waPlay(idx + 1, 0);
      return false;
    }
    try { buffer = await _waLoadBuffer(r.url); }
    catch (e) {
      console.warn('[gapless] decode failed:', e.message);
      _WA.loading = false;
      // Advance on decode failure too
      if (idx + 1 < Preview.queue.length) return _waPlay(idx + 1, 0);
      return false;
    }
  }
  // Stop any in-flight source.
  if (_WA.curSource) { try { _WA.curSource.onended = null; _WA.curSource.stop(0); } catch {} }
  const src = _WA.ctx.createBufferSource();
  src.buffer = buffer;
  src.playbackRate.value = (S.config?.['player-speed'] ?? 1);
  src.connect(_WA.gain);
  const startedAt = _WA.ctx.currentTime + 0.02;     // small headroom
  src.start(startedAt, startAtSec);
  _WA.curSource = src;
  _WA.curBuffer = buffer;
  _WA.curItem   = item;
  _WA.curStartT = startedAt - startAtSec;            // virtual t=0
  _WA.curOffset = startAtSec;
  // Update duration display immediately (WA has no durationchange event).
  const _waDurStr = fmtDur(Math.floor(buffer.duration));
  ['pp-dur','pp-dur-big','fp-dur'].forEach(id => { const el = document.getElementById(id); if(el) el.textContent = _waDurStr; });
  _WA.loading   = false;
  Preview.idx = idx;
  // Pre-decode the next track in background.
  _waPreloadNext();
  // Hook ended → advance.
  src.onended = () => {
    // Browsers fire onended also on .stop() — ignore if we replaced manually.
    if (_WA.curSource !== src) return;
    if (idx + 1 < Preview.queue.length) {
      _waPlay(idx + 1, 0);
    } else {
      _WA.curSource = null;
    }
  };
  // Mirror to UI (title/cover/play-state) so the visible plumbing stays in sync.
  if (typeof fpSyncMeta === 'function') fpSyncMeta(item);
  ['pp-play','pp-play-big','fp-play'].forEach(id => {
    const el = document.getElementById(id); if (el) el.textContent = '⏸';
  });
  if ('mediaSession' in navigator) navigator.mediaSession.playbackState = 'playing';
  _waStartKeepalive();
  try { _syncAlbumPlayBtns?.(); } catch {}
  return true;
}
async function _waPreloadNext() {
  if (_WA.nextBuffer || _WA._preloading) return;
  const nextIdx = Preview.idx + 1;
  if (nextIdx >= Preview.queue.length) return;
  const item = Preview.queue[nextIdx];
  if (!item) return;
  _WA._preloading = true;
  try {
    const r = await _waResolveUrl(item);
    if (!r?.url) return;
    _WA.nextBuffer = await _waLoadBuffer(r.url);
    _WA.nextItem = item;
    console.log('[gapless] preloaded next:', item.title);
  } catch (e) {
    console.warn('[gapless] preload failed:', e.message);
  } finally {
    _WA._preloading = false;
  }
}
function _waPause() {
  if (_WA.ctx && _WA.curSource) {
    _WA.suspendedAt = _WA.ctx.currentTime - _WA.curStartT;
    _WA.ctx.suspend();
    ['pp-play','pp-play-big','fp-play'].forEach(id => {
      const el = document.getElementById(id); if (el) el.textContent = '▶';
    });
    if ('mediaSession' in navigator) navigator.mediaSession.playbackState = 'paused';
    _waStopKeepalive();
    try { _syncAlbumPlayBtns?.(); } catch {}
  }
}
async function _waResume() {
  if (_WA.ctx) {
    await _WA.ctx.resume();
    ['pp-play','pp-play-big','fp-play'].forEach(id => {
      const el = document.getElementById(id); if (el) el.textContent = '⏸';
    });
    if ('mediaSession' in navigator) navigator.mediaSession.playbackState = 'playing';
    _waStartKeepalive();
    try { _syncAlbumPlayBtns?.(); } catch {}
  }
}
function _waIsPaused() {
  return _WA.ctx?.state === 'suspended';
}
function _waCurrentTime() {
  if (!_WA.ctx || !_WA.curSource) return 0;
  if (_waIsPaused()) return _WA.suspendedAt;
  return _WA.ctx.currentTime - _WA.curStartT;
}
function _waDuration() {
  return _WA.curBuffer?.duration || 0;
}
async function _waSeek(sec) {
  // Seek WITHIN the current track by restarting a BufferSource from the already
  // decoded buffer — no re-fetch/re-decode (the old code called _waPlay, which
  // re-downloaded the whole track on every seek → seek felt dead on long mixes).
  if (!_WA.ctx || !_WA.curBuffer) return;
  const off = Math.max(0, Math.min(sec, _WA.curBuffer.duration - 0.2));
  if (_WA.curSource) { try { _WA.curSource.onended = null; _WA.curSource.stop(0); } catch {} }
  if (_WA.ctx.state === 'suspended') { try { await _WA.ctx.resume(); } catch {} }
  const src = _WA.ctx.createBufferSource();
  src.buffer = _WA.curBuffer;
  src.playbackRate.value = (S.config?.['player-speed'] ?? 1);
  src.connect(_WA.gain);
  const startedAt = _WA.ctx.currentTime + 0.01;
  src.start(startedAt, off);
  _WA.curSource = src;
  _WA.curStartT = startedAt - off;
  _WA.curOffset = off;
  const _idx = Preview.idx;
  src.onended = () => {
    if (_WA.curSource !== src) return;
    if (_idx + 1 < Preview.queue.length) _waPlay(_idx + 1, 0);
    else _WA.curSource = null;
  };
  ['pp-play','pp-play-big','fp-play'].forEach(id => {
    const el = document.getElementById(id); if (el) el.textContent = '⏸';
  });
  if ('mediaSession' in navigator) navigator.mediaSession.playbackState = 'playing';
}
function _waSetVolume(v) {
  if (_WA.gain) _WA.gain.gain.value = Math.max(0, Math.min(1, v));
}

// Periodic UI tick for Web Audio (it has no `timeupdate` event).
setInterval(() => {
  if (!_waEnabled() || !_WA.curSource || _waIsPaused()) return;
  const cur = _waCurrentTime(), dur = _waDuration();
  if (_WA.curItem) _mixPosSave?.(_WA.curItem.posKey, cur, dur);
  try { _lrcSyncTick?.(cur); } catch {}
  // If user seeked to within 60s of end — urgently preload next track so the
  // gapless transition is ready even if normal preload hasn't finished yet.
  if (dur > 0 && (dur - cur) < 60 && !_WA.nextBuffer && !_WA._preloading) {
    _waPreloadNext();
  }
  if (_seekDragging) return;          // don't snap visuals while user drags
  const pct = dur ? (cur / dur * 100) : 0;
  const t   = `${Math.floor(cur/60)}:${String(Math.floor(cur%60)).padStart(2,'0')}`;
  try { _updateBuffered(); _updateCurrentChapter(cur); } catch (_) {}
  ['pp-fill','pp-fill-big','fp-fill'].forEach(id => { const el = document.getElementById(id); if(el) el.style.width = pct + '%'; });
  ['pp-cur','pp-cur-big','fp-cur'].forEach(id => { const el = document.getElementById(id); if(el) el.textContent = t; });
  const thumb = document.getElementById('fp-thumb'); if (thumb) thumb.style.setProperty('--x-pct', pct + '%');
  if ('mediaSession' in navigator && dur > 0) {
    try { navigator.mediaSession.setPositionState({duration: dur, playbackRate: 1, position: Math.min(cur, dur)}); } catch {}
  }
  _floatSyncFn?.();
}, 250);

// Apply player-toggle changes live (called by Settings → Плеер).
function _playerCfgChanged() {
  // Spin toggle is purely CSS — body class flips it.
  document.body.classList.toggle('no-spin', !(S.config?.['player-spin']));
}

// ── Draggable seek bars (pointer-capture, click + drag both work) ─────────
// Previously seek bars were click-only — each `mousemove` registered as a
// fresh click on its target, ending wherever the finger lifted (often near
// the left edge → "track restarts"). Now: pointerdown captures, pointermove
// only updates the visual fill, pointerup commits the seek at the release X.
// Set while the user is actively dragging ANY seek bar. The audio timeupdate
// handler skips its own fill-width updates during this window, otherwise it
// snaps the visual back to current playtime every 250ms — drag feels broken.
let _seekDragging = false;
function _wireSeekBar(bar) {
  if (!bar || bar._dragWired) return;
  bar._dragWired = true;
  bar.onclick = null;
  bar.removeAttribute('onclick');
  bar.style.touchAction = 'none';
  const fill = bar.querySelector('[id$="-fill"]');
  let dragging = false;
  let lastX    = 0;
  const showAt = (clientX) => {
    const rect = bar.getBoundingClientRect();
    if (!rect.width) return;
    const frac = Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
    if (fill) fill.style.width = (frac * 100) + '%';
    lastX = clientX;
  };
  const onMove = (e) => {
    if (!dragging) return;
    const x = (e.touches ? e.touches[0]?.clientX : e.clientX);
    if (typeof x === 'number') showAt(x);
    e.preventDefault();
  };
  const onUp = (e) => {
    if (!dragging) return;
    dragging = false;
    document.removeEventListener('pointermove', onMove);
    document.removeEventListener('pointerup',   onUp);
    document.removeEventListener('pointercancel', onUp);
    let x = e.clientX;
    if (typeof x !== 'number' || x === 0) x = lastX;
    previewSeek({currentTarget: bar, clientX: x});
    // 120ms defer so timeupdate doesn't snap bar back before audio.currentTime
    // has finished propagating to the new position.
    setTimeout(() => { _seekDragging = false; }, 120);
  };
  bar.addEventListener('pointerdown', (e) => {
    dragging = true; _seekDragging = true;
    showAt(e.clientX);
    document.addEventListener('pointermove',   onMove);
    document.addEventListener('pointerup',     onUp);
    document.addEventListener('pointercancel', onUp);
    e.preventDefault();
  });
  // Kill any residual synthesised click.
  bar.addEventListener('click', (e) => { e.preventDefault(); e.stopPropagation(); }, true);
}
function _wireAllSeekBars() {
  ['pp-progress', 'pp-progress-big', 'fp-progress'].forEach(id =>
    _wireSeekBar(document.getElementById(id))
  );
  _wireVolumeKeeper();
}
// Keep the hover-popup volume slider open for the whole drag. Without this, the
// first pointermove of a vertical drag leaves the narrow popup, :hover drops,
// the popup goes display:none and the native drag aborts — so only clicks worked.
function _wireVolumeKeeper() {
  const slider = document.getElementById('pp-vol');
  const wrap   = document.getElementById('pp-vol-wrap');
  if (!slider || !wrap || slider._volWired) return;
  slider._volWired = true;
  slider.style.touchAction = 'none';
  const release = () => {
    wrap.classList.remove('vol-active');
    document.removeEventListener('pointerup', release);
    document.removeEventListener('pointercancel', release);
  };
  slider.addEventListener('pointerdown', () => {
    wrap.classList.add('vol-active');
    document.addEventListener('pointerup', release);
    document.addEventListener('pointercancel', release);
  });
}
// Re-arm at every possible moment — DOMContentLoaded, full load, and again
// just before the first audio session starts. Idempotent — `_dragWired` flag
// stops repeated bindings.
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', _wireAllSeekBars);
} else {
  _wireAllSeekBars();
}
window.addEventListener('load', _wireAllSeekBars);

// ── Global cover-art fade-in ──────────────────────────────────────────────
// Capture-phase listener catches `load` on any img[data-cover] anywhere in
// the DOM — works for covers set via innerHTML as long as the attribute is
// present. Immediately marks cached images as loaded too (MutationObserver).
(function _initCoverFade() {
  document.addEventListener('load', e => {
    const img = e.target;
    if (img && img.tagName === 'IMG' && img.hasAttribute('data-cover')) {
      img.classList.add('loaded');
    }
  }, true);
  // Handle images that were already complete when inserted (browser cache hit).
  const obs = new MutationObserver(recs => {
    recs.forEach(r => r.addedNodes.forEach(n => {
      if (!n.querySelectorAll) return;
      n.querySelectorAll('img[data-cover]').forEach(img => {
        if (img.complete && img.naturalWidth > 0) img.classList.add('loaded');
      });
      if (n.tagName === 'IMG' && n.hasAttribute('data-cover') && n.complete && n.naturalWidth > 0) {
        n.classList.add('loaded');
      }
    }));
  });
  obs.observe(document.body, { childList: true, subtree: true });
})();

async function _preloadNext() {
  const nextIdx = Preview.idx + 1;
  if (nextIdx >= Preview.queue.length) return;
  if (_Pre.idx === nextIdx && _Pre.url) return;     // already done
  if (_Pre.resolving) return;
  const item = Preview.queue[nextIdx];
  if (!item) return;
  // Resolved URL already in queue item — nothing to fetch.
  if (item.url) {
    _Pre.idx = nextIdx; _Pre.url = item.url; return;
  }
  // Lazy item ({service, id}) — ask backend for the stream URL.
  if (!item.service || !item.id) return;
  _Pre.resolving = true;
  try {
    const _pqp = _streamQp(item.service);
    const resp = await fetch(
      `/api/stream/${item.service}/${item.id}` +
      `?name=${encodeURIComponent(item.title || '')}` +
      `&artist=${encodeURIComponent(item.artist || '')}` +
      (_pqp ? `&${_pqp}` : ''),
    );
    if (!resp.ok) { _Pre.resolving = false; return; }
    const ct = (resp.headers.get('content-type') || '').toLowerCase();
    let url = '';
    if (ct.includes('application/json')) {
      const r = await resp.json();
      url = r?.url || '';
    } else {
      url = resp.url;     // Deezer returns audio bytes directly
    }
    if (url) {
      _Pre.idx = nextIdx;
      _Pre.url = url;
      // Stash on the queue item too so _playPreviewAt(idx+1) skips the fetch.
      item.url = url;
    }
  } catch {}
  _Pre.resolving = false;
}

// ── Mix playback-position memory (BBC + SoundCloud) ────────────────────────
// Saves the play position of long mixes to localStorage so playback resumes
// from the same spot on return — even after a server restart.
const _MIXPOS_KEY = 'ripster_mixpos';
function _mixPosAll() {
  try { return JSON.parse(localStorage.getItem(_MIXPOS_KEY) || '{}') || {}; }
  catch { return {}; }
}
let _mixPosWriteTs = 0;
function _mixPosSave(key, pos, dur) {
  // Save play position for any real track (≥2 min) so resume-after-reload works
  // for every stream, not just long SoundCloud mixes. The resume PROMPT is still
  // gated on pos ≥ 30 s in _offerMixResume, so trivial early positions don't nag.
  // Throttled to ~once per 4.5 s.
  if (!key || !dur || !isFinite(dur) || dur < 120) return;
  const now = Date.now();
  if (now - _mixPosWriteTs < 4500) return;
  _mixPosWriteTs = now;
  const all = _mixPosAll();
  if (pos > 20 && pos < dur - 25) all[key] = { p: Math.floor(pos), d: Math.floor(dur), t: now };
  else delete all[key];   // near the start or the end → forget it
  const keys = Object.keys(all);
  if (keys.length > 120) {
    keys.sort((a, b) => (all[a].t || 0) - (all[b].t || 0));
    for (const k of keys.slice(0, keys.length - 120)) delete all[k];
  }
  try { localStorage.setItem(_MIXPOS_KEY, JSON.stringify(all)); } catch {}
}
function _mixPosGet(key) {
  const e = key ? _mixPosAll()[key] : null;
  return (e && e.p) ? e.p : 0;
}

function _setupAudioEvents() {
  // Idempotent re-arm seek-bar drag handlers — by the time we hit play, the
  // dock has definitely rendered.
  try { _wireAllSeekBars?.(); } catch {}
  // Lazy-wire the <audio> element into the Web Audio EQ chain — happens once,
  // first time audio events bind. Works only for same-origin streams (we
  // proxy all CDN audio through /api/proxy now, so it does work universally).
  try { _wireAudioToEQ?.(); } catch {}
  // One-time keyboard shortcuts wire-up — no-op on subsequent calls.
  if (!window._kbWired) {
    window._kbWired = true;
    document.addEventListener('keydown', (e) => {
      // Skip when user types in an input / textarea / contenteditable.
      const t = e.target;
      const tag = (t.tagName || '').toLowerCase();
      if (tag === 'input' || tag === 'textarea' || tag === 'select' || t.isContentEditable) return;
      // Don't hijack Cmd/Ctrl/Alt combos — leave them for the OS / browser.
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      const audio = document.getElementById('pp-audio');
      const hasAudio = audio && audio.src;
      switch (e.key) {
        case ' ':
          if (!hasAudio) return;
          e.preventDefault();
          previewToggle();
          break;
        case 'ArrowRight':
          if (!hasAudio) return;
          e.preventDefault();
          if (e.shiftKey && audio.duration && isFinite(audio.duration)) {
            audio.currentTime = Math.min(audio.duration - 0.5, audio.currentTime + 10);
          } else { previewNext(); }
          break;
        case 'ArrowLeft':
          if (!hasAudio) return;
          e.preventDefault();
          if (e.shiftKey && audio.duration && isFinite(audio.duration)) {
            audio.currentTime = Math.max(0, audio.currentTime - 10);
          } else { previewPrev(); }
          break;
        case 'm': case 'M':
          if (!hasAudio) return;
          e.preventDefault();
          previewMute();
          break;
        case 'f': case 'F':
          e.preventDefault();
          if (typeof fpOpen === 'function') {
            if (_FP?.open) fpClose(); else fpOpen();
          }
          break;
        case 'Escape':
          if (_FP?.open) { e.preventDefault(); fpClose(); }
          break;
      }
    });
  }
  if (Preview._bound) return;
  Preview._bound = true;
  const audio = document.getElementById('pp-audio');
  if (!audio) return;
  // Media Session — gives lockscreen cover + play/pause/next/prev on mobile.
  if ('mediaSession' in navigator) {
    try {
      navigator.mediaSession.setActionHandler('play',  () => {
        if (_waEnabled() && _WA.curSource) {
          _waResume();
        } else {
          if (_WA.ctx && _WA.ctx.state === 'suspended') _WA.ctx.resume().catch(() => {});
          audio.play().catch(() => {});
        }
      });
      navigator.mediaSession.setActionHandler('pause', () => {
        if (_waEnabled() && _WA.curSource) _waPause(); else audio.pause();
      });
      navigator.mediaSession.setActionHandler('previoustrack',  () => previewPrev());
      navigator.mediaSession.setActionHandler('nexttrack',      () => previewNext());
      navigator.mediaSession.setActionHandler('seekbackward',   (e) => {
        const offset = (e && e.seekOffset) || 10;
        if (_waEnabled() && _WA.curSource) { _waSeek(Math.max(0, _waCurrentTime() - offset)); return; }
        const a = document.getElementById('pp-audio'); if (!a) return;
        a.currentTime = Math.max(0, a.currentTime - offset);
      });
      navigator.mediaSession.setActionHandler('seekforward',    (e) => {
        const offset = (e && e.seekOffset) || 10;
        if (_waEnabled() && _WA.curSource) { _waSeek(Math.min(_waDuration(), _waCurrentTime() + offset)); return; }
        const a = document.getElementById('pp-audio'); if (!a) return;
        a.currentTime = Math.min(a.duration || 0, a.currentTime + offset);
      });
      navigator.mediaSession.setActionHandler('seekto',         (e) => {
        if (!e || e.seekTime == null) return;
        if (_waEnabled() && _WA.curSource) { _waSeek(e.seekTime); return; }
        const a = document.getElementById('pp-audio'); if (!a) return;
        try { a.currentTime = e.seekTime; } catch {}
      });
    } catch {}
  }
  audio.addEventListener('play',  () => {
    if ('mediaSession' in navigator) navigator.mediaSession.playbackState = 'playing';
  });
  audio.addEventListener('pause', () => {
    if ('mediaSession' in navigator) navigator.mediaSession.playbackState = 'paused';
  });
  audio.addEventListener('timeupdate', () => {
    if (audio.duration && audio.duration - audio.currentTime < 15) {
      _preloadNext();
    }
    const _it = Preview.queue[Preview.idx];
    if (_it) _mixPosSave(_it.posKey, audio.currentTime, audio.duration);
    try { _lrcSyncTick?.(audio.currentTime); } catch {}
    try { _updateBuffered(); _updateCurrentChapter(audio.currentTime); } catch (_) {}
    // While the user drags the seek bar, never let timeupdate snap the visual
    // fill back to live playtime — the drag would feel broken.
    if (_seekDragging) return;
    const pct = audio.duration ? (audio.currentTime / audio.duration * 100) + '%' : '0%';
    const timeStr = fmtDur(Math.floor(audio.currentTime));
    const fill = document.getElementById('pp-fill');
    const cur  = document.getElementById('pp-cur');
    if (fill) fill.style.width = pct;
    if (cur)  cur.textContent  = timeStr;
    const fillB = document.getElementById('pp-fill-big');
    const curB  = document.getElementById('pp-cur-big');
    if (fillB) fillB.style.width = pct;
    if (curB)  curB.textContent  = timeStr;
    const fillF = document.getElementById('fp-fill');
    const curF  = document.getElementById('fp-cur');
    const thumb = document.getElementById('fp-thumb');
    if (fillF) fillF.style.width = pct;
    if (curF)  curF.textContent  = timeStr;
    if (thumb) thumb.style.setProperty('--x-pct', pct);
  });
  audio.addEventListener('durationchange', () => {
    if (!audio.duration || !isFinite(audio.duration)) return;
    const durStr = fmtDur(Math.floor(audio.duration));
    const dur  = document.getElementById('pp-dur');
    const durB = document.getElementById('pp-dur-big');
    const durF = document.getElementById('fp-dur');
    if (dur)  dur.textContent  = durStr;
    if (durB) durB.textContent = durStr;
    if (durF) durF.textContent = durStr;
    try { _renderChapterTicks(); _updateBuffered(); } catch (_) {}
  });
  audio.addEventListener('progress', () => { try { _updateBuffered(); } catch (_) {} });
  audio.addEventListener('play', () => {
    // Real playback started — safe to allow ended/error events again.
    clearTimeout(Preview._suppressEndedTimer);
    Preview._suppressEnded = false;
    const b = document.getElementById('pp-play'); if(b) b.textContent = '⏸';
    const bB = document.getElementById('pp-play-big'); if(bB) bB.textContent = '⏸';
    const bF = document.getElementById('fp-play'); if(bF) bF.textContent = '⏸';
    const art = document.getElementById('fp-art'); if (art && art.querySelector('img')) art.classList.add('playing');
    // Persist last-playing track so we can offer resume on next page load.
    const _lpt = Preview.queue[Preview.idx];
    if (_lpt && _lpt.posKey) {
      try { localStorage.setItem('ripster_last_track', JSON.stringify({
        service: _lpt.service || '', id: _lpt.id || '', title: _lpt.title || '',
        artist: _lpt.artist || '', cover: _lpt.cover || '', posKey: _lpt.posKey, ts: Date.now(),
      })); } catch {}
    }
    try { _syncAlbumPlayBtns?.(); } catch {}
  });
  audio.addEventListener('pause', () => {
    const b = document.getElementById('pp-play'); if(b) b.textContent = '▶';
    const bB = document.getElementById('pp-play-big'); if(bB) bB.textContent = '▶';
    const bF = document.getElementById('fp-play'); if(bF) bF.textContent = '▶';
    const art = document.getElementById('fp-art'); if (art) art.classList.remove('playing');
    try { _syncAlbumPlayBtns?.(); } catch {}
  });
  audio.addEventListener('ended', () => {
    // Ignore stale `ended` fired by HLS.destroy() or a failed DRM play() during
    // track transitions — set by _playPreviewAt for 250 ms around each switch.
    if (Preview._suppressEnded) return;
    // Only auto-advance if the track GENUINELY played to its end. A broken/empty
    // stream fires `ended` at time ~0 with no real duration — treating that as
    // "finished" made the player skip-cascade through the whole album. Require
    // that we actually reached the end before moving on.
    if (!(audio.duration > 0 && isFinite(audio.duration) &&
          audio.currentTime >= audio.duration - 5)) {
      return;
    }
    // Sleep timer "end of track" mode — pause and stop
    if (_FP && _FP.sleepEndOfTrack) {
      _FP.sleepEndOfTrack = false;
      const lbl = document.getElementById('fp-sleep-val'); if (lbl) lbl.textContent = 'Сон';
      const btn = document.getElementById('fp-sleep-btn'); if (btn) btn.classList.remove('active');
      toast('💤 Таймер сна — пауза в конце трека', 'var(--muted)');
      return;   // don't auto-advance
    }
    if (Preview.idx >= 0 && Preview.idx < Preview.queue.length - 1) previewNext();
    else closePreview();
  });
  audio.addEventListener('error', () => {
    // MEDIA_ERR_ABORTED (code 1) = we changed src ourselves (a track switch). Never
    // a real failure.
    if (audio.error && audio.error.code === 1) return;
    // Debounce everything else: swapping src on a track switch aborts the previous
    // load and can fire a spurious 'error' BEFORE the new track loads. Only report
    // if, after a beat, we're STILL on the same track + same src, the element is
    // still in an error state, and it never started playing — i.e. a genuine,
    // persistent load failure rather than a switch artifact.
    const errIdx = Preview.idx;
    const errSrc = audio.currentSrc || audio.src || '';
    clearTimeout(Preview._errDebounce);
    Preview._errDebounce = setTimeout(() => {
      if (Preview.idx !== errIdx) return;                          // moved to another track
      if ((audio.currentSrc || audio.src || '') !== errSrc) return; // src changed under us
      if (!audio.error || audio.error.code === 1) return;          // recovered / abort
      if (!audio.paused || audio.currentTime > 0.5) return;        // actually playing
      const item = Preview.queue[Preview.idx];
      if (!item) return;
      const svcHints = {
        '/api/stream/deezer/': 'Deezer: трек недоступен (регион/лицензия). «⬇ Скачать» может помочь.',
        '/api/stream/tidal/':  'Tidal stream error — возможно токен истёк (Settings → Tidal)',
        '/api/stream/qobuz/':  'Qobuz stream error — проверь токен (Settings → Qobuz)',
      };
      const hint = Object.entries(svcHints).find(([k]) => (item.url || '').includes(k));
      toast(hint ? hint[1] : 'Ошибка воспроизведения аудио', 'var(--red)', '', 5000);
      const btn = document.getElementById('pp-play'); if(btn) btn.textContent = '▶';
      const btnB = document.getElementById('pp-play-big'); if(btnB) btnB.textContent = '▶';
    }, 900);
  });
}

async function _playPreviewAt(idx) {
  const item  = Preview.queue[idx];
  console.log('[_playAt]', idx, {hasItem: !!item, hasUrl: !!item?.url, svc: item?.service, id: item?.id});
  if (!item) return;
  // Refresh the in-bar quality pill for this track's service (options + label).
  try { _updateQualityPill(item); } catch (_) {}
  // Suppress stale `ended`/`error` events that some browsers fire when HLS.js
  // is destroyed or when a DRM play() call fails — they would trigger previewNext()
  // concurrently with the new track we're about to start.
  Preview._suppressEnded = true;
  clearTimeout(Preview._suppressEndedTimer);
  Preview._suppressEndedTimer = setTimeout(() => { Preview._suppressEnded = false; }, 250);
  // Kill any SC DRM stream immediately — must happen before WA/audio path branching
  // so HLS.js / FairPlay video don't keep playing in parallel with the new track.
  if (Preview._hls) { Preview._hls.destroy(); Preview._hls = null; }
  if (Preview._fpsEl) { Preview._fpsEl.pause(); Preview._fpsEl.src = ''; Preview._fpsEl = null; }
  // Per-track chapters + service colour. _playPreviewAt is the play path for the
  // queue/release flow, which previously left the PREVIOUS track's tracklist
  // stuck in the player (e.g. playing a PROFF release still showed the Anjunadeep
  // tracklist). Refresh them for THIS item: SC mixes with timecodes get ticks +
  // tracklist; everything else clears them.
  try {
    if (typeof _applyServiceColor === 'function') _applyServiceColor(item.service);
    if (item.service === 'soundcloud' && typeof _scChaptersFor === 'function') {
      _playerSetChapters(_scChaptersFor(item.id));
      const _ytFallback = () => {
        if (!(Preview._chapters || []).length && typeof _scFetchYtTimecodes === 'function') {
          _scFetchYtTimecodes(item.id, () => { try { _playerSetChapters(_scChaptersFor(item.id)); } catch (_) {} });
        }
      };
      if (typeof _scFetch1001 === 'function') {
        _scFetch1001(item.id, () => { try { _playerSetChapters(_scChaptersFor(item.id)); } catch (_) {} _ytFallback(); });
      } else { _ytFallback(); }
    } else if (typeof _playerSetChapters === 'function') {
      _playerSetChapters([]);
    }
  } catch (_) {}
  // Gapless Web Audio path — only for same-origin URLs (cross-origin CDNs
  // block decodeAudioData by missing CORS headers).
  if (_waEnabled() && _waCanPlay(item)) {
    console.log('[_playAt] WA path');
    Preview.idx = idx;
    _setupAudioEvents();
    // Show bar + set data-preview-open so content padding kicks in
    const bar  = document.getElementById('preview-player');
    const main = document.querySelector('.main');
    if (bar) bar.classList.add('visible');
    if (main) {
      const isExpanded = document.getElementById('pp-expanded')?.style.display !== 'none';
      main.removeAttribute('data-preview-open');
      main.removeAttribute('data-preview-expanded');
      main.setAttribute(isExpanded ? 'data-preview-expanded' : 'data-preview-open', '1');
    }
    // Sync mini player title/artist/cover (WA path skips the normal assignment below)
    const _waArtistSub = item.full
      ? (item.label || (item.artist ? `${item.artist} · полный трек` : 'Полный трек'))
      : (item.artist ? `${item.artist} · 30 сек` : 'Предпрослушка');
    const _waPpTitle   = document.getElementById('pp-title');
    const _waPpArtist  = document.getElementById('pp-artist');
    const _waPpTitleB  = document.getElementById('pp-title-big');
    const _waPpArtistB = document.getElementById('pp-artist-big');
    if (_waPpTitle)   _waPpTitle.textContent   = item.title || '—';
    if (_waPpArtist)  _waPpArtist.textContent  = _waArtistSub;
    if (_waPpTitleB)  _waPpTitleB.textContent  = item.title || '—';
    if (_waPpArtistB) _waPpArtistB.textContent = _waArtistSub;
    const _waCoverHtml = item.cover
      ? `<img src="${esc(item.cover)}" data-cover onload="this.classList.add('loaded')" style="width:100%;height:100%;object-fit:cover"/>`
      : '♪';
    const _waPpArt    = document.getElementById('pp-art');
    const _waPpArtBig = document.getElementById('pp-art-big');
    if (_waPpArt)    _waPpArt.innerHTML    = _waCoverHtml;
    if (_waPpArtBig) _waPpArtBig.innerHTML = _waCoverHtml;
    const _waPrev = document.getElementById('pp-prev');
    const _waNext = document.getElementById('pp-next');
    if (_waPrev) _waPrev.disabled = (idx === 0);
    if (_waNext) _waNext.disabled = (idx >= Preview.queue.length - 1);
    if (typeof _updateMediaSession === 'function') _updateMediaSession(item, _waArtistSub);
    await _waPlay(idx, 0);
    return;
  }
  if (_waEnabled() && !_waCanPlay(item)) {
    console.log('[_playAt] WA blocked (cross-origin) — falling back to <audio>');
  }
  // Invalidate any preload that points elsewhere — we're jumping.
  if (_Pre.idx !== idx + 1) { _Pre.idx = -1; _Pre.url = ''; }
  const audio = document.getElementById('pp-audio');
  const bar   = document.getElementById('preview-player');
  const main  = document.querySelector('.main');
  if (!audio || !bar) return;

  // Stop BBC if it was playing
  if (Preview.mode === 'bbc') {
    const bbcAudio = document.getElementById('bbc-audio');
    if (BBC.hls) { BBC.hls.destroy(); BBC.hls = null; }
    if (bbcAudio) { bbcAudio.pause(); bbcAudio.src = ''; }
    Preview.mode = 'spotify';
  }

  ['pp-fill','pp-fill-big'].forEach(id => { const el = document.getElementById(id); if(el) el.style.width = '0%'; });
  ['pp-cur','pp-cur-big'].forEach(id => { const el = document.getElementById(id); if(el) el.textContent = '0:00'; });
  ['pp-dur','pp-dur-big'].forEach(id => { const el = document.getElementById(id); if(el) el.textContent = '0:00'; });

  // Show ⏸ immediately — don't wait for play event (autoplay might block)
  const playBtn  = document.getElementById('pp-play');
  const playBtnB = document.getElementById('pp-play-big');
  if (playBtn)  playBtn.textContent  = '⏸';
  if (playBtnB) playBtnB.textContent = '⏸';

  // Lazy URL resolution — playlist queue items only carry {service, id} until
  // we actually reach them. This avoids resolving 50 stream URLs upfront.
  if (!item.url && item.service && item.id) {
    // SC fast path: check prewarm cache (populated by hover/render prewarm in sc.js).
    // A cache hit means instant play — no network round-trip at all.
    if (item.service === 'soundcloud' && typeof _scCacheGet === 'function') {
      const _sc = _scCacheGet(item.id);
      if (_sc) {
        item.url           = _sc.url;
        item.format        = _sc.format || '';
        item.license_token = _sc.license_token || '';
        if (_sc.cover && !item.cover) item.cover = _sc.cover;
        console.log('[_playAt] SC cache hit → instant play', item.format);
      }
    }
    // Deezer: build the same-origin proxy URL directly and let <audio> stream it.
    // No resolve-fetch (which would download the whole track just to read resp.url,
    // then the browser fetches it AGAIN for playback — a double download + stall).
    if (item.service === 'deezer') {
      const _dqp = _streamQp('deezer');
      item.url = `/api/stream/deezer/${item.id}` +
        `?name=${encodeURIComponent(item.title || '')}` +
        `&artist=${encodeURIComponent(item.artist || '')}` +
        (_dqp ? `&${_dqp}` : '');
      item.format = 'mp3';
    }
  }
  if (!item.url && item.service && item.id) {
    try {
      const _qp = _streamQp(item.service);
      const resp = await fetch(
        `/api/stream/${item.service}/${item.id}` +
        `?name=${encodeURIComponent(item.title || '')}` +
        `&artist=${encodeURIComponent(item.artist || '')}` +
        (_qp ? `&${_qp}` : ''),
      );
      if (!resp.ok) {
        let detail = resp.statusText;
        try { const j = await resp.clone().json(); if (j?.detail) detail = j.detail; } catch {}
        // 451 = DRM/paid track (no Go+ subscription). Skip silently to next;
        // at the END of the queue offer a one-click download via Lucida which
        // can decrypt SC's encrypted streams server-side.
        if (resp.status === 451 || resp.status === 404) {
          _FP._drmHits = (_FP._drmHits || 0) + 1;
          if (idx < Preview.queue.length - 1) { Preview.idx = idx + 1; return _playPreviewAt(idx + 1); }
          const n = _FP._drmHits;
          _FP._drmHits = 0;
          if (item.service === 'soundcloud') {
            toast(`SC: ${n} тр. пропущено — платный контент (нет Go+). Попробуй «⬇ Скачать».`, 'var(--orange)', '', 6000);
          } else {
            toast(`Треков с DRM пропущено: ${n}. «⬇ Скачать» обходит это.`,
                  'var(--orange)', '', 6000);
          }
          return;
        }
        toast(`Стрим ${item.service}: ${detail}`, 'var(--red)');
        if (idx < Preview.queue.length - 1) { Preview.idx = idx + 1; return _playPreviewAt(idx + 1); }
        return;
      }
      // Deezer returns binary audio — keep the same URL (browser streams it)
      const ct = (resp.headers.get('content-type') || '').toLowerCase();
      if (ct.includes('application/json')) {
        const r = await resp.json();
        if (!r.url) { toast('Нет stream URL', 'var(--red)'); return; }
        item.url           = r.url;
        item.format        = r.format || item.format;
        item.license_token = r.license_token || '';
        if (r.artwork && !item.cover) item.cover = r.artwork;
        // Also populate SC cache so the next track switch doesn't re-fetch.
        if (item.service === 'soundcloud' && typeof _scCacheSet === 'function') {
          _scCacheSet(item.id, { url: r.url, format: r.format || '', license_token: r.license_token || '', cover: r.artwork || '' });
        }
      } else {
        item.url    = resp.url;
        item.format = 'mp3';
      }
    } catch (e) {
      toast('Стрим: ' + e.message, 'var(--red)');
      return;
    }
  }

  if (item.format === 'drm-hls-cbc' || item.format === 'drm-hls-ctr') {
    _scDrmHls(audio, item, playBtn, playBtnB);
  } else {
    audio.src = _proxyAudioUrl(item.url);
    // Auto-resume from a saved position ONLY for long mixes (>10 min). Clicking a
    // normal track always starts from 0 — otherwise it'd silently jump to wherever
    // you last scrolled it, which feels like a bug. (Resume of any track after a
    // page reload is still offered, explicitly, by _offerMixResume.)
    const _resumeAt = _mixPosGet(item.posKey);
    if (_resumeAt > 0) {
      audio.addEventListener('loadedmetadata', function _seek() {
        audio.removeEventListener('loadedmetadata', _seek);
        if (audio.duration && audio.duration > 600 && _resumeAt < audio.duration - 20) {
          try { audio.currentTime = _resumeAt; } catch(_) {}
          toast(`▶ Продолжаю с ${fmtDur(_resumeAt)}`, 'var(--muted)', '', 2600);
        }
      });
    }
    // Unlock AudioContext before play — on mobile the EQ wire suspends the ctx.
    if (_WA.ctx && _WA.ctx.state === 'suspended') _WA.ctx.resume().catch(() => {});
    const p = audio.play();
    if (p) p.catch(e => {
      console.warn('preview autoplay blocked:', e.message);
      if (playBtn)  playBtn.textContent  = '▶';
      if (playBtnB) playBtnB.textContent = '▶';
    });
  }

  const artistSub = item.full
    ? (item.label || (item.artist ? `${item.artist} · полный трек` : 'Полный трек'))
    : (item.artist ? `${item.artist} · 30 сек` : 'Предпрослушка');

  document.getElementById('pp-title').textContent  = item.title   || '—';
  document.getElementById('pp-artist').textContent = artistSub;
  bar.classList.add('visible');
  if (main) {
    const isExpanded = document.getElementById('pp-expanded')?.style.display !== 'none';
    main.removeAttribute('data-preview-open');
    main.removeAttribute('data-preview-expanded');
    main.setAttribute(isExpanded ? 'data-preview-expanded' : 'data-preview-open', '1');
  }

  // Cover art — sync compact and expanded (fade-in via data-cover + CSS)
  const coverHtml = item.cover
    ? `<img src="${esc(item.cover)}" data-cover onload="this.classList.add('loaded')" style="width:100%;height:100%;object-fit:cover"/>`
    : '♪';
  const ppArt    = document.getElementById('pp-art');
  const ppArtBig = document.getElementById('pp-art-big');
  if (ppArt)    ppArt.innerHTML    = coverHtml;
  if (ppArtBig) ppArtBig.innerHTML = coverHtml;

  // Sync expanded panel text
  const titleBig  = document.getElementById('pp-title-big');
  const artistBig = document.getElementById('pp-artist-big');
  if (titleBig)  titleBig.textContent  = item.title   || '—';
  if (artistBig) artistBig.textContent = artistSub;

  // Update lockscreen / system media controls
  _updateMediaSession(item, artistSub);
  // Update fullscreen player meta (sync UI even if not currently open)
  if (typeof fpSyncMeta === 'function') fpSyncMeta(item);
  // If the lyrics panel is open, fetch fresh lines for the new track.
  if (_LRC?.open) { try { fpFetchLyrics?.(); } catch {} }
  // Restore saved playback speed for the new audio element src
  if (_FP && _FP.speed && audio) audio.playbackRate = _FP.speed;

  // prev/next disabled state
  const prevBtn = document.getElementById('pp-prev');
  const nextBtn = document.getElementById('pp-next');
  if (prevBtn) prevBtn.disabled = (idx === 0);
  if (nextBtn) nextBtn.disabled = (idx >= Preview.queue.length - 1);
  Preview.idx = idx;
}

// Apple 30-sec previews are direct cross-origin iTunes URLs. The <audio> element
// is wired into the Web Audio EQ graph (createMediaElementSource), and browsers
// output SILENCE for cross-origin media routed through Web Audio (no CORS). So
// route iTunes/mzstatic previews through our same-origin /api/proxy → sound + EQ.
function _proxyAudioUrl(url) {
  try {
    const h = new URL(url, location.origin).hostname;
    if (/(^|\.)itunes\.apple\.com$|(^|\.)mzstatic\.com$/.test(h)) {
      const b64 = btoa(url).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
      return `/api/proxy?u=${b64}&svc=apple&mime=${encodeURIComponent('audio/mp4')}`;
    }
  } catch (_) {}
  return url;
}

function playPreview(url, title, artist, cover) {
  _setupAudioEvents();
  // Pass cover so the player shows artwork + info (it reads item.cover); without
  // it the 30-sec preview played with a blank player.
  Preview.queue = [{url, title, artist, cover: cover || '', service: 'apple'}];
  Preview.idx   = 0;
  _playPreviewAt(0);
}

// Full-length streaming via /api/stream/{service}/{trackId}
async function playStreamTrack(service, trackId, title, artist, cover) {
  const svcLabels = {qobuz:'Qobuz · ' + t('player.full_track'), tidal:'Tidal · ' + t('player.full_track'), deezer:'Deezer · ' + t('player.full_track'), soundcloud:'SoundCloud'};
  const svcLabel  = svcLabels[service] || t('player.full_track');

  // If a Web Audio context already exists (e.g. a prior gapless track), resume it
  // inside this click gesture so playback isn't silent. Do NOT create one here —
  // SC plays via plain <audio> (_waCanPlay=false), and creating a fresh suspended
  // ctx would route SC audio through it and mute it.
  try {
    if (_WA.ctx && _WA.ctx.state === 'suspended') _WA.ctx.resume().catch(() => {});
  } catch (_) {}

  try {
    let streamUrl, format, licenseToken = '';

    if(service === 'deezer') {
      // Deezer returns StreamingResponse (audio bytes), not JSON.
      // Set audio.src directly — browser streams+plays as data arrives.
      const dqp = _streamQp('deezer');
      streamUrl = `/api/stream/deezer/${trackId}?name=${encodeURIComponent(title||'')}&artist=${encodeURIComponent(artist||'')}${dqp ? '&'+dqp : ''}`;
      format    = 'mp3';
    } else {
      // SC fast path: prewarm cache populated on hover/render → skip network call entirely.
      if (service === 'soundcloud' && typeof _scCacheGet === 'function') {
        const _sc = _scCacheGet(trackId);
        if (_sc && _sc.url) {
          streamUrl    = _sc.url;
          format       = _sc.format || '';
          licenseToken = _sc.license_token || '';
          console.log('[playStreamTrack] SC cache hit:', format);
        }
      }
      if (!streamUrl) {
        // Qobuz / Tidal / SC cache-miss — fetch JSON {url, format} from backend.
        const loadingLabel = document.getElementById('pp-artist');
        if(loadingLabel) loadingLabel.textContent = t('player.loading');
        // Show dock early so user sees feedback
        const bar  = document.getElementById('preview-player');
        const main = document.querySelector('.main');
        if(bar)  bar.classList.add('visible');
        if(main) { const isExpanded = document.getElementById('pp-expanded')?.style.display !== 'none'; main.setAttribute(isExpanded ? 'data-preview-expanded' : 'data-preview-open', '1'); }

        const sqp = _streamQp(service);
        const resp = await fetch(`/api/stream/${service}/${trackId}?name=${encodeURIComponent(title||'')}&artist=${encodeURIComponent(artist||'')}${sqp ? '&'+sqp : ''}`);
        if(!resp.ok) {
          const err = await resp.json().catch(() => ({}));
          if (resp.status === 451) {
            toast(`${service === 'soundcloud' ? 'SC' : service}: ${t('player.paid_content')}`, 'var(--orange)', '', 6000);
          } else {
            toast(t('player.stream_error') + (err.detail || resp.statusText), 'var(--red)');
          }
          if(bar)  bar.classList.remove('visible');
          if(main) { main.removeAttribute('data-preview-open'); main.removeAttribute('data-preview-expanded'); }
          return;
        }
        const r = await resp.json();
        if(!r.url) {
          toast('Нет stream URL: ' + (r.detail || r.error || '?'), 'var(--red)');
          return;
        }
        streamUrl    = r.url;
        format       = r.format || '';
        licenseToken = r.license_token || '';
        // Populate SC cache so subsequent plays (e.g. after queue switch) are instant.
        if (service === 'soundcloud' && typeof _scCacheSet === 'function') {
          _scCacheSet(trackId, { url: r.url, format: r.format || '', license_token: r.license_token || '', cover: r.artwork || '' });
        }
      }
    }

    _setupAudioEvents();
    Preview.queue = [{url: streamUrl, title, artist, full: true, format, label: svcLabel,
                      cover: cover || '', posKey: service + ':' + trackId,
                      service, id: String(trackId), license_token: licenseToken}];
    Preview.idx   = 0;
    // Per-service progress colour + YouTube-style chapters (SC mixes w/ timecodes).
    try {
      _applyServiceColor(service);
      if (service === 'soundcloud' && typeof _scChaptersFor === 'function') {
        _playerSetChapters(_scChaptersFor(trackId));
        const _ytFb = () => {
          if (!(Preview._chapters || []).length && typeof _scFetchYtTimecodes === 'function') {
            _scFetchYtTimecodes(trackId, () => { try { _playerSetChapters(_scChaptersFor(trackId)); } catch (_) {} });
          }
        };
        if (typeof _scFetch1001 === 'function') {
          _scFetch1001(trackId, () => { try { _playerSetChapters(_scChaptersFor(trackId)); } catch (_) {} _ytFb(); });
        } else { _ytFb(); }
      } else {
        _playerSetChapters([]);
      }
    } catch (_) {}
    _playPreviewAt(0);
    setTimeout(() => _syncAlbumPlayBtns?.(), 150);

  } catch(e) {
    toast('Ошибка стрима: ' + e.message, 'var(--red)');
  }
}

function playAlbumTrackPreview(idx) {
  _setupAudioEvents();
  Preview.queue = window._albumPreviews || [];
  Preview.idx   = idx;
  _playPreviewAt(idx);
}

function previewToggle() {
  if (Preview.mode === 'bbc') { bbcTogglePlay(); return; }
  if (_waEnabled() && _WA.curSource) {
    if (_waIsPaused()) _waResume(); else _waPause();
    setTimeout(() => _syncAlbumPlayBtns?.(), 50);
    return;
  }
  if (Preview._fpsEl) {
    if (Preview._fpsEl.paused) Preview._fpsEl.play().catch(() => {});
    else Preview._fpsEl.pause();
    setTimeout(() => _syncAlbumPlayBtns?.(), 50);
    return;
  }
  const audio = document.getElementById('pp-audio');
  if (!audio) return;
  if (audio.paused) {
    if (_WA.ctx && _WA.ctx.state === 'suspended') _WA.ctx.resume().catch(() => {});
    audio.play().catch(() => {});
  } else {
    audio.pause();
  }
  setTimeout(() => _syncAlbumPlayBtns?.(), 50);
}

function previewNext() {
  if (Preview.mode === 'bbc') return;
  if (Preview.idx < Preview.queue.length - 1) {
    Preview.idx++;
    _playPreviewAt(Preview.idx);
  }
}

function previewPrev() {
  if (Preview.mode === 'bbc') return;
  const _ct = Preview._fpsEl ? Preview._fpsEl.currentTime : document.getElementById('pp-audio')?.currentTime;
  if (_ct > 3) {
    if (Preview._fpsEl) Preview._fpsEl.currentTime = 0;
    else { const a = document.getElementById('pp-audio'); if (a) a.currentTime = 0; }
    return;
  }
  if (Preview.idx > 0) {
    Preview.idx--;
    _playPreviewAt(Preview.idx);
  }
}

function previewSeek(event) {
  const bar  = event.currentTarget || document.getElementById('pp-progress');
  if (!bar) return;
  const rect = bar.getBoundingClientRect();
  const frac = Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width));
  if (Preview.mode === 'bbc') {
    const audio = document.getElementById('bbc-audio');
    if (audio && audio.duration) audio.currentTime = frac * audio.duration;
    return;
  }
  if (_waEnabled() && _WA.curBuffer) { _waSeek(frac * _waDuration()); return; }
  if (Preview._fpsEl && Preview._fpsEl.duration && isFinite(Preview._fpsEl.duration)) {
    try { Preview._fpsEl.currentTime = frac * Preview._fpsEl.duration; } catch (_) {}
    return;
  }
  const audio = document.getElementById('pp-audio');
  if (!audio || !audio.duration || !isFinite(audio.duration)) return;
  try { audio.currentTime = frac * audio.duration; }
  catch (e) { console.warn('seek failed:', e.message); }
}

// ── YouTube-style chapters + buffered (cache) bar + per-service progress colour ──
function _playerSvcColor(svc) {
  try {
    const ov = (typeof S !== 'undefined' && S.config && S.config['service-colors']) || {};
    if (svc && ov[svc]) return ov[svc];
    if (svc && typeof _svcColor === 'function') { const c = _svcColor(svc); if (c) return c; }
  } catch (_) {}
  return 'var(--red)';
}

// Paint the PLAYED fills in the current service's colour. The buffered/cache bar
// keeps one neutral colour everywhere (set in markup).
function _applyServiceColor(svc) {
  const col = _playerSvcColor(svc);
  ['pp-fill', 'pp-fill-big', 'fp-fill'].forEach(id => {
    const el = document.getElementById(id); if (el) el.style.background = col;
  });
  // Drive the played-line glow + thumb ring colour via a CSS variable.
  try { document.documentElement.style.setProperty('--pp-svc', col); } catch (_) {}
  Preview._svcColor = col;
}

// Buffered-ahead ("cache") bar from <audio>.buffered — YouTube-style.
function _updateBuffered() {
  const audio = Preview._fpsEl || document.getElementById('pp-audio');
  let pct = 0;
  try {
    if (audio && isFinite(audio.duration) && audio.duration && audio.buffered && audio.buffered.length) {
      pct = Math.min(100, audio.buffered.end(audio.buffered.length - 1) / audio.duration * 100);
    } else if (_waEnabled() && _WA.curBuffer) {
      pct = 100;  // gapless WA decodes the whole track up front
    }
  } catch (_) {}
  const w = pct.toFixed(1) + '%';
  ['pp-buffered', 'pp-buffered-big', 'fp-buffered'].forEach(id => {
    const el = document.getElementById(id); if (el) el.style.width = w;
  });
}

function _fmtChapTs(sec) {
  sec = Math.max(0, Math.floor(sec || 0));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return h ? `${h}:${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}`
           : `${m}:${String(s).padStart(2,'0')}`;
}

function _playerDuration() {
  if (_waEnabled() && _WA.curBuffer) { try { return _waDuration(); } catch (_) {} }
  if (Preview.mode === 'bbc') {
    const b = document.getElementById('bbc-audio');
    if (b && isFinite(b.duration)) return b.duration;
    return (typeof BBC !== 'undefined' && BBC.duration) || 0;
  }
  const a = Preview._fpsEl || document.getElementById('pp-audio');
  return (a && isFinite(a.duration)) ? a.duration : 0;
}

// Attach chapters: [{seconds,label}] (sorted). Empty array = hide the UI.
function _playerSetChapters(chapters) {
  Preview._chapters = Array.isArray(chapters) ? chapters : [];
  Preview._curChap  = -1;
  const wrap = document.getElementById('pp-chapters-wrap');
  const list = document.getElementById('pp-chapters');
  const cnt  = document.getElementById('pp-chapter-count');
  const has  = Preview._chapters.length > 0;
  if (wrap) wrap.style.display = has ? '' : 'none';
  if (cnt)  cnt.textContent = has ? Preview._chapters.length + ' тр.' : '';
  if (list) {
    list.innerHTML = has ? Preview._chapters.map((ch, i) =>
      `<div class="pp-chap" data-i="${i}" onclick="previewSeekTo(${ch.seconds})"
         style="display:flex;gap:10px;align-items:baseline;padding:5px 8px;border-radius:6px;cursor:pointer">
         <span style="color:var(--blue);font-family:var(--mono);font-size:10px;flex:0 0 auto;min-width:40px">${_fmtChapTs(ch.seconds)}</span>
         <span style="flex:1;min-width:0;color:var(--muted);font-size:12px;line-height:1.4;word-break:break-word">${esc(ch.label)}</span>
       </div>`).join('') : '';
  }
  _renderChapterTicks();
}

// Tick marks at chapter boundaries on every seek bar (needs known duration).
function _renderChapterTicks() {
  const chapters = Preview._chapters || [];
  const dur = _playerDuration();
  ['pp-progress', 'pp-progress-big', 'fp-progress'].forEach(barId => {
    const bar = document.getElementById(barId); if (!bar) return;
    bar.querySelectorAll('.pp-tick').forEach(t => t.remove());
    if (!dur || !chapters.length) return;
    chapters.forEach(ch => {
      if (ch.seconds <= 0 || ch.seconds >= dur) return;
      const d = document.createElement('div');
      d.className = 'pp-tick';
      // Thin centred marker (not full-height) so the seek bar stays a clean line
      // instead of a row of tall blocks on the fullscreen player.
      d.style.cssText = `position:absolute;top:50%;transform:translateY(-50%);height:10px;width:2px;left:${(ch.seconds/dur*100).toFixed(2)}%;background:rgba(0,0,0,.55);pointer-events:none;z-index:2`;
      bar.appendChild(d);
    });
  });
}

// Highlight the chapter for the current playback time + show "now playing" track.
function _updateCurrentChapter(curSec) {
  const chapters = Preview._chapters || [];
  if (!chapters.length) return;
  let idx = -1;
  for (let i = 0; i < chapters.length; i++) {
    if (chapters[i].seconds <= curSec + 0.5) idx = i; else break;
  }
  if (idx === Preview._curChap) return;
  Preview._curChap = idx;
  const list = document.getElementById('pp-chapters');
  if (list) {
    list.querySelectorAll('.pp-chap').forEach(el => {
      const on = (+el.dataset.i === idx);
      el.style.background = on ? 'rgba(255,255,255,.07)' : '';
      const nm = el.children[1]; if (nm) nm.style.color = on ? 'var(--text)' : 'var(--muted)';
      if (on) try { el.scrollIntoView({ block: 'nearest' }); } catch (_) {}
    });
  }
  if (idx >= 0) {
    const label = '▸ ' + chapters[idx].label;
    const a1 = document.getElementById('pp-artist');     if (a1) a1.textContent = label;
    const a2 = document.getElementById('pp-artist-big');  if (a2) a2.textContent = label;
  }
}

function previewSeekTo(seconds) {
  if (!isFinite(seconds) || seconds < 0) return;
  if (Preview.mode === 'bbc') { const a = document.getElementById('bbc-audio'); if (a && isFinite(a.duration)) a.currentTime = seconds; return; }
  if (_waEnabled() && _WA.curBuffer) { _waSeek(seconds); return; }
  if (Preview._fpsEl && isFinite(Preview._fpsEl.duration)) { try { Preview._fpsEl.currentTime = seconds; } catch (_) {} return; }
  const a = document.getElementById('pp-audio');
  if (a && isFinite(a.duration)) { try { a.currentTime = seconds; } catch (_) {} }
}

function previewMute() {
  const audioId = Preview.mode === 'bbc' ? 'bbc-audio' : 'pp-audio';
  const audio   = document.getElementById(audioId);
  if (!audio) return;
  audio.muted = !audio.muted;
  const icon = audio.muted ? '🔇' : '🔊';
  const vol  = audio.muted ? 0 : audio.volume;
  ['pp-mute','pp-mute-big'].forEach(id => { const el = document.getElementById(id); if(el) el.textContent = icon; });
  ['pp-vol','pp-vol-big'].forEach(id => { const el = document.getElementById(id); if(el) el.value = vol; });
}

function previewVolume(val) {
  const v = parseFloat(val);
  if (Preview.mode === 'bbc') { bbcVol(v); return; }
  if (Preview._fpsEl) { Preview._fpsEl.volume = v; Preview._fpsEl.muted = (v === 0); }
  const audio = document.getElementById('pp-audio');
  if (_WA._audioSourceNode || (_waEnabled() && _WA.curSource)) {
    // Audio is routed through the WA graph — use gain node as the sole volume knob.
    // If audio.volume were also < 1, the two would multiply and give wrong levels.
    _waSetVolume(v);
    if (audio) { audio.volume = 1.0; audio.muted = (v === 0); }
  } else {
    if (!audio) return;
    audio.volume = v;
    audio.muted  = (v === 0);
  }
  const icon = (v === 0) ? '🔇' : (v < 0.5 ? '🔉' : '🔊');
  ['pp-mute','pp-mute-big'].forEach(id => { const el = document.getElementById(id); if(el) el.textContent = icon; });
  ['pp-vol','pp-vol-big'].forEach(id => { const el = document.getElementById(id); if(el) el.value = v; });
}

function togglePlayerExpanded() {
  // On mobile go fullscreen by default (unless user opted out in settings).
  const mobileFsOn = (S.config?.['player-mobile-fs'] !== false);
  if (mobileFsOn && window.matchMedia && window.matchMedia('(max-width:699px)').matches) {
    fpOpen();
    return;
  }
  const bar = document.getElementById('preview-player');
  const exp = document.getElementById('pp-expanded');
  const btn = document.getElementById('pp-expand-btn');
  const main = document.querySelector('.main');
  if (!bar || !exp) return;
  const opening = exp.style.display === 'none';
  exp.style.display = opening ? '' : 'none';
  if (btn) btn.classList.toggle('expanded', opening);
  // Desktop: expanded view becomes an AIMP-style vertical panel docked on the
  // left border between the nav sidebar and the content (CSS body.pp-side).
  document.body.classList.toggle('pp-side', opening);
  if (main) {
    if (opening) {
      main.removeAttribute('data-preview-open');
      main.setAttribute('data-preview-expanded', '1');
    } else {
      main.removeAttribute('data-preview-expanded');
      main.setAttribute('data-preview-open', '1');
    }
  }
}

// ── Floating PC player (Document Picture-in-Picture API) ───────────────────
// Opens a real OS-level always-on-top window with the player UI. Works even
// when the browser is minimised — perfect for desktop "DJ-mode" listening
// while doing other things. Chrome / Edge 116+ have Document PiP; for
// everything else we fall back to a window.open() popup.
let _floatWin = null;
let _floatSyncFn = null;  // set by _renderFloatPlayer; called from WA tick for gapless sync

async function floatPlayer() {
  // Already floating? Close and bring focus back.
  if (_floatWin && !_floatWin.closed) {
    try { _floatWin.close(); } catch {}
    _floatWin = null;
    return;
  }

  // Pull current state so the float window opens with the right cover/title.
  const item = Preview.queue[Preview.idx] || {};
  const audio = document.getElementById('pp-audio');

  // Prefer the real Document Picture-in-Picture API — true always-on-top OS window.
  if (typeof documentPictureInPicture !== 'undefined') {
    try {
      _floatWin = await documentPictureInPicture.requestWindow({width: 380, height: 180});
      _renderFloatPlayer(_floatWin, item, audio);
      _floatWin.addEventListener('pagehide', () => { _floatWin = null; });
      return;
    } catch (e) {
      // User denied or feature blocked — fall through to popup.
      console.warn('Document PiP failed:', e.message);
    }
  }

  // Fallback: a regular popup. Not always-on-top but better than nothing.
  _floatWin = window.open('', 'ripster-float', 'width=380,height=180,resizable=yes,toolbar=no,location=no,menubar=no');
  if (!_floatWin) {
    toast('Не удалось открыть окно — браузер заблокировал popup', 'var(--orange)');
    return;
  }
  _renderFloatPlayer(_floatWin, item, audio);
  _floatWin.addEventListener('beforeunload', () => { _floatWin = null; });
}

function _renderFloatPlayer(win, item, audio) {
  const doc = win.document;
  const fmt = (s) => {
    s = Math.max(0, Math.floor(s||0));
    const m = Math.floor(s/60), sec = s%60;
    return `${m}:${String(sec).padStart(2,'0')}`;
  };
  // Chrome's PiP chrome shows the source URL, not the title — that's a
  // browser policy, can't be overridden. We compensate by making the in-window
  // track title big enough to read at a glance.
  doc.title = item.title || 'Ripster';
  doc.head.innerHTML = `
    <meta charset="utf-8"/>
    <title>${esc(item.title || 'Ripster')}</title>
    <style>
      *,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
      html,body{height:100%;background:linear-gradient(180deg,#1a1c24 0%,#0e0f14 100%);color:#fff;
        font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;overflow:hidden;user-select:none}
      .wrap{display:flex;flex-direction:column;height:100%;padding:10px 12px 0}
      .row1{display:flex;align-items:center;gap:10px;margin-bottom:8px;min-height:0}
      .art{width:56px;height:56px;border-radius:8px;background:#1c1e2a center/cover no-repeat;
        flex-shrink:0;box-shadow:0 4px 14px rgba(0,0,0,.55)}
      .meta{flex:1;min-width:0}
      .title{font-size:14px;font-weight:700;line-height:1.2;
        white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
      .artist{font-size:11px;color:rgba(255,255,255,.55);margin-top:3px;
        white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
      .row2{display:flex;align-items:center;gap:6px;margin-bottom:6px}
      .ctrls{display:flex;gap:4px;align-items:center;flex-shrink:0}
      button{background:rgba(255,255,255,.08);border:none;color:#fff;cursor:pointer;
        border-radius:50%;width:30px;height:30px;font-size:12px;
        transition:transform .12s cubic-bezier(.3,1.5,.6,1),background .12s;
        box-shadow:inset 0 1px 0 rgba(255,255,255,.06),0 1px 2px rgba(0,0,0,.2)}
      button:hover{background:rgba(255,255,255,.18)}
      button:active{transform:scale(.88)}
      .play{width:38px;height:38px;background:#fff;color:#111;font-size:14px}
      .play:hover{background:#fff;filter:brightness(.96)}
      .vol-wrap{flex:1;display:flex;align-items:center;gap:6px;margin-left:8px}
      input[type=range]{-webkit-appearance:none;appearance:none;width:100%;height:3px;
        background:rgba(255,255,255,.18);border-radius:2px;outline:none;cursor:pointer;margin:0}
      input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;appearance:none;
        width:11px;height:11px;border-radius:50%;background:#fff;cursor:pointer;
        box-shadow:0 1px 3px rgba(0,0,0,.5)}
      input[type=range]::-moz-range-thumb{width:11px;height:11px;border-radius:50%;
        background:#fff;border:none;cursor:pointer;box-shadow:0 1px 3px rgba(0,0,0,.5)}
      .row3{display:flex;align-items:center;gap:7px;padding-bottom:8px}
      .time{font-size:10px;color:rgba(255,255,255,.5);font-family:'SF Mono',monospace;
        flex-shrink:0;min-width:32px;text-align:center}
      .seek{flex:1;height:18px;cursor:pointer;display:flex;align-items:center;position:relative}
      .seek::before{content:"";position:absolute;left:0;right:0;height:4px;background:rgba(255,255,255,.15);border-radius:2px}
      .fill{position:absolute;left:0;height:4px;background:linear-gradient(90deg,#c084a0,#e58cad);
        border-radius:2px;width:0%;pointer-events:none}
    </style>
  `;
  doc.body.innerHTML = `
    <div class="wrap">
      <div class="row1">
        <div class="art" id="fw-art" style="${item.cover ? `background-image:url('${esc(item.cover)}')` : ''}"></div>
        <div class="meta">
          <div class="title" id="fw-title">${esc(item.title || '—')}</div>
          <div class="artist" id="fw-artist">${esc(item.artist || '')}</div>
        </div>
      </div>
      <div class="row2">
        <div class="ctrls">
          <button id="fw-prev" title="Prev">⏮</button>
          <button id="fw-play" class="play" title="Play/Pause">${audio && !audio.paused ? '⏸' : '▶'}</button>
          <button id="fw-next" title="Next">⏭</button>
        </div>
        <div class="vol-wrap">
          <span style="font-size:11px;opacity:.6">🔊</span>
          <input type="range" id="fw-vol" min="0" max="1" step="0.01" value="${audio?.volume ?? 1}"/>
        </div>
      </div>
      <div class="row3">
        <span class="time" id="fw-cur">0:00</span>
        <div class="seek" id="fw-seek"><div class="fill" id="fw-fill"></div></div>
        <span class="time" id="fw-dur">0:00</span>
      </div>
    </div>
  `;

  // Wire controls — call back into the main document's functions.
  const main = window;
  doc.getElementById('fw-prev').onclick = () => main.previewPrev?.();
  doc.getElementById('fw-next').onclick = () => main.previewNext?.();
  doc.getElementById('fw-play').onclick = () => main.previewToggle?.();
  doc.getElementById('fw-vol').oninput = (e) => {
    if (audio) audio.volume = parseFloat(e.target.value);
    // Mirror back to the main dock slider too.
    const m = main.document.getElementById('pp-vol');
    if (m) m.value = e.target.value;
    const mb = main.document.getElementById('pp-vol-big');
    if (mb) mb.value = e.target.value;
  };
  doc.getElementById('fw-seek').onclick = (e) => {
    const r = e.currentTarget.getBoundingClientRect();
    const frac = (e.clientX - r.left) / r.width;
    if (audio && audio.duration) audio.currentTime = frac * audio.duration;
  };

  // Live-sync from the main audio element (or WA gapless state).
  const sync = () => {
    if (!_floatWin || _floatWin.closed) return;
    const it = Preview.queue[Preview.idx] || {};
    doc.title = it.title || 'Ripster';
    const titleEl = doc.getElementById('fw-title');
    if (titleEl) titleEl.textContent  = it.title  || '—';
    const artistEl = doc.getElementById('fw-artist');
    if (artistEl) artistEl.textContent = it.artist || '';
    const art = doc.getElementById('fw-art');
    if (art) art.style.backgroundImage = it.cover ? `url('${it.cover}')` : '';

    let curSec, durSec, paused;
    if (_waEnabled() && _WA.curSource) {
      curSec = _waCurrentTime();
      durSec = _waDuration();
      paused = _waIsPaused();
    } else {
      curSec = audio?.currentTime || 0;
      durSec = audio?.duration   || 0;
      paused = !audio || audio.paused;
    }

    const playBtn = doc.getElementById('fw-play');
    if (playBtn) playBtn.textContent = paused ? '▶' : '⏸';
    const pct = durSec ? (curSec / durSec * 100) : 0;
    const fill = doc.getElementById('fw-fill');
    if (fill) fill.style.width = pct + '%';
    const curEl = doc.getElementById('fw-cur');
    if (curEl) curEl.textContent = fmt(curSec);
    const durEl = doc.getElementById('fw-dur');
    if (durEl) durEl.textContent = fmt(durSec);
    const vol = doc.getElementById('fw-vol');
    if (vol && audio) vol.value = audio.volume;
  };
  _floatSyncFn = sync;
  if (audio) {
    audio.addEventListener('timeupdate', sync);
    audio.addEventListener('play',       sync);
    audio.addEventListener('pause',      sync);
    audio.addEventListener('durationchange', sync);
    audio.addEventListener('volumechange',   sync);
  }
  win.addEventListener('pagehide', () => {
    _floatSyncFn = null;
    if (!audio) return;
    audio.removeEventListener('timeupdate', sync);
    audio.removeEventListener('play',       sync);
    audio.removeEventListener('pause',      sync);
    audio.removeEventListener('durationchange', sync);
    audio.removeEventListener('volumechange',   sync);
  });
  sync();
}

// ── Fullscreen player (mobile + opt-in desktop) ─────────────────────────────
const _FP = { open:false, speed:1, sleepMs:0, sleepTimer:null, startY:0, startX:0, dragging:false };
const _FP_SPEEDS = [1, 1.25, 1.5, 1.75, 2];

function fpOpen() {
  const el = document.getElementById('fullscreen-player');
  if (!el) return;
  _FP.open = true;
  el.classList.add('open');
  el.setAttribute('aria-hidden', 'false');
  document.body.classList.add('fp-open');
  fpSyncFromState();
  _haptic(8);
  try { _vizStart?.(); } catch {}
}
function fpClose() {
  const el = document.getElementById('fullscreen-player');
  if (!el) return;
  _FP.open = false;
  el.classList.remove('open');
  el.setAttribute('aria-hidden', 'true');
  document.body.classList.remove('fp-open');
  _haptic(6);
  try { _vizStop?.(); } catch {}
}
function _haptic(ms) {
  if (navigator.vibrate) { try { navigator.vibrate(ms); } catch {} }
}

// Sync FP UI from the current Preview state
function fpSyncFromState() {
  const item = Preview.queue[Preview.idx] || {};
  fpSyncMeta(item);
  // Restore play/pause icon from audio state
  const audio = document.getElementById('pp-audio');
  const playEl = document.getElementById('fp-play');
  if (audio && playEl) playEl.textContent = audio.paused ? '▶' : '⏸';
}
function fpSyncMeta(item) {
  const art    = document.getElementById('fp-art');
  const title  = document.getElementById('fp-title');
  const artist = document.getElementById('fp-artist');
  const bg     = document.getElementById('fp-bg');
  const src    = document.getElementById('fp-source');
  if (title)  title.textContent  = item.title  || '—';
  if (artist) artist.textContent = item.artist || (item.label || '');
  if (src)    src.textContent    = item.label  || t('player.now_playing');
  if (art) {
    // Crossfade: preload the new image, then swap into place. The old <img>
    // fades to 0 over the same span. Skips work if the URL hasn't changed —
    // otherwise a no-op cover-replace would flash.
    const cur = art.querySelector('img[data-cover]');
    const sameSrc = cur && item.cover && cur.getAttribute('src') === item.cover;
    if (item.cover && !sameSrc) {
      const next = document.createElement('img');
      next.setAttribute('data-cover', '');
      next.alt = '';
      next.draggable = false;
      next.style.cssText = 'position:absolute;inset:0;width:100%;height:100%;object-fit:cover;opacity:0;transition:opacity .22s ease';
      next.onload = () => {
        requestAnimationFrame(() => {
          next.style.opacity = '1';
          if (cur) cur.style.opacity = '0';
          // Drop the old <img> after the fade completes
          setTimeout(() => { try { cur && cur.remove(); } catch {} }, 280);
        });
      };
      next.src = item.cover;
      // Make sure the container can host the absolute-positioned children
      if (getComputedStyle(art).position === 'static') art.style.position = 'relative';
      art.appendChild(next);
    } else if (!item.cover) {
      art.innerHTML = '♪';
    }
  }
  if (bg && item.cover) {
    bg.style.backgroundImage = `url("${item.cover}")`;
  } else if (bg) {
    bg.style.backgroundImage = '';
  }
}

// Speed control: cycle through preset speeds
function fpCycleSpeed() {
  const audio = document.getElementById('pp-audio');
  const i = _FP_SPEEDS.indexOf(_FP.speed);
  _FP.speed = _FP_SPEEDS[(i + 1) % _FP_SPEEDS.length];
  if (audio) audio.playbackRate = _FP.speed;
  const lbl = document.getElementById('fp-speed-val');
  if (lbl) lbl.textContent = (_FP.speed === 1 ? '1' : _FP.speed) + '×';
  const btn = document.getElementById('fp-speed-btn');
  if (btn) btn.classList.toggle('active', _FP.speed !== 1);
  try { localStorage.setItem('ripster_playback_speed', String(_FP.speed)); } catch {}
  _haptic(6);
}

// Sleep timer: pop a menu of presets
function fpToggleSleepMenu(ev) {
  ev?.stopPropagation();
  const pop = document.getElementById('fp-sleep-pop');
  if (!pop) return;
  if (pop.style.display !== 'none') { pop.style.display = 'none'; return; }
  pop.innerHTML = [
    [0,  t('sleep.off')],
    [15, t('sleep.15m')],
    [30, t('sleep.30m')],
    [45, t('sleep.45m')],
    [60, t('sleep.1h')],
    [-1, t('sleep.end_track')],
  ].map(([m, lbl]) =>
    `<div onclick="fpSetSleep(${m})" style="padding:10px 14px;cursor:pointer;border-radius:8px;font-size:13px;color:#fff" onmouseover="this.style.background='rgba(255,255,255,.08)'" onmouseout="this.style.background=''">  ${lbl}</div>`
  ).join('');
  pop.style.display = 'block';
  // position near the sleep button
  const btn = document.getElementById('fp-sleep-btn');
  if (btn) {
    const r = btn.getBoundingClientRect();
    pop.style.left = Math.max(12, Math.min(window.innerWidth - 200, r.left)) + 'px';
    pop.style.top  = Math.max(60, r.top - pop.offsetHeight - 8) + 'px';
  }
  // outside-click closes
  setTimeout(() => {
    const off = (e) => {
      if (!pop.contains(e.target) && e.target !== btn) {
        pop.style.display = 'none';
        document.removeEventListener('click', off, true);
      }
    };
    document.addEventListener('click', off, true);
  }, 0);
}
function fpSetSleep(minutes) {
  const pop = document.getElementById('fp-sleep-pop'); if (pop) pop.style.display = 'none';
  const lbl = document.getElementById('fp-sleep-val');
  const btn = document.getElementById('fp-sleep-btn');
  if (_FP.sleepTimer) { clearTimeout(_FP.sleepTimer); _FP.sleepTimer = null; }
  if (minutes === 0) {
    if (lbl) lbl.textContent = t('sleep.label');
    if (btn) btn.classList.remove('active');
    return;
  }
  if (minutes === -1) {
    if (lbl) lbl.textContent = t('sleep.active_end');
    if (btn) btn.classList.add('active');
    _FP.sleepEndOfTrack = true;
    return;
  }
  const ms = minutes * 60 * 1000;
  if (lbl) lbl.textContent = `⌛ ${minutes} ${t('sleep.30m').split(' ')[1] || 'min'}`;
  if (btn) btn.classList.add('active');
  _FP.sleepTimer = setTimeout(() => {
    const a = document.getElementById('pp-audio'); if (a) a.pause();
    if (lbl) lbl.textContent = t('sleep.label');
    if (btn) btn.classList.remove('active');
    toast(t('sleep.toast_fired'), 'var(--muted)');
  }, ms);
  _haptic(8);
  toast(`${t('sleep.label')}: ${minutes} ${t('sleep.30m').split(' ')[1] || 'min'}`, 'var(--green)', '', 2500);
}

// ── Lyrics (LRCLIB) — slide-up panel with synced line scrolling ───────────
const _LRC = { lines: [], plain: '', activeIdx: -1, fetchKey: '', open: false };
function fpToggleLyrics() {
  const panel = document.getElementById('fp-lyrics');
  if (!panel) return;
  _LRC.open = !_LRC.open;
  if (_LRC.open) {
    panel.style.display = 'block';
    requestAnimationFrame(() => panel.style.transform = 'translateY(0)');
    fpFetchLyrics();
  } else {
    panel.style.transform = 'translateY(100%)';
    setTimeout(() => { panel.style.display = 'none'; }, 320);
  }
  const btn = document.getElementById('fp-lyrics-btn');
  if (btn) btn.classList.toggle('active', _LRC.open);
}
async function fpFetchLyrics() {
  const item = Preview.queue[Preview.idx];
  if (!item) return;
  const key = `${item.artist}::${item.title}`;
  const status = document.getElementById('fp-lyrics-status');
  const body   = document.getElementById('fp-lyrics-body');
  if (_LRC.fetchKey === key) { _renderLrcBody(); return; }
  _LRC.fetchKey = key; _LRC.lines = []; _LRC.plain = ''; _LRC.activeIdx = -1;
  if (status) status.textContent = 'Загрузка…';
  if (body)   body.innerHTML = '';
  if (!item.artist || !item.title) {
    if (status) status.textContent = 'Нет метаданных трека';
    return;
  }
  try {
    const audio = document.getElementById('pp-audio');
    const dur = audio?.duration && isFinite(audio.duration) ? Math.round(audio.duration) : 0;
    const params = new URLSearchParams({artist: item.artist, track: item.title});
    if (dur) params.set('duration', String(dur));
    const r = await fetch('/api/lyrics?' + params.toString());
    const d = await r.json();
    if (d.synced) {
      _LRC.lines = _lrcParse(d.synced);
      if (status) status.textContent = `🎵 LRCLIB · ${_LRC.lines.length} строк`;
    } else if (d.plain) {
      _LRC.plain = d.plain;
      if (status) status.textContent = '📄 LRCLIB · без таймингов';
    } else {
      if (status) status.textContent = '— Текст не найден';
    }
    _renderLrcBody();
  } catch (e) {
    if (status) status.textContent = '✗ ' + e.message;
  }
}
function _lrcParse(lrc) {
  const out = [];
  const re = /^\[(\d{1,2}):(\d{2})(?:\.(\d{1,3}))?\](.*)$/gm;
  let m;
  while ((m = re.exec(lrc)) !== null) {
    const t = parseInt(m[1]) * 60 + parseInt(m[2]) + parseInt(m[3] || '0') / 1000;
    out.push({ t, text: m[4].trim() });
  }
  out.sort((a, b) => a.t - b.t);
  return out;
}
function _renderLrcBody() {
  const body = document.getElementById('fp-lyrics-body');
  if (!body) return;
  if (_LRC.lines.length) {
    body.innerHTML = _LRC.lines.map((l, i) =>
      `<div class="lrc-line" data-i="${i}" style="padding:6px 0;transition:opacity .25s,transform .25s,color .25s">${esc(l.text || '·')}</div>`
    ).join('');
  } else if (_LRC.plain) {
    body.innerHTML = `<div style="white-space:pre-wrap;text-align:left">${esc(_LRC.plain)}</div>`;
  } else {
    body.innerHTML = '<div style="color:rgba(255,255,255,.35);padding:24px 0">…</div>';
  }
}
function _lrcSyncTick(currentTime) {
  if (!_LRC.open || !_LRC.lines.length) return;
  let idx = -1;
  for (let i = 0; i < _LRC.lines.length; i++) {
    if (_LRC.lines[i].t <= currentTime) idx = i; else break;
  }
  if (idx === _LRC.activeIdx) return;
  _LRC.activeIdx = idx;
  const body = document.getElementById('fp-lyrics-body');
  if (!body) return;
  const lines = body.querySelectorAll('.lrc-line');
  lines.forEach((el, i) => {
    if (i === idx) {
      el.style.color = '#fff';
      el.style.fontSize = '18px';
      el.style.transform = 'scale(1.05)';
      el.style.opacity = '1';
      el.scrollIntoView({behavior: 'smooth', block: 'center'});
    } else {
      el.style.color = 'rgba(255,255,255,.45)';
      el.style.fontSize = '15px';
      el.style.transform = 'scale(1)';
    }
  });
}

// Share — copy current item URL (or the service link)
function fpShare() {
  const item = Preview.queue[Preview.idx];
  const link = item?.shareUrl || item?.url || location.href;
  if (navigator.share) {
    navigator.share({ title: item?.title || 'Ripster', url: link }).catch(() => {});
  } else {
    try {
      navigator.clipboard.writeText(link);
      toast('🔗 Ссылка скопирована', 'var(--green)', '', 2000);
    } catch {
      toast('Не удалось скопировать', 'var(--red)');
    }
  }
  _haptic(6);
}

function fpMenu(ev) {
  // Minimal menu — for now just open share. Future: queue, lyrics, etc.
  fpShare();
}

// Ripple on tap — gives "physical" press feedback
function _fpAttachRipples() {
  const el = document.getElementById('fullscreen-player');
  if (!el) return;
  el.addEventListener('pointerdown', (e) => {
    const t = e.target.closest('.fp-icon-btn,.fp-ctrl,.fp-pill');
    if (!t) return;
    const r = t.getBoundingClientRect();
    const dot = document.createElement('span');
    dot.className = 'fp-ripple';
    const size = 10;
    dot.style.width = size + 'px';
    dot.style.height = size + 'px';
    dot.style.left = (e.clientX - r.left - size / 2) + 'px';
    dot.style.top  = (e.clientY - r.top  - size / 2) + 'px';
    t.style.position = t.style.position || 'relative';
    t.style.overflow = 'hidden';
    t.appendChild(dot);
    setTimeout(() => dot.remove(), 560);
    _haptic(4);
  });
}

// Telegram-style swipe-down to dismiss + horizontal swipe → prev/next.
// Velocity-aware: a quick flick dismisses even at small distance. Panel fades
// out as it travels — that's the perceptual cue that makes the gesture feel
// 1:1 with the finger rather than a binary "passed threshold? snap".
function _fpAttachGestures() {
  const el = document.getElementById('fullscreen-player');
  if (!el) return;
  let sy = 0, sx = 0, dy = 0, dx = 0, active = false;
  let lastT = 0, lastY = 0, velY = 0;
  let lockedAxis = null;        // 'y' | 'x' | null — set after small move
  let raf = 0, pendingDy = 0;

  const applyDrag = (dyPx) => {
    // GPU-only update via translate3d. Opacity scales linearly from 1→.45
    // over the first 320px of travel — Telegram's exact feel.
    const h = el.clientHeight || 800;
    const fade = Math.max(.45, 1 - Math.min(dyPx, 320) / 580);
    el.style.transform = `translate3d(0, ${dyPx}px, 0)`;
    el.style.opacity   = String(fade);
  };
  const flush = () => {
    raf = 0;
    applyDrag(pendingDy);
  };

  el.addEventListener('touchstart', (e) => {
    if (e.touches.length !== 1) return;
    sy = lastY = e.touches[0].clientY; sx = e.touches[0].clientX;
    lastT = performance.now();
    dy = 0; dx = 0; velY = 0; lockedAxis = null; active = true;
    el.classList.add('dragging');
  }, { passive: true });

  el.addEventListener('touchmove', (e) => {
    if (!active || e.touches.length !== 1) return;
    const y = e.touches[0].clientY;
    const x = e.touches[0].clientX;
    dy = y - sy; dx = x - sx;

    // Decide axis after first ~8px of movement so a vertical scroll inside
    // the panel (e.g. lyrics) doesn't get hijacked by the drag.
    if (lockedAxis === null) {
      if (Math.abs(dx) < 8 && Math.abs(dy) < 8) return;
      lockedAxis = Math.abs(dx) > Math.abs(dy) ? 'x' : 'y';
    }

    // Velocity in px/ms — exponential smoothing
    const now = performance.now();
    const dt  = Math.max(1, now - lastT);
    velY = velY * 0.6 + ((y - lastY) / dt) * 0.4;
    lastT = now; lastY = y;

    if (lockedAxis === 'y' && dy > 0) {
      pendingDy = dy;
      if (!raf) raf = requestAnimationFrame(flush);
    }
  }, { passive: true });

  el.addEventListener('touchend', () => {
    el.classList.remove('dragging');
    if (!active) return;
    active = false;
    if (raf) { cancelAnimationFrame(raf); raf = 0; }

    const DIST = 120;     // px to commit by distance alone
    const VEL  = 0.7;     // px/ms to commit by flick
    const HORIZ = 90;

    if (lockedAxis === 'y' && (dy > DIST || velY > VEL)) {
      // Dismissed — let CSS finish the slide out, then close. Translating to
      // the panel's own height keeps the motion in sync with the open
      // transition (same curve, same axis).
      const h = el.clientHeight || window.innerHeight;
      el.style.transition = 'transform .26s cubic-bezier(.4,0,1,1), opacity .2s ease';
      el.style.transform  = `translate3d(0, ${h}px, 0)`;
      el.style.opacity    = '0';
      setTimeout(() => {
        el.style.transition = '';
        el.style.transform = '';
        el.style.opacity   = '';
        fpClose();
      }, 230);
    } else if (lockedAxis === 'x' && Math.abs(dx) > HORIZ) {
      el.style.transform = '';
      el.style.opacity   = '';
      if (dx < 0) previewNext(); else previewPrev();
    } else {
      // Snap back — same spring as the open transition for visual consistency
      el.style.transition = 'transform .28s cubic-bezier(.22,1.05,.36,1), opacity .2s ease';
      el.style.transform  = '';
      el.style.opacity    = '';
      setTimeout(() => { el.style.transition = ''; }, 280);
    }
  });

  // Cancel/abort restores cleanly — iOS sometimes fires touchcancel during
  // scroll fights or notification UI taking focus.
  el.addEventListener('touchcancel', () => {
    el.classList.remove('dragging');
    if (raf) { cancelAnimationFrame(raf); raf = 0; }
    el.style.transform = '';
    el.style.opacity   = '';
    active = false;
  });
}

// Init FP behaviors once the DOM is ready
window.addEventListener('load', () => {
  _fpAttachRipples();
  _fpAttachGestures();
  // Restore saved speed
  try {
    const s = parseFloat(localStorage.getItem('ripster_playback_speed') || '1');
    if (!isNaN(s) && s > 0) {
      _FP.speed = s;
      const a = document.getElementById('pp-audio'); if (a) a.playbackRate = s;
      const lbl = document.getElementById('fp-speed-val');
      if (lbl) lbl.textContent = (s === 1 ? '1' : s) + '×';
      const btn = document.getElementById('fp-speed-btn');
      if (btn) btn.classList.toggle('active', s !== 1);
    }
  } catch {}
});

function closePreview() {
  if (Preview.mode === 'bbc') { bbcStop(); return; }
  const audio = document.getElementById('pp-audio');
  const bar   = document.getElementById('preview-player');
  const main  = document.querySelector('.main');
  const exp   = document.getElementById('pp-expanded');
  const btn   = document.getElementById('pp-expand-btn');
  if (audio) { audio.pause(); audio.removeAttribute('src'); audio.load(); }
  if (_WA.curSource) { try { _WA.curSource.onended = null; _WA.curSource.stop(0); } catch {} _WA.curSource = null; }
  _waStopKeepalive();
  if (bar)   bar.classList.remove('visible');
  if (exp)   exp.style.display = 'none';
  if (btn)   btn.classList.remove('expanded');
  document.body.classList.remove('pp-side');
  if (main)  { main.removeAttribute('data-preview-open'); main.removeAttribute('data-preview-expanded'); }
  // Also close fullscreen player if open
  if (typeof fpClose === 'function' && _FP && _FP.open) fpClose();
  ['pp-fill','pp-fill-big'].forEach(id => { const el = document.getElementById(id); if(el) el.style.width = '0%'; });
  ['pp-cur','pp-cur-big'].forEach(id => { const el = document.getElementById(id); if(el) el.textContent = '0:00'; });
  const playBtn  = document.getElementById('pp-play');     if(playBtn)  playBtn.textContent  = '▶';
  const playBtnB = document.getElementById('pp-play-big'); if(playBtnB) playBtnB.textContent = '▶';
  const prevBtn  = document.getElementById('pp-prev');     if(prevBtn)  prevBtn.disabled = true;
  const nextBtn  = document.getElementById('pp-next');     if(nextBtn)  nextBtn.disabled = true;
  Preview.queue = [];
  Preview.idx   = -1;
}

// ── Local file playback ────────────────────────────────────────────────────────
// Called from the 📂 button in the player bar. Works on mobile (file picker
// shows local storage / Downloads folder). No server round-trip needed.
function playLocalFile(file) {
  if (!file) return;
  // Revoke any previous local object URL to avoid leaks
  if (playLocalFile._objUrl) {
    URL.revokeObjectURL(playLocalFile._objUrl);
    playLocalFile._objUrl = null;
  }
  const url = URL.createObjectURL(file);
  playLocalFile._objUrl = url;

  // Strip extension from name for a cleaner title
  const title = file.name.replace(/\.[^.]+$/, '');
  _setupAudioEvents();
  Preview.queue = [{
    url,
    title,
    artist:  t('player.local_file'),
    cover:   '',
    label:   t('player.local_file'),
    posKey:  'local:' + file.name,
    local:   true,
  }];
  Preview.idx = 0;
  _playPreviewAt(0);
}



// ── Mix auto-resume on page reload ────────────────────────────────────────
// Offers to continue a long mix from where the user left off.
// Called from app.js handleMessage('init') with a 2s delay so the UI is ready.
let _resumeOffered = false;
function _offerMixResume() {
  if (_resumeOffered) return;
  _resumeOffered = true;
  try {
    const lt = JSON.parse(localStorage.getItem('ripster_last_track') || 'null');
    if (!lt || !lt.posKey || (Date.now() - lt.ts) > 86400000) return;
    const pos = _mixPosGet(lt.posKey);
    if (!pos || pos < 30) return;
    const mm = Math.floor(pos / 60), ss = Math.floor(pos % 60);
    const durStr = mm + ':' + String(ss).padStart(2, '0');
    window._resumeLastTrack = () => {
      try { localStorage.removeItem('ripster_last_track'); } catch {}
      if (!lt.service || !lt.id) return;
      _setupAudioEvents();
      Preview.queue = [{
        service: lt.service, id: String(lt.id), title: lt.title || '—',
        artist: lt.artist || '', cover: lt.cover || '', full: true,
        label: (lt.service === 'soundcloud' ? 'SoundCloud' : lt.service) + ' · ' + t('player.continuation'),
        posKey: lt.posKey,
      }];
      Preview.idx = 0;
      _playPreviewAt(0);
    };
    const safeName = (lt.title || 'микс').replace(/</g,'&lt;').replace(/>/g,'&gt;').slice(0, 40);
    toast(
      '<span>▶ «' + safeName + '» — продолжить с ' + durStr + '?</span>' +
      '<button onclick="event.stopPropagation();_resumeLastTrack()" style="margin-left:8px;padding:2px 9px;border-radius:5px;font-size:11px;font-weight:700;background:rgba(192,132,160,.18);border:1px solid rgba(192,132,160,.3);color:var(--red);cursor:pointer;font-family:var(--font)">▶ Слушать</button>',
      'var(--muted)', '', 9000
    );
  } catch {}
}
