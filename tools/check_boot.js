/* Реально ли ГРУЗИТСЯ интерфейс — последняя проверка перед сдачей правки фронта.
 *
 * Запуск (сервер должен быть уже поднят):
 *     node tools/check_boot.js
 * Код возврата 0 — норма, 1 — страница не собралась. Переопределяется через
 * env: RIPSTER_URL, CHROME_PATH, CDP_PORT.
 *
 * Зачем, если есть check_js_collisions и check_i18n_keys: те смотрят ФАЙЛЫ и
 * говорят «всё на месте». Чёрный экран 3.5.0 они бы не поймали — там всё и было
 * на месте, падал ЗАПУСК: один отсутствующий фрагмент ронял Promise.all, сайдбар
 * рисовался, контент оставался пустой, версия «v–», WebSocket не открывался.
 * Поэтому здесь настоящий браузер и настоящая страница, и проверяются ровно те
 * признаки: собрались ли фрагменты, открылся ли WebSocket, есть ли версия, пуста
 * ли консоль.
 *
 * Осторожность (скилл ripster-headless-verify, оба правила из инцидентов):
 *  • порт приложения только ЧИТАЕМ. Ничего на нём не поднимаем: app.py умеет
 *    вытеснять «устаревший» экземпляр, и тестовый запуск убивал живой сервер;
 *  • свой Chrome — в отдельном user-data-dir и убивается на ЛЮБОМ выходе.
 *    Чужой chrome.exe ПО ИМЕНИ не трогаем никогда: там открытые вкладки человека.
 */
const { spawn } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const CHROME = process.env.CHROME_PATH
  || 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const PORT = Number(process.env.CDP_PORT || 9333);
const APP = process.env.RIPSTER_URL || 'http://127.0.0.1:7799';
const UDD = path.join(os.tmpdir(), 'ripster-bootcheck-' + process.pid);

function cookie() {
  // Владельческая сессия из того же файла, которым пользуются curl-проверки.
  try {
    const txt = fs.readFileSync(path.join(ROOT, '_admincookies.txt'), 'utf8');
    for (const ln of txt.split(/\r?\n/)) {
      if (ln.includes('ripster-session') && !ln.startsWith('# ')) return ln.split('\t').pop().trim();
    }
  } catch (_) {}
  return '';
}

const proc = spawn(CHROME, [
  '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
  `--remote-debugging-port=${PORT}`, `--user-data-dir=${UDD}`, 'about:blank',
], { stdio: 'ignore' });
const kill = () => { try { proc.kill('SIGKILL'); } catch (_) {} };
process.on('exit', kill);
process.on('SIGINT', () => { kill(); process.exit(1); });
process.on('uncaughtException', (e) => { kill(); console.error('УПАЛ:', e.message); process.exit(1); });

const sleep = (ms) => new Promise(r => setTimeout(r, ms));

async function target() {
  for (let i = 0; i < 60; i++) {
    try {
      const r = await fetch(`http://127.0.0.1:${PORT}/json/new?about:blank`, { method: 'PUT' });
      const j = await r.json();
      if (j.webSocketDebuggerUrl) return j.webSocketDebuggerUrl;
    } catch (_) {}
    await sleep(500);
  }
  throw new Error('devtools не поднялся');
}

let _id = 0;
function cdp(ws, method, params) {
  const id = ++_id;
  return new Promise((resolve, reject) => {
    const to = setTimeout(() => reject(new Error('таймаут ' + method)), 30000);
    const on = (ev) => {
      const m = JSON.parse(ev.data);
      if (m.id !== id) return;
      clearTimeout(to); ws.removeEventListener('message', on);
      m.error ? reject(new Error(method + ': ' + m.error.message)) : resolve(m.result);
    };
    ws.addEventListener('message', on);
    ws.send(JSON.stringify({ id, method, params: params || {} }));
  });
}

async function evalJs(ws, expr) {
  const r = await cdp(ws, 'Runtime.evaluate',
    { expression: expr, returnByValue: true, awaitPromise: true });
  if (r.exceptionDetails) throw new Error('JS: ' + (r.exceptionDetails.exception?.description || expr));
  return r.result.value;
}

async function waitFor(ws, expr, ms) {
  const end = Date.now() + ms;
  while (Date.now() < end) {
    try { if (await evalJs(ws, expr)) return true; } catch (_) {}
    await sleep(500);
  }
  return false;
}

(async () => {
  const wsUrl = await target();
  const ws = new WebSocket(wsUrl);
  await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });

  const errors = [];
  ws.addEventListener('message', (ev) => {
    const m = JSON.parse(ev.data);
    if (m.method === 'Runtime.exceptionThrown') {
      errors.push('EXC ' + (m.params.exceptionDetails.exception?.description
                            || m.params.exceptionDetails.text));
    }
    if (m.method === 'Log.entryAdded' && m.params.entry.level === 'error') {
      errors.push('LOG ' + m.params.entry.text + ' ' + (m.params.entry.url || ''));
    }
    if (m.method === 'Runtime.consoleAPICalled' && m.params.type === 'error') {
      errors.push('CONSOLE ' + m.params.args.map(a => a.value ?? a.description).join(' '));
    }
  });

  await cdp(ws, 'Runtime.enable');
  await cdp(ws, 'Log.enable');
  await cdp(ws, 'Page.enable');
  await cdp(ws, 'Network.enable');
  await cdp(ws, 'Network.setCookie',
    { name: 'ripster-session', value: cookie(), domain: '127.0.0.1', path: '/' });
  await cdp(ws, 'Page.navigate', { url: APP });

  const ready = await waitFor(ws,
    "typeof t === 'function' && typeof LANG === 'object' && !!document.getElementById('view-queue')", 90000);

  // `ready` — это «каркас собрался», а не «загрузка кончилась». Версию рисует
  // loadAppInfo(), и её вызов стоит в самом конце обработчика БЕЗ await, как и
  // наполнение вьюх. Замер сразу после `ready` ловил здоровое приложение с
  // «v—» и нулём непустых экранов — то есть ровно те признаки, по которым эта
  // проверка объявляет чёрный экран. Прибор, который врёт в красную сторону,
  // приучает не верить прибору, поэтому ждём завершения, а не мгновения.
  if (ready) {
    await waitFor(ws, "/^v\d/.test((document.getElementById('tb-ver')||{}).textContent||'')", 20000);
    await waitFor(ws,
      "[...document.querySelectorAll('section.view')].every(s => s.innerHTML.trim().length > 0)", 20000);
  }

  const out = {};
  if (ready) {
    out.version = await evalJs(ws, "(document.getElementById('tb-ver')||{}).textContent");
    out.lang_keys = await evalJs(ws,
      "'ru=' + Object.keys(LANG.ru).length + ' en=' + Object.keys(LANG.en).length"
      + " + (Object.keys(LANG.ru).length === Object.keys(LANG.en).length ? ' (поровну)' : ' РАСХОЖДЕНИЕ')");
    // Ключи, добавленные правкой, передаются аргументами:
    //     node tools/check_boot.js err.my_new_key rl.another
    // Смысл в том, чтобы убедиться, что новый ключ доехал ДО БРАУЗЕРА, а не
    // просто лежит в файле: между файлом и страницей стоит кэш и `?v=`.
    const want = process.argv.slice(2).filter(a => !a.startsWith('-'));
    out.new_keys = want.length
      ? await evalJs(ws, JSON.stringify(want)
          + ".map(k => k + '=' + (LANG.ru[k] ? 'ru+' : 'RU НЕТ ') + (LANG.en[k] ? 'en' : 'EN НЕТ')).join(' | ')")
      : '(ключи не заданы — передай их аргументами)';
    out.avail_fn = await evalJs(ws, "typeof _availSummary");
    out.avail_ru = await evalJs(ws,
      "typeof _availSummary === 'function' ? _availSummary({beatport:{available:false,reason:'no_entitlement'}}) : 'НЕТ ФУНКЦИИ'");
    out.views_loaded = await evalJs(ws,
      "document.querySelectorAll('[id^=view-]').length + ' контейнеров, непустых: ' "
      + "+ Array.from(document.querySelectorAll('[id^=view-]')).filter(e=>e.innerHTML.trim()).length");
    // Именно голое имя: `ws` объявлен через let на верхнем уровне, значит на
    // window его НЕТ (скилл ripster-headless-verify, отдельный пункт).
    // 1 = OPEN. Ноль ошибок в консоли при закрытом WS — это и был чёрный экран
    // 3.5.0, поэтому проверяем отдельно.
    // Ждём именно ОТКРЫТИЯ, а не мгновенного снимка: readyState=0 сразу после
    // загрузки значит «ещё жмёт руку», и принять это за поломку так же неверно,
    // как принять за здоровье.
    const wsOpen = await waitFor(ws, "typeof ws !== 'undefined' && ws && ws.readyState === 1", 20000);
    out.ws_state = await evalJs(ws,
      "typeof ws !== 'undefined' && ws ? ('readyState=' + ws.readyState + (ws.readyState === 1 ? ' ОТКРЫТ' : ' НЕ ОТКРЫТ')) : 'ws не создан'");
    out.ws_open = wsOpen ? 'ДА' : 'НЕТ за 20с';
  }

  console.log('готовность страницы:', ready ? 'ДА' : 'НЕТ (таймаут 90с)');
  for (const [k, v] of Object.entries(out)) console.log('  ' + k + ':', v);
  const real = errors.filter(e => !/favicon|manifest\.json|apple-touch-icon/i.test(e));
  console.log('\nошибок в консоли:', real.length);
  real.slice(0, 15).forEach(e => console.log('  ' + e.slice(0, 220)));

  ws.close();
  kill();
  await sleep(500);
  const fatal = !ready || real.some(e => /is not defined|has already been declared|SyntaxError|Unexpected token/i.test(e));
  process.exit(fatal ? 1 : 0);
})();
