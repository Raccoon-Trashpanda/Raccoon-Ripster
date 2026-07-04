// ======================================================================
// Ripster self-update UI
// Extracted from app.js (mechanical split — same global functions, no behaviour
// change). Loaded AFTER app.js in index.html, so it sees S/api/toast/etc.
// ======================================================================

// ── Ripster self-update ────────────────────────────────────────────────────
let _ripsterUpdate = null;   // last /api/update/check result
let _updateApplying = false;

async function checkRipsterUpdate(silent){
  const st = document.getElementById('ripster-update-status');
  const verLine = document.getElementById('ripster-version-line');
  const applyBtn = document.getElementById('btn-update-apply');
  const log = document.getElementById('ripster-update-changelog');
  if(!silent && st){ st.textContent = t('su.checking'); st.style.color = 'var(--muted)'; }
  try {
    const d = await api('GET','/api/update/check');
    _ripsterUpdate = d;
    if(verLine) verLine.textContent = d.current ? ('v'+d.current) : '';
    if(!d.ok){
      if(st){ st.textContent = '✗ ' + (d.error || t('su.check_fail')); st.style.color = '#c084a0'; }
      if(applyBtn) applyBtn.style.display = 'none';
      updateSetupBadge();
      return d;
    }
    if(d.available){
      if(st){
        const _lat = esc(String(d.latest||'').replace(/^v/i,''));   // tag is "v3.0.25" → strip leading v (no "vv")
        const _cur = esc(String(d.current||'').replace(/^v/i,''));
        const dl = d.url ? `<a href="${esc(d.url)}" target="_blank" rel="noopener" style="color:#30d158;font-weight:700;text-decoration:underline">↓ ${t('su.dl_install')} v${_lat}</a>` : '';
        st.innerHTML = ti('su.avail',{latest:_lat,cur:_cur})
          + '<br><span style="color:var(--muted);font-size:11px">' + t('su.help') + (dl ? '' : t('su.need_installer')) + '</span>'
          + (dl ? ` ${dl}` : '');
        st.style.color = 'var(--text)';
      }
      if(applyBtn) applyBtn.style.display = '';
      if(log && d.changelog){ log.style.display = 'block'; log.textContent = d.changelog; }
    } else {
      if(st){ st.textContent = ti('su.latest',{v:d.current}); st.style.color = '#30d158'; }
      if(applyBtn) applyBtn.style.display = 'none';
      if(log) log.style.display = 'none';
    }
    updateSetupBadge();
    return d;
  } catch(e){
    if(!silent && st){ st.textContent = '✗ ' + e.message; st.style.color = '#c084a0'; }
    return null;
  }
}

async function applyRipsterUpdate(){
  if(_updateApplying) return;
  if(!_ripsterUpdate || !_ripsterUpdate.available){ toast(t('su.check_first'),'var(--orange)'); return; }
  if(!confirm(ti('su.confirm',{v:_ripsterUpdate.latest}))) return;
  _updateApplying = true;
  const st = document.getElementById('ripster-update-status');
  const applyBtn = document.getElementById('btn-update-apply');
  if(applyBtn){ applyBtn.disabled = true; applyBtn.textContent = '⏳ ' + t('su.updating'); }
  if(st){ st.textContent = '⏳ ' + t('su.applying'); st.style.color = 'var(--orange)'; }
  try {
    const d = await api('POST','/api/update/apply');
    if(d && d.ok){
      // Restart in place: /api/restart respawns the NEW on-disk code, which takes
      // over the port (server-side _takeover_stale_server). Then poll for the new
      // version and hard-reload so the new UI loads too — no manual close needed.
      const newVer = (d.latest || (_ripsterUpdate && _ripsterUpdate.latest) || '').replace(/^v/,'');
      if(st){ st.innerHTML = '✅ ' + t('su.done'); st.style.color = '#30d158'; }
      _ripsterUpdate = null;
      toast(t('su.applying'),'var(--green)', '', 8000);
      await fetch('/api/restart', {method:'POST'}).catch(()=>{});
      _waitForNewServerThenReload(newVer);
    } else {
      const rb = d && d.rolled_back ? ' ' + t('su.rolled_back') : '';
      if(st){ st.textContent = ti('su.fail',{stage:(d&&d.stage)||'?',err:(d&&d.error)||'?'})+rb; st.style.color = '#c084a0'; }
    }
  } catch(e){
    if(st){ st.textContent = '✗ ' + e.message; st.style.color = '#c084a0'; }
  } finally {
    _updateApplying = false;
    if(applyBtn){ applyBtn.disabled = false; applyBtn.textContent = '⬆️ ' + t('su.update_now'); }
    updateSetupBadge();
  }
}

// After an update we restart the server in place; poll until the NEW version is
// answering (or the server bounced) and then hard-reload so the new UI loads too.
async function _waitForNewServerThenReload(newVer){
  let downSeen = false;
  for(let i=0;i<90;i++){                       // up to ~90s
    await new Promise(r=>setTimeout(r,1000));
    try{
      const r = await fetch('/api/ping', {cache:'no-store'});
      if(r.ok){
        let v = '';
        try { v = String(((await r.json())||{}).version||''); } catch(e){}
        if((newVer && v === newVer) || downSeen){ location.reload(); return; }
      } else { downSeen = true; }
    }catch(e){ downSeen = true; }              // server went down = restart in progress
  }
  location.reload();                           // fallback — reload regardless
}

// Show the existing "restart required" banner with a custom reason.
function showRestartBanner(reason){
  const b = document.getElementById('restart-banner');
  const r = document.getElementById('restart-reason');
  if(r && reason) r.textContent = reason;
  if(b) b.style.display = 'block';
}

function setOverall(label, pct){
  const l=document.getElementById('setup-overall-label'), p=document.getElementById('setup-overall-pct'), b=document.getElementById('setup-overall-bar');
  if(l) l.textContent = label; if(p) p.textContent = pct+'%'; if(b) b.style.width = pct+'%';
}

// Resolve when a given WS message type arrives (consumed in the WS switch), or time out.
function waitForWs(type, timeoutMs){
  return new Promise((resolve,reject)=>{
    const to = setTimeout(()=>{ _wsWaiters.delete(type); reject(new Error('timeout')); }, timeoutMs||60000);
    _wsWaiters.set(type, ()=>{ clearTimeout(to); _wsWaiters.delete(type); resolve(); });
  });
}

// Install ONE component (used by both the bulk run and each row's own button).
// Sets s.running/installed/error and renders; returns nothing. Caller owns
// _activeSetupKey, console clearing and the final checkTools().
async function _runComponentInstall(c) {
  const s = setupCompState[c.key] = setupCompState[c.key] || {};
  s.running = true; s.error = false; s.pct = 0;
  renderChecklist();
  try {
    if(c.wizard){
      const r = await api('POST', c.endpoint);
      if(window.toast) toast(r&&r.ok?(r.msg||t('su.wizard_open')):('✗ '+((r&&r.error)||t('t.error'))), r&&r.ok?c.color:'var(--red)', 7000);
      s.running=false; s.pct=100;              // wizard runs in its own window
    } else if(c.wsdone){
      await api('POST', c.endpoint);            // fire-and-forget; wait for completion WS
      await waitForWs(c.wsdone, 900000).catch(()=>{});
      s.running=false; s.pct=100; s.installed=true;
    } else {
      const r = await api('POST', c.endpoint);  // await-style component endpoint
      s.running=false; s.pct=100; s.installed = !!(r&&r.ok);
      if(r && !r.ok){ s.error = true; if(window.toast) toast(c.label+': '+(r.error||t('t.error')),'var(--red)'); }
    }
  } catch(e){ s.running=false; s.error=true; if(window.toast) toast(c.label+': '+((e&&e.message)||e),'var(--red)'); }
  renderChecklist();
}

// Install a single component from its own row button.
async function installOne(key) {
  if(setupRunning) return;
  const c = SETUP_COMPONENTS.find(x => x.key === key);
  if(!c) return;
  setupRunning = true;
  clearSetupConsole();
  const setupNav = document.querySelector('.nav-item[data-view="setup"]');
  if(setupNav) showView('setup', setupNav);
  _activeSetupKey = key;
  renderChecklist();          // disable other buttons while running
  await _runComponentInstall(c);
  _activeSetupKey = null;
  setupRunning = false;
  checkTools();
}

async function installSelected() {
  if(setupRunning) return;
  const sel = SETUP_COMPONENTS.filter(c => (setupCompState[c.key]||{}).checked);
  if(!sel.length){ toast(t('su.tick'),'var(--orange)'); return; }
  setupRunning = true;
  const btn = document.getElementById('btn-setup');
  if(btn){ btn.disabled = true; btn.textContent = '⏳ ' + t('setup.st_installing'); }
  const overall = document.getElementById('setup-overall');
  if(overall) overall.style.display = 'block';
  clearSetupConsole();
  const setupNav = document.querySelector('.nav-item[data-view="setup"]');
  if(setupNav) showView('setup', setupNav);
  let done = 0;
  for(const c of sel){
    _activeSetupKey = c.key;
    setOverall(c.label, Math.round(done/sel.length*100));
    await _runComponentInstall(c);
    done++;
    setOverall(c.label, Math.round(done/sel.length*100));
  }
  _activeSetupKey = null;
  setOverall(t('su.done_word')+' ✓', 100);
  if(btn){ btn.disabled=false; btn.textContent='⚡ ' + t('su.install_sel'); }
  setupRunning = false;
  checkTools();
}

function appendSetupLog(entry) {
  // Write to Setup console
  const out = document.getElementById('setup-console');
  if(out) {
    const line = document.createElement('div');
    line.className = 'log-' + (entry.level || 'info');
    line.textContent = `[${entry.ts}] ${entry.text}`;
    out.appendChild(line);
    if(document.getElementById('setup-autoscroll')?.checked) out.scrollTop = out.scrollHeight;
  }
  // Also mirror to main console
  appendLog(`[SETUP] ${entry.text}`, entry.level || 'info');
  // Route a download "NN%" line to the active checklist item's progress bar.
  if(typeof _activeSetupKey !== 'undefined' && _activeSetupKey) {
    const m = /(^|\s)(\d{1,3})%\s/.exec(entry.text || '');
    if(m) {
      const s = setupCompState[_activeSetupKey];
      const v = Math.min(100, parseInt(m[2], 10));
      if(s && v > (s.pct || 0)) { s.pct = v; renderChecklist(); }
    }
  }
}

function clearSetupConsole() {
  const el = document.getElementById('setup-console');
  if(el) el.innerHTML = '';
}

// Collapse / expand the Setup live-console. When collapsed the wrapper stops
// claiming the flex space so the panels above breathe; when open it fills the rest.
function toggleSetupConsole() {
  const con   = document.getElementById('setup-console');
  const wrap  = document.getElementById('setup-console-wrap');
  const caret = document.getElementById('setup-console-caret');
  if(!con) return;
  const hidden = con.style.display === 'none';
  con.style.display = hidden ? '' : 'none';
  if(wrap)  wrap.style.flex = hidden ? '1' : '0 0 auto';
  if(caret) caret.style.transform = hidden ? '' : 'rotate(-90deg)';
}

async function restartApp() {
  await fetch('/api/restart', {method:'POST'}).catch(()=>{});
  toast('Restarting… reconnecting in 3s', 'var(--orange)');
  setTimeout(() => { location.reload(); }, 3500);
}


