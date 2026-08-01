"""«Раскопки» — радио: очередь, которая сама дописывается по вкусу.

ЗАЧЕМ
-----
Без этого «Раскопки» остаются списком: нашёл — забрал — закрыл. Включил один
трек, а дальше тишина. Смысл копателя в том, чтобы копание продолжалось само:
играет трек — под него подкладываются следующие, и очередь не кончается.

ОТКУДА БЕРУТСЯ ТРЕКИ
-------------------
1. Похожие артисты (MusicBrainz + ListenBrainz, см. digs_similar) — от того, что
   играет прямо сейчас.
2. Если похожих нет (у нишевых артистов их часто нет вовсе) — опоры собственного
   профиля вкуса. Лучше играть своё, чем молчать.
3. Конкретный трек ищется в Deezer: он публичный, без токена, и плеер Ripster
   умеет его стримить. Apple/Qobuz/Tidal сюда не годятся — им нужен свой
   рабочий токен, а радио должно играть даже когда настроен один сервис.

ЧЕГО ЗДЕСЬ СОЗНАТЕЛЬНО НЕТ
--------------------------
Случайной подмешки «популярного». Радио, которое подсовывает хиты, — это чужая
лента, а не раскопки; такой шарм у Ripster уже есть у всех остальных.
"""
from __future__ import annotations

import asyncio

from ripster import digs_similar as _sim
from ripster.digs import _norm, _split_credit

_DZ_SEARCH = "https://api.deezer.com/search/track"
_UA = {"User-Agent": "Ripster/3.4 (personal music library tool)"}


async def _top_track(client, artist: str) -> dict | None:
    """Самый ходовой трек артиста в Deezer.

    Имя сверяется: Deezer всегда что-нибудь возвращает, и без проверки в радио
    приезжает трек постороннего исполнителя — а это ровно то, из-за чего чужие
    рекомендации и раздражают.
    """
    try:
        r = await client.get(_DZ_SEARCH, params={"q": f'artist:"{artist}"', "limit": 5})
        if r.status_code != 200:
            return None
        want = _norm(artist)
        for it in ((r.json() or {}).get("data") or []):
            nm = ((it.get("artist") or {}).get("name") or "").strip()
            if _norm(nm) != want:
                continue
            alb = it.get("album") or {}
            return {
                "service": "deezer",
                "id":      str(it.get("id") or ""),
                "title":   it.get("title") or "",
                "artist":  nm,
                "cover":   alb.get("cover_medium") or alb.get("cover") or "",
                "full":    True,
                "posKey":  f"deezer:{it.get('id')}",
                "why":     "",
            }
    except Exception:
        return None
    return None


async def next_tracks(cfg: dict | None, seed: str, exclude: list[str] | None = None,
                      limit: int = 6) -> list[dict]:
    """Следующие треки после `seed`. Пустой список — честный ответ «нечего»."""
    import httpx

    seen = {_norm(x) for x in (exclude or [])}
    seen.add(_norm(seed))

    names: list[tuple[str, str]] = []          # (имя, почему)
    for it in await _sim.similar(seed, limit=24):
        k = _norm(it["name"])
        if k and k not in seen:
            seen.add(k)
            names.append((it["name"], f"похож на {seed}"))

    if len(names) < limit:
        # Похожих не нашлось (у нишевых артистов данных часто нет) — играем
        # СВОИХ. Молчание хуже, чем знакомое.
        from ripster.digs_finds import find_all
        try:
            prof = (await find_all(cfg, per_kind=1))["profile"]
            for a in prof.get("artists", []):
                k = _norm(a["name"])
                if k and k not in seen and not a.get("is_show"):
                    seen.add(k)
                    names.append((a["name"], "из твоих опорных"))
                if len(names) >= limit * 2:
                    break
        except Exception:
            pass

    out: list[dict] = []
    if not names:
        return out
    sem = asyncio.Semaphore(6)
    async with httpx.AsyncClient(timeout=12, headers=_UA) as c:
        async def one(nm: str, why: str) -> None:
            async with sem:
                tr = await _top_track(c, nm)
                if tr:
                    tr["why"] = why
                    out.append(tr)
        await asyncio.gather(*(one(n, w) for n, w in names[:limit * 3]),
                             return_exceptions=True)
    return out[:limit]
