// FairPlay patch — overrides _scDrmHls CBC path with correct WebKit FPS implementation.
// Capture player.js's original via window to avoid hoisting-induced self-capture.
const _scDrmHlsOrig = window._scDrmHls || null;
fetch('/api/sc_fps_log', { method: 'POST', headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ msg: '[player_fps.js] patch v10 loaded', ua: navigator.userAgent.slice(0, 150) }) }).catch(() => {});

window._scDrmHls = async function _scDrmHls(audioEl, item, playBtn, playBtnB) {
  const licToken = item.license_token || '';

  // Always log entry so we can see format + which DRM path was selected
  fetch('/api/sc_fps_log', { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ msg: `[FPS] enter fmt=${item.format} prefer=${window._scDrmPrefer} wk=${'WebKitMediaKeys' in window}`, ua: navigator.userAgent.slice(0,150) }) }).catch(() => {});

  if (item.format === 'drm-hls-cbc') {
    const _fpsLog = (msg) => {
      console.log(msg);
      fetch('/api/sc_fps_log', { method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ msg, ua: navigator.userAgent.slice(0, 200) }),
      }).catch(() => {});
    };

    let fpsEl = document.getElementById('pp-fps-video');
    if (!fpsEl) {
      fpsEl = document.createElement('video');
      fpsEl.id = 'pp-fps-video';
      fpsEl.setAttribute('playsinline', '');
      fpsEl.setAttribute('webkit-playsinline', '');
      fpsEl.setAttribute('x-webkit-airplay', 'allow');
      Object.assign(fpsEl.style, {
        position: 'fixed', top: '-9999px', left: '-9999px',
        width: '1px', height: '1px', opacity: '0', pointerEvents: 'none',
      });
      document.body.appendChild(fpsEl);
    }
    fpsEl.pause();
    try { await fpsEl.setMediaKeys?.(null); } catch (_) {}
    try { fpsEl.webkitSetMediaKeys?.(null); } catch (_) {}
    fpsEl.removeAttribute('src'); fpsEl.load();
    audioEl.pause(); audioEl.src = '';
    Preview._fpsEl = fpsEl;

    // iOS always uses webkitneedkey — never check standard EME when WebKitMediaKeys exists.
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
      _scDrmSkip(t('p.drm_fps_safari'));
      return;
    }
    _fpsLog(`[FPS] patch1 wk=${hasWebKitKeys} std=${hasStdFps}`);

    // Session-local cert cache — cert never changes within a tab session.
    // Saves one network round-trip on every subsequent FPS track play.
    let serverCert;
    try {
      if (window._fpsCertCache instanceof Uint8Array && window._fpsCertCache.length > 200) {
        serverCert = window._fpsCertCache;
        _fpsLog(`[FPS] cert ${serverCert.length}B (cached)`);
      } else {
        const certResp = await fetch('/api/sc_fps_cert');
        serverCert = new Uint8Array(await certResp.arrayBuffer());
        window._fpsCertCache = serverCert;
        _fpsLog(`[FPS] cert ${serverCert.length}B (fetched)`);
      }
    } catch (e) {
      _fpsLog('[FPS] cert fail: ' + e.message);
      Preview._fpsEl = null; _scDrmSkip(t('p.drm_fps_cert')); return;
    }

    let _keyInstalled = false;
    let _fpsPending = false;
    const _doLicense = async (spc) => {
      const body = spc instanceof Uint8Array ? spc : new Uint8Array(spc);
      const r = await fetch(`/api/sc_fps_license?token=${encodeURIComponent(licToken)}`, {
        method: 'POST', body,
        headers: { 'Content-Type': 'application/octet-stream' },
      });
      if (!r.ok) {
        const errText = await r.text().catch(() => '');
        throw new Error(`lic ${r.status}: ${errText.slice(0, 120)}`);
      }
      return new Uint8Array(await r.arrayBuffer());
    };

    // Standard EME — only on Safari desktop (no WebKitMediaKeys)
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
      } catch (e) { _fpsLog('[FPS] EME err: ' + e.message); mediaKeys = null; }
      if (mediaKeys) {
        fpsEl.addEventListener('encrypted', async (e) => {
          if (_keyInstalled || _fpsPending) return;
          _fpsPending = true;
          _fpsLog('[FPS] encrypted: ' + e.initDataType);
          try {
            const sess = mediaKeys.createSession();
            sess.addEventListener('message', async (msg) => {
              try {
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

    // Legacy WebKit FPS — iOS Safari.
    //
    // Apple's FairPlay HTML5 flow (per Sample Player & FPS dev guide):
    //   1. video.webkitSetMediaKeys(new WebKitMediaKeys('com.apple.fps.1_0'))
    //   2. Event 'webkitneedkey' fires with initData = [4B LE len][UTF-16LE skd://kid]
    //      (Safari computes this from EXT-X-KEY URI="skd://..."). PASS IT AS-IS.
    //   3. session = keys.createSession('video/mp4', initData)
    //   4. Event 'webkitkeymessage' delivers SPC bytes
    //   5. Send SPC to license server, receive CKC, call session.update(CKC)
    //
    // The server certificate is NOT injected into createSession — the license
    // server uses its own copy of the cert to mint the CKC. The SPC contains a
    // hash of the cert public key so server can identify which cert was used.
    if (hasWebKitKeys) {
      try {
        const wkKeys = new WebKitMediaKeys('com.apple.fps.1_0');

        // setServerCertificate — try multiple shapes (some Safari builds need it)
        let certApplied = false;
        if (typeof wkKeys.webkitSetServerCertificate === 'function') {
          try {
            const cr = wkKeys.webkitSetServerCertificate(serverCert);
            if (cr && typeof cr.then === 'function') await cr;
            _fpsLog('[FPS] WK cert set'); certApplied = true;
          } catch (ce) { _fpsLog('[FPS] WK cert err: ' + ce.message); }
        }
        if (!certApplied) {
          _fpsLog('[FPS] no setServerCertificate — relying on server-side cert');
        }

        fpsEl.webkitSetMediaKeys(wkKeys);
        _fpsLog('[FPS] WK set');

        // Remove stale listener (fpsEl is reused across tracks)
        if (fpsEl._wkNeedKey) fpsEl.removeEventListener('webkitneedkey', fpsEl._wkNeedKey);

        const _wkNeedKey = (ev) => {
          if (_keyInstalled || _fpsPending) return;
          _fpsPending = true;
          _fpsLog('[FPS] needkey');
          try {
            const initBuf = ev.initData instanceof Uint8Array ? ev.initData
              : new Uint8Array(ev.initData instanceof ArrayBuffer ? ev.initData : ev.initData.buffer);

            _fpsLog('[FPS] init[' + initBuf.length + '] ' +
              Array.from(initBuf.slice(0, 16)).map(b => b.toString(16).padStart(2, '0')).join(' '));

            // Diagnostic: extract keyId from UTF-16LE skd://kid (Safari format)
            let keyId = '';
            try {
              const u16 = new TextDecoder('utf-16le').decode(initBuf.slice(4));
              const i = u16.indexOf('skd://');
              if (i >= 0) keyId = u16.slice(i + 6).replace(/\x00.*$/, '');
            } catch (_) {}
            _fpsLog('[FPS] keyId=' + keyId.slice(0, 60));

            // CRITICAL: pass initBuf as-is. Safari knows the format. DO NOT
            // rebuild into legacy [kid_len BE][kid][cert_len BE][cert] — that's
            // QuickTime FairPlay format and iOS 13+ rejects it with null-code keyerr.
            const sess = wkKeys.createSession('video/mp4', initBuf);

            sess.addEventListener('webkitkeymessage', async (msg) => {
              try {
                _fpsLog('[FPS] SPC ' + (msg.message?.byteLength || 0) + 'B → CKC...');
                const spc = msg.message instanceof Uint8Array ? msg.message
                  : new Uint8Array(msg.message instanceof ArrayBuffer ? msg.message
                      : msg.message.buffer.slice(msg.message.byteOffset, msg.message.byteOffset + msg.message.byteLength));
                const ckc = await _doLicense(spc);
                _fpsLog('[FPS] CKC ' + ckc.length + 'B, calling update');
                sess.update(ckc);
                _keyInstalled = true;
                _fpsLog('[FPS] key OK');
                if (fpsEl.paused) fpsEl.play().catch(pe => _fpsLog('[FPS] resume: ' + pe.message));
              } catch (err) { _fpsPending = false; _fpsLog('[FPS] lic: ' + err.message); }
            });
            sess.addEventListener('webkitkeyerror', (ev2) => {
              _fpsPending = false;
              // WebKitMediaKeyError fields are on session.error, NOT on event
              const err = sess.error || ev2.target?.error || {};
              _fpsLog('[FPS] keyerr ' + JSON.stringify({
                code: err.code ?? null,
                sys: err.systemCode ?? null,
                msg: err.message ?? null,
              }));
            });
            sess.addEventListener('webkitkeyadded', () => {
              _fpsLog('[FPS] keyadded');
              if (fpsEl.paused) fpsEl.play().catch(pe => _fpsLog('[FPS] resume2: ' + pe.message));
            });
            _fpsLog('[FPS] session created');
          } catch (err) { _fpsPending = false; _fpsLog('[FPS] req err: ' + err.message); }
        };
        fpsEl._wkNeedKey = _wkNeedKey;
        fpsEl.addEventListener('webkitneedkey', _wkNeedKey);
      } catch (e) { _fpsLog('[FPS] WK init: ' + e.message); }
    }

    fpsEl.addEventListener('timeupdate', () => {
      const cur = fpsEl.currentTime, dur = fpsEl.duration;
      if (!dur || !isFinite(dur) || _seekDragging) return;
      const pct = (cur / dur * 100) + '%';
      const t = fmtDur(Math.floor(cur));
      ['pp-fill','pp-fill-big','fp-fill'].forEach(id => { const e = document.getElementById(id); if(e) e.style.width = pct; });
      ['pp-cur','pp-cur-big','fp-cur'].forEach(id => { const e = document.getElementById(id); if(e) e.textContent = t; });
      const thumb = document.getElementById('fp-thumb'); if (thumb) thumb.style.left = pct;
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

    const _diagT = setTimeout(() => {
      if (_keyInstalled) return;
      _fpsLog(`[FPS] timeout rs=${fpsEl.readyState} err=${fpsEl.error?.code || 0} ne=${fpsEl.networkState}`);
    }, 8000);
    fpsEl.addEventListener('playing', () => clearTimeout(_diagT), { once: true });

    fpsEl.volume = audioEl.volume || 1;
    fpsEl.src = item.url;
    fpsEl.load();
    if (window._WA?.ctx?.state === 'suspended') window._WA.ctx.resume().catch(() => {});
    // Non-blocking play() — DRM handshake is async; webkitkeyadded resumes
    fpsEl.play().catch(e => {
      _fpsLog('[FPS] play blocked: ' + e.message);
    });
    return;
  }

  // CTR = Widevine — delegate to original implementation
  if (_scDrmHlsOrig) return _scDrmHlsOrig(audioEl, item, playBtn, playBtnB);
};
