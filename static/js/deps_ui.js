// ======================================================================
// Dependency updates (owner Settings)
// Extracted from app.js (mechanical split — same global functions, no behaviour
// change). Loaded AFTER app.js in index.html, so it sees S/api/toast/etc.
// ======================================================================

// ── Dependency updates (owner-only, Settings → О сервисе) ──────────────────
async function loadDeps() {
  const box = document.getElementById('deps-list');
  if (box) box.innerHTML = '⏳ Проверяю pip (может занять до минуты)…';
  try {
    const r = await api('GET', '/api/admin/deps');
    const pkgs = r.packages || [];
    if (!pkgs.length) { box.innerHTML = '✅ Всё актуально — устаревших пакетов нет.'; return; }
    box.innerHTML = pkgs.map(p => {
      const pin = p.pinned ? ' <span title="закреплён — обновление ломает сборку">📌</span>' : '';
      const col = p.pinned ? '#ffb84d' : 'var(--text)';
      return `<div style="display:flex;justify-content:space-between;align-items:center;gap:8px;padding:5px 0;border-bottom:1px solid #ffffff11">
        <span style="color:${col};min-width:0;overflow:hidden;text-overflow:ellipsis">${esc(p.name)}${pin}
          <span style="color:var(--muted)">${esc(p.version)} → ${esc(p.latest)}</span></span>
        <button onclick="updateDep('${esc(p.name)}',${p.pinned?'true':'false'})"
          style="flex-shrink:0;padding:4px 10px;border-radius:7px;border:1px solid var(--red);background:transparent;color:var(--text);cursor:pointer;font-size:12px">⬆</button>
      </div>`;
    }).join('');
  } catch (e) { box.innerHTML = '⛔ ' + esc(e.message || e); }
}
async function updateDep(pkg, pinned) {
  if (pinned && !confirm(pkg + ' закреплён — обновление может сломать сборку (Qobuz/AMD/Widevine). Точно обновить?')) return;
  const box = document.getElementById('deps-list');
  if (box) box.innerHTML = '⏳ Обновляю ' + esc(pkg) + '… (до 10 мин)';
  try {
    const r = await api('POST', '/api/admin/deps/update', { package: pkg, force: !!pinned });
    if (r.pinned) { alert(r.msg); loadDeps(); return; }
    alert((r.ok ? '✅ ' : '⚠️ ') + pkg + ' — ' + (r.ok ? 'обновлён. Нужен рестарт app.py.' : 'не удалось, см. консоль.'));
    loadDeps();
  } catch (e) { alert('⛔ ' + (e.message || e)); loadDeps(); }
}
async function updateAllDeps() {
  if (!confirm('Обновить ВСЕ незакреплённые пакеты? Закреплённые (📌) не трогаются. Может занять время; после — рестарт app.py.')) return;
  const box = document.getElementById('deps-list');
  if (box) box.innerHTML = '⏳ Обновляю все незакреплённые… (это долго)';
  try {
    const r = await api('POST', '/api/admin/deps/update', { package: 'all' });
    const n = (r.updated || []).length;
    alert((r.ok ? '✅ ' : '⚠️ ') + 'Обработано пакетов: ' + n + (r.msg ? ('\n' + r.msg) : '') + '\nНужен рестарт app.py.');
    loadDeps();
  } catch (e) { alert('⛔ ' + (e.message || e)); loadDeps(); }
}

async function saveSetting(key, value) {
  const configKey = SETTING_KEY_MAP[key] || key;
  const _triggerEl = document.activeElement;
  // GUEST PATH: never write to server config. Store locally only.
  if (typeof _isGuest === 'function' && _isGuest()) {
    if (!_isGuestWritable(configKey)) {
      console.warn(`[guest] dropping write of '${configKey}' — owner-only setting`);
      return;
    }
    S.config[configKey] = value;
    _guestPrefsSave(configKey, value);
    if (configKey === 'quality') renderQualityGrid?.();
    else _showSavedChip(_triggerEl);
    return;
  }
  // OWNER PATH (server)
  const SECRET_KEYS = new Set(['qobuz-auth-token','qobuz-password','deezer-arl','tidal-token','tidal-refresh','media-user-token','authorization-token','qobuz-secrets','spotify-sp-dc']);
  if (SECRET_KEYS.has(configKey) && !value) return;
  S.config[configKey] = value;
  await api('POST','/api/config',{[configKey]:value});
  if(configKey==='quality') renderQualityGrid();
  else _showSavedChip(_triggerEl);
  if(configKey.startsWith('releases-') || configKey === 'qobuz-auth-token' || configKey === 'tidal-token') _syncReleasesSettingsTab();
  renderConfig();
}

