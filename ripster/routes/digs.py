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


_save_cfg = None


def install(app, ctx) -> None:
    global _cfg, _save_cfg
    _cfg = ctx.config
    _save_cfg = ctx.save_config
    app.include_router(router)


_finds_cache: dict = {"data": None, "ts": 0.0}


def _save_list(key: str, values: list) -> list:
    """Записать список в конфиг с дедупом и разумным потолком."""
    clean, seen = [], set()
    for v in values:
        s = str(v).strip()
        k = s.lower()
        if s and k not in seen and len(s) <= 80:
            seen.add(k)
            clean.append(s)
    _cfg[key] = clean[:200]
    if _save_cfg:
        _save_cfg(_cfg)
    _finds_cache["data"] = None          # профиль изменился — пересчитать
    return clean


@router.post("/api/digs/favorites")
async def digs_favorites(body: dict):
    """Что человек объявил своим САМ.

    Нужно по трём причинам, и ни одна не покрывается статистикой:
    у нового человека истории нет вовсе; в базе лежат ещё и загрузки гостей
    бота; а направления вроде «ликвид фанк» или «балеарик транс» из жанров
    iTunes («Dance», «Electronic») не выводятся в принципе.
    """
    artists = _save_list("digs-favorite-artists", (body or {}).get("artists") or [])
    genres = _save_list("digs-favorite-genres", (body or {}).get("genres") or [])
    return {"ok": True, "artists": artists, "genres": genres}


@router.get("/api/digs/similar")
async def digs_similar(artist: str = "", limit: int = 12):
    """Похожие артисты — для дерева пузырей.

    Источник: MusicBrainz (MBID по имени) + ListenBrainz. Бесплатно, без ключей.
    Deezer `/artist/{id}/related` для этого НЕ годится: отвечает 200 и `total: 0`
    даже у крупных артистов — выглядит рабочим, отдаёт пустоту.
    """
    from ripster import digs_similar as _s
    name = (artist or "").strip()
    if not name:
        return {"ok": False, "error": "не указан артист"}
    items = await _s.similar(name, limit=limit)
    # Фото подтягиваем и для центрального артиста тоже — иначе центр круга
    # выглядит беднее собственных ответвлений.
    pics = await _s.artist_pics([name] + [i["name"] for i in items])
    for it in items:
        it["pic"] = pics.get(it["name"], "")
    return {"ok": True, "artist": name, "pic": pics.get(name, ""), "items": items}


@router.get("/api/digs/radio")
async def digs_radio(seed: str = "", exclude: str = "", limit: int = 6):
    """Следующие треки для самодополняющейся очереди.

    `exclude` — имена, уже прозвучавшие, через `|`: без этого радио начинает
    ходить по кругу из трёх артистов, и «копание» превращается в повтор.
    """
    from ripster import digs_radio as _r
    name = (seed or "").strip()
    if not name:
        return {"ok": False, "error": "не указан артист"}
    ex = [x for x in (exclude or "").split("|") if x.strip()]
    items = await _r.next_tracks(_cfg, name, ex, limit=max(1, min(limit, 12)))
    return {"ok": True, "seed": name, "items": items}


@router.post("/api/digs/exclude")
async def digs_exclude(body: dict):
    """«Это не моё» — вычеркнуть артиста из профиля. Слово владельца выше любых
    улик из статистики: надёжно вывести принадлежность из данных нельзя."""
    name = str((body or {}).get("artist") or "").strip()
    if not name:
        return {"ok": False, "error": "пустое имя"}
    cur = list(_cfg.get("digs-exclude-artists") or [])
    cur.append(name)
    return {"ok": True, "excluded": _save_list("digs-exclude-artists", cur)}


@router.get("/api/digs/finds")
async def digs_finds(per_kind: int = 12, force: int = 0):
    """Профиль + сами находки одним запросом.

    Кэш на те же 15 минут: считается всё по локальным файлам, но профиль ходит
    за жанрами в iTunes, а стор радара — это 16 МБ JSON. Пересчитывать это на
    каждое открытие вкладки незачем.
    """
    from ripster import digs_finds as _f
    now = time.time()
    if not force and _finds_cache["data"] is not None and now - _finds_cache["ts"] < _TTL:
        return {**_finds_cache["data"], "cached": True}
    data = await _f.find_all(_cfg, per_kind=per_kind)
    _finds_cache.update({"data": data, "ts": now})
    return {**data, "cached": False}


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
