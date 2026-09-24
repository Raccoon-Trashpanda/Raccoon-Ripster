"""Ростер учёток по всем сервисам — для настроек ПК и мобилки.

Install: accounts.install(app, ctx)

  GET /api/accounts/roster       — карточки всех сервисов: флаг · страна ·
                                    план · срок · статус (active/bench/retired)

Владелец 13.09.2026: реквизиты учётки (страна с флагом, дата окончания, тариф,
статус) должны видеть и он, и любой пользователь ПК/мобилки. Данные считает
`account_roster.roster_cards` из кэша замеров — здесь только выдача наружу.

24.09.2026: ручка отвечала 22.9s — в туннеле Telegram-панели это 502 через
10s. Виноват не показ, а сеть по ходу показа; теперь показ строго из кэша,
а сетевой обход (`account_roster.refresh_measurements`) стартует фоном сразу
после ответа: не чаще одного параллельного обхода и не чаще раза в
`_REFRESH_MIN_INTERVAL`. Пользователь видит последний известный статус и его
возраст («проверено N мин назад»), а не вращающийся песочный час.
"""
from __future__ import annotations

import time

from fastapi import APIRouter

router = APIRouter()

_config: dict = {}

#: Как часто допустимо поднимать сетевой обход по показу панели (сек).
_REFRESH_MIN_INTERVAL = 120.0
_refresh_task = None
_refresh_started = 0.0


def install(app, ctx) -> None:
    global _config
    _config = ctx.config
    app.include_router(router)


def _kick_refresh() -> None:
    """Фоновое обновление кэша измерений. Огонь-и-забудь: ответ панели уже
    посчитан из кэша и не ждёт сеть."""
    global _refresh_task, _refresh_started
    import asyncio

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # не в событийном цикле — фоном не займёмся
        return
    if _refresh_task is not None and not _refresh_task.done():
        return                       # один обход за раз: панель листают быстро
    if time.time() - _refresh_started < _REFRESH_MIN_INTERVAL:
        return                       # троттлинг: не гонять сеть на каждый клик
    _refresh_started = time.time()

    async def _run():
        try:
            from ripster import account_roster as ar
            await ar.refresh_measurements(_config)
        except Exception as e:  # noqa: BLE001
            print(f"[roster] фоновый обход не прошёл: {type(e).__name__}", flush=True)

    _refresh_task = loop.create_task(_run())


@router.get("/api/accounts/roster")
async def roster():
    """Карточки учёток по сервисам. Меру берём из кэша (без сетевого пробега
    на каждый показ); обновляет её фоновый обход, старующий здесь же."""
    import asyncio

    from ripster import account_roster as ar
    try:
        cards = await asyncio.to_thread(ar.roster_cards, _config)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "services": {}}
    _kick_refresh()
    errors = cards.pop("_errors", [])
    return {"ok": True, "services": cards, "errors": errors}
