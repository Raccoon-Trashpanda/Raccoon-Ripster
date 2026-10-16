"""Маршруты радара ГРЯДУЩЕГО — того, что объявлено, но ещё не вышло.

Install: upcoming.install(app, ctx)

Обычный радар видит только появившееся в каталоге сервиса. Анонс живёт за
месяцы до этого и только в прессе — разбор новостных лент и сверка с
MusicBrainz лежат в `ripster.upcoming_radar`, здесь только доступ к ним.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query

from ripster import upcoming_radar as _ur
from ripster.i18n_msg import imsg

router = APIRouter()

_s: dict = {}

#: Сколько неподтверждённых карточек дозапрашивать за проход. MusicBrainz просит
#: не чаще запроса в секунду, и каждая карточка стоит двух — потолок держит
#: обход в разумных пределах, а список дозаполняется за несколько проходов.
_RECHECK_PER_SCAN = 20


def install(app, ctx) -> None:
    _s.update({
        "base_dir": ctx.base_dir,
        "watchlist": ctx.watchlist,
        "save_watchlist": ctx.save_watchlist,
        "config": ctx.config,
    })
    app.include_router(router)


def _base() -> Path:
    return Path(_s.get("base_dir") or ".")


def _as_card(u: _ur.Upcoming) -> dict:
    """Карточка релиза в том же виде, в каком их показывает остальное
    приложение: обложка, дата, лейбл, число вещей, авторство."""
    return {
        "artist":      u.artist,
        "title":       u.title,
        "kind":        u.kind,
        "date":        u.release_date,
        "label":       u.label,
        "track_count": u.track_count,
        "credits":     u.credits,
        "genres":      u.genres,
        "artwork":     u.artwork,
        "sources":     u.sources,
        "mbid":        u.mbid,
        # Чем больше изданий написали — тем очевиднее, что релиза ждут.
        "buzz":        len(u.sources),
    }


@router.get("/api/upcoming")
async def api_upcoming(genre: str = Query(""), limit: int = Query(200)):
    """Что объявлено и ещё не вышло.

    Порядок: сначала с подтверждённой датой (по близости), затем те, у кого
    даты пока нет. «Дата не объявлена» — не повод прятать анонс: именно про
    такие релизы чаще всего и не знаешь.
    """
    items = _ur.drop_released(_ur.load(_base()))
    g = genre.strip().lower()
    if g:
        items = [i for i in items if any(g in x.lower() for x in i.genres)]

    def order(u: _ur.Upcoming):
        return (0, u.release_date) if u.release_date else (1, "")

    items.sort(key=order)
    return {"items": [_as_card(u) for u in items[:limit]],
            "sources": [{"name": s.name, "measured": s.measured, "note": s.note}
                        for s in _ur.SOURCES]}


@router.post("/api/upcoming/scan")
async def api_upcoming_scan(confirm: bool = Query(True)):
    """Обойти ленты и обновить список.

    Сверка с MusicBrainz идёт по одному запросу в секунду — это их правило.
    Поэтому за проход подтверждаются новые записи и ограниченная порция старых,
    у которых даты до сих пор нет: список дозаполняется за несколько проходов,
    а не встаёт колом на первой же неудаче.
    """
    known = _ur.load(_base())
    known_keys = {i.key for i in known}

    fresh = await asyncio.to_thread(_ur.collect)
    new = [f for f in fresh if f.key not in known_keys]

    # Дозапрашиваем и СТАРЫЕ неподтверждённые. Сбой MusicBrainz — обычное дело
    # (лимит запроса в секунду, редкие 503), а подтверждали мы только новинки:
    # одна неудача оставляла карточку без даты, лейбла и обложки навсегда.
    # Замер 05.09.2026 показал ровно это — дата была у 3 карточек из 30.
    stale = [k for k in known if not k.release_date][:_RECHECK_PER_SCAN]

    if confirm:
        todo = new + stale
        if todo:
            await asyncio.to_thread(lambda: [_ur.confirm(x) for x in todo])

    merged = _ur.drop_released(_ur.merge(known, fresh))
    _ur.save(_base(), merged)
    return {"ok": True, "found": len(fresh), "new": len(new), "total": len(merged)}


@router.post("/api/upcoming/wait")
async def api_upcoming_wait(body: dict):
    """«Жду» — артист уходит в вишлист, и релиз будет пойман, как только
    появится у сервисов.

    Радар грядущего знает, что релиз БУДЕТ; ловить его появление умеет вишлист
    (в том числе через ранние витрины). Соединять их вручную человеку незачем.
    """
    artist = (body.get("artist") or "").strip()
    if not artist:
        raise HTTPException(400, imsg("err.artist_required", "нужно имя артиста"))

    items = _s.get("watchlist")
    if items is None:
        raise HTTPException(503, imsg("err.watchlist_unavailable", "вишлист недоступен"))

    if any((w.get("name") or "").strip().lower() == artist.lower() for w in items):
        return {"ok": True, "already": True}

    from datetime import datetime
    items.append({
        "id":            f"wl_{int(datetime.now().timestamp() * 1000)}",
        "name":          artist,
        "kind":          "artist",
        "url":           "",
        "service":       (_s.get("config") or {}).get("default-service", "deezer"),
        "artist_id":     "",
        "quality":       (_s.get("config") or {}).get("quality", "alac"),
        "added":         datetime.now().isoformat(timespec="seconds"),
        "last_check":    None,
        "last_release":  None,
        # Ждём ИМЕННО объявленный релиз, а не всю дискографию: без базовой
        # отметки первая же проверка вывалила бы в очередь весь каталог.
        "last_release_date": "",
        "auto_download": bool(body.get("auto_download", False)),
        "from_upcoming": True,
    })
    _s["save_watchlist"](items)
    return {"ok": True, "added": artist}
