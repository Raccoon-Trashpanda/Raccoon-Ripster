"""ripster.setup — auto-installer and tool-detection helpers.

Public surface:
    install(cfg, broadcast_fn, save_config_fn, base_dir, is_windows)
    install_log          — list of {text, level, ts} entries streamed to browser
    ilog(text, level)    — append to install_log and broadcast
    istep(name, status)  — broadcast install step status
    irun(cmd, cwd)       — run subprocess, stream output to install_log
    check_tools()        — return dict of tool presence / version
    tool_path(name)      — find tool on PATH or in base_dir/tools
    find_go()            — locate go executable with Windows fallbacks
    check_docker_installed() — (bool, path_or_error)
    run_full_setup()     — master setup routine (installs everything)
    _gamdl_flag(name, *args) — return flag only if gamdl supports it
    _build_env()         — os.environ copy with extra PATH entries
    download_file(url, dest, label)        — download with progress (TLS via certifi)
    install_go_windows()
    install_gpac_windows()
    install_mp4decrypt_windows()
    clone_pinned(iid, dest, preserve_existing)
    clone_downloader()
    go_mod_download()
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Optional

from ripster.net_ssl import ssl_context as _ssl_context

# Windows: suppress the console window every child process would otherwise flash
# on the owner's desktop. Tool-detection probes (docker/gamdl/node/--version)
# run when the Setup tab loads, so without this the user sees "3-4 black windows"
# pop up. 0 on non-Windows. Defined up top so every helper can reference it.
_NO_WIN = 0x08000000 if os.name == "nt" else 0

_cfg:         dict     = {}
_broadcast              = None
_save_config            = None
_base_dir:    Path      = Path(".")
_is_windows:  bool      = platform.system() == "Windows"

install_log:  list[dict] = []
_need_restart: bool       = False
_gamdl_flags: set[str]   = set()


def install(
    cfg:            dict,
    broadcast_fn,
    save_config_fn,
    base_dir:       Path,
    is_windows:     bool,
) -> None:
    """Wire globals. Call once at app startup before any setup function."""
    global _cfg, _broadcast, _save_config, _base_dir, _is_windows
    _cfg          = cfg
    _broadcast    = broadcast_fn
    _save_config  = save_config_fn
    _base_dir     = base_dir
    _is_windows   = is_windows


# ─── tool detection ──────────────────────────────────────────────────────────

def find_go() -> str:
    """Locate the go executable on PATH, with Windows fallbacks."""
    go = shutil.which("go")
    if go:
        return go
    if _is_windows:
        candidates = [
            r"C:\Program Files\Go\bin\go.exe",
            r"C:\Go\bin\go.exe",
            os.path.expandvars(r"%USERPROFILE%\go\bin\go.exe"),
            os.path.expandvars(r"%LOCALAPPDATA%\Programs\Go\bin\go.exe"),
        ]
        for c in candidates:
            if os.path.isfile(c):
                return c
    return "go"


def check_docker_installed() -> tuple[bool, str]:
    """Return (installed, path_or_error)."""
    docker = shutil.which("docker")
    if docker:
        try:
            r = subprocess.run(
                [docker, "info", "--format", "{{.ServerVersion}}"],
                capture_output=True, text=True, timeout=5, creationflags=_NO_WIN,
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
                    capture_output=True, text=True, timeout=5, creationflags=_NO_WIN,
                )
                if r.returncode == 0:
                    return True, p
            except Exception:
                pass
            return False, "Docker found but daemon not running — start Docker Desktop"
    return False, "Docker not installed"


def tool_path(name: str) -> Optional[str]:
    """Check if a tool exists on PATH or in base_dir/tools."""
    found = shutil.which(name)
    if found:
        return found
    local = _base_dir / "tools" / (name + (".exe" if _is_windows else ""))
    if local.exists():
        return str(local)
    if _is_windows:
        candidates: list[str] = {
            "go": [
                str(_base_dir / "tools" / "go" / "bin" / "go.exe"),  # portable zip
                r"C:\Program Files\Go\bin\go.exe",
                r"C:\Go\bin\go.exe",
                os.path.expandvars(r"%USERPROFILE%\go\bin\go.exe"),
            ],
            "MP4Box": [
                r"C:\Program Files\GPAC\MP4Box.exe",
                r"C:\Program Files (x86)\GPAC\MP4Box.exe",
            ],
            "mp4decrypt": [
                str(_base_dir / "tools" / "mp4decrypt.exe"),
                os.path.expandvars(r"%APPDATA%\bento4\bin\mp4decrypt.exe"),
            ],
        }.get(name, [])
        for c in candidates:
            if os.path.isfile(c):
                return c
    return None


def _detect_gamdl_flags() -> set[str]:
    """Parse gamdl --help once and cache available CLI flags."""
    global _gamdl_flags
    if _gamdl_flags:
        return _gamdl_flags
    try:
        # НЕ shutil.which("gamdl"): console-script shim под изолированным
        # embeddable-питоном молча выходит с кодом 1 → флаги пустые
        # (preflight Gate 0.5). Тот же интерпретатор, -m gamdl.
        from ripster.py_runtime import app_python
        r = subprocess.run(
            [app_python(), "-m", "gamdl", "--help"],
            capture_output=True, text=True, timeout=10, creationflags=_NO_WIN,
        )
        flags = set(re.findall(r"--([a-z][a-z0-9-]+)", r.stdout + r.stderr))
        _gamdl_flags = flags
        print(f"[gamdl] Detected {len(flags)} flags: {sorted(flags)[:10]}…", flush=True)
    except Exception as e:
        print(f"[gamdl] Could not detect flags: {e}", flush=True)
        _gamdl_flags = set()
    return _gamdl_flags


def _gamdl_flag(name: str, *args) -> list[str]:
    """Return [--name, *args] only if the flag exists in this gamdl version."""
    flags = _detect_gamdl_flags()
    if not flags or name in flags:
        return [f"--{name}"] + [str(a) for a in args]
    print(f"[gamdl] Skipping unknown flag: --{name}", flush=True)
    return []


async def check_tools() -> dict:
    """Return status of all required tools."""
    engine = _cfg.get("engine", "zhaarey")
    tools = {
        "go":          {"label": "Go (zhaarey engine)",         "required": engine == "zhaarey"},
        "git":         {"label": "Git",                          "required": True},
        "gamdl":       {"label": "gamdl (Python)",              "required": engine == "gamdl"},
        "ffmpeg":      {"label": "FFmpeg",                       "required": False},
        "MP4Box":      {"label": "MP4Box (GPAC)",               "required": engine == "zhaarey"},
        "mp4decrypt":  {"label": "mp4decrypt (Bento4)",         "required": False},
        "N_m3u8DL-RE": {"label": "N_m3u8DL-RE (fast)",         "required": False},
    }
    result: dict = {}
    for name, info in tools.items():
        path = tool_path(name)
        result[name] = {
            "label":    info["label"],
            "required": info["required"],
            "found":    bool(path),
            "path":     path or "",
        }
        if path:
            try:
                r = subprocess.run(
                    [path, "version" if name == "go" else "--version"],
                    capture_output=True, text=True, timeout=5, creationflags=_NO_WIN,
                )
                ver = (r.stdout or r.stderr or "").strip().splitlines()[0][:60]
                result[name]["version"] = ver
            except Exception:
                result[name]["version"] = "?"
        else:
            result[name]["version"] = "NOT FOUND"

    docker_ok, docker_msg = check_docker_installed()
    result["docker"] = {
        "label":    "Docker (zhaarey wrapper)",
        "required": engine == "zhaarey",
        "found":    docker_ok,
        "path":     docker_msg if docker_ok else "",
        "version":  "daemon running" if docker_ok else docker_msg,
    }

    main_go = Path(_cfg.get("main-go-path", _base_dir / "main.go"))
    result["downloader"] = {
        "label":    "apple-music-downloader (main.go)",
        "required": engine == "zhaarey",
        "found":    main_go.exists(),
        "path":     str(main_go),
        "version":  "present" if main_go.exists() else "NOT FOUND",
    }
    result["zhaarey"] = {
        "label":    "Apple wrapper (zhaarey)",
        "required": engine == "zhaarey",
        "found":    main_go.exists()
                    and bool(tool_path("MP4Box"))
                    and bool(tool_path("mp4decrypt")),
        "path":     "",
        "version":  "ready" if (main_go.exists()
                                and bool(tool_path("MP4Box"))
                                and bool(tool_path("mp4decrypt")))
                    else "incomplete",
    }
    return result


# ─── environment + subprocess helpers ────────────────────────────────────────

def _build_env() -> dict:
    env = os.environ.copy()
    extras = [
        r"C:\Program Files\Go\bin", r"C:\Go\bin",
        os.path.expandvars(r"%USERPROFILE%\go\bin"),
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\Go\bin"),
        r"C:\Program Files\GPAC", r"C:\Program Files (x86)\GPAC",
        os.path.expandvars(r"%APPDATA%\bento4\bin"),
        str(_base_dir / "tools"),
        r"C:\Windows\System32",
    ] if _is_windows else []
    for p in extras:
        if p and p not in env.get("PATH", ""):
            env["PATH"] = p + os.pathsep + env.get("PATH", "")
    return env


async def ilog(text: str, level: str = "info") -> None:
    entry = {"text": text, "level": level, "ts": datetime.now().strftime("%H:%M:%S")}
    install_log.append(entry)
    if _broadcast:
        await _broadcast({"type": "install_log", "entry": entry})


async def istep(name: str, status: str = "running") -> None:
    """Broadcast current install step to UI (running / done / error / skip)."""
    if _broadcast:
        await _broadcast({"type": "install_step", "name": name, "status": status})


async def irun(cmd: list, cwd: Optional[str] = None,
               timeout: Optional[int] = None) -> tuple[int, str]:
    """Run a command, stream every line to setup console, return (rc, output).

    `timeout` — потолок на ВЕСЬ процесс. Раньше его не было: `git clone` на
    отвалившемся HTTPS, `npm install` без сети и `go mod download` застревали
    навечно, и человек видел вращающуюся кнопку вместо ошибки (аудит 25.09.2026).
    По истечении убивается ВСЁ ДЕРЕВО потомков (`taskkill /T`): у npm и go
    живой ребёнок переживает смерть своего прямого родителя.
    """
    env   = _build_env()
    flags: dict = {}
    if _is_windows:
        flags["creationflags"] = 0x08000000  # CREATE_NO_WINDOW

    cmd_str = " ".join(f'"{a}"' if " " in str(a) else str(a) for a in cmd)
    await ilog(f"$ {cmd_str}", "stdout")
    print(f"[irun] {cmd_str}", flush=True)

    try:
        proc = await asyncio.create_subprocess_exec(
            *[str(c) for c in cmd],
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
            cwd=cwd,
            **flags,
        )
    except FileNotFoundError:
        msg = f"Executable not found: {cmd[0]}"
        await ilog(f"✗ {msg}", "error")
        print(f"[irun] ERROR: {msg}", flush=True)
        return -1, msg
    except Exception as e:
        await ilog(f"✗ Failed to start: {e}", "error")
        print(f"[irun] ERROR: {e}", flush=True)
        return -1, str(e)

    async def _pump() -> list[str]:
        acc: list[str] = []
        async for raw in proc.stdout:
            line = raw.decode(errors="replace").rstrip()
            if line:
                acc.append(line)
                await ilog(line, "stdout")
        await proc.wait()
        return acc

    try:
        out = await asyncio.wait_for(_pump(), timeout=timeout) if timeout \
              else await _pump()
        return proc.returncode, "\n".join(out)
    except asyncio.TimeoutError:
        try:
            if _is_windows:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                               capture_output=True, timeout=20, creationflags=_NO_WIN)
            else:
                proc.kill()
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        msg = f"остановлено по таймауту {timeout} с"
        await ilog(f"✗ {cmd[0]}: {msg} — процесс снят, Setup продолжится.", "error")
        return -1, msg


# ─── download helpers ─────────────────────────────────────────────────────────

# 25.09.2026, аудит автоустановщиков. У `opener.open(url)` таймаута не было НИ
# на одном скачивании Setup: подвешенное TCP-соединение (провайдер/антивирус
# молча держит порт открытым) вешало компонент НАВСЕГДА — кнопка крутится,
# консоль молчит, человек пишет «падает». Замер 23.08.2026 на ffmpeg: 120 КБ/с,
# клиент отвалился через 880 с, а загрузка шла дальше без него.
#   STALL_TIMEOUT — сколько можно НЕ получать ни байта (socket-timeout на read);
#   DOWNLOAD_TIMEOUT — общий потолок на артефакт, даже если капает по чуть-чуть.
STALL_TIMEOUT    = 30
DOWNLOAD_TIMEOUT = 900
_DEFAULT_TIMEOUTS = {}          # id установщика → секунды (из реестра)


def _timeout_for(iid: str) -> int:
    if iid not in _DEFAULT_TIMEOUTS:
        try:
            from ripster.installers_manifest import by_id
            spec = by_id(iid)
            _DEFAULT_TIMEOUTS[iid] = spec.timeout_s if spec else DOWNLOAD_TIMEOUT
        except Exception:
            _DEFAULT_TIMEOUTS[iid] = DOWNLOAD_TIMEOUT
    return _DEFAULT_TIMEOUTS[iid] or DOWNLOAD_TIMEOUT


def _expected_sha256(iid: str) -> str:
    """Хэш из реестра — НО только когда он реально закреплён (hash_source=pinned).

    Плавные артефакты (ffmpeg с gyan.dev, GPAC-nightly) пересобираются ежедневно:
    требовать от них хэш — значит сломать установку верным ожиданием неверного
    файла. Для них хэш берёт установщик у провайдера (go/node/JRE) или не берёт
    вовсе, и реестр честно помечает это hash_source != pinned (аудит 25.09.2026).
    """
    try:
        from ripster.installers_manifest import by_id
        spec = by_id(iid)
        if spec and spec.hash_source == "pinned":
            return spec.sha256 or ""
        return ""
    except Exception:
        return ""


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


async def _fetch(url: str, dest: Path, label: str, *, iid: str = "",
                 expected_sha256: Optional[str] = None, timeout: Optional[int] = None) -> bool:
    """Один честный загрузчик: таймаут на залипание + общий потолок + SHA256.

    Возвращает False с осмысленной строкой в консоли при ЛЮБОЙ неудаче; никогда
    не зависает. При несовпадении хэша файл СНОВА не оставляют на диске.
    TLS-проверка — через ripster.net_ssl.ssl_context() (certifi, если есть).
    """
    label = label or dest.name
    timeout = timeout if timeout is not None else _timeout_for(iid)
    if expected_sha256 is None:
        expected_sha256 = _expected_sha256(iid)
    await ilog(f"⬇ Downloading {label}…", "info")
    await ilog(f"  URL: {url}", "stdout")

    loop = asyncio.get_running_loop()
    _last_pct = [-1]

    def _reporthook(count, block, total):
        if total <= 0:
            return
        pct = min(100, int(count * block * 100 / total))
        if pct % 10 == 0 and pct != _last_pct[0]:
            _last_pct[0] = pct
            mb_done  = count * block / 1_048_576
            mb_total = total  / 1_048_576
            msg = f"  {pct:3d}%  {mb_done:.1f} / {mb_total:.1f} MB"
            loop.call_soon_threadsafe(
                lambda m=msg: asyncio.ensure_future(ilog(m, "stdout"), loop=loop)
            )

    def _blocking():
        # 01.10.2026 (079): один контекст на весь проект — certifi-бандл, если
        # доступен, иначе системный магазин. Без CERT_NONE, без повторов.
        handler = urllib.request.HTTPSHandler(context=_ssl_context())
        opener = urllib.request.build_opener(handler)
        opener.addheaders = [("User-Agent", "Mozilla/5.0 ripster-setup")]
        deadline = time.monotonic() + timeout
        with opener.open(url, timeout=STALL_TIMEOUT) as resp, open(dest, "wb") as out_f:
            total_size = int(resp.headers.get("Content-Length", 0) or 0)
            block, count = 8192, 0
            while chunk := resp.read(block):
                out_f.write(chunk)
                count += 1
                _reporthook(count, block, total_size)
                if time.monotonic() > deadline:
                    raise TimeoutError(
                        f"не уложился в {timeout} с "
                        f"(получено {out_f.tell() / 1_048_576:.1f} МБ)")

    try:
        await loop.run_in_executor(None, _blocking)
    except Exception as e:
        try:
            dest.unlink(missing_ok=True)      # недокачанный хвост не должен
        except Exception:                      # притворяться целым архивом
            pass
        reason = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
        is_tls = "CERTIFICATE" in str(e).upper() or "SSL" in type(e).__name__.upper()
        if is_tls:
            await ilog(f"  ✗ {label}: не удалось проверить сертификат сервера — "
                       f"ничего не скачано. Причина: {reason}", "error")
            from urllib.parse import urlparse
            host = urlparse(url).hostname or url
            await ilog(f"  Обнови корневые сертификаты Windows: Центр обновления "
                       f"или certutil -generateSSTFromWU (хост: {host}).", "warn")
        else:
            await ilog(f"  ✗ {label}: не удалось скачать — {reason}", "error")
        await ilog(_manual_hint(iid, url), "warn")
        print(f"[download] ERROR: {reason}", flush=True)
        return False

    size_mb = dest.stat().st_size / 1_048_576
    if expected_sha256:
        got = sha256_file(dest)
        if got.lower() != expected_sha256.lower():
            dest.unlink(missing_ok=True)
            await ilog(f"  ✗ {label}: контрольная сумма не совпала "
                       f"(ожидался {expected_sha256[:12]}…, получен {got[:12]}…) — "
                       f"файл удалён, установщик не продолжит.", "error")
            await ilog(_manual_hint(iid, url), "warn")
            return False
        await ilog(f"  ✓ SHA256 совпал ({got[:12]}…)", "success")
    await ilog(f"  ✓ Saved {dest.name} ({size_mb:.1f} MB)", "success")
    return True


def _manual_hint(iid: str, url: str) -> str:
    """Что человеку делать, когда автоустановка не смогла — по каждому источнику."""
    return {
        "go":       "Поставь Go вручную: https://go.dev/dl/",
        "gpac":     "Поставь GPAC вручную: https://gpac.io/downloads/gpac-nightly-builds/",
        "bento4":   "Скачай Bento4 вручную: https://www.bento4.com/downloads/",
        "ffmpeg":   "Поставь ffmpeg вручную: winget install Gyan.FFmpeg",
        "node":     "Поставь Node вручную: winget install OpenJS.NodeJS.LTS",
        "spotiflac": "Скачай SpotiFLAC вручную: "
                     "https://github.com/Nizarberyan/SpotiFLAC/releases",
    }.get(iid, f"Скачай вручную: {url}")


async def download_file(url: str, dest: Path, label: str = "", *,
                        iid: str = "", expected_sha256: Optional[str] = None,
                        timeout: Optional[int] = None) -> bool:
    """Download url→dest with live KB/s progress. Thread-safe.
    TLS-проверка — через ripster.net_ssl (certifi, если есть; без отключений)."""
    return await _fetch(url, dest, label, iid=iid, expected_sha256=expected_sha256,
                        timeout=timeout)


# ─── platform installers ──────────────────────────────────────────────────────

async def _verify_runs(exe: str, args: tuple[str, ...] = ("--version",),
                       label: str = "", timeout: int = 15,
                       banner: str = "") -> bool:
    """Доказать, что установленный инструмент РЕАЛЬНО исполняется, а не просто
    лежит файлом. Половинка установщиков продукта проверяла «файл есть» — и
    «установлено» означало «скачалось», при сломанном бинаре дальше падал уже
    движок, в сотне строк от причины.

    `banner` — строка, которая доказывает запуск, даже когда кода возврата нет:
    замер 25.09.2026 — инструменты Bento4 (`mp4decrypt`, `mp4extract`) не знают
    флага --version и ВСЕГДА возвращают 1, печатая баннер «Bento4 Version»;
    требовать от них rc=0 значило бы объявлять живой бинарь сломанным.
    """
    name = label or Path(exe).name
    try:
        r = subprocess.run([exe, *args], capture_output=True, text=True,
                           timeout=timeout, creationflags=_NO_WIN)
        text = f"{r.stdout or ''}\n{r.stderr or ''}"
        if r.returncode == 0 or (banner and banner in text):
            ver = text.strip().splitlines()
            await ilog(f"✓ {name} исполняется: {(ver[0] if ver else '')[:70]}", "success")
            return True
        await ilog(f"✗ {name}: установщик отчитался, но запуск вернул "
                   f"код {r.returncode} — инструмент не работает.", "error")
    except FileNotFoundError:
        await ilog(f"✗ {name}: файл есть, но Windows его не запускает "
                   f"(не тот формат или не хватает DLL).", "error")
    except Exception as e:
        await ilog(f"✗ {name}: проверка запуска не пройдена — "
                   f"{type(e).__name__}: {e}", "error")
    return False


def _go_release_sync(arch: str = "") -> tuple[str, str, str]:
    """Синхронное ядро выбора релиза Go: (version, filename, sha256).

    Берёт go.dev/dl/?mode=json, ищет самый новый стабильный релиз, у которого
    есть файл os=windows, arch=<нужная>, kind=archive. Имя файла и SHA256 —
    из этой же записи (никаких вторичных запросов). Если JSON недоступен или
    подходящего файла нет — явный запасной релиз из реестра
    (installers_manifest.BY_ID["go"].extra["fallback"]), а не молчаливый
    hardcoded в коде.

    Используется и установщиком (через async-обёртку), и стражем
    (tools/check_installers.py) — чтобы они не могли разойтись.
    """
    if not arch:
        arch = "amd64" if platform.machine().endswith("64") else "386"
    try:
        import json
        with urllib.request.urlopen("https://go.dev/dl/?mode=json",
                                    timeout=STALL_TIMEOUT) as r:
            data = json.load(r)
        for rel in data:
            if not rel.get("stable"):
                continue
            ver = rel.get("version", "")
            for f in rel.get("files", []):
                if (f.get("os") == "windows" and f.get("arch") == arch
                        and f.get("kind") == "archive"):
                    fname = f.get("filename", "")
                    return ver, fname, f.get("sha256", "")
    except Exception:
        pass
    from ripster.installers_manifest import by_id
    fb = (by_id("go").extra.get("fallback") if by_id("go") else "") or "go1.27.1"
    fname = f"{fb}.windows-{arch}.zip"
    return fb, fname, ""


async def _go_release(arch: str = "") -> tuple[str, str, str]:
    """Async-обёртка над _go_release_sync: не блокирует event loop на HTTP."""
    try:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _go_release_sync, arch)
    except Exception:
        from ripster.installers_manifest import by_id
        if not arch:
            arch = "amd64" if platform.machine().endswith("64") else "386"
        fb = (by_id("go").extra.get("fallback") if by_id("go") else "") or "go1.27.1"
        return fb, f"{fb}.windows-{arch}.zip", ""


async def install_go_windows() -> None:
    """Download and install Go on Windows silently."""
    global _need_restart
    existing = tool_path("go")
    if existing and Path(existing).is_file() and \
            str(_base_dir / "tools" / "go").lower() in str(existing).lower():
        await ilog(f"✓ Go уже стоит (portable): {existing}", "success")
        return
    await ilog("📦 Fetching latest Go version info…")
    ver, fname, sha = await _go_release()
    await ilog(f"   Latest Go: {ver}")
    # Use the PORTABLE zip, not the MSI: the MSI needs elevation and returned
    # 1603 on a normal (non-admin) install. The zip extracts locally, no admin.
    url  = f"https://go.dev/dl/{fname}"
    tmp  = Path(tempfile.gettempdir()) / fname
    ok   = await download_file(url, tmp, f"Go {ver} (portable zip)",
                               iid="go", expected_sha256=sha)
    if not ok:
        await ilog(_manual_hint("go", url), "warn")
        return
    await ilog("🔧 Extracting Go (portable, no admin)…", "info")
    tools = _base_dir / "tools"
    go_root = tools / "go"
    try:
        tools.mkdir(exist_ok=True)
        if go_root.exists():
            shutil.rmtree(go_root, ignore_errors=True)
        with zipfile.ZipFile(tmp) as z:
            z.extractall(tools)                      # creates tools\go\
        go_bin = go_root / "bin"
        # Usable immediately this session; tool_path() also finds it after restart.
        os.environ["PATH"] = str(go_bin) + os.pathsep + os.environ.get("PATH", "")
        if (go_bin / "go.exe").exists():
            await _verify_runs(str(go_bin / "go.exe"), ("version",), "Go")
            await ilog(f"✓ Go installed (portable) → {go_root}", "success")
        else:
            await ilog("✗ Go extracted but go.exe missing", "error")
    except Exception as e:
        await ilog(f"✗ Go extract failed: {e}", "error")
        await ilog(_manual_hint("go", url), "warn")


async def install_gpac_windows() -> None:
    """Download GPAC from the official gpac.io permalink — always the latest build."""
    existing = tool_path("MP4Box")
    if existing:
        await _verify_runs(existing, ("-version",), "MP4Box")
        await ilog(f"✓ MP4Box уже стоит: {existing}", "success")
        return
    await ilog("📦 Fetching GPAC (MP4Box) installer from gpac.io…")
    await ilog("📦 Fetching GPAC (MP4Box) installer from gpac.io…")
    is64 = platform.machine().endswith("64")

    GPAC_NIGHTLY_URL = (
        "https://download.tsi.telecom-paristech.fr/gpac/new_builds/gpac_latest_head_win64.exe"
        if is64 else
        "https://download.tsi.telecom-paristech.fr/gpac/new_builds/gpac_latest_head_win32.exe"
    )
    GPAC_STABLE_URL = (
        "https://download.tsi.telecom-paristech.fr/gpac/release/26.02/gpac-26.02-rev0-g118e60a9-master-x64.exe"
        if is64 else
        "https://download.tsi.telecom-paristech.fr/gpac/release/26.02/gpac-26.02-rev0-g118e60a9-master-win32.exe"
    )

    tmp = Path(tempfile.gettempdir()) / "gpac_latest_win.exe"
    ok  = await download_file(GPAC_NIGHTLY_URL, tmp, "GPAC latest nightly build", iid="gpac")
    if not ok:
        await ilog("   Nightly failed, trying stable 26.02…", "stdout")
        ok = await download_file(GPAC_STABLE_URL, tmp, "GPAC 26.02 stable", iid="gpac")
    if not ok:
        await ilog("✗ GPAC download failed", "error")
        await ilog("   Download manually: https://gpac.io/downloads/gpac-nightly-builds/", "warn")
        return

    await ilog("🔧 Running GPAC installer (silent)… this may take 10–30 seconds", "info")
    rc, _ = await irun([str(tmp), "/S"], timeout=600)
    if rc == 0:
        await ilog("✓ GPAC / MP4Box installed successfully", "success")
    elif rc == 1:
        if tool_path("MP4Box"):
            await ilog("✓ GPAC / MP4Box installed (exit 1 but binary found)", "success")
        else:
            await ilog(f"⚠ Installer exit {rc} — try running manually if MP4Box is missing", "warn")
    else:
        await ilog(f"✗ Installer exit code {rc}", "error")
        await ilog("   Run the downloaded installer manually if needed", "warn")


async def install_mp4decrypt_windows() -> None:
    """Download Bento4 SDK and extract the FULL CLI toolset into tools/.

    `mp4extract` (Bento4) — тот же набор, что нужен Apple-путям при ремуксе
    (ALAC: `mp4extract …/alac …`). Extracting only mp4decrypt.exe (the old
    behaviour) left mp4extract.exe missing → every ALAC track died at the decrypt
    step with a cryptic `[WinError 2]` in EVERY region. Grab all bin/*.exe."""
    tools_dir = _base_dir / "tools"
    dec = tools_dir / "mp4decrypt.exe"
    ext = tools_dir / "mp4extract.exe"
    if dec.is_file() and ext.is_file():
        both_ok = (await _verify_runs(str(dec), (), "mp4decrypt", banner="Bento4")
                   and await _verify_runs(str(ext), (), "mp4extract", banner="Bento4"))
        if both_ok:
            await ilog(f"✓ Bento4 уже стоит в {tools_dir} — перекачиваю не буду", "success")
            return
        await ilog("⚠ Bento4 лежит в tools/, но не запускается — перекачиваю.", "warn")
    await ilog("📦 Downloading Bento4 SDK (mp4decrypt + mp4extract + …)…")
    tools_dir.mkdir(exist_ok=True)

    BENTO4_VER  = "1-6-0-641"
    name        = f"Bento4-SDK-{BENTO4_VER}.x86_64-microsoft-win32.zip"
    # bok.net serves the zip directly under /binaries/ (the /{ver}/ subpath 404s).
    PRIMARY_URL = f"https://www.bok.net/Bento4/binaries/{name}"
    tmp = Path(tempfile.gettempdir()) / name

    await ilog(f"   URL: {PRIMARY_URL}", "stdout")
    ok = await download_file(PRIMARY_URL, tmp, f"Bento4 SDK {BENTO4_VER}", iid="bento4")
    if not ok:
        await ilog("✗ Could not download Bento4", "error")
        await ilog("  Download manually: https://www.bento4.com/downloads/", "warn")
        return

    try:
        with zipfile.ZipFile(tmp) as z:
            # Prefer the SDK's bin/ folder; fall back to any .exe in the archive.
            exes = [m for m in z.namelist()
                    if m.lower().endswith(".exe")
                    and "/bin/" in m.replace("\\", "/").lower()]
            if not exes:
                exes = [m for m in z.namelist() if m.lower().endswith(".exe")]
            if not exes:
                await ilog("✗ Bento4 .exe не найдены внутри zip", "error")
                return
            n = 0
            for m in exes:
                (tools_dir / Path(m).name).write_bytes(z.read(m))
                n += 1
            have_dec = (tools_dir / "mp4decrypt.exe").exists()
            have_ext = (tools_dir / "mp4extract.exe").exists()
            ok = have_dec and have_ext
            await ilog(
                f"✓ Bento4: распаковано {n} бинарей в {tools_dir} "
                f"(mp4decrypt={'OK' if have_dec else '✗'}, mp4extract={'OK' if have_ext else '✗'})",
                "success" if ok else "warn")
            if not ok:
                await ilog("⚠ Ключевые бинари Bento4 не извлеклись — ALAC-декрипт может падать.", "warn")
            else:
                await _verify_runs(str(dec), (), "mp4decrypt", banner="Bento4")
                await _verify_runs(str(ext), (), "mp4extract", banner="Bento4")
    except Exception as e:
        await ilog(f"✗ Failed: {e}", "error")


async def install_ffmpeg_windows() -> None:
    """Download a portable FFmpeg (Gyan 'essentials' build) and drop ffmpeg.exe +
    ffprobe.exe into tools/. CRITICAL for the gamdl Apple engine: shells out
    out to a bare `ffmpeg` to remux the decrypted track and reads the output
    WITHOUT checking the return code — so on a machine with no ffmpeg it 'decrypts'
    but never writes a file ('downloaded 0'). No admin needed (plain zip extract)."""
    if tool_path("ffmpeg"):
        await ilog("✓ FFmpeg already present", "success")
        return
    await ilog("📦 Downloading FFmpeg (portable, no admin)…")
    tools_dir = _base_dir / "tools"
    tools_dir.mkdir(exist_ok=True)
    URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
    tmp = Path(tempfile.gettempdir()) / "ffmpeg-release-essentials.zip"
    await ilog(f"   URL: {URL}", "stdout")
    ok = await download_file(URL, tmp, "FFmpeg (essentials)", iid="ffmpeg")
    if not ok:
        await ilog("✗ Could not download FFmpeg", "error")
        await ilog("  Install manually: winget install Gyan.FFmpeg", "warn")
        return
    try:
        wanted = {"ffmpeg.exe", "ffprobe.exe"}
        got = 0
        with zipfile.ZipFile(tmp) as z:
            for member in z.namelist():
                base = member.rsplit("/", 1)[-1].lower()
                if base in wanted and member.lower().endswith(".exe"):
                    (tools_dir / base).write_bytes(z.read(member))
                    got += 1
                    await ilog(f"✓ Extracted {base} → {tools_dir / base}", "success")
        if got:
            # usable immediately this session; on PATH for app subprocesses too
            os.environ["PATH"] = str(tools_dir) + os.pathsep + os.environ.get("PATH", "")
            for exe in ("ffmpeg.exe", "ffprobe.exe"):
                f = tools_dir / exe
                if f.is_file():
                    await _verify_runs(str(f), ("-version",), exe)
        else:
            await ilog("✗ ffmpeg.exe not found inside zip", "error")
    except Exception as e:
        await ilog(f"✗ FFmpeg extract failed: {e}", "error")


# SoundCloud/Lucida runs on Node's global fetch (undici). Node 18's bundled
# undici throws a bare "Error: terminated" on the very first SoundCloud resolve —
# proven live on the tester box: identical runner.mjs fails on Node 18.12.1 and
# succeeds on Node 20.18.1. So we require Node ≥ 20; an older SYSTEM node is not
# good enough and we install a portable v20 beside it.
_MIN_NODE_MAJOR = 20


def _node_version(exe: str) -> int:
    """Return the major version of a node executable (e.g. 20), or 0 if unknown."""
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True,
                             timeout=10, creationflags=_NO_WIN).stdout.strip()
        m = re.match(r"v?(\d+)\.", out)
        return int(m.group(1)) if m else 0
    except Exception:
        return 0


async def _node_provider_sha256(ver: str, fname: str) -> str:
    """Официальный SHA256 с nodejs.org (SHASUMS256.txt лежит рядом с архивом).

    "" — если SHASUMS не ответил: качаем без сверки, но честно об этом говорим.
    """
    try:
        loop = asyncio.get_running_loop()

        def _get():
            with urllib.request.urlopen(
                    f"https://nodejs.org/dist/{ver}/SHASUMS256.txt",
                    timeout=STALL_TIMEOUT) as r:
                return r.read().decode(errors="replace")
        for line in await loop.run_in_executor(None, _get).splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1] == fname:
                return parts[0]
    except Exception as e:
        await ilog(f"   ⚠ SHASUMS256.txt nodejs.org не ответил ({type(e).__name__}) — "
                   f"хэш проверить нечем, ставим как есть.", "warn")
    return ""


async def install_node_windows() -> Optional[str]:
    """Ensure Node.js ≥ 20 is available and return the path to node.exe.

    A fresh PC has no Node at all; some testers have an OLD system Node (18.x)
    whose bundled undici breaks SoundCloud/Lucida with 'terminated'. So:
      1. a portable Node we installed earlier (tools/node, guaranteed ≥20) wins;
      2. a system Node is accepted ONLY if it is ≥20;
      3. otherwise download a portable v20 into tools/node and prepend it to PATH
         (it then shadows the stale system Node for every child process)."""
    tools_dir = _base_dir / "tools"
    node_dir  = tools_dir / "node"
    # 1) Portable Node we control — always new enough.
    portable = node_dir / "node.exe"
    if portable.exists() and _node_version(str(portable)) >= _MIN_NODE_MAJOR:
        if str(node_dir).lower() not in os.environ.get("PATH", "").lower():
            os.environ["PATH"] = str(node_dir) + os.pathsep + os.environ.get("PATH", "")
        await ilog(f"✓ Node.js (portable) present: {portable}", "success")
        return str(portable)
    # 2) System Node — accept only if ≥20.
    sys_node = shutil.which("node")
    if sys_node:
        ver = _node_version(sys_node)
        if ver >= _MIN_NODE_MAJOR:
            await ilog(f"✓ Node.js already present: {sys_node} (v{ver})", "success")
            return sys_node
        await ilog(f"⚠ Системный Node.js v{ver} слишком старый (нужен ≥{_MIN_NODE_MAJOR}) "
                   f"— ставлю portable Node 20 рядом (иначе SoundCloud падает с "
                   f"'terminated').", "warn")
    # 3) Download a portable v20.
    tools_dir.mkdir(exist_ok=True)
    NODE_VER = "v20.18.1"
    arch = "x64" if platform.machine().endswith("64") else "x86"
    name = f"node-{NODE_VER}-win-{arch}"
    url  = f"https://nodejs.org/dist/{NODE_VER}/{name}.zip"
    tmp  = Path(tempfile.gettempdir()) / f"{name}.zip"
    await ilog(f"📦 Downloading Node.js {NODE_VER} (portable, no admin)…")
    await ilog(f"   URL: {url}", "stdout")
    sha = await _node_provider_sha256(NODE_VER, f"{name}.zip")
    ok = await download_file(url, tmp, f"Node.js {NODE_VER}", iid="node",
                             expected_sha256=sha or None)
    if not ok:
        await ilog("✗ Could not download Node.js", "error")
        await ilog("  Install manually: winget install OpenJS.NodeJS.LTS", "warn")
        return None
    try:
        if node_dir.exists():
            shutil.rmtree(node_dir, ignore_errors=True)
        with zipfile.ZipFile(tmp) as z:
            z.extractall(tools_dir)               # creates tools/node-vXX-win-x64/
        extracted = tools_dir / name
        if extracted.exists():
            extracted.rename(node_dir)            # flatten → tools/node
        node_exe = node_dir / "node.exe"
        if node_exe.exists():
            os.environ["PATH"] = str(node_dir) + os.pathsep + os.environ.get("PATH", "")
            if await _verify_runs(str(node_exe), ("--version",), "Node.js"):
                await ilog(f"✓ Node.js installed (portable) → {node_dir}", "success")
                return str(node_exe)
            await ilog("✗ Node.js скачан, но не запускается — SoundCloud на нём не поедет.", "error")
            return None
        await ilog("✗ node.exe missing after extract", "error")
    except Exception as e:
        await ilog(f"✗ Node.js extract failed: {e}", "error")
    return None


# Пин релиза SpotiFLAC: двигать ТОЛЬКО вместе с `pin` в installers_manifest.
SPOTIFLAC_VER = "v1.1.0"


def _spotiflac_url(ext: str = "") -> str:
    """URL релиза из реестра — единственный источник правды для этого бинаря."""
    try:
        from ripster.installers_manifest import by_id
        spec = by_id("spotiflac")
        if spec:
            return spec.url.replace("{ext}", ext)
    except Exception:
        pass
    return ("https://github.com/Nizarberyan/SpotiFLAC/releases/download/"
            f"{SPOTIFLAC_VER}/SpotiFLAC{{ext}}").replace("{ext}", ext)


async def install_spotiflac_windows() -> Optional[str]:
    """SpotiFLAC: до 25.09.2026 ЭТОГО установщика в продукте не было вовсе.

    Движок `ripster/engines/spotiflac.py` умел только сказать «бинарь не найден»,
    а его `download_url()` не вызывал ни один участок кода — то есть кнопка в
    Настройках обещала установку, которой не существовало (прямое нарушение
    правила «настройка, которая ничего не меняет, хуже отсутствующей»).

    Портативный single-file бинарь с оффициального релиза, пин + SHA256 из
    реестра `installers_manifest`, в tools/ рядом с остальными. Возвращает путь.
    """
    tools_dir = _base_dir / "tools"
    ext  = ".exe" if _is_windows else ""
    dest = tools_dir / f"spotiflac{ext}"
    if dest.is_file():
        if await _verify_runs(str(dest), ("--help",), "SpotiFLAC"):
            await ilog(f"✓ SpotiFLAC уже стоит: {dest}", "success")
            return str(dest)
        try:
            dest.unlink()                       # нерабочий бинарь чиним перекачкой
        except Exception:
            pass
    url = _spotiflac_url(ext)
    tmp = Path(tempfile.gettempdir()) / f"SpotiFLAC-{SPOTIFLAC_VER}{ext}"
    ok = await download_file(url, tmp, f"SpotiFLAC {SPOTIFLAC_VER}", iid="spotiflac")
    if not ok:
        await ilog(_manual_hint("spotiflac", url), "warn")
        return None
    try:
        tools_dir.mkdir(exist_ok=True)
        shutil.move(str(tmp), str(dest))
        if _is_windows:
            try:
                os.chmod(dest, 0o755)
            except Exception:
                pass
        if await _verify_runs(str(dest), ("--help",), "SpotiFLAC"):
            await ilog(f"✓ SpotiFLAC установлен → {dest}", "success")
            return str(dest)
        dest.unlink(missing_ok=True)
        await ilog("✗ SpotiFLAC скачан, но не запускается — удалён, чтобы движок "
                   "не падал на нём каждый раз.", "error")
    except Exception as e:
        await ilog(f"✗ SpotiFLAC: установка не удалась — {type(e).__name__}: {e}", "error")
    return None


# ── Widevine L3 toolchain — autonomous, ZERO manual steps ────────────────────
# A fresh PC has none of: JRE, Android SDK, cmdline-tools, emulator, system-image,
# AEHD hypervisor, AVD. The old flow only PRINTED "run silent_install.bat as admin"
# → dead end. This provisions the WHOLE chain itself (one UAC prompt for the kernel
# driver, nothing else). Mirrors the install_node_windows download/extract pattern.
_ANDROID_ROOT = Path(r"C:\Android")
_WVD_AVD      = "wvd"
_WVD_SYS_IMG  = "system-images;android-30;google_apis;x86_64"


def _jre_java() -> Optional[Path]:
    """java.exe under C:\\Android\\jre17 — version-agnostic (don't hardcode 17.0.x)."""
    base = _ANDROID_ROOT / "jre17"
    if base.is_dir():
        for d in list(base.glob("jdk-*")) + [base]:
            j = d / "bin" / "java.exe"
            if j.exists():
                return j
    return None


def _wvd_sdkmgr() -> Path:
    return _ANDROID_ROOT / "Sdk" / "cmdline-tools" / "latest" / "bin" / "sdkmanager.bat"


async def _wvd_install_jre17() -> bool:
    if _jre_java():
        await ilog("│  ✓ JRE 17 уже установлен", "success"); return True
    jre_dir = _ANDROID_ROOT / "jre17"
    jre_dir.mkdir(parents=True, exist_ok=True)
    url = "https://api.adoptium.net/v3/binary/latest/17/ga/windows/x64/jre/hotspot/normal/eclipse"
    tmp = Path(tempfile.gettempdir()) / "temurin17-jre.zip"
    await ilog("│  ⬇ JRE 17 (Adoptium Temurin, ~45 МБ)…", "info")
    ok = await download_file(url, tmp, "JRE 17", iid="jre17")
    if not ok:
        await ilog("│  ✗ Не удалось скачать JRE 17", "error"); return False
    try:
        with zipfile.ZipFile(tmp) as z:
            z.extractall(jre_dir)                 # → jdk-17.x+y-jre/
    except Exception as e:
        await ilog(f"│  ✗ Распаковка JRE: {e}", "error"); return False
    if _jre_java():
        await ilog(f"│  ✓ JRE 17 → {jre_dir}", "success"); return True
    await ilog("│  ✗ java.exe не найден после распаковки", "error"); return False


async def _wvd_install_cmdline_tools() -> bool:
    if _wvd_sdkmgr().exists():
        await ilog("│  ✓ cmdline-tools уже установлены", "success"); return True
    from ripster.installers_manifest import by_id as _cmdline_spec
    spec = _cmdline_spec("cmdline-tools")
    if not spec:
        await ilog("│  ✗ cmdline-tools: нет записи в реестре", "error"); return False
    latest = _ANDROID_ROOT / "Sdk" / "cmdline-tools" / "latest"
    latest.parent.mkdir(parents=True, exist_ok=True)
    url = spec.url
    tmp = Path(tempfile.gettempdir()) / "cmdline-tools.zip"
    await ilog("│  ⬇ Android cmdline-tools…", "info")
    ok = await download_file(url, tmp, "cmdline-tools", iid="cmdline-tools")
    if not ok:
        await ilog("│  ✗ Не удалось скачать cmdline-tools", "error"); return False
    try:
        tmpx = Path(tempfile.mkdtemp())
        with zipfile.ZipFile(tmp) as z:
            z.extractall(tmpx)                    # → cmdline-tools/
        if latest.exists():
            shutil.rmtree(latest, ignore_errors=True)
        shutil.move(str(tmpx / "cmdline-tools"), str(latest))
    except Exception as e:
        await ilog(f"│  ✗ Распаковка cmdline-tools: {e}", "error"); return False
    if _wvd_sdkmgr().exists():
        await ilog(f"│  ✓ cmdline-tools → {latest}", "success"); return True
    await ilog("│  ✗ sdkmanager.bat не найден после распаковки", "error"); return False


async def _wvd_run_sdk_provision() -> bool:
    """Generate + run (windowless) a resilient .bat — accept licenses, install
    platform-tools + emulator + system-image + AEHD, then create the AVD. A .bat
    here is internal (Python runs it automatically); the user never touches it."""
    java, sdkm = _jre_java(), _wvd_sdkmgr()
    if not (java and sdkm.exists()):
        await ilog("│  ✗ JRE/cmdline-tools не готовы", "error"); return False
    java_home = java.parent.parent
    avdm = sdkm.parent / "avdmanager.bat"
    bat  = _ANDROID_ROOT / "_ripster_sdk_provision.bat"
    log  = _ANDROID_ROOT / "sdk_install.log"
    bat.write_text(
        "@echo off\r\n"
        f'set "JAVA_HOME={java_home}"\r\n'
        'set "PATH=%JAVA_HOME%\\bin;%PATH%"\r\n'
        f'set "SDKM={sdkm}"\r\n'
        f'set "AVDM={avdm}"\r\n'
        f'set "LOG={log}"\r\n'
        'echo licenses> "%LOG%"\r\n'
        '(for /l %%i in (1,1,60) do @echo y)| call "%SDKM%" --licenses >> "%LOG%" 2>&1\r\n'
        # Ретрай был БЕСКОНЕЧНЫМ (`goto retry` без счётчика): на мёртвом
        # dl.google.com или отклонённой лицензии прогон молча крутился сутками, а
        # Setup выглядел «зависшим». Теперь ровно 3 попытки и честный финал
        # RIPSTER_SDK_FAILED в логе — вместо вечного `:retry`.
        'for /L %%a in (1,1,3) do ( call :try & if not errorlevel 1 goto :installed )\r\n'
        'echo RIPSTER_SDK_FAILED>> "%LOG%"\r\n'
        'exit /b 1\r\n'
        ':installed\r\n'
        # -d pixel: give the AVD a real device profile. A bare `create avd` defaults
        # to hw.ramSize=96M, which starves Android so badly that connectivity/radio
        # services never come up ("Active default network: none"). The pixel profile
        # sets 2G. We ALSO patch config.ini below as belt-and-suspenders.
        f'echo no | call "%AVDM%" create avd -n {_WVD_AVD} -d pixel -k "{_WVD_SYS_IMG}" --force >> "%LOG%" 2>&1\r\n'
        'echo DONE_MARKER_0>> "%LOG%"\r\n'
        'exit /b 0\r\n'
        ':try\r\n'
        'echo y| call "%SDKM%" "platform-tools" "emulator" '
        f'"{_WVD_SYS_IMG}" "extras;google;Android_Emulator_Hypervisor_Driver" >> "%LOG%" 2>&1\r\n'
        'if "%errorlevel%"=="0" exit /b 0\r\n'
        'timeout /t 15 /nobreak >nul\r\n'
        'exit /b 1\r\n',
        encoding="utf-8")
    await ilog("│  ⚙ sdkmanager: лицензии + platform-tools + emulator + system-image + AEHD + AVD (5–15 мин)…", "info")
    # 3600 с: sdkmanager legitimately качает system-image ~1.5 ГБ; раньше ждали ввек.
    rc, _ = await irun(["cmd", "/c", str(bat)], timeout=3600)
    done = log.exists() and "DONE_MARKER_0" in log.read_text(encoding="utf-8", errors="replace")
    if done:
        _patch_avd_ram()
        await ilog("│  ✓ SDK-пакеты + AVD установлены", "success"); return True
    await ilog(f"│  ✗ sdkmanager не завершился (rc={rc}) — лог {log}", "error"); return False


def _patch_avd_ram() -> None:
    """Ensure the AVD has enough RAM. A bare avdmanager AVD defaults to 96M, which
    starves Android (no network/radio). Force hw.ramSize=2048 + a real device name."""
    try:
        from pathlib import Path as _P
        cfg = _P(os.path.expanduser("~")) / ".android" / "avd" / f"{_WVD_AVD}.avd" / "config.ini"
        if not cfg.is_file():
            return
        import re as _re
        txt = cfg.read_text(encoding="utf-8", errors="replace")
        if _re.search(r"(?m)^\s*hw\.ramSize\s*=", txt):
            txt = _re.sub(r"(?m)^\s*hw\.ramSize\s*=.*$", "hw.ramSize = 2048", txt)
        else:
            txt += "\nhw.ramSize = 2048\n"
        if not _re.search(r"(?m)^\s*hw\.device\.name\s*=", txt):
            txt += "hw.device.name = pixel\n"
        cfg.write_text(txt, encoding="utf-8")
    except Exception:
        pass


async def _wvd_install_aehd() -> bool:
    """Install the AEHD hypervisor driver via pnputil (proven non-interactive).

    The bundled silent_install.bat uses RUNDLL32 InstallHinfSection, which is async
    (the following ``sc start`` races it → 1060) and can need an interactive driver
    dialog. ``pnputil /add-driver <inf> /install`` creates + installs the service in
    one synchronous, UI-less call — verified live on the tester box. Strategy:
      1. direct pnputil — works when the server already holds an admin token
         (headless/SSH/Session-0 with full token);
      2. elevated via Start-Process -Verb RunAs — shows ONE UAC in an interactive
         desktop session (a normal Ripster.exe launch);
      3. self-elevating Install-AEHD.cmd the user double-clicks from Explorer
         (covers background/Session-0 servers that can't raise UAC at all)."""
    def _aehd_running() -> bool:
        try:
            out = subprocess.run(["sc.exe", "query", "aehd"], capture_output=True,
                                 text=True, creationflags=_NO_WIN).stdout or ""
            return "RUNNING" in out
        except Exception:
            return False
    if _aehd_running():
        await ilog("│  ✓ AEHD гипервизор уже работает", "success"); return True
    drv = _ANDROID_ROOT / "Sdk" / "extras" / "google" / "Android_Emulator_Hypervisor_Driver"
    inf = drv / "aehd.inf"
    if not inf.exists():
        await ilog("│  ✗ Драйвер AEHD не найден (шаг sdkmanager неполный)", "error"); return False

    # Always drop a self-elevating helper (pnputil-based) — used as the manual
    # fallback AND as the elevated payload for attempt #2.
    helper = _base_dir / "Install-AEHD.cmd"
    try:
        helper.write_text(
            "@echo off\r\n"
            "net session >nul 2>&1\r\n"
            "if %errorlevel% NEQ 0 (\r\n"
            "  echo Requesting administrator rights...\r\n"
            "  powershell -NoProfile -Command \"Start-Process -Verb RunAs -FilePath '%~f0'\"\r\n"
            "  exit /b\r\n"
            ")\r\n"
            f'pnputil /add-driver "{inf}" /install\r\n'
            "sc start aehd\r\n"
            'sc query aehd | find "RUNNING" >nul && (echo AEHD OK) || (echo AEHD FAILED)\r\n'
            "pause\r\n",
            encoding="utf-8")
    except Exception:
        helper = None

    # 1) Direct (works if we already have an elevated token).
    await ilog("│  🔐 Ставлю AEHD-гипервизор (pnputil)…", "info")
    try:
        subprocess.run(["pnputil", "/add-driver", str(inf), "/install"],
                       capture_output=True, text=True, creationflags=_NO_WIN, timeout=120)
        subprocess.run(["sc.exe", "start", "aehd"], capture_output=True,
                       text=True, creationflags=_NO_WIN)
    except Exception:
        pass
    if _aehd_running():
        await ilog("│  ✓ AEHD гипервизор работает", "success"); return True

    # 2) Elevated via UAC (interactive desktop session). HIDDEN window + no pause —
    # the only thing the user sees is the one-time UAC consent (unavoidable for a
    # kernel driver); no cmd window pops.
    await ilog("│  🔐 Запрашиваю права на драйвер (ОДИН UAC — нажми «Да»)…", "info")
    _drvcmd = f'pnputil /add-driver "{inf}" /install & sc start aehd'
    ps = (f"Start-Process -Verb RunAs -WindowStyle Hidden -Wait -FilePath cmd "
          f"-ArgumentList '/c','{_drvcmd}'")
    await irun(["powershell", "-NoProfile", "-Command", ps], timeout=600)
    if _aehd_running():
        await ilog("│  ✓ AEHD гипервизор работает", "success"); return True

    # 3) Manual fallback — a background/Session-0 server can't raise UAC at all.
    if helper and helper.exists():
        await ilog("│  ⚠ Окно UAC не появилось (фоновый процесс не может его показать).", "warn")
        await ilog(f"│  👉 Запусти вручную (двойной клик → «Да»): {helper}", "warn")
        try:
            subprocess.Popen(["explorer", "/select,", str(helper)], creationflags=_NO_WIN)
        except Exception:
            pass
    else:
        await ilog("│  ⚠ AEHD не установился и не удалось создать Install-AEHD.cmd", "warn")
    return False


async def _ensure_wvd_venv() -> bool:
    """Provision the ISOLATED pywidevine runtime venv (tools/wvdvenv) used by the
    SoundCloud-DRM runner + the device.wvd validator. Kept OUT of the shared bundled
    python on purpose: pywidevine needs protobuf>=6.33, but OrpheusDL pins it down to
    3.15.8 in the shared env (which also breaks the Apple paths). Isolating pywidevine is the only
    robust fix. The runner only imports pywidevine + httpx + mutagen.
    See the ripster-dependency-versions skill."""
    venv = _base_dir / "tools" / "wvdvenv"
    vpy  = venv / ("Scripts/python.exe" if _is_windows else "bin/python")
    try:
        if not vpy.is_file():
            await ilog("│  ⚙ создаю изолированный venv для pywidevine (SC DRM)…", "info")
            await irun([sys.executable, "-m", "venv", str(venv)], timeout=300)
            if not vpy.is_file():
                # The bundled embeddable python ships WITHOUT the stdlib `venv`
                # module → fall back to virtualenv (pip-installable).
                await irun([sys.executable, "-m", "pip", "install", "-q",
                            "--break-system-packages", "virtualenv"], timeout=900)
                await irun([sys.executable, "-m", "virtualenv", str(venv)], timeout=300)
        if not vpy.is_file():
            await ilog("│  ⚠ venv не создан — SC DRM будет на общем python (возможны конфликты)", "warn")
            return False
        # pywidevine==1.9.0 — пин из реестра (requirements.lock): без пина fresh-venv
        # получал последний мажор и расходился с остальным тулчейном (аудит 25.09).
        await irun([str(vpy), "-m", "pip", "install", "-q", "--upgrade",
                    "pip", "pywidevine==1.9.0", "httpx", "mutagen"], timeout=900)
        vrc, vout = await irun([str(vpy), "-c",
                                "from pywidevine.device import Device; print('wvd-venv OK')"], timeout=120)
        if vrc == 0 and "wvd-venv OK" in (vout or ""):
            await ilog("│  ✓ pywidevine venv готов (изолирован от protobuf/construct-конфликтов)",
                       "success")
            return True
        await ilog(f"│  ⚠ pywidevine venv не проверился: {(vout or '')[:120]}", "warn")
        return False
    except Exception as e:
        await ilog(f"│  ⚠ wvd venv: {type(e).__name__}: {e}", "warn")
        return False


async def setup_widevine_toolchain() -> bool:
    """Autonomous L3 Widevine toolchain — JRE 17 + Android cmdline-tools + SDK
    packages (platform-tools/emulator/system-image/AEHD) + AVD + AEHD driver, with
    ZERO manual steps (one UAC for the kernel driver). Idempotent. After this the
    WVD minter (Settings → SoundCloud) can boot the emulator and extract device.wvd."""
    await ilog("┌─ Widevine L3 (SoundCloud DRM) — автоустановка тулчейна", "info")
    if platform.system() != "Windows":
        await ilog("└─ ✗ Только Windows", "error"); return False
    # The isolated pywidevine RUNTIME (tools/wvdvenv) is needed to USE device.wvd,
    # independent of minting — ensure it first, even if the .wvd already exists.
    await _ensure_wvd_venv()
    # The minting TOOLCHAIN (JRE + Android SDK + emulator + AEHD) exists ONLY to mint
    # device.wvd. If it's already minted, skip everything — re-running sdkmanager
    # on an already-provisioned box silently re-verifies for ~15 min (looks frozen).
    try:
        _wvd_dst = _base_dir / "tools" / "widevine" / "device.wvd"
        if _wvd_dst.is_file() and _wvd_dst.stat().st_size > 0:
            await ilog("└─ ✓ device.wvd уже есть — тулчейн не нужен, минт уже выполнен. Пропускаю.",
                       "success")
            return True
    except Exception:
        pass
    try:
        if not await _wvd_install_jre17():         return False
        if not await _wvd_install_cmdline_tools(): return False
        if not await _wvd_run_sdk_provision():     return False
        # AEHD only HW-accelerates the emulator; minting still works without it
        # (just slower). So it's non-fatal — but report honestly, never claim a
        # green toolchain when the hypervisor didn't actually come up.
        aehd_ok = await _wvd_install_aehd()
        if aehd_ok:
            await ilog("└─ ✓ WVD-тулчейн готов (с AEHD-ускорением). "
                       "Минт device.wvd — кнопкой в Настройках → SoundCloud.", "success")
        else:
            await ilog("└─ ⚠ Тулчейн установлен, но AEHD-гипервизор НЕ активен — эмулятор "
                       "запустится, но медленно (или не стартует). Причины: UAC отклонён, "
                       "нужна перезагрузка, либо включён Hyper-V/виртуализация выключена в BIOS. "
                       "Можно пробовать минт device.wvd; если эмулятор висит — включи "
                       "виртуализацию/перезагрузись и повтори установку AEHD.", "warn")
        return True
    except Exception as e:
        await ilog(f"└─ ✗ WVD setup: {e}", "error"); return False


async def ensure_git() -> Optional[str]:
    """Best-effort install Git per-user via winget if missing (no admin). Git is
    required to clone the Apple downloader; a fresh PC usually has neither."""
    git = tool_path("git")
    if git:
        return git
    if not shutil.which("winget"):
        await ilog("✗ Git missing and winget unavailable — install: https://git-scm.com", "error")
        return None
    await ilog("📦 Git not found — installing via winget…")
    rc, _ = await irun(["winget", "install", "-e", "--id", "Git.Git", "--silent",
                        "--accept-package-agreements", "--accept-source-agreements"],
                       timeout=900)
    # winget's Git-for-Windows installer lands in Program Files (or per-user). Our
    # running process still has the OLD PATH, so refresh it from the registry and
    # probe the known install locations directly.
    try:
        import winreg
        for hive, sub in ((winreg.HKEY_LOCAL_MACHINE,
                           r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
                          (winreg.HKEY_CURRENT_USER, "Environment")):
            try:
                with winreg.OpenKey(hive, sub) as k:
                    val, _ = winreg.QueryValueEx(k, "Path")
                    os.environ["PATH"] = os.environ.get("PATH", "") + os.pathsep + val
            except Exception:
                pass
    except Exception:
        pass
    for c in (r"C:\Program Files\Git\cmd\git.exe",
              r"C:\Program Files (x86)\Git\cmd\git.exe",
              os.path.expandvars(r"%LOCALAPPDATA%\Programs\Git\cmd\git.exe")):
        if os.path.isfile(c):
            os.environ["PATH"] = os.path.dirname(c) + os.pathsep + os.environ["PATH"]
            await ilog("✓ Git installed", "success")
            return c
    git = shutil.which("git") or tool_path("git")
    if git:
        await ilog("✓ Git installed", "success")
    else:
        await ilog("✗ Git installed but not visible yet — restart Ripster and retry",
                   "error")
    return git


async def clone_pinned(iid: str, *, dest: Path,
                       preserve_existing: bool = False) -> bool:
    """Clone *iid*'s repo and check out the pinned commit from the registry.

    One function for every git-clone in the product — the manifest holds the pin,
    this helper enforces it.  If the commit is unreachable the install fails with
    a clear log line instead of silently landing on HEAD.

    *dest* — target directory.  When ``dest/.git`` exists the pin is enforced
    in-place (fetch + checkout).  Otherwise the repo is cloned into a temp
    directory next to *dest*, the pin is checked out there, and the contents are
    merged into *dest* (``preserve_existing`` skips files already present —
    used by OrpheusDL which keeps ``_auth_helper.py`` in the same folder).

    Returns True on success, False on any failure (reason → install_log).
    """
    from ripster.installers_manifest import by_id
    spec = by_id(iid)
    if not spec or not spec.pin:
        await ilog(f"✗ {iid}: нет пина в реестре — установка прервана", "error")
        return False
    pin = spec.pin
    url = spec.url
    git = await ensure_git() or "git"

    if (dest / ".git").is_dir():
        await ilog(f"⟳ {iid}: обновляю и фиксирую на {pin[:8]}…", "info")
        rc, _ = await irun([git, "fetch", "--all"], cwd=str(dest), timeout=300)
        if rc != 0:
            await ilog(f"✗ {iid}: git fetch failed (exit {rc})", "error")
            return False
        rc, _ = await irun([git, "checkout", pin], cwd=str(dest), timeout=120)
        if rc != 0:
            await ilog(f"✗ {iid}: коммит {pin[:8]} недоступен — обновите пин в реестре",
                       "error")
            return False
    else:
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.parent / (dest.name + "_clone_tmp")
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)
        await ilog(f"⬇ Клонирую {iid}…", "info")
        rc, _ = await irun([git, "clone", url, str(tmp)], timeout=600)
        if rc != 0:
            await ilog(f"✗ {iid}: git clone failed (exit {rc})", "error")
            return False
        rc, _ = await irun([git, "checkout", pin], cwd=str(tmp), timeout=120)
        if rc != 0:
            shutil.rmtree(tmp, ignore_errors=True)
            await ilog(f"✗ {iid}: коммит {pin[:8]} недоступен — обновите пин в реестре",
                       "error")
            return False
        dest.mkdir(parents=True, exist_ok=True)
        for item in tmp.iterdir():
            dst = dest / item.name
            if preserve_existing and dst.exists():
                continue
            if item.is_dir():
                shutil.copytree(str(item), str(dst), dirs_exist_ok=True)
            else:
                shutil.copy2(str(item), str(dst))
        shutil.rmtree(tmp, ignore_errors=True)

    rc, sha = await irun([git, "rev-parse", "HEAD"], cwd=str(dest), timeout=30)
    if rc == 0 and sha.strip() != pin:
        await ilog(f"✗ {iid}: пин {pin[:8]} не совпал с HEAD {sha.strip()[:8]}", "error")
        return False
    await ilog(f"✓ {iid}: пин {pin[:8]} зафиксирован", "success")
    return True


async def clone_downloader() -> bool:
    """Clone or update zhaarey/apple-music-downloader next to app.py."""
    main_go = _base_dir / "main.go"
    if main_go.exists():
        await ilog("✓ main.go already present — skipping clone", "success")
        return True
    await ilog("📥 Cloning zhaarey/apple-music-downloader…")
    if not await clone_pinned("zhaarey", dest=_base_dir, preserve_existing=True):
        return False
    _cfg["main-go-path"] = str(_base_dir / "main.go")
    if _save_config:
        _save_config(_cfg)
    await ilog(f"✓ Cloned to {_base_dir / 'main.go'}", "success")
    return True


async def go_mod_download() -> None:
    """Run go mod download to fetch Go dependencies."""
    go = tool_path("go") or find_go()
    if not go or (not shutil.which(go) and not os.path.isfile(go)):
        await ilog("⚠ go not found, skipping go mod download", "warn")
        return
    main_go = Path(_cfg.get("main-go-path", _base_dir / "main.go"))
    if not main_go.exists():
        await ilog("⚠ main.go not found, skipping go mod download", "warn")
        return
    await ilog("📦 Running go mod download…")
    rc, _ = await irun([go, "mod", "download"], cwd=str(main_go.parent), timeout=900)
    if rc == 0:
        await ilog("✓ Go modules downloaded", "success")
    else:
        await ilog(f"⚠ go mod download exit {rc} (may still work)", "warn")


# ─── master setup routine ─────────────────────────────────────────────────────

async def _run_full_setup_inner() -> None:
    global _need_restart, _gamdl_flags
    _need_restart = False
    install_log.clear()

    await ilog("══════════════════════════════════════════", "info")
    await ilog("   🚀  Ripster — Auto-Setup", "info")
    await ilog("══════════════════════════════════════════", "info")
    await ilog(f"   Platform : {platform.system()} {platform.machine()}", "info")
    await ilog(f"   App dir  : {_base_dir}", "info")
    await ilog("", "info")

    engine = _cfg.get("engine", "zhaarey")
    await ilog(f"   Engine   : {engine}", "info")
    await ilog("", "info")

    tools = await check_tools()
    if _broadcast:
        await _broadcast({"type": "tools_status", "tools": tools})

    # ── Step 1: Go / gamdl ───────────────────────────────────────────────────
    await istep("go", "running")
    if engine == "gamdl":
        await ilog("┌─ Step 1/5 : gamdl Python package", "info")
        # Пин из реестра: раньше стояло `pip install gamdl --upgrade` БЕЗ версии,
        # и один клик уносил движок на новый мажор посреди рабочей недели (25.09).
        from ripster.installers_manifest import by_id as _spec_by_id
        _gpin = getattr(_spec_by_id("pip-gamdl"), "pin", "") or ""
        _gspec = f"gamdl=={_gpin}" if _gpin else "gamdl"
        rc1, out1 = await irun([sys.executable, "-m", "pip", "install",
                                 _gspec, "--upgrade", "--break-system-packages", "-q"],
                               timeout=900)
        if rc1 != 0:
            await ilog(f"│  ⚠ gamdl install: {out1[:100]}", "warn")
        await ilog("│  Upgrading protobuf (required by pywidevine)…", "info")
        await irun([sys.executable, "-m", "pip", "install",
                    "protobuf>=4.21.0", "--upgrade", "--break-system-packages", "-q"],
                   timeout=900)
        _gamdl_flags = set()
        verify_rc, verify_out = await irun([sys.executable, "-c",
            "import gamdl; print('gamdl', gamdl.__version__)"], timeout=120)
        if verify_rc == 0:
            await ilog(f"│  ✓ {verify_out.strip()}", "success")
            api_rc, _ = await irun([sys.executable, "-c",
                "from gamdl.api import AppleMusicApi; print('API OK')"], timeout=120)
            if api_rc != 0:
                await ilog("│  ⚠ Older gamdl API — some features may differ", "warn")
            await istep("go", "done")
        else:
            await ilog("│  ✗ gamdl failed:", "error")
            for line in verify_out.splitlines()[-5:]:
                await ilog(f"│    {line}", "error")
            await istep("go", "error")
        await ilog("└" + "─" * 42, "info")
        await ilog("", "info")
    elif not tools["go"]["found"]:
        await ilog("┌─ Step 1/5 : Installing Go runtime", "info")
        if _is_windows:
            await install_go_windows()
            if _need_restart:
                await ilog("│  ⚠ PATH will update after app restart", "warn")
        else:
            await ilog("│  ✗ Go not found — install manually:", "error")
            await ilog("│    Linux : sudo apt install golang-go", "warn")
            await ilog("│    Mac   : brew install go", "warn")
            await ilog("│    URL   : https://go.dev/dl/", "warn")
        await istep("go", "done" if (tool_path("go") or _need_restart) else "error")
    else:
        await ilog(f"┌─ Step 1/5 : Go — already installed", "success")
        await ilog(f"│  {tools['go']['version']}", "info")
        await istep("go", "skip")
    await ilog("└" + "─" * 42, "info")
    await ilog("", "info")

    # ── Step 2: Downloader source ────────────────────────────────────────────
    await istep("downloader", "running")
    if not tools["downloader"]["found"]:
        await ilog("┌─ Step 2/5 : Cloning apple-music-downloader", "info")
        ok = await clone_downloader()
        await istep("downloader", "done" if ok else "error")
    else:
        await ilog(f"┌─ Step 2/5 : main.go — already present", "success")
        await ilog(f"│  {tools['downloader']['path']}", "info")
        await istep("downloader", "skip")
    await ilog("└" + "─" * 42, "info")
    await ilog("", "info")

    # ── Step 3: MP4Box ───────────────────────────────────────────────────────
    await istep("MP4Box", "running")
    if not tools["MP4Box"]["found"]:
        await ilog("┌─ Step 3/5 : Installing MP4Box (GPAC)", "info")
        if _is_windows:
            await install_gpac_windows()
        else:
            await ilog("│  ✗ MP4Box not found — install manually:", "error")
            await ilog("│    Linux : sudo apt install gpac", "warn")
            await ilog("│    Mac   : brew install gpac", "warn")
            await ilog("│    URL   : https://gpac.io/downloads/", "warn")
        await istep("MP4Box", "done" if tool_path("MP4Box") else "error")
    else:
        await ilog(f"┌─ Step 3/5 : MP4Box — already installed", "success")
        await ilog(f"│  {tools['MP4Box']['version']}", "info")
        await istep("MP4Box", "skip")
    await ilog("└" + "─" * 42, "info")
    await ilog("", "info")

    # ── Step 4: mp4decrypt ───────────────────────────────────────────────────
    await istep("mp4decrypt", "running")
    if not tools["mp4decrypt"]["found"]:
        await ilog("┌─ Step 4/5 : Installing mp4decrypt (Bento4) [optional]", "info")
        if _is_windows:
            await install_mp4decrypt_windows()
        else:
            await ilog("│  ⚠ mp4decrypt not found (only needed for MV)", "warn")
            await ilog("│    URL: https://www.bento4.com/downloads/", "warn")
        await istep("mp4decrypt", "done" if tool_path("mp4decrypt") else "warn")
    else:
        await ilog(f"┌─ Step 4/5 : mp4decrypt — already installed", "success")
        await ilog(f"│  {tools['mp4decrypt']['version']}", "info")
        await istep("mp4decrypt", "skip")
    await ilog("└" + "─" * 42, "info")
    await ilog("", "info")

    # ── Step 5: FFmpeg (Apple-ремукс — без него Apple «декриптит 0 файлов») ───
    await istep("ffmpeg", "running")
    if not tool_path("ffmpeg"):
        await ilog("┌─ Step 5/5 : Installing FFmpeg (Apple remux)", "info")
        if _is_windows:
            await install_ffmpeg_windows()
        else:
            await ilog("│  ⚠ ffmpeg not found — install via your package manager", "warn")
        await istep("ffmpeg", "done" if tool_path("ffmpeg") else "warn")
    else:
        await ilog("┌─ Step 5/5 : FFmpeg — already installed", "success")
        await ilog(f"│  {tools.get('ffmpeg', {}).get('version', '')}", "info")
        await istep("ffmpeg", "skip")
    await ilog("└" + "─" * 42, "info")
    await ilog("", "info")

    # ── Go mod download ──────────────────────────────────────────────────────
    if not _need_restart:
        await ilog("┌─ Bonus    : go mod download", "info")
        await go_mod_download()
        await ilog("└" + "─" * 42, "info")
        await ilog("", "info")

    # ── Final summary ────────────────────────────────────────────────────────
    tools2 = await check_tools()
    if _broadcast:
        await _broadcast({"type": "tools_status", "tools": tools2})
    missing = [k for k, v in tools2.items() if v["required"] and not v["found"]]

    await ilog("══════════════════════════════════════════", "info")
    if _need_restart:
        await ilog("  ⚠  RESTART REQUIRED", "warn")
        await ilog("", "info")
        await ilog("  Go was installed. Close this terminal,", "warn")
        await ilog("  then run:  python app.py  again.", "warn")
        await ilog("  The PATH will update on restart.", "warn")
    elif missing:
        await ilog("  ⚠  Some tools still missing:", "warn")
        for m in missing:
            await ilog(f"     ✗ {tools2[m]['label']}", "error")
        await ilog("  Install manually and restart app.py", "warn")
    else:
        await ilog("  ✅  All dependencies ready!", "success")
        await ilog("  You can start downloading now.", "success")
    await ilog("══════════════════════════════════════════", "info")

    if _broadcast:
        await _broadcast({"type": "setup_done", "missing": missing, "need_restart": _need_restart})


async def run_full_setup() -> None:
    """Wrapper that catches all exceptions and reports them."""
    try:
        await _run_full_setup_inner()
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print(f"[setup] FATAL ERROR:\n{tb}", flush=True)
        await ilog(f"✗ FATAL ERROR: {e}", "error")
        for line in tb.splitlines():
            await ilog(f"  {line}", "error")
        if _broadcast:
            await _broadcast({"type": "setup_done", "missing": ["error"], "need_restart": False})


__all__ = [
    "install",
    "install_log",
    "ilog", "istep", "irun",
    "check_tools", "tool_path",
    "find_go", "check_docker_installed",
    "_gamdl_flag", "_build_env",
    "download_file",
    "install_go_windows", "install_gpac_windows", "install_mp4decrypt_windows",
    "install_ffmpeg_windows", "install_node_windows", "install_spotiflac_windows",
    "setup_widevine_toolchain",
    "clone_downloader", "go_mod_download",
    "run_full_setup",
]
