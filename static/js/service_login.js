/* Единый вход в сервисы.
 *
 * Кнопка открывает окно входа сервиса в отдельном пустом профиле браузера и
 * ждёт, пока после успешного входа появится нужный токен — вместо инструкции
 * «открой DevTools, найди вкладку Application, скопируй куку», которую половина
 * людей не доводит до конца. Пароль вводится на странице самого сервиса и через
 * нас не проходит.
 */

const _SVC_LOGIN_POLL_MS = 1500;
const _svcLoginTimers = {};

/* Открыть внешнюю страницу так, чтобы это работало И в браузере, И в окне
 * Ripster.exe. Окно приложения — WebView2, и он режет window.open() молча:
 * возвращается null, как при запрете попапов. Из-за этого в окне не работал ни
 * один внешний вход. Если попап не открылся — просим сервер открыть системным
 * браузером. Относительный путь превращаем в полный здесь, на клиенте.
 */
async function openExternal(url, name, features) {
  let w = null;
  try { w = window.open(url, name || '_blank', features || 'noopener'); } catch (e) {}
  if (w) return w;
  let abs = url;
  try { abs = new URL(url, location.origin).href; } catch (e) {}
  let failed = '';
  try {
    const r = await api('POST', '/api/open-url', { url: abs });
    if (!r || !r.ok) failed = (r && r.error) || t('sl.open_fail');
    else if (window.toast) toast(t('sl.opened_ext'), 'var(--green)', 7000);
  } catch (e) {
    failed = t('sl.open_fail');
  }
  if (failed) {
    // Никогда не оставляем человека в тупике: если ни попап, ни системный
    // браузер не сработали (например, Ripster открыт через туннель — тогда
    // сервер и не должен открывать браузер хозяина), отдаём ссылку на руки.
    if (window.toast) toast(failed, 'var(--orange)', 9000);
    try { await navigator.clipboard.writeText(abs); if (window.toast) toast(t('sl.link_copied'), 'var(--green)', 9000); }
    catch (e) { try { window.prompt(t('sl.copy_manual'), abs); } catch (e2) {} }
  }
  return null;
}

/* Выбор способа открыть страницу входа сервиса.
 *
 * Раньше вход всегда начинался с window.open(). В окне Ripster.exe это WebView2,
 * и он режет попапы молча — возвращается null, ровно как при блокировщике в
 * браузере. Пользователь видел «попап заблокирован», разрешал попапы, и ничего
 * не менялось: разрешать было нечего. Поэтому спрашиваем явно и даём вариант,
 * который заблокировать невозможно в принципе — переход на страницу входа прямо
 * в этом окне. Обратно в Ripster возвращает сам helper (RIPSTER_RETURN_URL).
 *
 * ПРАВИЛО: любой вход в аккаунт любого сервиса обязан идти через эту функцию.
 * Ни один вход не имеет права начинаться с голого window.open() — в окне
 * Ripster.exe он не открывается никогда, и человек остаётся без входа вовсе.
 *
 * opts.here === false — убрать вариант «прямо здесь». Нужен для device-flow
 * (Tidal TV, Яндекс): там код показан в самом Ripster, и увести окно на
 * страницу сервиса значит потерять код и опрос из виду. Там остаются внешний
 * браузер и ссылка — этого достаточно, тупика не возникает.
 *
 * Возвращает 'here' | 'external' | 'copy' | null (отменено).
 */
function openAuthPage(url, title, opts) {
  const allowHere = !(opts && opts.here === false);
  return new Promise((resolve) => {
    const ov = document.createElement('div');
    ov.style.cssText = 'position:fixed;inset:0;z-index:100000;background:rgba(0,0,0,.6);'
      + 'display:flex;align-items:center;justify-content:center;padding:20px';
    const btn = (label, sub, accent) =>
      `<button data-act style="display:block;width:100%;text-align:left;margin-bottom:8px;padding:11px 13px;`
      + `background:${accent ? 'rgba(29,185,84,.14)' : 'var(--surface,#1a1a20)'};`
      + `border:1px solid ${accent ? 'rgba(29,185,84,.45)' : 'var(--border,#2a2a32)'};border-radius:9px;`
      + `color:var(--text,#f0f0f4);cursor:pointer;font-family:var(--font)">`
      + `<div style="font-size:12px;font-weight:700">${label}</div>`
      + `<div style="font-size:10px;color:var(--muted,#9a9aa4);margin-top:2px">${sub}</div></button>`;
    ov.innerHTML = `<div onclick="event.stopPropagation()" style="max-width:400px;width:100%;`
      + `background:var(--surface,#15151a);border:1px solid var(--border,#2a2a32);border-radius:14px;`
      + `padding:20px 22px;box-shadow:0 20px 60px rgba(0,0,0,.55)">`
      + `<div style="font-size:14px;font-weight:700;margin-bottom:4px">${esc(title || t('sl.how_open'))}</div>`
      + `<div style="font-size:11px;color:var(--muted,#9a9aa4);line-height:1.6;margin-bottom:14px">${t('sl.how_open_sub')}</div>`
      + (allowHere ? btn(t('sl.open_here'), t('sl.open_here_sub'), true) : '')
      + btn(t('sl.open_browser'), t('sl.open_browser_sub'), !allowHere)
      + btn(t('sl.copy_link'), t('sl.copy_link_sub'), false)
      + `<button data-act style="width:100%;margin-top:4px;padding:8px;background:transparent;`
      + `border:none;color:var(--muted,#9a9aa4);font-size:11px;cursor:pointer;font-family:var(--font)">`
      + `${t('sl.cancel')}</button></div>`;
    const acts = allowHere ? ['here', 'external', 'copy', null] : ['external', 'copy', null];
    ov.querySelectorAll('[data-act]').forEach((b, i) => {
      b.onclick = async () => {
        ov.remove();
        const act = acts[i];
        if (act === 'here') {
          location.href = url;                    // нечего блокировать
        } else if (act === 'external') {
          await openExternal(url);
        } else if (act === 'copy') {
          try { await navigator.clipboard.writeText(url); if (window.toast) toast(t('sl.link_copied'), 'var(--green)', 8000); }
          catch (e) { window.prompt(t('sl.copy_manual'), url); }
        }
        resolve(act);
      };
    });
    ov.onclick = () => { ov.remove(); resolve(null); };
    document.body.appendChild(ov);
  });
}

async function svcLogin(service, inputId, btn) {
  const statusId = 'svc-login-st-' + service;
  let stEl = document.getElementById(statusId);
  if (!stEl && btn) {
    stEl = document.createElement('div');
    stEl.id = statusId;
    stEl.style.cssText = 'font-size:10px;margin-top:6px;line-height:1.5';
    btn.parentNode.appendChild(stEl);
  }
  const say = (msg, color) => {
    if (stEl) { stEl.textContent = msg; stEl.style.color = color || 'var(--muted)'; }
  };
  const done = () => {
    if (_svcLoginTimers[service]) { clearInterval(_svcLoginTimers[service]); delete _svcLoginTimers[service]; }
    if (btn) { btn.disabled = false; btn.textContent = '🔐 ' + t('sl.login'); }
  };

  if (btn) { btn.disabled = true; btn.textContent = '⏳ ' + t('sl.opening'); }
  say(t('sl.opening'), '#0a84ff');

  const r = await api('POST', `/api/login/${service}/start`);
  if (!r || !r.ok) {
    say(r?.error || t('sl.start_fail'), 'var(--red)');
    done();
    return;
  }
  say(r.hint || t('sl.waiting'), '#0a84ff');

  _svcLoginTimers[service] = setInterval(async () => {
    let s;
    try { s = await api('GET', `/api/login/${service}/status`); } catch (e) { return; }
    if (!s || s.state === 'waiting') {
      const sec = (s && s.waiting_sec) || 0;
      say((r.hint || t('sl.waiting')) + (sec > 5 ? ` (${sec} ${t('sl.sec_short')})` : ''), '#0a84ff');
      return;
    }
    done();
    if (s.state === 'done') {
      say(ti('sl.ok', { n: s.token_len || 0 }), 'var(--green)');
      if (window.toast) toast(ti('sl.ok_toast', { svc: service }), 'var(--green)', 8000);
      // подставляем добытое значение в поле, чтобы человек видел результат;
      // сервер его уже сохранил, так что повторно ничего не пишем
      const inp = inputId && document.getElementById(inputId);
      if (inp) {
        try {
          const cfg = await api('GET', '/api/config');
          const key = (s.saved_key || '');
          if (cfg && key && cfg[key]) inp.value = cfg[key];
        } catch (e) { /* поле просто останется пустым на вид */ }
      }
    } else if (s.state === 'cancelled') {
      say(t('sl.cancelled'), 'var(--muted)');
    } else {
      say(s.error || t('sl.fail'), 'var(--orange)');
    }
  }, _SVC_LOGIN_POLL_MS);
}

function svcLoginCancel(service) {
  if (_svcLoginTimers[service]) { clearInterval(_svcLoginTimers[service]); delete _svcLoginTimers[service]; }
  api('DELETE', `/api/login/${service}/cancel`);
}
