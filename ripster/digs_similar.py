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

_MB_URL = "https://musicbrainz.org/ws/2/artist"
_LB_URL = "https://labs.api.listenbrainz.org/similar-artists/json"
# Порядок важен: первый рабочий и берём. Имя алгоритма — не украшение, неверное
# даёт 400, а не пустой ответ.
_LB_ALGOS = (
    "session_based_days_9000_session_300_contribution_5_threshold_15_limit_50_skip_30",
    "session_based_days_7500_session_300_contribution_5_threshold_15_limit_50",
)
_UA = {"User-Agent": "Ripster/3.3 (personal music library tool)"}


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


async def _mbid(client, name: str) -> str:
    try:
        r = await client.get(_MB_URL, params={"query": name, "fmt": "json", "limit": 1})
        if r.status_code != 200:
            return ""
        arts = (r.json() or {}).get("artists") or []
        if not arts:
            return ""
        # Сверяем имя: MusicBrainz всегда что-нибудь возвращает, и без проверки
        # «Debit» легко превращается в постороннего артиста.
        got = (arts[0].get("name") or "").strip().lower()
        return arts[0].get("id", "") if got == name.strip().lower() else ""
    except Exception:
        return ""


async def similar(name: str, limit: int = 12) -> list[dict]:
    """Похожие на `name`. Пустой список — это НЕ ошибка: у малоизвестного артиста
    данных может не быть вовсе, и врать выдуманными именами хуже, чем молчать."""
    import httpx

    key = (name or "").strip().lower()
    if not key:
        return []
    cache = _load()
    hit = cache.get(key)
    if hit and (time.time() - hit.get("ts", 0)) < _TTL:
        return hit.get("items", [])[:limit]

    out: list[dict] = []
    async with httpx.AsyncClient(timeout=20, headers=_UA, follow_redirects=True) as c:
        mbid = await _mbid(c, name)
        if mbid:
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
    out.sort(key=lambda x: x["score"], reverse=True)
    cache[key] = {"ts": int(time.time()), "items": out[:40]}
    _save(cache)
    return out[:limit]
