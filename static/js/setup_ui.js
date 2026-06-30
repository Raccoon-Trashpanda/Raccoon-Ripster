// ======================================================================
// SETUP + SELF-UPDATE (component checklist, installer, self-update, restart)
// Extracted from app.js (mechanical split — same global functions, no behaviour
// change). Loaded AFTER app.js in index.html, so it sees S/api/toast/etc.
// ======================================================================

// ── SETUP ────────────────────────────────────────────────────


// Component checklist model. Each row = a checkbox the user ticks; install runs
// the selected components in order, with a per-item bar + an overall bar.
// Each row installs ONE component and shows ITS OWN status — no bundling several
// packages under a single button, so the user can see exactly what landed and
// what didn't. Shared tools (ffmpeg / Bento4 / Node) are their own rows. Every
// install streams to the Setup console.
const SETUP_COMPONENTS = [
  // ── Apple Music ───────────────────────────────────────────────────────────
  { key:'apple', icon:'🍎', label:'Apple Music (AMD v2)', tag:'рекомендуется', color:'#fc3c44', def:true,
    desc:'Движок AppleMusicDecrypt — ALAC / AAC / Atmos через публичный wrapper (wm.wol.moe), БЕЗ Apple ID, БЕЗ Docker, БЕЗ токена. Для расшифровки нужны ещё ffmpeg и Bento4 (ниже).',
    endpoint:'/api/setup/component/apple', status:'apple' },
  { key:'ffmpeg', icon:'🎞️', label:'ffmpeg', tag:'для Apple', color:'#fc8a44', def:true,
    desc:'Ремукс/перекодирование. Нужен для Apple ALAC и общей конвертации формата вывода.',
    endpoint:'/api/setup/component/ffmpeg', status:'ffmpeg' },
  { key:'mp4decrypt', icon:'🔓', label:'Bento4 (mp4decrypt)', tag:'для Apple', color:'#fc8a44', def:true,
    desc:'Извлечение/декрипт MP4-фрагментов. Нужен для Apple ALAC и музыкальных видео.',
    endpoint:'/api/setup/component/mp4decrypt', status:'mp4decrypt' },
  // ── SoundCloud ────────────────────────────────────────────────────────────
  { key:'node', icon:'🟩', label:'Node.js', tag:'для SoundCloud', color:'#3c873a', def:true,
    desc:'Среда выполнения для Lucida. Ставится автоматически вместе с SoundCloud, но можно отдельно.',
    endpoint:'/api/setup/component/node', status:'node' },
  { key:'soundcloud', icon:'🎧', label:'SoundCloud (Lucida)', color:'#ff5500', def:true,
    desc:'Node.js + Lucida (клон исходников + npm-сборка, ~1–2 мин). Нужен только для скачивания с SoundCloud.',
    endpoint:'/api/setup/component/soundcloud', status:'soundcloud' },
  { key:'wvd', icon:'🔐', label:'Widevine L3 (один клик)', tag:'опционально', color:'#c084e0',
    desc:'Один клик ставит ВЕСЬ L3-тулчейн (JRE + Android SDK + эмулятор + system-image + AEHD-гипервизор + AVD) и сразу извлекает твой device.wvd → SoundCloud DRM. Один UAC на драйвер, ~5–15 мин, неск. ГБ. Нужно ТОЛЬКО для DRM-треков SoundCloud (миксы/приваты). Прогресс — в консоли ниже.',
    endpoint:'/api/widevine/mint-auto', wsdone:'widevine_minted', status:'wvd' },
  { key:'wvd-manual', icon:'🔑', label:'Widevine L3 — минт вручную (мастер)', tag:'fallback', color:'#c084e0', advanced:true,
    desc:'Если авто-минт застрял (например KeyDive завис на экране приветствия Chrome) — открывает интерактивный мастер в отдельном окне, где шаги выбираются вручную.',
    endpoint:'/api/widevine/mint-wizard', wizard:true, status:'wvd' },
  // ── Spotify / Beatport (OrpheusDL) ────────────────────────────────────────
  { key:'orpheus', icon:'🟢', label:'OrpheusDL (Spotify)', color:'#1db954', def:true,
    desc:'База для Spotify и Beatport — клонирует OrpheusDL + модуль Spotify. БЕЗ секретов (вход настраивается потом в Настройки → Spotify). Нативный Spotify-декрипт требует ещё Spotify.dll (отдельно).',
    endpoint:'/api/setup/component/orpheus', status:'orpheus' },
  { key:'beatport', icon:'🎚️', label:'Beatport', color:'#01f49c', def:true,
    desc:'Модуль orpheusdl-beatport поверх OrpheusDL. Если OrpheusDL не стоит — поставится автоматически.',
    endpoint:'/api/setup/component/beatport', status:'beatport' },
  // ── Advanced ──────────────────────────────────────────────────────────────
  { key:'zhaarey', icon:'⚙️', label:'Apple wrapper (zhaarey)', tag:'продвинутое', color:'#af52de',
    desc:'Go + Docker + ТВОЙ premium Apple ID (~71 МБ Go). Для ALAC/Atmos через локальный wrapper. Большинству НЕ нужно — публичного Apple Music выше достаточно для lossless.',
    endpoint:'/api/setup/component/zhaarey', advanced:true, status:'go' },
];
let setupCompState = {};   // key -> { checked, installed, running, pct, error }
let _activeSetupKey = null;
const _wsWaiters = new Map();

async function fetchSetupStatuses() {
  const st = {};
  try { const t = await api('GET','/api/tools');
        st.go         = !!(t && t.go         && t.go.found);
        st.ffmpeg     = !!(t && t.ffmpeg     && t.ffmpeg.found);
        st.mp4decrypt = !!(t && t.mp4decrypt && t.mp4decrypt.found);
  } catch {}
  try { const a = await api('GET','/api/amd/status'); st.apple = !!(a && a.cloned); } catch {}
  try { const s = await api('GET','/api/soundcloud/status');
        st.soundcloud = !!(s && s.installed);
        st.node       = !!(s && s.node_ok);
  } catch {}
  try { const w = await api('GET','/api/widevine/status'); st.wvd = !!(w && w.installed); } catch {}
  try { const o = await api('GET','/api/orpheus/status'); st.orpheus = !!(o && o.installed); } catch {}
  try { const b = await api('GET','/api/beatport/status'); st.beatport = !!(b && b.module_installed); } catch {}
  return st;
}

async function checkTools() {
  const st = await fetchSetupStatuses();
  SETUP_COMPONENTS.forEach(c => {
    if(!setupCompState[c.key]) setupCompState[c.key] = {};
    setupCompState[c.key].installed = !!st[c.status];
  });
  renderChecklist();
  updateSetupBadge();
}

function renderChecklist() {
  const wrap = document.getElementById('setup-checklist');
  if(!wrap) return;
  wrap.innerHTML = SETUP_COMPONENTS.map(c => {
    const s = setupCompState[c.key] || (setupCompState[c.key] = {});
    if(s.checked === undefined) s.checked = !!c.def;
    const badge = s.running
      ? `<span style="font-size:9px;color:var(--blue,#0a84ff);font-weight:800">⏳ устанавливаю…</span>`
      : s.error ? `<span style="font-size:9px;color:#fc3c44;font-weight:800">✗ ошибка</span>`
      : s.installed ? `<span style="font-size:9px;color:#30d158;font-weight:800">✓ установлено</span>`
      : `<span style="font-size:9px;color:var(--muted)">не установлено</span>`;
    const pct = Math.max(0, Math.min(100, s.pct || 0));
    const barShow = !!s.running || (pct > 0 && pct < 100);
    const animate = s.running && !pct;
    const tag = c.tag ? `<span style="font-size:8px;background:${c.color}22;color:${c.color};padding:1px 6px;border-radius:8px;font-weight:800;margin-left:6px">${c.tag}</span>` : '';
    const btnLabel = s.running ? '⏳ устанавливаю…'
      : c.wizard   ? '🧙 Открыть мастер'
      : s.installed ? '↻ Переустановить'
      : '⚡ Установить';
    return `<div style="background:var(--surface);border:1px solid ${c.color}28;border-radius:11px;padding:11px 13px">
      <label style="display:flex;align-items:flex-start;gap:10px;cursor:pointer;margin:0">
        <input type="checkbox" ${s.checked?'checked':''} onchange="setupToggle('${c.key}',this.checked)" style="width:auto;margin-top:2px;padding:0;background:none;border:none;flex-shrink:0"/>
        <div style="flex:1;min-width:0">
          <div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap">
            <span style="font-size:14px">${c.icon}</span>
            <span style="font-size:12.5px;font-weight:800;color:${c.color};font-family:var(--display)">${c.label}</span>
            ${tag}
            <span style="margin-left:auto">${badge}</span>
          </div>
          <div style="font-size:10.5px;color:var(--muted);line-height:1.55;margin-top:4px">${c.desc}</div>
          <div style="height:6px;background:rgba(255,255,255,.06);border-radius:5px;overflow:hidden;margin-top:8px;display:${barShow?'block':'none'}">
            <div style="height:100%;width:${animate?100:pct}%;background:${c.color};border-radius:5px;transition:width .3s${animate?';animation:amd-blink 1s infinite':''}"></div>
          </div>
        </div>
      </label>
      <div style="display:flex;justify-content:flex-end;margin-top:8px">
        <button class="btn-ghost btn-sm" style="padding:3px 11px;font-size:10.5px;border-color:${c.color}55;color:${c.color}"
          ${s.running||setupRunning?'disabled':''} onclick="installOne('${c.key}')">${btnLabel}</button>
      </div>
    </div>`;
  }).join('');
}

function setupToggle(key, checked){ (setupCompState[key] = setupCompState[key]||{}).checked = checked; }
function setupSelectAll(on){ SETUP_COMPONENTS.forEach(c => { (setupCompState[c.key]=setupCompState[c.key]||{}).checked = on; }); renderChecklist(); }

function updateSetupBadge(){
  // Nav badge = selected-but-missing components + (1 if a Ripster update is available).
  const missing = SETUP_COMPONENTS.filter(c => { const s=setupCompState[c.key]||{}; return s.checked && !s.installed; }).length;
  const upd = (_ripsterUpdate && _ripsterUpdate.available) ? 1 : 0;
  const n = missing + upd;
  const badge = document.getElementById('setup-badge');
  if(badge){ badge.style.display = n ? '' : 'none'; badge.textContent = n || ''; }
}

// ── Ripster self-update ────────────────────────────────────────────────────
let _ripsterUpdate = null;   // last /api/update/check result
let _updateApplying = false;

async function checkRipsterUpdate(silent){
  const st = document.getElementById('ripster-update-status');
  const verLine = document.getElementById('ripster-version-line');
  const applyBtn = document.getElementById('btn-update-apply');
  const log = document.getElementById('ripster-update-changelog');
  if(!silent && st){ st.textContent = 'Проверяю GitHub…'; st.style.color = 'var(--muted)'; }
  try {
    const d = await api('GET','/api/update/check');
    _ripsterUpdate = d;
    if(verLine) verLine.textContent = d.current ? ('v'+d.current) : '';
    if(!d.ok){
      if(st){ st.textContent = '✗ ' + (d.error || 'не удалось проверить'); st.style.color = '#c084a0'; }
      if(applyBtn) applyBtn.style.display = 'none';
      updateSetupBadge();
      return d;
    }
    if(d.available){
      if(st){
        const _lat = esc(String(d.latest||'').replace(/^v/i,''));   // tag is "v3.0.25" → strip leading v (no "vv")
        const _cur = esc(String(d.current||'').replace(/^v/i,''));
        const dl = d.url ? `<a href="${esc(d.url)}" target="_blank" rel="noopener" style="color:#30d158;font-weight:700;text-decoration:underline">↓ Скачать и установить v${_lat}</a>` : '';
        st.innerHTML = `🆕 Доступна <b style="color:#30d158">v${_lat}</b> (у тебя v${_cur}).`
          + `<br><span style="color:var(--muted);font-size:11px">Кнопка «Обновить сейчас» обновит только код/интерфейс. Чтобы получить новые функции уровня приложения (системный трей, память окна и т.п.) — нужно ${dl ? '' : 'скачать и установить установщик новой версии'}</span>`
          + (dl ? ` ${dl}` : '');
        st.style.color = 'var(--text)';
      }
      if(applyBtn) applyBtn.style.display = '';
      if(log && d.changelog){ log.style.display = 'block'; log.textContent = d.changelog; }
    } else {
      if(st){ st.textContent = `✓ Установлена последняя версия (v${d.current}).`; st.style.color = '#30d158'; }
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
  if(!_ripsterUpdate || !_ripsterUpdate.available){ toast('Сначала проверь обновления','var(--orange)'); return; }
  if(!confirm(`Обновить Ripster до v${_ripsterUpdate.latest}?\n\nФайлы кода и интерфейса будут перезаписаны, затем потребуется рестарт. Настройки/токены/ключи/загрузки не трогаются. При сбое — автоматический откат.`)) return;
  _updateApplying = true;
  const st = document.getElementById('ripster-update-status');
  const applyBtn = document.getElementById('btn-update-apply');
  if(applyBtn){ applyBtn.disabled = true; applyBtn.textContent = '⏳ Обновляю…'; }
  if(st){ st.textContent = '⏳ Скачиваю и применяю обновление… (не закрывай окно)'; st.style.color = 'var(--orange)'; }
  try {
    const d = await api('POST','/api/update/apply');
    if(d && d.ok){
      // New code is on disk. Auto-restart in place: /api/restart respawns the NEW
      // on-disk code and that instance takes over the port (server-side
      // _takeover_stale_server kills the stale one) — so the old "close & reopen
      // shows the old version" bug is gone. Then we wait for the new server and
      // hard-reload so the new UI loads too. No manual close needed.
      const newVer = (d.latest || (_ripsterUpdate && _ripsterUpdate.latest) || '').replace(/^v/,'');
      if(st){ st.innerHTML = '✅ Обновление установлено — перезапускаю и обновляю страницу…'; st.style.color = '#30d158'; }
      _ripsterUpdate = null;
      toast('Обновление применяется — страница перезагрузится сама','var(--green)', '', 8000);
      await fetch('/api/restart', {method:'POST'}).catch(()=>{});
      _waitForNewServerThenReload(newVer);
    } else {
      const rb = d && d.rolled_back ? ' (откат выполнен — установка в порядке)' : '';
      if(st){ st.textContent = `✗ Сбой на этапе «${(d&&d.stage)||'?'}»: ${(d&&d.error)||'?'}${rb}`; st.style.color = '#c084a0'; }
    }
  } catch(e){
    if(st){ st.textContent = '✗ ' + e.message; st.style.color = '#c084a0'; }
  } finally {
    _updateApplying = false;
    if(applyBtn){ applyBtn.disabled = false; applyBtn.textContent = '⬆️ Обновить сейчас'; }
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
      if(window.toast) toast(r&&r.ok?(r.msg||'Мастер открылся'):('✗ '+((r&&r.error)||'ошибка')), r&&r.ok?c.color:'var(--red)', '', 7000);
      s.running=false; s.pct=100;              // wizard runs in its own window
    } else if(c.wsdone){
      await api('POST', c.endpoint);            // fire-and-forget; wait for completion WS
      await waitForWs(c.wsdone, 900000).catch(()=>{});
      s.running=false; s.pct=100; s.installed=true;
    } else {
      const r = await api('POST', c.endpoint);  // await-style component endpoint
      s.running=false; s.pct=100; s.installed = !!(r&&r.ok);
      if(r && !r.ok){ s.error = true; if(window.toast) toast(c.label+': '+(r.error||'ошибка'),'var(--red)'); }
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
  if(!sel.length){ toast('Отметь галочками, что установить','var(--orange)'); return; }
  setupRunning = true;
  const btn = document.getElementById('btn-setup');
  if(btn){ btn.disabled = true; btn.textContent = '⏳ Устанавливаю…'; }
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
  setOverall('Готово ✓', 100);
  if(btn){ btn.disabled=false; btn.textContent='⚡ Установить выбранное'; }
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
  toast('Перезапуск… жду новый сервер', 'var(--orange)');
  // Don't blind-reload after a fixed delay — the fresh server may not be bound yet
  // (you'd land on a dead page, the "обновление зависло" bug). Poll /api/ping:
  // reload once the NEW server answers — either after we saw the old one drop, or
  // after enough time that the old (1.5s-delayed) exit + restart is surely done.
  const started = Date.now();
  let sawDown = false;
  const ping = async () => {
    try { const r = await fetch('/api/ping', {cache:'no-store'}); return r.ok; }
    catch(e){ return false; }
  };
  const tick = async () => {
    const elapsed = Date.now() - started;
    if (elapsed > 90000) { location.reload(); return; }   // fallback: reload anyway
    const up = await ping();
    if (!up) sawDown = true;
    if (up && (sawDown || elapsed > 8000)) { location.reload(); return; }
    setTimeout(tick, 800);
  };
  setTimeout(tick, 1000);
}
