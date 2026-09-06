"""Жанровые станции — маршруты.

  GET /api/stations              список плиток
  GET /api/station?id=…&limit=…  собранный эфир

Сама сборка — в `ripster/stations.py`; здесь только вход. Правила и почему они
именно такие описаны там же и в скилле `ripster-cross-service-availability`
(соседний по духу принцип: спрашиваем все источники разом, «не знаю» не равно
«чужое»).
"""
from __future__ import annotations

from fastapi import APIRouter, Query

router = APIRouter()

_config: dict = {}


def install(app, ctx) -> None:
    global _config
    _config = ctx.config
    app.include_router(router)


@router.get("/api/stations")
async def api_stations():
    from ripster import stations as _st
    return {"ok": True, "stations": _st.catalog()}


@router.get("/api/station")
async def api_station(id: str = Query(""), limit: int = Query(30), seed: int = Query(0)):
    """Эфир станции. Пустой ответ приходит С ПРИЧИНОЙ, а не молча."""
    from ripster import stations as _st
    if not id:
        return {"ok": False, "reason": "нужен id станции", "tracks": []}
    return await _st.build(id, limit=max(1, min(100, limit)), seed=seed or None)
