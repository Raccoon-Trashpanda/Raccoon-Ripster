"""Кто на самом деле знает жанр: справочник поверх Beatport и Discogs.

Владелец 05.09.2026: «у эпла, споти жанров нет, практически нигде нет. есть
только у битпорта… доверять этим потокам нельзя».

Проверено замером, и он прав. Одни и те же артисты:

    Stan Kolev     Beatport: Progressive House          Apple: Dance, Music, House
    Boris Brejcha  Beatport: Techno (Peak Time/Driving) Apple: Dance, Music, Electronic
    Lane 8         Beatport: Melodic House & Techno     Apple: Music, Electronic, Dance
    Bicep          Beatport: Electronica                Apple: Electronic, Music, Dance

Слово «Music» в ответах Apple — это буквально корневая категория каталога.
У Apple 22 широких ведра и НИ ОДНОГО поджанра; у Deezer 22 и локализованные;
у MusicBrainz размечает кто угодно (тег «melodic techno» стоял у Ланы Дель Рей).
У Beatport 45 жанров, и они рабочие: Melodic House & Techno, Techno (Peak Time /
Driving), Minimal / Deep Tech, Organic House, Indie Dance, Afro House.

Дальше два открытия, которые и определили устройство модуля — оба из замеров,
и оба противоречат первому побуждению «просто спросить Beatport про артиста»:

1. СПРАШИВАТЬ НАДО ПРО ТРЕК, А НЕ ПРО АРТИСТА. Жанр у Beatport висит на
   релизе, его ставит лейбл. Каталог артиста — это ещё и ремиксы, сборники,
   лицензионные куски, и в сумме получается шум: по артисту Anyma выходит
   «Psy-Trance 78%» (совпадение имён с другим исполнителем), Lane 8 и Nina
   Kraviz вообще отбрасывались из-за ведра «Dance / Pop». По конкретным
   трекам — 10 попаданий из 10, и все верные.

2. У ИСТОЧНИКА ЕСТЬ ОБЛАСТЬ КОМПЕТЕНЦИИ. За её пределами Beatport не
   «менее точен», а прямо неверен: Massive Attack по каталогу выходит «Pop»,
   Aretha Franklin — «R&B», Portishead — «Rock». Это не жанры этих артистов,
   а полки магазина, куда он сложил лицензированное. Поэтому ответ из
   «ведёрных» жанров мы ОТБРАСЫВАЕМ, а не понижаем в весе: неправильный ответ
   хуже отсутствующего.

   Проверка правила на живых данных: Massive Attack, Portishead и Aretha
   Franklin отсеиваются, Anyma / Nina Kraviz / Monolink / ARTBAT / Tale Of Us /
   Stephan Bodzin проходят с точным поджанром.
"""
from __future__ import annotations

import re
import time

import httpx

_API = "https://api.beatport.com/v4"

# Жанры, в которые Beatport ссыпает всё, что продаёт, но не считает своим.
# Ответ из этого списка означает «артист не из моей области», а не «жанр такой».
CATCH_ALL = {
    "pop", "rock", "r&b", "hip-hop", "country", "latin", "caribbean",
    "african", "dance / pop", "dj tools / acapellas", "brazilian funk",
}

_cache: dict[str, tuple[str | None, float]] = {}
_dc_cache: dict[str, tuple[str | None, float]] = {}
_TTL = 30 * 24 * 3600           # жанр релиза не меняется; держим месяц

# Discogs просит представляться и не любит частых запросов без ключа.
_UA = {"User-Agent": "Ripster/1.0 (+https://github.com/Raccoon-Trashpanda/Raccoon-Ripster)"}


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def is_catch_all(genre: str | None) -> bool:
    return (genre or "").strip().lower() in CATCH_ALL


def matches(artist: str, title: str, cand_artists: list[str], cand_title: str) -> bool:
    """Тот ли это трек.

    Совпадение по артисту — в обе стороны (у нас «ARTBAT», у них «ARTBAT,
    WhoMadeWho»), по названию — по первым символам, потому что у Beatport в
    названии живут пометки вида «feat. …» и «(Extended Mix)».
    """
    na, nt = norm(artist), norm(title)
    if not na or not nt:
        return False
    if not any(na in norm(c) or norm(c) in na for c in cand_artists if c):
        return False
    ct = norm(cand_title)
    return nt[:12] in ct or ct[:12] in nt


async def genre_for(artist: str, title: str, token: str) -> str | None:
    """Жанр этого трека по Beatport, или None.

    None означает «не знаю»: не нашли, не совпало, либо ответ оказался ведром.
    Ни один из этих случаев не должен превращаться в догадку — модуль, который
    учится, лучше не научить, чем научить неверно.
    """
    key = norm(artist) + "|" + norm(title)
    now = time.time()
    hit = _cache.get(key)
    if hit and now - hit[1] < _TTL:
        return hit[0]
    if not token or not artist.strip() or not title.strip():
        return None

    out: str | None = None
    try:
        async with httpx.AsyncClient(timeout=20) as cl:
            r = await cl.get(f"{_API}/catalog/search/",
                             headers={"Authorization": f"Bearer {token}"},
                             params={"q": f"{artist} {title}", "type": "tracks",
                                     "per_page": 10})
        if r.status_code == 200:
            for t in r.json().get("tracks", []):
                names = [str((x or {}).get("name") or "") for x in (t.get("artists") or [])]
                if not matches(artist, title, names, str(t.get("name") or "")):
                    continue
                g = ((t.get("genre") or {}).get("name") or "").strip()
                if g and not is_catch_all(g):
                    out = g
                break
    except Exception:
        # Сеть отвалилась — это «не знаю», и в кэш такое не кладём: иначе одна
        # осечка похоронит трек на месяц (ровно та ошибка, что была в
        # GenreCanon с недельным кэшем пустого ответа).
        return None

    _cache[key] = (out, now)
    return out


# ── Discogs: второй учитель, и он шире первого ──────────────────────────────
#
# Beatport точен внутри танцевальной электроники и неверен вне её. Discogs
# закрывает ровно эту дыру: у него есть слой `styles` поверх `genres`, и он
# размечен по строгим правилам, а не свободными тегами.
#
# Замер 05.09.2026 на том же наборе, включая те случаи, где Beatport ошибся:
#     Massive Attack — Teardrop      Trip Hop, Dub           (Beatport: Pop)
#     Portishead — Sour Times        Trip Hop, Downtempo     (Beatport: Dance/Pop)
#     Aretha Franklin — Rock Steady  Soul, Funk              (Beatport: R&B)
#     Timecop1983 — On the Run       Synth-pop, Synthwave    (MusicBrainz: пусто)
#     Lane 8 — Road                  Progressive House, Deep House
#     Deepchord — Vantage Isle       Minimal, Ambient
# Восемь из восьми, и синтвейв нашёлся там, где у MusicBrainz по этому тегу
# не было НИ ОДНОГО артиста.
#
# Ключ не нужен. Это важно не для удобства, а для честности: справочник,
# который работает только у владельца, бесполезен для всех остальных.
async def discogs_genre(artist: str, title: str) -> str | None:
    """Стиль этого трека по Discogs, или None.

    Берём `style` (точный слой), а `genre` — только если стиля нет вовсе:
    «Electronic» без стиля ничем не лучше апловского «Dance».
    """
    key = norm(artist) + "|" + norm(title)
    now = time.time()
    hit = _dc_cache.get(key)
    if hit and now - hit[1] < _TTL:
        return hit[0]
    if not artist.strip() or not title.strip():
        return None
    out: str | None = None
    try:
        async with httpx.AsyncClient(timeout=20) as cl:
            r = await cl.get("https://api.discogs.com/database/search", headers=_UA,
                             params={"artist": artist, "track": title,
                                     "type": "release", "per_page": 5})
        if r.status_code == 200:
            styles: dict[str, int] = {}
            genres: dict[str, int] = {}
            for x in (r.json().get("results") or [])[:5]:
                for st in (x.get("style") or []):
                    styles[st] = styles.get(st, 0) + 1
                for gn in (x.get("genre") or []):
                    genres[gn] = genres.get(gn, 0) + 1
            pick = styles or genres
            if pick:
                out = max(pick.items(), key=lambda kv: kv[1])[0]
    except Exception:
        return None          # «не знаю» — и в кэш такое не кладём
    _dc_cache[key] = (out, now)
    return out


async def best_genre(artist: str, title: str, beatport_token: str = "") -> tuple[str | None, str]:
    """Лучший доступный ярлык и ЧЕЙ он.

    Порядок не «кто первый ответит», а по области компетенции:
      1. Beatport — если ответил не ведром, это ярлык лейбла на релизе, точнее
         не бывает; но он молчит обо всём, что не танцевальная электроника;
      2. Discogs — шире и почти всегда попадает, включая то, где Beatport врёт.

    Возвращает `(ярлык, источник)`; `(None, "")` означает честное «не знаю».
    Источник отдаётся наружу намеренно: обучаемому модулю на той стороне важно,
    насколько доверять учителю, а не только что он сказал.
    """
    if beatport_token:
        g = await genre_for(artist, title, beatport_token)
        if g:
            return g, "beatport"
    g = await discogs_genre(artist, title)
    if g:
        return g, "discogs"
    return None, ""
