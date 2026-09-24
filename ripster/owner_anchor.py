"""Якорь владельца: чем артист является для того, кто подписан.

Жалоба владельца 23.09.2026: «по-прежнему Соломон Грей вижу в релиз радаре,
который не тот Соломон вообще»; «Bop то же самое, Aruna то же самое будет, да
много кто так будет, нужно не артиста лечить а алгоритм и правила».

Причина, по которой прежний алгоритм не мог это поймать: у витрины НЕТ честного
профиля подписки. Страницы Apple, Deezer и Qobuz лепят однофамильцев в один id,
поэтому профиль личности, собранный из подтверждённых свидетелей, — ОБЪЕДИНЕНИЕ
людей: у Aruna в жанрах подписки живут и trance, и telugu-devotional, и
gangsta rap. Мерить карточку об такое объединение — значит никогда не увидеть
чужого человека.

Якорь — единственное доказательство, которое не склеивается: то, что владелец
ДЕЛАЛ со своей фонотекой.

  downloads   `ripster_stats.db` — он скачал «Start A Fire (Remixes)» Aruna и
              «Rumination» BOP: вот это и есть его артист;
  rel-favs    звёздочка на карточке радара (`rel_favorites.json`);
  stations    `stations.db:station_events` — что реально звучало.

Каждый якорь несёт лейблы и жанры своих релизов; где локальной строки нет,
метаданные добираются ОДНИМ запросом к публичному каталогу и ложатся на диск
(`identity_anchor_cache.json`) — повторных запросов не бывает, а пропажа сети
не стирает уже посчитанное.

Признак, а не список имён: здесь нет ни одной подписки и ни одного артиста по
имени. Есть карта жанровых семей и список совместимостей — данные, по которым
решают ВСЕ подписки сразу (`GENRE_FAMILIES`, `FAMILY_COMPAT`).
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Callable, Optional

from . import compilations as _comps
from .artist_xref import norm
from .genre_resolver import canon, is_bucket

# ── Жанровые семьи ────────────────────────────────────────────────────────────
# Витрины называют одну и ту же музыку по-разному и путают жанры с форматами,
# поэтому сравнение идёт НЕ по строке жанра, а по СЕМЬЕ. Порядок значим:
# совпадение ищется сверху вниз, и узкое имя стоит раньше широкого
# («drum & bass» — это dnb, а не «bass» из электроники; «folk metal» — металл,
# а не фолк). Паттерны сравниваются с `canon()`: «Trip Hop», «trip-hop» и
# «Trip Hop» сводятся к «trihop».
GENRE_FAMILIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    # Сон, медитация и «целительные частоты»: самая частая подделка под
    # электронщика с тем же именем (Aruna, 23.09.2026 — «963 Hz …»).
    ("meditation", ("meditation", "sleep", "dreaming", "newage", "new age",
                    "healing", "relaxation", "relax", "binaural", "mindfulness",
                    "soundbath", "lullaby", "mantra", "spa", "sound therapy",
                    "breathwork", "calm")),
    # Региональная музыка: витрины лепят её к однофамильцам-электронщикам
    # (telugu-синглы Aruna). Индийские ветки — здесь же, как и просит владелец.
    # СТОИТ ВЫШЕ christian намеренно: жанр Apple «Devotional & Spiritual» —
    # это индийское бхакти, а не госпел; кто прочитал его первым, тот и правит.
    # Слово «spiritual» из christian этому не мешает: «Gospel & Spiritual»
    # ни одним узором из world не схватывается.
    ("world", ("world", "indian", "telugu", "tamil", "malayalam", "kannada",
               "bengali", "hindi", "bollywood", "devotional", "bhajan",
               "qawwali", "punjabi", "bhangra", "nepali", "tibetan", "african",
               "celtic", "arabic", "persian", "klezmer", "oriental", "regional",
               "turkish", "greek", "balkan", "jewish", "sufi", "afrobeat",
               "caribbean", "native american")),
    ("christian", ("gospel", "christian", "worship", "hymn", "praise", "ccm",
                   "spiritual", "jesus")),
    ("latin", ("latin", "latino", "salsa", "bachata", "merengue", "cumbia",
               "reggaeton", "bossa", "samba", "forro", "tropical")),
    # dnb ВЫШЕ reggae намеренно: «dub» — подстрока «dubstep», а dubstep — это
    # не регги, и для танцевальной подписки он родственен электронике.
    ("dnb", ("drumbass", "drumandbass", "drumnbass", "dnb", "jungle",
             "breakbeat", "breaks",
             "breakcore", "neurofunk", "dubstep", "drumstep", "riddim")),
    ("reggae", ("reggae", "ska", "dancehall", "dub")),
    ("kids", ("children", "kids", "nursery", "disney")),
    ("soundtrack", ("soundtrack", "score", "film", "cinema", "movie", "tv",
                    "video game", "game", "anime", "trailer", "broadway",
                    "musical", "adapted from")),
    ("classical", ("classical", "orchestral", "symphony", "chamber", "opera",
                   "concerto", "baroque", "choral", "neoclassical")),
    ("jazz", ("jazz", "swing", "bebop", "big band", "standards")),
    ("blues", ("blues",)),
    ("hiphop", ("hiphop", "hip hop", "rap", "trap", "grime", "boombap",
                "turntablism", "old school")),
    ("rnb", ("rnb", "r&b", "rhythm and blues", "soul", "funk", "motown")),
    ("metal", ("metal", "djent", "thrash", "deathcore", "hardcore")),
    ("rock", ("rock", "indie", "alternative", "punk", "grunge", "shoegaze",
              "britpop", "psychedelic")),
    ("country_folk", ("country", "folk", "bluegrass", "americana",
                      "singer songwriter", "songwriter")),
    ("ambient", ("ambient", "downtempo", "trihop", "trip hop", "idm",
                 "soundscape", "drone", "field recording", "chillout",
                 "chill out")),
    # Танцевальная электроника целиком. «progressive» сюда не попало намеренно:
    # с ним prog-rock становился электроникой.
    ("electronic", ("dance", "electro", "house", "techno", "trance", "edm",
                    "club", "rave", "minimal", "hardstyle", "synth", "disco",
                    "bass", "eurodance", "hi nrg", "breakbeat")),
    ("pop", ("pop", "kpop", "europop", "adult contemporary")),
)

# Паттерны сравниваются с жанром ПОСЛЕ `canon()`, а canon выбрасывает пробелы и
# знаки препинания: значит и сами паттерны надо сводить к canon. Иначе «hip hop»
# никогда не совпало бы с «Hip Hop» (canon — «hiphop»), и треть таблицы была бы
# мёртвым грузом.
_FAM_PATS: tuple[tuple[str, tuple[str, ...]], ...] = tuple(
    (fam, tuple(canon(p) for p in pats)) for fam, pats in GENRE_FAMILIES)

# Те же вёдра, которых нет в `genre_resolver.BUCKETS`, но которые витрина
# пишет в `primaryGenreName` ОТДЕЛЬНОЙ полкой: «Singer/Songwriter» у Apple —
# корзина для всего гитарно-певчего, от Канье до какой-нибудь нью-ейдж певицы.
# Замер 23.09: якорь Ólafur Arnalds сложился из скачанного «Dawn - Single»
# (ведро «Singer/Songwriter») и «A Dawning» (ведро «Pop») — из первого вылезла
# семья `country_folk`, и пять НОВЫХ релизов того же Ólafur (жанр «Classical
# Crossover») правило спрятало как «чужих». Ведро опасно вдвойне: оно портит не
# только карточку, но и якорь, против которого карточка судится.
COARSE_GENRES: frozenset = frozenset({
    canon("Singer/Songwriter"), canon("Singer Songwriter"), canon("Songwriter"),
    canon("Album-Oriented Rock"), canon("AOR"), canon("Adult Contemporary"),
    canon("Easy Listening"), canon("Dance & Electronic"),
})

# Формат, а не жанр: по этим словам личность не судят. «DJ-Mixes» у Apple —
# полка, на которой лежит сет кого угодно.
GENRE_IGNORE: tuple[str, ...] = (
    "djmix", "djmixes", "dj mix", "mixes", "compilation", "various",
    "original sound", "miscellaneous",
)

# Совместимости. ПАРНЫЕ и НЕ транзитивные: электроника родня dnb, dnb родня
# эмбиенту — но рэп от транса родство не получает (иначе подписка BOP на dnb
# пропускала бы хип-хоп, с чего и началась жалоба).
FAMILY_COMPAT: frozenset = frozenset(
    frozenset(p) for p in (
        ("electronic", "dnb"),
        ("electronic", "ambient"),
        ("electronic", "soundtrack"),      # киберпанк-саундтрек — электроника
        ("ambient", "soundtrack"),
        ("ambient", "meditation"),
        ("ambient", "classical"),
        ("classical", "soundtrack"),
        ("hiphop", "rnb"),
        ("rnb", "pop"),
        ("metal", "rock"),
        ("rock", "pop"),
        ("jazz", "blues"),
        ("world", "latin"),
        ("country_folk", "rock"),
    ))

# Apple пишет составной жанр одной строкой: «Jungle/Drum 'n' Bass»,
# «Hip Hop/Rap». Режем только по «/» и «,» — по «&» нельзя, там живёт
# «drum & bass», которое режется на бессмысленные «drum» и «bass».
_GENRE_SPLIT_RE = re.compile(r"[/,]")

# ── Функциональная музыка: единственный признак, которого нет в жанре ─────────
# Сон, медитация и «целительные частоты» — то, чем витшины затирают электронщика
# с тем же именем (Aruna, 23.09.2026: «963 Hz …», «Night Sleep Alpha 8 Hz»,
# «Deep Sleep Soundscape»). Apple складывает это в ведро «Ambient», и по жанру
# такая карточка НЕОтличима от эмбиента, который владелец качает: жанр сломан не
# ошибкой, а тем, что для витрины это и правда «атмосферная музыка». Разводит их
# только заголовок — он кричит о назначении записи.
#
# Узор СПЕЦИАЛЬНЫЙ, а не «слова про сон»: «Sleep» — обычное имя для транса и
# хауса («Sleepwalking», «Sleepless»), поэтому одиночного слова мало. Нужен
# маркер функции: частота в герцах, бинауральные ритмы, сольфеджо, чакровая/
# рейки-терминология, «music for …», «for sleeping/relaxation».
#
# Замер 23.09: у Aruna в ленте остались «Night Rest Waves» и «Where Sleep
# Begins» — лежали в ведре «Ambient», а маркера не было: «Sleep» в них стоит не
# в просьбе «for sleep», а в имени самой функции. Поэтому добавлены связки
# «calm/deep … sleep», «sleep soundscape/waves/…», «sleep begins», «night rest»,
# «rest waves». Обычные имена по-прежнему мимо: «Asleep in the Garden of
# Infernal Stars», «Don't Want To Sleep Alone», «Sleep In It», «Dream of You».
_FUNC_TITLE_RE = re.compile(
    r"\d+\s*h(?:z|erz|ertz)\b"                # «963 Hz», «8 hz», «432 Hertz»
    r"|\bbinaural\b|\bsolfeggio\b|\bfrequency\s+healing\b|\bhealing\s+frequenc"
    r"|\bchakra\b|\breiki\b|\bsound\s?bath\b|\bsound\s?therapy\b"
    r"|\bmusic\s+for\s+\w"                    # «Music for Sleep», «for Deep Focus»
    r"|\bfor\s+(?:deep\s+|profound\s+)?(?:sleep|sleeping|relax|relaxing|"
    r"relaxation|meditat\w*|breath\w*|yoga|study|focus|calm|healing|manifest\w*|"
    r"stress\s*relief|mindfulness|spa|dream\w*|reiki|soul|body)\b"
    r"|\blullaby\b|\bbreathwork\b|\bmindfulness\b|\bstress\s*relief\b"
    r"|\bbrainwave\b|\bisochronic\b"
    r"|\b(?:deep|calm|profound|instant|sound|gentle|peaceful|endless)"
    r"\s+sleep\b"                             # «Deep Sleep …», «Calm Sleep»
    r"|\bsleep\s+(?:soundscape|scapes?|waves?|tones?|music|therapy|meditation"
    r"|hypnosi\w*|journey|state|zone|station|land|bath|frequency|begins?)\b"
    r"|\bnight\s+rest\b|\brest\s+waves?\b",    # «Sleep Soundscape», «Night Rest Waves»
    re.IGNORECASE)
# Ведра, а не семейства: «Ambient» и «Pop» говорят о карточке слишком мало,
# чтобы по одному жанру признавать её своей.
#
# «Electronic» сюда НЕ входит намеренно: семья «electronic» — крышка, под
# которую family_of прячет транс, dnb и хаус. Считать её ведром — значит
# замещать медитацией «963 Hz» с точным жанром «Trance», и транс-подписка
# теряла бы свои же релизы. Слово «Electronic» как жанр отсекает
# `_genre_is_shallow` через genre_resolver.BUCKETS.
BUCKET_FAMILIES: frozenset = frozenset({"ambient", "pop"})


def _genre_is_shallow(genre: str) -> bool:
    """Сказал ли жанр что-то о ЛИЧНОСТИ, или только «музыка вообще»?

    Судим по СТРОКЕ жанра, а не по выпавшей семье: семья «electronic»
    рождается и из точного «Trance», и из пустого «Electronic», и различить
    их по семье уже нельзя. Пустой ответ family_of (формат вроде «DJ-Mixes»,
    незнакомое слово) — тоже молчание. Сюда же попадают полки витрины, которых
    нет в `genre_resolver.BUCKETS` («Singer/Songwriter»): они широки ровно
    настолько же и портят не только карточку, но и якорь.
    """
    k = canon(genre)
    if not k or k in COARSE_GENRES:
        return True
    f = family_of(genre)
    return is_bucket(genre) or not f or f in BUCKET_FAMILIES


def title_family(title: str) -> str:
    """Семья по заголовку — или "", если заголовок молчит о назначении записи."""
    return "meditation" if _FUNC_TITLE_RE.search(str(title or "")) else ""


# Разделители составного кредита: «Old Jim, ARTY, Alexandra Stan & Ceres»,
# «A feat. B», «A x B». Нормализация `artist_xref.norm` склеивает «&» в «and»,
# поэтому режем СЫРУЮ строку до нормализации.
_SPLIT_RE = re.compile(
    r"\s*(?:[,;/&+]|\bvs\.?\b|\bft\.?\b|\bfeat\.?\b|\bfeaturing\b|\bx\b|"
    r"\band\b|\bwith\b|\bperforming\b)\s*", re.IGNORECASE)

# Счётчики исходов правила. Живут в памяти и пересобираются на каждом чтении
# ленты: «сколько карточек правило не тронуло, потому что судить было нечем» —
# цифра, которую нельзя прятать, иначе «скрыто 12» выглядит победой.
_COUNTS = {"anchor_show": 0, "anchor_hide": 0, "anchor_unknown": 0,
           "no_anchor": 0, "stitch_dropped": 0, "name_claim_dropped": 0}

_MAX_PER_SOURCE = 15
# Витрины, которых можно спросить лейбл/жанр БЕЗ хозяина: публичный Deezer,
# публичный iTunes-lookup и Qobuz (у него свой app-id в коде). Spotify здесь
# нет намеренно — якорь не должен зависеть от того, жив ли чужой аккаунт.
# Tidal/Beatport/Yandex тоже: без хозяйского токена это гарантированный
# отказ, а 300 заведомо пустых запросов — не «один запрос на релиз».
_ENRICH_SERVICES = ("apple", "deezer", "qobuz")
# Отказ тоже ответ: без него неподписанный/закрытый релиз спрашивают заново
# на каждом проходе, и «один запрос на релиз» превращается в вечный.
_MISS_RETRY_S = 7 * 24 * 3600


def family_of(genre: str) -> str:
    """Семья жанра или "" — если сказать нечем (формат, ведро, неизвестное)."""
    k = canon(genre)
    if not k:
        return ""
    if any(canon(ig) in k for ig in GENRE_IGNORE):
        return ""
    for fam, pats in _FAM_PATS:
        if any(p in k for p in pats):
            return fam
    return ""


def compatible(a: str, b: str) -> bool:
    """Родня ли две семьи (по явным парам; транзитивности нет намеренно)."""
    return bool(a) and a == b or frozenset((a, b)) in FAMILY_COMPAT


_MIX_KINDS: frozenset = frozenset(
    {"mix", "mixes", "djmix", "djmixes", "djset", "session", "liveset",
     "mixtape"})

_MIX_TITLE_RE = re.compile(
    r"\bdj[ \-]?mix(?:es)?\b|\bdj[ \-]?set\b|\bmixtape\b|\bmix\b"
    # «Mixed» — маркер полки ТОЛЬКО в скобках или с «by»: иначе под правило
    # попадали бы обычные имена («Mixed Emotions», «Mixed Feelings»).
    r"|\((?:\s*(?:dj\s+)?mixed(?:\s+by[^)]*)?)\)|\bmixed\s+by\b"
    r"|\bcompilation\b|\bsampler\b|\bpresents\b|\bcurated by\b|\bselected by\b"
    r"|\bcompiled by\b|\bradio[:\s]", re.IGNORECASE)


def is_mix(rel) -> bool:
    """Диджейский микс, сессия или сборник-толпа: полка витрины, а не работа артиста.

    Радар помечает такие карточки `group`/`type` = «mix» (`routes/radar.py`),
    и жанр у них — про сет, поставленный кем-то ещё. Личность по ним не судят.
    На складах, где поля типа нет (Apple-лента кладёт микс «альбомом»),
    маркером служит сам заголовок: «… (DJ Mix)» — это витринная метка полки.

    Замер 23.09: «Stamma Presents, Vol. 2: World Dancehall» и «Store Run Radio:
    Staff Picks 001» прятались по жанру, хотя это чужие сеты и радиошоу. Одного
    «(DJ Mix)» мало — нужны «Mixed», «(Mix)», «presents», «curated/selected/
    compiled by», «radio:», «compilation/sampler».

    Типа «compilation» и «Various Artists» тут НЕТ намеренно: «Greatest Hits»
    артиста — его собственная работа, а VA-сборник с трэком некоего Aruna — это
    как раз тот однофамилец, ради которого правило и заведено. Смесь «компиляции»
    ловит заголовок («FSOE Summer Compilation», «… Sampler»).
    """
    r = rel or {}
    for k in ("group", "type", "kind", "album_type"):
        if canon(str(r.get(k) or "")) in _MIX_KINDS:
            return True
    return bool(_MIX_TITLE_RE.search(str(r.get("title") or "")))


def label_foreign(lbl: str, home_labels, artist: str = "", raw: str = "") -> bool:
    """Чужой ли лейбл — с учётом того, как витрины называют один и тот же бренд.

    Лейбл — единственный довод, который прячет карточку, когда жанр молчит.
    Ошибиться в нём дороже, чем молчать, поэтому «чужой» тут значит «никак не
    свой»: филиал пишется через мать («Hospital» ↔ «Hospital Records»), а свой
    imprint артист называет своим именем («Anjunadeep» ↔ «Anjunadeep
    Explorations»). Без этой оговорки правило прятало «Anjunadeep Explorations
    Vol. 2» у подписки на Anjunadeep.

    `lbl` — ключ из `label_key`, `raw` — СТРОКА витрины: ключ вырезает имя
    артиста (это его законное дело в кластеризации), а именно имя и говорит,
    что imprint собственный.
    """
    n = norm(lbl)
    if not n:
        return False
    own = norm(artist)
    if len(own) > 3 and own in norm(raw or lbl):
        return False
    home = {norm(str(h or "")) for h in (home_labels or ()) if h}
    if not home:
        return False
    if n in home:
        return False
    for h in home:
        if len(h) < 4 or len(n) < 4:
            continue
        if h.startswith(n) or n.startswith(h):
            return False
    return True


def families_of(rel) -> set:
    """Семьи релиза. Жанр — главное; заголовок вмешивается только там, где
    жанры не сказали НИЧЕГО внятного (молчат или ведра): иначе «963 Hz …»
    из ведра «Ambient» не отличить от эмбиента, который владелец качает.

    Вмешательство ЗАМЕЩАЕТ, а не дополняет: карточка про сон — это про сон, и
    добавлять ей «ambient» значило бы пропускать её домой к эмбиент-подписке.
    """
    out, shallow = set(), True
    for g in genres_of(rel):
        # Сначала СТРКОЙ ЦЕЛИКОМ: «Dance & Electronic» — одна полка, а не два
        # точных жанра, и порезав её по «&» мы бы прочитали «dance» как довод.
        if _genre_is_shallow(g):
            continue
        for part in _GENRE_SPLIT_RE.split(g):
            if not part.strip():
                continue
            # Ведро («Pop», «Dance», «Ambient») не говорит НИЧЕГО: из него нельзя
            # вывести ни «свой», ни «чужой» — вердикт обязан быть «нечем судить».
            # Замер 23.09: сингл Skrillex «911» с жанром-ведром «Pop» правила
            # прятало именно как «чужой жанр».
            if _genre_is_shallow(part):
                continue
            shallow = False
            f = family_of(part)
            if f:
                out.add(f)
    title = rel.get("title") if isinstance(rel, dict) else ""
    tf = title_family(title)
    if tf and shallow:
        out = {tf}
    return out


def genres_of(rel) -> list:
    """Жанры карточки в виде списка строк — витрины дают то скаляр, то массив."""
    if isinstance(rel, str):
        return [rel]
    gs = (rel or {}).get("genres")
    if isinstance(gs, str):
        gs = [gs]
    if not gs:
        g = (rel or {}).get("genre")
        gs = [g] if g else []
    return [str(x) for x in gs if x]


def person_keys(credit: str) -> list:
    """Имена, которых касается строка кредитования, — нормализованные.

    «Old Jim, ARTY, Alexandra Stan & Ceres» — это и про ARTY тоже: скачав
    такое, владелец подтвердил именно ARTY. Целое значение возвращается всегда:
    дуэт «Lane 8 & Monoem» подписан целиком, и разрезать его — значит потерять
    якорь.
    """
    raw = str(credit or "").strip()
    if not raw:
        return []
    whole = norm(raw)
    out = [whole] if whole else []
    for part in _SPLIT_RE.split(raw):
        k = norm(part)
        if k and k != whole and len(k) > 2 and not _comps.is_various_artists(k):
            out.append(k)
    return out


def note(kind: str) -> None:
    if kind in _COUNTS:
        _COUNTS[kind] += 1


def counters() -> dict:
    return dict(_COUNTS)


def reset_counts() -> None:
    for k in _COUNTS:
        _COUNTS[k] = 0


# ── Реестр якорей ─────────────────────────────────────────────────────────────
# Строится один раз на изменение источников (mtime) и живёт в памяти: ленту
# радара открывают часто, а прочитать 4 тысячи строк локальной базы — доли
# миллисекунды.

_BASE: Optional[Path] = None
_REG: dict = {"key": None, "data": {}}


def configure(base_dir) -> None:
    """Каталог, где лежат `ripster_stats.db`, `rel_favorites.json`, `stations.db`."""
    global _BASE
    if base_dir is not None:
        _BASE = Path(base_dir)


def cache_file() -> Path:
    return (_BASE or Path(".")) / "identity_anchor_cache.json"


def _files() -> tuple:
    base = _BASE or Path(".")
    return (base / "ripster_stats.db", base / "rel_favorites.json",
            base / "stations.db")


def _sources_key() -> tuple:
    out = []
    for f in _files():
        try:
            st = f.stat()
            out.append((f.name, st.st_mtime_ns, st.st_size))
        except OSError:
            out.append((f.name, 0, 0))
    return tuple(out)


def _new_anchor(name: str) -> dict:
    return {"name": name, "releases": [], "labels": set(), "genres": set(),
            "families": set(), "sources": {}, "n": 0}


def _add_release(a: dict, rel: dict) -> None:
    """Один релиз якоря: сразу пересчитываем его лейблы, жанры и семьи."""
    from .artist_identity import label_key          # поздно: иначе цикл импортов

    key = norm(rel.get("title") or "")
    if not key or any(norm(r.get("title") or "") == key for r in a["releases"]):
        return
    a["releases"].append(rel)
    lbl = label_key(str(rel.get("label") or ""), str(a["name"] or ""))
    if rel.get("label") and lbl:
        a["labels"].add(lbl)
    for g in genres_of(rel):
        k = norm(g)
        if k:
            a["genres"].add(k)
    # Семьи считаем тем же способом, что и для карточек: иначе якорь и подпись
    # судятся по разным правилам («Jungle/Drum 'n' Bass» — две семьи в строке).
    a["families"] |= families_of(rel)


def _read_downloads(base: Path) -> list:
    """Что владелец реально скачал (status='done'), от новых к старым."""
    f = base / "ripster_stats.db"
    if not f.exists():
        return []
    try:
        con = sqlite3.connect(f"file:{f.as_posix()}?mode=ro", uri=True)
    except Exception:                                          # noqa: BLE001
        return []
    try:
        cur = con.execute(
            "SELECT service, artist, album, url FROM downloads "
            "WHERE status='done' AND artist<>'' AND album<>'' "
            "ORDER BY ts DESC LIMIT 40000")
        return [{"service": r[0] or "", "artist": r[1] or "", "title": r[2] or "",
                 "url": r[3] or "", "source": "downloads"} for r in cur.fetchall()]
    except Exception:                                          # noqa: BLE001
        return []
    finally:
        con.close()


def _read_favs(base: Path) -> list:
    f = base / "rel_favorites.json"
    if not f.exists():
        return []
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
        items = d if isinstance(d, list) else (d.get("items") or [])
    except Exception:                                          # noqa: BLE001
        return []
    return [{"service": str(r.get("service") or ""),
             "artist": str(r.get("artist") or ""),
             "title": str(r.get("title") or ""),
             "label": str(r.get("label") or ""),
             "genre": str(r.get("genre") or ""),
             "url": str(r.get("url") or ""), "source": "rel-favs"}
            for r in items if isinstance(r, dict) and r.get("title")]


def _read_stations(base: Path) -> list:
    """Что звучало в станциях. Доказательство вкуса слабое (в станции может
    играть и однофамилец), поэтому оно и в ядре списка не первым идёт."""
    f = base / "stations.db"
    if not f.exists():
        return []
    try:
        con = sqlite3.connect(f"file:{f.as_posix()}?mode=ro", uri=True)
    except Exception:                                          # noqa: BLE001
        return []
    try:
        cur = con.execute(
            "SELECT DISTINCT artist, title FROM station_events "
            "WHERE event='track_started' AND artist<>'' AND title<>'' LIMIT 5000")
        return [{"service": "", "artist": r[0], "title": r[1], "url": "",
                 "source": "stations"} for r in cur.fetchall()]
    except Exception:                                          # noqa: BLE001
        return []
    finally:
        con.close()


def build(base: Path) -> dict:
    """{нормализованное имя: якорь} по всем трём источнику владельца."""
    cache = _cache_load()
    rows: list = []
    for read in (_read_favs, _read_downloads, _read_stations):
        try:
            rows.extend(read(base))
        except Exception as e:                                 # noqa: BLE001
            print(f"[anchor] {read.__name__}: {e}", flush=True)

    out: dict[str, dict] = {}
    for rel in rows:
        for key in person_keys(rel.get("artist", "")):
            a = out.setdefault(key, _new_anchor(key))
            src = rel.get("source") or "?"
            if len(a["sources"].get(src) or []) >= _MAX_PER_SOURCE:
                continue
            got = dict(rel)
            _from_cache(got, cache)
            a["sources"].setdefault(src, []).append(got)
    for name, a in out.items():
        for lst in a["sources"].values():
            for rel in lst:
                _add_release(a, rel)
        a["n"] = len(a["releases"])
    return {k: v for k, v in out.items() if v["releases"]}


def _meta_key(rel: dict) -> str:
    return f"{rel.get('service') or ''}|{rel.get('url') or ''}"


def _from_cache(rel: dict, cache: dict) -> None:
    """Лейбл/жанр с диска — молча, без сети."""
    _apply_cache([rel], cache, "meta")


def registry() -> dict:
    """Якоря по нормализованному имени; пересборка, когда тронуты источники."""
    if _BASE is None:
        return {}
    key = _sources_key()
    if _REG["key"] != key or not _REG["data"]:
        _REG["data"] = build(_BASE)
        _REG["key"] = key
    return _REG["data"]


def invalidate() -> None:
    """Забыть собранный реестр (после обогащения или в тесте)."""
    _REG["key"] = None


def for_name(name: str) -> Optional[dict]:
    return registry().get(norm(name))


def for_entry(entry: dict) -> Optional[dict]:
    """Якорь подписки по её имени. Пустой якорь — None: без доказательства
    цензуры не бывает."""
    a = for_name(str((entry or {}).get("name") or ""))
    return a if a and a.get("releases") else None


# ── Кэш публичных метаданных ─────────────────────────────────────────────────

def _cache_load() -> dict:
    try:
        d = json.loads(cache_file().read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:                                          # noqa: BLE001
        return {}


def _cache_save(cache: dict) -> None:
    try:
        cache_file().write_text(json.dumps(cache, ensure_ascii=False, indent=1),
                                encoding="utf-8")
    except Exception as e:                                     # noqa: BLE001
        print(f"[anchor] кэш метаданных не записан: {e}", flush=True)


def _sparse(row: dict) -> bool:
    """Карточка, о которой ещё нечего сказать: ни лейбла, ни жанра."""
    return not row.get("label") and not genres_of(row)


def _asked(have: dict, key: str) -> bool:
    """Ключ уже спрашивали и он либо что-то дал, либо отказ свежий.

    Отказ MUST быть посчитан: без него релиз, которого в витрине нет (или
    витрина без токена), спрашивается на каждом проходе — «один запрос на
    релиз» превращается в вечный.
    """
    hit = have.get(key)
    if not hit:
        return False
    if hit.get("label") or hit.get("genre"):
        return True
    return (time.time() - float(hit.get("miss") or 0)) < _MISS_RETRY_S


def _apply_cache(rows: list, cache: dict, store: str) -> int:
    """Разложить то, что уже лежит на диске; возвращает число пополненных."""
    have = (cache.get(store) or {})
    filled = 0
    for row in rows:
        if not _sparse(row) or not row.get("url"):
            continue
        hit = have.get(_meta_key(row))
        if not hit or not (hit.get("label") or hit.get("genre")):
            continue
        row["label"] = row.get("label") or hit.get("label") or ""
        if not genres_of(row):
            row["genres"] = [g for g in (hit.get("genre") or "").split(", ") if g]
            if not row["genres"]:
                row.pop("genres")
                row["genre"] = hit.get("genre") or ""
        filled += 1
    return filled


def _pending(rows: list, cache: dict, store: str, cap: int) -> list:
    have = cache.get(store) or {}
    out, seen = [], set()
    for row in rows:
        if not _sparse(row) or not row.get("url"):
            continue
        if str(row.get("service") or "") not in _ENRICH_SERVICES:
            continue
        k = _meta_key(row)
        if k in seen or _asked(have, k):
            continue
        seen.add(k)
        out.append(row)
        if len(out) >= cap:
            break
    return out


async def _enrich(rows: list, cache: dict, store: str, *,
                 fetch: Optional[Callable] = None, budget_sec: float = 6.0) -> int:
    """Один публичный запрос на карточку, результат — на диск."""
    if fetch is None:
        try:
            from .metadata import fetch_meta_any
        except Exception:                                      # noqa: BLE001
            return 0
        fetch = fetch_meta_any
    have = cache.setdefault(store, {})
    done, asked, started = 0, 0, time.time()
    for row in rows:
        if time.time() - started > budget_sec:
            break                          # остаток доживёт до следующего прохода
        try:
            got = await fetch(row.get("url") or "", row.get("service") or "") or {}
        except Exception:                                      # noqa: BLE001
            continue
        label = str(got.get("label") or "")
        genre = str(got.get("genre") or "")
        k = _meta_key(row)
        if not label and not genre:
            if not have.get(k):            # пустой ответ: запоминаем ОТКАЗ по времени
                have[k] = {"label": "", "genre": "", "miss": time.time()}
                asked += 1
            continue
        have[k] = {"label": label, "genre": genre}
        done += 1
    if done or asked:
        # Отказ тоже пишем: без него релиз, которого в витрине нет, спрашивали
        # бы заново на каждом проходе.
        cache["ts"] = time.time()
        _cache_save(cache)
    return done


def pending_releases(entries: list, cap: int = 20) -> list:
    """Релизы якорей этих подписок, о которых ещё ничего не известно."""
    cache = _cache_load()
    out = []
    for e in entries or []:
        a = for_entry(e)
        if not a:
            continue
        out.extend(_pending(a["releases"], cache, "meta", cap - len(out)))
        if len(out) >= cap:
            break
    return out


async def enrich(entries: list, *, fetch: Optional[Callable] = None,
                 cap: int = 8, budget_sec: float = 6.0) -> int:
    """Добрать лейбл/жанр якоря публичным каталогом — ОДИН запрос на релиз.

    Возвращает число добранного. `fetch(url, service)` подменяется в тесте; в
    продукте — общий диспетчер `ripster.metadata`. Темп: только из сетевого
    прохода привязки (`bind_pass`), ленту он не задерживает: бюджет — секунды,
    а всё добытое лежит на диске.
    """
    todo = pending_releases(entries, cap=cap)
    if not todo:
        return 0
    cache = _cache_load()
    done = await _enrich(todo, cache, "meta", fetch=fetch, budget_sec=budget_sec)
    if done:
        invalidate()
    return done


# ── Доказательства на карточках ленты ────────────────────────────────────────
# Правило судит по лейблу и жанру карточки. Витрина отдаёт их не всегда (у
# Deezer-списка релизов лейбла нет вовсе), и тогда вердикт обязан быть «нечем
# судить», а не «чужой». Добирать — одним запросом на карточку, только там,
# где подписку есть с чем сравнивать (якорь есть), и с кэшем на диске: то, что
# спросили однажды, больше не спрашивается.

def apply_card_cache(items: list) -> int:
    """Разложить карточкам то, что уже добыто и лежит на диске (без сети)."""
    if not items:
        return 0
    return _apply_cache(items, _cache_load(), "items")


def pending_cards(items: list, cap: int = 20) -> list:
    """Карточки без доказательств, по которым есть ЧЕМ судить."""
    rows = [r for r in (items or [])
            if _sparse(r) and for_name(str(r.get("artist") or ""))]
    return _pending(rows, _cache_load(), "items", cap)


async def evidence(items: list, *, fetch: Optional[Callable] = None,
                   cap: int = 6, budget_sec: float = 5.0) -> int:
    """Синхронизировать доказательства ленты: диск, потом сколько успеваем.

    Возвращает число карточек, ставших доказуемыми. Сеть — в пределах бюджета,
    и только по карточкам, которых ещё нет на диске: лента от этого медленнее
    не становится (на втором запросе кэш уже тёплый).
    """
    if not items:
        return 0
    got = apply_card_cache(items)
    todo = pending_cards(items, cap=cap)
    if todo:
        cache = _cache_load()
        done = await _enrich(todo, cache, "items", fetch=fetch,
                             budget_sec=budget_sec)
        # Считаем ОДИН раз: _enrich уже вернул число пополненных, а
        # apply_card_cache ниже только раскладывает эти же карточки по
        # строкам в памяти (с диска, без сети).
        got += done
        if done:
            apply_card_cache(items)
    return got


# ── Отчёт для владельца (/api/identity) ──────────────────────────────────────

def describe(entry: dict) -> dict:
    """Что считается якорем этой подписки и откуда это взялось."""
    a = for_entry(entry)
    if not a:
        return {"anchor": None, "sources": {}, "reason": "нет якоря"}
    return {
        "anchor": {
            "labels": sorted(a.get("labels") or ()),
            "families": sorted(a.get("families") or ()),
            "genres": sorted(a.get("genres") or ())[:12],
            "releases": [{"title": r.get("title"), "service": r.get("service"),
                          "label": r.get("label") or "",
                          "genre": ", ".join(genres_of(r))}
                         for r in (a.get("releases") or [])[:8]],
            "n": a.get("n") or 0,
            "unknown_n": sum(1 for r in (a.get("releases") or [])
                             if not r.get("label") and not genres_of(r)),
        },
        "sources": {k: len(v) for k, v in (a.get("sources") or {}).items()},
    }
