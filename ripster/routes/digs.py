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
    # Похожим артистам нужен конфиг: там лежит ключ Last.fm (если задан).
    try:
        from ripster import digs_similar as _ds
        _ds.configure(ctx.config)
    except Exception:
        pass
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
async def digs_similar(artist: str = "", limit: int = 12, exclude: str = "",
                        known: str = "last"):
    """Похожие артисты — для дерева пузырей.

    Источник: MusicBrainz (MBID по имени) + ListenBrainz. Бесплатно, без ключей.
    Deezer `/artist/{id}/related` для этого НЕ годится: отвечает 200 и `total: 0`
    даже у крупных артистов — выглядит рабочим, отдаёт пустоту.

    СНАЧАЛА НЕЗНАКОМЫЕ. Рейтинг похожести устойчив: на один запрос приходит один
    и тот же список, и наверху в нём те, кого владелец давно слушает и на кого
    подписан. Копатель показывал знакомое под видом находки (01.08.2026: «одни и
    те же, включая тех, на кого я уже подписался»). Поэтому берём кандидатов с
    запасом, помечаем знакомых и ставим их в конец.

    `known`: `last` — знакомые в конце (по умолчанию), `hide` — не показывать
    вовсе, `keep` — не трогать порядок.
    `exclude` — имена, уже висящие в дереве, через `|`: без этого следующий
    уровень повторяет предыдущий.
    """
    from ripster import digs_known as _k
    from ripster import digs_similar as _s
    name = (artist or "").strip()
    if not name:
        return {"ok": False, "error_key": "digs.e_no_artist"}

    # Кандидатов берём с запасом: после отсева знакомых и уже показанных из
    # `limit` штук осталась бы горстка.
    cand = await _s.similar(name, limit=max(limit * 4, 40))
    _k.mark(cand)

    skip = {_k._norm(x) for x in (exclude or "").split("|") if x.strip()}
    skip.add(_k._norm(name))
    cand = [c for c in cand if _k._norm(c["name"]) not in skip]

    fresh = [c for c in cand if not c["known"]]
    seen  = [c for c in cand if c["known"]]
    if known == "hide":
        items = fresh[:limit]
    elif known == "keep":
        items = cand[:limit]
    else:
        # Знакомыми добираем хвост: у нишевого артиста похожих может быть трое, и
        # все знакомые — пустое дерево хуже знакомого.
        items = (fresh + seen)[:limit]

    # Фото подтягиваем и для центрального артиста тоже — иначе центр круга
    # выглядит беднее собственных ответвлений.
    pics = await _s.artist_pics([name] + [i["name"] for i in items])
    for it in items:
        it["pic"] = pics.get(it["name"], "")
    return {"ok": True, "artist": name, "pic": pics.get(name, ""), "items": items,
            "fresh_count": len(fresh), "known_count": len(seen)}


@router.get("/api/digs/radio")
async def digs_radio(seed: str = "", exclude: str = "", limit: int = 6):
    """Следующие треки для самодополняющейся очереди.

    `exclude` — имена, уже прозвучавшие, через `|`: без этого радио начинает
    ходить по кругу из трёх артистов, и «копание» превращается в повтор.
    """
    from ripster import digs_radio as _r
    name = (seed or "").strip()
    if not name:
        return {"ok": False, "error_key": "digs.e_no_artist"}
    ex = [x for x in (exclude or "").split("|") if x.strip()]
    items = await _r.next_tracks(_cfg, name, ex, limit=max(1, min(limit, 12)))
    return {"ok": True, "seed": name, "items": items}


@router.post("/api/digs/exclude")
async def digs_exclude(body: dict):
    """«Это не моё» — вычеркнуть артиста из профиля. Слово владельца выше любых
    улик из статистики: надёжно вывести принадлежность из данных нельзя."""
    name = str((body or {}).get("artist") or "").strip()
    if not name:
        return {"ok": False, "error_key": "digs.e_empty_name"}
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


# ── Подсказки в простое («мы знакомы» / «послушай») ──────────────────────────
# Смысл не в том, чтобы напоминать о себе, а в том, чтобы простой не пропадал
# впустую: когда ничего не играет, фонотека молчит — хотя в ней лежит то, что
# человек любил и забыл, и выходят релизы в его жанрах.
#
# ДВА ВИДА, и они отвечают на разные вопросы:
#   artist  — «мы знакомы»: артист из собственного профиля вкуса, которого давно
#             не слушали. Клик открывает его дискографию.
#   release — «послушай»: свежий релиз в любимом жанре. У него есть кнопка
#             воспроизведения прямо в подсказке, чтобы согласие стоило одного
#             движения.
#
# ЧЕГО ЗДЕСЬ НЕТ — случайного «популярного». Подсказка без видимого основания
# читается как реклама, поэтому каждая несёт причину (`why_key`) и строится
# только по собственным данным владельца.
_nudge_seen: dict = {}          # что уже показывали — чтобы не повторяться
_NUDGE_REPEAT = 12 * 3600       # столько не повторяем одну и ту же подсказку


def _svc_from_url(url: str) -> str:
    """Сервис по ссылке. Находки Раскопок поля `service` не несут, а кнопке
    воспроизведения оно нужно, чтобы знать, каким движком разворачивать релиз.

    Своего перебора здесь больше нет: он был третьей копией одного знания в
    приложении, и каждая копия знала СВОЙ набор сервисов.
    """
    from ripster.digs_finds import _service_of
    return _service_of(url)


@router.get("/api/nudge")
async def digs_nudge(kind: str = "auto"):
    """Одна подсказка. `{"ok": False}` — сейчас предложить нечего, и это
    нормальный ответ: выдумывать повод нельзя."""
    import random

    now = time.time()
    for k, ts in list(_nudge_seen.items()):
        if now - ts > _NUDGE_REPEAT:
            _nudge_seen.pop(k, None)

    want = kind if kind in ("artist", "release") else random.choice(("artist", "release"))
    for attempt in (want, "release" if want == "artist" else "artist"):
        try:
            if attempt == "artist":
                from ripster import digs_finds as _f
                prof = await _digs.build_profile(limit=40, with_genres=False)
                # Чужих сюда пускать нельзя. В базе лежат ещё и загрузки гостей
                # бота, и без этого фильтра подсказка «мы знакомы» показывает
                # артиста, которого владелец никогда не слушал (первый же прогон
                # 02.08.2026 выдал именно такого). Порог по счётчику — вторая
                # страховка: одна случайная загрузка знакомством не является.
                alien = _f.foreign_artists(_cfg)

                def _weight(a: dict) -> int:
                    # Профиль НЕ несёт поля `count` — в нём `plays`, `downloads`
                    # и сводный `score`. Первый заход 02.08.2026 фильтровал по
                    # `count`, тот везде None, и пул схлопывался в ноль: вид
                    # «мы знакомы» не показывался ни разу, молча уступая релизам.
                    return int(a.get("plays") or 0) + int(a.get("downloads") or 0)

                pool = [a for a in (prof.get("artists") or [])
                        if a.get("name") and not a.get("is_show")
                        and _weight(a) >= 1
                        and _digs._norm(a["name"]) not in alien
                        and f"a:{a['name']}" not in _nudge_seen]
                if pool:
                    a = random.choice(pool[:30])
                    _nudge_seen[f"a:{a['name']}"] = now
                    return {"ok": True, "kind": "artist", "name": a["name"],
                            "genre": a.get("genre") or "",
                            "cover": a.get("cover") or "",
                            "why_key": "nudge.why_known",
                            "why_args": {"n": _weight(a)}}
            else:
                from ripster import digs_finds as _f
                data = await _f.find_all(_cfg, per_kind=6)
                items = [it for grp in (data.get("digs") or {}).values()
                         for it in (grp or []) if it.get("url")
                         and f"r:{it.get('url')}" not in _nudge_seen]
                if items:
                    it = random.choice(items)
                    _nudge_seen[f"r:{it.get('url')}"] = now
                    return {"ok": True, "kind": "release",
                            "title": it.get("title") or "", "artist": it.get("artist") or "",
                            "cover": it.get("cover") or "", "url": it.get("url") or "",
                            # Без сервиса кнопка «включить» не знает, чем играть,
                            # а находки Раскопок его не несут — выводим из ссылки.
                            "service": it.get("service") or _svc_from_url(it.get("url") or ""),
                            "why_key": it.get("reason_key") or "nudge.why_taste",
                            "why_args": it.get("reason_args") or {}}
        except Exception:
            continue
    return {"ok": False}
