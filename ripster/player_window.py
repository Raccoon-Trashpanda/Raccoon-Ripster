"""Внешнее окно плеера: standalone-процесс + чистая логика для
POST /api/player-window/open (трекер #37).

Зачем. Кнопка «Открыть внешний плеер» обязана работать из ЛЮБОЙ вкладки
браузера, а не только из окна лаунчера. OS-окно с панелью создаёт Python —
браузеру это запрещено политикой. Порядок такой:

1. Окно уже живёт (наш lock PID жив) → поднять/сфокусировать его (show-флаг
   читает сторож внутри окна).
2. Жив лаунчер и понимает протокол launcher.rpwin → попросить его (у окна
   лаунчера есть pywebview-мост «oswin» — самый полный путь).
3. Иначе — поднять `python -m ripster.player_window`: крошечный процесс с
   одним pywebview-окном, которое грузит /static/panel/index.html
   ?transport=relay. Обмен с главной страницей идёт через сервер (/ws),
   поэтому плеер по-прежнему ОДИН — в табе, а не в окне.

Всё, что можно проверить без GUI, — здесь и покрыто тестами: пути, PID,
память геометрии, «single instance», выбор interpreters, таймауты. Сам
pywebview трогается только в main().
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

PANEL_QUERY = "/static/panel/index.html?transport=relay"

# Форму токена задаёт ripster.launch_token (secrets.token_hex(32)). Проверять
# её обязательно: файл-флаг лежит в общем каталоге и теоретически может быть
# подменён локальным процессом, а содержимое мы подставляем в JS окна.
_TOKEN_RE = re.compile(r"[0-9a-f]{64}")
_LAUNCH_GRACE_S = 90         # флаг живёт дольше серверного TTL — окно не гоно

# Токен запуска едем в ФРАГМЕНТЕ (#launch=…), а не в query: фрагмент не
# уходит на сервер ни в каком запросе, поэтому одноразовый пропуск не
# пропечатается в access-лог uvicorn и не останется в истории WebView2 как
# часть адреса. Панель забирает его, вызывает /api/player-window/claim и
# стирает из адреса history.replaceState.
LAUNCH_PREFIX = "#launch="

# Окно обязано влезть в любой разумный экран и не превратиться в полосу.
_GEO_BOUNDS = {"width": (280, 4000), "height": (400, 4000),
               "x": (-200, 20000), "y": (-200, 20000)}
_DEFAULT_GEO = {"width": 420, "height": 800}


def win_paths(base_dir: Path | str) -> dict:
    base = Path(base_dir)
    logs = base / "logs"
    return {
        "lock":          logs / "player_window.lock",
        "show":          logs / "player_window.show",
        "close":         logs / "player_window.close",
        "launch":        logs / "player_window.launch",
        "reauth":        logs / "player_window.reauth",
        "state":         logs / "player_window.json",
        "log":           logs / "player_window.log",
        "profile":       base / "player_window_profile",
        "launcher_lock": logs / "launcher.lock",
        "launcher_req":  logs / "launcher.rpwin",
        "launcher_ack":  logs / "launcher.rpwin.ok",
    }


def panel_url(http_url: str, token: str = "") -> str:
    """Адрес панели для окна из базового URL сервера (127.0.0.1 или туннель).
    С токеном — он во фрагменте, см. LAUNCH_PREFIX."""
    url = str(http_url).rstrip("/") + PANEL_QUERY
    return url + (LAUNCH_PREFIX + token if token else "")


def strip_launch(url: str) -> str:
    """URL без токена — для логов и сообщений. Токен всегда последний
    сегмент фрагмента, поэтому всё после него просто отбрасываем."""
    u = str(url)
    i = u.find(LAUNCH_PREFIX)
    return u[:i] if i >= 0 else u



# ── чистая механика: PID, флаги, геометрия ───────────────────────────────────
def pid_alive(pid: "int | None") -> bool:
    if not pid:
        return False
    try:
        import ctypes
        import ctypes.wintypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not h:
            return False
        try:
            code = ctypes.wintypes.DWORD()
            if k32.GetExitCodeProcess(h, ctypes.byref(code)):
                return code.value == STILL_ACTIVE
            return True                      # не прочитали код — процесс точно есть
        finally:
            k32.CloseHandle(h)
    except AttributeError:                   # не Windows: единственный честный тест
        try:
            os.kill(int(pid), 0)
            return True
        except OSError:
            return False
    except Exception:
        return False


def read_pid(path: Path) -> "int | None":
    try:
        txt = Path(path).read_text(encoding="utf-8").strip()
        return int(txt) if txt else None
    except Exception:
        return None


def running_pid(paths: dict) -> "int | None":
    """PID живого нашего окна (или None). Протухший lock не мешает: его
    владелец мёртв, окно поднимется заново."""
    pid = read_pid(paths["lock"])
    return pid if pid_alive(pid) else None


def load_geo(paths: dict) -> dict:
    """{width,height[,x,y]} из player_window.json; враньё по границам — к дефолтам."""
    geo = dict(_DEFAULT_GEO)
    try:
        d = json.loads(Path(paths["state"]).read_text(encoding="utf-8")) or {}
        g = d.get("geo")
        if isinstance(g, dict):
            for key in ("width", "height", "x", "y"):
                v = g.get(key)
                lo, hi = _GEO_BOUNDS[key]
                if isinstance(v, int) and lo <= v <= hi:
                    geo[key] = v
    except Exception:
        pass
    return geo


def save_geo(paths: dict, geo: dict, on_top: "bool | None" = None) -> None:
    try:
        d: dict = {}
        p = Path(paths["state"])
        if p.exists():
            try:
                d = json.loads(p.read_text(encoding="utf-8")) or {}
            except Exception:
                d = {}
        d["geo"] = {k: v for k, v in geo.items() if isinstance(v, int)}
        if on_top is not None:
            d["on_top"] = bool(on_top)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(d), encoding="utf-8")
    except Exception:
        pass


def load_on_top(paths: dict) -> bool:
    try:
        d = json.loads(Path(paths["state"]).read_text(encoding="utf-8")) or {}
        return bool(d.get("on_top"))
    except Exception:
        return False


def touch(path: Path) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(str(os.getpid()), encoding="utf-8")


def write_launch(paths: dict, token: str) -> bool:
    """Передать ЖИВОМУ окну свежий токен запуска (окно заберёт его из файла и
    попросит страницу его предъявить). Без этого повторный клик по кнопке
    только поднимал окно с протухшей кукой. Файл — не секрет: доступ к нему =
    доступ к диску владельца, где и config.yaml с session-secret. Вторая
    строка файла — дедлайн: просроченный флаг окно выбрасывает само.
    """
    if not _TOKEN_RE.fullmatch(token or ""):
        return False
    try:
        p = Path(paths["launch"])
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"{token}\n{int(time.time()) + _LAUNCH_GRACE_S}",
                     encoding="utf-8")
        return True
    except Exception:
        return False



def focus_existing(paths: dict, token: str = "") -> bool:
    """Окно живо — поднять (сторож внутри окна читает флаг) и, если есть чем,
    дать свежий пропуск."""
    if running_pid(paths) is None:
        return False
    ok = False
    try:
        touch(paths["show"])
        ok = True
    except Exception:
        pass
    if token:
        write_launch(paths, token)
    return ok



def request_launcher(paths: dict, url: str, timeout: float = 2.5,
                     poll: float = 0.1) -> bool:
    """Попросить ЖИВОЙ лаунчер показать своё панельное окно («oswin» —
    полноценный pywebview-мост). Лаунчер пишет launcher.rpwin.ok в ответ.
    Старый frozen-exe протокола не знает — дожидаемся timeout и честно
    уходим в standalone-окно, а не вешаем кнопку."""
    if not pid_alive(read_pid(paths["launcher_lock"])):
        return False
    req, ack = paths["launcher_req"], paths["launcher_ack"]
    try:
        ack.unlink(missing_ok=True)
        req.parent.mkdir(parents=True, exist_ok=True)
        req.write_text(url, encoding="utf-8")
    except Exception:
        return False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if ack.exists():
                ack.unlink(missing_ok=True)
                return req_missing_or_ok(req)
        except Exception:
            pass
        time.sleep(poll)
    try:
        req.unlink(missing_ok=True)           # не оставлять старому лаунчеру
    except Exception:
        pass
    return False


def req_missing_or_ok(req: Path) -> bool:
    """Лаунчер заберает запрос сам; если файл ещё там — он его не прочитал."""
    try:
        return not Path(req).exists()
    except Exception:
        return False


def spawn_interpreter(base_dir: Path) -> str:
    """Чем поднимать окно: .venv лаунчера (в нём точно есть pywebview),
    иначе текущий интерпретатор сервера."""
    venv = Path(base_dir) / ".venv" / "Scripts" / "python.exe"
    return str(venv) if venv.exists() else sys.executable


DETACHED_FLAGS = 0x00000008 | 0x00000200 | 0x08000000   # DETACHED | NEW_GROUP | NO_WINDOW


def spawn_standalone(paths: dict, base_dir: Path, url: str,
                     wait_s: float = 6.0, poll: float = 0.15) -> dict:
    """Запустить окно и дождаться, что оно реально живо (lock PID).
    Упало сразу — почти всегда нет pywebview/WebView2: это и есть честная
    причина для кнопки, а не молчаливый док."""
    exe = spawn_interpreter(Path(base_dir))
    try:
        Path(paths["log"]).parent.mkdir(parents=True, exist_ok=True)
        logf = open(paths["log"], "a", encoding="utf-8")
        proc = subprocess.Popen(
            [exe, "-m", "ripster.player_window", "--url", url,
             "--base", str(Path(base_dir))],
            cwd=str(base_dir), stdin=subprocess.DEVNULL,
            stdout=logf, stderr=logf,
            creationflags=DETACHED_FLAGS if os.name == "nt" else 0,
            close_fds=True,
        )
    except Exception as e:
        return {"ok": False, "reason": "spawn", "detail": str(e)[:160]}
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        rc = proc.poll()
        if rc is not None:
            return {"ok": False, "reason": "pywebview", "detail": f"exit {rc}"}
        if running_pid(paths):
            return {"ok": True, "how": "standalone"}
        time.sleep(poll)
    return {"ok": False, "reason": "timeout",
            "detail": f"window did not register within {wait_s:.0f}s"}


def open_external(base_dir: Path | str, url: str, ask_launcher: bool = True,
                  token: str = "") -> dict:
    """Оркестрация кнопки: фокус → лаунчер → standalone. Возвращает
    {ok, how} или {ok: False, reason} — reason переводит в i18n интерфейс.

    ask_launcher=False — звала ВКЛАДКА БРАУЗЕРА (хоста нет внутри pywebview,
    и лаунчерово окно панели связалось бы с главным окном ЛАУНЧЕРА, а не с
    этой вкладкой: мост oswin живёт только внутри pywebview). Такой просят
    сразу standalone relay-окно — оно честно говорит с вкладкой через /ws.

    token — одноразовый пропуск запуска (ripster.launch_token). В url он уже
    сидит во фрагменте (пути лаунчера и standalone); живому окну это единственный
    способ получить свежую куку — страницу никто не перезагружает.
    """
    paths = win_paths(base_dir)
    if focus_existing(paths, token):
        return {"ok": True, "how": "focus"}
    if ask_launcher and request_launcher(paths, url):
        return {"ok": True, "how": "launcher"}
    return spawn_standalone(paths, Path(base_dir), url)



def close_external(base_dir: Path | str) -> dict:
    """Просьба закрыть окно (команда ✕ из самой панели). Лаунчерово окно
    тут не закрываем: у него своя логика «крестик = спрятать»."""
    paths = win_paths(base_dir)
    if running_pid(paths) is None:
        return {"ok": False, "reason": "closed"}
    try:
        touch(paths["close"])
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "reason": "spawn", "detail": str(e)[:160]}


# ── рантайм: то самое окно ───────────────────────────────────────────────────
def _logf(paths: dict, msg: str) -> None:
    print(f"[player-window] {msg}", flush=True)
    try:
        Path(paths["log"]).parent.mkdir(parents=True, exist_ok=True)
        with open(paths["log"], "a", encoding="utf-8") as f:
            f.write(f"[player-window] {msg}\n")
    except Exception:
        pass


class WindowApi:
    """js_api окна. Панель зовёт pin() из настроек (📌), host() в relay-среде
    не нужен: сообщения идут через сервер (/ws)."""

    def __init__(self, paths: dict, get_window):
        self._paths = paths
        self._get_window = get_window

    def pin(self, on):
        on = bool(on)
        try:
            win = self._get_window()
            if win is not None:
                win.on_top = on
            save_geo(self._paths, load_geo(self._paths), on_top=on)
            return "ok"
        except Exception as e:
            _logf(self._paths, f"pin failed: {type(e).__name__}: {e}")
            return "fail"

    def hide(self):
        try:
            win = self._get_window()
            if win is not None:
                win.hide()
            return "ok"
        except Exception:
            return "fail"

    def reauth(self):
        """Панель упёрлась в 403 на /ws (кука села или профиль потерялся) и
        просит свежий запуск. Окно не может выписать себе допуск — оно лишь
        поднимает флаг, сервер (routes/player_window) видит его и передаёт
        через «launch» новый разовый пропуск."""
        try:
            Path(self._paths["reauth"]).parent.mkdir(parents=True, exist_ok=True)
            Path(self._paths["reauth"]).write_text(str(os.getpid()), encoding="utf-8")
            return "ok"
        except Exception as e:
            _logf(self._paths, f"reauth flag failed: {type(e).__name__}: {e}")
            return "fail"


def _read_launch_flag(path: Path) -> "str | None":
    """Токен из файла-флага, если он живой и правильной формы. Просроченный
    или мусорный возвращаем как '' — вызывающий обязан убрать флаг, чтобы он
    не всплыл в следующем окне."""
    try:
        lines = Path(path).read_text(encoding="utf-8").split()
    except Exception:
        return None                       # файла нет / не читается — ничего
    if not lines:
        return ""
    try:
        deadline = int(lines[1]) if len(lines) > 1 else 0
    except ValueError:
        deadline = 0
    if deadline and time.time() > deadline:
        return ""
    tok = lines[0]
    return tok if _TOKEN_RE.fullmatch(tok) else ""


def _claim_in_window(win, token: str) -> bool:
    """Попросить страницу предъявить токен серверу (window.rpClaim в panel.js).
    До загрузки страницы evaluate_js бросается/молчит — флаг тогда держим."""
    try:
        res = win.evaluate_js(
            "window.rpClaim ? String(window.rpClaim('%s')) : 'nopage'" % token)
        return str(res) != "nopage"
    except Exception:
        return False


def _watch_flags(paths: dict, win, stop) -> None:
    """Флаги серверной оркестрации: show — поднять (повторный клик по кнопке),
    close — панель попросила закрыться; launch — свежий разовый пропуск
    запуска (окно живо, страницу не перезагружаем, поэтому токен передаём
    вызовом window.rpClaim). Опрос 0.5 с: это не плеер, задержка нажатия
    невидна."""
    import threading
    while not stop.is_set():
        try:
            if Path(paths["show"]).exists():
                Path(paths["show"]).unlink()
                win.show()
                try:
                    win.restore()
                except Exception:
                    pass
                try:
                    win.focus()
                except Exception:
                    pass
            launch = Path(paths["launch"])
            if launch.exists():
                token = _read_launch_flag(launch)
                if token == "" or _claim_in_window(win, token or ""):
                    launch.unlink(missing_ok=True)   # '' = мусор/просрочено
            if Path(paths["close"]).exists():
                Path(paths["close"]).unlink()
                stop.set()
                win.destroy()              # у pywebview-окна нет close(): флаг
                # «закрыться» молча падал в AttributeError и окно выживало
        except Exception as e:
            _logf(paths, f"flag watch: {type(e).__name__}: {e}")
        time.sleep(0.5)


def _start_kwargs(paths: dict, webview_module=None) -> dict:
    """Аргументы webview.start: ПОСТОЯННЫЙ профиль браузера, а не инкогнито.

    Без него WebView2 живёт в inPrivate-профиле: кука, полученная по разовому
    токену запуска, умирает вместе с окном, и каждый следующий старт окна
    требовал бы нового допуска. С ним — окно входит один раз, а дальние
    перезапуски получают ту же сессию (её отзыв — /api/logout или смена
    пароля, как у любой другой хозяйской куки).

    Старый pywebview может не знать `storage_path` — тогда молча уходим в
    дефолтный запуск, окно обязанности не теряет.
    """
    if webview_module is None:
        import webview as webview_module     # noqa: PLC0415
    try:
        import inspect
        params = inspect.signature(webview_module.start).parameters
    except (TypeError, ValueError):
        return {}
    if "storage_path" not in params or "private_mode" not in params:
        return {}
    try:
        Path(paths["profile"]).mkdir(parents=True, exist_ok=True)
    except Exception:
        return {}
    return {"private_mode": False, "storage_path": str(paths["profile"])}


def main(argv: "list[str] | None" = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="ripster.player_window",
                                 description="Внешнее окно панели Ripster (relay)")
    ap.add_argument("--url", required=True)
    ap.add_argument("--base", default=str(Path(__file__).resolve().parent.parent))
    args = ap.parse_args(argv)

    base = Path(args.base)
    paths = win_paths(base)

    # Single instance: второй запуск только поднимает живое окно.
    if running_pid(paths):
        focus_existing(paths)
        return 0
    try:
        paths["lock"].parent.mkdir(parents=True, exist_ok=True)
        paths["lock"].write_text(str(os.getpid()), encoding="utf-8")
    except Exception as e:
        _logf(paths, f"lock failed: {e}")

    try:
        import webview                       # pywebview
    except Exception as e:
        _logf(paths, f"no pywebview ({type(e).__name__}: {e})")
        try:
            paths["lock"].unlink()
        except Exception:
            pass
        return 3

    geo = load_geo(paths)
    win = webview.create_window(
        "Ripster — плеер", url=args.url, js_api=WindowApi(paths, lambda: win),
        width=geo.get("width"), height=geo.get("height"),
        x=geo.get("x"), y=geo.get("y"),
        on_top=load_on_top(paths), resizable=True, min_size=(340, 520))

    import threading
    stop = threading.Event()

    def _closing():
        stop.set()
        return True                          # здесь крестик = выход, окно одноразовое

    def _remember(geom_key):
        def handler(*vals):
            try:
                g = load_geo(paths)
                names = (["width", "height"] if geom_key == "resized" else ["x", "y"])
                g.update(dict(zip(names, [int(v) for v in vals])))
                save_geo(paths, g)
            except Exception:
                pass
        return handler

    win.events.closed += lambda: stop.set()
    win.events.closing += _closing
    win.events.resized += _remember("resized")
    win.events.moved += _remember("moved")
    threading.Thread(target=_watch_flags, args=(paths, win, stop), daemon=True).start()

    _logf(paths, f"opened {strip_launch(args.url)} geo={geo}")
    try:
        webview.start(**_start_kwargs(paths))   # блокирует до закрытия окна
    except Exception as e:
        _logf(paths, f"webview.start failed: {type(e).__name__}: {e}")
    finally:
        try:
            paths["lock"].unlink()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
