"""Statistics routes.

GET /api/stats?period=day|week|month|year|all
GET /api/stats/live   — active streams + connected clients count
"""
from __future__ import annotations

from fastapi import APIRouter, Query
from ripster import stats_collector as _sc

router = APIRouter()

_ws_clients_ref = None   # set by install() to the ws_clients set from app.py


def install(app, config: dict, ws_clients_ref=None) -> None:
    global _ws_clients_ref
    _ws_clients_ref = ws_clients_ref
    app.include_router(router)


@router.get("/api/stats")
async def get_stats(period: str = Query("week")):
    if period not in ("day", "week", "month", "year", "all"):
        period = "week"
    return _sc.get_stats(period)


@router.get("/api/stats/live")
async def get_live():
    connected = len(_ws_clients_ref) if _ws_clients_ref is not None else 0
    return {"connected_clients": connected}
