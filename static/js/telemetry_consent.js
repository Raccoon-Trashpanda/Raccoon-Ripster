// Первый запуск: ЯВНО спросить согласие на отправку диагностики разработчику.
//
// Вынесено из telemetry_ui.js 23.09.2026. Там жил и запрос согласия, и
// владельческий просмотрщик диагностики, а публичная сборка (github_setup)
// telemetry_ui.js не подключает вовсе — просмотрщик ей не нужен. Вместе с ним
// пропал и вопрос: у тестеров `_maybeAskTelemetryName` была не определена,
// согласие не спрашивалось никогда, отправка оставалась выключенной — и
// диагностика тестеров молчала с 24 июля. Этот файл подключают ОБЕ сборки.
//
// Спрашиваем, только когда есть куда отправлять (telemetry-url задан) и решение
// ещё не принято; никогда — у самого владельца (instance приёма). Один раз на
// машину в обе стороны: и «да», и «нет» больше не спрашиваются.
async function _maybeAskTelemetryName(){
  try {
    const c = S.config || {};
    if (c['telemetry-ingest-enabled']) return;               // owner instance — never ask
    if (!(c['telemetry-url']||'').trim()) return;             // nowhere to send — nothing to ask
    if (localStorage.getItem('tlm_consent_asked') === '1') return; // already decided here
    // НЕ window.prompt()/confirm(): WebView2 (оболочка Ripster.exe) их глушит,
    // родное окно молча не появляется. Только окно внутри страницы.
    _showFirstRunNameModal();
  } catch(e){}
}

function _showFirstRunNameModal(){
  if(document.getElementById('firstrun-name-modal')) return;
  const modal = document.createElement('div');
  modal.id = 'firstrun-name-modal';
  modal.style.cssText = 'position:fixed;inset:0;z-index:9999;display:flex;align-items:center;justify-content:center;background:rgba(0,0,0,.75);backdrop-filter:blur(4px)';
  modal.innerHTML = `<div style="background:var(--surface,#1c1c1e);border:1px solid var(--border);border-radius:16px;padding:24px;width:400px;max-width:90vw">
    <div style="font-size:16px;font-weight:700;color:var(--text);margin-bottom:6px">${esc(t('tlm.consent_title'))}</div>
    <div style="font-size:12px;color:var(--muted,#888);margin-bottom:12px">${t('tlm.consent_body')}</div>
    <input id="firstrun-name-input" type="text" maxlength="48" placeholder="${esc(t('tlm.consent_name_ph'))}"
      style="width:100%;padding:10px 12px;background:var(--surface2);border:1px solid var(--border);border-radius:9px;color:var(--text);font-size:13px;box-sizing:border-box;outline:none"
      onkeydown="if(event.key==='Enter') _saveFirstRunName(true)">
    <div style="display:flex;gap:8px;margin-top:14px">
      <button onclick="_saveFirstRunName(true)" style="flex:1;padding:10px;background:#0a84ff;border:none;border-radius:9px;cursor:pointer;color:#fff;font-weight:600;font-size:13px;font-family:var(--font)">${esc(t('tlm.consent_yes'))}</button>
      <button onclick="_saveFirstRunName(false)" style="padding:10px 16px;background:transparent;border:1px solid var(--border);border-radius:9px;cursor:pointer;font-size:13px;color:var(--muted,#888);font-family:var(--font)">${esc(t('tlm.consent_no'))}</button>
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
    toast(t(consent ? 'tlm.consent_on' : 'tlm.consent_off'), 'var(--green)');
  }catch(e){}
}
