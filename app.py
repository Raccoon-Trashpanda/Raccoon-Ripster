#!/usr/bin/env python3
"""
Ripster
Universal music downloader · Apple Music · Qobuz · Deezer · Tidal
Run:  python app.py
Open: http://127.0.0.1:7799
      (not http://localhost — Spotify OAuth rejects localhost since April 2025)
"""

import asyncio, os, sys, time
import platform

# Belt-and-suspenders sys.path setup. The bundled installer ships an embeddable
# Python whose python3xx._pth already puts the app dir (`..`) AND bundled
# site-packages on sys.path natively at startup — that's the primary fix. This
# runtime insert covers the OTHER launch modes that have no ._pth (from-source
# .venv, IDE/double-click run from a foreign cwd): add the app dir (for
# `import ripster`) and bundled site-packages (for `import uvicorn` etc.), as
# absolute paths so it works regardless of cwd.
_APPDIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _APPDIR)
_BUNDLED_SP = os.path.join(_APPDIR, "python", "Lib", "site-packages")
if os.path.isdir(_BUNDLED_SP):
    sys.path.insert(0, _BUNDLED_SP)

# Put two dirs on PATH that the embeddable Python otherwise hides, so engines and
# their child processes can find their tools:
#   • <python>\Scripts — pip console-scripts (deemix, rip/streamrip, gamdl). Without
#     this shutil.which("deemix"/"rip") → None → download dies "[WinError 2]".
#   • <app>\tools — Setup-installed binaries (ffmpeg, mp4decrypt, N_m3u8DL-RE …).
#     AMD shells out to bare `ffmpeg`; missing it means AMD "decrypts" but writes
#     no file. Putting tools/ on PATH lets amd_runner's shutil.which("ffmpeg") find it.
for _pdir in (os.path.join(os.path.dirname(sys.executable), "Scripts"),
              os.path.join(_APPDIR, "tools"),
              os.path.join(_APPDIR, "tools", "node")):       # portable Node for SoundCloud/Lucida
    if os.path.isdir(_pdir) and _pdir.lower() not in os.environ.get("PATH", "").lower():
        os.environ["PATH"] = _pdir + os.pathsep + os.environ.get("PATH", "")

# Ensure UTF-8 output on Windows (avoids cp1251 crash on box-drawing chars)
if sys.stdout and hasattr(sys.stdout, 'reconfigure'):
    try: sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception: pass
if sys.stderr and hasattr(sys.stderr, 'reconfigure'):
    try: sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception: pass

from pathlib import Path
from datetime import datetime

IS_WINDOWS = platform.system() == "Windows"


def _resolve_real_home() -> str:
    """Find the REAL user-profile dir even when the launcher stripped the env.

    The PyQt/detached launcher can start this process with USERPROFILE/HOME/APPDATA/
    USERNAME all missing — so a naive ``C:\\Users\\%USERNAME%`` guess lands on
    ``C:\\Users\\Default`` (a read-only system profile → streamrip "Access denied").
    Resolve it reliably: env → derive from APPDATA/TEMP → Win32 token profile dir."""
    # 1) Direct env vars (only if they point at a real dir)
    for v in (os.environ.get("USERPROFILE"), os.environ.get("HOME")):
        if v and os.path.isdir(v):
            return v
    hd, hp = os.environ.get("HOMEDRIVE", ""), os.environ.get("HOMEPATH", "")
    if hd and hp and os.path.isdir(hd + hp):
        return hd + hp
    # 2) Derive from any AppData/Temp var that survived
    for v, suffix in ((os.environ.get("APPDATA"),      r"\AppData\Roaming"),
                      (os.environ.get("LOCALAPPDATA"), r"\AppData\Local"),
                      (os.environ.get("TEMP"),         r"\AppData\Local\Temp"),
                      (os.environ.get("TMP"),          r"\AppData\Local\Temp")):
        if v and v.rstrip("\\").lower().endswith(suffix.lower()):
            h = v.rstrip("\\")[: -len(suffix)]
            if h and os.path.isdir(h):
                return h
    # 3) Win32: the profile dir of the user actually running this process
    if IS_WINDOWS:
        try:
            import ctypes
            from ctypes import wintypes
            k32, adv, uenv = ctypes.windll.kernel32, ctypes.windll.advapi32, ctypes.windll.userenv
            # Set types so the GetCurrentProcess pseudo-handle (-1) isn't truncated
            # on 64-bit — without this OpenProcessToken fails.
            k32.GetCurrentProcess.restype = wintypes.HANDLE
            adv.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                             ctypes.POINTER(wintypes.HANDLE)]
            adv.OpenProcessToken.restype = wintypes.BOOL
            uenv.GetUserProfileDirectoryW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR,
                                                      ctypes.POINTER(wintypes.DWORD)]
            uenv.GetUserProfileDirectoryW.restype = wintypes.BOOL
            tok = wintypes.HANDLE()
            if adv.OpenProcessToken(k32.GetCurrentProcess(), 0x0008, ctypes.byref(tok)):
                sz = wintypes.DWORD(0)
                uenv.GetUserProfileDirectoryW(tok, None, ctypes.byref(sz))
                buf = ctypes.create_unicode_buffer(sz.value or 260)
                if uenv.GetUserProfileDirectoryW(tok, buf, ctypes.byref(sz)) and buf.value:
                    if os.path.isdir(buf.value):
                        return buf.value
        except Exception:
            pass
    # 4) expanduser as a last resort
    h = os.path.expanduser("~")
    return h if h and h != "~" and os.path.isdir(h) else ""


def _repair_home_env() -> None:
    """Ensure USERPROFILE/HOME/APPDATA exist so streamrip/deezer (which call
    Path.home()/read APPDATA at import) and every child process have a valid home.
    See _resolve_real_home for why a naive guess is unsafe."""
    home = _resolve_real_home()
    if not home:
        return  # couldn't determine — better to leave env alone than point at Default
    # Override (not setdefault) USERPROFILE/HOME/APPDATA: a bogus value from an
    # earlier broken run must be corrected, not preserved.
    os.environ["USERPROFILE"] = home
    os.environ["HOME"] = home
    if IS_WINDOWS:
        drive, path = os.path.splitdrive(home)
        if drive:
            os.environ["HOMEDRIVE"] = drive
            os.environ["HOMEPATH"] = path or "\\"
        roaming = os.path.join(home, "AppData", "Roaming")
        local   = os.path.join(home, "AppData", "Local")
        # Only fix APPDATA if missing or pointing outside the resolved home.
        if not os.environ.get("APPDATA", "").lower().startswith(home.lower()):
            os.environ["APPDATA"] = roaming
        if not os.environ.get("LOCALAPPDATA", "").lower().startswith(home.lower()):
            os.environ["LOCALAPPDATA"] = local


_repair_home_env()

try:
    import httpx
except ImportError:
    print("\n" + "=" * 68, file=sys.stderr)
    print("  RIPSTER: httpx is not installed.", file=sys.stderr)
    print("  Run:  pip install -r requirements.txt", file=sys.stderr)
    print("=" * 68 + "\n", file=sys.stderr)
    raise SystemExit(1)

import uvicorn
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import JSONResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

# Ensure the script directory is on sys.path so ``ripster`` resolves
# even when cwd is somewhere else (common on Windows double-click / IDE runs).
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))


# ── Protobuf runtime guard — kills the recurring "wrapper doesn't download" bug ──
# Protobuf's rule is: runtime version must be >= the gencode version of every
# loaded _pb2 module. We load modules from several generators:
#   • AMD/public-wrapper stubs — gencode 6.31.x
#   • OrpheusDL desktop_api / extendedmetadata — gencode 6.33.4
#   • pywidevine — needs >= 6.33
# So the correct floor is 6.33.4 (covers ALL three; 6.33.4 runtime happily loads
# the 6.31 AMD stubs). The OLD guard force-pinned 6.31.1, which broke orpheus's
# 6.33 gencode (VersionError on lyrics/metadata) AND silently reverted every
# manual bump on each restart. We now only repair when protobuf is BELOW the
# floor (e.g. a stray `pip install` dragged it down), never downgrade. Read the
# installed dist version via importlib.metadata WITHOUT importing protobuf, so a
# repair in this run is picked up by the later real import.
_PB_FLOOR = "6.33.4"


def _ver_tuple(v: str) -> tuple:
    out = []
    for p in (v or "").split("."):
        try:
            out.append(int(p))
        except Exception:
            break
    return tuple(out)


def _ensure_protobuf_runtime() -> None:
    try:
        from importlib.metadata import version as _pbver
        ver = _pbver("protobuf")
    except Exception:
        return
    if _ver_tuple(ver) >= _ver_tuple(_PB_FLOOR):
        return
    print(f"[startup] ⚠ protobuf {ver} < {_PB_FLOOR} — orpheus/AMD/pywidevine need ≥{_PB_FLOOR}; auto-repairing…",
          flush=True)
    try:
        import subprocess
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", f"protobuf=={_PB_FLOOR}"],
                       timeout=180, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        from importlib.metadata import version as _pbver2
        new = _pbver2("protobuf")
        ok = _ver_tuple(new) >= _ver_tuple(_PB_FLOOR)
        print(f"[startup] {'✓ protobuf repaired → ' + new if ok else '✗ protobuf still ' + new + ' — run: pip install protobuf==' + _PB_FLOOR}",
              flush=True)
    except Exception as e:
        print(f"[startup] ✗ protobuf auto-repair failed ({e}); run: pip install protobuf=={_PB_FLOOR}",
              flush=True)


_ensure_protobuf_runtime()

# ── Ripster service layer ──────────────────────────────────────────────────────
try:
    from ripster.engines import get_engine
    from ripster.task_state import (
        TaskStatus, advance as _advance_task, try_advance as _try_advance_task,
        current as _task_status,
    )
    import ripster.metadata as _metadata
    import ripster.setup as _setup
    import ripster.amd as _amd

    # Auto-discover engines
    import pkgutil
    import ripster.engines as _engines_pkg
    _SKIP_MODULES = {"base", "registry", "__init__", "streamrip_utils"}
    for _mod_info in pkgutil.iter_modules(_engines_pkg.__path__):
        if _mod_info.name in _SKIP_MODULES:
            continue
        __import__(f"ripster.engines.{_mod_info.name}")
except ModuleNotFoundError as _e:
    # Distinguish the THREE real failure modes instead of always blaming a
    # missing 'ripster' package (the old message lied: it printed "не нашёл
    # ripster" even when ripster was present but an inner dependency — or a
    # broken bundled-Python path — was the real culprit). ALWAYS surface the
    # actual exception + a full traceback so the cause is diagnosable on a user's
    # machine without code spelunking.
    import traceback as _tb
    _has_pkg = (_SCRIPT_DIR / "ripster" / "__init__.py").exists()
    _missing = (getattr(_e, "name", "") or "").split(".")[0]
    _dep_problem = _has_pkg and _missing and _missing != "ripster"
    print("\n" + "=" * 68, file=sys.stderr)
    if _dep_problem:
        print(f"  RIPSTER: пакет 'ripster' на месте, но НЕ ХВАТАЕТ зависимости '{_missing}'", file=sys.stderr)
    else:
        print("  RIPSTER: не нашёл пакет 'ripster' рядом с app.py", file=sys.stderr)
    print("=" * 68, file=sys.stderr)
    print(f"  Точная ошибка: {type(_e).__name__}: {_e}", file=sys.stderr)
    print(f"  Script dir:    {_SCRIPT_DIR}", file=sys.stderr)
    print(f"  Python:        {sys.executable}", file=sys.stderr)
    print(f"  ripster/__init__.py есть: {_has_pkg}", file=sys.stderr)
    if _dep_problem:
        print(f"\n  Зависимость '{_missing}' не установлена в ЭТОТ интерпретатор.", file=sys.stderr)
        print("  Доустанови зависимости именно в него:", file=sys.stderr)
        print(f'    "{sys.executable}" -m pip install -r requirements.txt', file=sys.stderr)
        print(f'  или только её:  "{sys.executable}" -m pip install {_missing}', file=sys.stderr)
    elif not _has_pkg:
        print("\n  Похоже, при распаковке архива не извлеклись подпапки.", file=sys.stderr)
        print("  Рядом с app.py должны лежать:", file=sys.stderr)
        print("    ripster/__init__.py", file=sys.stderr)
        print("    ripster/auth.py", file=sys.stderr)
        print("    ripster/engines/*.py", file=sys.stderr)
        print("    ripster/routes/*.py", file=sys.stderr)
        print("    static/index.html", file=sys.stderr)
        print("\n  Перераспакуй архив (через 7-Zip / WinRAR, а не 'Extract' из", file=sys.stderr)
        print("  Проводника — он иногда пропускает вложенные папки).", file=sys.stderr)
    else:
        # ripster present AND the unresolved name IS 'ripster' → the interpreter
        # can't see its own app dir. On bundled-Python that's a broken ._pth
        # (missing 'import site' or 'Lib\\site-packages').
        print("\n  Пакет на месте, но интерпретатор его не видит — обычно битый", file=sys.stderr)
        print("  bundled-Python: в python\\python3xx._pth нет строки 'import site'", file=sys.stderr)
        print("  или 'Lib\\site-packages'. Пересобери бандл build_embedded_python.ps1.", file=sys.stderr)
    print("\n  ── полная трасса ──", file=sys.stderr)
    _tb.print_exc()
    print("=" * 68 + "\n", file=sys.stderr)
    raise SystemExit(1)

# ── Paths ──────────────────────────────────────────────────────────────────────
BASE_DIR       = Path(__file__).parent
CONFIG_FILE    = BASE_DIR / "config.yaml"
TOKENS_DIR     = BASE_DIR / "tokens"
HISTORY_FILE   = BASE_DIR / "history.json"
QUEUE_FILE     = BASE_DIR / "queue_pending.json"
WATCHLIST_FILE = BASE_DIR / "watchlist.json"

APP_VERSION = "3.0.0"
# Distributable release tag — what the self-updater compares against GitHub release
# tags (e.g. "1.0.6"). Kept separate from the internal APP_VERSION (3.x) so the two
# version lines don't collide. MUST be bumped together with
# github_setup/installer/ripster.iss AppVersion on every packaged build.
RELEASE_VERSION = "3.0.26"
try:
    import hashlib as _hlib
    APP_BUILD = _hlib.sha256(open(__file__, "rb").read()).hexdigest()[:8]
except Exception:
    APP_BUILD = datetime.now().strftime("%Y-%m-%d")
APP_AUTHORS = "Universal Music Downloader"
APP_REPO    = ""

# ── Config / persistence ───────────────────────────────────────────────────────
from ripster.config_service import (
    ConfigService,
    load_config   as _load_config,
    save_config   as _save_config_fn,
    write_downloader_config,
)
from ripster.persistence import (
    load_history,        save_history        as _save_history_fn,
    load_pending_queue,  save_pending_queue  as _save_pending_fn,
    load_watchlist,      save_watchlist      as _save_watchlist_fn,
)

config           = ConfigService(_load_config(CONFIG_FILE, TOKENS_DIR))
download_history = load_history(HISTORY_FILE)
watchlist        = load_watchlist(WATCHLIST_FILE)

def save_config(cfg):
    _save_config_fn(cfg, CONFIG_FILE, TOKENS_DIR)

def save_history(h: list):
    _save_history_fn(h, HISTORY_FILE)

def save_watchlist(w: list):
    _save_watchlist_fn(w, WATCHLIST_FILE)

def save_pending_queue():
    _save_pending_fn(queue, QUEUE_FILE)

# ── Quality helper ─────────────────────────────────────────────────────────────
def get_qualities() -> list:
    e = config.get("engine", "zhaarey")
    if e in ("amd", "gamdl", "zhaarey"):
        try:
            return get_engine(e).qualities()
        except KeyError:
            pass
    return get_engine("zhaarey").qualities()

# ── Global mutable state ───────────────────────────────────────────────────────
queue        = []
ws_clients   = set()
_ws_guest_sids: dict = {}

from ripster.queue_manager import QueueManager
_qs         = QueueManager()
_queue_lock = _qs.lock

# ── Service layer ──────────────────────────────────────────────────────────────
from ripster import service_layer as _svc_layer
_svc_layer.install(config)
_validate_url              = _svc_layer.validate_url
_detect_service            = _svc_layer.detect_service
_default_quality_for_service = _svc_layer.default_quality
_engine_for_service        = _svc_layer.engine_for_svc

# ── Broadcast ─────────────────────────────────────────────────────────────────
from ripster.security import GUEST_BLOCKED_WS_TYPES as _GUEST_BLOCKED_TYPES
from ripster.security import GUEST_BLOCKED_PATHS    as _GUEST_BLOCKED


from ripster.ws_broker import WebSocketBroker
_ws_broker = WebSocketBroker()


def _ws_dead(ws) -> None:
    """Called by the broker when a client's socket dies — drop our bookkeeping."""
    ws_clients.discard(ws)
    _ws_guest_sids.pop(ws, None)


_ws_broker.set_dead_handler(_ws_dead)


# ── Stdout tee → WS broadcast ──────────────────────────────────────────────
# Every print() in the codebase gets mirrored to the in-UI console (with the
# `[service]` prefix extracted so the frontend can filter by service). The
# original stdout still receives the line, so run.bat keeps printing as before.
import sys as _sys, collections as _collections, re as _re

_LOG_TAIL: "_collections.deque[tuple[str, str]]" = _collections.deque(maxlen=4000)
# Persistent ring buffer for the admin console — never popped, only appended.
# _LOG_TAIL is a transient queue that _stdout_pump drains every 0.4s, so it
# can't be used as a "show me the last N lines" source. _LOG_HISTORY is the
# authoritative read-only view for /api/admin/system-log.
_LOG_HISTORY: "_collections.deque[tuple[float, str, str]]" = _collections.deque(maxlen=4000)
_SVC_RE = _re.compile(r'^\s*\[([a-z][a-z0-9:_-]+)\]', _re.IGNORECASE)
_KNOWN_SERVICES = (
    "apple", "qobuz", "tidal", "deezer", "spotify", "soundcloud",
    "bbc", "lucida", "orpheus", "amd", "gamdl", "zhaarey", "beatport",
    "wrapper", "watchlist", "release", "guest", "stats", "tunnel",
    "ngrok", "tokens", "startup", "queue", "meta", "isrc", "csrf",
)


def _svc_of(line: str) -> str:
    m = _SVC_RE.match(line)
    if not m:
        return ""
    name = m.group(1).lower().split(":", 1)[0]
    return name if name in _KNOWN_SERVICES else ""


# ── Persistent console log → logs/console.log (rotating) ──────────────────────
# broadcast() is the single chokepoint EVERY console line passes through (stdout
# tee → _stdout_pump → broadcast, plus direct ilog/log/engine-event broadcasts),
# so tee-ing "log" messages to disk here captures the FULL real-time console
# stream. This is what a remote tester sends us — the in-memory buffers (_LOG_TAIL,
# the browser ring buffer) die with the window, so without this we're blind to
# anything that scrolled off or happened before a screenshot.
import logging as _logging
from logging.handlers import RotatingFileHandler as _RotatingFileHandler

_CONSOLE_LOG_PATH = BASE_DIR / "logs" / "console.log"
_console_file_logger = None


def _init_console_file_logger():
    global _console_file_logger
    try:
        (BASE_DIR / "logs").mkdir(parents=True, exist_ok=True)
        lg = _logging.getLogger("ripster.console")
        lg.setLevel(_logging.DEBUG)
        lg.propagate = False
        if not lg.handlers:
            h = _RotatingFileHandler(_CONSOLE_LOG_PATH, maxBytes=5_000_000,
                                     backupCount=3, encoding="utf-8")
            h.setFormatter(_logging.Formatter("%(message)s"))
            lg.addHandler(h)
        _console_file_logger = lg
        lg.info(f"\n===== console log opened {datetime.now():%Y-%m-%d %H:%M:%S} "
                f"| Ripster {RELEASE_VERSION} =====")
    except Exception as e:                                    # noqa: BLE001
        print(f"[startup] console file logger init failed: {e}", flush=True)


_init_console_file_logger()


def _console_file_write(msg: dict) -> None:
    """Append one console line to logs/console.log. Best-effort, never raises."""
    lg = _console_file_logger
    if lg is None:
        return
    try:
        ts   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        svc  = msg.get("service") or ""
        lvl  = (msg.get("level") or "info").upper()
        text = msg.get("text") or msg.get("msg") or ""
        lg.info(f"{ts} {lvl:<5} {('['+svc+'] ') if svc else ''}{text}")
    except Exception:
        pass


class _StdoutTee:
    """Mirror stdout writes line-by-line into a bounded deque AND the real tty."""
    def __init__(self, original):
        self._orig = original
        self._buf  = ""

    def write(self, s):
        if self._orig is not None:
            try: self._orig.write(s)
            except Exception: pass
        if not isinstance(s, str):
            return len(s) if s is not None else 0
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            line = line.rstrip("\r")
            if line.strip():
                svc = _svc_of(line)
                _LOG_TAIL.append((svc, line))
                _LOG_HISTORY.append((time.time(), svc, line))
        return len(s)

    def flush(self):
        try:
            if self._orig is not None: self._orig.flush()
        except Exception: pass

    def isatty(self):
        return bool(self._orig and getattr(self._orig, "isatty", lambda: False)())

    def __getattr__(self, name):
        # Pass-through unknown attribute lookups to the real stdout — uvicorn /
        # libraries occasionally peek at fileno(), buffer, encoding etc.
        return getattr(self._orig, name)


if not isinstance(_sys.stdout, _StdoutTee):
    _sys.stdout = _StdoutTee(_sys.stdout)


async def _stdout_pump():
    """Drain captured stdout lines and broadcast each to all WS clients."""
    while True:
        try:
            await asyncio.sleep(0.4)
            n = 0
            while _LOG_TAIL and n < 120:        # cap per-tick burst
                svc, text = _LOG_TAIL.popleft()
                lvl = "error" if "error" in text.lower() or "✗" in text \
                      else "warn" if "warn" in text.lower() or "⚠" in text \
                      else "info"
                try:
                    await broadcast({
                        "type":    "log",
                        "level":   lvl,
                        "service": svc,
                        "text":    text,
                    })
                except Exception:
                    pass
                n += 1
        except asyncio.CancelledError:
            return
        except Exception:
            # Never let the pump die — it's the only path stdout→UI.
            await asyncio.sleep(1.0)


async def _ws_heartbeat():
    """Emit a tiny periodic ping to every WS client.

    The client's health watchdog (static/js/app.js) treats an OPEN socket that
    has been SILENT for >45 s as half-open and force-cycles it. When the app is
    idle — or stuck in one of AMD's long silent decrypt/tagging windows — no
    events flow, so without this the status flaps Connected↔Disconnected every
    ~45 s (and the queue UI churns mid-download). A 20 s ping keeps the
    watchdog's last-message clock fresh on healthy sockets, so it only ever
    fires on a genuinely dead connection. `type: "ping"` is ignored by the
    client switch (and not persisted/logged by broadcast)."""
    while True:
        try:
            await asyncio.sleep(20)
            if _ws_broker.clients:
                await broadcast({"type": "ping"})
        except asyncio.CancelledError:
            return
        except Exception:
            await asyncio.sleep(5)


async def broadcast(msg: dict):
    """Fan a message out to every WS client through the broker. Per-client
    queues mean a slow client never blocks the others — see ws_broker.py."""
    if msg.get("type") == "log":
        if "text" not in msg and "msg" in msg:
            msg = {**msg, "text": msg["msg"]}
        # SECRET-REDACT at the single chokepoint → covers the live WS console, the
        # persistent console.log AND telemetry in one place. Engines (e.g. streamrip
        # DEBUG) can echo a Qobuz user_auth_token / ARL / bearer into a log line;
        # redact() strips them here so they never reach a screen, disk or owner.
        try:
            from ripster import telemetry as _tlm
            msg = {k: (_tlm.redact(v) if k in ("text", "msg") and isinstance(v, str) else v)
                   for k, v in msg.items()}
        except Exception:
            pass
        _console_file_write(msg)               # persist the full stream to disk
        try:                                   # forward warn/error to the owner (tester builds)
            from ripster import telemetry as _tlm
            _tlm.record(msg.get("level", "info"), msg.get("text", ""))
        except Exception:
            pass
    if msg.get("type") == "queue_update":
        save_pending_queue()
    for ws in _ws_broker.clients:
        _ws_broker.enqueue(ws, msg)


async def log(text: str, level: str = "info"):
    ts = datetime.now().strftime("%H:%M:%S")
    await broadcast({"type": "log", "text": f"[{ts}] {text}", "level": level})


def queue_snapshot():
    return [{k: v for k, v in t.items() if k != "log"} for t in queue]


# ── FastAPI ────────────────────────────────────────────────────────────────────
_STATIC_DIR = BASE_DIR / "static"
_INDEX_HTML = _STATIC_DIR / "index.html"
_HTML_FALLBACK = (
    "<!DOCTYPE html><html><body style=\"font-family:system-ui;padding:40px;max-width:600px;margin:0 auto\">"
    "<h1 style=\"color:#fc3c44\">Ripster UI</h1>"
    "<p>Frontend file <code>static/index.html</code> is missing.</p>"
    "</body></html>"
)

def _load_html_page() -> str:
    if _INDEX_HTML.exists():
        try:
            return _INDEX_HTML.read_text(encoding="utf-8")
        except Exception as _e:
            print(f"[html] cannot read {_INDEX_HTML}: {_e}", flush=True)
    return _HTML_FALLBACK

HTML_PAGE = _load_html_page()


async def _startup_sync_orpheus() -> None:
    try:
        from ripster.routes.setup import _sync_orpheus_username
        await _sync_orpheus_username()
        print("[startup] OrpheusDL username sync done", flush=True)
    except Exception as _e:
        print(f"[startup] OrpheusDL username sync skipped: {_e}", flush=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    _metadata.install(
        config, broadcast, save_config, _detect_service,
        spotify_token_getter=_spotify.get_access_token,
        queue_snapshot_fn=queue_snapshot,
    )
    _setup.install(config, broadcast, save_config, BASE_DIR, IS_WINDOWS)
    _amd.install(config, broadcast, save_config, BASE_DIR, IS_WINDOWS)

    saved = load_pending_queue(QUEUE_FILE)
    if saved:
        queue.extend(saved)
        # deemix-style: the list keeps finished tasks too (for the record), so only
        # spin up the runner when there's actually something queued to resume —
        # a list of only-done tasks should restore silently, not restart the queue.
        _n_resume = sum(1 for t in saved if t.get("status") == "queued")
        print(f"[queue] restored {len(saved)} task(s) from previous session "
              f"({_n_resume} to resume)", flush=True)
        if _n_resume:
            _qs.start()
            asyncio.create_task(process_queue())

    asyncio.create_task(_startup_sync_orpheus())
    asyncio.create_task(_soundcloud_routes._prewarm_client_id())
    # Start the stdout→WS pump so every print() reaches the UI console.
    asyncio.create_task(_stdout_pump())
    # Periodic WS heartbeat so the client watchdog doesn't false-trip on an idle
    # or silently-downloading socket (status flapping Connected↔Disconnected).
    asyncio.create_task(_ws_heartbeat())
    # Diagnostics telemetry forwarder (tester builds → owner). No-op when disabled.
    try:
        asyncio.create_task(_telemetry.run_forwarder())
    except Exception as _e:
        print(f"[telemetry] forwarder wiring error: {_e}", flush=True)

    # Time-based disk cleanup: delete finished release folders N minutes after
    # completion (auto-delete-minutes config, 0 = off). Keeps the disk from
    # filling up — cache-channel copies on Telegram are unaffected.
    try:
        from ripster import auto_cleanup as _auto_cleanup
        asyncio.create_task(_auto_cleanup.run(config))
    except Exception as _e:
        print(f"[autodelete] wiring error: {_e}", flush=True)

    # Autonomous Spotify Bearer keeper: mints a fresh web-player token from the
    # durable librespot blob when the browser extension is idle (overnight), so
    # OGG downloads stop dying with "token expired (401)". No-op without a blob.
    try:
        from ripster import spotify_token_keeper as _sp_keeper
        asyncio.create_task(_sp_keeper.run(config, BASE_DIR))
    except Exception as _e:
        print(f"[sp-keeper] wiring error: {_e}", flush=True)


    yield


app = FastAPI(title="Ripster", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:7799", "http://127.0.0.1:7799"],
    allow_methods=["*"],
    allow_headers=["*"],
)
if _STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

from ripster import auth as _app_auth
_app_auth.install(app, config, save_config)

# Public build has no guest mode — a no-op stub so the few guest-aware call sites
# (auth checker, /ws fan-out, /api/services/status) all behave as "local owner only".
class _NoGuestManager:
    def is_guest_request(self, *_a, **_k): return False
    def active_session_count(self): return 0
    def get_session_id_from_request(self, *_a, **_k): return ""
    def get_session(self, *_a, **_k): return None
    def get_effective_tokens(self, *_a, **_k): return None
_guest_mgr = _NoGuestManager()
_app_auth.set_guest_checker(lambda r: _guest_mgr.is_guest_request(r))
_app_auth.add_public_path("/api/session-info")
_app_auth.add_public_path("/api/ping")


# Unauthenticated liveness/identity probe. The launcher hits this to tell OUR
# server apart from a foreign app squatting the port (so it can pick a free port
# instead of opening a window onto the wrong app). Returns no secrets.
@app.get("/api/ping")
async def _ping():
    return {"app": "ripster", "version": RELEASE_VERSION}


@app.get("/api/logs/download")
async def _logs_download():
    """Owner-gated: bundle the diagnostic logs into one zip the user can hand us.
    NOT a public path (logs may contain URLs/tokens), so the app's auth gate
    requires a valid session. Skips the huge bot.log; includes console + errors +
    launcher + rotated console backups."""
    import io as _io, zipfile as _zip
    logs_dir = BASE_DIR / "logs"
    wanted = ["console.log", "console.log.1", "console.log.2", "console.log.3",
              "errors.log", "launcher.log", "app_err.log"]
    buf = _io.BytesIO()
    with _zip.ZipFile(buf, "w", _zip.ZIP_DEFLATED) as zf:
        # errors.log lives at the repo root (runner.py writes it there), not logs/
        for p in [BASE_DIR / "errors.log"] + [logs_dir / n for n in wanted]:
            try:
                if p.exists() and p.stat().st_size > 0:
                    zf.write(p, p.name)
            except Exception:
                pass
        zf.writestr("_meta.txt",
                    f"Ripster {RELEASE_VERSION}\ngenerated {datetime.now():%Y-%m-%d %H:%M:%S}\n")
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="ripster-logs-{stamp}.zip"'},
    )


@app.middleware("http")
async def _no_cache_js(request: Request, call_next):
    response = await call_next(request)
    path = request.url.path
    ctype = response.headers.get("content-type", "")
    # Never cache the JS bundles OR the HTML document. index.html itself is NOT
    # ?v-busted, so if the browser caches it the asset version bumps inside it
    # never take effect — the stale HTML keeps requesting the OLD ?v= and fixes
    # appear "not applied" until a manual hard-reload. Forcing revalidation on the
    # document means a normal reload always pulls fresh CSS/JS.
    if path.startswith("/static/js/") and path.endswith(".js"):
        # JS bundles are cache-busted via ?v=N in index.html, so they're safe to
        # cache hard — this makes a normal reload INSTANT (no ~630KB re-download
        # of all scripts every refresh, which `no-store` was forcing). A version
        # bump changes the URL → the new file is fetched; a forgotten bump
        # self-heals within the hour.
        response.headers["Cache-Control"] = "public, max-age=3600"
    elif path == "/" or "text/html" in ctype:
        # The HTML document is NOT ?v-busted, so it MUST revalidate or stale HTML
        # keeps requesting old ?v= and fixes appear "not applied".
        response.headers["Cache-Control"] = "no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
    return response


@app.middleware("http")
async def _guest_guard(request: Request, call_next):
    path = request.url.path
    if _guest_mgr.is_guest_request(request):
        if any(path == p or path.startswith(p) for p in _GUEST_BLOCKED):
            return JSONResponse(
                {"error": "forbidden", "detail": "Not available for guests"},
                status_code=403,
            )
        if request.method in ("POST", "PUT", "PATCH", "DELETE") and path == "/api/config":
            return JSONResponse({"error": "forbidden"}, status_code=403)
    return await call_next(request)


# ── Runner ─────────────────────────────────────────────────────────────────────
import ripster.runner as _runner
from ripster.app_context import AppContext

_ctx = AppContext(
    config            = config,
    save_config       = save_config,
    queue             = queue,
    queue_manager     = _qs,
    download_history  = download_history,
    save_history      = save_history,
    broadcast         = broadcast,
    detect_service    = _detect_service,
    base_dir          = BASE_DIR,
    is_windows        = IS_WINDOWS,
    watchlist         = watchlist,
    save_watchlist    = save_watchlist,
    owner_auth_fn     = _app_auth.verify_session_cookie,
    fetch_meta        = _metadata.fetch_meta_any,
    get_engine        = get_engine,
    get_qualities     = get_qualities,
    auto_fetch_bearer = _metadata.auto_fetch_bearer,
    load_html         = _load_html_page,
    app_info          = {"version": RELEASE_VERSION, "build": APP_BUILD,
                         "name": "Ripster", "authors": APP_AUTHORS, "repo": APP_REPO},
    queue_snapshot    = queue_snapshot,
    validate_url      = _validate_url,
    enrich_meta       = _metadata.enrich_meta,
    default_quality   = _default_quality_for_service,
    engine_for_svc    = _engine_for_service,
    process_queue     = None,
)
_runner.install(_ctx)
process_queue      = _runner.process_queue
_ctx.process_queue = process_queue

# ── Route modules ──────────────────────────────────────────────────────────────
from ripster.routes import history    as _history
from ripster.routes import spotify    as _spotify
from ripster.routes import discovery  as _discovery
from ripster.routes import releases   as _releases
from ripster.routes import setup      as _setup_routes
from ripster.routes import streaming  as _streaming
from ripster.routes import auth       as _auth_routes
from ripster.routes import core       as _core_routes
from ripster.routes import queue      as _queue_routes
from ripster.routes import apple_auth as _apple_auth
from ripster.routes import bbc        as _bbc
from ripster.routes import spectrogram as _spectrogram
from ripster.routes import isrc        as _isrc
from ripster.routes import download    as _download_routes
from ripster.routes import beatport    as _beatport_routes
from ripster.routes import soundcloud  as _soundcloud_routes
from ripster.routes import ripster_coder as _coder_routes
from ripster.routes import telemetry    as _telemetry_routes
from ripster import telemetry as _telemetry
from ripster import tl1001 as _tl1001

_tl1001.install(config)          # 1001Tracklists source (login optional, disk-cached)
_history.install(app, _ctx)
_discovery.install(app, _ctx)
_spotify.install(app, _ctx)
_releases.install(app, _ctx)
_setup_routes.install(app, _ctx)
_streaming.install(app, _ctx)
_auth_routes.install(app, _ctx)
_core_routes.install(app, _ctx)
_redact_config = _core_routes._redact_config
_queue_routes.install(app, _ctx)
_apple_auth.install(app, _ctx)
_bbc.install(app, _ctx)
_spectrogram.install(app)
_isrc.install(app, _ctx)
_download_routes.install(app, _ctx)
_beatport_routes.install(app, _ctx)
_soundcloud_routes.install(app, _ctx)
_coder_routes.install(app, _ctx)
_telemetry_routes.install(app, _ctx)
# Diagnostics telemetry: this (tester) build forwards warn/error to the owner.
# configure() mints an anon instance id; ingest endpoint is PUBLIC (token-gated).
config["_release_version"] = RELEASE_VERSION
_telemetry.configure(config, save_config, BASE_DIR)
_app_auth.add_public_path("/api/telemetry/ingest")

# ── Hot-restart ────────────────────────────────────────────────────────────────
def _spawn_restart(delay: float = 0.4) -> None:
    """Restart the server. Shared by the manual restart endpoint and the idle
    watcher.

    Under the standalone launcher (Ripster.exe / ripster_launcher sets
    RIPSTER_LAUNCHER=1) the launcher SUPERVISES us — we must ONLY exit cleanly and
    let it respawn the server windowless. Spawning our own replacement here would
    duel the launcher's respawn over the port → the "cmd windows keep popping"
    loop. Standalone (dev `python app.py`) we spawn a fresh detached replacement."""
    import subprocess, threading
    def _do_restart():
        time.sleep(delay)   # let any in-flight HTTP response arrive first
        if os.environ.get("RIPSTER_LAUNCHER") == "1":
            os._exit(0)     # launcher respawns us (single owner, no console flash)
            return
        restart_env = {**os.environ, "RIPSTER_IS_RESTART": "1"}
        subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve())] + sys.argv[1:],
            cwd=str(BASE_DIR),
            env=restart_env,
            creationflags=(subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
                           | getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if IS_WINDOWS else 0,
        )
        os._exit(0)
    threading.Thread(target=_do_restart, daemon=True).start()


def _check_owner(request: Request) -> bool:
    from ripster.auth import verify_session_cookie
    from ripster.auth import is_enabled as _auth_enabled
    if verify_session_cookie(request.cookies.get("ripster-session", "")):
        return True
    try:
        return not _auth_enabled()
    except Exception:
        return False


@app.post("/api/admin/restart")
async def admin_restart(request: Request):
    if not _check_owner(request):
        raise HTTPException(403, "Not authorized")
    _spawn_restart()
    return {"ok": True}



# ── Services status (used by search selector to hide unconfigured services) ───
@app.get("/api/services/status")
async def services_status(request: Request):
    """Returns which services have credentials configured — no values exposed."""
    eff = {}
    try:
        sid = _guest_mgr.get_session_id_from_request(request)
        if sid:
            eff = _guest_mgr.get_effective_tokens(sid, config) or {}
    except Exception:
        pass

    def _has(key: str) -> bool:
        return bool((eff.get(key) or config.get(key) or "").strip())

    return {
        "apple":      True,   # iTunes public API — always searchable
        "qobuz":      _has("qobuz-auth-token"),
        "deezer":     _has("deezer-arl"),
        "tidal":      _has("tidal-token"),
        "spotify":    _has("spotify-client-id"),
        "beatport":   _has("beatport-username"),
        "soundcloud": _has("soundcloud-oauth-token"),
        "yandex":     _has("yandex-token"),
    }




# ── WebSocket ──────────────────────────────────────────────────────────────────
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    if not _app_auth.ws_allowed(ws):
        await ws.close(code=1008)
        return
    await ws.accept()
    ws_clients.add(ws)

    _ws_guest_sid = ""
    _ws_is_guest  = False
    _ws_stat_row = 0

    def _ws_queue_snapshot():
        return queue_snapshot()

    try:
        init_payload = {
            "type":    "init",
            "queue":   _ws_queue_snapshot(),
            "running": _qs.is_running,
            "paused":  _qs.is_paused,
        }
        if _ws_is_guest:
            init_payload["config"] = {
                "engine":     config.get("engine", "zhaarey"),
                "quality":    config.get("quality", "alac"),
                "storefront": config.get("storefront", "gb"),
            }
        else:
            init_payload["config"] = _redact_config(config)
        await ws.send_json(init_payload)
        _ws_broker.register(ws)   # ongoing fan-out goes through the broker
        while True:
            data = await ws.receive_json()
            if data.get("type") == "token_update" and not _ws_is_guest:
                b = data.get("bearer")
                m = data.get("mut")
                if isinstance(b, str) and not b.startswith("••"):
                    config["authorization-token"] = b
                if isinstance(m, str) and not m.startswith("••"):
                    config["media-user-token"] = m
                save_config(config)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"[ws] client dropped: {type(e).__name__}: {e}", flush=True)
    finally:
        _ws_broker.unregister(ws)
        ws_clients.discard(ws)


def _takeover_stale_server(host: str, port: int) -> bool:
    """Make sure THIS interpreter can bind `port`, taking it over from a STALE
    Ripster if needed.

    The frozen Ripster.exe launcher (a pre-built binary, NOT shipped by self-update)
    just *attaches* to whatever already answers on the port — so after an overlay
    update the user kept seeing the OLD version: the previous server lingered in the
    background (a detached restart successor, or a browser-fallback close that never
    terminated it) and the launcher reopened onto it. This server-side guard fixes
    that for installs that already have the old exe:

      • port free                       → bind (normal first start)
      • held by a foreign app           → leave it (uvicorn fails loudly, as before)
      • held by an OLDER Ripster        → kill it, wait for the port, then bind
                                          (the just-overlaid newer code wins)
      • held by a SAME/NEWER Ripster    → stand down (a sibling won the restart race)

    Returns True if the caller should proceed to bind, False if it should exit."""
    import socket, time, json, subprocess
    from urllib.request import urlopen
    if host not in ("127.0.0.1", "localhost"):
        return True                       # container 0.0.0.0 etc. — don't interfere

    def _can_bind() -> bool:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port)); return True
        except OSError:
            return False
        finally:
            s.close()

    if _can_bind():
        return True

    try:
        with urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=2) as r:
            j = json.loads(r.read() or b"{}")
    except Exception:
        return True                       # not answering / unknown — let uvicorn try
    if j.get("app") != "ripster":
        return True                       # foreign app on our port — don't touch it

    other = str(j.get("version") or "")
    try:
        from ripster.updater import is_newer
        newer_or_same = (other == RELEASE_VERSION) or is_newer(other, RELEASE_VERSION)
    except Exception:
        newer_or_same = (other == RELEASE_VERSION)
    if newer_or_same:
        print(f"  ↪ Ripster v{other} already live on {port} — stepping aside.", flush=True)
        return False                      # a same/newer sibling owns it → exit cleanly

    # An OLDER Ripster is squatting the port (stale after an update). Kill whatever
    # holds it so this newer code can bind.
    print(f"  ⟳ Replacing stale Ripster v{other} on {port} with v{RELEASE_VERSION}…", flush=True)
    _CNW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        if os.name == "nt":
            out = subprocess.run(["netstat", "-ano", "-p", "tcp"],
                                 capture_output=True, text=True, creationflags=_CNW).stdout
            pids = set()
            for line in out.splitlines():
                u = line.upper()
                if f"127.0.0.1:{port} " in line and "LISTENING" in u:
                    parts = line.split()
                    if parts and parts[-1].isdigit():
                        pids.add(parts[-1])
            for pid in pids:
                subprocess.run(["taskkill", "/F", "/T", "/PID", pid],
                               capture_output=True, creationflags=_CNW)
        else:
            subprocess.run(["bash", "-c", f"fuser -k {port}/tcp"], capture_output=True)
    except Exception as e:                                            # noqa: BLE001
        print(f"  ⚠ could not kill stale server: {e}", flush=True)
    for _ in range(40):                   # up to ~10 s for the port to free
        if _can_bind():
            return True
        time.sleep(0.25)
    return True                           # bind anyway; uvicorn will report if still busy


# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Host/port are env-overridable (RIPSTER_HOST / RIPSTER_PORT) so a container
    # binds 0.0.0.0 without a code change; CLI --host/--port still take priority.
    try:
        port = int(os.environ.get("RIPSTER_PORT") or 7799)
    except (TypeError, ValueError):
        port = 7799
    host = os.environ.get("RIPSTER_HOST") or "127.0.0.1"
    for i, arg in enumerate(sys.argv):
        if arg == "--host" and i + 1 < len(sys.argv):
            host = sys.argv[i + 1]
        if arg == "--port" and i + 1 < len(sys.argv):
            try:
                port = int(sys.argv[i + 1])
            except ValueError:
                pass

    # Refuse a non-localhost bind without a password — EXCEPT inside a container,
    # where 0.0.0.0 is the isolated container interface and the operator controls
    # exposure via `-p 127.0.0.1:7799:7799`. A password is still strongly advised
    # (the warning below still prints).
    _in_docker = os.path.exists("/.dockerenv")
    if host != "127.0.0.1" and not _app_auth.is_enabled() and not _in_docker:
        print("─" * 68, file=sys.stderr)
        print(f"  Refusing to bind to {host} without an app password set.", file=sys.stderr)
        print("  Open Ripster locally first, go to Settings → Security,", file=sys.stderr)
        print("  set a password, then restart with --host " + host + ".", file=sys.stderr)
        print("─" * 68, file=sys.stderr)
        sys.exit(1)

    print("─" * 52)
    print("  🎵  Ripster")
    print(f"  ➜   http://{host}:{port}")
    if _app_auth.is_enabled():
        print("  🔒  Password protection: ON")
    elif host != "127.0.0.1":
        print("  ⚠️  Open to LAN without a password — BAD idea.")
    print("─" * 52)
    Path(config.get("save-path", "downloads")).mkdir(parents=True, exist_ok=True)
    save_config(config)
    if not _takeover_stale_server(host, port):
        sys.exit(0)                       # a same/newer Ripster already owns the port
    uvicorn.run(app, host=host, port=port, log_level="warning")
