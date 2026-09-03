// ======================================================================
// Service detection in URL bar
// Extracted from app.js (mechanical split — same global functions, no behaviour
// change). Loaded AFTER app.js in index.html, so it sees S/api/toast/etc.
// ======================================================================

// ── Service detection in URL bar ─────────────────────────────────
const SVC_COLORS = {apple:'#fc3c44',qobuz:'#1b68d3',deezer:'#a238ff',tidal:'#00d4b3',spotify:'#1db954',soundcloud:'#ff5500',beatport:'#a6ce39',yandex:'#ffcc00',amazon:'#25d1da'};
const SVC_LABELS = {
  apple:      '🍎 Apple Music',
  qobuz:      '🎼 Qobuz',
  deezer:     '🎵 Deezer',
  tidal:      '🌊 Tidal',
  spotify:    '🟢 Spotify',
  soundcloud: '🎵 SoundCloud',
  beatport:   '🟣 Beatport',
  yandex:     '🟡 Yandex Music',
  amazon:     '🅰️ Amazon Music',
};

// Чистое определение сервиса по ссылке — БЕЗ трогания разметки. Отдельно от
// detectUrlService затем, что то же знание требовалось другим вкладкам, и они
// заводили свои перечни: у Раскопок он знал шесть сервисов из десяти, и клик по
// находке с Beatport/Яндекса уходил в плеер без имени сервиса. Зеркало этой
// функции на бэкенде — service_layer.detect_service.
function svcFromUrl(val) {
  val = String(val || '');
  if(val.includes('music.apple.com'))      return 'apple';
  if(val.includes('qobuz.com'))            return 'qobuz';
  if(val.includes('deezer.com'))           return 'deezer';
  if(val.includes('deezer.page'))          return 'deezer';
  if(val.includes('tidal.com'))            return 'tidal';
  if(val.includes('soundcloud.com'))       return 'soundcloud';
  if(val.includes('spotify.com'))          return 'spotify';
  if(val.includes('beatport.com'))         return 'beatport';
  if(val.includes('music.yandex.'))        return 'yandex';
  if(val.includes('music.amazon.'))        return 'amazon';
  if(val.includes('bbc.co.uk'))            return 'bbc';
  return '';
}

function detectUrlService(val) {
  const svc = svcFromUrl(val);

  const dot = document.getElementById('url-svc-dot');
  const lbl = document.getElementById('url-svc-label');
  if(dot) dot.style.background = svc ? (SVC_COLORS[svc]||'var(--muted)') : 'var(--muted)';
  if(lbl) {
    let label = SVC_LABELS[svc] || svc || (val.startsWith('http') ? t('u.unknown_svc') : '');
    if(svc === 'spotify') {
      const spEng = (S.config && S.config['spotify-engine']) || 'convert';
      label = (spEng === 'orpheus_spotify') ? '🟢 Spotify → OrpheusDL' : '🟢 Spotify — ' + t('u.will_convert');
    }
    lbl.textContent = label;
  }

  // Swap quality selector to match the pasted service
  if(svc) updateQualitySelector(svc);
}

// Service pill switcher — updates active service context (for quality list etc)
const SVC_BTN_COLS = {apple:'var(--red)',qobuz:'#1b68d3',deezer:'#a238ff',tidal:'#00d4b3',beatport:'#01f49c'};
function setActiveService(svc, btn) {
  // Style all buttons
  document.querySelectorAll('.svc-btn').forEach(b=>{
    b.style.background='transparent'; b.style.color='var(--muted)';
  });
  if(btn){ btn.style.background=SVC_BTN_COLS[svc]||'var(--red)'; btn.style.color='#fff'; }

  // Update URL bar placeholder
  const inp = document.getElementById('url-input');
  const placeholders = {
    apple:  ti('u.ph_svc',{svc:'Apple Music',types:'album, song, playlist, artist'}),
    qobuz:  ti('u.ph_svc',{svc:'Qobuz',types:'album, track, playlist'}),
    deezer: ti('u.ph_svc',{svc:'Deezer',types:'album, track, playlist, artist'}),
    tidal:  ti('u.ph_svc',{svc:'Tidal',types:'album, track, playlist'}),
  };
  if(inp) inp.placeholder = placeholders[svc] || t('u.ph_generic');

  // Update dot color
  const dot = document.getElementById('url-svc-dot');
  if(dot) dot.style.background = SVC_BTN_COLS[svc]||'var(--red)';

  // For Apple: also update engine (show engine-specific quality)
  // For others: quality list from their own qualities
  S._activeSvc = svc;
  if(svc === 'apple') {
    // switch engine quality back
  }
}

function detectSpotifyInUrl(val) {
  // If user pastes a Spotify link in the main URL bar, offer to convert
  if(val && val.includes('spotify.com')) {
    toast(t('u.sp_hint'), 'var(--green,#1db954)');
  }
}
// ══ SETTINGS TABS ════════════════════════════════════════════════
// Сворачиваемые блоки «Дополнительно» (класс .block.coll): клик по заголовку
// разворачивает/сворачивает. help-q внутри заголовка сам делает stopPropagation,
// так что «?» не триггерит сворачивание. DOM не трогаем — только класс .coll-open.
document.addEventListener('click', function(e){
  const title = e.target.closest && e.target.closest('.block.coll > .block-title');
  if(!title) return;
  title.parentElement.classList.toggle('coll-open');
});

function showStab(id, btn) {
  document.querySelectorAll('.stab-panel').forEach(p=>p.classList.remove('active'));
  document.querySelectorAll('.stab').forEach(b=>b.classList.remove('active'));
  if(id==='global') {
    const el=document.getElementById('stab-global'); if(el) el.classList.add('active');
  } else {
    const el=document.getElementById('stab-'+id); if(el) el.classList.add('active');
    // «Файлы» (бывш. stab-global-shared) держит сетку цветов сервисов — рисуем её здесь.
    if(id==='files') { try { renderSvcColorGrid?.(); } catch {} }
  }
  if(btn) btn.classList.add('active');
  const vb = document.querySelector('#view-settings .view-body');
  if(vb) vb.scrollTop = 0;
  // Spotify tab → live token auto-sync log (extension pushes); stop polling elsewhere.
  try { if(id==='spotify') startSpotifyTokenPoll?.(); else stopSpotifyTokenPoll?.(); } catch {}
  // Repopulate fields of newly visible tab from S.config
  const c = S.config||{};
  if(id==='deezer')  {
    _setSecret('s-deezer-arl', c['deezer-arl']); setVal('s-deezer-qual',c['deezer-quality']||'flac'); setVal('s-deezer-path',c['deezer-save-path']||'');
    if(c['deezer-arl']) testAuth('deezer');
    try { loadDeezerAccounts?.(); } catch {}
  }
  if(id==='qobuz')   {
    setVal('s-qobuz-userid',c['qobuz-user-id']||''); _setSecret('s-qobuz-authtok',c['qobuz-auth-token']); setVal('s-qobuz-email',c['qobuz-email']||''); _setSecret('s-qobuz-pass',c['qobuz-password']); setVal('s-qobuz-appid',c['qobuz-app-id']||''); setVal('s-qobuz-secrets',c['qobuz-secrets']||''); setVal('s-qobuz-qual',c['qobuz-quality']||'7'); setVal('s-qobuz-path',c['qobuz-save-path']||'');
    if(c['qobuz-auth-token'] || (c['qobuz-email'] && c['qobuz-password'])) testAuth('qobuz');
    try { loadQobuzAccounts?.(); } catch {}
  }
  if(id==='tidal')   {
    _setSecret('s-tidal-token',c['tidal-token']); _setSecret('s-tidal-refresh',c['tidal-refresh']); setVal('s-tidal-userid',c['tidal-user-id']||''); setVal('s-tidal-country',c['tidal-country']||'US'); setVal('s-tidal-expiry',c['tidal-token-expiry']||''); setVal('s-tidal-qual',c['tidal-quality']||'lossless'); setVal('s-tidal-path',c['tidal-save-path']||'');
    if(c['tidal-token']) testAuth('tidal');
    loadTokenExpiry('tidal');
  }
  if(id==='spotify') { setVal('s-sp-cid',c['spotify-client-id']||''); setVal('s-sp-csecret',c['spotify-client-secret']||''); _setSecret('s-sp-dc',c['spotify-sp-dc']); setVal('s-orp-path',c['orpheus-save-path']||''); setChk('s-orp-mp3',c['orpheus-convert-mp3']===true); setVal('s-orp-quality',c['orpheus-quality']||'hifi'); _renderSpotifySavedTarget(); loadSpotifyStatus(); loadOrpheusStatus(); testAuth('spotify'); }
  if(id==='digs') {
    setVal('s-dg-shape',   c['digs-shape']   || 'circle');
    setVal('s-dg-size',    String(c['digs-size'] || 44));
    setVal('s-dg-bg',      c['digs-bg']      || 'earth');
    setVal('s-dg-density', c['digs-density'] || 'normal');
    setVal('s-dg-motion',  c['digs-motion']  || 'full');
    setVal('s-dg-perkind', String(c['digs-per-kind'] || 12));
    setVal('s-dg-click',   c['digs-click']   || 'play');
    setVal('s-dg-dbl',     c['digs-dblclick']|| 'bubbles');
    const fd = document.getElementById('s-dg-forgot');
    if(fd) fd.value = c['digs-forgotten-days'] || 45;
    setChk('s-dg-covers', c['digs-covers'] !== false);
    setChk('s-dg-coon',   c['digs-coon']   !== false);
    try { digsBuildSettingChips?.(); } catch(e) {}
  }
  if(id==='apple')   {
    updateEngineUI(c['engine']||'zhaarey');
    setVal('s-wrapper-email',c['wrapper-apple-id']||'');
    setVal('s-wrapper-pass',c['wrapper-password']||'');
    const wm = c['wrapper-mode'] || 'docker-remote';
    const radio = document.querySelector(`input[name="wrapper-mode"][value="${wm}"]`);
    if(radio) radio.checked = true;
    _applyWrapperModeUI(wm, null);
    if((c['engine']||'zhaarey')==='amd') checkAMDWrapperStatus();
    refreshAppleAuthStatus();
    loadWrapperSessionStatus();
    if(c['media-user-token'] && c['authorization-token']) testAuth('apple');
    try { loadAppleAccounts?.(); } catch {}
  }
  if(id==='soundcloud') { setVal('s-sc-path', c['soundcloud-save-path']||''); setVal('s-sc-oauth', c['soundcloud-oauth-token']||''); setVal('s-sc-wvd-wrapper', c['sc-widevine-wrapper-url']||''); setChk('s-sc-isrc-fallback', !!c['sc-isrc-fallback']); if(c['soundcloud-oauth-token']) testAuth('soundcloud'); scEngineCheck(); try { _scCheckWvd?.(); _scCheckWvdWrapper?.(); loadSoundcloudAccounts?.(); } catch {} }
  if(id==='beatport') {
    setVal('s-bp-user', c['beatport-username']||'');
    setVal('s-bp-pass', c['beatport-password']||'');
    setVal('s-bp-path', c['beatport-save-path']||'');
    const bpq = document.getElementById('s-bp-quality');
    if(bpq) bpq.value = c['beatport-quality']||'hifi';
    loadBeatportStatus();
    if(c['beatport-username'] && c['beatport-password']) testAuth('beatport');
  }
  if(id==='yandex') {
    _setSecret('s-yandex-token', c['yandex-token']);
    setVal('s-yandex-qual', c['yandex-quality']||'flac');
    setVal('s-yandex-path', c['yandex-save-path']||'');
    try { loadYandexAccounts?.(); } catch {}
  }
  if(id==='amazon') {
    _setSecret('s-amazon-token', c['amazon-token']);
    setVal('s-amazon-qual', c['amazon-quality']||'High');
    setVal('s-amazon-path', c['amazon-save-path']||'');
  }
  if(id==='admin') {
    loadAdminLinks();
    loadRemoteStatus();
    loadTunnelStatus();
  }
  if(id==='bot') loadBotConfig();
  if(id==='global')  {
    loadAuthStatus();
    pairFillHint();
    setChk('s-queue-autostart', c['queue-autostart']!==false);
    setChk('s-notify-on-done', !!c['notify-on-done']);
    // Release toasts default ON — the point of a watchlist is being told.
    setChk('s-notify-on-release', c['notify-on-release'] !== false);
    setChk('s-minimize-to-tray', c['minimize-to-tray']!==false);
    const _mz = document.getElementById('s-minimize-to');
    if(_mz) _mz.value = (c['minimize-to'] === 'tray') ? 'tray' : 'taskbar';
    const mp = +(c['max-parallel'] || 1);
    const sl = document.getElementById('s-max-parallel');
    const vl = document.getElementById('s-max-parallel-val');
    if(sl) sl.value = mp;
    if(vl) vl.textContent = mp;
  }
}

// ══ BOT CONFIG TAB ════════════════════════════════════════════════════
// Reads/writes tgbot/config.json via /api/admin/bot-config so the bot token,
// owner id, local Bot API, cache channel etc. are edited from the UI instead of
// hand-editing the file (TASKS #12). Secrets (bot_token/api_hash) are never sent
// back from the server — only a "set" flag — and a blank secret field leaves the
// stored value untouched.
async function loadBotConfig(){
  const setSet = (elId, on) => { const e=document.getElementById(elId); if(e){ e.textContent = on ? t('prot.set') : t('prot.not_set'); e.style.color = on ? 'var(--green)' : 'var(--muted)'; } };
  try {
    const c = await api('GET','/api/admin/bot-config');
    setVal('b-owner-id',        c['owner_id'] ?? '');
    setVal('b-api-id',          c['api_id'] ?? '');
    setVal('b-local-api',       c['local_bot_api'] ?? '');
    setVal('b-max-upload',      c['max_upload_mb'] ?? '');
    setVal('b-cache-id',        c['cache_channel_id'] ?? '');
    setVal('b-cache-link',      c['cache_channel_link'] ?? '');
    setVal('b-backend-url',     c['backend_url'] ?? '');
    setVal('b-default-quality', c['default_quality'] ?? '');
    // Secrets: never populate the input, just show whether one is stored.
    const tok=document.getElementById('b-bot-token'); if(tok) tok.value='';
    const ah =document.getElementById('b-api-hash');  if(ah)  ah.value='';
    setSet('bot-token-set',   c['bot_token_set']);
    setSet('bot-apihash-set', c['api_hash_set']);
  } catch(e) {
    const s=document.getElementById('bot-config-status');
    if(s){ s.textContent=t('err.generic')+': '+e.message; s.style.color='var(--red)'; }
  }
}

async function saveBotConfig(){
  const s = document.getElementById('bot-config-status');
  const v = id => (document.getElementById(id)?.value ?? '').trim();
  // Non-secret fields are pre-filled by loadBotConfig, so they carry the current
  // value unless the user changed them. Blank secrets are skipped server-side.
  const body = {
    bot_token:          v('b-bot-token'),
    owner_id:           v('b-owner-id'),
    api_id:             v('b-api-id'),
    api_hash:           v('b-api-hash'),
    local_bot_api:      v('b-local-api'),
    max_upload_mb:      v('b-max-upload'),
    cache_channel_id:   v('b-cache-id'),
    cache_channel_link: v('b-cache-link'),
    backend_url:        v('b-backend-url'),
    default_quality:    v('b-default-quality'),
  };
  if(s){ s.textContent='…'; s.style.color='var(--muted)'; }
  try {
    const r = await api('POST','/api/admin/bot-config', body);
    const n = (r.changed||[]).length;
    let msg = n ? `${t('prot.saved')} (${n})` : t('prot.no_changes');
    if(r.restart_required) msg += t('prot.bot_restart');
    if(s){ s.textContent=msg; s.style.color = r.restart_required ? 'var(--orange)' : 'var(--green)'; }
    try { toast(msg, r.restart_required ? 'var(--orange)' : 'var(--green)'); } catch {}
    loadBotConfig();
  } catch(e) {
    if(s){ s.textContent=t('err.generic')+': '+e.message; s.style.color='var(--red)'; }
    try { toast('✗ '+e.message, 'var(--red)'); } catch {}
  }
}

// ══ APP AUTH ══════════════════════════════════════════════════════
async function loadAuthStatus(){
  const st = document.getElementById('sec-status');
  const curWrap = document.getElementById('sec-current-wrap');
  const logoutBtn = document.getElementById('sec-logout-btn');
  try {
    const r = await fetch('/api/auth-status');
    const d = await r.json();
    if(d.enabled) {
      if(st) { st.innerHTML = t('prot.on_html'); st.style.background = 'rgba(62,207,170,.08)'; }
      if(curWrap) curWrap.style.display = '';
      if(logoutBtn) logoutBtn.style.display = '';
    } else {
      if(st) { st.innerHTML = t('prot.off_html'); st.style.background = 'rgba(255,255,255,.04)'; }
      if(curWrap) curWrap.style.display = 'none';
      if(logoutBtn) logoutBtn.style.display = 'none';
    }
  } catch(e) {
    if(st) st.textContent = t('ui.err_pfx')+e.message;
  }
}

async function saveAppPassword(){
  const newPw = document.getElementById('sec-new')?.value || '';
  const curPw = document.getElementById('sec-current')?.value || '';
  const msgEl = document.getElementById('sec-msg');
  if(msgEl) { msgEl.textContent = t('prot.saving'); msgEl.style.color = 'var(--muted)'; }
  try {
    const r = await fetch('/api/set-password', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({password: newPw, current: curPw}),
    });
    const d = await r.json().catch(()=>({}));
    if(!r.ok) {
      if(msgEl) { msgEl.textContent = d.detail || t('ui.err_pfx')+r.status; msgEl.style.color = 'var(--red)'; }
      if(r.status === 401) {
        const curWrap = document.getElementById('sec-current-wrap');
        if(curWrap) curWrap.style.display = '';
      }
      return;
    }
    if(msgEl) { msgEl.textContent = d.auth_enabled ? t('prot.pw_set') : t('prot.off_done'); msgEl.style.color = 'var(--green)'; }
    document.getElementById('sec-new').value = '';
    document.getElementById('sec-current').value = '';
    loadAuthStatus();
  } catch(e) {
    if(msgEl) { msgEl.textContent = t('ui.net_err_pfx')+e.message; msgEl.style.color = 'var(--red)'; }
  }
}

async function logoutApp(){
  await fetch('/api/logout', {method:'POST'}).catch(()=>{});
  location.href = '/login';
}

// ── Сопряжение с телефоном ──────────────────────────────────────────────
async function pairStart(){
  const codeEl=document.getElementById('pair-code');
  const ttlEl=document.getElementById('pair-code-ttl');
  const msgEl=document.getElementById('pair-msg');
  if(msgEl){ msgEl.textContent=''; }
  try{
    const r=await fetch('/api/pair/start',{method:'POST'});
    const d=await r.json().catch(()=>({}));
    if(!r.ok){
      if(msgEl){ msgEl.textContent=(d.detail||d.error||('HTTP '+r.status)); msgEl.style.color='var(--red)'; }
      return;
    }
    if(codeEl) codeEl.textContent=d.code;
    const shareEl=document.getElementById('pair-share');
    if(shareEl && typeof d.share_credentials==='boolean') shareEl.checked=d.share_credentials;
    let left=(d.expires_in||300);
    if(window._pairTtlTimer) clearInterval(window._pairTtlTimer);
    const tick=()=>{
      if(!ttlEl) return;
      if(left<=0){ ttlEl.textContent=t('s.pair_expired'); if(codeEl) codeEl.textContent='— — —'; clearInterval(window._pairTtlTimer); return; }
      const m=Math.floor(left/60), s=(''+(left%60)).padStart(2,'0');
      ttlEl.textContent=t('s.pair_valid')+' '+m+':'+s; left--;
    };
    tick(); window._pairTtlTimer=setInterval(tick,1000);
  }catch(e){ if(msgEl){ msgEl.textContent=t('ui.net_err_pfx')+e.message; msgEl.style.color='var(--red)'; } }
}

async function pairShare(on){
  const msgEl=document.getElementById('pair-msg');
  try{
    const r=await fetch('/api/pair/share',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:!!on})});
    const d=await r.json().catch(()=>({}));
    if(msgEl){
      msgEl.textContent = r.ok ? (on?t('s.pair_share_on'):t('s.pair_share_off')) : (d.detail||d.error||('HTTP '+r.status));
      msgEl.style.color = r.ok ? 'var(--green)' : 'var(--red)';
    }
  }catch(e){ if(msgEl){ msgEl.textContent=t('ui.net_err_pfx')+e.message; msgEl.style.color='var(--red)'; } }
}

async function pairRevokeAll(){
  const msgEl=document.getElementById('pair-msg');
  if(!confirm(t('s.pair_revoke_confirm'))) return;
  try{
    const r=await fetch('/api/pair/revoke-all',{method:'POST'});
    const d=await r.json().catch(()=>({}));
    if(msgEl){
      msgEl.textContent = r.ok ? (t('s.pair_revoked')+' ('+(d.revoked||0)+')') : (d.detail||d.error||('HTTP '+r.status));
      msgEl.style.color = r.ok ? 'var(--green)' : 'var(--red)';
    }
    const codeEl=document.getElementById('pair-code'); if(codeEl) codeEl.textContent='';
    const ttlEl=document.getElementById('pair-code-ttl'); if(ttlEl) ttlEl.textContent='';
  }catch(e){ if(msgEl){ msgEl.textContent=t('ui.net_err_pfx')+e.message; msgEl.style.color='var(--red)'; } }
}

function _pairRel(ts){
  if(!ts) return '';
  const s=Math.max(0,Math.floor(Date.now()/1000)-ts);
  if(s<60) return s+' '+t('s.pair_sec');
  if(s<3600) return Math.floor(s/60)+' '+t('s.pair_min');
  if(s<86400) return Math.floor(s/3600)+' '+t('s.pair_hr');
  return Math.floor(s/86400)+' '+t('s.pair_day');
}
async function pairFillHint(){
  const el=document.getElementById('pair-addr-hint');
  if(!el) return;
  const host=(location.hostname && location.hostname!=='localhost') ? location.hostname : '127.0.0.1';
  const addrLine = t('s.pair_addr_hint')+' <code>http://'+host+':7799</code> · '+t('s.pair_emu')+' <code>http://10.0.2.2:7799</code>';
  try{
    const r=await fetch('/api/pair/status'); const d=await r.json().catch(()=>({}));
    const shareEl=document.getElementById('pair-share');
    if(shareEl && d && typeof d.share_credentials==='boolean') shareEl.checked=d.share_credentials;
    let statusLine='';
    const n=(d.devices||[]).length;
    if(n>0){
      const online=d.online_devices||0;
      const last=Math.max(0,...(d.devices||[]).map(x=>x.seen||0));
      statusLine = '<div style="margin-top:6px;color:'+(online>0?'var(--green)':'var(--muted)')+'">📱 '
        + ti('s.pair_devices',{n:n}) + (online>0 ? ' · '+ti('s.pair_online',{n:online}) : '')
        + (last ? ' · '+t('s.pair_lastseen')+' '+_pairRel(last) : '') + '</div>';
    }
    el.innerHTML = addrLine + statusLine;
  }catch(e){ el.innerHTML = addrLine; }
}

// country code → flag emoji
function _countryFlag(code) {
  if(!code || code.length !== 2) return '';
  const c = code.toUpperCase();
  return String.fromCodePoint(0x1F1E6+c.charCodeAt(0)-65, 0x1F1E6+c.charCodeAt(1)-65);
}

