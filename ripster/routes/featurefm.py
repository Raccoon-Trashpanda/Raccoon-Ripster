"""feature.fm: прямой доступ к ISRC/UPC и грядущим релизам кабинета.

Install: featurefm.install(app, ctx)

Резолвер ISRC/UPC уже вплетён как фолбэк в `routes/isrc.py` (когда наш разбор
ссылки промолчал). Здесь — прямые маршруты для инструментов и UI:

  GET  /api/featurefm/status              — есть ли учётка, жива ли сессия
  GET  /api/featurefm/ids?url=<URL>       — ISRC/UPC/лейбл/кросс-ссылки по релизу
  GET  /api/featurefm/upcoming            — грядущие релизы кабинета (Control Room)

Отдельный неймспейс, а не подмешивание в общий радар грядущего: тот собирает
мировые анонсы из прессы, а Control Room feature.fm — это СОБСТВЕННЫЙ конвейер
релизов владельца (пресейвы до даты выхода), другое по смыслу.
"""
from __future__ import annotations

from fastapi import APIRouter, Query

from ripster import featurefm as _ffm

router = APIRouter()

_config: dict = {}


def install(app, ctx) -> None:
    global _config
    _config = ctx.config
    app.include_router(router)


@router.get("/api/featurefm/status")
async def ff_status():
    """Учётка настроена? Сессия жива? Честные три состояния, без угадывания."""
    if not _ffm.configured(_config):
        return {"configured": False, "alive": None,
                "note": "нет учётки feature.fm (tokens/featurefm_credentials.json)"}
    import asyncio
    try:
        me = await asyncio.to_thread(_ffm.whoami, cfg=_config)
        return {"configured": True, "alive": True,
                "user": me.get("email") or me.get("username") or ""}
    except _ffm.SessionExpired as e:
        return {"configured": True, "alive": False, "note": str(e)}
    except Exception as e:                                         # noqa: BLE001
        # Сеть/сервис — «не смог спросить», а не «мертво».
        return {"configured": True, "alive": None, "note": f"{type(e).__name__}: {e}"}


@router.get("/api/featurefm/ids")
async def ff_ids(url: str = Query(""), fresh: bool = Query(False)):
    """ISRC/UPC/лейбл/кросс-сервис ссылки по URL релиза."""
    url = (url or "").strip()
    if not url:
        return {"ok": False, "error_key": "err.url_required", "error": "нужен URL релиза"}
    try:
        d = await _ffm.a_ids_for(url, cfg=_config, fresh=fresh)
        return {"ok": True, **d}
    except _ffm.NotConfigured as e:
        return {"ok": False, "error_key": "ff.not_configured", "error": str(e)}
    except _ffm.SessionExpired as e:
        return {"ok": False, "error_key": "ff.session", "error": str(e)}


@router.get("/api/featurefm/upcoming")
async def ff_upcoming(artist_id: str = Query(""), page: int = Query(1),
                      size: int = Query(20)):
    """Грядущие релизы кабинета (Control Room): то, чего ещё нет в магазинах."""
    try:
        d = await _ffm.a_upcoming(artist_id=(artist_id or None),
                                  page=page, size=size, cfg=_config)
        return {"ok": True, **d}
    except _ffm.NotConfigured as e:
        return {"ok": False, "error_key": "ff.not_configured", "error": str(e)}
    except _ffm.SessionExpired as e:
        return {"ok": False, "error_key": "ff.session", "error": str(e)}
