"""Ripster launcher (from SOURCE) — starts the server and opens the UI in its OWN
native window, not a browser tab.

Replaces the 35 MB PyQt `RipsterLauncher.exe`: lightweight, shipped as source.
Uses **pywebview** (the system webview — Edge WebView2 on Windows; no bundled
Chromium, unlike Electron/QtWebEngine). Falls back to the default browser when
pywebview isn't installed. Autonomous-open like deemix, but a multi-file project.

Entry point: project-root `ripster_launcher.py` calls `main()`.

The server is the bootstrap chain `.venv python app.py → Py312 child` (the Py312
child owns the port — see github_setup/SESSION_LOG_FULL §0). DON'T kill the .venv
parent thinking it's a stray.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from urllib.request import urlopen

# Project root = parent of the `ripster` package dir.
BASE_DIR = Path(__file__).resolve().parent.parent


def launcher_log_path() -> Path:
    """Куда писать диагностику запуска. Репозиторий этого файла — НЕ боевой
    лог: тесты обязаны тыкать в tmp (иначе «webview halted» из-под monkeypatch
    засоряет logs/launcher.log живому лаунчеру). RIPSTER_LAUNCHER_LOG — явный
    override, им же пользуется базовый fixture тестов."""
    env = os.environ.get("RIPSTER_LAUNCHER_LOG")
    if env:
        return Path(env)
    return BASE_DIR / "logs" / "launcher.log"


def _log(msg: str) -> None:
    """Print AND append to launcher.log so a flash-and-close run is still
    diagnosable after the window/console is gone."""
    print(msg, flush=True)
    try:
        logf = launcher_log_path()
        logf.parent.mkdir(parents=True, exist_ok=True)
        with open(logf, "a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except Exception:
        pass


# ── pure helpers (unit-tested) ───────────────────────────────────────────────
def config_port(base_dir: Path = BASE_DIR) -> int:
    """HTTP port from config.yaml, default 7799."""
    try:
        import yaml
        c = yaml.safe_load((Path(base_dir) / "config.yaml").read_text(encoding="utf-8")) or {}
        return int(c.get("port", 7799))
    except Exception:
        return 7799


def server_url(base_dir: Path = BASE_DIR) -> str:
    # 127.0.0.1, NOT localhost (Spotify OAuth rejects localhost since Apr 2025).
    return f"http://127.0.0.1:{config_port(base_dir)}"


def bootstrap_python(base_dir: Path = BASE_DIR) -> str:
    """The .venv interpreter that bootstraps the server; falls back to the current
    interpreter if no .venv is present (portable installs)."""
    venv = Path(base_dir) / ".venv" / "Scripts" / "python.exe"
    return str(venv) if venv.exists() else sys.executable


# ── runtime (integration; not unit-tested) ───────────────────────────────────
def server_alive(url: str, timeout: float = 2.0) -> bool:
    """True if the server answers at all — any HTTP status (incl. 303/401 behind
    login) counts as up; only a connection error means down."""
    try:
        urlopen(url, timeout=timeout)
        return True
    except Exception as e:
        return getattr(e, "code", None) is not None   # HTTPError has .code → up


def wait_for_server(url: str, timeout: float = 45.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if server_alive(url):
            return True
        time.sleep(1.0)
    return False


def start_server(base_dir: Path = BASE_DIR) -> subprocess.Popen:
    return subprocess.Popen([bootstrap_python(base_dir), str(Path(base_dir) / "app.py")],
                            cwd=str(base_dir))


def _first_run_tool_hint(base_dir: Path = BASE_DIR, timeout: float = 8.0) -> list[str]:
    """Best-effort: list missing REQUIRED heavy tools so the user knows to open the
    Setup tab. Never blocks launch — and now never CAN, with a hard wall-clock cap:
    a hung tool-probe here used to stall the launcher BEFORE the backend was even
    spawned, so the user stared at nothing (see docs/BOOT_AUTOSTART_2026-09-24.md).
    The probe runs in a daemon thread the main flow does not wait past `timeout`."""
    out: list[str] = []

    def go():
        try:
            import asyncio
            from ripster import setup as _setup
            tools = asyncio.run(_setup.check_tools())
            out.extend(t.get("label", k) for k, t in tools.items()
                       if t.get("required") and not t.get("found"))
        except Exception:
            pass

    import threading
    th = threading.Thread(target=go, daemon=True)
    th.start()
    th.join(timeout)
    return out


def open_window(url: str, title: str = "Ripster") -> str:
    """Open `url` in a native pywebview window; fall back to the browser. Returns
    which path was taken ('webview' | 'browser'). Logs the webview error so a
    silent fallback (window flashes + closes) is diagnosable."""
    try:
        import webview                       # pywebview
        _log(f"[launcher] opening webview window → {url}")
        webview.create_window(title, url, width=1280, height=860)
        webview.start()                      # blocks until the window is closed
        _log("[launcher] webview window closed normally")
        return "webview"
    except Exception as e:
        import traceback
        _log(f"[launcher] webview FAILED ({type(e).__name__}: {e}) → browser fallback")
        _log(traceback.format_exc())
        webbrowser.open(url)
        return "browser"


def main(base_dir: Path = BASE_DIR) -> None:
    url = server_url(base_dir)
    proc = None
    if not server_alive(url):
        # Спавн бэкенда — ПЕРВЫМ делом: всё, что ниже (подсказка про инструменты),
        # только бы не задерживает старт. Раньше порядок был обратный, и зависший
        # tool-probe оставлял пользователя перед закрытым портом.
        proc = start_server(base_dir)
        missing = _first_run_tool_hint(base_dir)
        if missing:
            print("[launcher] Missing required tools — open the Setup tab to install: "
                  + ", ".join(missing), flush=True)
        if not wait_for_server(url):
            print(f"[launcher] server did not come up on {url}", flush=True)
    mode = open_window(url)
    # When the native window closes, stop the server we started (browser fallback
    # leaves it running — the user closed a tab, not the app).
    if mode == "webview" and proc is not None:
        try:
            proc.terminate()
        except Exception:
            pass


if __name__ == "__main__":
    main()
