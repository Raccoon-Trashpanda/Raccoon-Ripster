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


def open_window(url: str, title: str = "Ripster") -> str:
    """Native pywebview window; browser fallback. Returns 'webview' | 'browser'."""
    try:
        import webview
        _log(f"[launcher] opening webview window -> {url}")
        webview.create_window(title, url, width=1280, height=860)
        webview.start()  # blocks until the window is closed
        _log("[launcher] webview window closed normally")
        return "webview"
    except Exception as e:
        import traceback
        _log(f"[launcher] webview FAILED ({type(e).__name__}: {e}) -> browser fallback")
        _log(traceback.format_exc())
        webbrowser.open(url)
        return "browser"


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


def main() -> None:
    import threading
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
    mode = open_window(url)        # blocks until the window is closed

    # Window closed → stop supervising FIRST (so it doesn't respawn), then stop the
    # server we own. Browser fallback leaves it running (a tab was closed, not the app).
    win_open.clear()
    if mode == "webview" and box[0] is not None:
        try:
            box[0].terminate()
        except Exception:
            pass


if __name__ == "__main__":
    main()
