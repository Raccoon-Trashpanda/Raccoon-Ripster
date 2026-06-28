// ======================================================================
// cookies.txt upload UI
// Extracted from app.js (mechanical split — same global functions, no behaviour
// change). Loaded AFTER app.js in index.html, so it sees S/api/toast/etc.
// ======================================================================

// ── COOKIES.TXT ────────────────────────────────────────────────
async function importCookiesFile(input) {
  const file = input.files[0];
  if(!file) return;
  const text = await file.text();
  const r    = await api('POST','/api/upload-cookies',{
    content: text,
    path: S.config['gamdl-cookies-path'] || '',
  });
  if(r.ok){
    S.config['gamdl-cookies-path'] = r.path || '';
    setVal('s-cookies-path', r.path||'');
    setVal('t-cookies-path', r.path||'');
    toast('cookies.txt импортирован ✓','var(--green)');
    checkCookies();
  } else toast('Ошибка: '+(r.detail||r.msg||''),'var(--red)');
  input.value='';
}

// Pick a cookies.txt from the file explorer for the gamdl Apple section: read it,
// drop it into the #apple-cookies textarea, and save via the same /api/apple/cookies
// endpoint as the manual paste — so users don't have to open + copy the file by hand.
async function importAppleCookiesFile(input) {
  const file = input.files[0];
  if(!file) return;
  const st = document.getElementById('apple-cookies-status');
  try {
    const text = await file.text();
    const el = document.getElementById('apple-cookies');
    if(el) el.value = text;                         // reflect what was loaded
    if(st){ st.textContent='…'; st.style.color='var(--muted)'; }
    const r = await api('POST','/api/apple/cookies',{text});
    if(r&&r.ok){
      if(st){ st.textContent = r.exists?('✅ Сохранено: '+r.lines+' cookies'+(r.looks_apple?'':' ⚠ нет apple.com')):'🗑 очищено'; st.style.color='var(--green,#30d158)'; }
      toast('cookies.txt загружен из файла ✓','var(--green)');
    } else {
      if(st){ st.textContent='✗ '+((r&&r.error)||'ошибка'); st.style.color='#fc3c44'; }
    }
  } catch(e){ if(st){ st.textContent='✗ '+e; st.style.color='#fc3c44'; } }
  input.value='';
}

async function checkCookies() {
  const statusEl  = document.getElementById('cookies-status');
  const tokEl     = document.getElementById('cookies-tok-status');
  const cookiePill= document.getElementById('cookies-pill');
  const ituaWarn  = document.getElementById('cookies-itua-warn');
  if(statusEl) statusEl.textContent = '⏳…';
  try {
    const r = await fetch('/api/check-cookies');
    const d = await r.json();
    const ok  = d.valid;

    // Show/hide itua warning — the most common failure
    if(ituaWarn) ituaWarn.style.display = (d.exists && d.has_itua===false) ? '' : 'none';

    const msg = ok
      ? `✓ ${d.account||'Apple Music'}${d.storefront?' · '+d.storefront.toUpperCase():''}  (${d.lines||0} cookies)`
      : `✗ ${d.msg||'Invalid'}`;
    const col = ok ? 'var(--green)' : (d.has_itua===false ? 'var(--orange)' : 'var(--red)');
    if(statusEl){ statusEl.textContent=msg; statusEl.style.color=col; }
    if(tokEl)   { tokEl.textContent=msg;    tokEl.style.color=col;    }
    if(cookiePill){
      cookiePill.className = 'pill '+(ok?'pill-ok':'pill-err');
      cookiePill.innerHTML = `<div class="dot"></div>Cookies: ${ok?'OK ✓':d.has_itua===false?'No itua':'✗'}`;
      cookiePill.title = msg;
    }
  } catch(e) {
    if(statusEl){ statusEl.textContent='✗ Ошибка'; statusEl.style.color='var(--red)'; }
  }
}

async function checkAMDStatus() {
  try {
    const d = await (await fetch('/api/amd/status')).json();
    const dot=document.getElementById('amd-status-dot');
    const txt=document.getElementById('amd-status-text');
    const btn=document.getElementById('amd-install-btn');
    if(d.cloned){
      if(dot) dot.textContent='✓';
      if(txt){txt.textContent='Установлен · '+d.path; txt.style.color='var(--green)';}
      if(btn){btn.textContent='↺ Переустановить'; btn.disabled=false;}
    } else {
      if(dot) dot.textContent='○';
      if(txt){txt.textContent='Не установлен'; txt.style.color='var(--orange)';}
      if(btn){btn.textContent='⬇ Установить'; btn.disabled=false;}
    }
  } catch(e){}
}
async function checkAMDWrapperStatus() {
  const el     = document.getElementById('amd-wm-status');
  const dotEl  = document.getElementById('amd-wm-dot');
  const txtEl  = document.getElementById('amd-wm-text');
  const cliEl  = document.getElementById('amd-wm-clients');
  const regEl  = document.getElementById('amd-wm-regions');
  if(!el) return;
  el.style.display = '';
  if(dotEl) { dotEl.textContent = '●'; dotEl.style.color = 'var(--muted)'; }
  if(txtEl)  txtEl.textContent = t('as.checking');
  if(cliEl)  cliEl.textContent = '';
  if(regEl)  regEl.textContent = '';
  try {
    const r = await api('GET', '/api/amd/wrapper-status');
    if(r.error) {
      el.style.background = 'rgba(255,69,58,.1)';
      el.style.border = '1px solid rgba(255,69,58,.2)';
      if(dotEl) { dotEl.textContent='●'; dotEl.style.color='var(--red)'; }
      if(txtEl) { txtEl.textContent=t('as.amd_unavailable'); txtEl.style.color='var(--red)'; }
      if(cliEl)  cliEl.textContent = r.error;
      return;
    }
    if(r.ready) {
      el.style.background = 'rgba(62,207,170,.08)';
      el.style.border = '1px solid rgba(62,207,170,.2)';
      if(dotEl) { dotEl.textContent='●'; dotEl.style.color='var(--green)'; }
      if(txtEl) { txtEl.textContent=t('as.amd_ready'); txtEl.style.color='var(--green)'; }
    } else {
      const hasClients = (r.client_count || 0) > 0;
      el.style.background = hasClients ? 'rgba(239,159,39,.1)' : 'rgba(255,69,58,.1)';
      el.style.border = hasClients ? '1px solid rgba(239,159,39,.25)' : '1px solid rgba(255,69,58,.2)';
      if(dotEl) { dotEl.textContent='●'; dotEl.style.color = hasClients ? 'var(--orange)' : 'var(--red)'; }
      if(txtEl) {
        txtEl.textContent = hasClients
          ? t('as.amd_working_noready')
          : t('as.amd_not_ready_noclients');
        txtEl.style.color = hasClients ? 'var(--orange)' : 'var(--red)';
      }
    }
    if(cliEl) cliEl.textContent = ti('as.amd_clients', {n: r.client_count || 0});
    if(regEl && r.regions?.length) regEl.textContent = ti('as.amd_regions', {list: r.regions.join(', ')});
    else if(regEl) regEl.textContent = t('as.amd_no_accounts');
  } catch(e) {
    if(txtEl) txtEl.textContent = ti('as.amd_error', {msg: e.message});
  }
}

async function installAMD() {
  const btn=document.getElementById('amd-install-btn');
  const txt=document.getElementById('amd-status-text');
  if(btn){btn.disabled=true; btn.textContent='⏳ Устанавливаю…';}
  if(txt){txt.textContent='Клонирование + зависимости…'; txt.style.color='var(--muted)';}
  const sNav=document.querySelector('.nav-item[data-view="setup"]');
  if(sNav) showView('setup',sNav);
  toast('⬇ Устанавливаю AMD v2…','var(--blue)');
  try { await fetch('/api/setup/amd',{method:'POST'}); }
  catch(e){ toast('Ошибка: '+e.message,'var(--red)'); if(btn){btn.disabled=false;} }
}

// ══ SEARCH ═══════════════════════════════════════════════════════
const _SEARCH_SVCS = [
  {value: 'apple',    label: '🍎 Apple Music', key: 'apple'},
  {value: 'deezer',   label: '🎵 Deezer',       key: 'deezer'},
  {value: 'qobuz',    label: '🎼 Qobuz',         key: 'qobuz'},
  {value: 'tidal',    label: '🌊 Tidal',         key: 'tidal'},
  {value: 'spotify',  label: '🟢 Spotify',       key: 'spotify'},
  {value: 'beatport', label: '🎧 Beatport',      key: 'beatport'},
  {value: 'yandex',   label: '🟡 Яндекс.Музыка', key: 'yandex'},
];

// SEARCH / BROWSE (search grid + cards + artist/album/detail pages) → moved to its own module file (see index.html).

