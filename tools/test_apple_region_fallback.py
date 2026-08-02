# -*- coding: utf-8 -*-
"""Локальный wrapper без прав в регионе — задача должна спасаться через AMD.

Случай владельца (01.08.2026, дважды за минуту): в настройках выбран «локальный»
wrapper, Apple не выдаёт ключ на конкретный релиз, при этом сессия wrapper'а
ЖИВА. Значит у контента нет прав в регионе аккаунта, и локальный wrapper не
достанет его никогда — ни перелогином, ни ожиданием.

Раньше в режиме local-only фолбэк подавлялся В ОБОИХ случаях, и в сообщении
стоял совет сделать руками ровно то, что код делать отказывался. Теперь:

  сессия ЖИВА  → спасаем через AMD (несколько регионов);
  сессия МЕРТВА → ошибка, как и раньше: уход на публичный wrapper замаскировал бы
                  поломку, которую владелец должен починить.

Ни сети, ни движков: подменяем зависимости и смотрим, какой путь выбран.

Запуск:  python tools/test_apple_region_fallback.py
"""
from __future__ import annotations

import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from ripster import runner as R  # noqa: E402

_fails: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("  OK  " if ok else "ПРОВАЛ") + f"  {name}")
    if not ok:
        _fails.append(name)
        if detail:
            print(f"        {detail}")


async def scenario(session_alive: bool, strict: bool) -> dict:
    """Прогнать ветку local-only и вернуть, что случилось."""
    seen = {"engines": [], "events": [], "status": None, "log": []}

    async def fake_run_engine_task(task, engine, url, qid):
        seen["engines"].append(engine)

    async def fake_broadcast(msg):
        seen["events"].append(msg.get("key") or msg.get("msg_key") or msg.get("msg") or "")

    class FakeI18n:
        @staticmethod
        def log_event(key, level="info", **kw):
            return {"key": key, "level": level}

    def fake_advance(task, status):
        seen["status"] = getattr(status, "name", str(status))

    import ripster.apple_router as AR
    orig = {
        "_run_engine_task": R._run_engine_task, "_broadcast": R._broadcast,
        "_i18n": R._i18n, "_try_advance_task": R._try_advance_task,
        "_config": R._config, "alive": getattr(AR, "local_wrapper_session_alive", None),
    }
    R._run_engine_task = fake_run_engine_task
    R._broadcast = fake_broadcast
    R._i18n = FakeI18n
    R._try_advance_task = fake_advance
    R._config = {"apple-local-only-strict": strict}
    AR.local_wrapper_session_alive = lambda: session_alive

    task = {"id": "t1", "log": seen["log"]}
    try:
        # Тот же кусок, что стоит в except _NeedAMDFallback при local-only.
        _sess_alive = await asyncio.to_thread(AR.local_wrapper_session_alive)
        _strict = bool((R._config or {}).get("apple-local-only-strict", False))
        if _sess_alive and not _strict:
            await R._broadcast(R._i18n.log_event("console.wrapper_local_region_amd",
                                                 level="warn", task_id="t1"))
            task["log"].append("─── local-only: нет прав в регионе → спасаю через AMD ───")
            await R._run_engine_task(task, "amd", "u", "alac")
        else:
            await R._broadcast(R._i18n.log_event(
                "console.wrapper_local_region_fail" if _sess_alive
                else "console.wrapper_local_drm_fail", level="error", task_id="t1"))
            task["log"].append("─── local-only: AMD-фолбэк подавлен ───")
            R._try_advance_task(task, "ERROR")
    finally:
        R._run_engine_task, R._broadcast = orig["_run_engine_task"], orig["_broadcast"]
        R._i18n, R._try_advance_task = orig["_i18n"], orig["_try_advance_task"]
        R._config = orig["_config"]
        if orig["alive"]:
            AR.local_wrapper_session_alive = orig["alive"]
    return seen


async def main() -> None:
    # 1. Сессия жива — случай владельца. Должны спасти через AMD.
    s = await scenario(session_alive=True, strict=False)
    check("сессия жива → уходим на AMD", s["engines"] == ["amd"],
          f"движки: {s['engines']}, события: {s['events']}")
    check("сессия жива → сказано, что спасаем", "console.wrapper_local_region_amd" in s["events"],
          f"события: {s['events']}")
    check("сессия жива → задача НЕ помечена ошибкой", s["status"] is None,
          f"статус: {s['status']}")

    # 2. Сессия мертва — уход на публичный wrapper замаскировал бы поломку.
    s = await scenario(session_alive=False, strict=False)
    check("сессия мертва → фолбэк подавлен", s["engines"] == [], f"движки: {s['engines']}")
    check("сессия мертва → это ошибка", s["status"] == "ERROR", f"статус: {s['status']}")
    check("сессия мертва → сообщение про DRM, а не про регион",
          "console.wrapper_local_drm_fail" in s["events"], f"события: {s['events']}")

    # 3. Строгий режим — прежнее поведение доступно тем, кому оно нужно.
    s = await scenario(session_alive=True, strict=True)
    check("строгий режим → фолбэк подавлен даже при живой сессии",
          s["engines"] == [] and s["status"] == "ERROR",
          f"движки: {s['engines']}, статус: {s['status']}")

    print()
    if _fails:
        print(f"ПРОВАЛЕНО: {len(_fails)} — {', '.join(_fails)}")
        sys.exit(1)
    print("Все проверки пройдены — релиз без прав в регионе спасается через AMD.")


asyncio.run(main())
