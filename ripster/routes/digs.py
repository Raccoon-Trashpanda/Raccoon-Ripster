"""«Раскопки» (Digs) — маршруты.

Пока только ПЕРВЫЙ шаг замысла: профиль вкуса по собственной статистике.
Ничего не рекомендует и никуда за чартами не ходит — так задумано. Профиль
сначала показывается владельцу и оценивается на осмысленность, и только потом
на него навешиваются подбор и источники. Иначе, когда подбор начнёт промахиваться,
будет не понять, врёт профиль или веса.

  GET /api/digs/profile?limit=40&genres=1 — артисты-опоры, жанры, шоу/лейблы

Install: digs.install(app, ctx)
"""
from __future__ import annotations

import time

from fastapi import APIRouter

from ripster import digs as _digs

router = APIRouter()

_cfg: dict = {}
# Профиль считается по всей истории и меняется медленно — пересчитывать его на
# каждое открытие панели незачем. Жанры к тому же ходят в сеть.
_cache: dict = {"data": None, "ts": 0.0, "key": ""}
_TTL = 900


def install(app, ctx) -> None:
    global _cfg
    _cfg = ctx.config
    app.include_router(router)


@router.get("/api/digs/profile")
async def digs_profile(limit: int = 40, genres: int = 1, force: int = 0):
    key = f"{limit}:{genres}"
    now = time.time()
    if (not force and _cache["data"] is not None and _cache["key"] == key
            and now - _cache["ts"] < _TTL):
        return {**_cache["data"], "cached": True}
    data = await _digs.build_profile(limit=limit, with_genres=bool(genres))
    _cache.update({"data": data, "ts": now, "key": key})
    return {**data, "cached": False}
