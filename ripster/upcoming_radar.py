"""Радар ГРЯДУЩЕГО: релизы, которые ещё не вышли, но уже объявлены.

Замысел владельца (05.09.2026): «искать не менее чем по 20+ источникам релизы,
которые грядут, что-то что ждут люди, в определённом жанре, и чтобы это вставало
в вишлист. Я не знал, что выйдет альбом у Eden Samara, Klara Lewis, но потом
увидел и добавил артистов в любимые — а кто-то мог бы это обсудить в Guardian
или BBC».

Чем это отличается от обычного радара. Тот видит только то, что УЖЕ появилось в
каталоге сервиса: подписки, дискографии, чарты. Анонс альбома живёт за месяцы до
этого и только в новостях — в каталоге его ещё нет.

Список источников СОБРАН ЗАМЕРАМИ 05.09.2026, а не по представлению о том, кто
пишет про музыку. Проверялось одно: сколько заголовков в свежей ленте являются
анонсами релиза.

    Quietus          34 заголовка,  7 анонсов   — лучший по плотности
    Pitchfork news   30            7
    BrooklynVegan    16            9  (много про КОНЦЕРТЫ, не про альбомы)
    Clash            10            2
    XLR8R            10            2  (электроника)
    NME              10            1
    FACT             11            0  в этот заход, но формат подходящий

    Guardian music   31            0  — рецензии и очерки, НЕ новости релизов
    Bandcamp Daily   36            0  — то же самое
    Stereogum / Consequence / Loud and Quiet — сеть не пустила
    Reddit           403 и на JSON, и на RSS — нужен зарегистрированный app (OAuth)

Отсюда честная цифра: открытых источников семь, а не «20+». Guardian, названный
владельцем, для этой задачи не годится — он про уже вышедшее; Reddit годится, но
требует регистрации приложения. Обе вещи стоит знать до того, как строить поверх
них ожидания.

Здесь только РАЗБОР — чистые функции над строками. Сеть отдельно: правило
«является ли этот заголовок анонсом альбома» должно проверяться тестом на живых
заголовках, а не наблюдением за лентой.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# ── источники ───────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Source:
    name: str
    url: str
    #: Плотность анонсов, замеренная 05.09.2026 (анонсов / заголовков).
    measured: str
    #: Чем источник силён — для отчёта человеку, а не для кода.
    note: str = ""


SOURCES: tuple[Source, ...] = (
    Source("The Quietus", "https://thequietus.com/feed/", "7/34",
           "лучшая плотность анонсов; много андеграунда и электроники"),
    Source("Pitchfork", "https://pitchfork.com/rss/news/", "7/30",
           "крупные анонсы, инди и мейнстрим"),
    Source("Brooklyn Vegan", "https://www.brooklynvegan.com/feed/", "9/16",
           "много про концерты — заголовки о турах отсеиваются разбором"),
    Source("Clash", "https://www.clashmusic.com/feed/", "2/10", ""),
    Source("XLR8R", "https://xlr8r.com/feed/", "2/10", "электроника"),
    Source("NME", "https://www.nme.com/news/music/feed", "1/10", ""),
    Source("FACT", "https://www.factmag.com/feed/", "0/11",
           "в замер анонсов не попало, но формат ленты подходящий"),
)


# ── разбор заголовка ────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Announcement:
    artist: str
    #: Название релиза. Пусто — анонс есть, названия в заголовке нет.
    title: str
    #: Что именно объявлено: 'album' | 'ep'.
    kind: str


#: Глаголы анонса ровно в тех формах, что встретились в живых лентах:
#: «Announces / Announce / Detail / Unveils / Reveals / Shares».
_VERB = r"(?:announces?|announced|details?|unveils?|reveals?|shares?|returns?\s+with)"

#: Что объявляют. «Live Album», «First Album in Five Years», «Debut EP» — всё это
#: релизы; слово может стоять не сразу после глагола.
_WHAT = r"(?:album|ep|mixtape|record)"

#: Заголовки про КОНЦЕРТЫ ловятся тем же глаголом: «Cypress Hill announce
#: ‘Haunted’ shows in NYC». Для радара грядущих релизов это шум.
_NOT_RELEASE = re.compile(
    r"\b(?:show|shows|tour|dates|festival|residency|gig|concert|livestream)\b",
    re.I,
)

#: Служебные слова, с которых название релиза начинаться не может.
_FILLER = (
    "in", "since", "for", "after", "with", "from", "on", "this", "next",
    "and", "that", "which", "ft", "feat", "featuring", "out", "due",
    "shares", "share", "song", "single", "track", "video",
)

#: Кавычки, в которые ленты заворачивают название: обычные, типографские, «ёлочки».
_QUOTES_OPEN = "‘“«'\""
_QUOTES_CLOSE = "’”»'\""

_RE = re.compile(
    rf"^(?P<artist>.{{1,80}}?)\s+{_VERB}\b(?P<mid>.{{0,60}}?)\b(?P<what>{_WHAT})\b"
    rf"(?P<tail>.*)$",
    re.I,
)


def _clean_title(tail: str) -> str:
    """Вынуть название релиза из хвоста заголовка.

    Ленты пишут его либо в кавычках («Announce New Album, ‘Holy Bible Live’»),
    либо просто следом («Announces New Album I Wrote You a Letter»).

    Кавычки ищутся ПО ВСЕМУ хвосту, а не только в его начале. Между словом
    «Album» и названием бывает вставка: «First Album in Five Years, ‘Late Loop’».
    Пока бралось начало хвоста, названием оказывалось «in Five Years» —
    поймано тестом на живом заголовке The Quietus.
    """
    t = tail.strip().lstrip(",:–—-").strip()
    if not t:
        return ""

    start = next((i for i, ch in enumerate(t) if ch in _QUOTES_OPEN), -1)
    if start >= 0:
        end = -1
        for i in range(start + 1, len(t)):
            if t[i] in _QUOTES_CLOSE:
                end = i
                break
        inner = (t[start + 1:end] if end > start + 1 else t[start + 1:]).strip()
        # Запятая часто стоит ВНУТРИ кавычек: «‘Dopamine Chamber,’ out in March».
        inner = inner.strip(" ,.;:–—-")
        if inner:
            return inner

    # Без кавычек название бывает, а бывает и нет: «First Album in Five Years»
    # — это про срок, а не про название. Если хвост начинается со служебного
    # слова, названия в заголовке не было вовсе: живой прогон 05.09.2026 сделал
    # из такого заголовка «название» «in Five Years».
    if re.match(rf"^(?:{'|'.join(_FILLER)})\b", t, re.I):
        return ""
    first = re.split(r"\s+[–—|]\s+|,\s|\.\s", t)[0]
    return first.strip(" ,.;:–—-")


def parse_announcement(headline: str) -> Announcement | None:
    """Анонс релиза или нет.

    `None` — заголовок не про анонс релиза. Возвращать «наверное, анонс» нельзя:
    из этого списка потом собирается вишлист, и мусор в нём хуже пустоты.
    """
    h = (headline or "").strip()
    if not h or _NOT_RELEASE.search(h):
        return None
    m = _RE.match(h)
    if not m:
        return None

    artist = m.group("artist").strip(" \t-–—")
    if not artist or len(artist) < 2:
        return None
    # Имя артиста, а не кусок фразы. «Miley Cyrus Rebrands as Miley,» — это
    # пересказ новости; в вишлисте такое выглядит ровно как мусор. Поймано
    # живым прогоном 05.09.2026. Признаки: запятая на конце и длина в целое
    # предложение.
    if artist.endswith(",") or len(artist.split()) > 6:
        return None
    # «Listen to» / «Watch» — это про уже вышедшее, а не про анонс.
    if re.match(r"^(?:listen|watch|hear|stream)\b", artist, re.I):
        return None

    kind = "ep" if m.group("what").lower() == "ep" else "album"
    return Announcement(artist=artist, title=_clean_title(m.group("tail")), kind=kind)
