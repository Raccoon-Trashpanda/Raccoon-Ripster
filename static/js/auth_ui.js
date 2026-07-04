// ======================================================================
// Service login UIs + token probe + Tidal import
// Extracted from app.js (mechanical split — same global functions, no behaviour
// change). Loaded AFTER app.js in index.html, so it sees S/api/toast/etc.
// ======================================================================

// ── Yandex Music token — automated OAuth device flow ──────────────────────
// Backend gets a short device code from Yandex, we show it + open ya.ru/device,
// then poll until the user confirms; the token is saved to config server-side.
async function yandexGetToken() {
  const hint = document.getElementById('yandex-token-hint');
  const set = (h) => { if (hint) hint.innerHTML = h; };
  set(t('ya.requesting'));
  const r = await api('POST', '/api/yandex/auth/start', {});
  if (!r || !r.ok) { set(`<span style="color:var(--red)">${t('dlg.err')}: ${esc((r && r.error) || '?')}</span>`); return; }
  try { window.open(r.verification_url, '_blank', 'noopener'); } catch (e) {}
  set(ti('dlg.enter_code', {url: esc(r.verification_url)})
    + `<b style="font-size:15px;letter-spacing:2px;color:#ffcc00">${esc(r.user_code)}</b>`
    + ` <span style="opacity:.7">${t('dlg.waiting')}</span>`);
  const deadline = Date.now() + (r.expires_in || 300) * 1000;
  const iv = Math.max(3, (r.interval || 5)) * 1000;
  const poll = async () => {
    if (Date.now() > deadline) { set(`<span style="color:var(--red)">${t('ya.code_expired')}</span>`); return; }
    const p = await api('POST', '/api/yandex/auth/poll', { device_code: r.device_code });
    if (p && p.ok && p.saved) {
      set(`<span style="color:var(--green)">${t('ya.token_saved')}</span>`);
      if (S.config) S.config['yandex-token'] = '••••••';
      _setSecret('s-yandex-token', '••••••');
      return;
    }
    if (p && p.ok && p.pending) { setTimeout(poll, iv); return; }
    set(`<span style="color:var(--red)">${t('dlg.err')}: ${esc((p && p.error) || '?')}</span>`);
  };
  setTimeout(poll, iv);
}

// ── Tidal TV device-flow login (link.tidal.com) ───────────────────────────
// Tidal has NO password API — only the TV device-code flow. Backend fetches a
// device+user code, we show it + open link.tidal.com, then poll until the user
// confirms; the session is saved server-side into OrpheusDL's loginstorage.bin.
// Works on a fresh clone (built-in TV creds) → Tidal authenticates out of the box.
async function tidalTvLogin() {
  const hint = document.getElementById('tidal-tv-hint');
  const btn = document.getElementById('tidal-tv-btn');
  const set = (h) => { if (hint) hint.innerHTML = h; };
  if (btn) btn.disabled = true;
  set(t('td.requesting'));
  const r = await api('POST', '/api/tidal/auth/start', {});
  if (!r || !r.ok) { set(`<span style="color:var(--red)">${t('dlg.err')}: ${esc((r && r.error) || '?')}</span>`); if (btn) btn.disabled = false; return; }
  try { window.open(r.verification_url, '_blank', 'noopener'); } catch (e) {}
  set(ti('dlg.enter_code', {url: esc(r.verification_url)})
    + `<b style="font-size:15px;letter-spacing:2px;color:#00d4b3">${esc(r.user_code)}</b>`
    + ` <span style="opacity:.7">${t('dlg.waiting')}</span>`);
  const deadline = Date.now() + (r.expires_in || 300) * 1000;
  const iv = Math.max(2, (r.interval || 2)) * 1000;
  const poll = async () => {
    if (Date.now() > deadline) { set(`<span style="color:var(--red)">${t('td.code_expired')}</span>`); if (btn) btn.disabled = false; return; }
    const p = await api('POST', '/api/tidal/auth/poll', { device_code: r.device_code });
    if (p && p.ok && p.saved) {
      set(`<span style="color:var(--green)">${t('td.signed_in_pre')}${p.country ? ' (' + esc(p.country) + ')' : ''}${t('td.signed_in_post')}</span>`);
      if (btn) btn.disabled = false;
      return;
    }
    if (p && p.ok && p.pending) { setTimeout(poll, iv); return; }
    set(`<span style="color:var(--red)">${t('dlg.err')}: ${esc((p && p.error) || '?')}</span>`);
    if (btn) btn.disabled = false;
  };
  setTimeout(poll, iv);
}

// ── Spotify out-of-box OGG login (librespot PKCE OAuth) ────────────────────
// Backend runs librespot's browser OAuth flow which saves the durable blob
// (reusable_credentials.json) the keeper + orpheus use. One click, no extension,
// no token paste → Spotify downloads work on a fresh clone. Separate from the
// dev-API "Подключить" (release radar) above.
async function spotifyOggLogin() {
  const hint = document.getElementById('sp-ogg-hint');
  const btn = document.getElementById('sp-ogg-btn');
  const set = (h) => { if (hint) hint.innerHTML = h; };
  if (btn) btn.disabled = true;
  set(t('sp.ogg_starting'));
  const r = await api('POST', '/api/spotify/auth/start', {});
  if (!r || !r.ok) { set(`<span style="color:var(--red)">${t('dlg.err')}: ${esc((r && r.error) || '?')}</span>`); if (btn) btn.disabled = false; return; }
  try { window.open(r.auth_url, '_blank', 'noopener'); } catch (e) {}
  set(t('sp.ogg_page_opened'));
  const deadline = Date.now() + 180 * 1000;
  const poll = async () => {
    if (Date.now() > deadline) { set(`<span style="color:var(--red)">${t('sp.ogg_timeout')}</span>`); if (btn) btn.disabled = false; return; }
    const p = await api('POST', '/api/spotify/auth/status', {});
    if (p && p.ok && p.done) { set(`<span style="color:var(--green)">${t('sp.ogg_signed_in')}</span>`); if (btn) btn.disabled = false; return; }
    if (p && p.ok && p.pending) { setTimeout(poll, 2500); return; }
    set(`<span style="color:var(--red)">${t('dlg.err')}: ${esc((p && p.error) || '?')}</span>`); if (btn) btn.disabled = false;
  };
  setTimeout(poll, 3000);
}

// Alternative: browser (implicit) OAuth — opens Yandex login; after sign-in the
// page redirects to music.yandex.ru with the token in the URL fragment. The user
// copies the whole URL into the paste box; yandexSaveToken extracts the token.
function yandexBrowserToken() {
  const url = 'https://oauth.yandex.ru/authorize?response_type=token'
            + '&client_id=23cabbbdc6cd418abb4b39c32c41195d';
  try { window.open(url, '_blank', 'noopener'); } catch (e) {}
  const hint = document.getElementById('yandex-token-hint');
  if (hint) hint.innerHTML = t('ya.browser_hint');
}

// Accept a pasted redirect URL / fragment / raw token → extract access_token,
// save it server-side and reflect it in the masked token field.
function yandexSaveToken(raw) {
  raw = (raw || '').trim();
  if (!raw) return;
  let tok = '';
  const m = raw.match(/access_token=([^&\s#]+)/i);
  if (m) tok = decodeURIComponent(m[1]);
  else if (!/[\s/]/.test(raw)) tok = raw;   // looks like a bare token
  if (!tok || tok.length < 12) return;      // wait for a full paste
  saveSetting('yandex-token', tok);
  _setSecret('s-yandex-token', '••••••');
  if (S.config) S.config['yandex-token'] = '••••••';
  const hint = document.getElementById('yandex-token-hint');
  if (hint) hint.innerHTML = `<span style="color:var(--green)">${t('ya.token_accepted')}</span>`;
  const box = document.getElementById('s-yandex-paste');
  if (box) box.value = '';
}

// ── Guest service info — what Ripster is + which services are available ───────
async function showGuestServiceInfo() {
  let status = {};
  try { status = await (await fetch('/api/services/status')).json(); } catch(e){}
  const names = {apple:'Apple Music',qobuz:'Qobuz',deezer:'Deezer',tidal:'Tidal',
                 spotify:'Spotify',soundcloud:'SoundCloud',beatport:'Beatport',yandex:'Яндекс.Музыка'};
  const avail = Object.keys(names).filter(k => status[k]);
  const q = ((S.config && S.config.quality) || '—');
  const badges = avail.map(k => `<span style="font-size:11px;padding:3px 9px;border-radius:20px;background:${_svcColor(k)}22;color:${_svcColor(k)};border:1px solid ${_svcColor(k)}55">${esc(names[k])}</span>`).join(' ');
  const ov = document.createElement('div');
  ov.id = 'guest-info-overlay';
  ov.style.cssText = 'position:fixed;inset:0;z-index:99999;background:rgba(0,0,0,.6);display:flex;align-items:center;justify-content:center;padding:20px';
  ov.onclick = () => ov.remove();
  ov.innerHTML = `<div onclick="event.stopPropagation()" style="max-width:420px;width:100%;background:var(--surface,#15151a);border:1px solid var(--border);border-radius:14px;padding:22px 24px;box-shadow:0 20px 60px rgba(0,0,0,.55)">
      <div style="font-family:var(--display);font-size:20px;font-weight:800;margin-bottom:10px">Raccoon <span style="color:var(--red)">Ripster</span></div>
      <div style="font-size:13px;color:var(--muted);line-height:1.7;margin-bottom:14px">Загрузчик музыки в высоком качестве (вплоть до Hi-Res FLAC и Dolby Atmos). Ищи треки и альбомы, ставь в очередь, скачивай — готовое доступно прямо во встроенном плеере.</div>
      <div style="font-size:11px;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:.06em;margin-bottom:8px">Доступные сервисы</div>
      <div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:14px">${badges || '<span style="color:var(--muted)">—</span>'}</div>
      <div style="font-size:12px;color:var(--muted)">Качество по умолчанию: <span style="color:var(--text);font-weight:600">${esc(String(q).toUpperCase())}</span></div>
      <button onclick="document.getElementById('guest-info-overlay').remove()" style="margin-top:16px;width:100%;padding:9px;background:var(--red);color:#fff;border:none;border-radius:8px;font-size:13px;font-weight:700;cursor:pointer;font-family:var(--font)">Понятно</button>
    </div>`;
  document.body.appendChild(ov);
}

// ── Token probe: show live auth status for Qobuz/Tidal/Deezer ─────────────
async function testAuth(service){
  const out = document.getElementById('test-auth-' + service);
  if(out){ out.textContent = t('s.checking'); out.style.color = 'var(--muted)'; }
  try {
    const r = await fetch('/api/test-auth/' + service, {method: 'POST'});
    const d = await r.json().catch(()=>({ok:false,error:t('s.bad_response')}));
    if(d.ok) {
      const u = d.user || {};
      const parts = [];
      if(u.country) parts.push(`<span style="font-size:9px;font-weight:700;padding:1px 5px;border-radius:3px;background:rgba(255,255,255,.08);color:var(--muted);letter-spacing:.5px;vertical-align:middle">${esc(u.country)}</span>`);
      if(u.hires === true)         parts.push('<span style="color:#ffd60a;font-weight:700">Hi-Res ✓</span>');
      else if(u.lossless === true) parts.push('<span style="color:var(--green);font-weight:700">Lossless ✓</span>');
      else if(u.hq === true)       parts.push('<span style="color:var(--orange);font-weight:700">HQ ✓</span>');
      if(u.expiry) parts.push(`<span style="color:var(--muted)">${ti('s.until',{date:esc(u.expiry)})}</span>`);
      if(u.sub_offer) parts.push(`<span style="color:var(--muted)">${esc(u.sub_offer)}</span>`);
      if(u.sub_end){
        const dl = u.sub_days_left;
        const col = u.sub_expired ? 'var(--red)' : (dl!=null && dl<=14) ? 'var(--orange)' : 'var(--green)';
        const left = (dl!=null) ? (u.sub_expired ? ` ${t('s.sub_expired')}` : ` · ${ti('s.sub_days',{n:dl})}`) : '';
        parts.push(`<span style="color:${col};font-weight:600">${ti('s.sub_until',{date:esc(u.sub_end)})}${left}</span>`);
      }
      if(u.note) parts.push(`<span style="color:var(--muted)">${esc(u.note)}</span>`);
      const tag = parts.length ? ' &nbsp;' + parts.join(' · ') : '';
      const _lbl = (typeof _svcLabel === 'function' ? _svcLabel(service) : service);
      if(out){ out.innerHTML = `<span style="color:var(--green);font-weight:700">✓ ${esc(_lbl)} ${t('s.works')}</span>` + tag; }
    } else {
      if(out){ out.innerHTML = '<span style="color:var(--red)">✗ ' + esc(d.error || t('s.unknown_error')) + '</span>'; }
    }
  } catch(e) {
    if(out){ out.innerHTML = '<span style="color:var(--red)">✗ ' + t('s.net_error') + ': ' + esc(e.message) + '</span>'; }
  }
}

// Validate every configured service token at startup so they "take" and show
// their status without the user opening each settings tab. Staggered so we
// don't hammer the backend; testAuth() no-ops visually if a tab isn't rendered
// but the server-side probe (token refresh / promotion) still runs.
let _autoValidated = false;
function autoValidateServices() {
  if(_autoValidated) return;            // once per session
  _autoValidated = true;
  const c = S.config || {};
  const jobs = [];
  if(c['deezer-arl'])                                        jobs.push('deezer');
  if(c['qobuz-auth-token'] || (c['qobuz-email'] && c['qobuz-password'])) jobs.push('qobuz');
  if(c['tidal-token'])                                       jobs.push('tidal');
  if(c['media-user-token'] && c['authorization-token'])      jobs.push('apple');
  if(c['soundcloud-oauth-token'])                            jobs.push('soundcloud');
  if(c['beatport-username'] && c['beatport-password'])       jobs.push('beatport');
  if(c['spotify-client-id'] || c['spotify-sp-dc'])           jobs.push('spotify');
  if(c['yandex-token'])                                      jobs.push('yandex');
  jobs.forEach((svc, i) => setTimeout(() => { try { testAuth(svc); } catch {} }, i * 600));
}

async function qobuzPasswordLogin() {
  const out   = document.getElementById('qobuz-pw-status');
  const email = (document.getElementById('s-qobuz-email')?.value || '').trim();
  const pw    = document.getElementById('s-qobuz-pass')?.value || '';
  if (!email || !pw) {
    if (out) out.innerHTML = '<span style="color:var(--danger)">Заполни email и пароль</span>';
    return;
  }
  if (out) { out.textContent = 'Вход в Qobuz…'; out.style.color = 'var(--muted)'; }
  // Persist creds first — the server-side probe reads them from config.
  await saveSetting('qobuz-email', email);
  await saveSetting('qobuz-password', pw);
  try {
    const r = await fetch('/api/test-auth/qobuz', {method:'POST'});
    const d = await r.json().catch(() => ({ok:false, error:'Неверный ответ сервера'}));
    if (d.ok) {
      const u = d.user || {};
      const tier = u.hires ? 'Hi-Res' : (u.lossless ? 'Lossless' : (u.subscription || ''));
      if (out) out.innerHTML = '<span style="color:var(--green)">✓ Вход выполнен'
        + (tier ? ' · ' + esc(tier) : '') + ' — user-id и токен сохранены</span>';
      loadTokensToUI();   // probe auto-promoted email/pass → token; refresh fields
    } else {
      if (out) out.innerHTML = '<span style="color:var(--danger)">✗ ' + esc(d.error || 'Ошибка') + '</span>';
    }
  } catch(e) {
    if (out) out.innerHTML = '<span style="color:var(--danger)">✗ Сеть: ' + esc(e.message) + '</span>';
  }
}

async function saveServiceTab(service) {
  const g  = id => { const el = document.getElementById(id); return el ? el.value : ''; };
  // Only include a secret field if the user actually typed something into it
  // (non-empty value = new input; empty = placeholder state = keep existing).
  const gs = id => { const v = g(id); return v || undefined; };
  let cfg = {};
  if(service === 'qobuz') {
    cfg = {'qobuz-user-id':g('s-qobuz-userid'),'qobuz-email':g('s-qobuz-email'),
           'qobuz-app-id':g('s-qobuz-appid'),'qobuz-secrets':g('s-qobuz-secrets'),
           'qobuz-quality':g('s-qobuz-qual')};
    const tok = gs('s-qobuz-authtok'); if(tok !== undefined) cfg['qobuz-auth-token'] = tok;
    const pw  = gs('s-qobuz-pass');   if(pw  !== undefined) cfg['qobuz-password']    = pw;
  } else if(service === 'deezer') {
    cfg = {'deezer-quality':g('s-deezer-qual')};
    const arl = gs('s-deezer-arl'); if(arl !== undefined) cfg['deezer-arl'] = arl;
  } else if(service === 'tidal') {
    cfg = {'tidal-user-id':g('s-tidal-userid'),'tidal-country':g('s-tidal-country'),
           'tidal-token-expiry':g('s-tidal-expiry'),'tidal-quality':g('s-tidal-qual')};
    const tok = gs('s-tidal-token');   if(tok !== undefined) cfg['tidal-token']   = tok;
    const ref = gs('s-tidal-refresh'); if(ref !== undefined) cfg['tidal-refresh'] = ref;
  } else if(service === 'soundcloud') {
    cfg = {};
    const tok = gs('s-sc-oauth'); if(tok !== undefined) cfg['soundcloud-oauth-token'] = tok;
  } else if(service === 'beatport') {
    cfg = {'beatport-quality':g('s-bp-quality')};
    const user = gs('s-bp-user'); if(user !== undefined) cfg['beatport-username'] = user;
    const pw   = gs('s-bp-pass'); if(pw   !== undefined) cfg['beatport-password']  = pw;
  } else if(service === 'apple') {
    // Only the keys that have visible inputs in the Apple panel — never wipe
    // sibling keys (storefront, amd-instance-url, cookies path) the user set
    // elsewhere. Empty token fields are NOT persisted (keeps existing value).
    cfg = {};
    const sf  = g('s-apple-storefront');   if(sf)  cfg['storefront'] = sf;
    const mut = gs('s-apple-mut');         if(mut !== undefined) cfg['media-user-token'] = mut;
    const bea = gs('s-apple-bearer');      if(bea !== undefined) cfg['authorization-token'] = bea;
    const cp  = g('s-gamdl-cookies-path'); if(cp) cfg['gamdl-cookies-path'] = cp;
    const ai  = g('s-amd-instance');       if(ai) cfg['amd-instance-url'] = ai;
  } else if(service === 'spotify') {
    cfg = {};
    const cid = g('s-sp-cid');   if(cid) cfg['spotify-client-id'] = cid;
    const sec = gs('s-sp-csecret'); if(sec !== undefined) cfg['spotify-client-secret'] = sec;
    const dc  = gs('s-sp-dc');   if(dc  !== undefined) cfg['spotify-sp-dc'] = dc;
    const dy  = g('s-sp-days');  if(dy)  cfg['spotify-release-days'] = parseInt(dy) || 7;
    const tp  = g('s-sp-types'); if(tp)  cfg['spotify-release-types'] = tp;
    const oq  = g('s-orp-quality'); if(oq) cfg['orpheus-quality'] = oq;
  } else if(service === 'yandex') {
    cfg = {'yandex-quality':g('s-yandex-qual')};
    const tok = gs('s-yandex-token'); if(tok !== undefined && !tok.startsWith('••')) cfg['yandex-token'] = tok;
  }
  Object.assign(S.config, cfg);
  try { await api('POST','/api/config',cfg); toast(t('t.saved_ok'),'var(--green)'); }
  catch(e) { toast('✗ '+e.message,'var(--red)'); }
}

async function saveAndTestAuth(service) {
  await saveServiceTab(service);
  await testAuth(service);
}

// ── Tidal: import token.json from tidal-dl-ng / tidalapi ──────────────────
function toggleTidalImport(){
  const p = document.getElementById('tidal-import-panel');
  if(!p) return;
  const showing = p.style.display !== 'none';
  p.style.display = showing ? 'none' : '';
  if(!showing) {
    // Focus textarea when opening for quick paste
    setTimeout(() => document.getElementById('tidal-import-json')?.focus(), 50);
  }
}

async function importTidalToken(){
  const ta  = document.getElementById('tidal-import-json');
  const msg = document.getElementById('tidal-import-msg');
  const raw = (ta?.value || '').trim();
  if(!raw) {
    if(msg){ msg.innerHTML = '<span style="color:var(--red)">Пустое поле</span>'; }
    return;
  }
  if(msg){ msg.innerHTML = '<span style="color:var(--muted)">Импортирую…</span>'; }
  try {
    const r = await fetch('/api/import-token/tidal', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({content: raw}),
    });
    const d = await r.json().catch(()=>({}));
    if(!r.ok) {
      if(msg){ msg.innerHTML = '<span style="color:var(--red)">✗ ' + esc(d.detail || ('HTTP ' + r.status)) + '</span>'; }
      return;
    }
    const imp = d.imported || {};
    // Update S.config so UI fields reflect the new state
    if(S && S.config) {
      S.config['tidal-token']         = '••••••••';   // masked on server; show mask locally
      S.config['tidal-refresh']       = '••••••••';
      S.config['tidal-user-id']       = imp.user_id || '';
      S.config['tidal-country']       = imp.country || '';
      S.config['tidal-token-expiry']  = imp.expiry_unix || '';
    }
    // Re-populate the Tidal tab fields
    showStab('tidal', document.querySelector('.stab[data-stab="tidal"]'));
    if(msg){
      const parts = [];
      if(imp.user_id) parts.push('user_id=' + esc(imp.user_id));
      if(imp.country) parts.push('country=' + esc(imp.country));
      msg.innerHTML = '<span style="color:var(--green)">✓ Импортировано</span>' + (parts.length ? ' · ' + parts.join(' · ') : '');
    }
    // Auto-test after 500ms so user sees it works
    setTimeout(() => testAuth('tidal'), 500);
    // Clear textarea
    if(ta) ta.value = '';
  } catch(e) {
    if(msg){ msg.innerHTML = '<span style="color:var(--red)">✗ ' + esc(e.message) + '</span>'; }
  }
}

// ══ SPOTIFY STATUS ════════════════════════════════════════════════

// Show / hide "saved conversion target" row + reset handler.
function _renderSpotifySavedTarget() {
  const row     = document.getElementById('sp-saved-target-row');
  const nameEl  = document.getElementById('sp-saved-target-name');
  const target  = (S.config && S.config['spotify-default-target']) || '';
  if(!row) return;
  if(target && ['apple','deezer','qobuz'].includes(target)) {
    row.style.display = '';
    if(nameEl) {
      nameEl.textContent = _svcLabel(target);
      nameEl.style.color = _svcColor(target);
    }
  } else {
    row.style.display = 'none';
  }
}

async function resetSpotifyDefaultTarget() {
  try {
    await api('POST','/api/config', { 'spotify-default-target': '' });
    if(S.config) S.config['spotify-default-target'] = '';
    _renderSpotifySavedTarget();
    toast(t('au.reset_ask'), 'var(--muted)');
  } catch(e) {
    toast(t('au.reset_fail')+e.message, 'var(--red)');
  }
}

async function refreshAppleAuthStatus() {
  try {
    const r = await api('GET', '/api/apple/auth-status');
    const el      = document.getElementById('apple-auth-status-text');
    const loginBtn = document.getElementById('btn-apple-login');
    if (!el) return;
    if (r.mut_set) {
      el.textContent = ti('as.token_set', {n: r.mut_length});
      el.style.color = 'var(--green)';
      if(loginBtn) loginBtn.style.display = 'none';
      const inp = document.getElementById('t-mut');
      if (inp && !inp.value && S.config && S.config['media-user-token']) {
        inp.value = S.config['media-user-token'];
      }
    } else {
      el.textContent = t('as.token_not_set');
      el.style.color = 'var(--muted)';
      if(loginBtn) loginBtn.style.display = '';
    }
  } catch(e) {
    const el = document.getElementById('apple-auth-status-text');
    if (el) el.textContent = '';
  }
}

async function loadSpotifyStatus() {
  const r = await api('GET','/api/spotify/status');
  S._spStatus = r;  // cache for _syncReleasesSettingsTab
  const nameEl  = document.getElementById('sp-name');
  const emailEl = document.getElementById('sp-email');
  const logBtn  = document.getElementById('sp-logout-btn');
  const relBtn  = document.getElementById('rel-sp-btn');
  if(r.connected) {
    if(nameEl)  nameEl.textContent  = r.sp_dc_mode ? 'sp_dc (cookie)' : (r.display_name || 'Spotify');
    if(emailEl) emailEl.textContent = r.sp_dc_mode ? t('sp.auth_spdc') : (r.email || '');
    if(logBtn)  logBtn.style.display = r.sp_dc_mode ? 'none' : '';
    if(relBtn)  relBtn.style.display = 'none';
  } else {
    const err403   = r.error && r.error.toLowerCase().includes('not registered');
    const errSpDc  = r.sp_dc_expired;
    if(nameEl)  nameEl.textContent = errSpDc ? t('sp.spdc_expired')
      : err403 ? t('sp.not_registered') : t('sp.not_connected');
    if(emailEl) emailEl.textContent = errSpDc
      ? t('sp.spdc_refresh_hint')
      : err403 ? t('sp.not_registered_hint')
      : '';
    if(logBtn)  logBtn.style.display = 'none';
    if(relBtn)  relBtn.style.display = '';
  }
}

async function spotifyLogout() {
  await api('POST','/api/spotify/logout');
  loadSpotifyStatus();
  toast(t('sp.disconnected'));
}

async function autoExtractSpDc() {
  const btn    = document.getElementById('sp-dc-auto-btn');
  const status = document.getElementById('sp-dc-auto-status');
  const input  = document.getElementById('s-sp-dc');
  if(btn) { btn.disabled = true; btn.textContent = t('sp.searching_btn'); }
  if(status) { status.textContent = t('sp.searching'); status.style.color = 'var(--muted)'; }
  try {
    const r = await api('GET', '/api/spotify/extract-sp-dc');
    if(r.ok && r.value) {
      if(input) input.value = r.value;
      await api('POST', '/api/config', {'spotify-sp-dc': r.value});
      if(S.config) S.config['spotify-sp-dc'] = r.value;
      const _where = r.browser + (r.profile && r.profile!=='Default' ? ' ('+r.profile+')' : '');
      if(status) { status.textContent = ti('sp.found_saved', {where: _where}); status.style.color = 'var(--green)'; }
      loadSpotifyStatus();
      toast(ti('sp.spdc_updated', {browser: r.browser}), 'var(--green)');
    } else {
      // sp_dc — httpOnly, из document.cookie не читается. Показываем Application tab.
      if(status) {
        status.innerHTML = t('sp.spdc_manual_html');
        status.style.color = 'var(--muted)';
      }
    }
  } catch(e) {
    if(status) { status.textContent = '✗ ' + e.message; status.style.color = 'var(--red)'; }
  } finally {
    if(btn) { btn.disabled = false; btn.textContent = t('sp.auto_btn'); }
  }
}

