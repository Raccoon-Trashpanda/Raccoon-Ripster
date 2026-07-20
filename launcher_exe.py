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

import os
import subprocess
import sys
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


def _log(msg: str) -> None:
    print(msg, flush=True)
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
    """A REAL interpreter to run app.py — never sys.executable (that's Ripster.exe
    itself when frozen). Prefer the install's bundled embeddable, then a dev .venv."""
    for cand in (BASE / "python" / "python.exe",
                 BASE / ".venv" / "Scripts" / "python.exe"):
        if cand.exists():
            return str(cand)
    return sys.executable  # last resort (non-frozen dev run)


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


# ── Tray icon + minimize-to-tray ───────────────────────────────────────────────
# The window's close (X) and minimize both fold the app into the system tray
# instead of quitting — the server keeps running so downloads continue. The tray
# icon's left-click restores the window; "Выход" really quits. Gated by the
# config key `minimize-to-tray` (default ON). All of this is best-effort: any
# failure degrades to plain close/quit and is logged, never crashes the launcher.

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
    import json
    try:
        if WIN_STATE:
            WIN_STATE.write_text(json.dumps(geo), encoding="utf-8")
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
    window; "Выход" calls do_quit(). Returns the pystray Icon or None."""
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

    try:
        menu = pystray.Menu(
            pystray.MenuItem("Открыть Ripster", _restore, default=True),
            pystray.MenuItem("Выход", _quit),
        )
        icon = pystray.Icon("ripster", img, "Ripster", menu)
        icon.run_detached()   # spins its own thread; returns immediately
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


def _loading_html(url: str) -> str:
    """Self-polling splash shown WHILE the server boots, instead of pointing the
    window straight at the server URL. A cold start (first run: antivirus
    scanning the freshly-unpacked python.exe, a slow disk, first-time imports)
    can easily take longer than any fixed wait we'd do server-side — users hit
    the raw browser "127.0.0.1 refused to connect" page and assume Ripster is
    broken. This polls /api/ping itself and navigates over once it's up, no
    matter how long that takes; only gives up (with an actionable message,
    not a dead end) after several minutes."""
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
  <h3 id="msg">Запускаю Ripster…</h3>
  <p id="detail">Первый запуск может занять минуту — антивирус проверяет файлы.</p>
</div>
<script>
  let tries = 0;
  async function poll() {{
    tries++;
    try {{
      const r = await fetch('{url}/api/ping', {{cache:'no-store'}});
      if (r.ok) {{ location.href = '{url}/'; return; }}
    }} catch (e) {{}}
    if (tries === 15) {{
      document.getElementById('detail').textContent =
        'Всё ещё запускается — это нормально на первом старте.';
    }}
    if (tries > 180) {{
      document.getElementById('msg').textContent = 'Сервер не отвечает';
      document.getElementById('detail').innerHTML =
        'Проверь <code>logs\\\\launcher.log</code> и <code>logs\\\\console.log</code> в папке установки.<br>' +
        'Частая причина — антивирус блокирует <code>python\\\\python.exe</code>.';
      return;
    }}
    setTimeout(poll, 1000);
  }}
  poll();
</script></body></html>"""


def open_window(url: str, port: int, win_open, title: str = "Ripster"):
    """Native pywebview window with tray + minimize-to-tray; browser fallback.
    Returns ('webview', state) | ('browser', None). `state['quit']` is True only
    when the user really chose Выход (so main() tears the server down)."""
    try:
        import threading
        import webview
        _log(f"[launcher] opening webview window -> {url}")
        geo = _load_win_state()
        window = webview.create_window(
            title, html=_loading_html(url),
            width=geo.get("width", 1280), height=geo.get("height", 860),
            x=geo.get("x"), y=geo.get("y"))
        state = {"quit": False, "tray": None, "notified": False}
        use_tray = tray_enabled()

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

        webview.start()  # blocks until the window is destroyed (real quit)
        _log("[launcher] webview window closed")
        return "webview", state
    except Exception as e:
        import traceback
        _log(f"[launcher] webview FAILED ({type(e).__name__}: {e}) -> browser fallback")
        _log(traceback.format_exc())
        webbrowser.open(url)
        return "browser", None


def _supervise(box: list, port: int, win_open) -> None:
    """Single owner of the server lifecycle. While the window is open, keep the
    server alive: if its process exits (crash OR a /api/restart clean-exit), wait
    for the port to free and respawn it WINDOWLESS — exactly once per exit. A
    backoff cap stops a crash-loop. This is what makes restart reliable and silent
    (no duelling respawns, no cmd-window flashes)."""
    import time as _t
    fails = 0
    last_spawn = _t.time()
    while win_open.is_set():
        proc = box[0]
        if proc is None or proc.poll() is None:
            _t.sleep(0.5)
            continue
        # Server process has exited.
        if not win_open.is_set():
            break
        # A server that ran a good while before dying = an intentional restart, not
        # a crash loop → reset the failure counter.
        if _t.time() - last_spawn > 20:
            fails = 0
        fails += 1
        if fails > 8:
            _log("[launcher] server keeps exiting — stopping respawn")
            break
        # Let the old process fully release the port before rebinding.
        for _ in range(40):
            if not win_open.is_set() or port_free(port):
                break
            _t.sleep(0.25)
        if not win_open.is_set():
            break
        _log(f"[launcher] server exited → respawning on {port} (#{fails})")
        box[0] = start_server(port)
        last_spawn = _t.time()
        wait_for_ripster(port, timeout=30)


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
    LOCK_FILE = logs / "launcher.lock"
    SHOW_FLAG = logs / "launcher.show"
    WIN_STATE = logs / "window_state.json"
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


def main() -> None:
    import threading
    if not _single_instance_guard():
        return
    desired = config_port()
    box = [None]   # box[0] = the server process WE own (None when attaching to an existing one)
    if ripster_alive(desired):
        # OUR Ripster is already running here (e.g. user relaunched) → just attach.
        port = desired
        _log(f"[launcher] Ripster already live on {port} — attaching")
    else:
        # Free port at/after the desired one — auto-skips a busy 7799 so a clean
        # install never collides with another app (or another project's server).
        port = find_free_port(desired)
        if port != desired:
            _log(f"[launcher] port {desired} busy → using {port}")
        box[0] = start_server(port)
        if not wait_for_ripster(port):
            _log(f"[launcher] server did not come up on 127.0.0.1:{port}")

    # Supervise ONLY a server we started (not one we merely attached to).
    win_open = threading.Event()
    win_open.set()
    if box[0] is not None:
        threading.Thread(target=_supervise, args=(box, port, win_open), daemon=True).start()

    url = f"http://127.0.0.1:{port}"
    mode, _state = open_window(url, port, win_open)  # blocks until real quit

    # Window destroyed (user chose Выход) → stop supervising FIRST (so it doesn't
    # respawn), then stop the server we own. Browser fallback leaves it running
    # (a tab was closed, not the app).
    win_open.clear()
    if mode == "webview" and box[0] is not None:
        try:
            box[0].terminate()
        except Exception:
            pass
    try:
        if LOCK_FILE is not None and LOCK_FILE.exists():
            LOCK_FILE.unlink()
    except Exception:
        pass


if __name__ == "__main__":
    main()
