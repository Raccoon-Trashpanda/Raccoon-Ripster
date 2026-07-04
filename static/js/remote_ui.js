// ======================================================================
// Remote access + Serveo tunnel UI
// Extracted from app.js (mechanical split — same global functions, no behaviour
// change). Loaded AFTER app.js in index.html, so it sees S/api/toast/etc.
// ======================================================================

// ── Remote access ─────────────────────────────────────────────

function updateRemoteUI(enabled, publicUrl, activeSessions) {
  const pill  = document.getElementById('remote-status-pill');
  const cnt   = document.getElementById('remote-sessions-count');
  const startBtn = document.getElementById('remote-start-btn');
  const stopBtn  = document.getElementById('remote-stop-btn');
  const urlInput = document.getElementById('remote-public-url');
  if (pill) {
    pill.textContent  = enabled ? t('remote.on') : t('remote.off');
    pill.style.background = enabled ? 'rgba(34,197,94,.15)' : 'rgba(252,60,68,.15)';
    pill.style.color      = enabled ? '#22c55e' : 'var(--red)';
  }
  if (cnt) cnt.textContent = activeSessions > 0 ? `${activeSessions} ${t('remote.sessions')||'активных сессий'}` : '';
  if (startBtn) startBtn.style.display = enabled ? 'none'  : '';
  if (stopBtn)  stopBtn.style.display  = enabled ? '' : 'none';
  if (urlInput && publicUrl) urlInput.value = publicUrl;
}

async function loadRemoteStatus() {
  try {
    const r = await fetch('/api/remote/status');
    if (!r.ok) return;
    const d = await r.json();
    updateRemoteUI(d.enabled, d.public_url, d.active_links);
  } catch(e) {}
}

async function remoteStart() {
  const pub = (document.getElementById('remote-public-url')?.value || '').trim();
  try {
    const r = await api('POST', '/api/remote/start', { public_url: pub });
    if (r.ok) {
      updateRemoteUI(true, r.public_url, 0);
      toast(t('rm.enabled'), '#22c55e');
      await loadAdminLinks();
    } else {
      toast(r.detail || 'Error', 'var(--red)');
    }
  } catch(e) { toast(t('err.generic') + ': ' + e.message, 'var(--red)'); }
}

async function remoteStop() {
  try {
    const r = await api('POST', '/api/remote/stop');
    if (r.ok) {
      updateRemoteUI(false, '', 0);
      toast(ti('rm.stopped',{n:r.revoked}), 'var(--red)');
      await loadAdminLinks();
    } else {
      toast(r.detail || 'Error', 'var(--red)');
    }
  } catch(e) { toast(t('err.generic') + ': ' + e.message, 'var(--red)'); }
}

async function saveRemoteUrl() {
  const pub = (document.getElementById('remote-public-url')?.value || '').trim();
  if (!pub) return;
  try { await api('POST', '/api/remote/start', { public_url: pub }); } catch(e) {}
}

// ── Serveo tunnel ──────────────────────────────────────────────────────────
function updateTunnelUI(running, connecting, url) {
  const pill     = document.getElementById('tunnel-status-pill');
  const urlRow   = document.getElementById('tunnel-url-row');
  const urlInput = document.getElementById('tunnel-url-display');
  const startBtn = document.getElementById('tunnel-start-btn');
  const stopBtn  = document.getElementById('tunnel-stop-btn');
  if (pill) {
    if (connecting) {
      pill.textContent = '⏳ ' + t('rm.connecting');
      pill.style.background = 'rgba(234,179,8,.15)'; pill.style.color = '#eab308';
    } else if (running) {
      pill.textContent = '● ' + t('rm.active');
      pill.style.background = 'rgba(34,197,94,.15)'; pill.style.color = '#22c55e';
    } else {
      pill.textContent = '● ' + t('rm.off_word');
      pill.style.background = 'rgba(252,60,68,.15)'; pill.style.color = 'var(--red)';
    }
  }
  if (urlRow)   urlRow.style.display   = url ? '' : 'none';
  if (urlInput && url) urlInput.value  = url;
  if (startBtn) { startBtn.style.display = running || connecting ? 'none' : ''; startBtn.textContent = '▶ ' + t('rm.start'); startBtn.disabled = false; }
  if (stopBtn)  stopBtn.style.display  = running || connecting ? '' : 'none';
  if (url) {
    const pubInput = document.getElementById('remote-public-url');
    if (pubInput) pubInput.value = url;
  }
}

async function tunnelStart() {
  const startBtn = document.getElementById('tunnel-start-btn');
  if (startBtn) { startBtn.textContent = '⏳…'; startBtn.disabled = true; }
  updateTunnelUI(false, true, '');
  try {
    const r = await api('POST', '/api/tunnel/start', {});
    if (!r.ok) {
      updateTunnelUI(false, false, '');
      toast(t('r.tun_err') + (r.error || '?'), 'var(--red)');
    }
    // URL arrives via WebSocket tunnel_status event
  } catch(e) {
    updateTunnelUI(false, false, '');
    toast(t('err.generic') + ': ' + e.message, 'var(--red)');
  }
}

async function tunnelStop() {
  try {
    await api('POST', '/api/tunnel/stop');
    updateTunnelUI(false, false, '');
    toast(t('r.tun_stop'));
  } catch(e) { toast(t('err.generic') + ': ' + e.message, 'var(--red)'); }
}

async function loadTunnelStatus() {
  try {
    const r = await fetch('/api/tunnel/status');
    if (!r.ok) return;
    const d = await r.json();
    updateTunnelUI(d.running, d.connecting, d.url || '');
  } catch(e) {}
}

async function createGuestLink() {
  const label     = document.getElementById('gl-label')?.value.trim() || '';
  const qtype     = document.getElementById('gl-quota-type')?.value || 'unlimited';
  const qlimit    = parseInt(document.getElementById('gl-quota-val')?.value) || 20;
  const tokenMode = document.getElementById('gl-token-mode')?.value || 'owner';
  const quota     = qtype === 'unlimited' ? {type:'unlimited'} : {type:qtype, limit:qlimit};
  try {
    const r = await api('POST', '/api/admin/links/create', {label, quota, token_mode: tokenMode});
    if (r.ok) {
      const box = document.getElementById('gl-new-link');
      if (box) { box.style.display = 'block'; setTimeout(() => { box.style.display = 'none'; }, 3000); }
      document.getElementById('gl-label').value = '';
      await loadAdminLinks();
    } else {
      toast(r.detail || t('rm.link_err'), 'var(--red)');
    }
  } catch(e) { toast(t('err.generic') + ': ' + e.message, 'var(--red)'); }
}

function copyGuestLink() {
  const url = document.getElementById('gl-new-link-url')?.textContent || '';
  if (!url) return;
  navigator.clipboard.writeText(url).then(() => toast(t('toast.link_copied'))).catch(() => {
    const ta = document.createElement('textarea');
    ta.value = url; document.body.appendChild(ta); ta.select();
    document.execCommand('copy'); document.body.removeChild(ta);
    toast(t('toast.link_copied'));
  });
}

async function revokeGuestLink(token) {
  try {
    await api('POST', '/api/admin/links/revoke', {token});
    await loadAdminLinks();
    toast(t('r.link_revoked'));
  } catch(e) { toast(t('err.generic') + ': ' + e.message, 'var(--red)'); }
}

async function toggleTokenMode(token, newMode) {
  try {
    await api('POST', '/api/admin/links/token-mode', {token, token_mode: newMode});
    await loadAdminLinks();
  } catch(e) { toast(t('err.generic') + ': ' + e.message, 'var(--red)'); }
}

