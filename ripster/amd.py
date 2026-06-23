"""ripster.amd — AMD v2 (AppleMusicDecrypt) + Docker wrapper management.

Public surface:
    install(cfg, broadcast_fn, save_config_fn, base_dir, is_windows)

    # AMD helpers
    get_amd_dir()              — resolve the AMD installation directory
    amd_wrapper_status(inst, secure) — gRPC Status() → dict
    write_amd_config(amd_dir)  — write config.toml for AMD v2
    clone_amd()                — git-clone AMD v2
    patch_amd_for_headless(d)  — patch prompt_toolkit out of AMD sources
    install_amd_deps()         — pip-install AMD Python dependencies

    # Docker wrapper
    check_wrapper_running()    — TCP check on decrypt port
    check_docker_installed()   — (bool, path_or_error)
    start_wrapper_docker()     — pull + start wrapper container
    stop_wrapper_docker()      — stop wrapper container
    pull_wrapper_image()       — docker pull wrapper image

Private names follow the convention used in app.py so aliases are one-liners.
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from typing import Optional

_cfg:          dict  = {}
_broadcast            = None
_save_config          = None
_base_dir:     Path  = Path(".")
_is_windows:   bool  = False

WRAPPER_CONTAINER_NAME = "amd-wrapper"
WRAPPER_LOCAL_IMAGE    = "ripster-wrapper"
_wrapper_proc:       Optional[asyncio.subprocess.Process] = None
_wrapper_log_task:   Optional[asyncio.Task]               = None
_wrapper_direct_proc: Optional[asyncio.subprocess.Process] = None
# Interactive login process (docker run -i) whose STDIN receives the 2FA code.
_wrapper_login_proc: Optional[asyncio.subprocess.Process] = None


def _redact(text: str) -> str:
    """Hide the Apple ID password from any log/broadcast line.
    `-L email:password` → `-L email:***`. The password leaked into the on-screen
    log before because the full `docker run … -L user:pass …` command was echoed."""
    import re as _re
    return _re.sub(r"(-L\s+[^\s:]+:)[^\s\"]+", r"\1***", text or "")


# 2FA prompt markers the wrapper actually prints (the real prompt is "2FA code:";
# older builds say "Enter your 2FA code" / "Waiting for input").
def _is_2fa_prompt(low: str) -> bool:
    return ("2fa code" in low or "enter your 2fa" in low
            or "waiting for input" in low or "enter the code" in low)


def _is_login_failed(text: str) -> bool:
    low = text.lower()
    return ("[!] login failed" in low or "incorrectly entered more than once" in low
            or "forgot your password" in low)


async def submit_2fa(code: str) -> bool:
    """Deliver the user-entered 2FA code where THIS wrapper build reads it: a
    file at <rootfs>/data/com.apple.android.music/files/2fa.txt, written with NO
    trailing newline (the wrapper prints exactly:
      "Enter your 2FA code into …/files/2fa.txt"
      "Example: echo -n 114514 > …/files/2fa.txt").
    Also writes a couple of legacy locations as a harmless fallback."""
    code = (code or "").strip()
    if not code:
        return False
    ok = False
    try:
        rootfs = _rootfs_data(_wrapper_mode())
        deep = rootfs / "data" / "com.apple.android.music" / "files"
        deep.mkdir(parents=True, exist_ok=True)
        # echo -n → NO newline (a trailing \n makes the code "072610\n" → rejected)
        (deep / "2fa.txt").write_text(code)
        ok = True
        # legacy fallbacks (older builds)
        for fn in ("code.txt", "2fa.txt"):
            try:
                (rootfs / fn).write_text(code)
            except Exception:
                pass
    except Exception as e:
        print(f"[wrapper] submit_2fa write failed: {e}", flush=True)
    return ok


def install(
    cfg:            dict,
    broadcast_fn,
    save_config_fn,
    base_dir:       Path,
    is_windows:     bool,
) -> None:
    """Wire globals. Call once at app startup."""
    global _cfg, _broadcast, _save_config, _base_dir, _is_windows
    _cfg         = cfg
    _broadcast   = broadcast_fn
    _save_config = save_config_fn
    _base_dir    = base_dir
    _is_windows  = is_windows


# ─── AMD directory / config ───────────────────────────────────────────────────

def get_amd_dir() -> Path:
    d = _cfg.get("amd-dir", "").strip()
    if d and (Path(d) / "main.py").exists():
        return Path(d)
    return _base_dir / "AppleMusicDecrypt"


def write_amd_config(amd_dir: Path, codec: str = "alac") -> None:
    """Write config.toml for AMD v2 from current settings."""
    cfg        = _cfg
    base_out   = str(Path(cfg.get("save-path", str(_base_dir / "downloads")))).replace("\\", "/")

    if codec in ("ec3", "ac3", "atmos"):
        out = str(Path(cfg.get("atmos-save-folder") or cfg.get("atmos-path") or
                       (base_out + "/Atmos"))).replace("\\", "/")
    elif codec == "aac":
        out = str(Path(cfg.get("aac-save-folder") or cfg.get("aac-path") or
                       (base_out + "/AAC"))).replace("\\", "/")
    else:
        out = str(Path(cfg.get("alac-save-folder") or base_out)).replace("\\", "/")

    url_secure = str(cfg.get("amd-instance-secure", True)).lower()
    lang       = cfg.get("language", "en-US")
    parallel   = int(cfg.get("amd-parallel", 8))
    # The user-facing toggle is the global "Save .lrc file" (`save-lrc-file`);
    # `amd-save-lyrics` is a legacy orphan key with NO UI control (defaults True),
    # so reading it alone meant AMD kept writing .lrc files even with the toggle
    # OFF. Honour the global toggle as the authority, fall back to the legacy key.
    save_lrc   = str(bool(cfg.get("save-lrc-file", cfg.get("amd-save-lyrics", True)))).lower()
    lrc_fmt    = cfg.get("lrc-format", cfg.get("amd-lyrics-format", "lrc"))
    save_cov   = str(cfg.get("save-cover-to-folder", True)).lower()
    cov_fmt    = cfg.get("cover-format", "jpg")
    codec_alt  = str(cfg.get("amd-codec-alt", True)).lower()
    instance   = cfg.get("amd-instance-url", "wm.wol.moe")

    toml = f'''version = "0.0.10"

[instance]
url = "{instance}"
secure = {url_secure}

[localInstance]
enable = false
enableHardwareAcceleration = false
hardwareAccelerator = ""
memorySize = "512M"
cpuModel = "Cascadelake-Server-v5"
showWindow = false
startArgs = "-host 0.0.0.0 -port 32767 -debug"

[region]
language = "{lang}"
languageNotExistWarning = false

[download]
proxy = ""
parallelNum = {parallel}
maxRunningTasks = 128
appleCDNIP = ""
codecAlternative = {codec_alt}
codecPriority = ["alac", "ec3", "ac3", "aac"]
atmosConventToM4a = true
failedSongNotPassIntegrityCheck = false
audioInfoFormat = ""
songNameFormat = "{{disk}}-{{tracknum:02d}} {{title}}"
dirPathFormat = "{out}/{{album_artist}}/{{album}}"
playlistDirPathFormat = "{out}/playlists/{{playlistName}}"
playlistSongNameFormat = "{{playlistSongIndex:02d}}. {{artist}} - {{title}}"
saveLyrics = {save_lrc}
lyricsFormat = "{lrc_fmt}"
lyricsExtra = ["translation", "pronunciation"]
saveCover = {save_cov}
coverFormat = "{cov_fmt}"
# Embedded (in-audio) cover pinned to 1000×1000 across all services by request —
# was 5000×5000, which bloated every Apple track's tags.
coverSize = "1000x1000"
maxSampleRate = 192000
maxBitDepth = 24
afterDownloaded = ""
retryTime = 8
maxWaitTime = 30

[metadata]
embedMetadata = ["title", "artist", "album", "album_artist", "composer", "album_created",
    "genre", "created", "tracknum", "disk", "lyrics", "cover", "copyright",
    "record_company", "upc", "isrc", "rtng", "song_id", "album_id", "artist_id"]
'''
    (amd_dir / "config.toml").write_text(toml, encoding="utf-8")


async def amd_wrapper_status(instance: str, secure: bool = True) -> dict:
    """Call WrapperManagerService.Status() and return a plain dict.

    Returns {"ready": bool, "client_count": int, "regions": [...]}
    or {"error": "reason"} if the RPC fails.
    """
    try:
        import grpc as _grpc
    except ImportError:
        return {"error": "grpcio not installed"}

    amd_dir  = get_amd_dir()
    pb2_path = amd_dir / "src" / "grpc" / "manager_pb2.py"
    if not pb2_path.exists():
        return {"error": "AMD not installed (manager_pb2.py missing)"}

    try:
        _amd_src = str(amd_dir)
        _added   = _amd_src not in sys.path
        if _added:
            sys.path.insert(0, _amd_src)
        try:
            from src.grpc.manager_pb2_grpc import WrapperManagerServiceStub
            from google.protobuf.empty_pb2 import Empty
        finally:
            if _added and _amd_src in sys.path:
                sys.path.remove(_amd_src)

        creds   = _grpc.ssl_channel_credentials() if secure else None
        channel = (_grpc.secure_channel(instance, creds)
                   if secure else _grpc.insecure_channel(instance))
        stub    = WrapperManagerServiceStub(channel)
        loop    = asyncio.get_event_loop()
        reply   = await loop.run_in_executor(
            None, lambda: stub.Status(Empty(), timeout=8))
        channel.close()
        return {
            "ready":        reply.data.ready,
            "status":       reply.data.status,
            "client_count": reply.data.client_count,
            "regions":      list(reply.data.regions),
            "code":         reply.header.code,
            "msg":          reply.header.msg,
        }
    except Exception as e:
        msg = str(e)
        m   = re.search(r'details\s*=\s*"([^"]+)"', msg)
        return {"error": m.group(1) if m else msg[:200]}


# ─── AMD install helpers ──────────────────────────────────────────────────────

async def clone_amd() -> bool:
    """Clone AppleMusicDecrypt v2 branch."""
    from ripster import setup as _setup
    amd_dir = get_amd_dir()
    if (amd_dir / "main.py").exists():
        await _setup.ilog(f"  AppleMusicDecrypt already cloned at {amd_dir}", "success")
        return True
    git = shutil.which("git") or "git"
    await _setup.ilog(f"  Cloning AppleMusicDecrypt v2 → {amd_dir}", "info")
    rc, out = await _setup.irun([git, "clone", "--depth=1", "-b", "v2",
                                  "https://github.com/WorldObservationLog/AppleMusicDecrypt.git",
                                  str(amd_dir)])
    if rc == 0:
        await _setup.ilog("  ✓ Cloned OK", "success")
        _cfg["amd-dir"] = str(amd_dir)
        if _save_config:
            _save_config(_cfg)
        await patch_amd_for_headless(amd_dir)
        return True
    await _setup.ilog(f"  ✗ Clone failed: {out[:200]}", "error")
    return False


async def patch_amd_for_headless(amd_dir: Path) -> None:
    """Patch AMD's prompt_toolkit usage so it runs headless as a subprocess."""
    from ripster import setup as _setup

    logger_py = amd_dir / "src" / "logger.py"
    if logger_py.exists():
        src = logger_py.read_text(encoding="utf-8", errors="replace")
        replacement = (
            "# patched for headless\n"
            "def print_formatted_text(msg, end='', **kw): print(msg, end=end, flush=True)\n"
            "def ANSI(s): return s"
        )
        patched = src.replace(
            "from prompt_toolkit import print_formatted_text, ANSI",
            replacement,
        )
        if patched != src:
            logger_py.write_text(patched, encoding="utf-8")
            await _setup.ilog("  ✓ Patched src/logger.py (headless mode)", "success")

    cmd_py = amd_dir / "src" / "cmd.py"
    if cmd_py.exists():
        src = cmd_py.read_text(encoding="utf-8", errors="replace")

        OLD_HANDLE = """    async def handle_command(self):
        session = PromptSession("> ", bottom_toolbar=self.bottom_toolbar, completer=self.completer(),
                                refresh_interval=1)

        while True:
            try:
                command = await session.prompt_async()
                if command.lower() == 'login':
                    await self.login_flow()
                if command.lower() == 'logout':
                    await self.logout_flow()
                elif command.strip() == '':
                    continue
                else:
                    await self.command_parser(command)
            except (EOFError, KeyboardInterrupt):
                self.handle_exit()"""

        NEW_HANDLE = """    async def handle_command(self):
        # patched for headless: read commands from stdin line by line
        import sys as _sys
        loop = _asyncio.get_event_loop()
        while True:
            try:
                line = await loop.run_in_executor(None, _sys.stdin.readline)
                if not line:
                    break
                command = line.strip()
                if not command:
                    continue
                if command.lower() == 'exit':
                    break
                await self.command_parser(command)
            except (EOFError, KeyboardInterrupt):
                break
        # CRITICAL: wait for all background download tasks before returning
        # Otherwise event loop exits and kills in-progress downloads
        try:
            from src.utils import background_tasks as _bt
            while _bt:
                await _asyncio.sleep(0.5)
        except Exception:
            await _asyncio.sleep(5)"""

        patched = src.replace(OLD_HANDLE, NEW_HANDLE)
        patched = patched.replace("from prompt_toolkit.patch_stdout import patch_stdout\n", "")
        patched = re.sub(
            r'^(\s*)with patch_stdout\(\):\s*$',
            lambda m: m.group(1) + 'if True:  # headless',
            patched, flags=re.MULTILINE,
        )
        patched = patched.replace("from prompt_toolkit import PromptSession\n", "")
        patched = patched.replace(
            "from prompt_toolkit import PromptSession, NestedCompleter\n",
            "from prompt_toolkit import NestedCompleter\n",
        )

        if "background_tasks" not in patched:
            await _setup.ilog("  ⚠ src/cmd.py patch incomplete — forcing re-patch", "warn")
        if patched != src or "background_tasks" not in src:
            cmd_py.write_text(patched, encoding="utf-8")
            await _setup.ilog("  ✓ Patched src/cmd.py (headless + background_tasks wait)", "success")
        else:
            await _setup.ilog("  ✓ src/cmd.py already patched", "info")


async def install_amd_deps() -> bool:
    """Install AMD v2 Python dependencies via pip (no poetry needed)."""
    from ripster import setup as _setup
    amd_dir = get_amd_dir()
    reqs = amd_dir / "pyproject.toml"
    if not reqs.exists():
        await _setup.ilog("  pyproject.toml not found — clone first", "error")
        return False
    await _setup.ilog("  Installing AMD dependencies (this may take ~2 min)…", "info")
    deps = [
        "httpx>=0.28", "grpcio>=1.78", "grpcio-tools>=1.78",
        # FLOOR, not a hard pin. AMD's committed _pb2 modules were built against
        # protobuf 6.31.x gencode, but a NEWER runtime loads older gencode fine —
        # so >=6.31.1 keeps the bundle's 6.33.4 (which OrpheusDL/pywidevine need)
        # instead of DOWNGRADING it and breaking those (VersionError). Do not
        # change back to ==6.31.1.
        "protobuf>=6.31.1",
        "pydantic>=2", "loguru", "m3u8", "mutagen", "tenacity",
        "prompt-toolkit", "lxml", "beautifulsoup4", "hishel[async]",
        "tabulate", "anysqlite", "async-lru", "creart", "six",
        "regex",
    ]
    rc, out = await _setup.irun(
        [sys.executable, "-m", "pip", "install", "--break-system-packages", "-q"] + deps)
    if rc != 0:
        await _setup.ilog(f"  ✗ pip install failed: {out[-300:]}", "error")
        return False
    rc2, out2 = await _setup.irun([sys.executable, "-m", "pip", "install",
                                    "--break-system-packages", "-q",
                                    "git+https://github.com/WorldObservationLog/pywidevine"])
    if rc2 != 0:
        await _setup.irun([sys.executable, "-m", "pip", "install",
                           "--break-system-packages", "-q",
                           "pywidevine", "protobuf>=6.31.1"])
        await _setup.ilog("  ⚠ Using standard pywidevine (some features may differ)", "warn")
    else:
        await _setup.ilog("  ✓ pywidevine (AMD fork) installed", "success")
    await _setup.ilog("  ✓ AMD dependencies installed", "success")
    return True


# ─── Mode / path helpers ──────────────────────────────────────────────────────

def _wrapper_mode() -> str:
    return _cfg.get("wrapper-mode", "docker-remote")


def _dist_dir(mode: str) -> Path:
    folder = "non-docker" if mode == "non-docker" else "docker"
    return _base_dir / "dist" / folder


def _rootfs_data(mode: str) -> Path:
    """Путь к rootfs/data для хранения сессии Apple Music."""
    if mode == "docker-remote":
        return _base_dir / "rootfs" / "data"
    return _dist_dir(mode) / "rootfs" / "data"


def _wrapper_bin() -> Path:
    return _dist_dir("non-docker") / "wrapper"


def _to_wsl_path(p: Path) -> str:
    s = str(p).replace("\\", "/")
    if len(s) >= 2 and s[1] == ":":
        return f"/mnt/{s[0].lower()}{s[2:]}"
    return s


def check_wsl_available() -> bool:
    return shutil.which("wsl") is not None


# ─── Docker wrapper management ────────────────────────────────────────────────

def check_docker_installed() -> tuple[bool, str]:
    """Return (installed, path_or_error)."""
    docker = shutil.which("docker")
    if docker:
        try:
            r = subprocess.run(
                [docker, "info", "--format", "{{.ServerVersion}}"],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0:
                return True, docker
            return False, "Docker installed but daemon not running"
        except Exception as e:
            return False, str(e)
    for p in [r"C:\Program Files\Docker\Docker\resources\bin\docker.exe"]:
        if os.path.isfile(p):
            try:
                r = subprocess.run(
                    [p, "info", "--format", "{{.ServerVersion}}"],
                    capture_output=True, text=True, timeout=5,
                )
                if r.returncode == 0:
                    return True, p
            except Exception:
                pass
            return False, "Docker found but daemon not running — start Docker Desktop"
    return False, "Docker not installed"


async def check_wrapper_running() -> bool:
    """TCP connect + peek on decrypt port.

    A port that accepts connections and immediately closes them (EOF) is NOT
    treated as running — that is the 'decryptFragment: EOF' failure mode where
    Docker Desktop is up but no wrapper container is listening on the port.
    """
    addr = _cfg.get("decrypt-port", "127.0.0.1:10020")
    try:
        host, port = addr.rsplit(":", 1)
        with socket.create_connection((host, int(port)), timeout=1) as conn:
            conn.settimeout(0.3)
            try:
                data = conn.recv(4)
                if data == b"":
                    return False   # peer closed immediately — not a real decrypt service
                # got some data → alive
            except socket.timeout:
                pass  # silence means the service is waiting for our request — good
        return True
    except Exception:
        return False


async def _monitor_wrapper_logs() -> None:
    """Stream container logs via 'docker logs -f'; reconnects on restart."""
    ok, docker_path = check_docker_installed()
    if not ok:
        return
    while True:
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                docker_path, "logs", "-f", "--tail", "50", WRAPPER_CONTAINER_NAME,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            _login_tries = 0
            async for raw in proc.stdout:
                text = raw.decode(errors="replace").strip()
                low  = text.lower()
                if text and _broadcast:
                    await _broadcast({"type": "wrapper_log", "text": _redact(text)})
                if _is_2fa_prompt(low):
                    if _broadcast:
                        await _broadcast({"type": "wrapper_2fa_needed"})
                if "code file detected" in low:
                    # The 2FA code is single-use — delete it the moment the wrapper
                    # picks it up, so an internal retry can't re-feed the now-spent
                    # code (that was the endless "Code file detected → restart" loop).
                    try:
                        _rf = _rootfs_data(_wrapper_mode())
                        for _p in (_rf / "data" / "com.apple.android.music" / "files" / "2fa.txt",
                                   _rf / "2fa.txt", _rf / "code.txt"):
                            if _p.exists():
                                _p.unlink()
                    except Exception:
                        pass
                if "[+] logging in" in low:
                    _login_tries += 1
                # Hard-stop on failure OR a retry loop — never spin forever (it
                # risks an Apple lock and spams 2FA).
                if ("[!] login failed" in low
                        or "check the account information" in low
                        or "response type 6" in low
                        or _login_tries >= 3):
                    if _broadcast:
                        await _broadcast({"type": "wrapper_login_failed",
                            "msg": "Логин не удался / зациклился — wrapper остановлен. "
                                   "Скорее всего у аккаунта нет подписки Apple Music, либо неверный пароль."})
                    ok2, dp2 = check_docker_installed()
                    if ok2:
                        subprocess.run([dp2, "rm", "-f", WRAPPER_CONTAINER_NAME],
                                       capture_output=True, timeout=10)
                    return
            await proc.wait()
            await asyncio.sleep(3)
        except asyncio.CancelledError:
            break
        except Exception:
            await asyncio.sleep(5)
        finally:
            if proc and proc.returncode is None:
                try:
                    proc.kill()
                except Exception:
                    pass


def _has_saved_session() -> bool:
    """Return True when rootfs/data already has an Apple Music session.

    If adi.pb (device registration) is present, a session exists and -F
    must NOT be passed — doing so triggers 2FA on every restart.
    """
    rootfs = _rootfs_data(_wrapper_mode())
    adi = rootfs / "data" / "com.apple.android.music" / "files" / "adi.pb"
    return adi.exists() and adi.stat().st_size > 0


async def _start_wrapper_docker(force_login: bool = False) -> dict:
    """Start wrapper via Docker (remote или local image). Returns {ok, msg}."""
    global _wrapper_log_task

    mode = _wrapper_mode()
    ok, docker_path = check_docker_installed()
    if not ok:
        return {"ok": False, "msg": docker_path}

    if await check_wrapper_running():
        return {"ok": True, "msg": "Wrapper already running"}

    dec_port = _cfg.get("decrypt-port", "127.0.0.1:10020")
    m3u_port = _cfg.get("m3u8-port",    "127.0.0.1:20020")
    dec_p    = dec_port.split(":")[-1]
    m3u_p    = m3u_port.split(":")[-1]
    rootfs   = str(_rootfs_data(mode))
    Path(rootfs).mkdir(parents=True, exist_ok=True)

    image = WRAPPER_LOCAL_IMAGE if mode == "docker-local" else "ghcr.io/itouakirai/wrapper:x86"
    subprocess.run([docker_path, "rm", "-f", WRAPPER_CONTAINER_NAME],
                   capture_output=True, timeout=10)

    apple_id  = _cfg.get("wrapper-apple-id", "")
    apple_pwd = _cfg.get("wrapper-password", "")
    has_session = _has_saved_session()
    need_login = force_login or (not has_session and bool(apple_id) and bool(apple_pwd))

    # ── LOGIN path: a fresh login needs 2FA. Run the container INTERACTIVELY
    # (stdin open, no --restart) so (a) the code typed in the UI reaches the
    # wrapper's stdin, and (b) a wrong password can't loop into an Apple lock —
    # one failed attempt tears the container down. ──
    if need_login:
        if not (apple_id and apple_pwd):
            return {"ok": False,
                    "msg": "Apple ID и пароль не заданы в Settings → Apple Music → Wrapper"}
        return await _docker_login(docker_path, image, dec_p, m3u_p, rootfs,
                                   apple_id, apple_pwd, force_login)

    # ── NORMAL path: saved session → detached, auto-restart, no 2FA. ──
    wrapper_args = "-H 0.0.0.0"
    cmd = [
        docker_path, "run", "-d",
        "--name", WRAPPER_CONTAINER_NAME,
        "--restart", "unless-stopped",
        "-v", f"{rootfs}:/app/rootfs/data",
        "-p", f"{dec_p}:10020",
        "-p", f"{m3u_p}:20020",
        "-e", f"args={wrapper_args}",
        image,
    ]
    if _broadcast:
        await _broadcast({"type": "wrapper_log", "text": f"$ {_redact(' '.join(cmd))}"})
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=120)
        lines   = out.decode(errors="replace").strip()
        rc      = proc.returncode

        if rc != 0:
            if _broadcast:
                await _broadcast({"type": "wrapper_log", "text": _redact(lines), "level": "error"})
            return {"ok": False, "msg": f"docker run failed (exit {rc}): {_redact(lines)[:200]}"}

        container_id = lines.strip()[:12]
        if _broadcast:
            await _broadcast({"type": "wrapper_log",
                               "text": f"Container started: {container_id}"})

        for i in range(15):
            await asyncio.sleep(1)
            if await check_wrapper_running():
                if _broadcast:
                    await _broadcast({"type": "wrapper_started"})
                if _wrapper_log_task and not _wrapper_log_task.done():
                    _wrapper_log_task.cancel()
                _wrapper_log_task = asyncio.create_task(_monitor_wrapper_logs())
                return {"ok": True, "msg": f"Wrapper started (container {container_id})"}
            if _broadcast:
                await _broadcast({"type": "wrapper_log",
                                   "text": f"Waiting for wrapper… ({i+1}/15)"})

        return {"ok": False, "msg": "Container started but port not responding after 15s"}
    except asyncio.TimeoutError:
        return {"ok": False, "msg": "Timeout starting wrapper container"}
    except FileNotFoundError:
        return {"ok": False, "msg": f"docker not found at {docker_path}"}
    except Exception as e:
        return {"ok": False, "msg": str(e)}


async def _read_login_stream(proc: "asyncio.subprocess.Process") -> None:
    """Stream the interactive login wrapper's stdout: surface logs, pop the 2FA
    input the moment it's prompted, and tear the container down on a failed
    login (so a wrong password never loops into an account lock)."""
    try:
        if not proc.stdout:
            return
        async for raw in proc.stdout:
            text = raw.decode(errors="replace").strip()
            if not text:
                continue
            if _broadcast:
                await _broadcast({"type": "wrapper_log", "text": _redact(text)})
            low = text.lower()
            if _is_2fa_prompt(low):
                if _broadcast:
                    await _broadcast({"type": "wrapper_2fa_needed"})
            if _is_login_failed(text):
                if _broadcast:
                    await _broadcast({"type": "wrapper_login_failed",
                        "msg": "Логин отклонён Apple (неверный пароль) — остановлено, чтобы не залочить аккаунт"})
                try:
                    proc.terminate()
                except Exception:
                    pass
                ok2, dp2 = check_docker_installed()
                if ok2:
                    subprocess.run([dp2, "rm", "-f", WRAPPER_CONTAINER_NAME],
                                   capture_output=True, timeout=10)
                return
    except asyncio.CancelledError:
        pass
    except Exception:
        pass


async def _docker_login(docker_path: str, image: str, dec_p: str, m3u_p: str,
                        rootfs: str, apple_id: str, apple_pwd: str,
                        force: bool) -> dict:
    """Login start for THIS wrapper build: it reads the 2FA code from a FILE
    (<rootfs>/data/com.apple.android.music/files/2fa.txt — see submit_2fa), not
    stdin, so we run it detached. Crucially NO --restart: a wrong password /
    expired code must not loop into an Apple lock. The log monitor is started
    immediately so the 2FA prompt AND a failed login are caught during login."""
    global _wrapper_log_task
    args_str = f"-H 0.0.0.0 -L {apple_id}:{apple_pwd}" + (" -F" if force else "")
    # Clear any leftover 2FA code first — otherwise the wrapper instantly
    # "detects" a stale (expired) code file and fails the login with
    # "Check the account information". Wait for a FRESH code from the UI.
    try:
        _rf = _rootfs_data(_wrapper_mode())
        for _p in (_rf / "data" / "com.apple.android.music" / "files" / "2fa.txt",
                   _rf / "2fa.txt", _rf / "code.txt"):
            if _p.exists():
                _p.unlink()
    except Exception:
        pass
    cmd = [
        docker_path, "run", "-d",
        "--name", WRAPPER_CONTAINER_NAME,
        "-v", f"{rootfs}:/app/rootfs/data",
        "-p", f"{dec_p}:10020",
        "-p", f"{m3u_p}:20020",
        "-e", f"args={args_str}",
        image,
    ]
    if _broadcast:
        await _broadcast({"type": "wrapper_log", "text": f"$ {_redact(' '.join(cmd))}"})
        await _broadcast({"type": "wrapper_log",
                          "text": "🔐 Логин в Apple… дождись 2FA-кода на телефоне — откроется поле для ввода."})
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=120)
        if proc.returncode != 0:
            return {"ok": False,
                    "msg": f"docker run failed: {_redact(out.decode(errors='replace'))[:200]}"}
    except FileNotFoundError:
        return {"ok": False, "msg": f"docker not found at {docker_path}"}
    except Exception as e:
        return {"ok": False, "msg": str(e)}

    # Start the monitor NOW (catches the 2FA prompt + a login failure during the
    # login phase, not only after the wrapper is already serving).
    if _wrapper_log_task and not _wrapper_log_task.done():
        _wrapper_log_task.cancel()
    _wrapper_log_task = asyncio.create_task(_monitor_wrapper_logs())

    # Login + 2FA can take a while — poll readiness up to 120 s.
    for _ in range(120):
        await asyncio.sleep(1)
        if await check_wrapper_running():
            if _broadcast:
                await _broadcast({"type": "wrapper_started"})
            return {"ok": True, "msg": "Враппер залогинен и запущен"}
    return {"ok": True, "msg": "Логин идёт — введи 2FA-код в открывшемся поле"}


async def stop_wrapper_docker() -> dict:
    """Stop the wrapper container."""
    ok, docker_path = check_docker_installed()
    if not ok:
        return {"ok": False, "msg": docker_path}
    try:
        proc = await asyncio.create_subprocess_exec(
            docker_path, "rm", "-f", WRAPPER_CONTAINER_NAME,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        await proc.communicate()
        return {"ok": True, "msg": "Wrapper stopped"}
    except Exception as e:
        return {"ok": False, "msg": str(e)}


# ── Non-Docker wrapper ─────────────────────────────────────────────────────────

async def _monitor_wrapper_proc_logs() -> None:
    """Stream stdout от non-docker wrapper процесса."""
    global _wrapper_direct_proc
    if not _wrapper_direct_proc or not _wrapper_direct_proc.stdout:
        return
    try:
        async for raw in _wrapper_direct_proc.stdout:
            text = raw.decode(errors="replace").strip()
            if text:
                if _broadcast:
                    await _broadcast({"type": "wrapper_log", "text": text})
                if _is_2fa_prompt(text.lower()):
                    if _broadcast:
                        await _broadcast({"type": "wrapper_2fa_needed"})
    except asyncio.CancelledError:
        pass
    except Exception:
        pass


async def _start_wrapper_direct(force_login: bool = False) -> dict:
    """Запуск dist/non-docker/wrapper напрямую (Linux) или через WSL (Windows)."""
    global _wrapper_direct_proc, _wrapper_log_task

    bin_path = _wrapper_bin()
    if not bin_path.exists():
        return {"ok": False, "msg": f"Бинарник не найден: {bin_path}"}

    if await check_wrapper_running():
        return {"ok": True, "msg": "Wrapper already running"}

    dec_port = _cfg.get("decrypt-port", "127.0.0.1:10020")
    m3u_port = _cfg.get("m3u8-port",    "127.0.0.1:20020")
    dec_p    = int(dec_port.split(":")[-1])
    m3u_p    = int(m3u_port.split(":")[-1])

    apple_id  = _cfg.get("wrapper-apple-id", "")
    apple_pwd = _cfg.get("wrapper-password", "")
    has_session = _has_saved_session()

    wrapper_args = ["-H", "0.0.0.0", "-D", str(dec_p), "-M", str(m3u_p)]
    if force_login:
        if not apple_id or not apple_pwd:
            return {"ok": False, "msg": "Apple ID и пароль не заданы в Settings → Apple Music → Wrapper"}
        wrapper_args += ["-L", f"{apple_id}:{apple_pwd}", "-F"]
    elif not has_session and apple_id and apple_pwd:
        wrapper_args += ["-L", f"{apple_id}:{apple_pwd}"]

    _rootfs_data("non-docker").mkdir(parents=True, exist_ok=True)
    dist_dir = _dist_dir("non-docker")

    if _is_windows:
        if not check_wsl_available():
            return {"ok": False, "msg": "WSL не найден. Установи WSL 2 для запуска non-docker враппера на Windows."}
        wsl_bin = _to_wsl_path(bin_path)
        wsl_cwd = _to_wsl_path(dist_dir)
        cmd = ["wsl", "--cd", wsl_cwd, "--", wsl_bin] + wrapper_args
    else:
        cmd = [str(bin_path)] + wrapper_args

    if _broadcast:
        await _broadcast({"type": "wrapper_log", "text": f"$ {' '.join(cmd)}"})

    try:
        _wrapper_direct_proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(dist_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        for i in range(15):
            await asyncio.sleep(1)
            if await check_wrapper_running():
                if _broadcast:
                    await _broadcast({"type": "wrapper_started"})
                if _wrapper_log_task and not _wrapper_log_task.done():
                    _wrapper_log_task.cancel()
                _wrapper_log_task = asyncio.create_task(_monitor_wrapper_proc_logs())
                return {"ok": True, "msg": "Wrapper (non-docker) запущен"}
            if _broadcast:
                await _broadcast({"type": "wrapper_log",
                                   "text": f"Waiting for wrapper… ({i+1}/15)"})
        return {"ok": False, "msg": "Wrapper не ответил на порту после 15s"}
    except Exception as e:
        return {"ok": False, "msg": str(e)}


async def _stop_wrapper_direct() -> dict:
    global _wrapper_direct_proc
    if _wrapper_direct_proc and _wrapper_direct_proc.returncode is None:
        try:
            _wrapper_direct_proc.terminate()
            await asyncio.wait_for(_wrapper_direct_proc.wait(), timeout=5.0)
        except Exception:
            try:
                _wrapper_direct_proc.kill()
            except Exception:
                pass
        _wrapper_direct_proc = None
    return {"ok": True, "msg": "Wrapper остановлен"}


# ── Public dispatcher ──────────────────────────────────────────────────────────

async def start_wrapper(force_login: bool = False) -> dict:
    """Запуск враппера в режиме, заданном wrapper-mode в конфиге."""
    mode = _wrapper_mode()
    if mode == "non-docker":
        return await _start_wrapper_direct(force_login)
    return await _start_wrapper_docker(force_login)


# backward-compat alias
async def start_wrapper_docker(force_login: bool = False) -> dict:
    return await start_wrapper(force_login)


async def stop_wrapper() -> dict:
    """Остановить враппер (любой режим)."""
    mode = _wrapper_mode()
    if mode == "non-docker":
        return await _stop_wrapper_direct()
    return await stop_wrapper_docker()


async def build_wrapper_image() -> dict:
    """docker build из dist/docker/ → образ ripster-wrapper (для docker-local режима)."""
    ok, docker_path = check_docker_installed()
    if not ok:
        return {"ok": False, "msg": docker_path}
    dist = _dist_dir("docker-local")
    if not (dist / "Dockerfile").exists():
        return {"ok": False, "msg": f"Dockerfile не найден в {dist}"}
    if _broadcast:
        await _broadcast({"type": "wrapper_log",
                           "text": f"🔨 Building {WRAPPER_LOCAL_IMAGE} из {dist}…"})
    try:
        proc = await asyncio.create_subprocess_exec(
            docker_path, "build", "-t", WRAPPER_LOCAL_IMAGE, ".",
            cwd=str(dist),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        async for raw in proc.stdout:
            line = raw.decode(errors="replace").rstrip()
            if line and _broadcast:
                await _broadcast({"type": "wrapper_log", "text": line})
        await proc.wait()
        if proc.returncode == 0:
            if _broadcast:
                await _broadcast({"type": "wrapper_log",
                                   "text": f"✓ Image {WRAPPER_LOCAL_IMAGE} собран",
                                   "level": "success"})
                await _broadcast({"type": "wrapper_built"})
            return {"ok": True, "msg": f"Image {WRAPPER_LOCAL_IMAGE} собран"}
        return {"ok": False, "msg": f"docker build failed (exit {proc.returncode})"}
    except Exception as e:
        return {"ok": False, "msg": str(e)}


async def pull_wrapper_image() -> dict:
    """docker pull для remote-образа, или build для docker-local режима."""
    mode = _wrapper_mode()
    if mode == "docker-local":
        return await build_wrapper_image()
    ok, docker_path = check_docker_installed()
    if not ok:
        return {"ok": False, "msg": docker_path}
    image = "ghcr.io/itouakirai/wrapper:x86"
    if _broadcast:
        await _broadcast({"type": "wrapper_log", "text": f"Pulling {image}…"})
    try:
        proc = await asyncio.create_subprocess_exec(
            docker_path, "pull", image,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        async for raw in proc.stdout:
            line = raw.decode(errors="replace").rstrip()
            if line and _broadcast:
                await _broadcast({"type": "wrapper_log", "text": line})
        await proc.wait()
        if proc.returncode == 0:
            return {"ok": True, "msg": "Image pulled"}
        return {"ok": False, "msg": f"Pull failed (exit {proc.returncode})"}
    except Exception as e:
        return {"ok": False, "msg": str(e)}


__all__ = [
    "install",
    "get_amd_dir",
    "write_amd_config",
    "amd_wrapper_status",
    "clone_amd",
    "patch_amd_for_headless",
    "install_amd_deps",
    "check_docker_installed",
    "check_wrapper_running",
    "check_wsl_available",
    "start_wrapper",
    "start_wrapper_docker",   # backward-compat alias
    "stop_wrapper",
    "stop_wrapper_docker",
    "pull_wrapper_image",
    "build_wrapper_image",
    "WRAPPER_CONTAINER_NAME",
    "WRAPPER_LOCAL_IMAGE",
    "_wrapper_mode",
    "_rootfs_data",
    "_wrapper_bin",
]
