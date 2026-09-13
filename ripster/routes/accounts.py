"""Ростер учёток по всем сервисам — для настроек ПК и мобилки.

Install: accounts.install(app, ctx)

  GET /api/accounts/roster       — карточки всех сервисов: флаг · страна ·
                                    план · срок · статус (active/bench/retired)

Владелец 13.09.2026: реквизиты учётки (страна с флагом, дата окончания, тариф,
статус) должны видеть и он, и любой пользователь ПК/мобилки. Данные считает
`account_roster.roster_cards` из кэша замеров — здесь только выдача наружу.
"""
from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()

_config: dict = {}


def install(app, ctx) -> None:
    global _config
    _config = ctx.config
    app.include_router(router)


@router.get("/api/accounts/roster")
async def roster():
    """Карточки учёток по сервисам. Меру берём из кэша (без сетевого пробега
    на каждый показ); обновляет её сторож здоровья по расписанию."""
    import asyncio

    from ripster import account_roster as ar
    try:
        cards = await asyncio.to_thread(ar.roster_cards, _config)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "services": {}}
    errors = cards.pop("_errors", [])
    return {"ok": True, "services": cards, "errors": errors}
