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


async def install_node_windows() -> Optional[str]:
    """Download a portable Node.js LTS into tools/node (no admin) and return the
    path to node.exe. SoundCloud's Lucida engine needs node + npm; a fresh PC has
    neither, so the SC install/build dies with 'node not found'. Extracts the zip,
    flattens node-vXX-win-x64/ → tools/node, and puts it on PATH for this session."""
    node_exe = tool_path("node")
    if node_exe:
        await ilog(f"✓ Node.js already present: {node_exe}", "success")
        return node_exe
    tools_dir = _base_dir / "tools"
    node_dir  = tools_dir / "node"
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
    "clone_downloader", "go_mod_download",
    "run_full_setup",
]
