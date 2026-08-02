"""Похожие артисты для «Раскопок» — дерево пузырей.

ИСТОЧНИКИ — НЕ ОДИН, А ВСЕ, ЧТО ОТДАЮТ ПО ДЕЛУ
----------------------------------------------
Одного ресурса мало: у каждого свои пробелы, и «похожих нет» чаще означает
«этот источник не знает артиста», а не «похожих не существует». Поэтому
опрашиваем несколько и сливаем, а имя чистим ПЕРЕД поиском — половина промахов
была не в источнике, а в мусорной строке («Артист — Трек», «A / B», сборники).

  * **ListenBrainz** `/similar-artists/json` — бесплатно, без ключа, ~100 похожих.
    На Bonobo отдаёт Boards of Canada, Massive Attack, Tycho, Air — по делу.
    Ловушка: алгоритмов несколько, имя нужно точное; `…days_7500…` даёт 400,
    рабочий — `…days_9000…skip_30`. Перебираем по списку.
  * **Last.fm** `artist.getSimilar` — лучший по качеству. Работает при заданном
    `lastfm-api-key` (бесплатный, 2 минуты на регистрацию). Нет ключа — просто
    не участвует, остальные всё равно отвечают.
  * **Deezer** `/artist/{id}/related` — часто пуст (`total:0`), но у мейнстрима
    иногда отдаёт; берём как ДОПОЛНЕНИЕ, а не основу, и только если что-то есть.

Вход — имя артиста, поэтому для ListenBrainz сначала MusicBrainz даёт MBID.
Службы просят осмысленный User-Agent и не любят шквал, поэтому результат кладётся
на диск надолго: похожесть артистов не меняется от недели к неделе.
"""
from __future__ import annotations

import json
import re
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

_LASTFM_URL = "https://ws.audioscrobbler.com/2.0/"
_DZ_RELATED = "https://api.deezer.com/artist/{id}/related"

# Конфиг для ключа Last.fm прокидывается из app.py при старте.
_cfg: dict = {}


def configure(cfg: dict) -> None:
    global _cfg
    _cfg = cfg or {}


def _name_candidates(name: str) -> list[str]:
    """Из сырой строки достать реальные имена артистов для поиска.

    В «Раскопки» имя приходит как есть: «Артист — Трек», «A feat. B», «A / B»,
    «A, B & C», названия сборников. Ни один сервис такое целиком не находит —
    отсюда «большинство не ищется». Режем на кандидатов: сначала пробуем строку
    целиком (вдруг это и есть имя), потом первого автора, потом остальных.
    Порядок = приоритет: первый живой ответ и берём.
    """
    raw = (name or "").strip()
    if not raw:
        return []
    cands: list[str] = []

    def _add(x: str) -> None:
        x = x.strip(" -–—·|/.,").strip()
        if x and x.lower() not in {c.lower() for c in cands} and len(x) > 1:
            cands.append(x)

    _add(raw)
    # «Артист — Трек» / «Артист - Трек»: слева артист, справа название.
    head = re.split(r"\s[–—-]\s", raw, 1)[0]
    if head != raw:
        _add(head)
    # Разбить соавторов и брать каждого отдельным кандидатом.
    base = head if head != raw else raw
    parts = re.split(r"\s*(?:,|/|&|\bfeat\.?\b|\bft\.?\b|\bvs\.?\b|\bx\b|\bpres\.?\b|\band\b|\bwith\b)\s*",
                     base, flags=re.I)
    for p in parts:
        _add(p)
    return cands[:5]


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


async def _listenbrainz(c, name: str, key: str) -> list[dict]:
    """MusicBrainz → MBID → ListenBrainz similar. Лучший keyless-источник."""
    out: list[dict] = []
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
                    out.append({"name": nm, "score": int(it.get("score", 0) or 0) or 50,
                                "mbid": it.get("artist_mbid", ""), "src": "lb"})
            if out:
                return out
    return out


async def _lastfm(c, name: str, key: str) -> list[dict]:
    """Last.fm artist.getSimilar — лучший по качеству, но нужен бесплатный ключ."""
    api_key = str((_cfg or {}).get("lastfm-api-key") or "").strip()
    if not api_key:
        return []
    try:
        r = await c.get(_LASTFM_URL, params={
            "method": "artist.getsimilar", "artist": name, "autocorrect": 1,
            "limit": 50, "api_key": api_key, "format": "json"})
        if r.status_code != 200:
            return []
        arr = ((r.json() or {}).get("similarartists") or {}).get("artist") or []
    except Exception:
        return []
    out = []
    for a in arr:
        nm = (a.get("name") or "").strip()
        if nm and nm.lower() != key:
            # match — доля 0..1; переводим в шкалу очков, сравнимую с LB.
            try:
                sc = int(float(a.get("match") or 0) * 100)
            except (TypeError, ValueError):
                sc = 40
            out.append({"name": nm, "score": sc or 40,
                        "mbid": a.get("mbid", ""), "src": "lastfm"})
    return out


async def _deezer_related(c, name: str, key: str) -> list[dict]:
    """Deezer /related — часто пуст, но у мейнстрима иногда отдаёт. Дополнение."""
    try:
        s = await c.get(_DZ_SEARCH, params={"q": name, "limit": 1})
        if s.status_code != 200:
            return []
        d0 = ((s.json() or {}).get("data") or [{}])[0]
        if (d0.get("name") or "").strip().lower() != name.strip().lower() or not d0.get("id"):
            return []
        r = await c.get(_DZ_RELATED.format(id=d0["id"]))
        if r.status_code != 200:
            return []
        arr = (r.json() or {}).get("data") or []
    except Exception:
        return []
    out = []
    for a in arr:
        nm = (a.get("name") or "").strip()
        if nm and nm.lower() != key:
            out.append({"name": nm, "score": 30, "mbid": "", "src": "deezer"})
    return out


async def similar(name: str, limit: int = 12) -> list[dict]:
    """Похожие на `name` из НЕСКОЛЬКИХ источников сразу.

    Имя чистим до реальных кандидатов (сборники и «Артист — Трек» иначе не
    находятся вовсе), затем опрашиваем все источники и сливаем по имени. Пустой
    список — не ошибка: у совсем нишевого артиста данных может не быть нигде, а
    врать выдуманными именами хуже, чем молчать.
    """
    import asyncio as _aio
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

    merged: dict[str, dict] = {}
    async with httpx.AsyncClient(timeout=20, headers=_UA, follow_redirects=True) as c:
        # Кандидаты по приоритету: как только на очередном что-то нашлось у всех
        # источников — останавливаемся (не тащим соавторов, если основной ответил).
        for cand in _name_candidates(name):
            ck = cand.strip().lower()
            results = await _aio.gather(
                _listenbrainz(c, cand, ck),
                _lastfm(c, cand, ck),
                _deezer_related(c, cand, ck),
                return_exceptions=True,
            )
            for res in results:
                if not isinstance(res, list):
                    continue
                for it in res:
                    k = it["name"].strip().lower()
                    if k == key or k == ck:
                        continue
                    prev = merged.get(k)
                    # Один и тот же артист из двух источников — суммируем вес:
                    # согласие источников = более уверенная похожесть.
                    if prev:
                        prev["score"] = prev.get("score", 0) + it.get("score", 0)
                        prev.setdefault("srcs", set()).add(it.get("src", ""))
                    else:
                        it = dict(it)
                        it["srcs"] = {it.get("src", "")}
                        merged[k] = it
            if len(merged) >= max(limit, 12):
                break

    out = sorted(merged.values(), key=lambda x: x.get("score", 0), reverse=True)
    for it in out:
        it.pop("srcs", None)              # set не сериализуется в JSON
        it.pop("src", None)
    cache[key] = {"ts": int(time.time()), "items": out[:40], "empty": not out}
    _save(cache)
    return out[:limit]
