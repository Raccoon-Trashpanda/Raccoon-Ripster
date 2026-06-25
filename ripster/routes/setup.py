"""
Setup, tools, wrapper and AMD management routes.

  GET  /api/tools               — check installed tools
  POST /api/setup               — run full auto-installer
  POST /api/setup/amd           — clone + install AMD v2
  GET  /api/amd/status          — AMD clone status
  GET  /api/amd/wrapper-status  — gRPC wrapper-manager status
  GET  /api/wrapper-status      — Docker wrapper health
  POST /api/wrapper/start       — start wrapper container
  POST /api/wrapper/stop        — stop wrapper container
  POST /api/wrapper/pull        — pull wrapper image
  POST /api/wrapper/2fa         — submit 2FA code to wrapper
  GET  /api/orpheus/status      — OrpheusDL-Spotify install/auth status
  POST /api/orpheus/login-start — start PKCE OAuth flow, returns Spotify auth URL
  DELETE /api/orpheus/login-cancel — cancel in-progress OAuth
  DELETE /api/orpheus/logout    — remove saved credentials
  GET  /api/soundcloud/status   — Lucida/SoundCloud install status
  POST /api/soundcloud/install  — npm install Lucida into tools/lucida/
  POST /api/fix-gamdl-deps      — fix protobuf/pywidevine
  GET  /api/install-log         — stream install log
  POST /api/restart             — graceful app restart

Install: setup.install(app, cfg, broadcast_fn, base_dir)
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
from pathlib import Path

import httpx

from fastapi import APIRouter

from ripster import setup as _setup
from ripster import amd as _amd

router = APIRouter()

_cfg:        dict = {}
_broadcast         = None
_base_dir:   Path = Path(".")


def install(app, ctx) -> None:
    global _cfg, _broadcast, _base_dir
    _cfg       = ctx.config
    _broadcast = ctx.broadcast
    _base_dir  = ctx.base_dir
    app.include_router(router)


# ── Widevine L3 (DRM SoundCloud) ──────────────────────────────────────────────
# Each user mints their OWN device.wvd locally; Ripster ships none. The minting
# pipeline (Android AVD + KeyDive) is interactive, multi-GB and admin-touching, so
# it runs in its own console window — we launch the guided wizard, the user follows
# it. The resulting .wvd is uploaded + shown installed in the SoundCloud SETTINGS
# tab (that part deliberately stays there); this is only the "mint a new one" help.

@router.get("/api/widevine/status")
async def widevine_status():
    # Honour a user-configured device path first, then the bundled default —
    # same resolution order as /api/soundcloud/wvd-status so the Setup badge and
    # the SoundCloud settings status never disagree. Validate by actually loading
    # the device so a corrupt/wrong-format .wvd reads as installed-but-invalid.
    p_cfg = (_cfg.get("sc-widevine-device") or "").strip()
    candidates = [Path(p_cfg)] if p_cfg else []
    candidates.append(_base_dir / "tools" / "widevine" / "device.wvd")
    for c in candidates:
        if c and c.is_file():
            try:
                from pywidevine.device import Device
                Device.load(c)
                return {"installed": True, "path": str(c),
                        "size": c.stat().st_size, "valid": True}
            except Exception as e:
                return {"installed": True, "path": str(c),
                        "size": c.stat().st_size, "valid": False, "error": str(e)}
    return {"installed": False, "path": str(candidates[-1])}


@router.post("/api/widevine/mint-wizard")
async def widevine_mint_wizard():
    import subprocess
    bat = _base_dir / "_widevine_setup" / "wvd.bat"
    if sys.platform != "win32":
        return {"ok": False, "error": "Мастер WVD доступен только на Windows."}
    if not bat.exists():
        return {"ok": False, "error": "_widevine_setup/wvd.bat не найден в установке."}
    try:
        subprocess.Popen(
            ["cmd", "/c", "start", "Ripster WVD L3 minter", "cmd", "/k", str(bat)],
            cwd=str(bat.parent),
            creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
        )
    except Exception as e:
        return {"ok": False, "error": f"не удалось запустить мастер: {e}"}
    return {"ok": True, "msg": "Мастер WVD открылся в отдельном окне — следуй инструкциям там."}


# ── Setup checklist: install ONE component synchronously ──────────────────────
# The redesigned Setup tab is a checklist; each ticked row calls this and AWAITS
# completion (progress streams to the Setup console via WS log/step events). Async
# install_* helpers yield to the loop, so WS broadcasts keep flowing meanwhile.
# SoundCloud + WVD keep their own dedicated endpoints (npm build / console wizard).

@router.post("/api/setup/component/{key}")
async def setup_component(key: str):
    # NOTE: each component installs ONLY its own thing and reports its own status,
    # so the user can see exactly what landed and what didn't. Shared tools
    # (ffmpeg / Bento4 / Node) are their own rows — NOT bundled into an engine.
    # The install log is NOT cleared here (the frontend clears the console once at
    # the start of a run) so a multi-component install keeps the full history.
    try:
        if key == "apple":
            # Apple Music engine (AMD v2): clone AppleMusicDecrypt + its Python deps.
            # It needs ffmpeg + Bento4 to actually decrypt — those are separate rows.
            await _setup.istep("amd", "running")
            await _setup.ensure_git()
            ok = await _amd.clone_amd() and await _amd.install_amd_deps()
            await _setup.istep("amd", "done" if ok else "error")
            if _broadcast:
                await _broadcast({"type": "amd_ready"})
            done = ok
        elif key == "ffmpeg":
            await _setup.install_ffmpeg_windows()
            done = bool(_setup.tool_path("ffmpeg"))
        elif key == "mp4decrypt":
            await _setup.install_mp4decrypt_windows()
            done = bool(_setup.tool_path("mp4decrypt"))
        elif key == "node":
            await _setup.install_node_windows()
            done = bool(_setup.tool_path("node"))
        elif key == "soundcloud":
            done = await _install_soundcloud_component()
        elif key == "orpheus":
            done = await _install_orpheus_component()
        elif key == "beatport":
            done = await _install_beatport_component()
        elif key == "zhaarey":
            # Advanced: the Go downloader toolchain (own premium Apple ID + Docker).
            await _setup.ensure_git()
            if not _setup.tool_path("go"):
                await _setup.install_go_windows()
            await _setup.clone_downloader()
            if not _setup.tool_path("MP4Box"):
                await _setup.install_gpac_windows()
            await _setup.install_mp4decrypt_windows()
            await _setup.go_mod_download()
            done = bool(_setup.tool_path("go"))
        else:
            return {"ok": False, "error": f"неизвестный компонент: {key}"}
    except Exception as e:                                # noqa: BLE001
        await _setup.ilog(f"✗ {key}: {e}", "error")
        return {"ok": False, "error": str(e)}
    return {"ok": done}


# ── Tools / Setup ─────────────────────────────────────────────────────────────

@router.get("/api/tools")
async def get_tools():
    return await _setup.check_tools()


@router.post("/api/setup")
async def run_setup():
    asyncio.create_task(_setup.run_full_setup())
    return {"ok": True, "msg": "Setup started — watch Setup tab"}


@router.post("/api/setup/amd")
async def run_amd_setup():
    async def _do():
        _setup.install_log.clear()
        await _setup.ilog("── AppleMusicDecrypt v2 Setup ──────────", "info")
        if await _amd.clone_amd():
            if await _amd.install_amd_deps():
                await _setup.ilog("✅ AMD готов! Нажми AMD в топбаре.", "success")
                if _broadcast:
                    await _broadcast({"type": "amd_ready"})
            else:
                await _setup.ilog("✗ Ошибка установки зависимостей", "error")
        else:
            await _setup.ilog("✗ Ошибка клонирования AMD", "error")
        if _broadcast:
            await _broadcast({"type": "setup_done", "missing": [], "need_restart": False})
    asyncio.create_task(_do())
    return {"ok": True}


# ── AMD ───────────────────────────────────────────────────────────────────────

@router.get("/api/amd/status")
async def amd_status_ep():
    amd_dir = _amd.get_amd_dir()
    return {"cloned": (amd_dir / "main.py").exists(), "path": str(amd_dir)}


@router.get("/api/amd/wrapper-status")
async def amd_wrapper_status_ep():
    instance = _cfg.get("amd-instance-url", "wm.wol.moe")
    secure   = _cfg.get("amd-instance-secure", True)
    result   = await _amd.amd_wrapper_status(instance, secure)
    result["instance"] = instance
    return result


# ── Docker wrapper ────────────────────────────────────────────────────────────

@router.get("/api/wrapper-status")
async def get_wrapper_status():
    running               = await _amd.check_wrapper_running()
    docker_ok, docker_msg = _amd.check_docker_installed()
    mode                  = _amd._wrapper_mode()
    return {
        "running":        running,
        "has_session":    _amd._has_saved_session(),
        "port":           _cfg.get("decrypt-port", "127.0.0.1:10020"),
        "docker":         docker_ok,
        "docker_msg":     docker_msg,
        "mode":           mode,
        "has_local_bin":  _amd._wrapper_bin().exists(),
        "wsl_ok":         _amd.check_wsl_available(),
    }


@router.post("/api/wrapper/start")
async def wrapper_start():
    asyncio.create_task(_amd.start_wrapper(force_login=False))
    return {"ok": True, "msg": "Starting wrapper — watch the banner"}


@router.post("/api/wrapper/relogin")
async def wrapper_relogin():
    has_session = _amd._has_saved_session()
    asyncio.create_task(_amd.start_wrapper(force_login=True))
    return {
        "ok": True,
        "had_session": has_session,
        "msg": "Re-login started — ожидай 2FA на телефоне",
    }


@router.get("/api/wrapper/session-status")
async def wrapper_session_status():
    return {
        "has_session": _amd._has_saved_session(),
        "running":     await _amd.check_wrapper_running(),
        "mode":        _amd._wrapper_mode(),
    }


@router.post("/api/wrapper/stop")
async def wrapper_stop():
    return await _amd.stop_wrapper()


@router.post("/api/wrapper/pull")
async def wrapper_pull():
    asyncio.create_task(_amd.pull_wrapper_image())
    return {"ok": True, "msg": "Pulling/building image — watch the banner"}


@router.post("/api/wrapper/build")
async def wrapper_build():
    asyncio.create_task(_amd.build_wrapper_image())
    return {"ok": True, "msg": "Building local image — watch the banner"}


@router.post("/api/wrapper/2fa")
async def wrapper_2fa(body: dict):
    code = (body.get("code") or "").strip()
    if not code:
        return {"ok": False, "msg": "Нет кода"}
    # Deliver to the interactive login process's STDIN (primary) + files (fallback).
    fed = await _amd.submit_2fa(code)
    return {"ok": True, "fed_stdin": fed}


# ── OrpheusDL-Spotify ─────────────────────────────────────────────────────────

def _orpheus_dir() -> Path:
    return _base_dir / "orpheus"

def _orpheus_creds_path() -> Path:
    return _orpheus_dir() / "config" / "credentials.json"

_oauth_proc: asyncio.subprocess.Process | None = None


@router.get("/api/orpheus/status")
async def orpheus_status():
    from ripster.engines.orpheus_spotify import is_installed, is_authenticated
    # Sync real username + credentials on every status check (cheap, idempotent)
    asyncio.create_task(_sync_orpheus_username())
    creds_p = _orpheus_creds_path()
    username = ""
    if creds_p.exists():
        try:
            d = json.loads(creds_p.read_text(encoding="utf-8"))
            username = d.get("spotify_username", "")
        except Exception:
            pass
    return {
        "installed":      is_installed(),
        "authenticated":  is_authenticated(),
        "username":       username,
        "mode":           _cfg.get("spotify-engine", "convert"),
        "quality":        _cfg.get("orpheus-quality", "hifi"),
    }


@router.post("/api/orpheus/login-start")
async def orpheus_login_start():
    """Start PKCE OAuth flow. Returns Spotify auth URL for the browser popup."""
    global _oauth_proc

    if not (_orpheus_dir() / "orpheus.py").exists():
        return {"ok": False, "error": "OrpheusDL не установлен — OrpheusDL отсутствует в папке orpheus/"}

    # Kill any existing OAuth process
    if _oauth_proc is not None:
        try:
            _oauth_proc.kill()
            await _oauth_proc.wait()
        except Exception:
            pass
        _oauth_proc = None

    helper_p = _orpheus_dir() / "_auth_helper.py"
    if not helper_p.exists():
        return {"ok": False, "error": f"Auth helper не найден: {helper_p}"}

    env = {**os.environ, "PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION": "python",
           "PYTHONIOENCODING": "utf-8"}
    try:
        _oauth_proc = await asyncio.create_subprocess_exec(
            sys.executable, str(helper_p),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            stdin=asyncio.subprocess.DEVNULL,
            cwd=str(_orpheus_dir()),
            env=env,
        )
    except Exception as e:
        return {"ok": False, "error": str(e)}

    # Read stdout until we see ORPHEUS_AUTH_URL (timeout 15 s)
    auth_url = None
    try:
        async def _read_url():
            nonlocal auth_url
            async for raw in _oauth_proc.stdout:
                line = raw.decode(errors="replace").strip()
                if line.startswith("ORPHEUS_AUTH_URL:"):
                    auth_url = line[len("ORPHEUS_AUTH_URL:"):]
                    break
                if line.startswith("ORPHEUS_AUTH_FAILED:"):
                    raise RuntimeError(line[len("ORPHEUS_AUTH_FAILED:"):])
        await asyncio.wait_for(_read_url(), timeout=15)
    except asyncio.TimeoutError:
        return {"ok": False, "error": "Auth helper не выдал URL — возможно, порт 4381 занят"}
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}

    asyncio.create_task(_watch_orpheus_oauth())
    return {"ok": True, "url": auth_url}


async def _sync_orpheus_username() -> str:
    """Fetch real Spotify user ID from /me and write it into credentials.json + settings.json."""
    creds_p = _orpheus_creds_path()
    settings_p = _orpheus_dir() / "config" / "settings.json"

    # Always clean client_id/client_secret from settings.json FIRST, even if credentials.json
    # doesn't exist. This ensures re-authentication always uses OrpheusDL's built-in PKCE client
    # (65b708073f...) rather than any previously-written Ripster client_id.
    if settings_p.exists():
        try:
            cfg = json.loads(settings_p.read_text(encoding="utf-8"))
            sp_mod = cfg.setdefault("modules", {}).setdefault("spotify", {})
            sp_mod["client_id"] = ""
            sp_mod["client_secret"] = ""
            settings_p.write_text(json.dumps(cfg, indent=4, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    if not creds_p.exists():
        return ""
    try:
        creds = json.loads(creds_p.read_text(encoding="utf-8"))
        token = creds.get("access_token", "")
        user_id = ""
        if token:
            try:
                async with httpx.AsyncClient(timeout=10) as c:
                    r = await c.get("https://api.spotify.com/v1/me",
                                    headers={"Authorization": f"Bearer {token}"})
                    if r.status_code == 200:
                        user_id = r.json().get("id", "")
            except Exception:
                pass
        if not user_id:
            user_id = creds.get("spotify_username", "")
        if user_id and user_id != creds.get("spotify_username"):
            creds["spotify_username"] = user_id
            creds_p.write_text(json.dumps(creds, indent=2), encoding="utf-8")
        # Sync username into settings.json (client_id already cleared above)
        if settings_p.exists() and user_id:
            try:
                cfg = json.loads(settings_p.read_text(encoding="utf-8"))
                sp_mod = cfg.setdefault("modules", {}).setdefault("spotify", {})
                sp_mod["username"] = user_id
                settings_p.write_text(json.dumps(cfg, indent=4, ensure_ascii=False), encoding="utf-8")
            except Exception:
                pass
        return user_id
    except Exception:
        return ""


async def _watch_orpheus_oauth():
    """Wait for PKCE callback, then broadcast orpheus_authed."""
    global _oauth_proc
    if _oauth_proc is None:
        return
    try:
        remaining = await _oauth_proc.stdout.read()
        await _oauth_proc.wait()
        text = remaining.decode(errors="replace")
        if "ORPHEUS_AUTH_DONE" in text:
            username = await _sync_orpheus_username()
            if not username:
                creds_p = _orpheus_creds_path()
                try:
                    username = json.loads(creds_p.read_text()).get("spotify_username", "")
                except Exception:
                    pass
            if _broadcast:
                await _broadcast({"type": "orpheus_authed", "username": username})
        elif "ORPHEUS_AUTH_FAILED:" in text:
            msg = text.split("ORPHEUS_AUTH_FAILED:", 1)[-1].strip()
            if _broadcast:
                await _broadcast({"type": "log", "msg": f"✗ Spotify login failed: {msg}",
                                  "level": "error"})
    except Exception:
        pass
    finally:
        _oauth_proc = None


@router.delete("/api/orpheus/login-cancel")
async def orpheus_login_cancel():
    global _oauth_proc
    if _oauth_proc is not None:
        try:
            _oauth_proc.kill()
            await _oauth_proc.wait()
        except Exception:
            pass
        _oauth_proc = None
    return {"ok": True}


@router.delete("/api/orpheus/logout")
async def orpheus_logout():
    p = _orpheus_creds_path()
    if p.exists():
        try:
            p.unlink()
        except OSError:
            pass
    return {"ok": True}


# ── SoundCloud / Lucida ───────────────────────────────────────────────────────

_LUCIDA_REPO = "https://codeberg.org/lucida/lucida.git"


def _lucida_dir() -> Path:
    return _base_dir / "tools" / "lucida"


@router.get("/api/soundcloud/status")
async def soundcloud_status():
    import shutil
    from ripster.engines.soundcloud import is_installed, _runner_path
    node_ok = shutil.which("node") is not None
    node_ver = ""
    if node_ok:
        try:
            import asyncio as _aio
            p = await _aio.create_subprocess_exec(
                "node", "--version",
                stdout=_aio.subprocess.PIPE, stderr=_aio.subprocess.DEVNULL,
            )
            out, _ = await p.communicate()
            node_ver = out.decode().strip()
        except Exception:
            pass
    return {
        "installed":  is_installed(),
        "runner":     str(_runner_path()),
        "node_ok":    node_ok,
        "node_ver":   node_ver,
        "npm_dir":    str(_lucida_dir()),
    }


@router.get("/api/beatport/status")
async def beatport_status():
    from ripster.engines.orpheus_beatport import is_installed, _module_path, _orpheus_dir
    return {
        "orpheus_installed": (_orpheus_dir() / "orpheus.py").exists(),
        "module_installed":  is_installed(),
        "module_path":       str(_module_path()),
    }


@router.post("/api/setup/beatport")
async def beatport_install():
    """Clone orpheusdl-beatport into orpheus/modules/beatport/ and install its
    requirements. Progress streams to the Setup console (install_log)."""
    async def _do():
        await _install_beatport_component()
    asyncio.create_task(_do())
    return {"ok": True, "msg": "Установка запущена — смотри Setup-лог"}


async def _install_soundcloud_component() -> bool:
    """SoundCloud/Lucida turnkey: ensure git + node, clone the Lucida source,
    `npm install` its deps (incl. TypeScript) and build it (TypeScript → build/).

    A plain `npm install` of the git package does NOT work: lucida ships
    ``files:["build/**"]`` with a non-build ``prepare`` script, so the npm tarball
    contains no code at all — clone + `npm run build` is required.

    Streams every line to the SETUP console (install_log) — NOT the main log — so
    the Setup tab shows live progress. Returns True iff build/index.js was produced.
    """
    import shutil
    lucida_dir = _lucida_dir()
    lucida_dir.mkdir(parents=True, exist_ok=True)
    src_dir = lucida_dir / "lucida-src"

    await _setup.ilog("── SoundCloud (Lucida) ─────────────────", "info")
    if not (lucida_dir / "runner.mjs").exists():
        await _setup.ilog("✗ runner.mjs не найден в tools/lucida/ — обнови/переустанови "
                          "Ripster (файл должен идти в сборке).", "error")
        return False

    # Turnkey on a fresh PC: SoundCloud/Lucida needs git + node(npm), which a clean
    # machine lacks. Provision both here so the user never installs anything by hand.
    await _setup.ensure_git()
    await _setup.install_node_windows()
    if not _setup.tool_path("node"):
        await _setup.ilog("✗ Node.js не установлен — поставь компонент «Node.js» отдельной "
                          "строкой выше.", "error")
        return False
    git = shutil.which("git") or "git"
    npm = shutil.which("npm") or "npm"

    # 1 — clone (or update) the Lucida source
    if (src_dir / ".git").is_dir():
        await _setup.ilog("⟳ Обновляю исходники Lucida…", "info")
        rc, _ = await _setup.irun([git, "pull", "--ff-only"], cwd=str(src_dir))
    else:
        await _setup.ilog("⬇ Клонирую Lucida…", "info")
        rc, _ = await _setup.irun([git, "clone", "--depth", "1", _LUCIDA_REPO, str(src_dir)],
                                  cwd=str(lucida_dir))
    if rc != 0:
        await _setup.ilog(f"✗ git: код {rc}", "error")
        return False

    # 2 — install Lucida's own deps. --ignore-scripts skips the husky `prepare` hook.
    await _setup.ilog("⬇ npm install зависимостей Lucida (~1–2 мин)…", "info")
    rc, _ = await _setup.irun([npm, "install", "--ignore-scripts"], cwd=str(src_dir))
    if rc != 0:
        await _setup.ilog(f"✗ npm install: код {rc}", "error")
        return False

    # 3 — build TypeScript → build/
    await _setup.ilog("🔧 Сборка Lucida (tsc)…", "info")
    await _setup.irun([npm, "run", "build"], cwd=str(src_dir))

    if (src_dir / "build" / "index.js").exists():
        await _setup.ilog("✓ Lucida установлена и собрана — SoundCloud готов", "success")
        if _broadcast:
            await _broadcast({"type": "soundcloud_installed"})
        return True
    await _setup.ilog("✗ Сборка не дала build/index.js — смотри лог выше", "error")
    return False


async def _install_orpheus_component() -> bool:
    """OrpheusDL core + Spotify module + pip deps. Clones the OFFICIAL repos at
    install time — NO secrets/config shipped (the dev's orpheus/config holds personal
    Spotify/Tidal credentials and must never be packaged). This is the base that
    Spotify and Beatport sit on. NOTE: native Spotify *decryption* additionally needs
    Spotify.dll (~42 MB, not in any repo) — separate; this gets the engine installed
    so Beatport + metadata work and Spotify login can be set up."""
    import shutil
    orph_dir = _orpheus_dir()
    await _setup.ilog("── OrpheusDL (база Spotify / Beatport) ──", "info")
    await _setup.ensure_git()
    git = shutil.which("git") or "git"

    if (orph_dir / "orpheus.py").exists():
        await _setup.ilog("↻ OrpheusDL уже есть — git pull…", "info")
        await _setup.irun([git, "pull"], cwd=str(orph_dir))
    else:
        await _setup.ilog("⬇ Клонирую OrpheusDL…", "info")
        rc, _ = await _setup.irun(
            [git, "clone", "https://github.com/OrfiTeam/OrpheusDL", str(orph_dir)])
        if rc != 0:
            await _setup.ilog("✗ Ошибка git clone OrpheusDL", "error")
            return False

    # Spotify module (separate repo) → orpheus/modules/spotify
    (orph_dir / "modules").mkdir(parents=True, exist_ok=True)
    sp_dir = orph_dir / "modules" / "spotify"
    if (sp_dir / "interface.py").exists():
        await _setup.ilog("↻ Модуль Spotify — git pull…", "info")
        await _setup.irun([git, "pull"], cwd=str(sp_dir))
    else:
        await _setup.ilog("⬇ Клонирую модуль Spotify…", "info")
        await _setup.irun(
            [git, "clone", "https://github.com/bascurtiz/orpheusdl-spotify", str(sp_dir)])

    # Resilient module init: a newer Spotify module imports symbols the bundled
    # utils lacks (find_system_ffmpeg / vendor_bootstrap) → ImportError that aborts
    # init for ALL modules, taking Beatport/Tidal-via-orpheus down too. Wrap the
    # per-module import so a broken module is skipped, not fatal. Idempotent.
    try:
        _core_py = orph_dir / "orpheus" / "core.py"
        if _core_py.exists():
            _src = _core_py.read_text(encoding="utf-8", errors="replace")
            _old = ("        for module in module_list:  # Loading module information into module_settings\n"
                    "            module_information: ModuleInformation = getattr("
                    "importlib.import_module(f'modules.{module}.interface'), 'module_information', None)\n")
            _new = ("        for module in module_list:  # Loading module information into module_settings\n"
                    "            try:\n"
                    "                _iface = importlib.import_module(f'modules.{module}.interface')\n"
                    "            except Exception as _e:\n"
                    "                logging.warning(f'Orpheus: skipping module \"{module}\" — failed to import: {_e}')\n"
                    "                continue\n"
                    "            module_information: ModuleInformation = getattr(_iface, 'module_information', None)\n")
            if "_iface = importlib.import_module" not in _src and _old in _src:
                _core_py.write_text(_src.replace(_old, _new, 1), encoding="utf-8")
                await _setup.ilog("✓ Patched orpheus/core.py (resilient module init — Beatport fix)", "success")
    except Exception as _e:
        await _setup.ilog(f"⚠ orpheus/core.py patch skipped: {_e}", "warn")

    req = orph_dir / "requirements.txt"
    if req.exists():
        await _setup.ilog("📦 pip install OrpheusDL requirements…", "info")
        await _setup.irun([sys.executable, "-m", "pip", "install", "-r", str(req), "--quiet"],
                          cwd=str(orph_dir))

    from ripster.engines.orpheus_spotify import is_installed
    ok = is_installed()
    if ok:
        await _setup.ilog("✓ OrpheusDL установлен. Spotify-вход — Настройки → Spotify. "
                          "Нативный Spotify-декрипт требует ещё Spotify.dll (отдельно).", "success")
    else:
        await _setup.ilog("✗ OrpheusDL не определяется (нет orpheus.py) — смотри лог.", "error")
    return ok


async def _install_beatport_component() -> bool:
    """Beatport (orpheusdl-beatport): clone into orpheus/modules/beatport + pip
    deps. Needs OrpheusDL present. Streams to the SETUP console. Returns
    is_installed()."""
    import shutil
    from ripster.engines.orpheus_beatport import _module_path, _orpheus_dir, is_installed
    mod_path = _module_path()
    orph_dir = _orpheus_dir()

    await _setup.ilog("── Beatport (orpheusdl-beatport) ───────", "info")
    # Beatport is a module ON TOP of OrpheusDL. Auto-install the base if it's
    # missing so a single click just works (turnkey).
    if not (orph_dir / "orpheus.py").exists():
        await _setup.ilog("ℹ OrpheusDL не найден — ставлю его сначала (база для Beatport)…", "info")
        if not await _install_orpheus_component():
            await _setup.ilog("✗ Не удалось поставить OrpheusDL — Beatport прерван.", "error")
            return False
    (orph_dir / "modules").mkdir(parents=True, exist_ok=True)
    git = shutil.which("git") or "git"

    if mod_path.exists():
        await _setup.ilog("↻ orpheusdl-beatport уже есть — git pull…", "info")
        await _setup.irun([git, "pull"], cwd=str(mod_path))
    else:
        await _setup.ilog("⬇ Клонирую orpheusdl-beatport…", "info")
        rc, _ = await _setup.irun(
            [git, "clone", "https://github.com/Dniel97/orpheusdl-beatport", str(mod_path)])
        if rc != 0:
            await _setup.ilog("✗ Ошибка git clone", "error")
            return False

    req = mod_path / "requirements.txt"
    if req.exists():
        await _setup.ilog("📦 pip install requirements.txt…", "info")
        await _setup.irun([sys.executable, "-m", "pip", "install", "-r", str(req), "--quiet"],
                          cwd=str(mod_path))

    ok = is_installed()
    await _setup.ilog("✓ orpheusdl-beatport установлен." if ok
                      else "✗ Модуль не определяется как установленный — смотри лог.",
                      "success" if ok else "error")
    return ok


@router.post("/api/soundcloud/install")
async def soundcloud_install():
    """Install/build Lucida (SoundCloud). Progress streams to the Setup console;
    the SC settings tab polls scEngineCheck and reacts to the WS
    'soundcloud_installed' on completion."""
    async def _do():
        await _install_soundcloud_component()
    asyncio.create_task(_do())
    return {"ok": True, "msg": "Installing Lucida — watch Setup log"}


# ── gamdl deps ────────────────────────────────────────────────────────────────

@router.post("/api/fix-gamdl-deps")
async def fix_gamdl_deps():
    async def _fix():
        await _setup.ilog("🔧 Fixing gamdl dependencies…", "info")
        rc1, o1 = await _setup.irun([sys.executable, "-m", "pip", "install",
                                      "protobuf>=4.21.0", "--upgrade",
                                      "--break-system-packages", "-q"])
        if rc1 == 0:
            await _setup.ilog("   ✓ protobuf upgraded", "success")
        else:
            await _setup.ilog(f"   ✗ protobuf upgrade failed: {o1[:100]}", "error")
        rc2, o2 = await _setup.irun([sys.executable, "-m", "pip", "install",
                                      "pywidevine", "--upgrade",
                                      "--break-system-packages", "-q"])
        if rc2 == 0:
            await _setup.ilog("   ✓ pywidevine upgraded", "success")
        else:
            await _setup.ilog(f"   ✗ pywidevine failed: {o2[:100]}", "error")
        rc_v, verify_out = await _setup.irun([sys.executable, "-c",
            "from gamdl.downloader import Downloader; print('gamdl OK')"])
        if rc_v == 0:
            await _setup.ilog("✅ gamdl imports OK — ready to download!", "success")
            if _broadcast:
                await _broadcast({"type": "gamdl_deps_fixed"})
        else:
            await _setup.ilog("✗ Still failing. Try: pip install gamdl --force-reinstall",
                               "error")
    asyncio.create_task(_fix())
    return {"ok": True}


# ── Self-update ───────────────────────────────────────────────────────────────

def _app_version() -> str:
    """Installed Ripster RELEASE tag (app.RELEASE_VERSION, e.g. '1.0.6') — this is
    what the self-updater compares against GitHub release tags. Falls back to the
    internal APP_VERSION, then 0.0.0. Read lazily to avoid a circular import at
    module load (app.py imports this routes module)."""
    try:
        import app as _app_mod
        return getattr(_app_mod, "RELEASE_VERSION", None) \
            or getattr(_app_mod, "APP_VERSION", "0.0.0")
    except Exception:
        return "0.0.0"


@router.get("/api/update/check")
async def update_check():
    """Is a newer Ripster release available on GitHub? (repo: config `ripster-repo`)."""
    from ripster import updater
    return await updater.check_for_update(_cfg, _app_version())


@router.post("/api/update/apply")
async def update_apply():
    """Pull new source (git), reconcile pinned pip deps, verify the tree imports.
    Heavy deps + user data untouched. On {ok, restart_required} the UI then calls
    /api/restart. On a verify failure the result flags rollback_needed."""
    from ripster import updater
    return await updater.apply_update(_cfg, _base_dir)


def _respawn_detached() -> bool:
    """Spawn a fresh, WINDOWLESS, detached server before this process exits — so a
    restart no longer depends on the launcher respawning us. The launcher only
    respawns a server it OWNS; when it merely ATTACHED to an already-running server
    (multiple Ripster.exe instances, or the owner launcher having died) nothing
    brings the server back after os._exit → the window hangs on a dead page. A
    self-spawned successor grabs the port regardless; a duelling launcher respawn
    just loses the bind and exits silently. CREATE_NO_WINDOW keeps it windowless,
    so this does NOT reintroduce the old 'cmd windows keep popping' flash."""
    try:
        import subprocess
        env   = {**os.environ, "RIPSTER_IS_RESTART": "1"}
        flags = 0
        if os.name == "nt":
            flags = (subprocess.DETACHED_PROCESS
                     | subprocess.CREATE_NEW_PROCESS_GROUP
                     | getattr(subprocess, "CREATE_NO_WINDOW", 0))
        subprocess.Popen([sys.executable, str(_base_dir / "app.py")],
                         cwd=str(_base_dir), env=env, creationflags=flags,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False


# ── Log / restart ─────────────────────────────────────────────────────────────

@router.get("/api/install-log")
async def get_install_log():
    return _setup.install_log


@router.post("/api/restart")
async def restart_app():
    import threading

    def _do():
        import time
        time.sleep(1.5)
        # When started by the standalone launcher (Ripster.exe / ripster_launcher),
        # that process SUPERVISES us: a clean exit makes it respawn the server
        # WINDOWLESS. os.execv must NOT be used there — on Windows it spawns a fresh
        # console-subsystem python WITHOUT the no-window flag, flashing a cmd window
        # every restart, and races the launcher's respawn (the "cmd windows keep
        # popping" bug). Outside the launcher (dev `python app.py`), os.execv is
        # correct — it re-execs in the existing console.
        if os.environ.get("RIPSTER_LAUNCHER") == "1":
            _respawn_detached()    # don't rely on the launcher (it may have only attached)
            os._exit(0)
        else:
            os.execv(sys.executable, [sys.executable] + sys.argv)

    threading.Thread(target=_do, daemon=True).start()
    return {"ok": True}
