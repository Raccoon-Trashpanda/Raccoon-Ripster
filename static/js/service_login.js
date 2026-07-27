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
  try {
    const r = await api('POST', '/api/open-url', { url: abs });
    if (!r || !r.ok) {
      if (window.toast) toast((r && r.error) || t('sl.open_fail'), 'var(--orange)', 9000);
      return null;
    }
    if (window.toast) toast(t('sl.opened_ext'), 'var(--green)', 7000);
  } catch (e) {
    if (window.toast) toast(t('sl.open_fail'), 'var(--orange)', 9000);
  }
  return null;
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
      say((r.hint || t('sl.waiting')) + (sec > 5 ? ` (${sec} с)` : ''), '#0a84ff');
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
