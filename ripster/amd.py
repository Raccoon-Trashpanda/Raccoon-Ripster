"""ripster.amd — управление локальным Docker-враппером Apple.

    install(cfg, broadcast_fn, save_config_fn, base_dir, is_windows)

    # Docker wrapper
    check_wrapper_running()    — TCP check on decrypt port
    check_docker_installed()   — (bool, path_or_error)
    start_wrapper_docker()     — pull + start wrapper container
    stop_wrapper_docker()      — stop wrapper container
    pull_wrapper_image()       — docker pull wrapper image

Публичный враппер к этому проводу отношения не имеет: он живёт на `lite`
(Wrapper-Lite HTTP API — ripster/lite.py, ripster/apple_router.py). Имя модуля
и контейнера (`amd-wrapper`) остались от прежнего транспорта и не меняются: их
помнит и настройка владельца, и инфраструктура Docker.
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import socket
import subprocess
import time
from pathlib import Path
from typing import Optional

# Hide the transient console window Windows opens for every child process. Without
# this, the wrapper-status poll (docker info, every ~10s) flashes a cmd window
# constantly on the owner's desktop.
_CNW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

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
# Диалог «Invalid store» печатается на КАЖДЫЙ запрос ключа, пока аккаунт не в
# своей стране — без глушилки владелец получил бы спам в боте каждые несколько
# секунд. Храним момент последней отправки, чтобы повторить не чаще раза в час.
_last_store_alert_ts: float = 0.0


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


# ─── Mode / path helpers ──────────────────────────────────────────────────────

def _wrapper_mode() -> str:
    return _cfg.get("wrapper-mode", "docker-remote")


def _publish(spec: str, container_port: int) -> str:
    """Строка для `docker -p`, в которой ХОСТ не теряется.

    Раньше из настройки `decrypt-port: 127.0.0.1:10020` брался только хвост
    после двоеточия, и докер получал `-p 10020:10020` — а это публикация на
    ВСЕ интерфейсы. 21.09.2026 так и оказалось: порт 30020 без авторизации
    отдавал `dev_token` и media-user-token активной учётки любому в сети.

    Правила:
      * «127.0.0.1:10020» — хост уважаем;
      * «10020» — умолчание ПЕТЛЯ, а не 0.0.0.0: локальный враппер наружу не
        нужен, и безопасное поведение должно получаться само, без настройки;
      * «0.0.0.0:10020» — уважаем, но это осознанное открытие наружу.
    """
    s = str(spec or "").strip()
    host, _, port = s.rpartition(":")
    port = (port or s).strip() or str(container_port)
    host = (host or "127.0.0.1").strip() or "127.0.0.1"
    return f"{host}:{port}:{container_port}"


def _wrapper_account_port() -> str:
    """Host port to publish the wrapper's account-info API (container 30020) on.
    Derived from `gamdl-wrapper-account-url` so both point at the same place;
    defaults to 30020."""
    url = (_cfg.get("gamdl-wrapper-account-url") or "").strip()
    if url:
        tail = url.rstrip("/").rsplit(":", 1)[-1]
        if tail.isdigit():
            return tail
    return "30020"


async def _harvest_wrapper_token() -> None:
    """After the wrapper is confirmed serving, pull a fresh media-user-token from
    its account API into config (music videos need a token from the SUBSCRIBED
    account). Best-effort, never fatal. Kept out of apple_auth's import graph via
    a lazy import — both modules share the same live `config` dict object."""
    try:
        from ripster.routes import apple_auth as _aa
        loop = asyncio.get_event_loop()
        mut = await loop.run_in_executor(None, _aa.sync_mut_from_wrapper)
        if mut and _broadcast:
            await _broadcast({"type": "wrapper_log",
                              "text": f"🎬 media-user-token обновлён из аккаунта "
                                      f"({len(mut)} симв.) — видео и aac-lc готовы."})
            await _broadcast({"type": "apple_authed", "mut_length": len(mut)})
    except Exception:
        pass


def _dist_dir(mode: str) -> Path:
    folder = "non-docker" if mode == "non-docker" else "docker"
    return _base_dir / "dist" / folder


def _rootfs_data(mode: str) -> Path:
    """Путь к rootfs/data для хранения сессии Apple Music.

    У КАЖДОГО Apple ID своя папка. Причина не в аккуратности, а в отказах: образ
    несёт вшитую device-identity, общую для всех, и Apple видит её как одно
    перегруженное устройство — «You have reached your device limit» приходит
    мгновенно и одинаково разным аккаунтам (22.07.2026). Своя папка = своя
    identity, и вход проходит.

    Но identity — тоже устройство на аккаунте, и слоты у Apple ID кончаются
    (01.08.2026: у первого аккаунта не помогла уже никакая, даже пустая). Поэтому
    папка ПРИВЯЗАНА К ЛОГИНУ и переиспользуется: один аккаунт — одно устройство,
    а не новое на каждый запуск. См. [[project_apple_device_limit_exhausted_2026-08-01]].
    """
    base = _base_dir if mode == "docker-remote" else _dist_dir(mode)
    aid = ""
    try:
        aid = str((_cfg or {}).get("wrapper-apple-id") or "").strip().lower()
    except Exception:
        pass
    if aid:
        safe = re.sub(r"[^a-z0-9_.-]", "_", aid.split("@")[0])[:40]
        if safe:
            return base / "rootfs_id" / safe / "data"
    return base / "rootfs" / "data"


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
                capture_output=True, text=True, timeout=5, creationflags=_CNW,
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
                    capture_output=True, text=True, timeout=5, creationflags=_CNW,
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
    addr = str(_cfg.get("decrypt-port", "127.0.0.1:10020")).strip()
    host, _, port = addr.rpartition(":")
    host = (host or "127.0.0.1").strip() or "127.0.0.1"   # та же логика, что в _publish
    try:
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


async def _handle_invalid_store(text: str) -> None:
    """Диалог Apple «Invalid store» → внятная ошибка владельцу + сигнал в бот.

    Это НЕ «ключа нет по незнанию»: Apple прямо говорит, что аккаунт закреплён
    не за той страной, где оформлена подписка. Лечится только на стороне Apple
    (сменить страну учётной записи либо взять аккаунт нужной страны), поэтому
    говорим это вслух и не ждём, пока человек сам догадается.
    """
    global _last_store_alert_ts
    from ripster import i18n as _i18n
    from ripster import wrapper_storefront as _wsf
    info = _wsf.parse_invalid_store(text)
    if not info:
        return
    signed = info.get("signed_in") or "?"
    allowed = info.get("allowed") or "?"
    signed_cc = info.get("signed_in_cc") or "?"
    allowed_cc = info.get("allowed_cc") or "?"
    if _broadcast:
        try:
            await _broadcast(_i18n.log_event(
                "console.wrapper_store_mismatch", level="error",
                signed_in=signed, allowed=allowed,
                signed_cc=signed_cc, allowed_cc=allowed_cc))
        except Exception:
            pass
    # В бот — не на каждый трек, а раз в час на один и тот же диагноз.
    now = time.time()
    if now - _last_store_alert_ts < 3600:
        return
    _last_store_alert_ts = now
    try:
        from ripster import accounts_watch as _aw
        bot_text = (
            "🔴 Apple: аккаунт wrapper'а не в своей стране.\n"
            f"Wrapper вошёл в витрину «{signed}» ({signed_cc}), а покупать этому "
            f"аккаунту разрешено только в «{allowed}» ({allowed_cc}).\n"
            "Ключ на такие релизы Apple не выдаст, и перелогин/смена ссылки не "
            "помогут — это настройка страны самой учётной записи Apple. "
            "Нужно либо сменить страну аккаунта на странице Apple ID, либо "
            "использовать аккаунт той страны, где оформлена подписка.")
        await _aw._default_notify(bot_text, _base_dir)
    except Exception:
        pass


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
                if "invalid store" in low or "not valid for use in the" in low:
                    # Apple прямо говорит про чужую страну аккаунта — это не
                    # «релиза нет в витрине», и лестница слотов тут не поможет.
                    await _handle_invalid_store(text)
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
                # Лимит устройств Apple ID — отдельная беда, и совет «перелогинь»
                # тут ВРЕДЕН: каждый новый вход занимает ещё один слот, а Apple
                # освобождает их только со временем (порядка 90 дней на
                # устройство). 22.07.2026 это лечилось чистой identity — образ
                # раздавал всем одну и ту же. 01.08.2026 не вылечилось уже ничем:
                # даже полностью пустая identity получила тот же отказ, то есть
                # слоты кончились у самого аккаунта. Останавливаемся и говорим
                # правду, вместо того чтобы жечь слоты дальше.
                if ("device limit" in low
                        or "concurrent playing devices" in low
                        or "lease code 3062" in low):
                    if _broadcast:
                        await _broadcast({"type": "wrapper_login_failed",
                            "msg_key": "w.device_limit", "params": {},
                            "msg": "Apple: у аккаунта исчерпан лимит устройств "
                                   "(«device limit»). Перелогин НЕ поможет — каждый "
                                   "вход занимает ещё один слот, а Apple освобождает "
                                   "их только со временем. Нужен другой Apple ID "
                                   "либо переждать. Пока качай lossless через Qobuz "
                                   "или Deezer.",
                            "reason": "device_limit"})
                    ok3, dp3 = check_docker_installed()
                    if ok3:
                        subprocess.run([dp3, "rm", "-f", WRAPPER_CONTAINER_NAME],
                                       capture_output=True, timeout=10, creationflags=_CNW)
                    return
                # Hard-stop on failure OR a retry loop — never spin forever (it
                # risks an Apple lock and spams 2FA).
                if ("[!] login failed" in low
                        or "check the account information" in low
                        or "response type 6" in low
                        or _login_tries >= 3):
                    if _broadcast:
                        await _broadcast({"type": "wrapper_login_failed",
                            "msg_key": "w.login_loop", "params": {},
                            "msg": "Логин не удался / зациклился — wrapper остановлен. "
                                   "Скорее всего у аккаунта нет подписки Apple Music, либо неверный пароль."})
                    ok2, dp2 = check_docker_installed()
                    if ok2:
                        subprocess.run([dp2, "rm", "-f", WRAPPER_CONTAINER_NAME],
                                       capture_output=True, timeout=10, creationflags=_CNW)
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


def _slot0_device_info(apple_id: str) -> str:
    """Назначенный владельцем `-I` отпечаток слота 0, либо '' (тогда слот
    стартует на заводском дефолте образа — ровно как до этой правки).

    Слот 0 — единственная живая учётка, и новый device-info для Apple = новое
    устройство (риск device_limit), поэтому молча подставлять отпечаток нельзя.
    `get_assigned` ничего не вычисляет: вернёт значение, только если его положил
    туда владелец через «Сменить отпечаток устройства» (routes/setup.py)."""
    if not apple_id:
        return ""
    try:
        from ripster import wrapper_device_info as _di
        return _di.get_assigned(apple_id) or ""
    except Exception as e:                              # noqa: BLE001
        # Без отпечатка слот поднимется как раньше; ронять запуск из-за этого
        # нельзя, но и молча глотать — нельзя (урок `_device_info` в пуле).
        print(f"[wrapper] slot0 device-info не прочитан: {e}", flush=True)
        return ""


async def _start_wrapper_docker(force_login: bool = False) -> dict:
    """Start wrapper via Docker (remote или local image). Returns {ok, msg}."""
    global _wrapper_log_task

    mode = _wrapper_mode()
    ok, docker_path = check_docker_installed()
    if not ok:
        return {"ok": False, "msg": docker_path}

    # A forced re-login must tear the running wrapper down and log in fresh —
    # otherwise a stale/expired Apple session keeps serving and every decrypt
    # fails with "Invalid CKC". Only short-circuit on a plain (non-forced) start.
    if not force_login and await check_wrapper_running():
        return {"ok": True, "msg": "Wrapper already running"}

    dec_port = _cfg.get("decrypt-port", "127.0.0.1:10020")
    m3u_port = _cfg.get("m3u8-port",    "127.0.0.1:20020")
    acct_p   = _wrapper_account_port()
    rootfs   = str(_rootfs_data(mode))
    Path(rootfs).mkdir(parents=True, exist_ok=True)

    image = WRAPPER_LOCAL_IMAGE if mode == "docker-local" else "ghcr.io/itouakirai/wrapper:x86"
    subprocess.run([docker_path, "rm", "-f", WRAPPER_CONTAINER_NAME],
                   capture_output=True, timeout=10, creationflags=_CNW)

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
                    "msg_key": "w.no_apple_id", "params": {},
                    "msg": "Apple ID и пароль не заданы в Settings → Apple Music → Wrapper"}
        return await _docker_login(docker_path, image, dec_port, m3u_port,
                                   rootfs, apple_id, apple_pwd, force_login)

    # ── NORMAL path: saved session → detached, auto-restart, no 2FA. ──
    _di0 = _slot0_device_info(apple_id)
    wrapper_args = "-H 0.0.0.0" + (f" -I {_di0}" if _di0 else "")
    cmd = [
        docker_path, "run", "-d",
        "--name", WRAPPER_CONTAINER_NAME,
        # БЕЗ `--restart`: Docker Desktop теряет HostIp при перезапуске
        # контейнера, и порты, созданные на 127.0.0.1, после перезагрузки
        # машины поднимаются на 0.0.0.0. Порт 30020 отдаёт токены Apple
        # без авторизации, поэтому цена такой «услужливости» — открытая
        # сессия для всей локальной сети. Проверено 21.09.2026 на
        # Docker Desktop 4.83 / engine 29.6.2. Приложение само поднимает
        # враппер, когда он не отвечает, — автоподъём докером не нужен.
        "-v", f"{rootfs}:/app/rootfs/data",
        "-p", _publish(dec_port, 10020),
        "-p", _publish(m3u_port, 20020),
        # Publish the account-info API too so the app can harvest a fresh
        # media-user-token from the subscribed account (music videos need it).
        "-p", _publish(acct_p, 30020),
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
                await _harvest_wrapper_token()
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
                        "msg_key": "w.login_rejected", "params": {},
                        "msg": "Логин отклонён Apple (неверный пароль) — остановлено, чтобы не залочить аккаунт"})
                try:
                    proc.terminate()
                except Exception:
                    pass
                ok2, dp2 = check_docker_installed()
                if ok2:
                    subprocess.run([dp2, "rm", "-f", WRAPPER_CONTAINER_NAME],
                                   capture_output=True, timeout=10, creationflags=_CNW)
                return
    except asyncio.CancelledError:
        pass
    except Exception:
        pass


async def _docker_login(docker_path: str, image: str, dec_port: str, m3u_port: str,
                        rootfs: str, apple_id: str, apple_pwd: str,
                        force: bool) -> dict:
    """Login start for THIS wrapper build: it reads the 2FA code from a FILE
    (<rootfs>/data/com.apple.android.music/files/2fa.txt — see submit_2fa), not
    stdin, so we run it detached. Crucially NO --restart: a wrong password /
    expired code must not loop into an Apple lock. The log monitor is started
    immediately so the 2FA prompt AND a failed login are caught during login."""
    global _wrapper_log_task
    _di0 = _slot0_device_info(apple_id)
    args_str = (f"-H 0.0.0.0 -L {apple_id}:{apple_pwd}"
                + (f" -I {_di0}" if _di0 else "")
                + (" -F" if force else ""))
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
        "-p", _publish(dec_port, 10020),
        "-p", _publish(m3u_port, 20020),
        "-p", _publish(_wrapper_account_port(), 30020),
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
            await _harvest_wrapper_token()
            return {"ok": True, "msg_key": "w.logged_in", "params": {},
                    "msg": "Враппер залогинен и запущен"}
    return {"ok": True, "msg_key": "w.login_pending_2fa", "params": {},
            "msg": "Логин идёт — введи 2FA-код в открывшемся поле"}


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
        return {"ok": False, "msg_key": "w.bin_missing", "params": {"path": str(bin_path)},
                "msg": f"Бинарник не найден: {bin_path}"}

    # A forced re-login must tear the running wrapper down and log in fresh —
    # otherwise a stale/expired Apple session keeps serving and every decrypt
    # fails with "Invalid CKC". Only short-circuit on a plain (non-forced) start.
    if not force_login and await check_wrapper_running():
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
            return {"ok": False, "msg_key": "w.no_apple_id", "params": {},
                    "msg": "Apple ID и пароль не заданы в Settings → Apple Music → Wrapper"}
        wrapper_args += ["-L", f"{apple_id}:{apple_pwd}", "-F"]
    elif not has_session and apple_id and apple_pwd:
        wrapper_args += ["-L", f"{apple_id}:{apple_pwd}"]
    _di0 = _slot0_device_info(apple_id)
    if _di0:
        wrapper_args += ["-I", _di0]

    _rootfs_data("non-docker").mkdir(parents=True, exist_ok=True)
    dist_dir = _dist_dir("non-docker")

    if _is_windows:
        if not check_wsl_available():
            return {"ok": False, "msg_key": "w.wsl_missing", "params": {},
                    "msg": "WSL не найден. Установи WSL 2 для запуска non-docker враппера на Windows."}
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
                return {"ok": True, "msg_key": "w.nondocker_started", "params": {},
                        "msg": "Wrapper (non-docker) запущен"}
            if _broadcast:
                await _broadcast({"type": "wrapper_log",
                                   "text": f"Waiting for wrapper… ({i+1}/15)"})
        return {"ok": False, "msg_key": "w.port_silent", "params": {},
                "msg": "Wrapper не ответил на порту после 15s"}
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
    return {"ok": True, "msg_key": "s.wrapper_stopped", "params": {},
            "msg": "Wrapper остановлен"}


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
        return {"ok": False, "msg_key": "w.dockerfile_missing", "params": {"dir": str(dist)},
                "msg": f"Dockerfile не найден в {dist}"}
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
            return {"ok": True, "msg_key": "w.image_built",
                    "params": {"image": WRAPPER_LOCAL_IMAGE},
                    "msg": f"Image {WRAPPER_LOCAL_IMAGE} собран"}
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
