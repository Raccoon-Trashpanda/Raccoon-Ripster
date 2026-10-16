// Выбор режима источника при первом запуске: свои ключи / Премиум Ripster / оба.
// Бэкенд: GET /api/premium/state, POST /api/premium/activate, POST /api/config {source-mode}.
// Тексты — только через i18n (ключи sm.*). Ключ поддержки нигде не сохраняется на клиенте.
(function () {
  'use strict';
  const T = (k, fb, p) => {
    try { const v = (p ? ti(k, p) : t(k)); return v && v !== k ? v : fb; } catch (e) { return fb; }
  };
  const post = (path, body) => api('POST', path, body);
  let shown = false;

  function el(tag, css, text) {
    const e = document.createElement(tag);
    if (css) e.style.cssText = css;
    if (text != null) e.textContent = text;
    return e;
  }

  function errText(code) {
    const known = ['invalid_key', 'unreachable', 'busy', 'flex_disabled'];
    return T('sm.err.' + (known.includes(code) ? code : 'other'), 'Не удалось подключить Премиум.');
  }

  async function saveMode(mode) {
    await post('/api/config', { 'source-mode': mode });
    if (window.S && S.config) S.config['source-mode'] = mode;
    try { if (typeof _refreshSearchSvcSelect === 'function') _refreshSearchSvcSelect(); } catch (e) {}
  }

  function close(root) { root.remove(); shown = false; }

  function open(force) {
    if (shown && !force) return;
    shown = true;
    const root = el('div', 'position:fixed;inset:0;z-index:99999;background:rgba(0,0,0,.72);display:flex;align-items:center;justify-content:center;padding:16px');
    root.id = 'sm-overlay';
    const card = el('div', 'box-sizing:border-box;background:var(--surface,#16181d);color:var(--text,#eee);border:1px solid var(--border,#333);border-radius:14px;max-width:460px;width:100%;padding:22px;display:flex;flex-direction:column;gap:12px;max-height:92vh;overflow:auto');
    root.appendChild(card);
    card.appendChild(el('div', 'font-size:20px;font-weight:800', T('sm.title', 'Как будете качать?')));
    card.appendChild(el('div', 'font-size:13px;opacity:.7', T('sm.sub', 'Выберите источник. Позже это можно изменить в настройках.')));
    const body = el('div', 'display:flex;flex-direction:column;gap:10px');
    card.appendChild(body);

    function option(title, sub, onclick) {
      const b = el('button', 'display:flex;flex-direction:column;align-items:flex-start;gap:3px;width:100%;box-sizing:border-box;text-align:left;white-space:normal;text-transform:none;letter-spacing:0;padding:12px 14px;border-radius:10px;border:1px solid var(--border,#333);background:transparent;color:inherit;cursor:pointer');
      b.appendChild(el('div', 'font-weight:700;font-size:15px;white-space:normal', title));
      b.appendChild(el('div', 'font-size:12px;font-weight:400;opacity:.7;white-space:normal', sub));
      b.onclick = onclick;
      return b;
    }

    function chooser() {
      body.replaceChildren(
        option(T('sm.own', 'Свои ключи'), T('sm.own_sub', 'Вы входите в свои сервисы (Deezer, Tidal, Qobuz…) сами.'), async () => {
          try { await saveMode('own'); close(root); } catch (e) { toast(T('sm.err.other', 'Не удалось сохранить выбор.'), 'var(--red)'); }
        }),
        option(T('sm.premium', 'Премиум Ripster'), T('sm.premium_sub', 'Ссылки и качество приходят с сервера владельца. Нужен ключ поддержки.'), () => keyForm('premium')),
        option(T('sm.both', 'Оба режима'), T('sm.both_sub', 'Свои ключи, а Премиум добирает то, чего им не хватает.'), () => keyForm('both')),
      );
    }

    function keyForm(mode) {
      const input = el('input', 'padding:10px;border-radius:8px;border:1px solid var(--border,#333);background:transparent;color:inherit;font-size:14px;width:100%;box-sizing:border-box');
      input.placeholder = 'RP-XXXX-XXXX-XXXX';
      input.autocomplete = 'off';
      input.spellcheck = false;
      const msg = el('div', 'font-size:12px;color:var(--red,#e55);min-height:16px');
      const go = el('button', 'padding:10px;border-radius:8px;border:0;background:var(--orange,#ef9f27);color:#000;font-weight:800;cursor:pointer', T('sm.connect', 'Подключить'));
      const back = el('button', 'padding:8px;border-radius:8px;border:1px solid var(--border,#333);background:transparent;color:inherit;cursor:pointer', T('sm.back', 'Назад'));
      back.onclick = chooser;
      go.onclick = async () => {
        const key = input.value.trim();
        if (!key) { msg.textContent = T('sm.err.empty', 'Введите ключ.'); return; }
        go.disabled = true; msg.textContent = '';
        try {
          const res = await post('/api/premium/activate', { key, mode });
          if (res && res.error) { msg.textContent = errText(res.error); go.disabled = false; return; }
          await saveMode(mode);
          close(root);
          toast(T('sm.ok', 'Премиум Ripster подключён.'), 'var(--green)');
        } catch (e) {
          msg.textContent = errText('other');
          go.disabled = false;
        }
      };
      body.replaceChildren(
        el('div', 'font-size:13px', T('sm.key_label', 'Ключ поддержки')),
        input, msg, go, back,
      );
      input.focus();
    }

    chooser();
    document.body.appendChild(root);
  }

  async function init() {
    try {
      const st = await fetch('/api/premium/state').then(r => (r.ok ? r.json() : null));
      if (st && st.needs_choice) open(false);
    } catch (e) { /* не владелец или сервер старой версии — молча пропускаем */ }
  }

  window.RpSourceMode = { open: () => open(true), init };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', () => setTimeout(init, 1500));
  else setTimeout(init, 1500);
})();
