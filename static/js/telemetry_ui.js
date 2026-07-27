// ======================================================================
// TELEMETRY owner tester diagnostics
// Extracted from app.js (mechanical split — same global functions, no behaviour
// change). Loaded AFTER app.js in index.html, so it sees S/api/toast/etc.
// ======================================================================

// ── TELEMETRY (owner: tester diagnostics) ─────────────────────────────────────
let _tlmSel = null, _tlmTimer = null, _tlmLines = [];
function telemetryInit() { tlmRefresh(); }

async function tlmRefresh() {
  try {
    const d = await api('GET', '/api/telemetry/instances');
    const list = (d && d.instances) || [];
    const off = document.getElementById('tlm-ingest-off');
    if (off) off.style.display = (d && d.ingest_enabled) ? 'none' : '';
    const sum = document.getElementById('tlm-summary');
    if (sum) sum.textContent = list.length ? `${list.length} инстанс(ов)` : 'пока никто не отправлял';
    tlmRenderInstances(list);
    if (_tlmSel) await tlmRenderLines();
  } catch (e) { /* owner-only / offline */ }
  tlmRenderReports();
}

// Повторная отправка себе в Telegram — на случай, если бот в тот момент лежал.
async function tlmPushBot(code, btn) {
  const orig = btn ? btn.textContent : '';
  if (btn) { btn.disabled = true; btn.textContent = '⏳'; }
  try {
    const r = await api('POST', '/api/telemetry/reports/push-bot', { code });
    const ok = r && r.ok;
    if (window.toast) {
      toast(ok ? `➤ Отчёт ${code} отправлен в бота`
               : `Не отправилось: ${(r?.failed?.[0]?.info) || (r?.error) || '—'}`,
            ok ? 'var(--green)' : 'var(--orange)', 9000);
    }
  } catch (e) {
    if (window.toast) toast('Не отправилось: ' + ((e && e.message) || e), 'var(--red)');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = orig; }
  }
}

// Архивы, присланные кнопкой «Отправить разработчику». Код рядом с записью —
// тот самый, который человек называет в переписке, чтобы не искать по времени.
async function tlmRenderReports() {
  const el = document.getElementById('tlm-reports');
  if (!el) return;
  let list = [];
  try {
    const d = await api('GET', '/api/telemetry/reports');
    list = (d && d.reports) || [];
  } catch (e) { return; }

  const cnt = document.getElementById('tlm-reports-count');
  if (cnt) cnt.textContent = list.length ? `${list.length} шт.` : '';
  if (!list.length) {
    el.innerHTML = '<div style="padding:14px;opacity:.6">Пока никто не присылал</div>';
    return;
  }
  el.innerHTML = list.map(r => {
    const when = new Date((r.t || 0) * 1000).toLocaleString();
    const who  = r.name || r.instance_id || '—';
    const kb   = Math.round((r.size || 0) / 1024);
    return `<div style="padding:9px 12px;border-bottom:1px solid var(--border,#333);display:flex;gap:10px;align-items:center;flex-wrap:wrap">
      <code style="background:rgba(255,255,255,.08);padding:2px 7px;border-radius:5px;font-weight:600">${esc(r.code || '')}</code>
      <span>${esc(who)}</span>
      <span style="opacity:.6;font-size:12px">${esc(r.app_version || '')} · ${esc(when)} · ${kb} КБ</span>
      ${r.note ? `<span style="opacity:.85;font-size:12px;flex-basis:100%">💬 ${esc(r.note)}</span>` : ''}
      <button class="btn" style="margin-left:auto" onclick="tlmPushBot('${esc(r.code || '')}',this)" title="Переслать себе в Telegram">➤ В бота</button>
      <a class="btn" href="/api/telemetry/report/${encodeURIComponent(r.code || '')}">⬇ Скачать</a>
    </div>`;
  }).join('');
}

function tlmRenderInstances(list) {
  const el = document.getElementById('tlm-instances');
  if (!el) return;
  if (!list.length) { el.innerHTML = '<div style="padding:16px;opacity:.6">Нет данных. Тестеры пришлют логи при ошибках/варнингах.</div>'; return; }
  const now = Math.floor(Date.now()/1000);
  el.innerHTML = list.map(r => {
    const ago = _tlmAgo(now - (r.last_seen||0));
    const sel = (r.instance_id === _tlmSel) ? 'background:rgba(122,162,255,.15)' : '';
    const errBadge = r.errors ? `<span style="color:#ff6b6b">⛔ ${r.errors}</span>` : '';
    const display = r.label || r.name || r.instance_id;   // owner label > tester name > id
    const idHint = (display !== r.instance_id) ? `<span style="opacity:.4;font-size:10.5px">#${_tlmEsc(r.instance_id)}</span>` : '';
    return `<div style="padding:10px 12px;border-bottom:1px solid var(--border,#333);${sel}">
      <div style="display:flex;align-items:center;gap:6px">
        <div onclick="tlmSelect('${_tlmEsc(r.instance_id)}')" style="font-weight:600;cursor:pointer;flex:1">${_tlmEsc(display)} ${errBadge}</div>
        <button onclick="event.stopPropagation();tlmRename('${_tlmEsc(r.instance_id)}','${_tlmEsc(r.label||r.name||'')}')" title="Переименовать" style="background:none;border:0;cursor:pointer;opacity:.6;font-size:13px">✏️</button>
      </div>
      <div onclick="tlmSelect('${_tlmEsc(r.instance_id)}')" style="cursor:pointer">
        <div style="font-size:11.5px;opacity:.65">${idHint} v${_tlmEsc(r.app_version||'?')} · ${_tlmEsc(r.platform||'?')} · ${ago}</div>
        <div style="font-size:11px;opacity:.5">строк: ${r.total||0}</div>
      </div>
    </div>`;
  }).join('');
}

// First-run: explicitly ASK CONSENT to forward diagnostics (WARN/ERROR console
// lines) to the developer. Only when a destination is actually configured
// (telemetry-url set — normally only on tester builds the owner hands out;
// the public config ships with it blank) and forwarding hasn't been decided
// yet. Never asks on the owner/ingest instance, and asks at most once per
// machine either way (opting in AND declining both suppress future asks).
async function _maybeAskTelemetryName(){
  try {
    const c = S.config || {};
    if (c['telemetry-ingest-enabled']) return;               // owner instance — never ask
    if (!(c['telemetry-url']||'').trim()) return;             // nowhere to send — nothing to ask
    if (localStorage.getItem('tlm_consent_asked') === '1') return; // already decided here
    // NOTE: do NOT use window.prompt()/confirm() — WebView2 (the pywebview
    // backend on Windows, which is what the Ripster.exe launcher uses)
    // suppresses them entirely, so a native prompt silently never appears.
    // Use an in-page modal instead.
    _showFirstRunNameModal();
  } catch(e){}
}

function _showFirstRunNameModal(){
  if(document.getElementById('firstrun-name-modal')) return;
  const modal = document.createElement('div');
  modal.id = 'firstrun-name-modal';
  modal.style.cssText = 'position:fixed;inset:0;z-index:9999;display:flex;align-items:center;justify-content:center;background:rgba(0,0,0,.75);backdrop-filter:blur(4px)';
  modal.innerHTML = `<div style="background:var(--surface,#1c1c1e);border:1px solid var(--border);border-radius:16px;padding:24px;width:400px;max-width:90vw">
    <div style="font-size:16px;font-weight:700;color:var(--text);margin-bottom:6px">👋 Отправлять диагностику разработчику?</div>
    <div style="font-size:12px;color:var(--muted,#888);margin-bottom:12px">Если включишь — при ошибках/варнингах в консоли эти строки (с вырезанными токенами/паролями) уходят разработчику, чтобы помочь чинить баги без переписки. <b>Ничего больше</b>: ни музыки, ни личных файлов, ни полных логов. Можно выключить в любой момент в Настройках.</div>
    <input id="firstrun-name-input" type="text" maxlength="48" placeholder="Имя/ник (необязательно, чтобы разработчик понял, чей это инстанс)"
      style="width:100%;padding:10px 12px;background:var(--surface2);border:1px solid var(--border);border-radius:9px;color:var(--text);font-size:13px;box-sizing:border-box;outline:none"
      onkeydown="if(event.key==='Enter') _saveFirstRunName(true)">
    <div style="display:flex;gap:8px;margin-top:14px">
      <button onclick="_saveFirstRunName(true)" style="flex:1;padding:10px;background:#0a84ff;border:none;border-radius:9px;cursor:pointer;color:#fff;font-weight:600;font-size:13px;font-family:var(--font)">Включить отправку</button>
      <button onclick="_saveFirstRunName(false)" style="padding:10px 16px;background:transparent;border:1px solid var(--border);border-radius:9px;cursor:pointer;font-size:13px;color:var(--muted,#888);font-family:var(--font)">Нет, спасибо</button>
    </div>
  </div>`;
  document.body.appendChild(modal);
  setTimeout(()=>{ const i=document.getElementById('firstrun-name-input'); if(i) i.focus(); },50);
}

async function _saveFirstRunName(consent){
  const inp=document.getElementById('firstrun-name-input');
  const name=((inp&&inp.value)||'').trim().slice(0,48);
  localStorage.setItem('tlm_consent_asked','1');
  const m=document.getElementById('firstrun-name-modal'); if(m) m.remove();
  try{
    const patch = {'telemetry-forward': consent};
    if (consent && name) patch['telemetry-name'] = name;
    await api('POST','/api/config', patch);
    if(S.config){ S.config['telemetry-forward']=consent; if(consent && name) S.config['telemetry-name']=name; }
    toast(consent ? 'Спасибо! Диагностика включена' : 'Ок, диагностика выключена', 'var(--green)');
  }catch(e){}
}

async function tlmRename(iid, cur){
  const name = prompt('Имя/метка для этого инстанса:', cur||'');
  if(name===null) return;
  try { await api('POST', `/api/telemetry/instance/${encodeURIComponent(iid)}/label`, {label:name}); } catch(e){}
  tlmRefresh();
}

async function tlmSelect(iid) { _tlmSel = iid; tlmRenderInstances_sel(); await tlmRenderLines(); }
function tlmRenderInstances_sel() {
  document.querySelectorAll('#tlm-instances > div').forEach(d => {
    d.style.background = d.textContent.trim().startsWith(_tlmSel) ? 'rgba(122,162,255,.15)' : '';
  });
}

async function tlmRenderLines() {
  if (!_tlmSel) return;
  const lvl = document.getElementById('tlm-level')?.value || '';
  const title = document.getElementById('tlm-lines-title');
  const box = document.getElementById('tlm-lines');
  const clr = document.getElementById('tlm-clear-btn'); const cp = document.getElementById('tlm-copy-btn');
  if (clr) clr.style.display = ''; if (cp) cp.style.display = '';
  if (title) title.textContent = _tlmSel;
  try {
    const d = await api('GET', `/api/telemetry/instance/${encodeURIComponent(_tlmSel)}?limit=600&level=${lvl}`);
    _tlmLines = (d && d.lines) || [];
    if (!box) return;
    if (!_tlmLines.length) { box.innerHTML = '<div style="opacity:.6">Нет строк для этого фильтра.</div>'; return; }
    box.innerHTML = _tlmLines.map(l => {
      const c = l.level==='error'||l.level==='critical' ? '#ff6b6b' : l.level==='warn'||l.level==='warning' ? '#ffcc66' : '#cfd3dc';
      const ts = new Date((l.t||0)*1000).toLocaleString();
      return `<div style="color:${c}"><span style="opacity:.45">${ts}</span>  ${_tlmEsc(l.text||'')}</div>`;
    }).join('');
    box.scrollTop = box.scrollHeight;
  } catch (e) { if (box) box.innerHTML = '<div style="color:#ff6b6b">Ошибка загрузки</div>'; }
}

function tlmToggleAuto() {
  const on = document.getElementById('tlm-auto')?.checked;
  if (_tlmTimer) { clearInterval(_tlmTimer); _tlmTimer = null; }
  if (on) _tlmTimer = setInterval(tlmRefresh, 10000);
}

async function tlmClearInstance() {
  if (!_tlmSel || !confirm(`Очистить логи инстанса ${_tlmSel}?`)) return;
  try { await api('DELETE', `/api/telemetry/instance/${encodeURIComponent(_tlmSel)}`); } catch (e) {}
  _tlmSel = null; _tlmLines = [];
  const box = document.getElementById('tlm-lines'); if (box) box.innerHTML = '<div style="opacity:.6">—</div>';
  const title = document.getElementById('tlm-lines-title'); if (title) title.textContent = 'Выбери инстанс слева';
  tlmRefresh();
}

function tlmCopyLines() {
  const txt = _tlmLines.map(l => `${new Date((l.t||0)*1000).toISOString()} [${l.level}] ${l.text}`).join('\n');
  try { navigator.clipboard.writeText(txt); toast('Скопировано', 'var(--green)'); }
  catch (e) { const ta=document.createElement('textarea'); ta.value=txt; document.body.appendChild(ta); ta.select(); document.execCommand('copy'); ta.remove(); }
}

function _tlmAgo(s) {
  if (s < 60) return 'только что'; if (s < 3600) return Math.floor(s/60)+' мин назад';
  if (s < 86400) return Math.floor(s/3600)+' ч назад'; return Math.floor(s/86400)+' дн назад';
}
function _tlmEsc(s) { return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
