// ======================================================================
// Wrapper management UI
// Extracted from app.js (mechanical split — same global functions, no behaviour
// change). Loaded AFTER app.js in index.html, so it sees S/api/toast/etc.
// ======================================================================

// ── WRAPPER MANAGEMENT ────────────────────────────────────────

async function checkWrapperStatus() {
  // The Apple wrapper is an OWNER-side thing (guests download via the owner's
  // engines). /api/wrapper-status is not guest-allowed → a guest would get 403,
  // render running=undefined and flash a false "wrapper unavailable" banner.
  if (document.body.classList.contains('guest-mode')) return;
  try {
    const r = await fetch('/api/wrapper-status');
    const d = await r.json();
    _dockerAvailable = d.docker !== false;
    updateWrapperUI(d.running, d.port, d.docker, d.docker_msg, d.has_session);
    // sync radio buttons with current mode
    const mode = d.mode || 'docker-remote';
    const radio = document.querySelector(`input[name="wrapper-mode"][value="${mode}"]`);
    if(radio && !radio.checked) { radio.checked = true; _applyWrapperModeUI(mode, d); }
  } catch(e) {}
}

async function recheckWrapper() {
  await checkWrapperStatus();
  toast(t('s.wrapper_updated'));
}

// Health of the PUBLIC Apple wrapper-manager (wm.wol.moe). This is what the AMD
// engine decrypts through, and it periodically overloads (502 / gRPC Deadline).
// We surface it in its own topbar pill so the owner can tell "public is down"
// apart from "my local wrapper is off" — even while the local wrapper is active.
async function checkPublicWrapperStatus(manual) {
  if (document.body.classList.contains('guest-mode')) return;
  const pill = document.getElementById('public-wrapper-pill');
  if (!pill) return;
  if (manual) pill.innerHTML = '<div class="dot"></div>Public…';
  try {
    const r = await fetch('/api/amd/wrapper-status');
    const d = await r.json();
    const ok = !!d.ready && !d.error;
    pill.className = 'pill ' + (ok ? 'pill-ok' : 'pill-err');
    pill.innerHTML = '<div class="dot"></div>' + (ok ? 'Public ✓' : 'Public ✗');
    const inst = d.instance || 'wm.wol.moe';
    pill.title = ok
      ? `${inst} — ${t('s.pubw_ok')} (${d.client_count ?? '?'})`
      : `${inst} — ${t('s.pubw_down')}: ${d.error || 'not ready'}`;
    if (manual) toast(pill.title, ok ? 'var(--green)' : 'var(--red)');
  } catch (e) {
    pill.className = 'pill pill-warn';
    pill.innerHTML = '<div class="dot"></div>Public ?';
    pill.title = 'wm.wol.moe — ' + e.message;
  }
}

function updateWrapperUI(running, port, dockerOk, dockerMsg, hasSession) {
  const pill     = document.getElementById('wrapper-pill');
  const banner   = document.getElementById('wrapper-banner');
  const okBanner = document.getElementById('wrapper-ok-banner');
  const q        = S.config['quality'] || 'alac';
  const qdef     = QUALITIES.find(x => x.id === q) || QUALITIES[0];
  const needsWrapper = qdef && qdef.req === 'wrapper';
  const isAMDEngine  = (S.config?.engine || '') === 'amd';

  // Topbar pill — hide entirely when AMD is selected (public wrapper, no local Docker needed)
  if(pill) {
    if(isAMDEngine) {
      pill.style.display = 'none';
    } else {
      pill.style.display = '';
      if(running) {
        pill.className = 'pill pill-ok';
        pill.innerHTML = '<div class="dot"></div>Wrapper ✓';
      } else {
        pill.className = 'pill ' + (needsWrapper ? 'pill-err' : 'pill-warn');
        pill.innerHTML = '<div class="dot"></div>Wrapper offline';
      }
      pill.title   = port + ' — ' + (running ? t('s.wrapper_connected') : t('s.wrapper_not_running'));
      pill.onclick  = recheckWrapper;
      pill.style.cursor = 'pointer';
    }
  }

  // Docker status note in banner
  const dockerStat = document.getElementById('wb-docker-status');
  if(dockerStat)
    dockerStat.textContent = (dockerOk===false) ? ti('s.docker_status', {msg: dockerMsg || t('s.docker_not_found')}) : '';

  // wb-btn-start state
  const wbStart = document.getElementById('wb-btn-start');
  if(wbStart) {
    if(dockerOk===false) {
      wbStart.textContent=t('s.need_docker_dl');
      wbStart.onclick=()=>window.open('https://www.docker.com/products/docker-desktop/','_blank');
    } else {
      wbStart.textContent=t('s.start_short');
      wbStart.onclick=startWrapper;
    }
    wbStart.disabled=false;
  }

  // Docker command ports
  const decPort = S.config['decrypt-port'] || '127.0.0.1:10020';
  const m3uPort = S.config['m3u8-port']    || '127.0.0.1:20020';
  const decP = decPort.split(':').pop() || '10020';
  const m3uP = m3uPort.split(':').pop() || '20020';
  const cmdEl = document.getElementById('wrapper-cmd-text');
  if(cmdEl) cmdEl.textContent = `docker run -v ./rootfs/data:/app/rootfs/data -p ${decP}:10020 -p ${m3uP}:20020 -e args="${S.config['wrapper-apple-id']&&S.config['wrapper-password']?`-L ${S.config['wrapper-apple-id']}:*** -H 0.0.0.0`:'-H 0.0.0.0'}" ghcr.io/itouakirai/wrapper:x86`;

  // Banner: show when offline + not dismissed — but never when AMD is selected
  // (AMD uses its own public wrapper, local Docker is irrelevant)
  const shouldShow = !running && !_wrapperDismissed && !isAMDEngine;
  if(banner) {
    banner.style.display = shouldShow ? '' : 'none';
    // Auto-expand detail section if Apple ID not yet configured
    if(shouldShow && !_detailAutoOpened) {
      _detailAutoOpened = true;
      const detail = document.getElementById('wrapper-detail');
      const lbl    = document.getElementById('wrapper-expand-lbl');
      if(detail && detail.style.display==='none'){
        detail.style.display='';
        if(lbl) lbl.textContent='? ▲';
      }
    }
  }

  // Start/Stop button
  const btnSW     = document.getElementById('btn-start-wrapper');
  const btnRelogin = document.getElementById('btn-relogin-wrapper');
  const sessionBar = document.getElementById('wrapper-session-bar');
  const sessionIcon = document.getElementById('wrapper-session-icon');
  const sessionText = document.getElementById('wrapper-session-text');
  if(btnSW) {
    if(running) {
      btnSW.textContent = t('s.stop_wrapper_btn');
      btnSW.style.cssText = 'flex-shrink:0;background:rgba(226,75,74,.15);border:1px solid rgba(226,75,74,.3);border-radius:8px;padding:6px 14px;font-size:11px;font-weight:700;color:var(--danger);cursor:pointer;font-family:var(--font);white-space:nowrap';
      btnSW.onclick = stopWrapper;
    } else {
      btnSW.textContent = _dockerAvailable ? t('s.start_wrapper_btn') : t('s.need_docker');
      btnSW.style.cssText = 'flex-shrink:0;background:rgba(62,207,170,.15);border:1px solid rgba(62,207,170,.25);border-radius:8px;padding:6px 14px;font-size:11px;font-weight:700;color:var(--green);cursor:pointer;font-family:var(--font);white-space:nowrap';
      btnSW.onclick = startWrapper;
    }
  }
  // Relogin: только когда не запущен (нет смысла перелогиниваться пока работает)
  if(btnRelogin) btnRelogin.style.display = running ? 'none' : '';
  // Session status bar
  if(sessionIcon && sessionText) {
    if(running) {
      sessionIcon.textContent = '🟢';
      sessionText.textContent = ti('s.sess_running', {state: hasSession ? t('s.sess_saved_mark') : t('s.sess_none')});
      sessionText.style.color = 'var(--green)';
    } else if(hasSession) {
      sessionIcon.textContent = '💾';
      sessionText.textContent = t('s.sess_saved_no2fa');
      sessionText.style.color = 'var(--text)';
    } else {
      sessionIcon.textContent = '❌';
      sessionText.textContent = t('s.sess_none_auth');
      sessionText.style.color = 'var(--muted)';
    }
  }

  // Flash green when comes back online
  if(running && _wrapperWasDown) {
    _wrapperDismissed = false;
    if(okBanner){ okBanner.style.display=''; setTimeout(()=>okBanner.style.display='none', 3500); }
    _wrapperWasDown = false;
  }
  if(!running) _wrapperWasDown = true;
}

async function startWrapper() {
  if(_wrapperStarting) return;
  _wrapperStarting = true;
  const btn      = document.getElementById('btn-start-wrapper');
  const startDiv = document.getElementById('wrapper-starting');
  const msgEl    = document.getElementById('wrapper-start-msg');
  if(btn){ btn.disabled=true; btn.textContent=t('s.starting_short'); }
  if(startDiv) startDiv.style.display='';
  if(msgEl) msgEl.textContent=t('s.connecting_docker');
  // Show console so user sees logs
  const cNav = document.querySelector('.nav-item[data-view="console"]');
  if(cNav) showView('console', cNav);
  try {
    const r = await fetch('/api/wrapper/start', {method:'POST'});
    const d = await r.json();
    if(d.ok) {
      if(msgEl) msgEl.textContent = d.msg;
      toast(d.msg, 'var(--green)');
      let tries=0;
      const poll = setInterval(async ()=>{ tries++; await checkWrapperStatus(); if(tries>90) clearInterval(poll); }, 1000);
    } else {
      toast('✗ ' + d.msg, 'var(--red)');
      appendLog('[wrapper] ✗ ' + d.msg, 'error');
      if(!_dockerAvailable) {
        appendLog('[wrapper] ' + t('s.docker_not_installed'), 'warn');
        appendLog('[wrapper] https://www.docker.com/products/docker-desktop/', 'warn');
        appendLog('[wrapper] ' + t('s.docker_after_install'), 'warn');
        // Expand instructions
        const det = document.getElementById('wrapper-detail');
        if(det) det.style.display='';
      }
    }
  } catch(e) {
    toast(t('err.generic') + ': ' + e.message, 'var(--red)');
  } finally {
    _wrapperStarting = false;
    if(btn){ btn.disabled=false; }
    if(startDiv) startDiv.style.display='none';
  }
}

async function wrapperRelogin() {
  const confirmed = confirm(t('s.relogin_confirm'));
  if (!confirmed) return;
  const btn = document.getElementById('btn-relogin-wrapper');
  if(btn){ btn.disabled=true; btn.textContent=t('s.relogin_progress'); }
  const cNav = document.querySelector('.nav-item[data-view="console"]');
  if(cNav) showView('console', cNav);
  try {
    const r = await fetch('/api/wrapper/relogin', {method:'POST'});
    const d = await r.json();
    toast(d.ok ? t('s.2fa_sent') : ('✗ ' + d.msg),
          d.ok ? 'var(--orange)' : 'var(--red)');
    if(d.ok) loadWrapperSessionStatus();
  } catch(e) {
    toast(t('err.generic') + ': ' + e.message, 'var(--red)');
  } finally {
    if(btn){ btn.disabled=false; btn.textContent=t('s.relogin_btn'); }
  }
}

async function loadWrapperSessionStatus() {
  // Запрашиваем полный статус — он включает has_session и обновит все элементы
  await checkWrapperStatus();
}

async function stopWrapper() {
  await fetch('/api/wrapper/stop', {method:'POST'});
  toast(t('s.wrapper_stopped'), 'var(--orange)');
  setTimeout(checkWrapperStatus, 800);
}

function showWrapper2FAModal() {
  if(document.getElementById('wrapper-2fa-modal')) return;
  const modal = document.createElement('div');
  modal.id = 'wrapper-2fa-modal';
  modal.style.cssText = 'position:fixed;inset:0;z-index:9999;display:flex;align-items:center;justify-content:center;background:rgba(0,0,0,.75);backdrop-filter:blur(4px)';
  modal.innerHTML = `<div style="background:var(--surface,#1c1c1e);border:1px solid rgba(255,255,255,.1);border-radius:16px;padding:24px;width:340px;max-width:90vw">
    <div style="font-size:15px;font-weight:700;color:var(--text);margin-bottom:6px">${t('wr.2fa_title')}</div>
    <div style="font-size:12px;color:var(--muted,#888);margin-bottom:16px">${t('wr.2fa_sub')}</div>
    <input id="wrapper-2fa-input" type="text" inputmode="numeric" maxlength="6" placeholder="000000"
      style="width:100%;padding:10px 12px;background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.15);border-radius:9px;color:var(--text);font-size:22px;font-family:var(--mono,monospace);text-align:center;letter-spacing:6px;box-sizing:border-box;outline:none"
      onkeydown="if(event.key==='Enter') submitWrapper2FA()">
    <div style="display:flex;gap:8px;margin-top:14px">
      <button onclick="submitWrapper2FA()" style="flex:1;padding:10px;background:#0a84ff;border:none;border-radius:9px;cursor:pointer;color:#fff;font-weight:600;font-size:13px;font-family:var(--font)">${t('wr.confirm')}</button>
      <button onclick="document.getElementById('wrapper-2fa-modal').remove()" style="padding:10px 16px;background:transparent;border:1px solid rgba(255,255,255,.1);border-radius:9px;cursor:pointer;font-size:13px;color:var(--muted,#888);font-family:var(--font)">${t('btn.cancel')}</button>
    </div>
  </div>`;
  document.body.appendChild(modal);
  setTimeout(()=>{ const i=document.getElementById('wrapper-2fa-input'); if(i) i.focus(); },50);
}

async function submitWrapper2FA() {
  const inp = document.getElementById('wrapper-2fa-input');
  if(!inp) return;
  const code = inp.value.replace(/\D/g,'');
  if(code.length < 6) { toast(t('w.enter_2fa'),'var(--orange)'); return; }
  try {
    const r = await fetch('/api/wrapper/2fa', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({code})});
    const d = await r.json();
    if(d.ok) { toast(t('w.code_sent'),'var(--green)'); document.getElementById('wrapper-2fa-modal').remove(); }
    else toast('✗ '+d.msg,'var(--red)');
  } catch(e) { toast(t('t.error_c')+e.message,'var(--red)'); }
}

function appendWrapperLog(text) {
  const logEl  = document.getElementById('wb-log');
  const logTxt = document.getElementById('wb-log-text');
  if(!logTxt) return;
  if(logEl) logEl.style.display = '';
  logTxt.textContent += text + '\n';
  logTxt.scrollTop = logTxt.scrollHeight;
}

function wrapperPull() {
  const mode   = (S.config && S.config['wrapper-mode']) || 'docker-remote';
  const logEl  = document.getElementById('wb-log');
  const logTxt = document.getElementById('wb-log-text');
  if(logEl)  logEl.style.display = '';
  if(mode === 'docker-local') {
    if(logTxt) logTxt.textContent = t('wr.build_local') + '\n';
    toast('Building local image…', 'var(--blue)');
    fetch('/api/wrapper/build', {method:'POST'});
  } else if(mode === 'non-docker') {
    toast(t('w.nondocker'), 'var(--muted)');
  } else {
    if(logTxt) logTxt.textContent = t('wr.pull_image') + '\n';
    toast(t('w.pulling'), 'var(--blue)');
    fetch('/api/wrapper/pull', {method:'POST'});
  }
}

function _applyWrapperModeUI(mode, statusData) {
  const buildSection = document.getElementById('wrapper-build-section');
  const wslWarn      = document.getElementById('wrapper-wsl-warn');
  const wbPull       = document.getElementById('wb-btn-pull');
  if(buildSection) buildSection.style.display = mode === 'docker-local' ? '' : 'none';
  if(wslWarn)      wslWarn.style.display      = mode === 'non-docker'   ? '' : 'none';
  if(wbPull) {
    if(mode === 'docker-local')  { wbPull.textContent = '🔨 Build'; }
    else if(mode === 'non-docker') { wbPull.style.display = 'none'; }
    else { wbPull.textContent = '⬇ Pull'; wbPull.style.display = ''; }
  }
}

async function onWrapperModeChange(mode) {
  await saveSetting('wrapper-mode', mode);
  _applyWrapperModeUI(mode, null);
}

async function wrapperBuild() {
  const btn    = document.getElementById('btn-wrapper-build');
  const status = document.getElementById('wrapper-build-status');
  if(btn) { btn.disabled = true; btn.textContent = t('wr.building'); }
  if(status) status.textContent = t('wr.see_banner');
  const cNav = document.querySelector('.nav-item[data-view="console"]');
  if(cNav) showView('console', cNav);
  fetch('/api/wrapper/build', {method:'POST'});
  setTimeout(()=>{ if(btn) { btn.disabled=false; btn.textContent='🔨 Build local image'; } }, 60000);
}



function wrapperStart() { startWrapper(); }
function wrapperStop()  { stopWrapper();  }

function toggleWrapperBanner() {
  const detail = document.getElementById('wrapper-detail');
  const lbl    = document.getElementById('wrapper-expand-lbl');
  if(!detail) return;
  const open = detail.style.display !== 'none';
  detail.style.display = open ? 'none' : '';
  if(lbl) lbl.textContent = open ? t('wr.instr_open') : t('wr.instr_close');
}

async function toggleWrapperLogs() {
  const area = document.getElementById('wrapper-log-area');
  const txt  = document.getElementById('wrapper-log-content');
  if(!area) return;
  if(area.style.display !== 'none') { area.style.display='none'; return; }
  area.style.display='';
  if(txt) txt.textContent=t('b.loading');
  try {
    const d = await (await fetch('/api/wrapper/logs')).json();
    if(!txt) return;
    txt.textContent = d.logs || t('wr.no_output');
    txt.scrollTop = txt.scrollHeight;
    const bad = /incorrect|unauthorized|error|failed|wrong/i.test(d.logs||'');
    const ok  = /login|authenticated|success|started/i.test(d.logs||'');
    txt.style.color = bad ? 'var(--orange)' : ok ? 'var(--green)' : 'var(--muted)';
  } catch(e) { if(txt) txt.textContent=t('ui.err_pfx')+e.message; }
}

function dismissWrapperBanner() {
  _wrapperDismissed = true;
  const banner = document.getElementById('wrapper-banner');
  if(banner) banner.style.display = 'none';
}

function switchToAAC() {
  selectQuality('aac');
  _wrapperDismissed = true;
  const banner = document.getElementById('wrapper-banner');
  if(banner) banner.style.display = 'none';
  toast(t('w.q_aac'), 'var(--orange)');
}

function copyWrapperCmd() {
  const cmd = document.getElementById('wrapper-cmd-text')?.textContent?.trim();
  if(cmd){ navigator.clipboard.writeText(cmd); toast(t('t.copied')); }
  const btn = document.getElementById('wrapper-cmd-copy');
  if(btn){ btn.textContent='✓'; setTimeout(()=>btn.textContent='⎘ Copy',2000); }
}

