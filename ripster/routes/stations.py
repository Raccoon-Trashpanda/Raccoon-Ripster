"""Жанровые станции — маршруты.

  GET  /api/stations                     список плиток
  GET  /api/stations/home                плитки + личное одним запросом
  GET  /api/station?id=…&limit=…         эфир ОДНОЙ пачкой (старый вызов, жив)
  GET  /api/station/preview?id=…         обложки для плитки
  GET  /api/station/artist?name=…        станция вокруг артиста

  POST /api/station/session              открыть СЕССИЮ (пачки + дозаказ)
  POST /api/station/session/{id}/next    следующая пачка, без сети
  POST /api/station/session/{id}/knobs   ручки внутри живого эфира
  GET  /api/station/session/{id}         состояние: резерв, выданное, что дальше

Контракт сессии — запросы, ответы и что делать плееру — описан целиком в
`ripster/station_sessions.py` (шапка модуля). Здесь только вход: форма тела,
гостевой барьер и то, что отказ обязан иметь причину.

Сама сборка — в `ripster/stations.py`; правила и почему они именно такие
описаны там же и в скилле `ripster-cross-service-availability` (соседний по духу
принцип: спрашиваем все источники разом, «не знаю» не равно «чужое»).
"""
from __future__ import annotations

from fastapi import APIRouter, Query, Request

router = APIRouter()

_config: dict = {}
_base_dir = "."


def install(app, ctx) -> None:
    global _config, _base_dir
    _config = ctx.config
    _base_dir = str(getattr(ctx, "base_dir", ".") or ".")
    app.include_router(router)


def _is_guest(request: Request) -> bool:
    """Чьё это слушание, решает СЕРВЕР по гостевой сессии — тот же закон, что в
    приёмнике событий: иначе гостевой браузер назвал бы себя владельцем и
    переучил его станции на свой вкус."""
    try:
        from ripster.routes.station_events import _guest_session_id
        return bool(_guest_session_id(request))
    except Exception:                                          # noqa: BLE001
        return False


@router.get("/api/stations")
async def api_stations():
    from ripster import stations as _st
    return {"ok": True, "stations": _st.catalog()}


@router.get("/api/stations/home")
async def api_stations_home():
    """Всё для вкладки разом: плитки + личное (кого слушал, что качал).

    Одним запросом, а не пятью: страница открывается целиком, и половина
    секций, приезжающих вразнобой, выглядела бы как поломка.
    """
    from ripster import stations as _st
    return {"ok": True, "tiles": _st.catalog(), **_st.personal(_base_dir)}


@router.get("/api/station/preview")
async def api_station_preview(id: str = Query("")):
    """Догреть превью ОДНОЙ станции — обложки для плитки.

    Отдельным маршрутом, а не пачкой в /home: собрать тридцать станций разом
    ради картинок — это тридцать опросов витрин ради данных, на которые ещё
    никто не смотрел. Вкладка догревает их по одной, в фоне и только те,
    которых не хватает.
    """
    from ripster import stations as _st
    if not id:
        return {"ok": False, "reason": "нужен id станции"}
    have = _st.preview_of(id)
    if have:
        return {"ok": True, "id": id, **have}
    res = await _st.build(id, limit=12)
    return {"ok": bool(res.get("ok")), "id": id, **_st.preview_of(id),
            "reason": res.get("reason", "")}


@router.get("/api/station/artist")
async def api_station_artist(name: str = Query(""), limit: int = Query(25),
                             seed: int = Query(0)):
    """Станция вокруг артиста: он сам и соседи по жанру."""
    from ripster import stations as _st
    return await _st.by_artist(name, limit=max(1, min(100, limit)), seed=seed or None)


@router.get("/api/station")
async def api_station(id: str = Query(""), limit: int = Query(30), seed: int = Query(0)):
    """Эфир станции ОДНОЙ пачкой. Пустой ответ приходит С ПРИЧИНОЙ, а не молча.

    Старый вызов остаётся: он умеет только «нажал — получил список». Живой эфир,
    который дозаказывается и меняет курс по фидбеку, — `/api/station/session`
    ниже.
    """
    from ripster import stations as _st
    if not id:
        return {"ok": False, "reason": "нужен id станции", "tracks": []}
    return await _st.build(id, limit=max(1, min(100, limit)), seed=seed or None)


# ── Сессия: пачки, дозаказ, пересборка по фидбеку ────────────────────────────
#
# Контракт (что шлёт фронт и что получает обратно) — в шапке
# `ripster/station_sessions.py`. Раздача здесь тонкая: проверить вход, решить
# гостевой барьер и позвать модуль.

def _batch_of(body: dict) -> int:
    try:
        b = int((body or {}).get("batch") or 0)
    except (TypeError, ValueError):
        return 0
    if b <= 0:
        return 0        # «не задано», а НЕ «одна песня»
    # `max(1, …)` на нуле давал 1, и вызывающий (`_batch_of(body) or None`)
    # получал не None, а единицу: сессия, открытая пачкой по 4, со следующего
    # `next` навсегда переходила на один трек за раз — размер пачки терялся
    # ровно там, где тело запроса пустое, то есть в обычном ходе плеера.
    return min(25, b)


@router.post("/api/station/session")
async def api_station_session_open(body: dict, request: Request):
    """Открыть станцию как сессию. Единственный запрос, который ходит в сеть."""
    from ripster import station_sessions as _ss
    sid = str((body or {}).get("id") or "").strip()
    if not sid:
        return {"ok": False, "reason": "нужен id станции", "tracks": []}
    try:
        seed = int((body or {}).get("seed")) or None
    except (TypeError, ValueError):
        seed = None
    sv = (body or {}).get("services")
    return await _ss.open_station(sid, batch=_batch_of(body) or _ss.BATCH, seed=seed,
                                  knobs=(body or {}).get("knobs"),
                                  services=sv if isinstance(sv, list) else None,
                                  is_guest=_is_guest(request))


@router.post("/api/station/session/{session_id}/next")
async def api_station_session_next(session_id: str, body: dict, request: Request):
    """Следующая пачка того же эфира. Сети нет — кандидаты лежат в сессии."""
    from ripster import station_sessions as _ss
    ev = (body or {}).get("events")
    return _ss.next_batch(session_id, events=ev if isinstance(ev, list) else None,
                          knobs=(body or {}).get("knobs"), batch=_batch_of(body) or None)


@router.post("/api/station/session/{session_id}/knobs")
async def api_station_session_knobs(session_id: str, body: dict):
    """Ручки внутри живого эфира: пересобирается ОСТАТОК пула, без новой сети."""
    from ripster import station_sessions as _ss
    return _ss.apply_knobs(session_id, (body or {}).get("knobs"))


@router.get("/api/station/session/{session_id}")
async def api_station_session_state(session_id: str):
    """Состояние сессии: резерв, сколько выдано, ближайшие кандидаты (`next_up`).

    По `next_up` и видно, что фидбек ПЕРЕСТАВИЛ очередь, а не «просто не упал».
    """
    from ripster import station_sessions as _ss
    return _ss.state(session_id)
