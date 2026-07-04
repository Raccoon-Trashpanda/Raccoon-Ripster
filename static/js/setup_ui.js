// ======================================================================
// Setup tab provisioning UI
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
  { key:'wvd', icon:'🔐', label:'Widevine L3 (one-click)', tag:'опционально', color:'#c084e0',
    desc:'Один клик ставит ВЕСЬ L3-тулчейн (JRE + Android SDK + эмулятор + system-image + AEHD-гипервизор + AVD) и сразу извлекает твой device.wvd → SoundCloud DRM. Один UAC на драйвер, ~5–15 мин, неск. ГБ. Нужно ТОЛЬКО для DRM-треков SoundCloud (миксы/приваты). Прогресс — в консоли ниже.',
    endpoint:'/api/widevine/mint-auto', wsdone:'widevine_minted', status:'wvd' },
  { key:'wvd-manual', icon:'🔐', label:'Widevine L3 — manual mint (wizard)', tag:'fallback', color:'#c084e0', advanced:true,
    desc:'Фолбэк: если авто-минт застрял (KeyDive завис на приветствии Chrome) — интерактивный мастер в отдельном окне.',
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
      ? `<span style="font-size:9px;color:var(--blue,#0a84ff);font-weight:800">⏳ ${t('setup.st_installing')}</span>`
      : s.error ? `<span style="font-size:9px;color:#fc3c44;font-weight:800">✗ ${t('setup.st_error')}</span>`
      : s.installed ? `<span style="font-size:9px;color:#30d158;font-weight:800">✓ ${t('setup.st_done')}</span>`
      : `<span style="font-size:9px;color:var(--muted)">${t('setup.st_none')}</span>`;
    const pct = Math.max(0, Math.min(100, s.pct || 0));
    const barShow = !!s.running || (pct > 0 && pct < 100);
    const animate = s.running && !pct;
    const tag = c.tag ? `<span style="font-size:8px;background:${c.color}22;color:${c.color};padding:1px 6px;border-radius:8px;font-weight:800;margin-left:6px">${t('setup.'+c.key+'.tag')}</span>` : '';
    const btnLabel = s.running ? ('⏳ ' + t('setup.st_installing'))
      : c.wizard   ? ('🧙 ' + t('setup.btn_wizard'))
      : s.installed ? ('↻ ' + t('setup.btn_reinstall'))
      : ('⚡ ' + t('setup.btn_install'));
    // Compact single-row card: [☐] icon label tag … status [install]. The full
    // description moved to a hover tooltip + the detailed guide below, so the list
    // is several times shorter. Progress is a thin line along the bottom edge.
    return `<div title="${esc(t('setup.'+c.key+'.desc'))}"
      style="background:var(--surface);border:1px solid ${c.color}28;border-radius:8px;padding:5px 10px;margin-bottom:5px;position:relative;overflow:hidden;display:flex;align-items:center;gap:8px">
      <input type="checkbox" ${s.checked?'checked':''} onchange="setupToggle('${c.key}',this.checked)" style="width:auto;margin:0;flex-shrink:0;cursor:pointer"/>
      <span style="font-size:13px;flex-shrink:0">${c.icon}</span>
      <span style="font-size:12px;font-weight:800;color:${c.color};font-family:var(--display);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0">${c.label}</span>
      ${tag}
      <span style="margin-left:auto;flex-shrink:0">${badge}</span>
      <button class="btn-ghost btn-sm" style="padding:2px 9px;font-size:10px;border-color:${c.color}55;color:${c.color};flex-shrink:0;white-space:nowrap"
        ${s.running||setupRunning?'disabled':''} onclick="installOne('${c.key}')">${btnLabel}</button>
      ${barShow ? `<div style="position:absolute;left:0;right:0;bottom:0;height:3px;background:rgba(255,255,255,.06)"><div style="height:100%;width:${animate?100:pct}%;background:${c.color};transition:width .3s${animate?';animation:amd-blink 1s infinite':''}"></div></div>` : ''}
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

