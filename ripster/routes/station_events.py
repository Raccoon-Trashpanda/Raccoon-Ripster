"""Приём событий прослушивания станций от плеера.

Одна ручка `POST /api/stations/event`. Кто слушает — решает СЕРВЕР по гостевой
сессии, а не клиент: иначе гостевой браузер мог бы назваться владельцем и
переучить его станции на свой вкус (флаг `is_guest` в station_events).

Ограничитель частоты здесь не «защита от атаки», а защита от зациклившегося
клиента: плеер шлёт событие на трек, а не на кадр, и сотня событий в секунду
означает ошибку в клиенте, а не живого человека.
"""
from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException, Request

from ripster import station_events as _se
from ripster.i18n_msg import imsg

router = APIRouter()
_cfg: dict = {}

_RATE: dict[str, list] = {}
_RATE_MAX = 20          # событий в секунду с одного клиента
_RATE_WINDOW = 1.0


def install(app, ctx) -> None:
    global _cfg
    _cfg = ctx.config if ctx is not None else {}
    base = getattr(ctx, "base_dir", None) or "."
    _se.init(base)
    app.include_router(router)


def _guest_session_id(request: Request) -> str:
    try:
        from ripster.guest_manager import get_manager
        gm = get_manager()
        sid = gm.get_session_id_from_request(request)
        if sid and gm.get_session(sid):
            return sid
    except Exception:                                          # noqa: BLE001
        pass
    return ""


def _too_fast(peer: str) -> bool:
    now = time.time()
    hits = [t for t in _RATE.get(peer, []) if now - t < _RATE_WINDOW]
    hits.append(now)
    _RATE[peer] = hits
    if len(_RATE) > 500:                                       # чистим редко и дёшево
        for k in [k for k, v in _RATE.items() if not v or now - v[-1] > 60]:
            _RATE.pop(k, None)
    return len(hits) > _RATE_MAX


@router.post("/api/stations/event")
async def station_event(body: dict, request: Request):
    """Записать одно событие. Ответ всегда быстрый: плеер не должен ждать базу."""
    peer = (request.client.host if request.client else "?") or "?"
    if _too_fast(peer):
        raise HTTPException(429, imsg("err.too_many_events",
                                      "слишком много событий станции подряд"))
    ev = dict(body or {})
    # Клиенту нельзя решать, чьё это событие: сервер смотрит гостевую сессию сам.
    ev["is_guest"] = bool(_guest_session_id(request))
    ok = _se.record(ev)
    if not ok:
        raise HTTPException(400, imsg("err.bad_station_event",
                                      "неизвестное событие станции"))
    return {"ok": True}


@router.get("/api/stations/stats")
async def station_stats(days: int = 90):
    """Сводка для отладки и будущего ранкера (владельцу; гостю ничего не говорит
    о владельце — отдаём только счётчики его же событий не отдельно, а общие
    цифры без имён)."""
    c = _se.counts()
    return {"ok": True, **c, "artists": len(_se.artist_stats(days))}
