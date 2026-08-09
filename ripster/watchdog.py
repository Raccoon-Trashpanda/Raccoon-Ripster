"""Сторож: поднимает упавший сервис сам, но знает, когда поднимать НЕЛЬЗЯ.

ЗАЧЕМ. 01.08.2026 за вечер падали враппер Apple, туннель и кипер токенов, и
каждый раз это замечал владелец, а не программа. Сторож закрывает разрыв между
«сломалось» и «кто-то увидел».

ПОЧЕМУ ЭТО НЕ ПРОСТО «ПЕРЕЗАПУСКАТЬ В ЦИКЛЕ». В тот же вечер именно цикл
перезапусков и стоил дороже всего: контейнер враппера перезаходил в Apple снова
и снова, каждый заход занимал слот устройства, и слоты у Apple ID кончились
насовсем. Поэтому сторож построен вокруг ЗАПРЕТОВ, а не вокруг попыток:

* **Диагноз важнее факта падения.** Есть причины, которые перезапуском не
  лечатся и от него только хуже — «device limit» у Apple первая из них. Увидел
  такую причину — не трогает и говорит владельцу.
* **Попыток мало и они дорожают.** Первая через 10 секунд, дальше пауза растёт.
  Больше `_MAX_TRIES` подряд — сдаётся до ручного вмешательства.
* **Успех сбрасывает счётчик, а не «прощает» бесконечно.** Сервис, который
  падает после каждого подъёма, чинится человеком, а не сторожем.
* **Сначала убедиться, что он вообще был жив.** Не поднимаем то, что владелец
  сам выключил: у сервиса должен быть предыдущий успешный опрос.
"""
from __future__ import annotations

import asyncio
import time

_FIRST_DELAY = 10.0        # владелец просил именно столько
_MAX_TRIES = 3             # дальше — только руками
_BACKOFF = (10.0, 60.0, 300.0)
_COOLDOWN = 1800.0         # столько молчим после сдачи

_state: dict = {}          # имя -> {"alive_seen", "tries", "next_try", "given_up"}
_broadcast = None
_config: dict = {}


def install(config: dict, broadcast=None) -> None:
    global _config, _broadcast
    _config = config or {}
    _broadcast = broadcast


def _st(name: str) -> dict:
    return _state.setdefault(name, {"alive_seen": False, "tries": 0,
                                    "next_try": 0.0, "given_up": 0.0})


async def _say(msg: str, level: str = "warn") -> None:
    print(f"[watchdog] {msg}", flush=True)
    if _broadcast:
        try:
            await _broadcast({"type": "log", "msg": f"[watchdog] {msg}", "level": level})
        except Exception:
            pass


# ── Apple-враппер ────────────────────────────────────────────────────────────

async def _apple_alive() -> bool:
    try:
        from ripster.apple_router import local_wrapper_session_alive
        return bool(await asyncio.to_thread(local_wrapper_session_alive))
    except Exception:
        return False


def _apple_blocked_reason() -> str:
    """Причина, по которой поднимать БЕССМЫСЛЕННО. Пустая строка — можно.

    Читаем журнал самого контейнера: «device limit» / lease 3062 означает, что
    у аккаунта кончились слоты устройств, и каждый новый вход занимает ещё один.
    """
    try:
        import subprocess
        from ripster import amd as _amd
        ok, dp = _amd.check_docker_installed()
        if not ok:
            return "docker недоступен"
        r = subprocess.run([dp, "logs", "--tail", "40", _amd.WRAPPER_CONTAINER_NAME],
                           capture_output=True, timeout=15, encoding="utf-8",
                           errors="replace")
        low = ((r.stdout or "") + (r.stderr or "")).lower()
        if "device limit" in low or "concurrent playing devices" in low or "lease code 3062" in low:
            return "у Apple ID исчерпан лимит устройств — перезапуск только съест ещё слот"
        if "login failed" in low or "check the account information" in low:
            return "Apple отверг вход — нужен верный пароль, перезапуск не поможет"
    except Exception:
        pass
    return ""


async def _apple_revive() -> bool:
    try:
        from ripster import amd as _amd
        await _amd.start_wrapper()
        return True
    except Exception as e:
        await _say(f"Apple: подъём не удался — {type(e).__name__}: {e}", "error")
        return False


# ── Кипер токенов Spotify ────────────────────────────────────────────────────

async def _sp_alive() -> bool:
    try:
        from ripster.routes.spotify import _sp_minted_bearer
        return bool(_sp_minted_bearer())
    except Exception:
        return False


async def _sp_revive() -> bool:
    try:
        from ripster import spotify_token_keeper as _k
        fn = getattr(_k, "mint_now", None) or getattr(_k, "refresh", None)
        if fn:
            await asyncio.to_thread(fn)
            return True
    except Exception as e:
        await _say(f"Spotify: обновить токен не вышло — {type(e).__name__}", "error")
    return False


SERVICES = {
    "Apple-враппер": (_apple_alive, _apple_revive, _apple_blocked_reason),
    "Spotify-токен": (_sp_alive, _sp_revive, lambda: ""),
}


async def _tick(name: str, alive_fn, revive_fn, blocked_fn) -> None:
    st = _st(name)
    now = time.time()
    if await alive_fn():
        if st["tries"]:
            await _say(f"{name}: снова жив", "info")
        st.update({"alive_seen": True, "tries": 0, "next_try": 0.0, "given_up": 0.0})
        return

    if not st["alive_seen"]:
        return                      # никогда не был жив — возможно, выключен намеренно
    if st["given_up"] and now - st["given_up"] < _COOLDOWN:
        return
    if now < st["next_try"]:
        return

    reason = ""
    try:
        reason = blocked_fn() or ""
    except Exception:
        pass
    if reason:
        await _say(f"{name} лежит, но поднимать нельзя: {reason}", "error")
        st["given_up"] = now
        st["tries"] = _MAX_TRIES
        return

    if st["tries"] >= _MAX_TRIES:
        await _say(f"{name}: {_MAX_TRIES} попытки подряд не помогли — жду вмешательства", "error")
        st["given_up"] = now
        return

    st["tries"] += 1
    await _say(f"{name} упал — поднимаю (попытка {st['tries']} из {_MAX_TRIES})")
    ok = await revive_fn()
    delay = _BACKOFF[min(st["tries"] - 1, len(_BACKOFF) - 1)]
    st["next_try"] = time.time() + delay
    if ok:
        await asyncio.sleep(3)
        if await alive_fn():
            await _say(f"{name}: поднят", "info")
            st.update({"tries": 0, "next_try": 0.0})


async def run() -> None:
    """Фоновый цикл. Первая проверка — через 10 секунд после старта."""
    if str(_config.get("watchdog-enabled", True)).lower() in ("0", "false", "no"):
        print("[watchdog] disabled in settings", flush=True)
        return
    await asyncio.sleep(_FIRST_DELAY)
    while True:
        for name, (a, r, b) in SERVICES.items():
            try:
                await _tick(name, a, r, b)
            except Exception as e:
                print(f"[watchdog] {name}: {type(e).__name__}: {e}", flush=True)
        await asyncio.sleep(_FIRST_DELAY)
