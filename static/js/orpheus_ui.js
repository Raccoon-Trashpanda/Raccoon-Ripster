// ======================================================================
// OrpheusDL (Spotify) setup UI
// Extracted from app.js (mechanical split — same global functions, no behaviour
// change). Loaded AFTER app.js in index.html, so it sees S/api/toast/etc.
// ======================================================================

// ── OrpheusDL (Spotify) ───────────────────────────────────────────
async function loadOrpheusStatus() {
  const r = await api('GET', '/api/orpheus/status').catch(()=>null);
  const badge   = document.getElementById('orp-badge');
  const authBar = document.getElementById('orp-auth-bar');
  const authDetail = document.getElementById('orp-auth-detail');
  const toggle  = document.getElementById('s-orp-mode');
  const qualSel = document.getElementById('s-orp-quality');
  if(!r) return;

  if(badge) {
    if(r.installed && r.authenticated) {
      badge.textContent = t('orp.connected');
      badge.style.background = 'rgba(62,207,170,.15)';
      badge.style.color = 'var(--green)';
    } else if(r.installed) {
      badge.textContent = t('orp.installed');
      badge.style.background = 'rgba(10,132,255,.15)';
      badge.style.color = '#0a84ff';
    } else {
      badge.textContent = t('orp.not_installed');
      badge.style.background = 'rgba(255,255,255,.07)';
      badge.style.color = 'var(--muted)';
    }
  }
  if(toggle) toggle.checked = (r.mode === 'orpheus_spotify');
  if(qualSel && r.quality) qualSel.value = r.quality;
  const mp3Tog = document.getElementById('s-orp-mp3');
  if(mp3Tog) mp3Tog.checked = S.config['orpheus-convert-mp3'] === true;

  const loginSec = document.getElementById('orp-login-section');
  if(authBar && loginSec) {
    if(r.authenticated) {
      authBar.style.display = 'flex';
      loginSec.style.display = 'none';
      if(authDetail) authDetail.textContent = r.username ? r.username : t('orp.sp_account');
    } else {
      authBar.style.display = 'none';
      loginSec.style.display = '';
    }
  }
}

async function saveSpotifyToken() {
  const ta = document.getElementById('s-sp-token-blob');
  const st = document.getElementById('sp-token-status');
  const blob = (ta && ta.value || '').trim();
  if (!blob) { if(st){st.textContent='вставь заголовки'; st.style.color='var(--muted)';} return; }
  if (st) { st.textContent='сохраняю…'; st.style.color='var(--muted)'; }
  try {
    const r = await api('POST', '/api/admin/spotify-token', { blob });
    if (r && r.ok) {
      if (st) { st.textContent='✓ обновлён: ' + ((r.updated||[]).join(', ')); st.style.color='var(--green)'; }
      if (ta) ta.value='';
    } else if (st) { st.textContent='✗ ' + ((r && r.error) || 'токен не найден'); st.style.color='#ff453a'; }
  } catch (e) {
    if (st) { st.textContent='✗ ' + (e.message || e); st.style.color='#ff453a'; }
  }
}

let _spTokPoll = null;
async function loadSpotifyTokenStatus() {
  const freshEl = document.getElementById('sp-tok-fresh');
  const logEl = document.getElementById('sp-push-log');
  try {
    const r = await api('GET', '/api/admin/spotify-token-status');
    if (freshEl) {
      const a = r.bearer_age_min;
      if (a == null) { freshEl.textContent = '⚪ нет токена'; freshEl.style.color = 'var(--muted)'; }
      else if (r.fresh) { freshEl.textContent = '🟢 свежий (' + a + ' мин)'; freshEl.style.color = 'var(--green)'; }
      else { freshEl.textContent = '🔴 протух (' + a + ' мин)'; freshEl.style.color = '#ff453a'; }
    }
    if (logEl) {
      const log = r.log || [];
      logEl.innerHTML = log.length
        ? log.map(e => `${e.time||''} ${e.status||''}${e.detail ? (' · ' + e.detail) : ''}`).join('<br>')
        : 'Пушей пока нет. Открой Spotify-таб и поиграй трек — расширение пришлёт токен.';
    }
  } catch (e) {
    if (logEl) logEl.textContent = '✗ ' + (e.message || e);
  }
}
function startSpotifyTokenPoll() {
  loadSpotifyTokenStatus();
  if (_spTokPoll) clearInterval(_spTokPoll);
  _spTokPoll = setInterval(loadSpotifyTokenStatus, 20000);
}
function stopSpotifyTokenPoll() { if (_spTokPoll) { clearInterval(_spTokPoll); _spTokPoll = null; } }

// Token expiry badge in the per-service settings tabs (computed from the real
// token, e.g. Tidal's JWT `exp`). One endpoint returns all services; each tab
// renders its own `#<svc>-tok-expiry` element. Tidal first; others follow.
async function loadTokenExpiry(svc) {
  const el = document.getElementById(svc + '-tok-expiry');
  if (!el) return;
  try {
    const r = await api('GET', '/api/admin/token-expiry');
    const t = r && r[svc];
    if (!t) { el.textContent = '⚪ токен не распознан (нет срока в JWT)'; el.style.color = 'var(--muted)'; return; }
    if (t.session === 'device-flow') { el.textContent = '🟢 Вход через device-flow — сессия обновляется сама, ручной токен не нужен'; el.style.color = 'var(--green)'; return; }
    const d = t.days_left;
    if (!t.valid)        { el.textContent = '🔴 токен ИСТЁК — обнови'; el.style.color = '#ff453a'; }
    else if (d < 1)      { el.textContent = '🟠 истекает менее чем через ' + Math.max(1, Math.round(t.hours_left)) + ' ч — обнови сегодня'; el.style.color = '#ff9f0a'; }
    else if (d < 3)      { el.textContent = '🟠 осталось ' + d + ' дн. — скоро обновить'; el.style.color = '#ff9f0a'; }
    else                 { el.textContent = '🟢 осталось ' + Math.round(d) + ' дн. до истечения'; el.style.color = 'var(--green)'; }
  } catch (e) {
    el.textContent = '✗ ' + (e.message || e); el.style.color = 'var(--muted)';
  }
}

async function orpheusLogin() {
  const btn    = document.getElementById('btn-orp-login');
  const stEl   = document.getElementById('orp-login-status');
  const setStatus = (msg, color='var(--muted)') => {
    if(stEl) { stEl.textContent=msg; stEl.style.color=color; stEl.style.display=''; }
  };
  if(btn) { btn.disabled=true; btn.textContent='⏳ Запускаю…'; }
  setStatus('Запрашиваю OAuth URL…');

  const r = await api('POST', '/api/orpheus/login-start');
  if(!r || !r.ok || !r.url) {
    setStatus(r?.error || 'Ошибка запуска OAuth', '#ff453a');
    if(btn) { btn.disabled=false; btn.textContent='🎵 Войти в Spotify'; }
    return;
  }

  setStatus('Открываю окно Spotify — войди любым способом…', '#0a84ff');
  const popup = window.open(r.url, 'orpheus_oauth', 'width=520,height=700');
  if(!popup) {
    setStatus('Popup заблокирован браузером — разреши попапы для этого сайта', 'var(--orange)');
    await api('DELETE', '/api/orpheus/login-cancel');
    if(btn) { btn.disabled=false; btn.textContent='🎵 Войти в Spotify'; }
    return;
  }

  const pollTimer = setInterval(async () => {
    if(popup.closed) {
      clearInterval(pollTimer);
      window._orpheusLoginDone = null;
      setStatus('Окно закрыто до завершения входа', 'var(--orange)');
      await api('DELETE', '/api/orpheus/login-cancel');
      if(btn) { btn.disabled=false; btn.textContent='🎵 Войти в Spotify'; }
    }
  }, 1000);

  window._orpheusLoginDone = () => {
    clearInterval(pollTimer);
    window._orpheusLoginDone = null;
    try { popup.close(); } catch(e) {}
    if(stEl) stEl.style.display='none';
    if(btn) { btn.disabled=false; btn.textContent='🎵 Войти в Spotify'; }
    loadOrpheusStatus();
  };
}

async function orpheusLogout() {
  await api('DELETE', '/api/orpheus/logout');
  toast('OrpheusDL: выход выполнен', 'var(--muted)');
  loadOrpheusStatus();
}

async function setOrpheusMode(enabled) {
  await saveSetting('spotify-engine', enabled ? 'orpheus_spotify' : 'convert');
  const sub = document.getElementById('orp-toggle-sub');
  if(sub) sub.textContent = enabled
    ? 'Spotify-ссылки идут в очередь напрямую'
    : 'Spotify-ссылки конвертируются через другой сервис';
}

