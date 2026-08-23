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
  GET  /api/wrapper/accounts        — list Apple accounts in the wrapper pool
  POST /api/wrapper/accounts/add    — add an account, start its wrapper slot
  POST /api/wrapper/accounts/{slot}/remove — remove an added account (not slot 0)
  GET  /api/deezer/accounts         — list Deezer ARL accounts in the load-balance pool
  POST /api/deezer/accounts/add     — add an ARL account
  POST /api/deezer/accounts/{slot}/remove — remove an added account (not slot 0)
  GET  /api/qobuz/accounts          — list Qobuz accounts in the load-balance pool
  POST /api/qobuz/accounts/add      — add an account (token or email/password mode)
  POST /api/qobuz/accounts/{slot}/remove — remove an added account (not slot 0)
  GET  /api/soundcloud/accounts     — list SoundCloud accounts in the load-balance pool
  POST /api/soundcloud/accounts/add — add a token
  POST /api/soundcloud/accounts/{slot}/remove — remove an added account (not slot 0)
  GET  /api/yandex/accounts         — list Yandex accounts in the load-balance pool
  POST /api/yandex/accounts/add     — add a token
  POST /api/yandex/accounts/{slot}/remove — remove an added account (not slot 0)
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

from fastapi import APIRouter, HTTPException, Request

from ripster.i18n_msg import imsg   # новые ручки отдают ключ+параметры,
                                    # а не готовую русскую строку
from ripster import setup as _setup
from ripster import amd as _amd

router = APIRouter()

_cfg:        dict = {}
_broadcast         = None
_base_dir:   Path = Path(".")
_save_config       = None


def install(app, ctx) -> None:
    global _cfg, _broadcast, _base_dir, _save_config
    _cfg         = ctx.config
    _broadcast   = ctx.broadcast
    _base_dir    = ctx.base_dir
    _save_config = ctx.save_config
    app.include_router(router)


# ── Widevine L3 (DRM SoundCloud) ──────────────────────────────────────────────
# Each user mints their OWN device.wvd locally; Ripster ships none. The minting
# pipeline (Android AVD + KeyDive) is interactive, multi-GB and admin-touching, so
# it runs in its own console window — we launch the guided wizard, the user follows
# it. The resulting .wvd is uploaded + shown installed in the SoundCloud SETTINGS
# tab (that part deliberately stays there); this is only the "mint a new one" help.

def _wvd_venv_python() -> "str | None":
    """The isolated pywidevine venv interpreter, if provisioned. pywidevine pins
    deps (protobuf>=6.33) that OTHER engines (OrpheusDL→protobuf 3.15.8) clobber in
    the shared bundled python, so we keep it in tools/wvdvenv and call it as a
    subprocess. See the ripster-dependency-versions skill."""
    for sub in (("Scripts", "python.exe"), ("bin", "python")):
        cand = _base_dir / "tools" / "wvdvenv" / sub[0] / sub[1]
        if cand.is_file():
            return str(cand)
    return None


def _validate_wvd(path: Path) -> tuple[bool, str]:
    """Return (valid, error). Validate in the ISOLATED venv if present (robust
    against a polluted shared env), else fall back to in-process import."""
    vpy = _wvd_venv_python()
    if vpy:
        import subprocess
        code = ("from pywidevine.device import Device;"
                "Device.load(r'''" + str(path) + "''');print('WVD_OK')")
        try:
            r = subprocess.run([vpy, "-c", code], capture_output=True, text=True,
                               timeout=25,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if r.returncode == 0 and "WVD_OK" in (r.stdout or ""):
                return True, ""
            tail = [l for l in (r.stderr or "").splitlines() if l.strip()]
            return False, (tail[-1] if tail else "load failed")
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"
    # No isolated venv — best-effort in-process (may fail on a polluted env).
    try:
        from pywidevine.device import Device
        Device.load(path)
        return True, ""
    except Exception as e:
        return False, str(e)


@router.get("/api/widevine/status")
async def widevine_status():
    # Honour a user-configured device path first, then the bundled default —
    # same resolution order as /api/soundcloud/wvd-status so the Setup badge and
    # the SoundCloud settings status never disagree. Validation runs in the
    # isolated pywidevine venv so a polluted shared env can't make a good .wvd
    # read as invalid (the device.wvd-shows-as-missing tester bug).
    p_cfg = (_cfg.get("sc-widevine-device") or "").strip()
    candidates = [Path(p_cfg)] if p_cfg else []
    candidates.append(_base_dir / "tools" / "widevine" / "device.wvd")
    for c in candidates:
        if c and c.is_file():
            valid, err = _validate_wvd(c)
            out = {"installed": True, "path": str(c), "size": c.stat().st_size,
                   "valid": valid}
            if not valid:
                out["error"] = err
            return out
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


@router.post("/api/widevine/mint-auto")
async def widevine_mint_auto():
    """Fully automated device.wvd mint — installs the JRE17 + cmdline-tools + SDK
    packages + AEHD toolchain first (idempotent, skips anything already present),
    THEN runs wvd_console.ps1 -Auto (boot emulator → KeyDive extract → install →
    verify → stop) headless, streaming progress to the Setup console. No
    interactive menu, no separate window — this is the single one-click button.
    Previously this skipped straight to the ps1, which assumes cmdline-tools/
    sdkmanager already exist — on a clean box nothing had ever installed them
    (setup_widevine_toolchain lived behind an unreferenced endpoint), so
    Ensure-Sdk failed immediately and every step after it stayed uninstalled."""
    if sys.platform != "win32":
        return {"ok": False, "error": "Авто-минт WVD доступен только на Windows."}
    ps1 = _base_dir / "_widevine_setup" / "wvd_console.ps1"
    if not ps1.exists():
        return {"ok": False, "error": "_widevine_setup/wvd_console.ps1 не найден в установке."}

    async def _do():
        await _setup.ilog("── Widevine L3: авто-минт device.wvd "
                          "(тулчейн → boot → extract → install → verify) ──", "info")
        await _setup.ilog("Займёт несколько минут (эмулятор + KeyDive). Не закрывай окно.", "info")
        if not await _setup.setup_widevine_toolchain():
            await _setup.ilog("✗ Тулчейн (JRE/SDK/AEHD) не установился — авто-минт остановлен, "
                              "смотри лог выше.", "error")
            return
        rc, out = await _setup.irun(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", str(ps1), "-Auto"],
            cwd=str(ps1.parent))
        if "AUTO_RESULT: OK" in (out or ""):
            await _setup.ilog("✓ device.wvd сминчен и установлен — SoundCloud DRM готов.", "success")
            if _broadcast:
                await _broadcast({"type": "widevine_minted"})
        else:
            await _setup.ilog(f"✗ Авто-минт не завершился (rc={rc}). Смотри лог выше; "
                              "если KeyDive застрял на Chrome — открой «Мастер WVD» и доведи вручную.",
                              "error")
    asyncio.create_task(_do())
    return {"ok": True, "msg": "Авто-минт запущен — следи за консолью Setup."}


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
        elif key == "widevine":
            # 5–15 min (downloads JRE + Android SDK + system-image + AEHD). Run
            # detached so the request returns now; progress streams to the Setup
            # console via ilog. Zero manual steps (one UAC for the kernel driver).
            asyncio.create_task(_setup.setup_widevine_toolchain())
            done = True
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


# ── Multi-account Apple wrapper pool ────────────────────────────────────────
# Each account gets its OWN wrapper container + its OWN fresh device identity
# (ripster/wrapper_pool.py) — one account cannot sustain 2+ concurrent
# sessions, and reusing another slot's device identity collides the same way
# (found 2026-07-22, project_service_gating_2026-07-22 memory). Slot 0 is
# always the primary wrapper-apple-id/wrapper-password account (the existing
# single-wrapper Settings UI); slots 1+ come from here.

@router.get("/api/wrapper/accounts")
async def wrapper_accounts_list():
    from ripster import wrapper_pool as _pool
    accounts = _pool._configured_accounts(_cfg)
    pool = _pool.get_pool(_cfg)
    status_by_slot = {s["slot"]: s for s in pool.status()} if pool else {}
    # Slot 0 (the primary wrapper) runs independently of "pool mode" — it's
    # the same always-on amd-wrapper the single-account UI already manages,
    # so its real status is available even with < 2 accounts configured
    # (when the pool object itself doesn't exist yet).
    if 0 not in status_by_slot:
        status_by_slot[0] = {"running": await _amd.check_wrapper_running(), "busy": False}
    return {
        "pool_enabled": _pool.pool_enabled(_cfg),
        "accounts": [
            {"slot": i, "label": a["label"], "primary": i == 0,
             **status_by_slot.get(i, {"running": False, "busy": False})}
            for i, a in enumerate(accounts)
        ],
    }


@router.post("/api/wrapper/accounts/order")
async def wrapper_accounts_order(body: dict):
    """Переставить Apple-аккаунты: тело {"order": [2, 0, 1]} — новые позиции
    по СТАРЫМ номерам слотов. Первый в списке становится основным.

    Зачем отдельная ручка, а не правка конфига руками: основной аккаунт живёт в
    `wrapper-apple-id`/`wrapper-password`, а остальные — в списке
    `wrapper-accounts`. Перестановка обязана переписать И то, и другое
    согласованно, иначе аккаунт задвоится или пропадёт.

    Безопасность: каталоги device-identity с 09.08.2026 именуются по аккаунту,
    а не по номеру слота, поэтому перестановка НЕ приводит к тому, что два
    аккаунта делят одну identity (это давало device-limit). Контейнеры при этом
    надо перезапустить — сделает следующий запуск пула, повторный логин
    неизбежен.
    """
    from ripster import wrapper_pool as _pool
    accounts = _pool._configured_accounts(_cfg)
    order = body.get("order") or []
    if sorted(order) != list(range(len(accounts))):
        raise HTTPException(400, f"order должен быть перестановкой 0..{len(accounts)-1}, "
                                 f"получено {order}")
    new = [accounts[i] for i in order]
    head, tail = new[0], new[1:]
    _cfg["wrapper-apple-id"]  = head["id"]
    _cfg["wrapper-password"]  = head["password"]
    _cfg["wrapper-accounts"]  = [{"id": a["id"], "password": a["password"],
                                  "label": a.get("label") or a["id"]} for a in tail]
    # Именно _save_config из контекста, а не config_service.save_config: у
    # последнего сигнатура (cfg, config_file, tokens_dir), и вызов с одним
    # аргументом упал бы в рантайме — при синтаксической корректности файла.
    if not _save_config:
        raise HTTPException(500, "сохранение конфига недоступно")
    try:
        _save_config(_cfg)
    except Exception as e:
        raise HTTPException(500, f"не сохранил конфиг: {e}")
    return {"ok": True,
            "primary": head.get("label") or head["id"],
            "order": [a.get("label") or a["id"] for a in new],
            "note": "контейнеры перезапустятся при следующем запуске пула — "
                    "каждый аккаунт останется на своей identity"}


@router.post("/api/wrapper/accounts/prefs")
async def wrapper_accounts_prefs(body: dict):
    """Приоритет и включение Apple-слотов.

    Тело: {"slots": [{"slot": 0, "priority": 2, "enabled": true}, …]}.
    Оба поля необязательны; переданное — записывается, остальное не трогается.

    Почему это ОТДЕЛЬНАЯ ручка, а не `/order`. `/order` физически переставляет
    записи, и для Apple это допустимо лишь потому, что каталоги device-identity
    именуются по аккаунту. Но перестановка меняет НОМЕР СЛОТА, то есть
    контейнер, а значит требует перелогина — а каждый перелогин жжёт слот
    устройства у Apple (скилл ripster-apple-wrapper). Приоритет ничего не
    переставляет: записи и контейнеры остаются на местах, меняется только
    очередь опроса. Это разные операции с разной ценой, и путать их нельзя.

    Приоритет действует ВНУТРИ витрины, а не поверх неё: страна решает, есть ли
    релиз вообще (`apple_accounts.pick_slot_for`).
    """
    slots = body.get("slots")
    if not isinstance(slots, list) or not slots:
        raise HTTPException(400, imsg("err.slots_required",
                                      "нужен непустой список slots"))
    from ripster import wrapper_pool as _pool
    n = len(_pool._configured_accounts(_cfg))
    extras = list(_cfg.get("wrapper-accounts") or [])
    changed = 0
    for item in slots:
        if not isinstance(item, dict):
            continue
        try:
            idx = int(item.get("slot"))
        except (TypeError, ValueError):
            continue
        if not (0 <= idx < n):
            raise HTTPException(400, imsg("err.slot_range",
                                          "слот {i} вне диапазона 0..{max}",
                                          i=idx, max=n - 1))
        has_p, has_e = "priority" in item, "enabled" in item
        if not (has_p or has_e):
            continue
        if idx == 0:
            if has_p and _cfg.get("wrapper-primary-priority") != item["priority"]:
                _cfg["wrapper-primary-priority"] = item["priority"]; changed += 1
            if has_e and _cfg.get("wrapper-primary-enabled") != bool(item["enabled"]):
                _cfg["wrapper-primary-enabled"] = bool(item["enabled"]); changed += 1
        else:
            e = extras[idx - 1]
            if not isinstance(e, dict):
                continue
            if has_p and e.get("priority") != item["priority"]:
                e["priority"] = item["priority"]; changed += 1
            if has_e and e.get("enabled") != bool(item["enabled"]):
                e["enabled"] = bool(item["enabled"]); changed += 1
    _cfg["wrapper-accounts"] = extras
    if not _save_config:
        raise HTTPException(500, imsg("err.cfg_save_unavailable",
                                      "сохранение конфига недоступно"))
    try:
        _save_config(_cfg)
    except Exception as e:
        raise HTTPException(500, imsg("err.cfg_save_failed",
                                      "не сохранил конфиг: {e}", e=str(e)))
    # Число, а не «готово»: массовое действие без счётчика неотличимо от
    # действия вхолостую (урок вишлиста, 22.08.2026).
    return {"ok": True, "changed": changed, "slots": n}


@router.post("/api/wrapper/accounts/add")
async def wrapper_accounts_add(body: dict):
    """Add an additional Apple account to the pool and start its wrapper.
    Does NOT touch the primary wrapper-apple-id/wrapper-password (slot 0)."""
    apple_id = (body.get("id") or "").strip()
    password = (body.get("password") or "").strip()
    label    = (body.get("label") or "").strip() or apple_id
    if not apple_id or not password:
        return {"ok": False, "msg": "Нужны id и password"}

    existing = list(_cfg.get("wrapper-accounts") or [])
    if any(a.get("id") == apple_id for a in existing):
        return {"ok": False, "msg": "Этот аккаунт уже добавлен"}
    existing.append({"id": apple_id, "password": password, "label": label})
    _cfg["wrapper-accounts"] = existing
    if _save_config:
        try:
            _save_config(_cfg)
        except Exception as e:
            return {"ok": False, "msg": f"Не сохранил конфиг: {e}"}

    from ripster import wrapper_pool as _pool
    if not _pool.pool_enabled(_cfg):
        return {"ok": True, "msg": "Аккаунт сохранён. Включи apple-pool, чтобы поднять враппер под него.",
                "started": False}

    def _do():
        try:
            p = _pool.get_pool(_cfg)
            slot = len(_pool._configured_accounts(_cfg)) - 1
            p.ensure(slot + 1)
        except Exception as e:
            print(f"[wrapper-pool] failed to start slot for new account: {e}", flush=True)
    asyncio.create_task(asyncio.to_thread(_do))
    return {"ok": True, "msg": f"Аккаунт добавлен, запускаю враппер…", "started": True}


@router.post("/api/wrapper/accounts/{slot}/remove")
async def wrapper_accounts_remove(slot: int):
    """Remove an additional account (slot >= 1 only — slot 0 is the primary
    account, managed via the regular wrapper-apple-id/wrapper-password UI)."""
    if slot < 1:
        return {"ok": False, "msg": "Слот 0 — основной аккаунт, убирается через обычные настройки Apple"}
    existing = list(_cfg.get("wrapper-accounts") or [])
    idx = slot - 1
    if idx < 0 or idx >= len(existing):
        return {"ok": False, "msg": "Нет такого аккаунта"}
    removed = existing.pop(idx)
    _cfg["wrapper-accounts"] = existing
    if _save_config:
        try:
            _save_config(_cfg)
        except Exception as e:
            return {"ok": False, "msg": f"Не сохранил конфиг: {e}"}

    def _stop():
        try:
            from ripster.wrapper_pool import _client, NAME_PREFIX
            c = _client()
            c.containers.get(f"{NAME_PREFIX}{slot}").remove(force=True)
        except Exception:
            pass
    await asyncio.to_thread(_stop)
    return {"ok": True, "msg": f"Аккаунт {removed.get('label', '')} убран"}


# ── Deezer multi-account pool (load-balanced, no Docker) ───────────────────────
#   GET  /api/deezer/accounts        — list ARL accounts in the pool
#   POST /api/deezer/accounts/add    — add an ARL account
#   POST /api/deezer/accounts/{slot}/remove — remove an added account (not slot 0)

@router.get("/api/deezer/accounts")
async def deezer_accounts_list(probe: int = 0):
    """Учётки Deezer. С ?probe=1 — дополнительно спрашивает КАЖДЫЙ ARL о его
    стране, тарифе и сроке.

    Почему опцией, а не всегда: проба — это сетевой запрос на учётку, а список
    открывают часто. Без пробы отдаём то же, что и раньше, мгновенно.

    Зачем вообще: до 09.08.2026 страна была известна только у ОСНОВНОЙ учётки —
    пул хранил ARL, но ничего о его владельце. Для Apple такое незнание стоило
    целой ночи разбирательств (задача #17), поэтому закрываем ту же дыру здесь,
    не дожидаясь повторения.
    """
    from ripster import deezer_pool as _dzp
    base = _dzp.live_status(_cfg)
    if not probe:
        return base
    try:
        from ripster import deezer_accounts as _dza
        return {**(base if isinstance(base, dict) else {"pool": base}),
                "probe": await _dza.survey(_cfg)}
    except Exception as e:
        return {**(base if isinstance(base, dict) else {"pool": base}),
                "probe_error": f"{type(e).__name__}: {e}"}


@router.post("/api/deezer/accounts/add")
async def deezer_accounts_add(body: dict):
    """Add an additional Deezer ARL to the pool. Does NOT touch the primary
    deezer-arl (slot 0) — takes effect on the NEXT queued Deezer download that
    goes through the pool dispatch (ripster/runner.py), no restart needed."""
    arl   = (body.get("arl") or "").strip()
    label = (body.get("label") or "").strip() or "account"
    if not arl:
        return {"ok": False, "msg": "Нужен ARL"}

    existing = list(_cfg.get("deezer-accounts") or [])
    if any(a.get("arl") == arl for a in existing):
        return {"ok": False, "msg": "Этот ARL уже добавлен"}
    existing.append({"arl": arl, "label": label})
    _cfg["deezer-accounts"] = existing
    if _save_config:
        try:
            _save_config(_cfg)
        except Exception as e:
            return {"ok": False, "msg": f"Не сохранил конфиг: {e}"}
    return {"ok": True, "msg": f"ARL добавлен как «{label}»"}


@router.post("/api/deezer/accounts/{slot}/remove")
async def deezer_accounts_remove(slot: int):
    """Remove an additional account (slot >= 1 only — slot 0 is the primary
    ARL, managed via the regular deezer-arl field in Settings → Deezer)."""
    if slot < 1:
        return {"ok": False, "msg": "Слот 0 — основной ARL, убирается через обычные настройки Deezer"}
    existing = list(_cfg.get("deezer-accounts") or [])
    idx = slot - 1
    if idx < 0 or idx >= len(existing):
        return {"ok": False, "msg": "Нет такого аккаунта"}
    removed = existing.pop(idx)
    _cfg["deezer-accounts"] = existing
    if _save_config:
        try:
            _save_config(_cfg)
        except Exception as e:
            return {"ok": False, "msg": f"Не сохранил конфиг: {e}"}
    return {"ok": True, "msg": f"Аккаунт {removed.get('label', '')} убран"}


# ── Qobuz multi-account pool (load-balanced, no Docker) ────────────────────────
#   GET  /api/qobuz/accounts        — list accounts in the pool
#   POST /api/qobuz/accounts/add    — add an account (token or email/password mode)
#   POST /api/qobuz/accounts/{slot}/remove — remove an added account (not slot 0)

@router.get("/api/qobuz/accounts")
async def qobuz_accounts_list():
    from ripster import qobuz_pool as _qzp
    return _qzp.live_status(_cfg)


@router.post("/api/qobuz/accounts/add")
async def qobuz_accounts_add(body: dict):
    """Add an additional Qobuz account to the pool — either token mode
    (user_id+auth_token) or email mode (email+password). Does NOT touch the
    primary qobuz-* config keys (slot 0). Takes effect on the NEXT queued
    Qobuz download that goes through the pool dispatch, no restart needed."""
    user_id    = (body.get("user_id") or "").strip()
    auth_token = (body.get("auth_token") or "").strip()
    email      = (body.get("email") or "").strip()
    password   = (body.get("password") or "").strip()
    label      = (body.get("label") or "").strip() or email or user_id or "account"
    if not ((user_id and auth_token) or email):
        return {"ok": False, "msg": "Нужны user_id+auth_token ИЛИ email+password"}

    existing = list(_cfg.get("qobuz-accounts") or [])
    if any((a.get("user_id") == user_id and user_id) or (a.get("email") == email and email)
           for a in existing):
        return {"ok": False, "msg": "Этот аккаунт уже добавлен"}
    existing.append({"user_id": user_id, "auth_token": auth_token,
                     "email": email, "password": password, "label": label})
    _cfg["qobuz-accounts"] = existing
    if _save_config:
        try:
            _save_config(_cfg)
        except Exception as e:
            return {"ok": False, "msg": f"Не сохранил конфиг: {e}"}
    return {"ok": True, "msg": f"Аккаунт добавлен как «{label}»"}


@router.post("/api/qobuz/accounts/{slot}/remove")
async def qobuz_accounts_remove(slot: int):
    """Remove an additional account (slot >= 1 only — slot 0 is the primary
    account, managed via the regular Settings → Qobuz fields)."""
    if slot < 1:
        return {"ok": False, "msg": "Слот 0 — основной аккаунт, убирается через обычные настройки Qobuz"}
    existing = list(_cfg.get("qobuz-accounts") or [])
    idx = slot - 1
    if idx < 0 or idx >= len(existing):
        return {"ok": False, "msg": "Нет такого аккаунта"}
    removed = existing.pop(idx)
    _cfg["qobuz-accounts"] = existing
    if _save_config:
        try:
            _save_config(_cfg)
        except Exception as e:
            return {"ok": False, "msg": f"Не сохранил конфиг: {e}"}
    return {"ok": True, "msg": f"Аккаунт {removed.get('label', '')} убран"}


# ── SoundCloud multi-account pool (load-balanced, token is a plain CLI arg) ────
#   GET  /api/soundcloud/accounts        — list accounts in the pool
#   POST /api/soundcloud/accounts/add    — add a token
#   POST /api/soundcloud/accounts/{slot}/remove — remove an added account (not slot 0)

@router.get("/api/soundcloud/accounts")
async def soundcloud_accounts_list():
    from ripster import soundcloud_pool as _scp
    return _scp.live_status(_cfg)


@router.post("/api/soundcloud/accounts/add")
async def soundcloud_accounts_add(body: dict):
    token = (body.get("token") or "").strip()
    label = (body.get("label") or "").strip() or "account"
    if not token:
        return {"ok": False, "msg": "Нужен токен"}
    existing = list(_cfg.get("soundcloud-accounts") or [])
    if any(a.get("token") == token for a in existing):
        return {"ok": False, "msg": "Этот токен уже добавлен"}
    existing.append({"token": token, "label": label})
    _cfg["soundcloud-accounts"] = existing
    if _save_config:
        try:
            _save_config(_cfg)
        except Exception as e:
            return {"ok": False, "msg": f"Не сохранил конфиг: {e}"}
    return {"ok": True, "msg": f"Аккаунт добавлен как «{label}»"}


@router.post("/api/soundcloud/accounts/{slot}/remove")
async def soundcloud_accounts_remove(slot: int):
    if slot < 1:
        return {"ok": False, "msg": "Слот 0 — основной токен, убирается через обычные настройки SoundCloud"}
    existing = list(_cfg.get("soundcloud-accounts") or [])
    idx = slot - 1
    if idx < 0 or idx >= len(existing):
        return {"ok": False, "msg": "Нет такого аккаунта"}
    removed = existing.pop(idx)
    _cfg["soundcloud-accounts"] = existing
    if _save_config:
        try:
            _save_config(_cfg)
        except Exception as e:
            return {"ok": False, "msg": f"Не сохранил конфиг: {e}"}
    return {"ok": True, "msg": f"Аккаунт {removed.get('label', '')} убран"}


# ── Yandex Music multi-account pool (load-balanced, token is a plain CLI arg) ──
#   GET  /api/yandex/accounts        — list accounts in the pool
#   POST /api/yandex/accounts/add    — add a token
#   POST /api/yandex/accounts/{slot}/remove — remove an added account (not slot 0)

# ── Приоритет и включение учёток — ОБЩЕЕ для всех пулов ───────────────────────
# Отдельной вкладки «учётные записи» нет намеренно: приоритет живёт там же, где
# сама учётка, то есть внутри вкладки своего сервиса. Иначе человеку пришлось бы
# держать в голове соответствие между двумя списками.
#
# У Apple своя ручка (`/api/wrapper/accounts/prefs`): там слот это контейнер, и
# приоритет действует ВНУТРИ витрины, потому что страна решает, существует ли
# релиз вообще. Здесь всё проще — приоритет это просто очередь.
_PREFS_POOLS = ("deezer", "qobuz", "soundcloud", "yandex")


@router.post("/api/{service}/accounts/prefs")
async def pool_accounts_prefs(service: str, body: dict):
    """Тело: {"slots": [{"slot": 0, "priority": 2, "enabled": true}, …]}.

    Слот 0 — ОСНОВНАЯ учётка: у неё нет своего словаря, она размазана по плоским
    ключам конфига, поэтому её настройки лежат в `<svc>-primary-priority` и
    `<svc>-primary-enabled`. Имена собирает `account_fallback.primary_src` — не
    повторять их здесь по памяти: общий ключ `priority` был бы один на все
    сервисы сразу.

    Возвращает число ИЗМЕНЁННЫХ полей: действие над списком без счётчика
    неотличимо от действия вхолостую (урок вишлиста, 22.08.2026).
    """
    svc = (service or "").strip().lower()
    if svc not in _PREFS_POOLS:
        raise HTTPException(404, imsg("err.prefs_no_pool",
                                      "у сервиса {svc} нет пула учёток", svc=svc))
    slots = body.get("slots")
    if not isinstance(slots, list) or not slots:
        raise HTTPException(400, imsg("err.slots_required", "нужен непустой список slots"))

    extras = list(_cfg.get(f"{svc}-accounts") or [])
    changed = 0
    for item in slots:
        if not isinstance(item, dict):
            continue
        try:
            idx = int(item.get("slot"))
        except (TypeError, ValueError):
            continue
        if not (0 <= idx <= len(extras)):
            raise HTTPException(400, imsg("err.slot_range",
                                          "слот {i} вне диапазона 0..{max}",
                                          i=idx, max=len(extras)))
        if "priority" in item:
            try:
                val = float(item["priority"])
            except (TypeError, ValueError):
                raise HTTPException(400, imsg("err.priority_number",
                                              "приоритет должен быть числом"))
            if idx == 0:
                if _cfg.get(f"{svc}-primary-priority") != val:
                    _cfg[f"{svc}-primary-priority"] = val
                    changed += 1
            elif extras[idx - 1].get("priority") != val:
                extras[idx - 1]["priority"] = val
                changed += 1
        if "enabled" in item:
            val = bool(item["enabled"])
            if idx == 0:
                if bool(_cfg.get(f"{svc}-primary-enabled", True)) != val:
                    _cfg[f"{svc}-primary-enabled"] = val
                    changed += 1
            elif bool(extras[idx - 1].get("enabled", True)) != val:
                extras[idx - 1]["enabled"] = val
                changed += 1

    if changed:
        _cfg[f"{svc}-accounts"] = extras
        if _save_config:
            try:
                _save_config(_cfg)
            except Exception as e:
                return {"ok": False, "msg": f"Не сохранил конфиг: {e}"}
    return {"ok": True, "changed": changed, "slots": len(extras) + 1}


@router.get("/api/yandex/accounts")
async def yandex_accounts_list():
    from ripster import yandex_pool as _yxp
    return _yxp.live_status(_cfg)


@router.post("/api/yandex/accounts/add")
async def yandex_accounts_add(body: dict):
    token = (body.get("token") or "").strip()
    label = (body.get("label") or "").strip() or "account"
    if not token:
        return {"ok": False, "msg": "Нужен токен"}
    existing = list(_cfg.get("yandex-accounts") or [])
    if any(a.get("token") == token for a in existing):
        return {"ok": False, "msg": "Этот токен уже добавлен"}
    existing.append({"token": token, "label": label})
    _cfg["yandex-accounts"] = existing
    if _save_config:
        try:
            _save_config(_cfg)
        except Exception as e:
            return {"ok": False, "msg": f"Не сохранил конфиг: {e}"}
    return {"ok": True, "msg": f"Аккаунт добавлен как «{label}»"}


@router.post("/api/yandex/accounts/{slot}/remove")
async def yandex_accounts_remove(slot: int):
    if slot < 1:
        return {"ok": False, "msg": "Слот 0 — основной токен, убирается через обычные настройки Yandex"}
    existing = list(_cfg.get("yandex-accounts") or [])
    idx = slot - 1
    if idx < 0 or idx >= len(existing):
        return {"ok": False, "msg": "Нет такого аккаунта"}
    removed = existing.pop(idx)
    _cfg["yandex-accounts"] = existing
    if _save_config:
        try:
            _save_config(_cfg)
        except Exception as e:
            return {"ok": False, "msg": f"Не сохранил конфиг: {e}"}
    return {"ok": True, "msg": f"Аккаунт {removed.get('label', '')} убран"}


# ── OrpheusDL-Spotify ─────────────────────────────────────────────────────────

def _orpheus_dir() -> Path:
    return _base_dir / "orpheus"

def _orpheus_creds_path() -> Path:
    return _orpheus_dir() / "config" / "credentials.json"

_oauth_proc: asyncio.subprocess.Process | None = None
# URL последнего login-start — чтобы login-open мог открыть его системным
# браузером. Держим на сервере, а не принимаем от клиента: открывать любой
# присланный URL на машине хозяина нельзя.
_oauth_url: str = ""


@router.get("/api/orpheus/status")
async def orpheus_status():
    from ripster.engines.orpheus_spotify import is_installed, is_authenticated, session_kind
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
        # "blob" = постоянная сессия librespot (то, чем реально качается),
        # "oauth" = слабая PKCE-сессия, "" = входа нет. Без этого различия UI
        # показывал одинаково зелёное «✓ Авторизован» в обоих случаях.
        "session":        session_kind(),
        "username":       username,
        "mode":           _cfg.get("spotify-engine", "convert"),
        "quality":        _cfg.get("orpheus-quality", "hifi"),
    }


@router.post("/api/orpheus/login-start")
async def orpheus_login_start():
    """Start PKCE OAuth flow. Returns Spotify auth URL for the browser popup."""
    global _oauth_proc, _oauth_url

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

    # RIPSTER_RETURN_URL: the helper's callback page bounces the browser back
    # here when it's done, so the in-window login option has somewhere to
    # return to and the UI re-checks auth without an app restart.
    from ripster.routes.core import _return_url
    env = {**os.environ, "PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION": "python",
           "PYTHONIOENCODING": "utf-8",
           "RIPSTER_RETURN_URL": _return_url()}
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

    _oauth_url = auth_url or ""
    asyncio.create_task(_watch_orpheus_oauth())
    return {"ok": True, "url": auth_url}


@router.post("/api/orpheus/login-open")
async def orpheus_login_open(request: Request):
    """Открыть страницу входа Spotify системным браузером.

    Окно Ripster.exe — это WebView2, и он молча режет window.open(): попап
    приходит null, фронт показывал «Popup заблокирован» и отменял вход. То есть
    из exe залогиниться было невозможно в принципе, а из браузерного ярлыка тот
    же самый билд работал — так и обнаружили (два пользователя, 27.07.2026).

    Открываем только URL, который сам же выдал login-start, и только если
    запрос пришёл с этой машины: через туннель браузер хозяина открывать нельзя.
    """
    host = (request.client.host if request.client else "") or ""
    if host not in ("127.0.0.1", "::1", "localhost"):
        return {"ok": False, "error": "Открыть браузер можно только на этой машине — "
                                      "скопируй ссылку входа вручную"}
    if not _oauth_url:
        return {"ok": False, "error": "Нет активной ссылки входа — нажми «Войти в Spotify» заново"}

    def _open() -> bool:
        import webbrowser
        try:
            if webbrowser.open(_oauth_url):
                return True
        except Exception:
            pass
        try:                       # запасной путь: штатный обработчик URL Windows
            os.startfile(_oauth_url)  # type: ignore[attr-defined]
            return True
        except Exception:
            return False

    if not await asyncio.to_thread(_open):
        return {"ok": False, "error": "Не удалось открыть браузер — скопируй ссылку входа вручную"}
    return {"ok": True}


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
    global _oauth_proc, _oauth_url
    _oauth_url = ""
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


def _orpheus_venv_python() -> "str | None":
    """The isolated OrpheusDL venv interpreter, if provisioned (tools/orpheusvenv)."""
    for sub in (("Scripts", "python.exe"), ("bin", "python")):
        cand = _base_dir / "tools" / "orpheusvenv" / sub[0] / sub[1]
        if cand.is_file():
            return str(cand)
    return None


async def _ensure_orpheus_venv() -> "str | None":
    """Create (if needed) the isolated OrpheusDL venv and return its python path,
    or None on failure (caller falls back to the shared interpreter). Keeps
    OrpheusDL's protobuf==3.15.8 out of the shared bundled python."""
    vpy = _orpheus_venv_python()
    if vpy:
        return vpy
    venv = _base_dir / "tools" / "orpheusvenv"
    try:
        await _setup.irun([sys.executable, "-m", "venv", str(venv)])
        if not _orpheus_venv_python():
            # Bundled embeddable python lacks the stdlib `venv` module → virtualenv.
            await _setup.irun([sys.executable, "-m", "pip", "install", "-q",
                               "--break-system-packages", "virtualenv"])
            await _setup.irun([sys.executable, "-m", "virtualenv", str(venv)])
    except Exception as e:
        await _setup.ilog(f"⚠ OrpheusDL venv не создан ({e}) — ставлю в общий python", "warn")
        return None
    return _orpheus_venv_python()


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
        # `git clone` РУГАЕТСЯ на непустой каталог: "destination path already
        # exists and is not an empty directory". А каталог существует всегда —
        # установщик кладёт туда наш _auth_helper.py (вход в Spotify). Поэтому
        # клонируем во временную папку рядом и переносим содержимое, сохраняя
        # то, что уже лежало.
        tmp_dir = orph_dir.parent / (orph_dir.name + "_clone_tmp")
        try:
            if tmp_dir.exists():
                shutil.rmtree(tmp_dir, ignore_errors=True)
            rc, _ = await _setup.irun(
                [git, "clone", "https://github.com/OrfiTeam/OrpheusDL", str(tmp_dir)])
            if rc != 0:
                await _setup.ilog("✗ Ошибка git clone OrpheusDL", "error")
                shutil.rmtree(tmp_dir, ignore_errors=True)
                return False
            orph_dir.mkdir(parents=True, exist_ok=True)
            for item in tmp_dir.iterdir():
                dst = orph_dir / item.name
                if dst.exists():
                    continue          # своё (например _auth_helper.py) не затираем
                shutil.move(str(item), str(dst))
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        if not (orph_dir / "orpheus.py").exists():
            await _setup.ilog("✗ OrpheusDL склонирован не полностью", "error")
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

    # Isolate OrpheusDL in its OWN venv: its requirements pin protobuf==3.15.8,
    # which — installed into the shared bundled python — breaks AMD (Apple) and
    # pywidevine (both need protobuf>=6.33). The engines run orpheus under this
    # venv's python (_orpheus_python). See the ripster-dependency-versions skill.
    pip_py = await _ensure_orpheus_venv() or sys.executable
    if pip_py != sys.executable:
        await _setup.ilog("⚙ OrpheusDL → изолированный venv (tools/orpheusvenv)", "info")
    req = orph_dir / "requirements.txt"
    if req.exists():
        await _setup.ilog("📦 pip install OrpheusDL requirements…", "info")
        await _setup.irun([pip_py, "-m", "pip", "install", "-r", str(req), "--quiet"],
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
        # Into the SAME isolated OrpheusDL venv (Beatport runs on top of OrpheusDL).
        pip_py = await _ensure_orpheus_venv() or sys.executable
        await _setup.ilog("📦 pip install requirements.txt (OrpheusDL venv)…", "info")
        await _setup.irun([pip_py, "-m", "pip", "install", "-r", str(req), "--quiet"],
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

        # ── construct: пин, без которого Apple перестаёт качать целиком ──────
        #
        # `pywidevine/device.py` пишет `Const(Int8ub, 2)`. Этот порядок аргументов
        # принимает ТОЛЬКО construct 2.8.8 — замерено перебором версий 22.08.2026:
        # 2.8.22, 2.9.45 и вся ветка 2.10.x поднимают
        # `TypeError: subcon should be a Construct field`. Правильный для них
        # порядок — `Const(2, Int8ub)`, но чинить чужой пакет мы не будем.
        # Сам pywidevine пин не объявляет: 2.8.8 приезжает транзитивно от pymp4.
        #
        # Цена промаха несоразмерна: AMD импортирует pywidevine в
        # `src/legacy/decrypt.py` на уровне модуля, поэтому кривой construct
        # убивает ВСЕ загрузки Apple на этапе BOOT — до единого сетевого запроса.
        # Именно так 22.08 человек получил три одинаковых трейсбека подряд.
        #
        # `--no-deps` обязателен: без него pip тянет за construct зависимости и
        # может снова сдвинуть версию, ради которой всё и делается.
        #
        # ВОССТАНОВЛЕНО 23.08.2026. Этот блок уже был здесь в 3.6.3 (89487d7) и
        # исчез в 30bfb0b: правка писалась поверх устаревшей копии файла и снесла
        # главную правку релиза заодно со своей целью. На этот раз покрыто
        # tests/test_fix_deps.py — тест читает ИСХОДНИК этой функции, поэтому
        # следующая такая запись упадёт, а не уедет в релиз молча.
        rc3, o3 = await _setup.irun([sys.executable, "-m", "pip", "install",
                                      "construct==2.8.8", "--no-deps",
                                      "--force-reinstall",
                                      "--break-system-packages", "-q"])
        if rc3 == 0:
            await _setup.ilog("   ✓ construct закреплён на 2.8.8 (нужен pywidevine)", "success")
        else:
            await _setup.ilog(f"   ✗ construct pin failed: {o3[:100]}", "error")

        # Проверка, которая МОЖЕТ провалиться: импорт, а не наличие файла.
        rc4, o4 = await _setup.irun([sys.executable, "-c",
            "from pywidevine.device import Device; print('WVD_IMPORT_OK')"])
        if rc4 == 0 and "WVD_IMPORT_OK" in (o4 or ""):
            await _setup.ilog("   ✓ pywidevine импортируется — Apple сможет качать", "success")
        else:
            await _setup.ilog("   ✗ pywidevine НЕ импортируется — загрузки Apple упадут "
                              f"на старте: {(o4 or '')[-160:]}", "error")
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
    Heavy deps + user data untouched. On success the NEW code is already on disk —
    we then EXIT this server so the stale (old-version) process can't survive: the
    bundled headless server keeps running after the window is closed, and the
    launcher would otherwise re-attach to it on reopen → 'update did nothing'.
    Killing it means the launcher respawns (window open) or the user's reopen starts
    fresh — either way loading the new version. Heavy deps/user data untouched."""
    from ripster import updater
    res = await updater.apply_update(_cfg, _base_dir)
    if res.get("ok"):
        import threading, time as _t
        def _bye():
            _t.sleep(3.0)          # let the HTTP response reach the UI first
            _respawn_detached()    # guarantee a successor regardless of launcher state
            os._exit(0)            # overlay already wrote new code → fresh start = new version
        threading.Thread(target=_bye, daemon=True).start()
    return res


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
        elif os.name == "nt":
            # os.execv on Windows re-execs in a NEW console-subsystem process WITHOUT
            # the no-window flag → a cmd window pops. Use the windowless detached
            # respawn instead (same successor mechanism as the launcher path).
            _respawn_detached()
            os._exit(0)
        else:
            os.execv(sys.executable, [sys.executable] + sys.argv)

    threading.Thread(target=_do, daemon=True).start()
    return {"ok": True}


# ── Обзор аккаунтов: страны, часовые пояса, ранняя доступность ────────────────
# Владелец не видел, работает ли учётка, из какой она СТРАНЫ и какой ЧАСОВОЙ ПОЯС
# — а на этом держится стратегия ранней доступности (НЗ-Tidal ловит пятницу на
# полсуток раньше US-Apple). Этот обзор показывает по каждому сервису страну,
# местное время и НАСКОЛЬКО раньше он входит в новый день.
_COUNTRY_TZ = {
    "KI": ("Pacific/Kiritimati", "Кирибати", "🇰🇮"),
    "NZ": ("Pacific/Auckland", "Новая Зеландия", "🇳🇿"),
    "TO": ("Pacific/Tongatapu", "Тонга", "🇹🇴"),
    "WS": ("Pacific/Apia", "Самоа", "🇼🇸"),
    "FJ": ("Pacific/Fiji", "Фиджи", "🇫🇯"),
    "AU": ("Australia/Sydney", "Австралия", "🇦🇺"),
    "JP": ("Asia/Tokyo", "Япония", "🇯🇵"),
    "KR": ("Asia/Seoul", "Корея", "🇰🇷"),
    "CN": ("Asia/Shanghai", "Китай", "🇨🇳"),
    "SG": ("Asia/Singapore", "Сингапур", "🇸🇬"),
    "AE": ("Asia/Dubai", "ОАЭ", "🇦🇪"),
    "IN": ("Asia/Kolkata", "Индия", "🇮🇳"),
    "RU": ("Europe/Moscow", "Россия", "🇷🇺"),
    "ZA": ("Africa/Johannesburg", "ЮАР", "🇿🇦"),
    "DE": ("Europe/Berlin", "Германия", "🇩🇪"),
    "FR": ("Europe/Paris", "Франция", "🇫🇷"),
    "IT": ("Europe/Rome", "Италия", "🇮🇹"),
    "GB": ("Europe/London", "Великобритания", "🇬🇧"),
    "BR": ("America/Sao_Paulo", "Бразилия", "🇧🇷"),
    "US": ("America/New_York", "США", "🇺🇸"),
    "CA": ("America/Toronto", "Канада", "🇨🇦"),
    "MX": ("America/Mexico_City", "Мексика", "🇲🇽"),
}


def _country_info(cc: str) -> dict:
    from datetime import datetime, timezone as _tz
    cc = (cc or "").strip().upper()
    tz, name, flag = _COUNTRY_TZ.get(cc, (None, cc or "?", "🏳"))
    off_h, local = None, ""
    if tz:
        try:
            from zoneinfo import ZoneInfo
            now = datetime.now(ZoneInfo(tz))
            off_h = now.utcoffset().total_seconds() / 3600.0
            local = now.strftime("%a %H:%M")
        except Exception:
            pass
    # Название страны НЕ отдаём: раньше оно уезжало по-русски и в английском
    # интерфейсе так и оставалось русским. Сервер отдаёт код и флаг, название
    # подставляет клиент по ключу cc.<КОД>.
    return {"country": cc, "flag": flag,
            "timezone": tz or "", "offset": off_h, "local_time": local}


@router.get("/api/accounts/overview")
async def accounts_overview():
    """Единый обзор всех учёток: страна, часовой пояс, местное время, кто раньше
    входит в новый день (ранняя доступность). Дёшево — только из config, без
    сетевых запросов. Срок подписки/детальный статус — в валидации каждого сервиса."""
    from ripster import wrapper_pool as _pool

    def _has(k):
        v = _cfg.get(k)
        return bool(v.strip()) if isinstance(v, str) else bool(v)

    def _sub(svc: str) -> dict:
        """Срок подписки, запомненный последней пробой /api/test-auth/<svc>."""
        active = _cfg.get(f"{svc}-sub-active")
        end = _cfg.get(f"{svc}-sub-end") or ""
        if not end:
            return {"sub_end": "", "sub_days_left": None, "sub_active": active,
                    "checked_at": _cfg.get(f"{svc}-checked-at")}
        dl = _cfg.get(f"{svc}-sub-days-left")
        # Дни считаем от СЕГОДНЯ, а не берём число из кэша: оно было верным в
        # день проверки и с тех пор тихо устарело бы ровно на столько же дней.
        try:
            from datetime import date as _date
            y, m, d = (int(x) for x in str(end)[:10].split("-"))
            dl = (_date(y, m, d) - _date.today()).days
        except Exception:
            dl = dl if isinstance(dl, int) else None
        return {"sub_end": str(end)[:10], "sub_days_left": dl, "sub_active": active,
                "checked_at": _cfg.get(f"{svc}-checked-at")}

    def _pool_n(svc: str) -> int:
        """Сколько учёток у сервиса: основная + пул <svc>-accounts.

        Раньше всем, кроме Apple, проставлялась единица, и Deezer с двумя
        аккаунтами выглядел как один. Считаем честно."""
        extra = _cfg.get(f"{svc}-accounts")
        return 1 + (len(extra) if isinstance(extra, (list, tuple)) else 0)

    def _entry(svc, label, configured, accounts, cc):
        # Страну мы знаем только у ОСНОВНОЙ учётки: пул хранит логин/ARL, но не
        # страну, а у пулового аккаунта она вполне может быть другой — на этом и
        # держится ранняя доступность. Помечаем, чтобы панель не выдавала страну
        # основной за страну всего пула.
        return {"service": svc, "label": label, "configured": configured,
                "accounts": accounts,
                "country_is_primary_only": accounts > 1,
                **_country_info(cc), **_sub(svc)}

    out = []

    # Apple — страна ПО ПРОБЕ, если она была: config storefront это то, что
    # человек вписал, а /v1/me/storefront отвечает, где учётка на самом деле
    # (у владельца было записано US, а аккаунт оказался CA).
    apple_cc = (_cfg.get("apple-country") or _cfg.get("storefront") or "us").upper()
    pool_n = len(_pool._configured_accounts(_cfg)) if hasattr(_pool, "_configured_accounts") else 1
    out.append(_entry("apple", "Apple Music", True, pool_n, apple_cc))

    # Tidal — страна автоопределяется из аккаунта (tidal-country), ключевой для НЗ-форы
    if _has("tidal-token") or _has("tidal-country"):
        out.append(_entry("tidal", "Tidal", _has("tidal-token"), _pool_n("tidal"),
                          _cfg.get("tidal-country") or ""))

    # Остальные: страну кладёт проба сервиса в <svc>-country (см.
    # routes/auth.py:_remember_probe). Пока человек ни разу не жал «проверить»,
    # страна честно неизвестна — выдумывать её неоткуда.
    if _has("deezer-arl"):
        out.append(_entry("deezer", "Deezer", True, _pool_n("deezer"), _cfg.get("deezer-country") or ""))
    if _has("qobuz-auth-token") or (_has("qobuz-email") and _has("qobuz-password")):
        out.append(_entry("qobuz", "Qobuz", True, _pool_n("qobuz"), _cfg.get("qobuz-country") or ""))
    if _has("spotify-client-id") or _has("spotify-sp-dc"):
        out.append(_entry("spotify", "Spotify", True, _pool_n("spotify"),
                          _cfg.get("spotify-country") or _cfg.get("spotify-market") or ""))
    if _has("soundcloud-oauth-token"):
        out.append(_entry("soundcloud", "SoundCloud", True, _pool_n("soundcloud"),
                          _cfg.get("soundcloud-country") or ""))

    # ранняя доступность: чем больше offset, тем раньше входит в новый день
    known = [o for o in out if o.get("offset") is not None]
    known.sort(key=lambda o: o["offset"], reverse=True)
    for i, o in enumerate(known):
        o["early_rank"] = i + 1
    earliest = known[0] if known else None
    apple_off = next((o["offset"] for o in out if o["service"] == "apple"
                      and o["offset"] is not None), None)
    # Подсказку отдаём РАЗОБРАННОЙ, а не готовой фразой: собранный на сервере
    # русский текст нельзя перевести на клиенте, он так и приезжал русским в
    # английский интерфейс. Здесь — только числа и коды, фраза собирается из
    # ключа s.acc_hint.
    hint = None
    if earliest and apple_off is not None and earliest["service"] != "apple":
        dh = earliest["offset"] - apple_off
        if dh > 0:
            hint = {"flag": earliest["flag"], "label": earliest["label"],
                    "country": earliest["country"], "hours": round(dh),
                    "vs": "Apple"}
    return {"ok": True, "accounts": out,
            "earliest": earliest["service"] if earliest else None, "hint": hint}
