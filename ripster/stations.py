"""
Жанровые станции для ПК-Рипстера.

Перенос того, что уже работает на телефоне (`core/service/Station.kt` и
`WaveStations.kt`), — владелец 06.09.2026: «а ты внедрил нашу разработку по
станциям в пк рипстер? я вот смотрю и как будто бы нет».

Не внедрял: на ПК станций не было вовсе, а жанровый движок (`genre_oracle`)
звался ровно из одного места — `routes/pairing.py`, то есть считал жанры ДЛЯ
ТЕЛЕФОНА. Классический случай «модуль есть, продукт его для себя не зовёт».

ПРАВИЛА — ТЕ ЖЕ, ЧТО НА ТЕЛЕФОНЕ, И ЭТО НАМЕРЕННО

1. **Плитка обязана играть то, что на ней написано.** Источник шире названия —
   это не «лучше, чем ничего», а подмена. На телефоне это стоило рабочей
   плитки: «Синтвейв» открывал ликвид-фанк, потому что за ним стояла станция
   «электроника вообще». Поэтому у поджанров курируемого источника просто НЕТ,
   и станция собирается точным запросом.

2. **Спрашиваем ВСЕ сервисы разом, а не по очереди до первого ответившего.**
   Отказ одного не мешает остальным: каждый источник обёрнут отдельно.

3. **Жанр сверяем по тому, что объявил САМ сервис.** «Не знаю» — не «чужое»:
   Deezer в поиске жанр не отдаёт вовсе, и выбрасывать его выдачу значило бы
   остаться без половины эфира.

4. **Курируемое ведёт, поиск добирает.** У чарта жанр задан сервисом, проверять
   его нечем и незачем; у поиска — обязан пройти сверку.

5. **Популярность — из настоящих счётчиков**, которые сервисы и так шлют
   (`rank` Deezer, прослушивания и лайки SoundCloud), по логарифму: линейно всё,
   кроме мировых хитов, было бы нулём. У кого счётчика нет — СРЕДНИЙ вес, а не
   нулевой, иначе такие сервисы исчезли бы из эфира совсем.

6. **Отбор взвешенный и без повторов**, с зерном на нажатие: популярное звучит
   чаще, но шанс есть у каждого, а следующее нажатие даёт другой эфир.

Чего здесь НЕТ и почему: станции Яндекс-ротора. На телефоне они первый
источник, но на ПК Glagol/ротор-клиента нет (см. пункт au8 трекера), и
подставлять вместо ротора «что-нибудь похожее» — ровно та подмена, против
которой написан пункт 1.
"""
from __future__ import annotations

import asyncio
import math
import random
import re
import time
from typing import Optional

# ── Таблица станций ──────────────────────────────────────────────────────────
# Один в один с мобильной `WAVE_STATIONS`. `sc` пуст там, где чарт SoundCloud
# называет НЕ то же самое, что плитка: пустой источник лучше широкого.
#   (id, чарт SoundCloud, точный запрос, показываемое имя)
STATIONS: list[tuple[str, str, str, str]] = [
    ("techno",     "techno",     "techno",              "Techno"),
    ("trance",     "trance",     "trance",              "Trance"),
    ("ambient",    "ambient",    "ambient",             "Ambient"),
    ("dnb",        "drumbass",   "drum and bass",       "Drum & Bass"),
    ("jazz",       "jazzblues",  "jazz",                "Jazz"),
    ("classical",  "classical",  "classical",           "Classical"),
    ("deephouse",  "deephouse",  "deep house",          "Deep House"),
    ("proghouse",  "",           "progressive house",   "Progressive House"),
    ("meltech",    "",           "melodic techno",      "Melodic Techno"),
    ("dubtechno",  "",           "dub techno",          "Dub Techno"),
    ("downtempo",  "",           "downtempo",           "Downtempo"),
    ("synthwave",  "",           "synthwave",           "Synthwave"),
    ("idm",        "",           "idm",                 "IDM"),
    ("lofi",       "",           "lofi hip hop",        "Lo-Fi"),
    ("indie",      "indie",      "indie rock",          "Indie"),
    ("indiepop",   "",           "indie pop",           "Indie Pop"),
    ("soul",       "soul",       "soul",                "Soul"),
    ("rnb",        "rbsoul",     "r&b",                 "R&B"),
    ("disco",      "disco",      "disco",               "Disco"),
    ("funk",       "funk",       "funk",                "Funk"),
    ("hiphop",     "hiphoprap",  "hip hop",             "Hip-Hop"),
    ("rock",       "rock",       "rock",                "Rock"),
    ("postpunk",   "",           "post-punk",           "Post-Punk"),
    ("shoegaze",   "",           "shoegaze",            "Shoegaze"),
    ("metal",      "metal",      "metal",               "Metal"),
    ("blues",      "jazzblues",  "blues",               "Blues"),
    ("reggae",     "reggae",     "reggae",              "Reggae"),
    ("dub",        "",           "dub",                 "Dub"),
    ("afrobeat",   "",           "afrobeat",            "Afrobeat"),
    ("latin",      "latin",      "latin",               "Latin"),
]

_BY_ID = {s[0]: s for s in STATIONS}

# Сервисы, у которых спрашиваем поиском. Порядок значения не имеет — идут разом.
SEARCH_SERVICES = ("deezer", "qobuz", "tidal", "apple", "yandex")

# Вес для сервиса без счётчика популярности. Не ноль и не максимум: «не знаю»
# должно оставлять запись в игре, а не выбрасывать её и не поднимать наверх.
_NEUTRAL_WEIGHT = 0.5


def norm(s: str) -> str:
    """Жанр к сравнимому виду: только буквы и цифры, нижний регистр.

    Одно и то же пишут по-разному: «Synth Wave» и «Synthwave», «Drum & Bass» и
    «drumbass», «Lo-Fi» и «lofi». Сравнение строк как есть уже стоило рабочей
    плитки на телефоне — не совпал пробел.
    """
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def genre_matches(station_id: str, declared: str) -> Optional[bool]:
    """Подходит ли объявленный сервисом жанр этой станции.

    None — сервис жанра НЕ НАЗВАЛ. Это не «чужое»: Deezer в поиске жанр не
    отдаёт вовсе, и трактовать молчание как отказ значит выкинуть половину
    эфира.
    """
    if not declared:
        return None
    st = _BY_ID.get(station_id)
    if not st:
        return None
    want = {norm(st[0]), norm(st[2]), norm(st[3])}
    d = norm(declared)
    if not d:
        return None
    return any(w and (w in d or d in w) for w in want)


def popularity_weight(item: dict) -> float:
    """Вес записи по НАСТОЯЩИМ счётчикам сервиса, логарифмически.

    Линейно всё, кроме мировых хитов, было бы нулём. Порядок выдачи за
    популярность НЕ считается: сервис ранжирует под запрос, а не по слушателям.
    """
    raw = 0.0
    for key in ("rank", "playback_count", "likes_count", "popularity"):
        v = item.get(key)
        if isinstance(v, (int, float)) and v > 0:
            raw = max(raw, float(v))
    if raw <= 0:
        return _NEUTRAL_WEIGHT
    # rank Deezer ~1e6, прослушивания SC ~1e7, popularity Spotify 0..100 —
    # логарифм сводит их к соизмеримым числам без подгонки под каждый сервис.
    return max(0.05, min(1.0, math.log10(raw + 10.0) / 8.0))


def _key(item: dict) -> str:
    """Чем считаем запись той же самой: ISRC, иначе «артист — название»."""
    isrc = (item.get("isrc") or "").strip().upper()
    if isrc:
        return "i:" + isrc
    return "t:" + norm(item.get("artist", "")) + "|" + norm(item.get("title", ""))


# ── Кэш артистов жанра ───────────────────────────────────────────────────────
# Живёт на диске: иначе он не переживает перезапуск, и каждое утро всё
# спрашивается заново — у MusicBrainz это прямой путь в ограничитель.
_ARTIST_TTL = 14 * 24 * 3600
_artist_cache: dict = {}
_artist_loaded = False


def _artist_cache_file():
    from pathlib import Path
    return Path("station_artists_cache.json")


def _artist_cache_load() -> None:
    global _artist_cache, _artist_loaded
    if _artist_loaded:
        return
    import json
    try:
        _artist_cache = json.loads(_artist_cache_file().read_text(encoding="utf-8"))
    except Exception:                                          # noqa: BLE001
        _artist_cache = {}
    _artist_loaded = True


def _artist_cache_get(genre: str):
    _artist_cache_load()
    rec = _artist_cache.get(norm(genre))
    if not isinstance(rec, dict):
        return None
    if time.time() - float(rec.get("ts") or 0) > _ARTIST_TTL:
        return None
    names = rec.get("names")
    return names if isinstance(names, list) else None


def _artist_cache_put(genre: str, names: list) -> None:
    import json
    _artist_cache_load()
    _artist_cache[norm(genre)] = {"ts": time.time(), "names": names}
    try:
        _artist_cache_file().write_text(json.dumps(_artist_cache, ensure_ascii=False),
                                        encoding="utf-8")
    except Exception:                                          # noqa: BLE001
        pass


async def artists_for_genre(genre: str, limit: int = 14) -> list[str]:
    """Кто ИГРАЕТ этот жанр — по весу тега в MusicBrainz.

    Это главный слой станции, а не украшение. Текстовый поиск по слову жанра
    находит вещи со словом В НАЗВАНИИ: замер 06.09.2026 на ПК по «techno» дал
    «Techno (feat. Waka Flocka Flame)», «Техно», «Techno Show», а по
    «synthwave» — Veil Of Maya, это метал-группа. При этом у настоящего
    synthwave-артиста трека со словом «synthwave» в названии обычно нет вовсе,
    то есть найтись он не мог в принципе. Ровно этот разбор уже был на телефоне
    (пункт e97 трекера) — здесь он повторён, потому что перенесена была только
    половина.

    Берём ВЕС ТЕГА, а не `score` выдачи: score — это релевантность строки
    запросу, по нему в «melodic techno» лезла Лана Дель Рей. Порог веса
    отсекает случайных: на замере Кайли Миноуг и Nine Inch Nails висели в
    «techno» ровно с весом 1.

    Пустой список — честный ответ «не знаю»: MusicBrainz мог не ответить, и
    выдумывать имена вместо него нельзя.
    """
    g = (genre or "").strip()
    if not g:
        return []

    # КЭШ НА ДИСКЕ, И ЭТО НЕ ОПТИМИЗАЦИЯ РАДИ СКОРОСТИ.
    #
    # Состав артистов жанра не меняется по часам, а у MusicBrainz жёсткое «не
    # чаще запроса в секунду». Без кэша каждая сборка станции тратила запрос, и
    # две подряд уже упирались в ограничитель: замер 06.09.2026 — «drum and
    # bass» собрался, а «synthwave» следом получил три отказа и остался без
    # слоя артистов, то есть плитка снова играла бы слова из названий.
    cached = _artist_cache_get(g)
    if cached is not None:
        return cached[:limit]

    from ripster import genre_sources as _gs
    rows = await _gs.musicbrainz_artists_by_tag(g, limit=40)
    if not rows:
        # Пустое НЕ кэшируем: «не ответили» и «артистов нет» — разные вещи, и
        # запомнить первое как второе значит убить жанр на неделю.
        return []
    strong = [n for w, n in rows if w >= 2]
    # Порог мягкий: если сильных мало, добираем весом 1 — лучше короткий
    # честный список, чем пустой, но и мусор наверх не пускаем.
    if len(strong) < 4:
        strong += [n for w, n in rows if w == 1][: 6 - len(strong)]
    _artist_cache_put(g, strong)
    return strong[:limit]


def credited_is(credited: str, artist: str) -> bool:
    """Значится ли [artist] среди исполнителей строки [credited].

    Сверять подстрокой нельзя. Замер 06.09.2026: для станции synthwave по
    артисту «ALEX» подошёл дуэт «Alex & Sierra» — «alex» и правда лежит
    внутри «alexsierra». Разбираем строку на исполнителей и требуем
    совпадения ЦЕЛИКОМ хотя бы с одним.

    Два подводных камня, оба пойманы тестом:

    * СНАЧАЛА сравниваем строку целиком. У многих имя само содержит «&» —
      «Dom & Roland», «Calyx & TeeBee», — и разбор на части разорвал бы
      именно тех, кого ищем.
    * Разделители берём только НА ГРАНИЦАХ СЛОВ. Без этого голые «x» и
      «and» режут внутри имён: «Alex» превращается в «Ale», «Roland» — в
      «Rol», и артист перестаёт находиться вовсе.
    """
    want = norm(artist)
    if not want or not credited:
        return False
    if norm(credited) == want:
        return True
    sep = (r"\s*(?:&|,|;|/|\bfeat\.?\b|\bft\.?\b|\bwith\b|\bvs\.?\b|\bx\b|\band\b)\s*")
    parts = re.split(sep, credited, flags=re.IGNORECASE)
    return any(norm(p) == want for p in parts if p.strip())


async def _artist_tracks(service: str, artist: str, limit: int = 4) -> list[dict]:
    """Треки конкретного артиста у одного сервиса, со СТРОГОЙ сверкой имени.

    Без сверки поиск по имени возвращает «похожее»: у сервисов свои правила
    расширения запроса, и в станцию заезжают чужие исполнители.
    """
    items = await _service_search(service, artist, limit * 3)
    out = []
    for it in items:
        if credited_is(it.get("artist", ""), artist):
            it["from_artist"] = True
            out.append(it)
        if len(out) >= limit:
            break
    return out


async def _sc_chart(slug: str, limit: int) -> list[dict]:
    """Чарт SoundCloud по жанру. Жанр здесь задан сервисом — сверять нечем."""
    if not slug:
        return []
    from ripster.routes import soundcloud as _sc
    from ripster import http_client as _HTTP
    cid = await _sc._get_client_id()
    if not cid:
        return []
    out: list[dict] = []
    async with _HTTP.ashared() as c:
        for kind in ("top", "trending"):
            r = await c.get("https://api-v2.soundcloud.com/charts", params={
                "kind": kind, "genre": f"soundcloud:genres:{slug}",
                "client_id": cid, "limit": limit,
            })
            if r.status_code != 200:
                continue
            for row in (r.json() or {}).get("collection") or []:
                t = row.get("track") or {}
                if not t.get("id"):
                    continue
                n = _sc._norm_track(t)
                n["service"] = "soundcloud"
                n["curated"] = True
                n["playback_count"] = t.get("playback_count") or 0
                n["likes_count"] = t.get("likes_count") or 0
                out.append(n)
            if out:
                break
    return out


async def _service_search(service: str, query: str, limit: int) -> list[dict]:
    """Точный запрос у одного сервиса. Ошибка одного не трогает остальных."""
    from ripster.routes import discovery as _d
    fn = {
        "deezer": _d._search_deezer,
        "qobuz": _d._search_qobuz,
        "tidal": _d._search_tidal,
        "yandex": _d._search_yandex,
    }.get(service)
    try:
        if fn is not None:
            res = await fn(query, "track", limit)
        elif service == "apple":
            res = await _d._search_apple(query, "song", limit, "")
        else:
            return []
    except Exception as e:                                     # noqa: BLE001
        print(f"[station] {service}: {type(e).__name__}: {e}", flush=True)
        return []
    items = (res or {}).get("results") or []
    for it in items:
        it.setdefault("service", service)
        it["curated"] = False
    return items


async def build(station_id: str, limit: int = 30, seed: Optional[int] = None) -> dict:
    """Собрать эфир станции.

    Возвращает `{ok, id, title, tracks, sources, reason}`. `reason` заполняется,
    только когда собрать не вышло, — экран обязан сказать правду, а не общее
    «проверьте связь».
    """
    st = _BY_ID.get(station_id)
    if not st:
        return {"ok": False, "id": station_id, "tracks": [],
                "reason": f"нет такой станции: {station_id}"}
    _id, sc_slug, query, title = st

    started = time.time()

    # СНАЧАЛА ИМЕНА. Кто играет жанр — вопрос к каталогу, а не к строке поиска.
    # Что бывает без этого слоя, описано в artists_for_genre().
    artists = await artists_for_genre(query)

    tasks = [_sc_chart(sc_slug, limit)]
    names = ["soundcloud:chart"]
    for svc in SEARCH_SERVICES:
        tasks.append(_service_search(svc, query, limit))
        names.append(svc)
    # Треки найденных артистов — у каждого сервиса свои. Спрашиваем разом,
    # отказ одного не мешает остальным.
    for a in artists[:10]:
        for svc in ("deezer", "qobuz", "tidal"):
            tasks.append(_artist_tracks(svc, a, 3))
            names.append(f"artist:{svc}")

    got = await asyncio.gather(*tasks, return_exceptions=True)
    sources: dict[str, int] = {}
    pool: list[dict] = []
    for name, res in zip(names, got):
        if isinstance(res, Exception) or not res:
            sources[name] = 0
            continue
        kept = []
        for it in res:
            if not it.get("curated"):
                # Поиск обязан пройти сверку жанра; «не знаю» пропускаем.
                if genre_matches(_id, it.get("genre") or "") is False:
                    continue
            kept.append(it)
        sources[name] = len(kept)
        pool.extend(kept)

    if not pool:
        return {"ok": False, "id": _id, "title": title, "tracks": [], "sources": sources,
                "reason": "ни один источник не дал треков этого жанра"}

    # Дедуп: ISRC вернее названия, но названием добираем то, у чего ISRC нет.
    uniq: dict[str, dict] = {}
    for it in pool:
        k = _key(it)
        if k not in uniq:
            uniq[k] = it
        elif (it.get("curated") or it.get("from_artist")) and not (
                uniq[k].get("curated") or uniq[k].get("from_artist")):
            uniq[k] = it            # запись от артиста/чарта вернее найденной словом
    items = list(uniq.values())

    # Взвешенный отбор без повторов. Зерно на вызов: перерисовка не дёргает
    # список, а следующее нажатие даёт другой эфир.
    rnd = random.Random(seed if seed is not None else int(time.time() * 1000))
    weights = []
    for it in items:
        w = popularity_weight(it)
        if it.get("curated"):
            w *= 1.35            # курируемое ведёт, но не вытесняет остальных
        if it.get("from_artist"):
            # Трек артиста жанра вернее, чем трек со словом жанра в названии.
            # Это не «чуть лучше»: без перевеса поиск по слову забивает эфир
            # своим объёмом.
            w *= 3.0
        weights.append(w)

    order: list[dict] = []
    pool_i = list(range(len(items)))
    while pool_i and len(order) < limit * 2:
        tot = sum(weights[i] for i in pool_i)
        if tot <= 0:
            break
        pick = rnd.random() * tot
        acc = 0.0
        chosen = pool_i[-1]
        for i in pool_i:
            acc += weights[i]
            if acc >= pick:
                chosen = i
                break
        pool_i.remove(chosen)
        order.append(items[chosen])

    # Анти-повтор артиста подряд: на телефоне без него шло по четыре трека
    # одного исполнителя кряду.
    # Анти-повтор артиста подряд: на телефоне без него шло по четыре трека
    # одного исполнителя кряду.
    #
    # Первая версия этого места ВСТАВЛЯЛА запасной трек и следом всё равно
    # исходный — то есть не разводила соседей, а добавляла лишнего, и один и
    # тот же трек попадал в эфир дважды («Fairy Fountain», «Pick 'Em Up»,
    # замер 06.09.2026). Правильно — отложить, а не продублировать.
    out: list[dict] = []
    used: set[int] = set()
    pending: list[int] = []
    for idx, it in enumerate(order):
        if idx in used:
            continue
        same = out and norm(out[-1].get("artist", "")) == norm(it.get("artist", ""))
        if same:
            # Ищем следующего с ДРУГИМ артистом; исходный ждёт своей очереди.
            alt = next((j for j in range(idx + 1, len(order))
                        if j not in used
                        and norm(order[j].get("artist", "")) != norm(it.get("artist", ""))), None)
            if alt is not None:
                out.append(order[alt]); used.add(alt)
                pending.append(idx)
                if len(out) >= limit:
                    break
                continue
        out.append(it); used.add(idx)
        if len(out) >= limit:
            break
    # Отложенные добираем в конец, если места ещё есть.
    for idx in pending:
        if len(out) >= limit:
            break
        if idx not in used:
            out.append(order[idx]); used.add(idx)

    return {"ok": True, "id": _id, "title": title,
            "tracks": out[:limit], "sources": sources,
            "artists": artists[:10],
            "from_artists": sum(1 for t in out[:limit] if t.get("from_artist")),
            "took_ms": int((time.time() - started) * 1000)}


def catalog() -> list[dict]:
    """Список плиток для интерфейса."""
    return [{"id": s[0], "title": s[3], "curated": bool(s[1])} for s in STATIONS]
