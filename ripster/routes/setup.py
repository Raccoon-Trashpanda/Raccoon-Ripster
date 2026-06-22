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
    """Clone orpheusdl-beatport into orpheus/modules/beatport/ and install its requirements."""
    import subprocess, asyncio
    from ripster.engines.orpheus_beatport import _module_path, _orpheus_dir

    mod_path  = _module_path()
    orph_dir  = _orpheus_dir()

    async def _stream(label: str, *cmd, cwd=None):
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(cwd) if cwd else None,
        )
        async for line in proc.stdout:
            txt = line.decode("utf-8", "replace").rstrip()
            if _broadcast:
                await _broadcast({"type": "log", "msg": f"[beatport-install] {txt}", "level": "info"})
        return await proc.wait()

    async def _do():
        try:
            if not orph_dir.exists():
                if _broadcast:
                    await _broadcast({"type": "log",
                                      "msg": "✗ Папка orpheus/ не найдена — сначала нужен OrpheusDL.",
                                      "level": "error"})
                return

            modules_dir = orph_dir / "modules"
            modules_dir.mkdir(parents=True, exist_ok=True)

            if mod_path.exists():
                # Reinstall: git pull
                if _broadcast:
                    await _broadcast({"type": "log", "msg": "↻ orpheusdl-beatport: git pull…", "level": "info"})
                rc = await _stream("git-pull", "git", "pull", cwd=mod_path)
            else:
                if _broadcast:
                    await _broadcast({"type": "log", "msg": "⬇ Клонирую orpheusdl-beatport…", "level": "info"})
                rc = await _stream(
                    "git-clone",
                    "git", "clone",
                    "https://github.com/Dniel97/orpheusdl-beatport",
                    str(mod_path),
                )
                if rc != 0:
                    if _broadcast:
                        await _broadcast({"type": "log", "msg": "✗ Ошибка git clone", "level": "error"})
                    return

            # Install module requirements if present
            req = mod_path / "requirements.txt"
            if req.exists():
                if _broadcast:
                    await _broadcast({"type": "log", "msg": "📦 pip install requirements.txt…", "level": "info"})
                await _stream("pip", sys.executable, "-m", "pip", "install", "-r", str(req),
                              "--quiet", cwd=mod_path)

            if _broadcast:
                await _broadcast({"type": "log", "msg": "✓ orpheusdl-beatport установлен.", "level": "info"})
        except Exception as exc:
            if _broadcast:
                await _broadcast({"type": "log", "msg": f"✗ beatport install error: {exc}", "level": "error"})

    asyncio.create_task(_do())
    return {"ok": True, "msg": "Установка запущена — смотри Лог"}


@router.post("/api/soundcloud/install")
async def soundcloud_install():
    """Clone the Lucida source, install its deps and build it (TypeScript →
    build/). A plain `npm install` of the git package does NOT work: lucida
    ships ``files:["build/**"]`` with a non-build ``prepare`` script, so the
    npm tarball contains no code at all. Clone + `npm run build` is required.
    """
    async def _do():
        import shutil
        lucida_dir = _lucida_dir()
        lucida_dir.mkdir(parents=True, exist_ok=True)
        src_dir = lucida_dir / "lucida-src"

        if not (lucida_dir / "runner.mjs").exists():
            if _broadcast:
                await _broadcast({"type": "log",
                                   "msg": "✗ runner.mjs не найден в tools/lucida/",
                                   "level": "error"})
            return

        git = shutil.which("git") or "git"
        npm = shutil.which("npm") or "npm"

        async def _step(label: str, *cmd, cwd: Path) -> int:
            if _broadcast:
                await _broadcast({"type": "log", "msg": label, "level": "info"})
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd, cwd=str(cwd),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                out, _ = await proc.communicate()
            except Exception as e:
                if _broadcast:
                    await _broadcast({"type": "log", "msg": f"✗ {e}", "level": "error"})
                return -1
            for ln in out.decode("utf-8", errors="replace").splitlines()[-15:]:
                if ln.strip() and _broadcast:
                    await _broadcast({"type": "log", "msg": ln.strip(), "level": "stdout"})
            return proc.returncode if proc.returncode is not None else -1

        # 1 — clone (or update) the Lucida source
        if (src_dir / ".git").is_dir():
            rc = await _step("⟳ Обновляю исходники Lucida…", git, "pull", "--ff-only", cwd=src_dir)
        else:
            rc = await _step("⬇ Клонирую Lucida…", git, "clone", "--depth", "1",
                             _LUCIDA_REPO, str(src_dir), cwd=lucida_dir)
        if rc != 0:
            if _broadcast:
                await _broadcast({"type": "log", "msg": f"✗ git: код {rc}", "level": "error"})
            return

        # 2 — install Lucida's own deps (incl. TypeScript). --ignore-scripts
        #     skips the husky `prepare` hook we don't need.
        rc = await _step("⬇ npm install зависимостей Lucida (~1–2 мин)…",
                         npm, "install", "--ignore-scripts", cwd=src_dir)
        if rc != 0:
            if _broadcast:
                await _broadcast({"type": "log", "msg": f"✗ npm install: код {rc}", "level": "error"})
            return

        # 3 — build TypeScript → build/
        await _step("🔧 Сборка Lucida (tsc)…", npm, "run", "build", cwd=src_dir)

        if (src_dir / "build" / "index.js").exists():
            if _broadcast:
                await _broadcast({"type": "log",
                                   "msg": "✓ Lucida установлена и собрана — SoundCloud готов",
                                   "level": "success"})
                await _broadcast({"type": "soundcloud_installed"})
        else:
            if _broadcast:
                await _broadcast({"type": "log",
                                   "msg": "✗ Сборка не дала build/index.js — смотри лог выше",
                                   "level": "error"})

    asyncio.create_task(_do())
    return {"ok": True, "msg": "Installing Lucida — watch log"}


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
    """Current Ripster version (app.APP_VERSION), read lazily to avoid a circular
    import at module load (app.py imports this routes module)."""
    try:
        import app as _app_mod
        return getattr(_app_mod, "APP_VERSION", "0.0.0")
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
        os.execv(sys.executable, [sys.executable] + sys.argv)

    threading.Thread(target=_do, daemon=True).start()
    return {"ok": True}
