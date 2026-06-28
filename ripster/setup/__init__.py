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
    download_file(url, dest, label)        — download with progress
    download_file_no_ssl(url, dest, label) — download, SSL relaxed
    install_go_windows()
    install_gpac_windows()
    install_mp4decrypt_windows()
    clone_downloader()
    go_mod_download()
"""
from __future__ import annotations

import asyncio
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Optional

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
        gamdl_exe = shutil.which("gamdl") or "gamdl"
        r = subprocess.run(
            [gamdl_exe, "--help"],
            capture_output=True, text=True, timeout=10,
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
                    capture_output=True, text=True, timeout=5,
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


async def irun(cmd: list, cwd: Optional[str] = None) -> tuple[int, str]:
    """Run a command, stream every line to setup console, return (rc, output)."""
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

    out: list[str] = []
    async for raw in proc.stdout:
        line = raw.decode(errors="replace").rstrip()
        if line:
            out.append(line)
            await ilog(line, "stdout")
    await proc.wait()
    return proc.returncode, "\n".join(out)


# ─── download helpers ─────────────────────────────────────────────────────────

async def download_file(url: str, dest: Path, label: str = "") -> bool:
    """Download url→dest with live KB/s progress. Thread-safe."""
    label = label or dest.name
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

    def _blocking_dl():
        opener = urllib.request.build_opener()
        opener.addheaders = [("User-Agent", "Mozilla/5.0 amd-downloader")]
        with opener.open(url) as resp, open(dest, "wb") as out_f:
            total_size = int(resp.headers.get("Content-Length", 0))
            block, count = 8192, 0
            while chunk := resp.read(block):
                out_f.write(chunk)
                count += 1
                _reporthook(count, block, total_size)

    try:
        await loop.run_in_executor(None, _blocking_dl)
        size_mb = dest.stat().st_size / 1_048_576
        await ilog(f"  ✓ Saved {dest.name} ({size_mb:.1f} MB)", "success")
        return True
    except Exception as e:
        await ilog(f"  ✗ Download failed: {e}", "error")
        print(f"[download] ERROR: {e}", flush=True)
        return False


async def download_file_no_ssl(url: str, dest: Path, label: str = "") -> bool:
    """Same as download_file but with SSL cert verification disabled."""
    import ssl
    label = label or dest.name
    await ilog(f"⬇ Downloading {label} (SSL relaxed)…", "info")
    loop = asyncio.get_running_loop()
    ctx  = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode    = ssl.CERT_NONE

    def _blocking():
        opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx))
        opener.addheaders = [("User-Agent", "Mozilla/5.0 amd-downloader")]
        with opener.open(url, timeout=30) as r, open(dest, "wb") as f:
            total    = int(r.headers.get("content-length", 0))
            done     = 0
            last_pct = -1
            while True:
                chunk = r.read(65536)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if total > 0:
                    pct = min(100, int(done * 100 / total))
                    if pct % 10 == 0 and pct != last_pct:
                        last_pct = pct

    try:
        await loop.run_in_executor(None, _blocking)
        size_mb = dest.stat().st_size / 1_048_576
        await ilog(f"  ✓ Saved {dest.name} ({size_mb:.1f} MB)", "success")
        return True
    except Exception as e:
        await ilog(f"  ✗ Failed: {e}", "error")
        return False


# ─── platform installers ──────────────────────────────────────────────────────

async def install_go_windows() -> None:
    """Download and install Go on Windows silently."""
    global _need_restart
    await ilog("📦 Fetching latest Go version info…")
    try:
        with urllib.request.urlopen("https://go.dev/VERSION?m=text", timeout=10) as r:
            ver = r.read().decode().strip().split("\n")[0].strip()
    except Exception:
        ver = "go1.22.4"
    await ilog(f"   Latest Go: {ver}")
    arch = "amd64" if platform.machine().endswith("64") else "386"
    # Use the PORTABLE zip, not the MSI: the MSI needs elevation and returned
    # 1603 on a normal (non-admin) install. The zip extracts locally, no admin.
    url  = f"https://go.dev/dl/{ver}.windows-{arch}.zip"
    tmp  = Path(tempfile.gettempdir()) / f"{ver}.windows-{arch}.zip"
    ok   = await download_file(url, tmp, f"Go {ver} (portable zip)")
    if not ok:
        await ilog("   Please install manually: https://go.dev/dl/", "warn")
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
            await ilog(f"✓ Go installed (portable) → {go_root}", "success")
        else:
            await ilog("✗ Go extracted but go.exe missing", "error")
    except Exception as e:
        await ilog(f"✗ Go extract failed: {e}", "error")
        await ilog("   Try manual install: https://go.dev/dl/", "warn")


async def install_gpac_windows() -> None:
    """Download GPAC from the official gpac.io permalink — always the latest build."""
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
    ok  = await download_file(GPAC_NIGHTLY_URL, tmp, "GPAC latest nightly build")
    if not ok:
        await ilog("   Nightly failed, trying stable 26.02…", "stdout")
        ok = await download_file(GPAC_STABLE_URL, tmp, "GPAC 26.02 stable")
    if not ok:
        await ilog("✗ GPAC download failed", "error")
        await ilog("   Download manually: https://gpac.io/downloads/gpac-nightly-builds/", "warn")
        return

    await ilog("🔧 Running GPAC installer (silent)… this may take 10–30 seconds", "info")
    rc, _ = await irun([str(tmp), "/S"])
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

    AMD's mp4.py extract_song shells out to BOTH `mp4decrypt` AND `mp4extract`
    (ALAC: `mp4extract …/alac …`). Extracting only mp4decrypt.exe (the old
    behaviour) left mp4extract.exe missing → every ALAC track died at the decrypt
    step with a cryptic `[WinError 2]` in EVERY region. Grab all bin/*.exe."""
    await ilog("📦 Downloading Bento4 SDK (mp4decrypt + mp4extract + …)…")
    tools_dir = _base_dir / "tools"
    tools_dir.mkdir(exist_ok=True)

    BENTO4_VER  = "1-6-0-641"
    name        = f"Bento4-SDK-{BENTO4_VER}.x86_64-microsoft-win32.zip"
    # bok.net serves the zip directly under /binaries/ (the /{ver}/ subpath 404s).
    PRIMARY_URL = f"https://www.bok.net/Bento4/binaries/{name}"
    tmp = Path(tempfile.gettempdir()) / name

    await ilog(f"   URL: {PRIMARY_URL}", "stdout")
    ok = await download_file(PRIMARY_URL, tmp, f"Bento4 SDK {BENTO4_VER}")
    if not ok:
        await ilog("   Retrying with relaxed SSL…", "stdout")
        ok = await download_file_no_ssl(PRIMARY_URL, tmp, f"Bento4 SDK {BENTO4_VER} (no-ssl)")
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
    except Exception as e:
        await ilog(f"✗ Failed: {e}", "error")


async def install_ffmpeg_windows() -> None:
    """Download a portable FFmpeg (Gyan 'essentials' build) and drop ffmpeg.exe +
    ffprobe.exe into tools/. CRITICAL for the AMD/gamdl Apple engines: AMD shells
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
    ok = await download_file(URL, tmp, "FFmpeg (essentials)")
    if not ok:
        await ilog("   Retrying with relaxed SSL…", "stdout")
        ok = await download_file_no_ssl(URL, tmp, "FFmpeg (no-ssl)")
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
                             timeout=10).stdout.strip()
        m = re.match(r"v?(\d+)\.", out)
        return int(m.group(1)) if m else 0
    except Exception:
        return 0


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
    ok = await download_file(url, tmp, f"Node.js {NODE_VER}")
    if not ok:
        ok = await download_file_no_ssl(url, tmp, f"Node.js {NODE_VER} (no-ssl)")
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
            await ilog(f"✓ Node.js installed (portable) → {node_dir}", "success")
            return str(node_exe)
        await ilog("✗ node.exe missing after extract", "error")
    except Exception as e:
        await ilog(f"✗ Node.js extract failed: {e}", "error")
    return None


# ── Widevine L3 toolchain — autonomous, ZERO manual steps ────────────────────
# A fresh PC has none of: JRE, Android SDK, cmdline-tools, emulator, system-image,
# AEHD hypervisor, AVD. The old flow only PRINTED "run silent_install.bat as admin"
# → dead end. This provisions the WHOLE chain itself (one UAC prompt for the kernel
# driver, nothing else). Mirrors the install_node_windows download/extract pattern.
_ANDROID_ROOT = Path(r"C:\Android")
_WVD_AVD      = "wvd"
_WVD_SYS_IMG  = "system-images;android-30;google_apis;x86_64"
_NO_WIN       = 0x08000000  # CREATE_NO_WINDOW


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
    ok = await download_file(url, tmp, "JRE 17") or await download_file_no_ssl(url, tmp, "JRE 17 (no-ssl)")
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
    latest = _ANDROID_ROOT / "Sdk" / "cmdline-tools" / "latest"
    latest.parent.mkdir(parents=True, exist_ok=True)
    url = "https://dl.google.com/android/repository/commandlinetools-win-11076708_latest.zip"
    tmp = Path(tempfile.gettempdir()) / "cmdline-tools.zip"
    await ilog("│  ⬇ Android cmdline-tools…", "info")
    ok = await download_file(url, tmp, "cmdline-tools") or await download_file_no_ssl(url, tmp, "cmdline-tools (no-ssl)")
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
        ':retry\r\n'
        'echo y| call "%SDKM%" "platform-tools" "emulator" '
        f'"{_WVD_SYS_IMG}" "extras;google;Android_Emulator_Hypervisor_Driver" >> "%LOG%" 2>&1\r\n'
        'if not "%errorlevel%"=="0" ( timeout /t 15 /nobreak >nul & goto retry )\r\n'
        # -d pixel: give the AVD a real device profile. A bare `create avd` defaults
        # to hw.ramSize=96M, which starves Android so badly that connectivity/radio
        # services never come up ("Active default network: none"). The pixel profile
        # sets 2G. We ALSO patch config.ini below as belt-and-suspenders.
        f'echo no | call "%AVDM%" create avd -n {_WVD_AVD} -d pixel -k "{_WVD_SYS_IMG}" --force >> "%LOG%" 2>&1\r\n'
        'echo DONE_MARKER_0>> "%LOG%"\r\n',
        encoding="utf-8")
    await ilog("│  ⚙ sdkmanager: лицензии + platform-tools + emulator + system-image + AEHD + AVD (5–15 мин)…", "info")
    rc, _ = await irun(["cmd", "/c", str(bat)])
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
    await irun(["powershell", "-NoProfile", "-Command", ps])
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
    3.15.8 in the shared env (which also breaks AMD). Isolating pywidevine is the only
    robust fix. The runner only imports pywidevine + httpx + mutagen.
    See the ripster-dependency-versions skill."""
    venv = _base_dir / "tools" / "wvdvenv"
    vpy  = venv / ("Scripts/python.exe" if _is_windows else "bin/python")
    try:
        if not vpy.is_file():
            await ilog("│  ⚙ создаю изолированный venv для pywidevine (SC DRM)…", "info")
            await irun([sys.executable, "-m", "venv", str(venv)])
            if not vpy.is_file():
                # The bundled embeddable python ships WITHOUT the stdlib `venv`
                # module → fall back to virtualenv (pip-installable).
                await irun([sys.executable, "-m", "pip", "install", "-q",
                            "--break-system-packages", "virtualenv"])
                await irun([sys.executable, "-m", "virtualenv", str(venv)])
        if not vpy.is_file():
            await ilog("│  ⚠ venv не создан — SC DRM будет на общем python (возможны конфликты)", "warn")
            return False
        await irun([str(vpy), "-m", "pip", "install", "-q", "--upgrade",
                    "pip", "pywidevine", "httpx", "mutagen"])
        vrc, vout = await irun([str(vpy), "-c",
                                "from pywidevine.device import Device; print('wvd-venv OK')"])
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
                        "--accept-package-agreements", "--accept-source-agreements"])
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


async def clone_downloader() -> bool:
    """Clone or update zhaarey/apple-music-downloader next to app.py."""
    main_go = _base_dir / "main.go"
    git     = await ensure_git() or "git"
    if main_go.exists():
        await ilog("✓ main.go already present — skipping clone", "success")
        return True
    await ilog("📥 Cloning zhaarey/apple-music-downloader…")
    tmp_dir = _base_dir / "_amd_clone"
    rc, _   = await irun([git, "clone", "--depth=1",
                           "https://github.com/zhaarey/apple-music-downloader.git",
                           str(tmp_dir)])
    if rc != 0:
        await ilog(f"✗ git clone failed (exit {rc})", "error")
        return False
    for item in tmp_dir.iterdir():
        dst = _base_dir / item.name
        if not dst.exists():
            if item.is_dir():
                shutil.copytree(item, dst)
            else:
                shutil.copy2(item, dst)
    shutil.rmtree(tmp_dir, ignore_errors=True)
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
    rc, _ = await irun([go, "mod", "download"], cwd=str(main_go.parent))
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

    engine = _cfg.get("engine", "amd")
    await ilog(f"   Engine   : {engine}", "info")
    await ilog("", "info")

    tools = await check_tools()
    if _broadcast:
        await _broadcast({"type": "tools_status", "tools": tools})

    # ── AMD engine: clone AppleMusicDecrypt + install its deps (the DEFAULT,
    # public Apple path — no Apple ID, no Docker). Done here so the single
    # "Auto-install everything" button makes the default engine actually work.
    # Previously AMD lived ONLY in a separate /api/setup/amd call, so a fresh
    # user's first Apple download died with "AppleMusicDecrypt не установлен"
    # (no tester had a working Apple download — they only ran Setup, not a DL).
    if engine == "amd":
        from ripster import amd as _amd
        await istep("amd", "running")
        await ilog("┌─ AMD      : AppleMusicDecrypt (public Apple wrapper, no Apple ID)", "info")
        await ensure_git()                       # clone needs git on a clean PC
        if await _amd.clone_amd() and await _amd.install_amd_deps():
            await ilog("│  ✓ AppleMusicDecrypt ready", "success")
            await istep("amd", "done")
            if _broadcast:
                await _broadcast({"type": "amd_ready"})
        else:
            await ilog("│  ✗ AppleMusicDecrypt setup failed — see log above", "error")
            await istep("amd", "error")
        await ilog("└" + "─" * 42, "info")
        await ilog("", "info")

    # ── Step 1: Go / gamdl ───────────────────────────────────────────────────
    await istep("go", "running")
    if engine == "gamdl":
        await ilog("┌─ Step 1/5 : gamdl Python package", "info")
        rc1, out1 = await irun([sys.executable, "-m", "pip", "install",
                                 "gamdl", "--upgrade", "--break-system-packages", "-q"])
        if rc1 != 0:
            await ilog(f"│  ⚠ gamdl install: {out1[:100]}", "warn")
        await ilog("│  Upgrading protobuf (required by pywidevine)…", "info")
        await irun([sys.executable, "-m", "pip", "install",
                    "protobuf>=4.21.0", "--upgrade", "--break-system-packages", "-q"])
        _gamdl_flags = set()
        verify_rc, verify_out = await irun([sys.executable, "-c",
            "import gamdl; print('gamdl', gamdl.__version__)"])
        if verify_rc == 0:
            await ilog(f"│  ✓ {verify_out.strip()}", "success")
            api_rc, _ = await irun([sys.executable, "-c",
                "from gamdl.api import AppleMusicApi; print('API OK')"])
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
    elif engine == "amd":
        await ilog("┌─ Step 1/5 : Go runtime — not needed for amd engine", "success")
        await istep("go", "skip")
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
    if engine == "amd":
        await ilog("┌─ Step 2/5 : Go downloader — not needed for amd engine", "success")
        await istep("downloader", "skip")
    elif not tools["downloader"]["found"]:
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

    # ── Step 5: FFmpeg (AMD/gamdl remux — without it Apple "decrypts 0 files") ─
    await istep("ffmpeg", "running")
    if not tool_path("ffmpeg"):
        await ilog("┌─ Step 5/5 : Installing FFmpeg (Apple/AMD remux)", "info")
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
    "download_file", "download_file_no_ssl",
    "install_go_windows", "install_gpac_windows", "install_mp4decrypt_windows",
    "install_ffmpeg_windows", "install_node_windows",
    "setup_widevine_toolchain",
    "clone_downloader", "go_mod_download",
    "run_full_setup",
]
