"""Все источники жанра, каждый со своим голосом.

Владелец 05.09.2026: «наша цель это идеальные теги, без компромиссов… берёшь
всё… модуль сам корректировал себя, дополнял либо наоборот убирал лишнее».

Поэтому здесь НЕ выбирается «лучший источник». Здесь каждый источник просто
рассказывает, что знает, а сведением занимается [ripster.genre_resolver].
Задача этого файла — добыть и очистить, не рассудить.

Что откуда берётся и чем оно по природе:

    beatport   ярлык лейбла на релизе. Точен внутри танцевальной электроники,
               ВНЕ ЕЁ ПРЯМО НЕВЕРЕН (Massive Attack → «Pop», Aretha Franklin
               → «R&B»): это полки магазина, а не жанры.
    discogs    `styles` поверх `genres`, размечает сообщество по строгим
               правилам. Шире всех; замер 8/8, включая синтвейв, которого нет
               у MusicBrainz вовсе.
    bandcamp   теги от самого артиста. Самые точные слова («dreamwave»,
               «synthwave») и самый грязный список: рядом лежат город
               («Eindhoven»), десятилетие («80s», «eighties») и настроение.
    musicbrainz теги сообщества с весом. Шумят: «melodic techno» стоял у Ланы
               Дель Рей весом 1.
    apple/deezer/yandex — широкие вёдра. Поджанра нет в принципе.

Чистка здесь ровно одна и она общая: выкинуть то, что заведомо НЕ ЖАНР —
географию, десятилетия, настроения, слова-паразиты. Всё остальное отдаётся
наверх как есть, включая противоречия: их разбирает сведение, а не добыча.
"""
from __future__ import annotations

import re
import time

import httpx

_UA = {"User-Agent": "Ripster/1.0 (+https://github.com/Raccoon-Trashpanda/Raccoon-Ripster)"}

# ── что заведомо не жанр ────────────────────────────────────────────────────
# Каждая строка добавлена по живому примеру из тегов, а не «на всякий случай».
_DECADE = re.compile(r"^(19|20)?\d0s$|^(eighties|nineties|seventies|sixties|noughties)$", re.I)
_YEAR = re.compile(r"^(19|20)\d\d$")

_NOT_GENRE = {
    # настроения и обстоятельства — их полно в тегах Bandcamp
    "chill", "chillout", "relax", "relaxing", "vibes", "mood", "moody", "sad",
    "happy", "dark", "dreamy", "melancholy", "energetic", "workout", "study",
    "focus", "sleep", "night", "summer", "winter", "driving", "road trip",
    # служебное
    # роли и занятия, а не направления: у Арво Пярта MusicBrainz первым
    # тегом ставит «composer», и он побеждал как «жанр» (замер 06.09.2026)
    "composer", "songwriter", "producer", "dj", "band", "singer", "musician",
    "music", "all", "other", "misc", "various", "va", "compilation", "album",
    "ep", "lp", "single", "remix", "remixes", "instrumental", "vocal", "live",
    "demo", "bootleg", "free", "free download", "download", "new", "2026",
    # носители и качество
    "vinyl", "cassette", "cd", "digital", "lossless", "flac", "hi-res", "320",
}

# Bandcamp торгует не только музыкой. Поиск по «Bob Marley Exodus» приводил на
# сэмпл-пак, и в жанры Марли приезжали «ableton», «psytrance», «sample pack» —
# замер цикла 2. Это не «шумный тег», а вообще не релиз, поэтому такой ответ
# отбрасывается ЦЕЛИКОМ, а не по одному тегу.
_NOT_MUSIC_RELEASE = {
    "sample pack", "samples", "sample", "template", "templates", "preset",
    "presets", "ableton", "fl studio", "logic pro", "midi", "loops", "loop kit",
    "stems", "tutorial", "course", "drum kit", "soundfont", "vst",
    # Клубные эдиты и бутлеги чужих треков, залитые ПОД ИМЕНЕМ оригинального
    # артиста. Замер цикла 6: у Кендрика Ламара так приезжали «deep house»,
    # «tech house», «weird house», у A Tribe Called Quest — «80s remix» и
    # «clubtek». Это не его музыка и не его жанр.
    "edits", "edit", "bootleg", "bootlegs", "mashup", "mashups", "rework",
    "reworks", "flip", "clubtek", "80s remix", "remix pack",
}

# Географию как жанр не берём НИКОГДА, но список стран/городов в код не
# вписываем: он бесконечен и устареет. Признак другой — теги Bandcamp
# заканчиваются местом артиста, и оно всегда ОДНО и всегда последнее.
# Поэтому у Bandcamp просто отрезаем последний тег, если он с заглавной и
# больше нигде не встречается как жанр. См. bandcamp_tags.


def _norm_name(s: str) -> str:
    """Имя для сверки: только буквы и цифры, в нижнем регистре."""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def looks_like_genre(tag: str) -> bool:
    """Похоже ли это вообще на жанр."""
    t = (tag or "").strip().lower()
    if not t or len(t) < 2 or len(t) > 40:
        return False
    if t in _NOT_GENRE:
        return False
    if _DECADE.match(t) or _YEAR.match(t):
        return False
    # Чистое число или набор знаков жанром не бывает.
    return any(ch.isalpha() for ch in t)


def clean(tags: list[str]) -> list[str]:
    """Отсеять заведомо не-жанры, сохранив порядок и убрав повторы."""
    out, seen = [], set()
    for t in tags:
        s = (t or "").strip()
        if not looks_like_genre(s):
            continue
        k = s.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(s)
    return out


# ── Bandcamp ────────────────────────────────────────────────────────────────
_bc_cache: dict[str, tuple[list[str], float]] = {}
_TTL = 30 * 24 * 3600


async def bandcamp_tags(artist: str, title: str) -> list[str]:
    """Теги артиста/релиза с Bandcamp.

    Ищем через их публичный автодополнитель, затем читаем теги со страницы.
    Последний тег отбрасываем: у Bandcamp там почти всегда место артиста
    («Eindhoven», «Berlin»), и жанром оно не является. Правило дешевле и
    честнее списка городов, который всё равно был бы неполон.
    """
    key = (artist + "|" + title).lower()
    now = time.time()
    hit = _bc_cache.get(key)
    if hit and now - hit[1] < _TTL:
        return hit[0]
    if not artist.strip():
        return []
    tags: list[str] = []
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as cl:
            # Фильтр «t» — искать ТРЕК. Без фильтра поиск по «Deepchord»
            # отдавал чужие альбомы и даже другого артиста («Deepchild»).
            r = await cl.post(
                "https://bandcamp.com/api/bcsearch_public_api/1/autocomplete_elastic",
                json={"search_text": f"{artist} {title}".strip(), "search_filter": "t",
                      "full_page": False}, headers=_UA)
            if r.status_code != 200:
                return []
            res = ((r.json().get("auto") or {}).get("results")) or []
            # Адрес лежит в `item_url_path`, а НЕ в `url`: поле `url` в этом
            # ответе всегда пустое. Первая версия читала его и молча получала
            # ноль тегов на каждом запросе — поймано замером, а не глазами.
            # Сверяем ИСПОЛНИТЕЛЯ, а не берём первый ответ. Без этого поиск
            # «Bob Marley Exodus» приводил на чужой трек, и в жанры Марли
            # приезжал «dubstep» — замер цикла 2. Первый попавшийся результат
            # это не ответ, а совпадение слов.
            # Сверка ТОЧНАЯ, а не по вхождению. Подстрочная пропускала
            # страницы вида «Kendrick Lamar Type Beat» — их на Bandcamp тысячи,
            # и в жанры Кендрика приезжал «deep house», а к A Tribe Called
            # Quest — «80s electronic» (замер цикла 5). Имя артиста должно
            # совпасть целиком.
            want = _norm_name(artist)
            url = ""
            for x in res:
                band = _norm_name(str(x.get("band_name") or ""))
                if band and want and band == want:
                    u = str(x.get("item_url_path") or x.get("item_url_root") or "")
                    if u.startswith("http"):
                        url = u
                        break
            if not url:
                return []
            page = await cl.get(url, headers=_UA)
            raw = re.findall(r'class="tag"[^>]*>\s*([^<]+?)\s*<', page.text)
    except Exception:
        return []                       # «не знаю» — в кэш не кладём

    if raw:
        low = {t.strip().lower() for t in raw}
        if low & _NOT_MUSIC_RELEASE:
            # Это не релиз, а товар для продюсеров. Ни один его тег не годится.
            _bc_cache[key] = ([], now)
            return []
        # Последний тег — место. Отрезаем его до общей чистки, чтобы не
        # полагаться на список городов.
        raw = raw[:-1] if len(raw) > 1 else raw
        tags = clean(raw)
    _bc_cache[key] = (tags, now)
    return tags


# ── MusicBrainz ─────────────────────────────────────────────────────────────
_mb_cache: dict[str, tuple[list[str], float]] = {}
_mb_last = 0.0


async def musicbrainz_get(params: dict, what: str = "запрос"):
    """Один вежливый запрос к MusicBrainz. `None` — не ответили.

    ОДИН НА ПРОЦЕСС, и это не педантизм. У MusicBrainz правило «не чаще
    запроса в секунду» на клиента; два независимых клиента внутри одной
    программы сбивают ограничитель ДРУГ ДРУГУ и получают 503 оба. Ровно это и
    случилось 06.09.2026, когда станции завели себе собственный запрос мимо
    этого троттлера: минуту назад те же теги отдавались с кодом 200, а из
    приложения пошли сплошные 503.

    503 у них значит «слишком часто, повтори», а не «ничего нет»: молча
    превращать его в пустой ответ нельзя — источник тогда пропадает на
    случайных запросах, и общий вывод прыгает.
    """
    global _mb_last
    import asyncio
    for attempt in range(3):
        try:
            wait = 1.1 - (time.time() - _mb_last)
            if wait > 0:
                await asyncio.sleep(wait)
            _mb_last = time.time()
            async with httpx.AsyncClient(timeout=20) as cl:
                r = await cl.get("https://musicbrainz.org/ws/2/artist",
                                 params=params, headers=_UA)
            if r.status_code == 200:
                return r
            if r.status_code not in (429, 503):
                print(f"[musicbrainz] {what}: ответ {r.status_code}", flush=True)
                return None
        except Exception as e:                                 # noqa: BLE001
            if attempt == 2:
                print(f"[musicbrainz] {what}: связь не удалась ({type(e).__name__})",
                      flush=True)
                return None
        await asyncio.sleep(1.5 * (attempt + 1))
    print(f"[musicbrainz] {what}: ограничитель не отпустил за три попытки", flush=True)
    return None


async def musicbrainz_artists_by_tag(tag: str, limit: int = 40) -> list[tuple]:
    """Артисты жанра: тройки «вес тега — имя — теги артиста», от сильного к слабому.

    Вес тега, а не `score` выдачи: score — релевантность СТРОКИ запросу, по
    нему в «melodic techno» лезла Лана Дель Рей.

    Теги артиста — пары «вес — имя», от тяжёлых к лёгким — едут В ТОМ ЖЕ
    ответе поиска (замер 20.09.2026): отвечать на вопрос «основной ли это у
    артиста жанр» можно без единого дополнительного запроса, что при их
    «не чаще запроса в секунду» дороже экономии. Разбор весов решает не этот
    модуль — здесь только добыча.
    """
    t = (tag or "").strip()
    if not t:
        return []
    r = await musicbrainz_get({"query": f'tag:"{t}"', "fmt": "json", "limit": limit},
                              f"артисты жанра «{t}»")
    if r is None:
        return []
    want = t.lower()
    out: list[tuple] = []
    try:
        for a in (r.json() or {}).get("artists") or []:
            name = (a.get("name") or "").strip()
            if not name:
                continue
            weight = 0
            tags: list[tuple[int, str]] = []
            for tg in (a.get("tags") or []):
                n = str(tg.get("name") or "")
                try:
                    c = int(tg.get("count") or 0)
                except (TypeError, ValueError):
                    c = 0
                tags.append((c, n))
                if n.lower() == want:
                    weight = c
            tags.sort(key=lambda x: -x[0])
            out.append((weight, name, tags))
    except Exception:                                          # noqa: BLE001
        return []
    out.sort(key=lambda x: -x[0])
    return out


async def musicbrainz_tags(artist: str) -> list[str]:
    """Теги артиста в MusicBrainz, от сильного к слабому.

    Соблюдаем их правило «не чаще запроса в секунду»: иначе они отвечают
    ПУСТЫМ телом, а не ошибкой, и это легко принять за «тегов нет».
    """
    global _mb_last
    key = artist.lower()
    now = time.time()
    hit = _mb_cache.get(key)
    if hit and now - hit[1] < _TTL:
        return hit[0]
    if not artist.strip():
        return []
    import asyncio
    # 503 у них означает «слишком часто, повтори», а не «тегов нет». Пока это
    # молча превращалось в пустой список, источник выпадал на случайных
    # артистах — и ответ всего модуля прыгал: в замере 06.09.2026 MusicBrainz
    # не ответил на шести из сорока, и на Кендрике Ламаре победил «deep house»
    # с чужой страницы, потому что возразить стало некому.
    r = None
    for attempt in range(3):
        try:
            wait = 1.1 - (time.time() - _mb_last)
            if wait > 0:
                await asyncio.sleep(wait)
            _mb_last = time.time()
            async with httpx.AsyncClient(timeout=20) as cl:
                r = await cl.get("https://musicbrainz.org/ws/2/artist",
                                 params={"query": f'artist:"{artist}"', "fmt": "json",
                                         "limit": 1}, headers=_UA)
            if r.status_code == 200:
                break
            if r.status_code not in (429, 503):
                print(f"[musicbrainz] {artist}: ответ {r.status_code} — "
                      f"тегов не будет", flush=True)
                return []
        except Exception as e:
            if attempt == 2:
                print(f"[musicbrainz] {artist}: связь не удалась "
                      f"({type(e).__name__}) — тегов не будет", flush=True)
                return []
        await asyncio.sleep(1.5 * (attempt + 1))
    else:
        print(f"[musicbrainz] {artist}: ограничитель не отпустил за три "
              f"попытки — тегов не будет", flush=True)
        return []

    try:
        arr = r.json().get("artists") or []
        if not arr:
            return []
        tags = sorted(
            ((int(t.get("count") or 0), str(t.get("name") or "")) for t in (arr[0].get("tags") or [])),
            reverse=True,
        )
        out = clean([n for _, n in tags])
    except Exception:
        return []
    _mb_cache[key] = (out, now)
    return out
