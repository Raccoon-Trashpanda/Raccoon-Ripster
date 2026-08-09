// ======================================================================
// Dependency updates UI
// Extracted from app.js (mechanical split — same global functions, no behaviour
// change). Loaded AFTER app.js in index.html, so it sees S/api/toast/etc.
// ======================================================================

// ── Dependency updates (owner-only, Settings → О сервисе) ──────────────────
async function loadDeps() {
  const box = document.getElementById('deps-list');
  if (box) box.innerHTML = t('deps.checking');
  try {
    const r = await api('GET', '/api/admin/deps');
    const pkgs = r.packages || [];
    // Про остальные зависимости говорим ЧЕСТНО и отдельно: они внутри есть, но
    // обновлять их отсюда нельзя — у них взаимоисключающие версии, и апгрейд
    // ломает загрузки целиком. Раньше они лежали в общем списке с рабочей
    // кнопкой, то есть до поломки был один клик.
    const note = r.locked_count
      ? `<div style="margin-top:10px;padding:8px 10px;background:rgba(255,184,77,.08);border:1px solid rgba(255,184,77,.25);border-radius:8px;font-size:11px;color:var(--muted);line-height:1.6">
           🔒 ${esc(t('dep.locked_head'))} — ${r.locked_count}<br>${esc(r.locked_note || '')}
           <div style="margin-top:5px;opacity:.75;font-family:var(--mono);font-size:10px;word-break:break-word">${esc((r.locked_names || []).join(', '))}</div>
         </div>`
      : '';
    const safeNote = r.safe_note
      ? `<div style="margin-bottom:8px;font-size:11px;color:var(--muted);line-height:1.6;opacity:.9">${esc(r.safe_note)}</div>`
      : '';
    if (!pkgs.length) { box.innerHTML = '✅ ' + esc(t('dep.all_fresh')) + note; return; }
    box.innerHTML = pkgs.map(p => {
      return `<div style="display:flex;justify-content:space-between;align-items:center;gap:8px;padding:5px 0;border-bottom:1px solid #ffffff11">
        <span style="color:var(--text);min-width:0;overflow:hidden;text-overflow:ellipsis">${esc(p.name)}
          <span style="color:var(--muted)">${esc(p.version)} → ${esc(p.latest)}</span></span>
        <button onclick="updateDep('${esc(p.name)}')"
          style="flex-shrink:0;padding:4px 10px;border-radius:7px;border:1px solid var(--red);background:transparent;color:var(--text);cursor:pointer;font-size:12px">⬆</button>
      </div>`;
    }).join('');
    box.innerHTML = safeNote + box.innerHTML + note;
  } catch (e) { box.innerHTML = '⛔ ' + esc(e.message || e); }
}
async function updateDep(pkg) {
  const box = document.getElementById('deps-list');
  if (box) box.innerHTML = ti('deps.updating', {pkg: esc(pkg)});
  try {
    const r = await api('POST', '/api/admin/deps/update', { package: pkg });
    if (r.pinned) { alert(r.msg); loadDeps(); return; }
    alert((r.ok ? '✅ ' : '⚠️ ') + pkg + ' — ' + (r.ok ? t('deps.updated_ok') : t('deps.update_fail')));
    loadDeps();
  } catch (e) { alert('⛔ ' + (e.message || e)); loadDeps(); }
}
async function updateAllDeps() {
  // Массовое обновление убрано намеренно. Список — это «не признано ломким», а
  // не «проверено, что безопасно»: перечень закреплённых ведётся руками и
  // заведомо неполон. Обновлять пачкой то, о чём не думал, — самый быстрый
  // способ получить нерабочую сборку, причём выяснится это не сразу.
  alert(t('dep.no_bulk'));
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

