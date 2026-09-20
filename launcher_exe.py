"""Ripster.exe entry point — the FROZEN launcher (PyInstaller one-file).

Double-click Ripster.exe → starts the bundled server and shows the UI in its own
native window (pywebview / Edge WebView2). No browser tab, no .cmd/.vbs, no terminal.
Falls back to the default browser if the system webview is unavailable.

Deliberately has NO `ripster.*` imports: the frozen exe stays tiny and can't be
bricked by an app-side import error — it only orchestrates the bundled interpreter.
The real server is `python\\python.exe app.py`, started as a windowless child.

Run dir = the folder Ripster.exe lives in (the install root), resolved from
sys.executable when frozen so it never points at PyInstaller's temp _MEIPASS.
"""
from __future__ import annotations

import contextlib
import html
import json
import os
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from urllib.request import urlopen

CREATE_NO_WINDOW = 0x08000000  # Windows: child server gets no console window


def base_dir() -> Path:
    """Install root. Frozen → folder of Ripster.exe; source → this script's folder."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


BASE = base_dir()


def _echo(msg: str) -> None:
    """Console line, degrades instead of raising: a cp1251 console (pythonw has one,
    and a test harness that pipes our stdio) cannot encode ▶ or →, and an unhandled
    UnicodeEncodeError here would abort open_window / the watchdog thread."""
    try:
        print(msg, flush=True)
    except Exception:
        try:
            enc = getattr(sys.stdout, "encoding", None) or "ascii"
            print(msg.encode(enc, "replace").decode(enc, "replace"), flush=True)
        except Exception:
            pass


def _log(msg: str) -> None:
    _echo(msg)
    try:
        (BASE / "logs").mkdir(parents=True, exist_ok=True)
        with open(BASE / "logs" / "launcher.log", "a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except Exception:
        pass


def config_port() -> int:
    """The port the UI lives on. RIPSTER_PORT env wins (matches app.py's own
    precedence), then config.yaml `port`, then 7799."""
    env = os.environ.get("RIPSTER_PORT")
    if env and env.strip().isdigit():
        return int(env.strip())
    try:
        import yaml
        c = yaml.safe_load((BASE / "config.yaml").read_text(encoding="utf-8")) or {}
        return int(c.get("port", 7799))
    except Exception:
        return 7799


def server_url() -> str:
    # 127.0.0.1, NOT localhost (Spotify OAuth rejects localhost since Apr 2025).
    return f"http://127.0.0.1:{config_port()}"


def server_python() -> str:
    """A REAL interpreter to run app.py — never sys.executable when frozen,
    because that IS Ripster.exe: spawning it re-enters the launcher, which sees
    the parent's lock file, logs "already running → surfacing it, exiting" and
    dies without ever starting a server. The launcher then respawns it forever.

    Seen in the wild on 3.0.38 (path `…\\Raccoon-Ripster-3.0.38\\Raccoon-Ripster-3.0.38\\`
    = the GitHub *source* zip): that archive has no bundled `python/`, so the
    old last-resort branch handed back Ripster.exe and the app never started.
    Returns "" when no real interpreter exists — the caller reports that
    plainly instead of looping."""
    for cand in (BASE / "python" / "python.exe",
                 BASE / ".venv" / "Scripts" / "python.exe"):
        if cand.exists():
            return str(cand)
    frozen = getattr(sys, "frozen", False)
    if frozen:
        return ""          # no interpreter shipped → cannot run the server
    return sys.executable  # plain dev run: this really is a python.exe


def ripster_alive(port: int, timeout: float = 1.5) -> bool:
    """True only if OUR Ripster answers on `port` — verified via the unauth
    /api/ping marker. A foreign app squatting the port does NOT count (so we pick
    a free port instead of opening a window onto someone else's server)."""
    import json
    try:
        with urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=timeout) as r:
            return json.loads(r.read() or b"{}").get("app") == "ripster"
    except Exception:
        return False


def port_free(port: int) -> bool:
    """True if nothing is bound on 127.0.0.1:`port` (we can take it)."""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def find_free_port(start: int, tries: int = 50) -> int:
    """First free port at or after `start` (skips busy ones automatically — a clean
    install on a machine where 7799 is already taken still launches)."""
    for p in range(start, start + tries):
        if port_free(p):
            return p
    return start  # all busy in range — let the bind fail loudly downstream


def wait_for_ripster(port: int, timeout: float = 60.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if ripster_alive(port):
            return True
        time.sleep(1.0)
    return False


def start_server(port: int) -> "subprocess.Popen | None":
    """Start app.py on a SPECIFIC port. RIPSTER_PORT is exported so app.py binds
    exactly the port the launcher will open the window on (config.yaml `port` no
    longer has to agree — the launcher is the single source of truth)."""
    py = server_python()
    if not py:
        # No bundled interpreter (source zip instead of the installer). Say so
        # once, in words the user can act on, and do NOT respawn — an endless
        # restart loop is what this cost people on 3.0.38.
        _log("[launcher] НЕТ интерпретатора: в этой папке отсутствует bundled python\\.")
        _log("[launcher] Похоже, скачан архив с ИСХОДНЫМ КОДОМ вместо установщика.")
        _log("[launcher] Поставь Ripster установщиком RipsterSetup-*.exe со страницы релиза.")
        try:
            (BASE / "logs").mkdir(parents=True, exist_ok=True)
            (BASE / "logs" / "ЧИТАЙ_МЕНЯ_ошибка_запуска.txt").write_text(
                "Ripster не запустился: в папке нет встроенного Python.\n\n"
                "Скорее всего скачан архив «Source code» со страницы релиза —\n"
                "в нём нет ни Python, ни зависимостей, только исходный код.\n\n"
                "Что делать: скачать RipsterSetup-<версия>.exe со страницы релиза\n"
                "и установить им. Либо, если нужен запуск из исходников,\n"
                "создать окружение: python -m venv .venv && "
                ".venv\\Scripts\\pip install -r requirements.txt\n",
                encoding="utf-8")
        except Exception:
            pass
        return None
    env = dict(os.environ)
    env["RIPSTER_PORT"] = str(port)
    env["RIPSTER_LAUNCHER"] = "1"   # tells /api/restart we supervise it → clean exit, no os.execv console flash
    _log(f"[launcher] starting server: {py} app.py (cwd={BASE}, port={port})")
    flags = CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        return subprocess.Popen([py, "app.py"], cwd=str(BASE),
                                creationflags=flags, env=env)
    except Exception as e:
        _log(f"[launcher] failed to start server: {type(e).__name__}: {e}")
        return None


# ── Режимы запуска ───────────────────────────────────────────────────────────


def isolated() -> bool:
    """Изолированный прогон нового exe рядом с живым стеком: RIPSTER_ISOLATED=1
    (или --isolated). Не поднимает Docker и контейнеры и не трогает бота совсем:
    тест «поднял ли сторож сервер» иначе ударил бы по рабочему 7799 и по живому
    bot.py (второй экземпляр бота = двойная доставка)."""
    env = (os.environ.get("RIPSTER_ISOLATED") or "").strip().lower()
    if env in ("1", "true", "yes", "on"):
        return True
    return "--isolated" in sys.argv


def iso_blocked(key: str) -> bool:
    """В изолированном прогоне новые Docker, контейнеры и бот запрещены и
    кнопкам: проверка нового exe не должна ронять живой стек владельца."""
    return isolated() and key in ("docker", "containers", "bot")


# ── Строки интерфейса лончера ─────────────────────────────────────────────────
# Окно лончера — это страница сервера, поэтому i18n.js сюда не доезжает: свои
# ru+en по тому же принципу «ключ → язык», что и во фронтенде. Язык: env
# RIPSTER_LANG (для тестов), иначе ru — весь существующий UI лончера русский.

_UI = {
    "panel.title":         {"ru": "Ripster · состояние", "en": "Ripster · status"},
    "panel.docker":        {"ru": "Docker", "en": "Docker"},
    "panel.containers":    {"ru": "Контейнеры", "en": "Containers"},
    "panel.server":        {"ru": "Сервер", "en": "Server"},
    "panel.bot":           {"ru": "Бот", "en": "Bot"},
    "panel.tunnel":        {"ru": "Туннель", "en": "Tunnel"},
    "panel.restarted":     {"ru": "перезапуск {time}", "en": "restarted {time}"},
    "st.up":               {"ru": "работает", "en": "up"},
    "st.down":             {"ru": "не запущен", "en": "down"},
    "st.starting":         {"ru": "запускается", "en": "starting"},
    "st.restarting":       {"ru": "перезапускаем", "en": "restarting"},
    "st.stopping":         {"ru": "останавливаем", "en": "stopping"},
    "st.dead":             {"ru": "упал и не поднимается", "en": "down, not recovering"},
    "st.off":              {"ru": "выключен", "en": "off"},
    "st.not_monitored":    {"ru": "не следим", "en": "not monitored"},
    "st.no_hc":            {"ru": "нет модуля проверок", "en": "health-check module missing"},
    "st.wait_docker":      {"ru": "ждёт Docker", "en": "waiting for Docker"},
    "st.missing":          {"ru": "нет: {names}", "en": "missing: {names}"},
    "st.all":              {"ru": "{n} из {n}", "en": "{n} of {n}"},
    "st.port":             {"ru": "порт {port}", "en": "port {port}"},
    "st.no_answer":        {"ru": "не отвечает", "en": "no answer"},
    "tray.open":           {"ru": "Открыть Ripster", "en": "Open Ripster"},
    "tray.quit":           {"ru": "Выход", "en": "Выход"},
    "tray.autostart":      {"ru": "Запускать при входе", "en": "Start at sign-in"},
    "tray.overlay":        {"ru": "Панель состояния в окне", "en": "Status panel in window"},
    "tray.autostart_on":   {"ru": "Буду стартовать при входе в Windows",
                            "en": "Will start at Windows sign-in"},
    "tray.autostart_off":  {"ru": "Автозапуск при входе выключен",
                            "en": "Start-at-sign-in is off"},
    "tray.autostart_err":  {"ru": "Автозапуск не переключился: {err}",
                            "en": "Could not toggle autostart: {err}"},
    "tray.status_title":   {"ru": "Состояние", "en": "Status"},
    # Панель управления в окне (кнопки/чекбоксы) — те же ru+en, что и трей.
    "panel.start_all":     {"ru": "▶ Запустить всё", "en": "▶ Start everything"},
    "panel.autostart":     {"ru": "Запускать при входе в Windows",
                            "en": "Start at Windows sign-in"},
    "st.stopped":          {"ru": "остановлен", "en": "stopped"},
    "st.not_ours":         {"ru": "запущен не лончером — не трогаю",
                            "en": "started outside the launcher — leaving alone"},
    "st.fail":             {"ru": "не вышло: {err}", "en": "failed: {err}"},
    "splash.server_off_t": {"ru": "Сервер выключен", "en": "Server is off"},
    "splash.server_off_d": {"ru": "Включи «Сервер» чекбоксом и нажми «Запустить всё».",
                            "en": "Tick \"Server\" and press \"Start everything\"."},
}

_LANG = None


def lang() -> str:
    global _LANG
    if _LANG is None:
        env = (os.environ.get("RIPSTER_LANG") or "").strip().lower()
        _LANG = "en" if env.startswith("en") else "ru"
    return _LANG


def t(key: str, **fmt) -> str:
    d = _UI.get(key) or {}
    txt = d.get(lang()) or d.get("ru") or key
    return txt.format(**fmt) if fmt else txt


# ── Хелперы подъёма стека (общие со сторожем) ─────────────────────────────────

_HC: object | None = None
_HC_TRIED = False


def healthcheck_module():
    """`tools/ripster_healthcheck.py` как библиотека, а не второй экземпляр логики.

    Там уже живёт единственный проверенный способ поднять Docker Desktop и
    дождаться движка (`ensure_docker_engine`, из `_heal_app_down`), поднять
    контейнер (`ensure_container`) и запустить бота тем интерпретатором, который
    сторож считает «живым» (`start_bot_process`). Переписать это в лончере
    означало бы две версии одного порядка, расходящиеся при первой же правке.
    Ищем рядом с exe, а в установщике, где каталога `tools/` нет, — упакованную
    копию из временной папки PyInstaller."""
    global _HC, _HC_TRIED
    if _HC_TRIED:
        return _HC
    _HC_TRIED = True
    import importlib.util
    roots = [BASE / "tools"]
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        roots.append(Path(meipass) / "tools")
    for d in roots:
        f = d / "ripster_healthcheck.py"
        if not f.exists():
            continue
        try:
            spec = importlib.util.spec_from_file_location("ripster_healthcheck", str(f))
            m = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(m)   # только stdlib, побочных действий на импорте нет
            # У упакованной копии ROOT указывает в _MEIPASS — поправишь на корень установки,
            # иначе конфиги и cwd бота резолвятся во временную папку.
            m.ROOT = BASE
            m.CONFIG = BASE / "config.yaml"
            m.BOT_CFG = BASE / "tgbot" / "config.json"
            _HC = m
            _log(f"[launcher] health-check helpers: {f}")
            return m
        except Exception as e:
            _log(f"[launcher] health-check import failed ({f}): {type(e).__name__}: {e}")
    _log("[launcher] health-check module not found — Docker/контейнеры/бот на себя не беру")
    return None


def _hc_log(msg: str) -> None:
    """Сообщения healthcheck-хелперов (`fixed`) идут в лог лончера, а не в отчёт
    владельцу: здесь у модуля нет ни репорта, ни HANDOFF."""
    _log(f"[stack] {msg}")


# ── Панель состояния ──────────────────────────────────────────────────────────

OK, WARN, BAD, OFF = "ok", "warn", "bad", "off"
_DOT = {OK: "🟢", WARN: "🟡", BAD: "🔴", OFF: "⚪"}
_HEX = {OK: "#3ddc84", WARN: "#ffcc4d", BAD: "#ff5f56", OFF: "#8a8a94"}


class Status:
    """Уровни подсистем + когда каждую в последний раз поднимали. Пишут треды
    старта, сторожа и опроса; читают окно, трей и лог."""

    KEYS = ("docker", "containers", "server", "bot", "tunnel")

    def __init__(self):
        self._lock = threading.Lock()
        self._s = {k: {"level": OFF, "detail": "", "restart": 0.0} for k in self.KEYS}

    def set(self, key: str, level: str, detail: str = "", restarted: bool = False) -> None:
        with self._lock:
            it = self._s.setdefault(key, {"level": OFF, "detail": "", "restart": 0.0})
            it["level"], it["detail"] = level, detail
            if restarted:      # время ставит только тот, кто РЕАЛЬНО поднимал
                it["restart"] = time.time()

    def snapshot(self) -> dict:
        with self._lock:
            return {k: dict(v) for k, v in self._s.items()}

    def line(self, key: str) -> str:
        v = self.snapshot()[key]
        stamp = _hhmm(v["restart"])
        txt = f"{t('panel.' + key)} {_DOT[v['level']]} {v['detail']}".strip()
        return f"{txt} · {t('panel.restarted', time=stamp)}" if stamp else txt


def _hhmm(ts: float) -> str:
    return time.strftime("%H:%M", time.localtime(ts)) if ts else ""


STATUS = Status()

# ── Компоненты: чекбоксы, запуск/остановка по одному, флаги сторожа ───────────
# ENABLED — что лончер вообще считает «своим»: входит в «Запустить всё»,
# за ним следит сторож. Хранится рядом с геометрией окна (logs/window_state*),
# а не в config.yaml: это настройки именно лончера, а не сервера.
# WANTED — что лончер поднимал (или взял под присмотр) и обязан оживлять;
# ручная остановка снимает WANTED, иначе сторож воскреет то, что гасили руками.
# BUSY — идёт действие («start»/«stop»): сторож в это время не мешает, панель
# рисует «запускается/останавливаем» вместо устаревшего снимка пробников.

COMPONENTS = ("docker", "containers", "server", "bot")
ENABLED = {k: True for k in COMPONENTS}
WANTED: dict = {k: False for k in COMPONENTS}
BUSY: dict = {k: False for k in COMPONENTS}
# Ключ живёт только на время Popen/terminate: сторож обязан спать, пока его
# иначе параллельная остановка и подъём меняют `box[0]` друг у друга.
TAKING: dict = {k: False for k in COMPONENTS}
CTX: dict = {}          # port/box/bot_box/hc/own/iso/win_open/stop/stack_done — из main()
SPLASH = {"active": False, "url": ""}   # окно ещё на «запускаюсь», не на сервере


_BTN = ('border:1px solid rgba(255,255,255,.22);background:#1d1d26;color:#eef0f6;'
        'border-radius:6px;padding:1px 7px;font-size:11px;line-height:1.5;cursor:pointer')


def _off_detail(key: str) -> str:
    """Почему компонент не под присмотром: изолированный прогон, снятый чекбокс
    или ручная остановка кнопкой — три разные причины, и владельцу важно
    различать их в панели."""
    if iso_blocked(key):
        return t("st.not_monitored")
    if not ENABLED.get(key):
        return t("st.off")
    if not WANTED.get(key):
        return t("st.stopped")
    return ""


def _overlay_html(snap: dict) -> str:
    """Панель управления внутри окна приложения: «Запустить всё», по чекбоксу
    и кнопкам ▶/■ на компонент, галочка автозапуска, состояние и время
    последнего перезапуска. Окно — чужая страница, поэтому всё инлайном и без
    классов (не словить конфликт с CSS Ripster). В раскрытом виде панель
    ловит клики (иначе кнопки нерабочи); свернутая в строчку занимает три
    миллиметра и мешать не может.
    Кнопки шлют вызовы в `pywebview.api` (LauncherApi), а ответом идёт не HTML
    кнопки, а новая команда Python-рендерa: состояние живёт только в лончере."""
    collapsed = not UI_PREFS.get("panel_open", True)
    arrow = "▸" if collapsed else "▾"
    head = ('<div style="display:flex;justify-content:space-between;align-items:center;'
            'gap:10px;font-weight:600;opacity:.8;margin-bottom:4px">'
            f'<span>{html.escape(t("panel.title"))}</span>'
            f'<button data-act="panel" style="{_BTN}">{arrow}</button></div>')
    if collapsed:
        return head
    rows = [head,
            f'<button data-act="start_all" style="{_BTN};margin:0 0 6px">'
            f'{html.escape(t("panel.start_all"))}</button>']
    for key in Status.KEYS:
        v = snap.get(key) or {}
        lvl = v.get("level", OFF)
        stamp = _hhmm(v.get("restart") or 0)
        row = ('<div style="display:flex;gap:6px;align-items:center;white-space:nowrap">'
               f'<span style="color:{_HEX.get(lvl, _HEX[OFF])}">&#9679;</span>')
        if key in COMPONENTS:
            row += (f'<input type="checkbox" data-pref="enabled" data-key="{key}"'
                    + (" checked" if ENABLED[key] else "")
                    + ' style="accent-color:#c084a0;margin:0">')
        row += (f'<span style="opacity:.72;min-width:64px">{html.escape(t("panel." + key))}</span>'
                f'<span style="flex:1">{html.escape(v.get("detail") or "")}</span>')
        if stamp:
            row += f'<span style="opacity:.5">{html.escape(stamp)}</span>'
        if key in COMPONENTS:
            busy = "disabled" if BUSY.get(key) else ""
            start_dis = busy if ENABLED[key] else "disabled"
            row += (f'<button data-act="start" data-key="{key}" {start_dis} '
                    f'style="{_BTN}">&#9654;</button>'
                    f'<button data-act="stop" data-key="{key}" {busy} '
                    f'style="{_BTN}">&#9632;</button>')
        rows.append(row + "</div>")
    try:
        auto_on = autostart_enabled()
    except Exception:
        auto_on = False
    rows.append('<div style="display:flex;gap:6px;align-items:center;margin-top:6px;'
                'white-space:nowrap"><input type="checkbox" data-pref="autostart"'
                + (" checked" if auto_on else "")
                + ' style="accent-color:#c084a0;margin:0">'
                + f'<span>{html.escape(t("panel.autostart"))}</span></div>')
    return "".join(rows)


_OVERLAY_JS = """
(function () {
  var ID = 'ripster-launch-status';
  var host = document.getElementById(ID);
  if (!host) {
    host = document.createElement('div');
    host.id = ID;
    host.style.cssText = 'position:fixed;left:14px;bottom:78px;z-index:2147483000;'
      + 'font:11px/1.55 -apple-system,Segoe UI,sans-serif;'
      + 'color:#eef0f6;background:rgba(12,12,16,.82);border:1px solid rgba(255,255,255,.10);'
      + 'border-radius:10px;padding:8px 11px;box-shadow:0 6px 24px rgba(0,0,0,.45)';
    (document.body || document.documentElement).appendChild(host);
  }
  var html = %(payload)s;
  if (html === null) { host.remove(); return; }
  host.innerHTML = html;
  host.style.pointerEvents = 'auto';
  var api = window.pywebview && window.pywebview.api;
  if (!api) { return; }   // браузерный фолбэк: панели рисовать нечем, только статус
  host.querySelectorAll('[data-act]').forEach(function (el) {
    el.addEventListener('click', function () {
      api.action(el.getAttribute('data-act'), el.getAttribute('data-key') || '');
    });
  });
  host.querySelectorAll('input[data-pref]').forEach(function (el) {
    el.addEventListener('change', function () {
      api.pref(el.getAttribute('data-pref'), el.getAttribute('data-key') || '', el.checked);
    });
  });
})();
"""


def push_overlay(window, enabled: bool, snap: dict) -> None:
    if window is None:
        return
    try:
        payload = "null" if not enabled else json.dumps(_overlay_html(snap))
        window.evaluate_js(_OVERLAY_JS % {"payload": payload})
    except Exception as e:
        # Перезагрузка страницы/закрытое окно — штатные причины; следующий тик
        # опроса перерисует панель заново (она пересоздаётся по id).
        _log(f"[launcher] status overlay skipped: {type(e).__name__}: {e}")


def render_panel() -> None:
    """Перерисовать панель немедленно — после клика ждать 12 с до следующего
    тика опроса нельзя, пользователь решит, что кнопка не нажалась."""
    try:
        push_overlay(WINDOW, UI_PREFS["overlay"], STATUS.snapshot())
    except Exception as e:
        _log(f"[launcher] panel render failed: {type(e).__name__}: {e}")


def refresh_status(hc, port: int, stack_done: threading.Event) -> dict:
    """Один снимок всего стека — то же, что видят окно и трей."""
    for key in COMPONENTS:
        if BUSY.get(key):
            STATUS.set(key, WARN, t("st.starting") if BUSY[key] == "start"
                       else t("st.stopping"))

    if hc is None:
        STATUS.set("docker", OFF, t("st.no_hc"))
        STATUS.set("containers", OFF, t("st.no_hc"))
    else:
        engine, probed = False, False
        if not ENABLED["docker"]:
            STATUS.set("docker", OFF, _off_detail("docker"))
        elif not BUSY.get("docker"):
            try:
                engine, probed = hc.docker_engine_up(timeout=6), True
            except Exception as e:
                _log(f"[launcher] docker probe failed: {type(e).__name__}: {e}")
            if engine:
                STATUS.set("docker", OK, t("st.up"))
            elif not stack_done.is_set():
                STATUS.set("docker", WARN, t("st.starting"))
            else:
                STATUS.set("docker", BAD if WANTED["docker"] else OFF,
                           t("st.down") if WANTED["docker"] else _off_detail("docker"))
        if BUSY.get("containers"):
            pass
        elif not ENABLED["containers"]:
            STATUS.set("containers", OFF, _off_detail("containers"))
        else:
            if not probed:   # Docker чекбоксом выключен — но контейнеры живут на
                try:         # движке, и его состояние надо честно прочитать
                    engine = hc.docker_engine_up(timeout=6)
                except Exception:
                    engine = False
            if not engine:
                STATUS.set("containers", OFF, t("st.wait_docker"))
            else:
                try:
                    running = hc.running_container_names()
                except Exception:
                    running = set()
                wanted = list(getattr(hc, "REQUIRED_CONTAINERS", ()))
                missing = [c for c in wanted if c not in running]
                if not wanted:
                    STATUS.set("containers", OFF, t("st.no_hc"))
                elif missing and not stack_done.is_set():
                    STATUS.set("containers", WARN, t("st.starting"))
                elif missing:
                    STATUS.set("containers", BAD if len(missing) == len(wanted) else WARN,
                               t("st.missing", names=", ".join(missing)))
                else:
                    STATUS.set("containers", OK, t("st.all", n=len(wanted)))

    if BUSY.get("server"):
        pass
    elif not ENABLED["server"]:
        STATUS.set("server", OFF, _off_detail("server"))
    else:
        alive = ripster_alive(port, timeout=2.0)
        if alive:
            STATUS.set("server", OK, t("st.port", port=port))
        elif stack_done.is_set() and WANTED["server"]:
            STATUS.set("server", BAD, f"{port} · {t('st.down')}")
        elif not stack_done.is_set():
            STATUS.set("server", WARN, f"{port} · {t('st.starting')}")
        else:
            STATUS.set("server", OFF, _off_detail("server") or t("st.down"))

    if BUSY.get("bot"):
        pass
    elif hc is None:
        STATUS.set("bot", OFF, t("st.no_hc"))
    elif iso_blocked("bot"):
        STATUS.set("bot", OFF, t("st.not_monitored"))
    elif not ENABLED["bot"]:
        STATUS.set("bot", OFF, t("st.off"))
    else:
        try:
            n = hc.bot_process_count()
        except Exception:
            n = 0
        if n:
            STATUS.set("bot", OK, t("st.up"))
        else:
            STATUS.set("bot", BAD if WANTED["bot"] else OFF,
                       t("st.down") if WANTED["bot"] else _off_detail("bot")
                       or t("st.stopped"))

    _tunnel_status(hc)
    return STATUS.snapshot()


_TUNNEL_CACHE = [0.0, OFF, ""]


def _tunnel_status(hc) -> None:
    """Туннель смотрит туда же, куда `check_tunnel`: `remote-enabled` +
    `public-url`. Пробу дорогая (до 12 с), поэтому кэш на 2 цикла опроса."""
    now = time.time()
    if now - _TUNNEL_CACHE[0] < 24:
        STATUS.set("tunnel", _TUNNEL_CACHE[1], _TUNNEL_CACHE[2])
        return
    if hc is None:
        level, detail = OFF, t("st.no_hc")
    else:
        try:
            if hc._cfg_get("remote-enabled").lower() not in ("true", "1"):
                level, detail = OFF, t("st.off")
            else:
                url = hc._cfg_get("public-url")
                if not url:
                    level, detail = WARN, t("st.down")
                else:
                    code = hc._tunnel_probe(url)
                    level = OK if 0 < code < 500 else (WARN if code >= 500 else BAD)
                    detail = f"HTTP {code}" if code else t("st.no_answer")
        except Exception as e:
            level, detail = WARN, f"{type(e).__name__}"
    _TUNNEL_CACHE[0], _TUNNEL_CACHE[1], _TUNNEL_CACHE[2] = now, level, detail
    STATUS.set("tunnel", level, detail)


def _status_loop(hc, port: int, stack_done: threading.Event,
                 win_open: threading.Event,
                 stop: "threading.Event | None" = None,
                 interval: float = 12.0) -> None:
    """Опрос состояния живёт в своём потоке и переживает и перезагрузку
    страницы (панель пересоздаётся по id), и отсутствие окна (браузерный
    фолбэк): тогда просто некому рисовать. Пауза читается из `stop`, а не из
    `win_open`: снятый флаг спящий поток не разбудил бы, и панель доживала бы до
    конца цикла уже после выхода из программы."""
    while win_open.is_set():
        try:
            snap = refresh_status(hc, port, stack_done)
            icon = TRAY_ICON
            if icon is not None:
                try:
                    icon.update_menu()   # тексты пунктов — колбэки, перечитаются
                except Exception:
                    pass
            push_overlay(WINDOW, UI_PREFS["overlay"], snap)
        except Exception as e:
            _log(f"[launcher] status loop: {type(e).__name__}: {e}")
        if stop is not None:
            stop.wait(interval)
        else:
            win_open.wait(interval)
    _log("[launcher] status loop stopped")


# ── Tray icon + minimize-to-tray ───────────────────────────────────────────────
# The window's close (X) and minimize both fold the app into the system tray
# instead of quitting — the server keeps running so downloads continue. The tray
# icon's left-click restores the window; "Выход" really quits. Gated by the
# config key `minimize-to-tray` (default ON). All of this is best-effort: any
# failure degrades to plain close/quit and is logged, never crashes the launcher.

UI_PREFS = {"overlay": True, "panel_open": True}   # панель; переключаются пунктом в трее
WINDOW = None      # pywebview-окно, ставит open_window — в него рисуется панель
TRAY_ICON = None   # pystray Icon — по нему обновляются пункты меню

LOCK_FILE = None  # set in main(); single-instance lock holding our PID
SHOW_FLAG = None  # a second launch drops this so the running instance pops up
WIN_STATE = None  # set in main(); remembers the window's last position + size


def _load_win_state() -> dict:
    """Last-saved window geometry {x,y,width,height}, sanity-checked. Empty dict
    → use defaults (pywebview centers a 1280×860 window)."""
    import json
    try:
        if WIN_STATE and WIN_STATE.exists():
            d = json.loads(WIN_STATE.read_text(encoding="utf-8"))
            w, h = d.get("width"), d.get("height")
            if isinstance(w, int) and isinstance(h, int) and 480 <= w <= 10000 and 360 <= h <= 10000:
                out = {"width": w, "height": h}
                x, y = d.get("x"), d.get("y")
                # Guard against off-screen coords (unplugged monitor etc).
                if isinstance(x, int) and isinstance(y, int) and -200 <= x <= 20000 and -200 <= y <= 20000:
                    out["x"], out["y"] = x, y
                return out
    except Exception as e:
        _log(f"[launcher] window-state load skipped: {type(e).__name__}: {e}")
    return {}


def _save_win_state(geo: dict) -> None:
    try:
        if WIN_STATE:
            cur = {}
            if WIN_STATE.exists():
                try:
                    cur = json.loads(WIN_STATE.read_text(encoding="utf-8")) or {}
                except Exception:
                    cur = {}
            cur.update(geo)   # prefs (overlay) живут в том же файле — не затирать
            WIN_STATE.write_text(json.dumps(cur), encoding="utf-8")
    except Exception:
        pass


def _ui_pref_file() -> "Path | None":
    return WIN_STATE


def load_ui_prefs() -> None:
    try:
        f = _ui_pref_file()
        if f and f.exists():
            d = json.loads(f.read_text(encoding="utf-8"))
            if isinstance(d.get("overlay"), bool):
                UI_PREFS["overlay"] = d["overlay"]
            if isinstance(d.get("panel_open"), bool):
                UI_PREFS["panel_open"] = d["panel_open"]
            comp = d.get("components")
            if isinstance(comp, dict):
                for k in COMPONENTS:
                    if isinstance(comp.get(k), bool):
                        ENABLED[k] = comp[k]
    except Exception:
        pass


def save_ui_prefs() -> None:
    """Пишем рядом с геометрией окна, но только свои ключи: файл общий, и
    _save_win_state перезаписал бы его целиком без панели."""
    try:
        f = _ui_pref_file()
        if not f:
            return
        f.parent.mkdir(parents=True, exist_ok=True)
        d = {}
        if f.exists():
            try:
                d = json.loads(f.read_text(encoding="utf-8")) or {}
            except Exception:
                d = {}
        d["overlay"] = UI_PREFS["overlay"]       # геометрию не затираем — файл общий
        d["panel_open"] = UI_PREFS["panel_open"]
        d["components"] = {k: ENABLED[k] for k in COMPONENTS}
        f.write_text(json.dumps(d), encoding="utf-8")
    except Exception:
        pass


def tray_enabled() -> bool:
    """Whether the tray icon exists at all. When True, closing the window (X)
    folds the app into the tray so downloads keep running; when False, closing
    really quits. config.yaml `minimize-to-tray` (default True). Env override for
    testing. NOTE: this no longer controls what a plain MINIMIZE does — see
    minimize_to_tray() for that (default: stay on the taskbar)."""
    env = os.environ.get("RIPSTER_TRAY")
    if env is not None:
        return env.strip() not in ("0", "false", "False", "")
    try:
        import yaml
        c = yaml.safe_load((BASE / "config.yaml").read_text(encoding="utf-8")) or {}
        return bool(c.get("minimize-to-tray", True))
    except Exception:
        return True


def minimize_to_tray() -> bool:
    """Where a plain MINIMIZE (the _ button) sends the window.

    Default is the taskbar — a normal minimize that stays visible on the
    taskbar, which is what users expect (testers reported the app "disappearing"
    from the taskbar on minimize). Set config.yaml `minimize-to: tray` to fold
    minimizes into the system tray instead. Env override RIPSTER_MINIMIZE_TRAY."""
    env = os.environ.get("RIPSTER_MINIMIZE_TRAY")
    if env is not None:
        return env.strip() not in ("0", "false", "False", "")
    try:
        import yaml
        c = yaml.safe_load((BASE / "config.yaml").read_text(encoding="utf-8")) or {}
        return str(c.get("minimize-to", "taskbar")).strip().lower() == "tray"
    except Exception:
        return False


def apply_webview_acceleration() -> str:
    """Разогнать окно программы средствами самого движка WebView2.

    Окно Ripster — это встроенный Chromium (WebView2), и по умолчанию Windows
    делает с ним две неприятные вещи, которые выглядят как «тормозит наш код»:

    1. **Расчёт перекрытия окна.** Когда сверху лежит другое полноэкранное
       окно (у владельца это игра), Chromium считает наше окно невидимым и
       перестаёт рисовать. Возврат к Ripster показывает застывший кадр, пока
       страница не перерисуется, а анимации в этот момент «прыгают».
    2. **Торможение фоновых таймеров.** Свёрнутому/перекрытому окну Chromium
       режет таймеры до одного тика в секунду — а на них держатся прогресс
       очереди, плеер и опрос WebSocket.

    Плюс включаем растеризацию и композитинг на видеокарте: у сеток обложек
    это самая тяжёлая часть кадра.

    Ничего не зашито намертво. Порядок: env `RIPSTER_WEBVIEW_ARGS` (польностью
    заменяет строку) → config.yaml `webview-browser-args` → набор по умолчанию.
    Отдельный выключатель `webview-hw-accel: false` (env `RIPSTER_HW_ACCEL=0`)
    переводит окно на программную отрисовку — это путь отхода для машин, где
    драйвер видеокарты бракованный и ускорение даёт чёрный экран.

    Возвращает итоговую строку аргументов (для лога)."""
    # Аргументы читает сам WebView2 при создании браузерного процесса, поэтому
    # переменную надо выставить ДО создания окна — позже она уже ни на что не влияет.
    env_args = os.environ.get("RIPSTER_WEBVIEW_ARGS")
    if env_args is not None:
        args = env_args.strip()
        os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = args
        return args

    cfg = {}
    try:
        import yaml
        cfg = yaml.safe_load((BASE / "config.yaml").read_text(encoding="utf-8")) or {}
    except Exception:
        pass

    accel_env = os.environ.get("RIPSTER_HW_ACCEL")
    if accel_env is not None:
        accel = accel_env.strip() not in ("0", "false", "False", "")
    else:
        accel = bool(cfg.get("webview-hw-accel", True))

    override = str(cfg.get("webview-browser-args", "") or "").strip()
    if override:
        args = override
    elif accel:
        args = " ".join([
            # Не переставать рисовать, когда окно перекрыто игрой или другим окном.
            "--disable-features=CalculateNativeWinOcclusion",
            "--disable-backgrounding-occluded-windows",
            # Держать таймеры живыми: на них прогресс очереди, плеер и WebSocket.
            "--disable-background-timer-throttling",
            "--disable-renderer-backgrounding",
            # Растеризация и композитинг силами видеокарты.
            "--enable-gpu-rasterization",
            "--enable-zero-copy",
        ])
    else:
        # Явный отказ от ускорения — окно рисуется процессором.
        args = "--disable-gpu --disable-gpu-compositing"

    os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = args
    return args


def _tray_image():
    """A PIL image for the tray icon — the shipped ripster.ico, or a drawn dot."""
    try:
        from PIL import Image
        ico = BASE / "ripster.ico"
        if ico.exists():
            return Image.open(str(ico))
    except Exception as e:
        _log(f"[launcher] tray icon image fallback: {type(e).__name__}: {e}")
    try:
        from PIL import Image, ImageDraw
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.ellipse((8, 8, 56, 56), fill=(124, 92, 255, 255))
        return img
    except Exception:
        return None


def start_tray(window, do_quit) -> "object | None":
    """Start a detached system-tray icon. Left-click / "Открыть" restores the
    window; "Выход" calls do_quit(). Returns the pystray Icon or None.

    Меню — не украшение, а второй экземпляр панели состояния: тексты пунктов это
    колбэки, поэтому `icon.update_menu()` (его зовёт поток опроса) держит в трее
    те же 🟢/🟡/🔴 и то же время перезапуска, что и полоса в окне. Галочка
    «Запускать при входе» читает состояние задачи Планировщика, а не своё
    представление о нём: задача могла быть удалена или отключена вручную."""
    try:
        import pystray
    except Exception as e:
        _log(f"[launcher] pystray unavailable ({type(e).__name__}: {e}) — no tray")
        return None
    img = _tray_image()
    if img is None:
        _log("[launcher] no tray image — no tray")
        return None

    def _restore(icon=None, item=None):
        try:
            window.show()
            window.restore()
        except Exception as e:
            _log(f"[launcher] tray restore failed: {type(e).__name__}: {e}")

    def _quit(icon=None, item=None):
        try:
            do_quit()
        except Exception as e:
            _log(f"[launcher] tray quit failed: {type(e).__name__}: {e}")

    def _notify(icon, msg):
        try:
            icon.notify(msg, t("panel.title"))
        except Exception:
            pass

    def _toggle_autostart(icon=None, item=None):
        want = not autostart_enabled()
        ok, err = autostart_set_enabled(want)
        _notify(icon, t("tray.autostart_on") if ok and want else
                         (t("tray.autostart_off") if ok else
                          t("tray.autostart_err", err=(err or "PowerShell")))[:250])

    def _toggle_overlay(icon=None, item=None):
        UI_PREFS["overlay"] = not UI_PREFS["overlay"]
        save_ui_prefs()
        push_overlay(window, UI_PREFS["overlay"], STATUS.snapshot())

    try:
        menu = pystray.Menu(
            pystray.MenuItem(t("tray.open"), _restore, default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(lambda i: t("tray.status_title"), None, enabled=False),
            *[pystray.MenuItem(lambda i, k=k: "  " + STATUS.line(k), None, enabled=False)
              for k in Status.KEYS],
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(t("tray.autostart"), _toggle_autostart,
                             checked=lambda i: autostart_enabled()),
            pystray.MenuItem(t("tray.overlay"), _toggle_overlay,
                             checked=lambda i: UI_PREFS["overlay"]),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(t("tray.quit"), _quit),
        )
        icon = pystray.Icon("ripster", img, "Ripster", menu)
        icon.run_detached()   # spins its own thread; returns immediately
        global TRAY_ICON
        TRAY_ICON = icon
        _log("[launcher] tray icon started")
        return icon
    except Exception as e:
        import traceback
        _log(f"[launcher] tray start failed: {type(e).__name__}: {e}")
        _log(traceback.format_exc())
        return None


def _watch_show_flag(window, win_open) -> None:
    """Poll for the SHOW_FLAG a second launch drops, and surface the window."""
    while win_open.is_set():
        try:
            if SHOW_FLAG and SHOW_FLAG.exists():
                SHOW_FLAG.unlink()
                window.show()
                window.restore()
                _log("[launcher] second launch → surfaced window")
        except Exception:
            pass
        time.sleep(0.7)


def _splash_html(msg: str, detail: str) -> str:
    """Static splash shell (pulsing dots + message), no network calls of its
    own. A page loaded via webview's html= gets a null/opaque origin, and a
    fetch() from there to 127.0.0.1 gets blocked outright by Chromium (CORS
    AND no-cors both fail — this isn't a CORS-allowlist problem, something
    more fundamental blocks private-network fetches from an opaque origin).
    So readiness is polled from PYTHON (open_window's background thread,
    plain urlopen — no browser involved) which calls window.load_url()/
    load_html() once it knows the answer, instead of asking the page to
    figure it out itself."""
    return f"""<!doctype html><html><head><meta charset="utf-8">
<style>
  html,body{{height:100%;margin:0;background:#0a0a0c;color:#f0f0f4;
    font-family:-apple-system,Segoe UI,sans-serif;display:flex;
    align-items:center;justify-content:center}}
  .wrap{{text-align:center;max-width:420px;padding:20px}}
  .dot{{width:10px;height:10px;border-radius:50%;background:#c084a0;
    display:inline-block;margin:0 3px;animation:pulse 1.2s ease-in-out infinite}}
  .dot:nth-child(2){{animation-delay:.2s}} .dot:nth-child(3){{animation-delay:.4s}}
  @keyframes pulse{{0%,80%,100%{{opacity:.25;transform:scale(.8)}}40%{{opacity:1;transform:scale(1)}}}}
  h3{{font-weight:600;margin:18px 0 6px}}
  p{{color:#9a9aa4;font-size:12px;line-height:1.6;margin:4px 0}}
  code{{background:#1a1a20;padding:1px 5px;border-radius:4px}}
</style></head>
<body><div class="wrap">
  <div><span class="dot"></span><span class="dot"></span><span class="dot"></span></div>
  <h3>{msg}</h3>
  <p>{detail}</p>
</div></body></html>"""


def _starting_html() -> str:
    return _splash_html("Запускаю Ripster…",
                         "Первый запуск может занять минуту — антивирус проверяет файлы.")


def _still_starting_html() -> str:
    return _splash_html("Запускаю Ripster…",
                         "Всё ещё запускается — это нормально на первом старте.")


def _not_responding_html() -> str:
    return _splash_html(
        "Сервер не отвечает",
        "Проверь <code>logs\\\\launcher.log</code> и <code>logs\\\\console.log</code> в папке установки.<br>"
        "Частая причина — антивирус блокирует <code>python\\\\python.exe</code>.")


# ── WebView2 runtime gate ────────────────────────────────────────────────────
# Окно Ripster рендерит `static/index.html`, а он использует современный JS.
# На доисторическом WebView2 скрипт падает ДО первого рендера → пользователь
# видит пустое белое окно без единого слова (жалоба yueka 02.09.2026: убил два
# часа, нашёл причину — старый WebView2 — сам). Ниже — явная проверка версии
# рантайма ДО создания окна; если она заведомо старая, показываем простую
# HTML-страницу (без модерного JS, отрисуется на любом Chromium) с прямой
# ссылкой на установщик, вместо белого окна.
_WEBVIEW2_MIN_MAJOR = 90  # ~Chromium 90 (апрель 2021); evergreen-рантайм давно выше


def _webview2_runtime_version() -> str:
    """Версия установленного Microsoft Edge WebView2 Runtime (`"122.0.2365.66"`)
    или "" если не найдена. GUID {F3017226-…} — это именно рантайм WebView2."""
    try:
        import winreg
    except Exception:
        return ""
    guid = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
    for root, sub in (
        (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{guid}"),
        (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{guid}"),
        (winreg.HKEY_CURRENT_USER, rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{guid}"),
    ):
        try:
            with winreg.OpenKey(root, sub) as k:
                val, _ = winreg.QueryValueEx(k, "pv")
                if val and val != "0.0.0.0":
                    return str(val)
        except OSError:
            continue
    return ""


def _webview2_ok() -> tuple[bool, str]:
    """(годен ли рантаём, найденная версия). Не смогли прочитать версию —
    считаем годным (не блокируем рабочую установку из-за нестандартного
    размещения ключа)."""
    ver = _webview2_runtime_version()
    if not ver:
        return True, ""
    try:
        major = int(ver.split(".", 1)[0])
    except ValueError:
        return True, ver
    return major >= _WEBVIEW2_MIN_MAJOR, ver


def _webview2_too_old_html(cur: str) -> str:
    dl = "https://developer.microsoft.com/en-us/microsoft-edge/webview2/"
    return f"""<!doctype html><html><head><meta charset="utf-8">
<style>
  html,body{{height:100%;margin:0;background:#0a0a0c;color:#f0f0f4;
    font-family:-apple-system,Segoe UI,Arial,sans-serif;display:flex;
    align-items:center;justify-content:center}}
  .wrap{{max-width:460px;padding:28px;text-align:center;line-height:1.6}}
  h2{{font-weight:600;margin:0 0 10px}}
  p{{color:#b8b8c0;font-size:13px;margin:8px 0}}
  a.btn{{display:inline-block;margin-top:16px;padding:10px 20px;border-radius:8px;
    background:#c084a0;color:#0a0a0c;text-decoration:none;font-weight:600;font-size:13px}}
  code{{background:#1a1a20;padding:1px 5px;border-radius:4px}}
</style></head><body><div class="wrap">
  <h2>Нужно обновить Microsoft Edge WebView2</h2>
  <p>Ripster работает на движке WebView2 версии {_WEBVIEW2_MIN_MAJOR}+, а у вас установлена <code>{cur or "неизвестно / отсутствует"}</code>.
     Без обновления окно останется пустым.</p>
  <p><b>Update Microsoft Edge WebView2</b> — Ripster needs WebView2 runtime {_WEBVIEW2_MIN_MAJOR}+, yours is <code>{cur or "missing"}</code>.</p>
  <a class="btn" href="{dl}">Скачать / Download WebView2</a>
  <p style="margin-top:18px;font-size:11px">Возьмите «Evergreen Standalone Installer», поставьте, перезапустите Ripster.</p>
</div></body></html>"""


def _poll_until_ready(window, url: str, port: int) -> None:
    """Background thread: keep checking readiness with a plain Python HTTP
    call (ripster_alive — no browser, no CORS/origin issues) and drive the
    ALREADY-OPEN window over to the real URL once it answers, updating the
    splash message if it's taking a while. Runs only when open_window()
    couldn't confirm readiness before creating the window."""
    import time as _t
    if not ENABLED.get("server"):
        # Сервер выключен чекбоксом лончера: крутить пробник бессмысленно —
        # страница «Сервер выключен» честная, панель поверх неё остаётся живой.
        try:
            window.load_html(_splash_html(t("splash.server_off_t"),
                                         t("splash.server_off_d")))
        except Exception:
            pass
        return
    start = _t.time()
    told_slow = False
    while _t.time() - start < 180:
        if ripster_alive(port):
            _leave_splash()
            return
        if not told_slow and _t.time() - start > 15:
            told_slow = True
            try:
                window.load_html(_still_starting_html())
            except Exception:
                pass
        _t.sleep(1.0)
    try:
        window.load_html(_not_responding_html())
        SPLASH["active"] = False
    except Exception:
        pass
    _log(f"[launcher] server never answered on 127.0.0.1:{port} within the extended splash window")


# ── Мост панель → лончер (pywebview js_api) ──────────────────────────────────
# DOM-кнопки ничего не решают: они зовут эти методы, а весь смысл — в состоянии
# Python (ENABLED/WANTED/BUSY), которое после каждого вызова перерисовывает
# панель. Долгих вызовов наружу нет: действие уходит в поток и отвечает сразу.


class LauncherApi:
    def action(self, name, key=""):
        name, key = str(name), str(key)
        if not CTX.get("win_open"):
            return "ok"   # окно ещё не открыто — из main() пути нет, но и crash не нужен
        _log(f"[panel] клик: {name} {key}".rstrip())
        if name == "panel":
            UI_PREFS["panel_open"] = not UI_PREFS.get("panel_open", True)
            save_ui_prefs()
            render_panel()
            return "ok"
        if name == "start_all":
            so = CTX.get("stack_done")
            if so is not None and not so.is_set():
                return "ok"          # стартовый подъём ещё идёт — не плодим второй
            _spawn(lambda: bring_up_stack(CTX["win_open"], so))
            return "ok"
        if key in COMPONENTS:
            if not CTX.get("win_open", threading.Event()).is_set():
                return "ok"
            if name == "start" and (not ENABLED[key] or iso_blocked(key)):
                return "ok"
            _spawn(lambda: _component_action(key, name == "start"))
            return "ok"
        return "bad"

    def pref(self, name, key, on):
        name, key = str(name), str(key)
        on = bool(on)
        _log(f"[panel] переключатель: {name} {key} → {on}")
        if name == "enabled" and key in COMPONENTS:
            ENABLED[key] = on
            if not on:
                WANTED[key] = False   # с checkbox-а надзор снимается сразу
            save_ui_prefs()
        elif name == "autostart":
            ok, err = autostart_set_enabled(on)
            if not ok:
                _log(f"[panel] autostart failed: {err}")
        render_panel()
        return "ok"


def open_window(url: str, port: int, win_open, title: str = "Ripster", ready: bool = False):
    """Native pywebview window with tray + minimize-to-tray; browser fallback.
    Returns ('webview', state) | ('browser', None). `state['quit']` is True only
    when the user really chose Выход (so main() tears the server down).

    `ready`: caller already confirmed (via wait_for_ripster, a plain Python
    HTTP check) that the server answers — the common case, since it binds in
    a few seconds. Then we point the window straight at the real URL, same
    as before the splash existed. Only when the server hasn't answered yet
    (a genuine cold start) do we show a static splash and hand off to a
    background Python thread that polls and navigates the window once ready
    — see _poll_until_ready. A client-side fetch() from the splash can't do
    this reliably: it runs from a null/opaque origin and browsers block its
    requests to 127.0.0.1 outright, independent of the server's own CORS
    config (learned the hard way — v3.0.30/31 shipped that broken)."""
    try:
        import threading
        # Строго ДО импорта/старта webview: WebView2 читает аргументы из окружения
        # в момент создания браузерного процесса, позже они уже не действуют.
        try:
            _log(f"[launcher] webview args: {apply_webview_acceleration()}")
        except Exception as _e:
            _log(f"[launcher] webview args skipped: {_e}")
        import webview
        _wv_ok, _wv_ver = _webview2_ok()
        if not _wv_ok:
            _log(f"[launcher] WebView2 runtime too old ({_wv_ver} < {_WEBVIEW2_MIN_MAJOR}) — showing update page instead of blank window")
        _log(f"[launcher] opening webview window -> {url} (ready={ready}, wv2={_wv_ver or '?'})")
        geo = _load_win_state()
        window = webview.create_window(
            title,
            **({"html": _webview2_too_old_html(_wv_ver)} if not _wv_ok
               else {"url": url} if ready else {"html": _starting_html()}),
            js_api=LauncherApi(),   # кнопки/чекбоксы панели приходят в LauncherApi
            width=geo.get("width", 1280), height=geo.get("height", 860),
            x=geo.get("x"), y=geo.get("y"))
        if _wv_ok and not ready:
            SPLASH.update(active=True, url=url)
            threading.Thread(target=_poll_until_ready, args=(window, url, port), daemon=True).start()
        state = {"quit": False, "tray": None, "notified": False}
        use_tray = tray_enabled()
        global WINDOW
        WINDOW = window
        load_ui_prefs()
        _log(f"[launcher] панель: «запустить всё»+'{t('panel.start_all')}', чекбоксы="
             + ",".join(f"{k}:{'вкл' if ENABLED[k] else 'выкл'}" for k in COMPONENTS))
        # страница перезагружается (Ctrl+R, load_url после splash) — панель уезжает
        # вместе с DOM, вешаем её обратно на каждый loaded
        try:
            window.events.loaded += lambda w: push_overlay(
                window, UI_PREFS["overlay"], STATUS.snapshot())
        except Exception as e:
            _log(f"[launcher] overlay reload hook unavailable: {type(e).__name__}: {e}")
        push_overlay(window, UI_PREFS["overlay"], STATUS.snapshot())

        # Remember where/how big the user left the window and restore it next launch.
        _geo = {"width": geo.get("width", 1280), "height": geo.get("height", 860)}
        if "x" in geo: _geo["x"] = geo["x"]
        if "y" in geo: _geo["y"] = geo["y"]
        _geo_timer = [None]
        def _schedule_geo_save():
            try:
                if _geo_timer[0]:
                    _geo_timer[0].cancel()
                tmr = threading.Timer(1.0, lambda: _save_win_state(dict(_geo)))
                tmr.daemon = True
                tmr.start()
                _geo_timer[0] = tmr
            except Exception:
                pass
        def on_resized(w, h):
            try:
                _geo["width"], _geo["height"] = int(w), int(h)
                _schedule_geo_save()
            except Exception:
                pass
        def on_moved(x, y):
            try:
                _geo["x"], _geo["y"] = int(x), int(y)
                _schedule_geo_save()
            except Exception:
                pass

        def do_quit():
            state["quit"] = True
            try:
                _save_win_state(dict(_geo))   # persist final geometry on real quit
            except Exception:
                pass
            try:
                if state["tray"] is not None:
                    state["tray"].stop()
            except Exception:
                pass
            try:
                window.destroy()
            except Exception:
                pass

        def on_closing():
            # Real quit (from tray) → allow the close. Otherwise fold to tray.
            if state["quit"] or not use_tray or state["tray"] is None:
                return True
            try:
                window.hide()
                if not state["notified"]:
                    state["notified"] = True
                    try:
                        state["tray"].notify(
                            "Ripster свёрнут в трей — загрузки продолжаются. "
                            "Клик по значку откроет окно, «Выход» закроет программу.",
                            "Ripster")
                    except Exception:
                        pass
            except Exception as e:
                _log(f"[launcher] hide-to-tray failed: {type(e).__name__}: {e}")
                return True   # if hiding fails, let it close normally
            return False      # cancel the close → app stays alive in tray

        min_to_tray = minimize_to_tray()
        def on_minimized():
            # Default: a plain minimize just goes to the taskbar (do nothing —
            # let the OS minimize normally). Only fold into the tray when the
            # user explicitly chose `minimize-to: tray`.
            if min_to_tray and use_tray and state["tray"] is not None and not state["quit"]:
                try:
                    window.hide()
                except Exception:
                    pass

        window.events.closing += on_closing
        try:
            window.events.minimized += on_minimized
        except Exception:
            pass
        try:
            window.events.resized += on_resized
            window.events.moved   += on_moved
        except Exception:
            pass

        if use_tray:
            state["tray"] = start_tray(window, do_quit)
            threading.Thread(target=_watch_show_flag, args=(window, win_open),
                             daemon=True).start()

        # Тестовый хук для проверок frozen-exe без человека: закрыть окно
        # «самому через N секунд» — штатный do_quit со всего сценария «Выход».
        sc = os.environ.get("RIPSTER_SELFCLOSE")
        if sc and sc.strip().isdigit():
            _log(f"[launcher] RIPSTER_SELFCLOSE={sc}: окно закроется само")
            threading.Timer(int(sc.strip()), do_quit).start()

        # ПОСТОЯННЫЙ ПРОФИЛЬ ОКНА. По умолчанию pywebview стартует в приватном
        # режиме: куки и localStorage живут только до закрытия окна. Из-за этого
        # после КАЖДОГО перезапуска Ripster снова просил пароль, хотя сессия
        # живёт 30 дней и ключ подписи в config.yaml не меняется (владелец,
        # 20.09.2026: «чтобы после рестарта не понадобилось входить»). Заодно
        # сохраняются тема, скин и язык. Папка — рядом с данными пользователя,
        # чтобы переустановка приложения её не стирала.
        _profile = os.path.join(
            os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
            "Ripster", "webview")
        try:
            os.makedirs(_profile, exist_ok=True)
        except Exception as e:                                  # noqa: BLE001
            _log(f"[launcher] профиль окна недоступен ({e!r}) — приватный режим")
            _profile = None
        if _profile:
            _log(f"[launcher] профиль окна: {_profile}")
            webview.start(private_mode=False, storage_path=_profile)
        else:
            webview.start()  # blocks until the window is destroyed (real quit)
        WINDOW = None
        SPLASH["active"] = False
        _log("[launcher] webview window closed")
        return "webview", state
    except Exception as e:
        import traceback
        _log(f"[launcher] webview FAILED ({type(e).__name__}: {e}) -> browser fallback")
        _log(traceback.format_exc())
        webbrowser.open(url)
        return "browser", None


def _sleep_stop(stop: "threading.Event | None", seconds: float) -> None:
    """Спать, но проснуться, если лончер сворачивают."""
    if stop is None:
        time.sleep(seconds)
        return
    stop.wait(seconds)


def _watchdog(name: str, is_alive, restart, win_open: threading.Event,
              stop: "threading.Event | None" = None, max_fails: int = 8,
              pause: float = 0.5, boot: "threading.Event | None" = None,
              gate=None) -> None:
    """Один общий сторож. Сервер и бот перезапускаются одинаково: умер → пауза →
    попытка → счётчик; различаются только `restart`. Счётчик обнуляется первой же
    удачной проверкой «жив», так что лимит — это именно подряд идущие попытки, а
    не всё время жизни процесса.

    `boot` — событие «поток старта дошёл до запуска этого сервиса»: до него
    процесс отсутствует по определению, и поднимать его с двух сторон было бы
    двойным запуском.

    `gate()` — можно ли вообще вмешиваться: снятый чекбокс, ручная остановка
    кнопкой или идущее прямо сейчас действие делают надзор неверным — сторож
    молча спит, пока гейт не откроется.

    `is_alive()` и `restart()` вызываются из этого потока и не должны бросать
    наружу: исключения логируются, цикл продолжается."""
    fails = 0
    while win_open.is_set():
        if boot is not None and not boot.is_set():
            _sleep_stop(stop, pause)
            continue
        if gate is not None and not gate():
            fails = 0
            _sleep_stop(stop, pause)
            continue
        try:
            alive = bool(is_alive())
        except Exception as e:
            alive = True   # сломанный пробник — не повод убивать рабочий сервис
            _log(f"[watchdog:{name}] probe failed: {type(e).__name__}: {e}")
        if alive:
            fails = 0
            _sleep_stop(stop, pause)
            continue
        if not win_open.is_set():
            break
        fails += 1
        if fails > max_fails:
            _log(f"[watchdog:{name}] {name} не поднимается за {max_fails} попыток — "
                 f"прекращаю, нужен ручной разбор (logs/)")
            STATUS.set(name, BAD, t("st.dead"))
            while stop is not None and not stop.is_set():   # ждём выхода, а не крутим пробник
                stop.wait(60)
            break
        STATUS.set(name, WARN, t("st.restarting"))
        _log(f"[watchdog:{name}] {name} exited → restart #{fails} (пауза {pause:g}с)")
        try:
            restart()
        except Exception as e:
            _log(f"[watchdog:{name}] restart failed: {type(e).__name__}: {e}")
        STATUS.set(name, WARN, t("st.restarting"), restarted=True)
        _sleep_stop(stop, pause)
    _log(f"[watchdog:{name}] stopped")


def _gate(key: str) -> bool:
    """Сторож вмешивается, только когда компонент включён чекбоксом, лончер его
    реально поднимал (WANTED) и никто прямо сейчас не жмёт ему кнопки. `TAKING`
    закрывает окно, пока сторож ждёт освобождения порта: без него restart()
    успевал породить сервер параллельно с ручной остановкой, и этот процесс
    доставался лишь быкому `box[0]` — «убитый» сервер продолжал отвечать."""
    return (bool(ENABLED.get(key)) and bool(WANTED.get(key)) and not BUSY.get(key)
            and not TAKING.get(key))


@contextlib.contextmanager
def _taking(key: str):
    """Контекст «я прямо сейчас поднимаю/гашу `key`»: на это время сторож
    отключён, а `box[0]` меняет только тот, кто внутри блока."""
    TAKING[key] = True
    try:
        yield
    finally:
        TAKING[key] = False


def _supervise(box: list, port: int, win_open: threading.Event,
               stop: "threading.Event | None" = None,
               boot: "threading.Event | None" = None) -> None:
    """Сервер: прежнее присматривание (respawn ровно один раз на выход процесса,
    с ожиданием освобождения порта) поверх общего `_watchdog`."""
    def alive() -> bool:
        proc = box[0]
        return proc is not None and proc.poll() is None

    def restart() -> None:
        box[0] = None      # труп больше никем не считается, и terminate его не тронет
        # Let the old process fully release the port before rebinding.
        for _ in range(40):
            if not win_open.is_set() or port_free(port):
                break
            _sleep_stop(stop, 0.25)
        if not win_open.is_set():
            return
        # Внутри TAKING свой же гейт закрыт, поэтому здесь проверяем только
        # признаки «передумали»: ручная остановка (WANTED) или снятый чекбокс.
        with _taking("server"):
            if not WANTED.get("server") or not ENABLED.get("server"):
                return
            box[0] = start_server(port)
            if box[0] is not None:
                WANTED["server"] = True
        # ожидание готовности — вне TAKING: кнопка ■ должна отвечать сразу
        wait_for_ripster(port, timeout=30)

    _watchdog("server", alive, restart, win_open, stop=stop, boot=boot,
              gate=lambda: CTX.get("own") and _gate("server"))


def _supervise_bot(bot_box: list, hc, win_open: threading.Event,
                   stop: "threading.Event | None" = None,
                   boot: "threading.Event | None" = None) -> None:
    """Бот: `hc.bot_process_count()` нужен потому, что бот мог быть поднят не
    нами (сторож healthcheck, руки владельца) — тогда handle'а нет, и единственный
    способ узнать, жив ли он, это командная строка процесса. Интерпретатор берётся
    оттуда же, откуда его запускает сторож, иначе «живой бот» и «наш бот» — разные
    процессы."""
    def alive() -> bool:
        proc = bot_box[0]
        if proc is not None and proc.poll() is None:
            return True
        try:
            return hc.bot_process_count() > 0
        except Exception:
            return True

    def restart() -> None:
        bot_box[0] = None
        with _taking("bot"):
            if not WANTED.get("bot") or not ENABLED.get("bot"):
                return
            bot_box[0] = hc.start_bot_process(notify=_hc_log)
            if bot_box[0] is None:
                WANTED["bot"] = False   # поднять нечем — не долбить лимит впустую

    # 8с между попытками: бот, падающий на плохом токене, не должен выжигать CPU
    # и плодить строки в logs/bot.log быстрее, чем человек успевает прочитать.
    _watchdog("bot", alive, restart, win_open, stop=stop, boot=boot,
              max_fails=5, pause=8.0,
              gate=lambda: CTX.get("own") and not iso_blocked("bot") and _gate("bot"))


def _supervise_docker(hc, win_open: threading.Event,
                      stop: "threading.Event | None" = None) -> None:
    """Docker Desktop после ребута сам не поднимается, а на нём весь стек.
    Пауза 20 с и три попытки: медленный старт движка — норма, а не повод
    крутить `docker ps` вхолостую сотни раз."""
    def alive() -> bool:
        return bool(hc.docker_engine_up(timeout=8))

    def restart() -> None:
        if not hc.ensure_docker_engine(notify=_hc_log):
            WANTED["docker"] = False   # движок не ожил за ~2 мин — хватит долбить

    _watchdog("docker", alive, restart, win_open, stop=stop,
              max_fails=3, pause=20.0,
              gate=lambda: _gate("docker") and not iso_blocked("docker"))


def _supervise_containers(hc, win_open: threading.Event,
                          stop: "threading.Event | None" = None) -> None:
    """Контейнеры: упал wrapper — поднят заново; лежит движок — очередь у
    сторожа Docker, здесь сторож не мешает."""
    def missing():
        wanted = list(getattr(hc, "REQUIRED_CONTAINERS", ()))
        running = hc.running_container_names()
        return [c for c in wanted if c not in running]

    def alive() -> bool:
        if not hc.docker_engine_up(timeout=6):
            return True
        return not missing()

    def restart() -> None:
        _raise_containers(hc, missing())

    _watchdog("containers", alive, restart, win_open, stop=stop,
              max_fails=3, pause=45.0,
              gate=lambda: _gate("containers") and not iso_blocked("containers"))


# ── Шаги подъёма/остановки: их же зовёт кнопка «Запустить всё», чекбоксы и
#    одиночные ▶/■. Порядок docker → контейнеры → сервер → бот выдержан в
#    bring_up_stack; каждый шаг сам ждёт готовности и сам рисует свой статус.


def _raise_containers(hc, missing: list) -> list:
    """Поднять список контейнеров; вернуть те, что так и не поднялись.
    amd-wrapper мог быть не создан вообще (не «остановлен») — для него
    единственный проверенный путь: пересоздание с логином (`_heal_wrapper`)."""
    for c in missing:
        hc.ensure_container(c, notify=_hc_log)
    wanted = list(getattr(hc, "REQUIRED_CONTAINERS", ()))
    running = hc.running_container_names()
    still = [c for c in wanted if c not in running]
    if "amd-wrapper" in missing and "amd-wrapper" in still:
        hc._heal_wrapper()
        still = [c for c in wanted if c not in hc.running_container_names()]
    return still


def _up_docker(hc) -> bool:
    if hc is None:
        STATUS.set("docker", OFF, t("st.no_hc"))
        return False
    if hc.docker_engine_up(timeout=8):
        WANTED["docker"] = True
        STATUS.set("docker", OK, t("st.up"))
        return True
    _log("[stack] Docker-движок лежит → поднимаю Docker Desktop")
    if hc.ensure_docker_engine(notify=_hc_log):
        WANTED["docker"] = True
        STATUS.set("docker", OK, t("st.up"), restarted=True)
        return True
    STATUS.set("docker", BAD, t("st.down"))
    return False


def _up_containers(hc) -> bool:
    if hc is None:
        STATUS.set("containers", OFF, t("st.no_hc"))
        return False
    if not hc.docker_engine_up(timeout=6):
        STATUS.set("containers", OFF, t("st.wait_docker"))
        return False
    wanted = list(getattr(hc, "REQUIRED_CONTAINERS", ()))
    running = hc.running_container_names()
    missing = [c for c in wanted if c not in running]
    raised = False
    if missing:
        _log(f"[stack] контейнеров нет: {', '.join(missing)} → поднимаю")
        still = _raise_containers(hc, missing)
        raised = len(still) < len(missing)
    else:
        still = []
    WANTED["containers"] = True
    STATUS.set("containers",
               OK if not still else (BAD if len(still) == len(wanted) else WARN),
               t("st.all", n=len(wanted)) if not still
               else t("st.missing", names=", ".join(still)),
               restarted=raised)
    return not still


def _leave_splash() -> None:
    """Окно ещё показывает «Запускаю Ripster…» — перевести его на живой сервер.
    Поток `_poll_until_ready` мог уже отработать свой таймаут, поэтому старт
    сервера из панели не может на него полагаться."""
    if SPLASH["active"] and WINDOW is not None:
        try:
            WINDOW.load_url(SPLASH["url"])
            SPLASH["active"] = False
            _log("[launcher] окно переведено на сервер из панели")
        except Exception as e:
            _log(f"[launcher] load_url from panel failed: {type(e).__name__}: {e}")


def _up_server(port: int, box: list) -> bool:
    if ripster_alive(port):
        # На порту уже кто-то отвечает (attach-режим, чужой сервер): показываем
        # «работает», но под надзор берём только свой процесс — «к чужому не лезем».
        proc = box[0]
        WANTED["server"] = proc is not None and proc.poll() is None
        STATUS.set("server", OK, t("st.port", port=port))
        _leave_splash()
        return True
    _log("[stack] стартую сервер")
    with _taking("server"):
        old = box[0]
        if old is not None and old.poll() is None:
            # наш старый процесс ещё жив: без terminate он пережил бы «остановку»
            # и ответил на порту, а новым handle'ом box[0] бы не стал.
            try:
                old.terminate()
                old.wait(15)
            except Exception as e:
                _log(f"[stack] old server terminate failed: {type(e).__name__}: {e}")
        proc = start_server(port)
        box[0] = proc
        if proc is None:
            # start_server уже объяснил причину (нет bundled python) и повторял её
            # восемь раз не должен сторож: он поднимает только тот процесс,
            # который хотя бы раз удалось запустить.
            WANTED["server"] = False
            STATUS.set("server", BAD, t("st.down"))
            return False
        CTX["own"] = True   # подняли сами — теперь наш: и сторожим, и останавливаем
        # Надзор с первого Popen: если сервер сдохнет, пока мы ждём /api/ping,
        # сторож увидит это сразу, как только мы снимем TAKING.
        WANTED["server"] = True
        STATUS.set("server", WARN, t("st.starting"))
        ready = wait_for_ripster(port, timeout=90)
    if not ready:
        WANTED["server"] = box[0] is not None and box[0].poll() is None
    STATUS.set("server", OK if ready else BAD,
               t("st.port", port=port) if ready else f"{port} · {t('st.down')}")
    if ready:
        _leave_splash()
    return ready


def _up_bot(hc, bot_box: list) -> bool:
    if hc is None:
        STATUS.set("bot", OFF, t("st.no_hc"))
        return False
    if hc.bot_process_count() > 0:
        WANTED["bot"] = True
        STATUS.set("bot", OK, t("st.up"))
        return True
    _log("[stack] бот не запущен → поднимаю bot.py")
    with _taking("bot"):
        old = bot_box[0]
        if old is not None and old.poll() is None:
            try:
                old.terminate()
                old.wait(15)
            except Exception as e:
                _log(f"[stack] old bot terminate failed: {type(e).__name__}: {e}")
        bot_box[0] = hc.start_bot_process(notify=_hc_log)
        up = bot_box[0] is not None
        WANTED["bot"] = up
    STATUS.set("bot", OK if up else BAD, t("st.up") if up else t("st.down"),
               restarted=up)
    return up


def _down_docker(hc) -> None:
    """Стоп движка через сам Docker Desktop (`docker desktop stop`) — он гасит и
    контейнеры, поэтому WANTED снимается у обоих. Насильно убивать процесс
    «Docker Desktop.exe» не будем: это чужие контейнеры тоже."""
    WANTED["docker"] = WANTED["containers"] = False
    if hc is None:
        return
    rc, out = hc._docker("desktop", "stop")
    if rc == 0 and not hc.docker_engine_up(timeout=6):
        STATUS.set("docker", OFF, t("st.stopped"))
        STATUS.set("containers", OFF, t("st.wait_docker"))
    else:
        STATUS.set("docker", BAD, t("st.fail", err=(out or "docker desktop stop")[:120]))


def _down_containers(hc) -> None:
    WANTED["containers"] = False
    if hc is None:
        return
    wanted = list(getattr(hc, "REQUIRED_CONTAINERS", ()))
    for c in wanted:
        try:
            if c in hc.running_container_names():
                hc._docker("stop", c)
                _log(f"[stack] контейнер {c} остановлен кнопкой")
        except Exception as e:
            _log(f"[stack] stop {c} failed: {type(e).__name__}: {e}")
    STATUS.set("containers", OFF, t("st.stopped"))


def _down_server(box: list, own: bool) -> None:
    if not own:
        STATUS.set("server", WARN, t("st.not_ours"))
        return
    WANTED["server"] = False   # раньше terminate: иначе сторож воскреет труп
    with _taking("server"):
        proc = box[0]
        box[0] = None
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(15)
        except Exception as e:
            _log(f"[stack] server terminate failed: {type(e).__name__}: {e}")
    STATUS.set("server", OFF, t("st.stopped"))
    _log("[stack] сервер остановлен кнопкой")


def _down_bot(bot_box: list) -> None:
    WANTED["bot"] = False
    with _taking("bot"):
        proc = bot_box[0]
        bot_box[0] = None
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
            STATUS.set("bot", OFF, t("st.stopped"))
            _log("[stack] бот остановлен кнопкой")
        except Exception as e:
            STATUS.set("bot", BAD, t("st.fail", err=str(e)[:120]))
        return
    hc = CTX.get("hc")
    try:
        external = hc is not None and hc.bot_process_count() > 0
    except Exception:
        external = False
    if external:
        # «К чужому не лезем» из _teardown: процесс поднимал не лончер —
        # лончер его и не убивает, только снимает с надзора.
        STATUS.set("bot", WARN, t("st.not_ours"))
    else:
        STATUS.set("bot", OFF, t("st.stopped"))


def _component_action(key: str, start: bool) -> None:
    """Одна кнопка панели. BUSY на время действия: сторож не подсуетится
    параллельно, панель честно рисует «запускается/останавливаем»."""
    if BUSY.get(key):
        return
    BUSY[key] = "start" if start else "stop"
    render_panel()
    hc, box, bot_box = CTX.get("hc"), CTX.get("box"), CTX.get("bot_box")
    try:
        if start:
            {"docker": lambda: _up_docker(hc),
             "containers": lambda: _up_containers(hc),
             "server": lambda: _up_server(CTX.get("port"), box),
             "bot": lambda: _up_bot(hc, bot_box)}[key]()
        else:
            {"docker": lambda: _down_docker(hc),
             "containers": lambda: _down_containers(hc),
             "server": lambda: _down_server(box, CTX.get("own")),
             "bot": lambda: _down_bot(bot_box)}[key]()
    except Exception as e:
        import traceback
        _log(f"[panel] {key} {'start' if start else 'stop'} failed: {type(e).__name__}: {e}")
        _log(traceback.format_exc())
        STATUS.set(key, BAD, t("st.fail", err=f"{type(e).__name__}"))
    finally:
        BUSY[key] = False
        render_panel()


def _spawn(fn) -> None:
    threading.Thread(target=fn, daemon=True).start()


def bring_up_stack(win_open: threading.Event,
                   stack_done: threading.Event) -> None:
    """«Запустить всё»: Docker → контейнеры → сервер → бот, по порядку, с
    ожиданием готовности каждого; идут только включённые чекбоксы. Один и тот же
    вызов звучит при старте Windows (фон за splash-окном) и по кнопке панели.
    Снятые шаги `refresh_status` рисует честно (выключен/остановлен), а не
    притворяется зеленым."""
    try:
        for key in ("docker", "containers", "server", "bot"):
            if not win_open.is_set():
                return
            if not ENABLED.get(key) or iso_blocked(key):
                continue
            if key != "server" and CTX.get("hc") is None:
                continue
            _component_action(key, True)
    finally:
        # Сторожу бота можно начинать только после нас: иначе он поднял бы бота
        # параллельно с нашим же запуском.
        stack_done.set()




def _pid_alive(pid: "int | None") -> bool:
    if not pid:
        return False
    try:
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not h:
            return False
        ctypes.windll.kernel32.CloseHandle(h)
        return True
    except Exception:
        return False


def _single_instance_guard() -> bool:
    """True = we are the sole instance and may proceed. False = another launcher
    already owns the window; we signalled it to surface and should exit."""
    global LOCK_FILE, SHOW_FLAG, WIN_STATE
    logs = BASE / "logs"
    # Изолированный прогон нового exe не должен отбирать lock у живого лончера
    # (иначе «уже запущено → показать окно и выйти» убило бы тест до старта).
    suffix = "_isolated" if isolated() else ""
    LOCK_FILE = logs / f"launcher{suffix}.lock"
    SHOW_FLAG = logs / f"launcher{suffix}.show"
    WIN_STATE = logs / f"window_state{suffix}.json"
    try:
        logs.mkdir(parents=True, exist_ok=True)
        existing = None
        if LOCK_FILE.exists():
            try:
                existing = int(LOCK_FILE.read_text(encoding="utf-8").strip())
            except Exception:
                existing = None
        if existing and existing != os.getpid() and _pid_alive(existing):
            _log(f"[launcher] already running (pid {existing}) → surfacing it, exiting")
            try:
                SHOW_FLAG.write_text("1", encoding="utf-8")
            except Exception:
                pass
            return False
        LOCK_FILE.write_text(str(os.getpid()), encoding="utf-8")
    except Exception as e:
        _log(f"[launcher] single-instance guard skipped: {type(e).__name__}: {e}")
    return True


def _claim_install_mutex() -> None:
    """Hold a named mutex the INSTALLER checks (AppMutex=Global\\RipsterRunning).

    Without it an install over a running Ripster silently failed: Ripster.exe and
    python\\python.exe are locked, the files are not replaced, and the user ends up
    still on the old build convinced they upgraded. Now Inno Setup detects the
    running app up front and asks to close it. Purely advisory — any failure here
    must never stop the launcher."""
    if os.name != "nt":
        return
    try:
        import ctypes
        ctypes.windll.kernel32.CreateMutexW(None, False, "Global\\RipsterRunning")
    except Exception as e:
        _log(f"[launcher] install-mutex skipped: {type(e).__name__}: {e}")


# ── Автозапуск: задача Планировщика Windows ───────────────────────────────────
# `uac_admin=True` в spec'e означает, что лончеру и так нужен административный
# токен, поэтому обычный ярлык в «Автозагрузка» показывал бы запрос UAC при каждом
# входе. Задача с RunLevel Highest и LogonType Interactive возвышает сама
# Планировщик — окно появляется без единого диалога.
# `schtasks /Query` на русской Windows печатает локализованные имена полей, так что
# состояние читаем PowerShell-cmdlet'ами: `.State` — это enum, он не переводится.

_AUTOSTART_CACHE = [0.0, "unknown"]     # [когда обновляли, состояние]


def autostart_task_name() -> str:
    """Имя задачи зависит от того, КАКОЙ exe её ставит: рабочий лончер
    (Ripster.exe / RipsterLauncher.exe) → «Ripster Autostart», всё остальное
    (Ripster_new.exe, прогон из исходников) → тестовая. Иначе проверка нового exe
    переписала бы задачу живого автозапуска."""
    exe = Path(sys.executable).name if getattr(sys, "frozen", False) else Path(__file__).name
    stem = exe.rsplit(".", 1)[0].lower()
    if stem.startswith("ripster") and not (stem.endswith("_new") or stem.endswith("_test")):
        return "Ripster Autostart"
    return "Ripster Autostart (test)"


def autostart_target() -> tuple[str, str, str]:
    """(что запускать, аргументы, рабочая папка). В исходниках задача указывает
    на pythonw — с python.exe при входе в систему моргнула бы консоль."""
    if getattr(sys, "frozen", False):
        return sys.executable, "", str(BASE)
    w = Path(sys.executable).with_name("pythonw.exe")
    py = str(w) if w.exists() else sys.executable
    return py, str(Path(__file__).resolve()), str(BASE)


def _ps(script: str, timeout: float = 60) -> tuple[int, str]:
    """PowerShell без окна консоли и с UTF-8 в выводе: иначе текст ошибки на
    русской Windows (cp866) приезжает кашей и в логе его не прочитать."""
    prelude = "[Console]::OutputEncoding=[Text.Encoding]::UTF8; $OutputEncoding=[Text.Encoding]::UTF8\n"
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-Command", prelude + script],
            capture_output=True, timeout=timeout, creationflags=CREATE_NO_WINDOW)
        out = (r.stdout or b"").decode("utf-8", "replace").strip()
        err = (r.stderr or b"").decode("utf-8", "replace").strip()
        return r.returncode, "\n".join(x for x in (out, err) if x)
    except Exception as e:
        return -1, f"{type(e).__name__}: {e}"


def _q(s: str) -> str:
    return str(s).replace("'", "''")   # одинарные кавычки внутри PS-'...'


# Кто именно «я» для Планировщика: `$env:USERDOMAIN + '\' + $env:USERNAME` в
# повышенном процессе иногда даёт «имя пользователя или группа не найдены»
# (14,8):UserId — задача тогда не создаётся вовсе. SID из токена не требует
# поиска по имени и принимается `-UserId` напрямую.
def current_user_sid() -> str:
    try:
        import ctypes
        import ctypes.wintypes as wt
        tok = wt.HANDLE()
        if not ctypes.windll.advapi32.OpenProcessToken(
                wt.HANDLE(-1), 0x0008, ctypes.byref(tok)):   # TOKEN_QUERY
            return ""
        size = wt.DWORD(0)
        ctypes.windll.advapi32.GetTokenInformation(tok, 1, None, 0, ctypes.byref(size))
        buf = (ctypes.c_char * size.value)()
        if not ctypes.windll.advapi32.GetTokenInformation(
                tok, 1, buf, size, ctypes.byref(size)):
            return ""
        sid = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p)).contents.value
        strp = wt.LPWSTR()
        if not ctypes.windll.advapi32.ConvertSidToStringSidW(ctypes.c_void_p(sid),
                                                             ctypes.byref(strp)):
            return ""
        return strp.value or ""     # LPWSTR сам разыменовывается в str
    except Exception as e:
        _log(f"[autostart] sid lookup failed: {type(e).__name__}: {e}")
        return ""


def autostart_state(force: bool = False) -> str:
    """missing | disabled | ready | running | queued | unknown (кэш 20 с — пункт
    меню перечитывается на каждом обновлении трея, а PowerShell стоит секунду)."""
    now = time.time()
    if not force and now - _AUTOSTART_CACHE[0] < 20:
        return _AUTOSTART_CACHE[1]
    rc, out = _ps("$t = Get-ScheduledTask -TaskName '{n}' -ErrorAction SilentlyContinue; "
                  "if ($t) {{ $t.State }} else {{ 'Missing' }}".format(n=_q(autostart_task_name())))
    state = out.strip().lower() if rc == 0 else "unknown"
    if rc != 0 or state not in ("missing", "disabled", "ready", "running", "queued"):
        state = state or "unknown"
    _AUTOSTART_CACHE[0] = now
    _AUTOSTART_CACHE[1] = state
    return state


def autostart_installed() -> bool:
    return autostart_state() != "missing"


def autostart_enabled() -> bool:
    return autostart_state() in ("ready", "running", "queued")


def autostart_install(enable: bool = True) -> tuple[bool, str]:
    """Зарегистрировать (или переписать) задачу «вход в систему → лончер».
    Идмпотентно: -Force перезаписывает существующую, повторный клик ничего не ломает.
    -ExecutionTimeLimit: по умолчанию у задачи лимит 72 часа, и Планировщик убил бы
    лончер на третьи сутки работы — отсюда практически бесконечный порог."""
    exe, arg, wd = autostart_target()
    name = autostart_task_name()
    who = current_user_sid() or (os.environ.get("USERDOMAIN", "") + "\\"
                                 + os.environ.get("USERNAME", ""))
    argument = f" -Argument '{_q(arg)}'" if arg else ""
    disable = "" if enable else "\nDisable-ScheduledTask -TaskName $name | Out-Null"
    script = f"""
$ErrorActionPreference = 'Stop'
$name = '{_q(name)}'
$action = New-ScheduledTaskAction -Execute '{_q(exe)}'{argument} -WorkingDirectory '{_q(wd)}'
$trigger = New-ScheduledTaskTrigger -AtLogOn
$principal = New-ScheduledTaskPrincipal -UserId '{_q(who)}' -LogonType Interactive -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit (New-TimeSpan -Days 3650)
Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null{disable}
'ok'
"""
    rc, out = _ps(script)
    ok = rc == 0 and "ok" in out.lower()
    _AUTOSTART_CACHE[0] = 0.0
    _log(f"[autostart] install {name!r} as {who} → {'ok' if ok else 'FAIL'}: {out[:600]}")
    return ok, out[:600]


def autostart_set_enabled(on: bool) -> tuple[bool, str]:
    """Переключатель из меню трея: выключил — задача остаётся зарегистрированной
    и отключена (включить можно обратно без пересоздания)."""
    if autostart_state(force=True) == "missing":
        if not on:
            return True, ""
        return autostart_install(enable=True)
    rc, out = _ps(
        ("Enable" if on else "Disable") + f"-ScheduledTask -TaskName '{_q(autostart_task_name())}' "
        "-ErrorAction Stop | Out-Null; 'ok'")
    ok = rc == 0 and "ok" in out.lower()
    _AUTOSTART_CACHE[0] = 0.0
    _log(f"[autostart] {'enable' if on else 'disable'} → {'ok' if ok else 'FAIL'}: {out[:200]}")
    return ok, out[:200]


def autostart_remove() -> tuple[bool, str]:
    rc, out = _ps(f"Unregister-ScheduledTask -TaskName '{_q(autostart_task_name())}' "
                  "-Confirm:$false -ErrorAction Stop; 'ok'")
    ok = rc == 0 and "ok" in out.lower()
    _AUTOSTART_CACHE[0] = 0.0
    _log(f"[autostart] remove → {'ok' if ok else 'FAIL'}: {out[:200]}")
    return ok, out[:200]


def autostart_report(action: str, ok: bool, detail: str = "") -> None:
    """console=False — stdout у frozen-exe нет, поэтому результат CLI-режима ещё
    и в файл: так проверку «задача поставлена/отключена» видно без человека."""
    payload = {"action": action, "ok": ok, "task": autostart_task_name(),
               "state": autostart_state(force=True), "target": autostart_target()[0],
               "detail": detail[:400], "when": time.strftime("%Y-%m-%d %H:%M:%S")}
    try:
        (BASE / "logs").mkdir(parents=True, exist_ok=True)
        (BASE / "logs" / "autostart_last.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as e:
        _log(f"[autostart] report write failed: {e}")
    _echo(json.dumps(payload, ensure_ascii=False))


def handle_autostart_cli() -> bool:
    """Обслужить автозапусчные флаги. True = работать дальше нельзя, выходить."""
    args = set(sys.argv[1:])
    if "--autostart-status" in args:
        autostart_report("status", True)
        return True
    for flag, action in (("--install-autostart", "install"),
                         ("--disable-autostart", "disable"),
                         ("--enable-autostart", "enable"),
                         ("--remove-autostart", "remove")):
        if flag not in args:
            continue
        if action == "install":
            ok, detail = autostart_install(enable=False)   # тестовую задачу не включаем
        elif action == "remove":
            ok, detail = autostart_remove()
        else:
            ok, detail = autostart_set_enabled(action == "enable")
        autostart_report(action, ok, detail)
        return True
    return False


def _teardown(box: list, bot_box: list, mode: str) -> None:
    """При выходе из лончера останавливаем ТО, что пустили сами: сервер и бота.
    К чужому не лезем — подключились к живому 7799 или бота поднял сторож, значит
    его и не нам гасить; закрытая вкладка браузера (mode="browser") тоже не повод
    ронять сервис."""
    if mode != "webview":
        return
    # Снимаем надзор до terminate: иначе сторож увидит «упал» и поднимет процесс
    # уже после того, как лончер вышел.
    for key in ("server", "bot"):
        WANTED[key] = False
        TAKING[key] = True
    try:
        for name, holder in (("server", box), ("bot", bot_box)):
            proc = holder[0]
            holder[0] = None
            if proc is None:
                continue
            try:
                proc.terminate()
                _log(f"[launcher] {name} остановлен (terminate)")
            except Exception as e:
                _log(f"[launcher] {name} terminate failed: {type(e).__name__}: {e}")
    finally:
        for key in ("server", "bot"):
            TAKING[key] = False


def main() -> None:
    if handle_autostart_cli():
        return
    _claim_install_mutex()
    if not _single_instance_guard():
        return
    load_ui_prefs()          # чекбоксы компонентов живут рядом с геометрией окна
    desired = config_port()
    hc = healthcheck_module()
    iso = isolated()
    box = [None]       # box[0] = the server process WE own (None when attaching to an existing one)
    bot_box = [None]   # то же про bot.py: None, если его поднял не лончер
    win_open = threading.Event()
    win_open.set()
    stop = threading.Event()
    stack_done = threading.Event()

    if ripster_alive(desired):
        # OUR Ripster is already running here (e.g. user relaunched) → just attach.
        # Ничего не перезапускаем и не останавливаем за собой: владелец — тот,
        # кто сервер поднял.
        port = desired
        own = False
        ready = True
        stack_done.set()
        _log(f"[launcher] Ripster already live on {port} — attaching")
    else:
        # Free port at/after the desired one — auto-skips a busy 7799 so a clean
        # install never collides with another app (or another project's server).
        port = find_free_port(desired)
        if port != desired:
            _log(f"[launcher] port {desired} busy → using {port}")
        own = True
        ready = False
        # CTX ДО.spawn: поток «запустить всё» читает box/port/hc через
        # _component_action → CTX.get(...), и если он успевает до update(),
        # _up_server получает box=None и падает на box[0] = ...
        CTX.update(hc=hc, port=port, box=box, bot_box=bot_box, own=own, iso=iso,
                   win_open=win_open, stop=stop, stack_done=stack_done)
        # Старт после входа в Windows = тот же «Запустить всё», что и кнопка:
        # тот же порядок, та же готовность каждого шага, те же чекбоксы.
        threading.Thread(target=bring_up_stack, args=(win_open, stack_done),
                         daemon=True).start()
    CTX.update(hc=hc, port=port, box=box, bot_box=bot_box, own=own, iso=iso,
               win_open=win_open, stop=stop, stack_done=stack_done)

    if own:
        threading.Thread(target=_supervise,
                         args=(box, port, win_open, stop),
                         kwargs={"boot": stack_done}, daemon=True).start()
        if not iso:
            threading.Thread(target=_supervise_bot,
                             args=(bot_box, hc, win_open, stop),
                             kwargs={"boot": stack_done}, daemon=True).start()
        else:
            _log("[launcher] изолированный режим: бота не запускаю и за ним не слежу")
    if hc is not None and not iso:
        threading.Thread(target=_supervise_docker, args=(hc, win_open, stop),
                         daemon=True).start()
        threading.Thread(target=_supervise_containers, args=(hc, win_open, stop),
                         daemon=True).start()
    threading.Thread(target=_status_loop,
                     args=(hc, port, stack_done, win_open, stop),
                     daemon=True).start()

    url = f"http://127.0.0.1:{port}"
    mode, _state = open_window(url, port, win_open, ready=ready)  # blocks until real quit

    # Window destroyed (user chose Выход) → stop supervising FIRST (so it doesn't
    # respawn), then stop the children we own.
    win_open.clear()
    stop.set()
    for key in COMPONENTS:      # после «Выход» никто никого не оживляет
        WANTED[key] = False
    _teardown(box, bot_box, mode)
    try:
        if LOCK_FILE is not None and LOCK_FILE.exists():
            LOCK_FILE.unlink()
    except Exception:
        pass


if __name__ == "__main__":
    main()
