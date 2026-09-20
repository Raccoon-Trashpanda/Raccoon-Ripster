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


def norm_any(s: str) -> str:
    """Как [norm], но не выбрасывает нелатинские буквы.

    [norm] оставляет только `[a-z0-9]` — она для сравнения латинских
    написаний, и менять её нельзя: на ней стоят сверки жанров и имён. Но
    сервисы называют жанр на языке своей витрины («Денс», «Джаз», «Електро»),
    и через [norm] такие строки становились ПУСТЫМИ — то есть ни одно русское
    написание не находилось в таблице вовсе (поймано на живой истории
    06.09.2026).
    """
    return "".join(ch for ch in (s or "").lower() if ch.isalnum())


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


# ── Как сервисы называют жанры ───────────────────────────────────────────────
#
# Это ВХОДНЫЕ данные, а не подписи: сервис отдаёт жанр на языке своей витрины и
# своей широты. Замер по живой истории 06.09.2026: «Electronic», «Dance»,
# «Денс», «Джаз», «Hip-Hop/Rap» — ни одно из этих написаний не совпадало с
# нашими плитками, и целый пласт прослушиваний никуда не вёл.
#
# Кириллица здесь законна по той же причине, что и в мобильном GenreKey:
# перевести её нельзя — сравнение сломается.
#
# Широкие названия («Electronic», «Dance») ведут на КОНКРЕТНУЮ плитку
# намеренно: это не утверждение «вы слушаете техно», а кратчайший честный путь
# от «электроника» к чему-то, что реально играет. Плитка при этом называется
# своим именем, подмены нет.
GENRE_ALIASES: dict[str, str] = {
    "electronic": "techno", "electronica": "techno", "электроника": "techno",
    "електро": "techno", "electro": "techno",
    "dance": "deephouse", "денс": "deephouse", "танцевальная": "deephouse",
    "house": "deephouse", "хаус": "deephouse",
    "hiphoprap": "hiphop", "rap": "hiphop", "хипхоп": "hiphop", "рэп": "hiphop",
    "jazz": "jazz", "джаз": "jazz",
    "classical": "classical", "классика": "classical", "классическая": "classical",
    "rock": "rock", "рок": "rock",
    "metal": "metal", "метал": "metal",
    "pop": "indiepop", "поп": "indiepop",
    "rnb": "rnb", "rbsoul": "rnb", "соул": "soul", "soul": "soul",
    "reggae": "reggae", "регги": "reggae",
    "blues": "blues", "блюз": "blues",
    "funk": "funk", "фанк": "funk",
    "disco": "disco", "диско": "disco",
    "ambient": "ambient", "эмбиент": "ambient",
    "drumbass": "dnb", "drumandbass": "dnb", "dnb": "dnb", "драмнбейс": "dnb",
    "techno": "techno", "техно": "techno",
    "trance": "trance", "транс": "trance",
    "indie": "indie", "инди": "indie",
    "latin": "latin", "латина": "latin",
}


def station_for_declared(declared: str) -> str:
    """Плитка, которой соответствует объявленный сервисом жанр. Пусто — не знаем.

    Сперва прямое совпадение с плиткой, потом таблица написаний. Не нашли —
    возвращаем пусто: жанр останется в списке как факт, но без ссылки. Это
    честнее, чем увести человека на соседний жанр.
    """
    if not (declared or "").strip():
        return ""
    for s in STATIONS:
        if genre_matches(s[0], declared):
            return s[0]
    return GENRE_ALIASES.get(norm_any(declared), "")


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
# Версия состава в кэше. Меняеться, когда меняется САМ ОТОРБОР: списки,
# собранные старым правилом, новый отбор должен пересчитать, а не раздавать.
_ARTIST_CACHE_V = 2
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
    if not isinstance(rec, dict) or rec.get("v") != _ARTIST_CACHE_V:
        return None
    if time.time() - float(rec.get("ts") or 0) > _ARTIST_TTL:
        return None
    names = rec.get("names")
    return names if isinstance(names, list) else None


def _artist_cache_put(genre: str, names: list) -> None:
    import json
    _artist_cache_load()
    _artist_cache[norm(genre)] = {"v": _ARTIST_CACHE_V, "ts": time.time(), "names": names}
    try:
        _artist_cache_file().write_text(json.dumps(_artist_cache, ensure_ascii=False),
                                        encoding="utf-8")
    except Exception:                                          # noqa: BLE001
        pass


# ── Превью станции ───────────────────────────────────────────────────────────
#
# Плитка показывает обложки из НАСТОЯЩЕЙ выдачи станции, а не подобранную
# картинку. Значит превью появляется только тогда, когда станция хоть раз
# собиралась: рисовать «примерную» обложку для жанра — это то же враньё, что и
# бейдж качества, взятый из константы.
#
# Собирать все тридцать станций разом ради красоты нельзя: это тридцать
# опросов витрин ради данных, на которые ещё никто не смотрел. Поэтому превью
# КОПИТСЯ: каждая собранная станция оставляет своё, а вкладка догревает
# недостающие по одной, в фоне.
_PREVIEW_TTL = 7 * 24 * 3600
_preview: dict = {}
_preview_loaded = False


def _preview_file():
    from pathlib import Path
    return Path("station_preview_cache.json")


def _preview_load() -> None:
    global _preview, _preview_loaded
    if _preview_loaded:
        return
    import json
    try:
        _preview = json.loads(_preview_file().read_text(encoding="utf-8"))
    except Exception:                                          # noqa: BLE001
        _preview = {}
    _preview_loaded = True


def _preview_save() -> None:
    import json
    try:
        _preview_file().write_text(json.dumps(_preview, ensure_ascii=False),
                                   encoding="utf-8")
    except Exception:                                          # noqa: BLE001
        pass


def _preview_put(station_id: str, tracks: list) -> None:
    """Запомнить, чем станция выглядит: обложки, сервисы, сколько треков."""
    covers, services = [], []
    for t in tracks:
        c = (t.get("cover") or "").strip()
        if c and c not in covers and len(covers) < 4:
            covers.append(c)
        sv = (t.get("service") or "").strip()
        if sv and sv not in services:
            services.append(sv)
    if not covers:
        return                       # нечего показывать — и не запоминаем
    _preview_load()
    _preview[station_id] = {"covers": covers, "services": services,
                            "tracks": len(tracks), "ts": time.time()}
    _preview_save()


def preview_of(station_id: str) -> dict:
    """Что известно о виде станции. Пусто — ещё не собирали, и это честно."""
    _preview_load()
    rec = _preview.get(station_id)
    if not isinstance(rec, dict):
        return {}
    if time.time() - float(rec.get("ts") or 0) > _PREVIEW_TTL:
        return {}
    return rec


def _primary_genre_ok(tag_rows: list, genre: str) -> Optional[bool]:
    """Основной ли это у артиста жанр по его собственным тегам.

    None — данных нет: жанрового тега в списке не видно, судить не о чем.
    Это НЕ «нет»: артист найден этим тегом, просто вес сюда не доехал.

    Только «верхних 2–3 по позиции» мало — замер 20.09.2026 на живой сборке:
    у Scooter techno ВТОРОЙ (rave=6, techno=4), у Moby ТРЕТИЙ вровень с
    downtempo (electronic=13, ambient=9, techno=6) — и оба играли станцию,
    будучи евродэнсом и эмбиент-рокером. Поэтому besides позиции требуем и
    magnitude: вес жанра не меньше ¾ самого тяжёлого тега. На тех же данных
    Underworld (techno 9 при top 11) и Jeff Mills (2 при 2) остаются, а
    Scooter (4 при 6), Moby, Kraftwerk (5 при 18), The Prodigy, deadmau5 —
    падают.
    """
    from ripster import genre_sources as _gs
    want = norm(genre)
    if not want:
        return None
    rows = sorted(((c, n) for c, n in tag_rows
                   if c > 0 and _gs.looks_like_genre(n)), reverse=True)
    if not rows:
        return None
    hit = next(((c, n) for c, n in rows if norm(n) == want), None)
    if hit is None:
        return None
    weight = hit[0]
    heavier = sum(1 for c, _n in rows if c > weight)
    return heavier <= 2 and weight >= 0.75 * rows[0][0]


async def artists_for_genre(genre: str, limit: int = 14) -> list[str]:
    """Кто ИГРАЕТ этот жанр — по ОСНОВНОМУ тегу артиста в MusicBrainz.

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
    «techno» ровно с весом 1. Весом 2–3 уже не отсекались (замер 20.09.2026):
    Moby и Scooter дали станцию не своей музыки — поэтому добавлена сверка
    с тегами САМОГО артиста, см. [_primary_genre_ok]. Теги едут в том же
    ответе поиска, лишних запросов к ограничителю это не просит.

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

    def confirmed(min_w: int, max_w: int) -> list:
        out = []
        for w, n, tags in rows:
            if not (min_w <= w <= max_w):
                continue
            # «Не знаю» не выбраковываем (правило 3): отбрасываем только
            # доказанно второстепенный жанр.
            if _primary_genre_ok(tags, g) is not False:
                out.append(n)
        return out

    strong = confirmed(2, 10 ** 9)
    # Порог мягкий: если сильных мало, добираем весом 1 — лучше короткий
    # честный список, чем пустой, но и мусор наверх не пускаем.
    if len(strong) < 4:
        strong += confirmed(1, 1)[: 6 - len(strong)]
    dropped = sum(1 for w, n, t in rows if w >= 1
                  and _primary_genre_ok(t, g) is False)
    print(f"[station] MB «{g}»: кандидатов {sum(1 for w, _n, _t in rows if w >= 1)},"
          f" жанр подтверждён у {len(strong)}, отвергнут как второстепенный у {dropped}",
          flush=True)
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


def credited_confirmed(credited: str, artist: str, credited_id: str = "",
                       known_ids=()) -> bool:
    """Тот же вопрос, что [credited_is], но спорные имена решает по id сервиса.

    Строкой дуэт от совместной вещи не отличить — это знал и записывался в
    честное ограничение тест: «Calyx & Teebee» для TeeBee брать НАДО,
    «Alex & Sierra» для ALEX — НЕ надо, а устроены строки одинаково.
    Различает их идентификатор, и замер 20.09.2026 показал, где его взять:
    ищет программа артиста у того же сервиса — и если под этим именем там
    НЕСКОЛЬКО разных артистов (Deezer на «ALEX» знает и «Alex», и «A L E X»),
    имя само по себе ничего не доказывает: тогда просим либо id из known_ids,
    либо полное совпадение строки исполнителя. Когда артист с таким именем
    у сервиса один («TeeBee» → 434024), частичное совпадение остаётся
    достаточным — совместный трек никуда не девается.

    Пустой known_ids — сервис айди не отдаёт или не ответил: не ужесточаем,
    «не знаю» не бывает отказом.
    """
    if not credited_is(credited, artist):
        return False
    if len(known_ids) > 1 and norm(credited) != norm(artist):
        return bool(credited_id) and str(credited_id) in known_ids
    return True


# Айди артиста у сервиса. Справочник стабильнее выдачи поиска: держим в
# процессе, сеть — ровно один запрос на пару (сервис, имя) за сборку.
_artist_ids_cache: dict = {}


async def _service_artist_ids(service: str, artist: str) -> set:
    """Идентификаторы, которыми ЭТОТ сервис знает артистов ровно с таким именем.

    Больше одного — имя многозначно, и для станции оно само по себе ничего не
    доказывает (см. [credited_confirmed]). Пусто — сервис айди не отдаёт или
    промолчал: сверку не ужесточаем.
    """
    key = (service, norm(artist))
    hit = _artist_ids_cache.get(key)
    if hit is not None:
        return hit
    from ripster.routes import discovery as _d
    ids: set = set()
    try:
        if service == "deezer":
            res = await _d._search_deezer(artist, "artist", 10)
        elif service == "qobuz":
            res = await _d._search_qobuz(artist, "artist", 10)
        elif service == "tidal":
            res = await _d._search_tidal(artist, "artist", 10)
        elif service == "yandex":
            res = await _d._search_yandex(artist, "artist", 10)
        elif service == "apple":
            res = await _d._search_apple(artist, "artist", 10, "")
        else:
            res = None
    except Exception as e:                                     # noqa: BLE001
        print(f"[station] {service}: поиск артиста «{artist}» не удался "
              f"({type(e).__name__}: {e})", flush=True)
        return set()                                           # провал не кэшируем
    for it in (res or {}).get("results") or []:
        if norm(str(it.get("artist") or it.get("title") or "")) == norm(artist):
            iid = str(it.get("id") or "")
            if iid:
                ids.add(iid)
    _artist_ids_cache[key] = ids
    return ids


async def _artist_tracks(service: str, artist: str, limit: int = 4) -> list[dict]:
    """Треки конкретного артиста у одного сервиса, со СТРОГОЙ сверкой имени.

    Без сверки поиск по имени возвращает «похожее»: у сервисов свои правила
    расширения запроса, и в станцию заезжают чужие исполнители. Имя, под
    которым сервис знает несколько разных артистов, доказательством не
    считается — там решает идентификатор, см. [credited_confirmed].
    """
    items = await _service_search(service, artist, limit * 3)
    ids = await _service_artist_ids(service, artist)
    out = []
    rejected = 0
    for it in items:
        if credited_is(it.get("artist", ""), artist):
            if credited_confirmed(it.get("artist", ""), artist,
                                  it.get("artist_id", ""), ids):
                it["from_artist"] = True
                out.append(it)
            else:
                rejected += 1
        if len(out) >= limit:
            break
    if rejected:
        print(f"[station] {service}: «{artist}» — совпадение по имени без "
              f"подтверждения айди: {rejected} трек(а) не взято", flush=True)
    return out


async def _sc_chart(slug: str, limit: int) -> list[dict]:
    """Чарт SoundCloud по жанру. Жанр здесь задан сервисом — сверять нечем."""
    if not slug:
        return []
    from ripster.routes import soundcloud as _sc
    from ripster import http_client as _HTTP
    cid = await _sc._get_client_id()
    if not cid:
        print("[station] soundcloud:chart: client_id не добыт — чарта не будет",
              flush=True)
        return []
    out: list[dict] = []
    async with _HTTP.ashared() as c:
        for kind in ("top", "trending"):
            r = await c.get("https://api-v2.soundcloud.com/charts", params={
                "kind": kind, "genre": f"soundcloud:genres:{slug}",
                "client_id": cid, "limit": limit,
            })
            if r.status_code != 200:
                # 404 на ВСЕХ валидных по форме жанрах (замер 20.09.2026:
                # techno/electronic/rock/allmusic — везде 404, страница
                # soundcloud.com/charts тоже мертва) — это чарт снят у сервиса,
                # а не наши параметры. Молча возвращать [] нельзя: источник
                # выглядит мёртвым по вине кода, когда он мёртв по вине сети.
                print(f"[station] soundcloud:chart: kind={kind} "
                      f"genre=soundcloud:genres:{slug}: ответ {r.status_code} "
                      f"— endpoint недоступен", flush=True)
                continue
            collection = (r.json() or {}).get("collection") or []
            if not collection:
                print(f"[station] soundcloud:chart: kind={kind} «{slug}»: "
                      f"200, но чарт пуст", flush=True)
            for row in collection:
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


# У сервисов доступ к сети настраивает app.py, когда ставит роуты. Станцию
# же зовут и мимо него — из теста, скрипта, фоновой догревки, — и там
# _config пуст: qobuz на пустом app_id отдаёт track/search total=0, tidal —
# «токен не настроен». Замер 20.09.2026: оба источника молча ноль в сборку,
# хотя учётки в tokens/*.yaml живые и с токеном оба отдают по 5+ треков.
# Читаем те же файлы, что читает приложение. config.yaml при этом только
# читается — не пишется.
_cfg_ensured = False


def _ensure_config() -> None:
    global _cfg_ensured
    if _cfg_ensured:
        return
    _cfg_ensured = True
    from ripster.routes import discovery as _d
    if _d._config:
        return
    from pathlib import Path
    from ripster import config_service as _cs
    try:
        base = Path(__file__).resolve().parent.parent
        cfg = _cs.load_config(base / "config.yaml", base / "tokens")
        if cfg:
            _d._config.update(cfg)
    except Exception as e:                                     # noqa: BLE001
        print(f"[station] конфиг сервисов не поднят ({type(e).__name__}): "
              f"qobuz/tidal могут молчать", flush=True)


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
    if (res or {}).get("error"):
        # Сервис ответил ПУСТО, но объяснил почему («токен не настроен»,
        # «истёк») — без этой строки ноль неотличим от «нет такой музыки».
        print(f"[station] {service}: {res['error']}", flush=True)
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
    _ensure_config()

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
        # Имя у источника треков артиста НЕ уникально: ten artists × три
        # сервиса дают десять «artist:deezer» подряд. Раньше каждое следующее
        # перезаписывало предыдущее, и расклад по источникам врал: в
        # diagnostics оставался только последний артист.
        sources.setdefault(name, 0)
        if isinstance(res, Exception):
            print(f"[station] {name}: {type(res).__name__}: {res}", flush=True)
            continue
        if not res:
            continue
        kept = []
        for it in res:
            if not it.get("curated"):
                gm = genre_matches(_id, it.get("genre") or "")
                if it.get("from_artist"):
                    # Трек артиста жанра: выкидываем только явное несовпадение.
                    if gm is False:
                        continue
                elif gm is not True:
                    # Находка ПО СЛОВУ жанра обязана жанр ДОКАЗАТЬ. Раньше «не знаю»
                    # пропускалось, а у выдачи поиска жанр почти всегда пустой —
                    # и в «Techno» ехали «Techno & Tequila», «TECHNO TAVERNE» и
                    # детский «Toddler Techno» (замер 20.09.2026): слово в названии,
                    # жанр неизвестен.
                    continue
            kept.append(it)
        sources[name] += len(kept)
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

    # ЧТО ЧЕЛОВЕК УЖЕ СКАЗАЛ СТАНЦИЯМ (station_events.py). Ровно этого не хватало,
    # чтобы станция перестала быть «случайной выборкой по жанру»: скип на пятой
    # секунде и дослушанный трек — противоположные сигналы, и оба у нас теперь
    # записаны. Всё локальное, наружу ничего не уходит.
    #
    # Сбой базы НЕ должен ронять станцию: нет истории — работаем как раньше.
    banned: set = set()
    recent: set = set()
    artist_bias: dict = {}
    try:
        from ripster import station_events as _se
        if _se._DB is not None:
            banned = _se.banned_track_keys()
            recent = _se.recent_track_keys(7)
            for name, st_ in _se.artist_stats(90).items():
                b = 1.0
                if st_.get("skip_rate") is not None:
                    # 0 скипов → ×1.3, сплошные скипы → ×0.35.
                    b *= 1.3 - 0.95 * float(st_["skip_rate"])
                b *= 1.0 + 0.25 * min(3, st_.get("downloads", 0))   # скачал — сильный плюс
                b *= 1.0 + 0.15 * min(3, st_.get("likes", 0))
                if st_.get("dislikes"):
                    b *= 0.5
                artist_bias[norm(name)] = max(0.2, min(2.5, b))
    except Exception as e:                                     # noqa: BLE001
        print(f"[station] история прослушиваний недоступна ({type(e).__name__}) — "
              f"эфир без подстройки", flush=True)

    def _ev_key(it: dict) -> str:
        isrc = norm((it.get("isrc") or "")).upper()
        if isrc:
            return f"isrc:{isrc}"
        return f"name:{(it.get('artist') or '').strip().lower()}|{(it.get('title') or '').strip().lower()}"

    # Забаненное выбрасываем всегда; «звучало на неделе» — только если после
    # этого эфир не схлопнется (правило «лучше повтор, чем пустая плитка»).
    if banned:
        items = [it for it in items if _ev_key(it) not in banned] or items
    if recent:
        fresh = [it for it in items if _ev_key(it) not in recent]
        if len(fresh) >= limit:
            items = fresh

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
        w *= artist_bias.get(norm(it.get("artist", "")), 1.0)
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

    _preview_put(_id, out[:limit])
    return {"ok": True, "id": _id, "title": title,
            "tracks": out[:limit], "sources": sources,
            "artists": artists[:10],
            "from_artists": sum(1 for t in out[:limit] if t.get("from_artist")),
            "took_ms": int((time.time() - started) * 1000)}


# ── Личная часть: что человек СЛУШАЛ и что КАЧАЛ ─────────────────────────────
#
# Два разных следа, и складывать их в один счёт нельзя. Скачивание — это
# намерение («хочу иметь»), прослушивание — факт («правда слушаю»). Скачанное
# и ни разу не тронутое говорит о вкусе меньше, чем то, что играется каждый
# день, поэтому прослушивание весит больше. Но и выбрасывать загрузки нельзя:
# у свежескачанного просто ещё не было времени накопить прослушивания.

_PLAY_WEIGHT = 2.0
_DOWNLOAD_WEIGHT = 1.0


def _split_credits(credited: str) -> list[str]:
    """«Massive Attack, Elizabeth Fraser, Trevor Jackson» → три имени.

    В истории соавторы лежат одной строкой, и без разбора Massive Attack
    делился на четыре разных «артиста»: замер по живой истории дал 73 + 23
    + 13 прослушиваний у трёх написаний одного коллектива.

    По «&» НЕ режем, и это осознанно. В строке исполнителей амперсанд почти
    всегда часть ИМЕНИ дуэта — «Calyx & TeeBee», «Above & Beyond», «Dom &
    Roland», — а соавторство пишут запятой или «feat.». Разрез по «&»
    превратил бы один коллектив в двух несуществующих артистов и разбил бы
    его счёт пополам. Обратная сторона известна и записана в тестах: по
    строке дуэт от совместной вещи не отличить, для этого нужен айди.

    Точка после «feat.» съедается вместе с разделителем — иначе в списке
    оставался артист с именем «. Baby Keem».
    """
    if not credited:
        return []
    parts = re.split(r"\s*(?:,|;|/|\bfeat(?:uring)?(?:\.|\b)|\bft(?:\.|\b)|\bvs(?:\.|\b)|\bwith\b)\s*",
                     credited, flags=re.IGNORECASE)
    return [p.strip(" .,&") for p in parts if p.strip(" .,&")]


def _history_rows(base_dir) -> tuple[list[dict], list[dict]]:
    """Скачанное и прослушанное. Отказ одного файла не отменяет второй."""
    import json
    from pathlib import Path
    base = Path(base_dir or ".")
    downloads, plays = [], []
    try:
        downloads = json.loads((base / "history.json").read_text(encoding="utf-8"))
    except Exception:                                          # noqa: BLE001
        downloads = []
    try:
        st = json.loads((base / "pairing_state.json").read_text(encoding="utf-8"))
        plays = st.get("phone_plays") or []
    except Exception:                                          # noqa: BLE001
        plays = []
    return (downloads if isinstance(downloads, list) else [],
            plays if isinstance(plays, list) else [])


def personal(base_dir=".", top: int = 18) -> dict:
    """Чем насытить вкладку: свои артисты, свои жанры, что играло недавно.

    Ничего не выдумываем: пустая история — пустые списки и честные счётчики,
    а не подставленные «рекомендации».
    """
    downloads, plays = _history_rows(base_dir)

    weight: dict[str, float] = {}
    label: dict[str, str] = {}
    plays_n: dict[str, int] = {}
    dl_n: dict[str, int] = {}

    def add(credited: str, w: float, is_play: bool) -> None:
        for name in _split_credits(credited):
            k = norm(name)
            if not k or len(k) < 2:
                continue
            weight[k] = weight.get(k, 0.0) + w
            label.setdefault(k, name)
            if is_play:
                plays_n[k] = plays_n.get(k, 0) + 1
            else:
                dl_n[k] = dl_n.get(k, 0) + 1

    for row in plays:
        add(str(row.get("artist") or ""), _PLAY_WEIGHT, True)
    for row in downloads:
        if str(row.get("status") or "") == "error":
            continue                      # неудачная загрузка о вкусе не говорит
        add(str(row.get("artist") or ""), _DOWNLOAD_WEIGHT, False)

    artists = sorted(weight.items(), key=lambda kv: -kv[1])
    top_artists = [{
        "name": label[k],
        "plays": plays_n.get(k, 0),
        "downloads": dl_n.get(k, 0),
    } for k, _w in artists[:top] if label.get(k)]

    # Жанры берём ТОЛЬКО объявленные сервисом. У большинства прослушиваний
    # жанра нет вовсе (замер: 250 из 300 пустых), и досочинять его здесь —
    # значит подменить факт догадкой. Сколько записей без жанра, говорим вслух.
    genre_hits: dict[str, int] = {}
    unknown = 0
    for row in plays:
        g = str(row.get("genre") or "").strip()
        if not g:
            unknown += 1
            continue
        genre_hits[g] = genre_hits.get(g, 0) + 1

    # Сопоставляем объявленные жанры с нашими плитками — чтобы «Electronic»
    # вело на живую станцию, а не в никуда.
    tiles = []
    for g, n in sorted(genre_hits.items(), key=lambda kv: -kv[1]):
        sid = station_for_declared(g)
        tiles.append({"genre": g, "plays": n, "station": sid or None})

    # «Играло недавно» — это СПИСОК, а не журнал событий. В сырых данных один
    # трек лежит столько раз, сколько его слушали: на живой истории Teardrop
    # шёл шесть раз подряд, и секция читалась как поломка. Схлопываем по
    # «артист + название», оставляя самое свежее и считая повторы — счёт тут
    # полезен, он и есть «часто слушаю».
    seen_recent: dict[str, dict] = {}
    for row in plays:
        k = norm(str(row.get("artist") or "")) + "|" + norm(str(row.get("title") or ""))
        if k in seen_recent:
            seen_recent[k]["times"] += 1
            continue
        seen_recent[k] = {
            "artist": row.get("artist", ""), "title": row.get("title", ""),
            "service": row.get("service", ""), "at": row.get("at", ""),
            "genre": row.get("genre", ""), "times": 1,
        }
        if len(seen_recent) >= 40:
            break
    recent = list(seen_recent.values())

    services: dict[str, int] = {}
    for row in downloads:
        sv = str(row.get("service") or "")
        if sv:
            services[sv] = services.get(sv, 0) + 1

    return {
        "artists": top_artists,
        "genres": tiles,
        "genres_unknown": unknown,
        "recent": recent,
        "services": sorted(services.items(), key=lambda kv: -kv[1]),
        "counts": {"downloads": len(downloads), "plays": len(plays)},
    }


async def by_artist(name: str, limit: int = 25, seed: Optional[int] = None) -> dict:
    """Станция вокруг одного артиста: он сам и те, кто рядом по жанру.

    Только его треки — это дискография, а не станция; поэтому берём его жанры
    в MusicBrainz и подмешиваем соседей по тегу.
    """
    who = (name or "").strip()
    if not who:
        return {"ok": False, "tracks": [], "reason": "нужно имя артиста"}

    from ripster import genre_sources as _gs
    tags = []
    try:
        tags = await _gs.musicbrainz_tags(who) or []
    except Exception:                                          # noqa: BLE001
        tags = []

    neighbours: list[str] = []
    for tg in tags[:2]:
        neighbours += await artists_for_genre(tg, limit=8)
    # Сам артист впереди, соседи следом, без повторов и без него самого.
    seen = {norm(who)}
    line = [who]
    for n in neighbours:
        if norm(n) in seen:
            continue
        seen.add(norm(n))
        line.append(n)

    tasks, names = [], []
    for a in line[:9]:
        for svc in ("deezer", "qobuz", "tidal"):
            tasks.append(_artist_tracks(svc, a, 3))
            names.append(f"artist:{svc}")
    got = await asyncio.gather(*tasks, return_exceptions=True)

    pool: list[dict] = []
    for res in got:
        if isinstance(res, Exception) or not res:
            continue
        pool.extend(res)
    if not pool:
        return {"ok": False, "tracks": [], "artist": who, "tags": tags[:3],
                "reason": "ни один сервис не дал треков этого артиста"}

    uniq: dict[str, dict] = {}
    for it in pool:
        uniq.setdefault(_key(it), it)
    items = list(uniq.values())
    rnd = random.Random(seed if seed is not None else int(time.time() * 1000))
    rnd.shuffle(items)
    # Сам артист не должен занять весь эфир: не больше трети.
    own_cap = max(2, limit // 3)
    own = [i for i in items if credited_is(i.get("artist", ""), who)][:own_cap]
    rest = [i for i in items if i not in own]
    out = (own[:2] + rest)[:limit] if own else rest[:limit]
    return {"ok": True, "artist": who, "tags": tags[:3],
            "neighbours": line[1:7], "tracks": out}


def catalog() -> list[dict]:
    """Список плиток для интерфейса, вместе с тем, что о них известно.

    `covers`/`services`/`tracks` появляются только у станций, которые уже
    собирались хоть раз. Пустые поля — это «ещё не знаем», и плитка честно
    показывает заглушку вместо выдуманной картинки.
    """
    out = []
    for st in STATIONS:
        row = {"id": st[0], "title": st[3], "curated": bool(st[1])}
        pv = preview_of(st[0])
        if pv:
            row["covers"] = pv.get("covers") or []
            row["services"] = pv.get("services") or []
            row["tracks"] = pv.get("tracks") or 0
        out.append(row)
    return out
