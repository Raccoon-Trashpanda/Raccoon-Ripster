"""Похожие артисты для «Раскопок» — дерево пузырей.

ИСТОЧНИК И ПОЧЕМУ ИМЕННО ОН
---------------------------
Проверено живьём 01.08.2026:

* **Deezer `/artist/{id}/related` — мёртв.** HTTP 200, но `total: 0` даже у
  крупных артистов. Выглядит рабочим, отдаёт пустоту — на такое легко купиться.
* **Last.fm `artist.getSimilar`** — лучший по качеству, но требует ключ, которого
  у нас нет (заведение ключа висит на владельце).
* **ListenBrainz `/similar-artists/json`** — бесплатно, без ключа, 100 похожих на
  запрос. На Bonobo отдал Boards of Canada, Massive Attack, Tycho, Air,
  Thievery Corporation, Zero 7 — по делу. Его и берём.

Ловушка алгоритма: у ListenBrainz их несколько, и имя нужно точное. Первый же
опробованный (`…days_7500…`) отвечает **400**, рабочий — `…days_9000…skip_30`.
Перебираем по списку, а не полагаемся на один.

Вход — имя артиста, поэтому сначала MusicBrainz даёт MBID. Обе службы просят
осмысленный User-Agent и не любят шквал, поэтому результат кладётся на диск
надолго: похожесть артистов не меняется от недели к неделе.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

_BASE = Path(__file__).parent.parent
_CACHE = _BASE / "digs_similar_cache.json"
_TTL = 30 * 86_400          # похожесть артистов — величина медленная
# ...но ПУСТОЙ ответ так кэшировать нельзя. MusicBrainz отдаёт 503 под нагрузкой
# (поймано живьём на «Lemongrass»), и один такой сбой записывал «похожих нет» на
# месяц вперёд. Так «Max Cooper» — у которого в ListenBrainz 88 похожих — на
# месяц стал в интерфейсе артистом без данных. Промах живёт час, не месяц.
_TTL_EMPTY = 3600

_MB_URL = "https://musicbrainz.org/ws/2/artist"
_LB_URL = "https://labs.api.listenbrainz.org/similar-artists/json"
# Порядок важен: первый рабочий и берём. Имя алгоритма — не украшение, неверное
# даёт 400, а не пустой ответ.
_LB_ALGOS = (
    "session_based_days_9000_session_300_contribution_5_threshold_15_limit_50_skip_30",
    "session_based_days_7500_session_300_contribution_5_threshold_15_limit_50",
)
_UA = {"User-Agent": "Ripster/3.4 (personal music library tool)"}

# Фото артистов. Deezer отдаёт их бесплатно и без токена, и имя в ответе можно
# сверить с запросом — проверено на выборке, совпало у всех. Это, кстати, тот же
# Deezer, чей эндпоинт /related мёртв: искать он умеет, «похожих» — нет.
_DZ_SEARCH = "https://api.deezer.com/search/artist"
_PIC_CACHE = _BASE / "digs_artist_pics.json"
_PIC_TTL = 90 * 86_400          # фото артиста меняется редко


def _load() -> dict:
    try:
        d = json.loads(_CACHE.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save(d: dict) -> None:
    try:
        _CACHE.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


async def _mbids(client, name: str) -> list[str]:
    """ВСЕ кандидаты с точным совпадением имени, а не только первый.

    Тёзки — обычное дело: по «Max Cooper» MusicBrainz отдаёт и электронщика
    (88 похожих в ListenBrainz), и вокалиста 1940-х (0). Взяв первого попавшегося,
    легко объявить мировую знаменитость артистом без данных. Проверяем по
    очереди, пока кто-то не отдаст похожих.

    Имя всё равно сверяем: MusicBrainz всегда что-нибудь возвращает, и без
    проверки «Debit» превращается в постороннего артиста.
    """
    try:
        r = await client.get(_MB_URL, params={"query": name, "fmt": "json", "limit": 8})
        if r.status_code != 200:
            return []
        want = name.strip().lower()
        return [a.get("id", "") for a in ((r.json() or {}).get("artists") or [])
                if (a.get("name") or "").strip().lower() == want and a.get("id")]
    except Exception:
        return []


def _load_pics() -> dict:
    try:
        d = json.loads(_PIC_CACHE.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save_pics(d: dict) -> None:
    try:
        _PIC_CACHE.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


async def artist_pics(names: list[str]) -> dict:
    """Имя → фото. Пусто у кого не нашлось — это не ошибка, кружок просто
    останется с инициалом.

    Сверяем имя из ответа с запросом: Deezer всегда что-нибудь возвращает, и без
    проверки в кружок «Debit» легко приезжает лицо постороннего артиста — а
    неверное лицо хуже отсутствующего.
    """
    import asyncio as _aio
    import httpx

    cache = _load_pics()
    now = int(time.time())
    todo = [n for n in names
            if (now - int((cache.get(n.lower()) or {}).get("ts", 0))) > _PIC_TTL]
    if todo:
        sem = _aio.Semaphore(6)
        async with httpx.AsyncClient(timeout=12, headers=_UA) as c:
            async def one(nm: str) -> None:
                async with sem:
                    pic = ""
                    try:
                        r = await c.get(_DZ_SEARCH, params={"q": nm, "limit": 1})
                        if r.status_code == 200:
                            d = ((r.json() or {}).get("data") or [{}])[0]
                            if (d.get("name") or "").strip().lower() == nm.strip().lower():
                                pic = d.get("picture_medium") or d.get("picture") or ""
                    except Exception:
                        pass
                    cache[nm.lower()] = {"pic": pic, "ts": now}
            await _aio.gather(*(one(n) for n in todo), return_exceptions=True)
        _save_pics(cache)
    return {n: (cache.get(n.lower()) or {}).get("pic", "") for n in names}


async def similar(name: str, limit: int = 12) -> list[dict]:
    """Похожие на `name`. Пустой список — это НЕ ошибка: у малоизвестного артиста
    данных может не быть вовсе, и врать выдуманными именами хуже, чем молчать."""
    import httpx

    key = (name or "").strip().lower()
    if not key:
        return []
    cache = _load()
    hit = cache.get(key)
    if hit:
        age = time.time() - hit.get("ts", 0)
        ttl = _TTL_EMPTY if not (hit.get("items") or []) else _TTL
        if age < ttl:
            return hit.get("items", [])[:limit]

    out: list[dict] = []
    async with httpx.AsyncClient(timeout=20, headers=_UA, follow_redirects=True) as c:
        for mbid in (await _mbids(c, name))[:3]:
            for algo in _LB_ALGOS:
                try:
                    r = await c.get(_LB_URL, params={"artist_mbids": mbid, "algorithm": algo})
                except Exception:
                    continue
                if r.status_code != 200:
                    continue
                try:
                    data = r.json()
                except Exception:
                    continue
                items = data if isinstance(data, list) else (data.get("data") or [])
                for it in items:
                    nm = (it.get("name") or it.get("artist_name") or "").strip()
                    if nm and nm.lower() != key:
                        out.append({"name": nm, "score": int(it.get("score", 0) or 0),
                                    "mbid": it.get("artist_mbid", "")})
                if out:
                    break
            if out:
                break
    out.sort(key=lambda x: x["score"], reverse=True)
    cache[key] = {"ts": int(time.time()), "items": out[:40], "empty": not out}
    _save(cache)
    return out[:limit]
