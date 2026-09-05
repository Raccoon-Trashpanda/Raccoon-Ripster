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
пишет про музыку: у каждого кандидата считалось, сколько заголовков свежей ленты
на самом деле являются анонсами релиза. Плотность каждого оставленного источника
стоит рядом с ним в [SOURCES].

Что НЕ прошло проверку и почему это стоит знать:

* Guardian music (0 из 31) и Bandcamp Daily (0 из 36) — рецензии и очерки, а не
  новости релизов. Guardian был назван владельцем как пример; для этой задачи он
  не годится.
* Rolling Stone (0/11), Gorilla vs Bear (0/36) — новости есть, анонсов в срезе
  нет; формат не про них.
* Blabbermouth (0/30, металл) — анонсы там ЕСТЬ, но заголовок устроен иначе:
  имя капсом и вставные обороты («UMBILICUS, Featuring CANNIBAL CORPSE And
  DEICIDE Members, Announces New EP»). Разбор под такое пришлось бы ослабить
  ровно там, где он отсекает пересказы новостей, — поэтому источник не заведён,
  а не заведён «мёртвым».
* Reddit — 403 и на JSON, и на RSS: нужен зарегистрированный app (OAuth).
  Владелец назвал его источником инсайдов, так что это стоит завести отдельно.
* Mixmag (404), The Fader / Resident Advisor / Exclaim / Under the Radar (403),
  Northern Transmissions и HipHopDX (лента не разбирается как XML).

Готча для того, кто будет писать сетевой слой: Stereogum отвечает 308, HipHopDX
— 301, и `urllib` в нашем окружении на части лент падает с URLError там, где
`curl -L` с браузерным User-Agent проходит. Значит: ходить с обычным UA и
СЛЕДОВАТЬ редиректам, иначе половина списка молча окажется пустой.

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
    # Замеры 05.09.2026: сколько заголовков свежей ленты оказались анонсами.
    Source("Stereogum", "https://www.stereogum.com/feed/", "12/41",
           "лучшая плотность из всех проверенных; инди и мейнстрим"),
    Source("The Quietus", "https://thequietus.com/feed/", "7/34",
           "андеграунд и электроника"),
    Source("Pitchfork", "https://pitchfork.com/rss/news/", "7/30",
           "крупные анонсы, инди и мейнстрим"),
    Source("Brooklyn Vegan", "https://www.brooklynvegan.com/feed/", "9/16",
           "много про концерты — заголовки о турах отсеиваются разбором"),
    Source("The Line of Best Fit", "https://www.thelineofbestfit.com/feed", "3/11", ""),
    Source("Clash", "https://www.clashmusic.com/feed/", "2/10", ""),
    Source("XLR8R", "https://xlr8r.com/feed/", "2/10", "электроника"),
    Source("Consequence", "https://consequence.net/feed/", "1/16", ""),
    Source("DJ Mag", "https://djmag.com/rss.xml", "1/15", "танцевальная сцена"),
    Source("The Ransom Note", "https://www.theransomnote.com/feed/", "1/11",
           "андеграундная электроника"),
    Source("NME", "https://www.nme.com/news/music/feed", "1/10", ""),
    # Ленты новостного формата, у которых в замеряемый заход анонсов не
    # оказалось. Держим: один срез — это не приговор источнику.
    Source("FACT", "https://www.factmag.com/feed/", "0/11", ""),
    Source("Attack Magazine", "https://www.attackmagazine.com/feed/", "0/11", "электроника"),
    Source("Spin", "https://www.spin.com/feed/", "0/11", ""),
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
    "shares", "share", "song", "single", "track", "video", "of", "by",
)

#: Описательная приставка перед именем: «Alternative metal band Prodigal
#: announces…». В вишлисте нужен артист, а не пересказ, кто он такой.
_DESCRIPTOR = re.compile(
    r"^.{0,40}?\b(?:band|duo|trio|quartet|collective|project|singer|songwriter"
    r"|rapper|producer|artist|group|composer|dj)\s+(?=\S)",
    re.I,
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
    first = first.strip(" ,.;:–—-")
    # «…New Album Bloodwork Feat. Someone» — разрез по точке оставлял в конце
    # висящее «Feat»/«Ft». К названию оно не относится.
    return re.sub(r"\s+(?:feat|ft|featuring|with)$", "", first, flags=re.I).strip()


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
    # «Alternative metal band Prodigal» → «Prodigal». Живой заголовок The Line
    # of Best Fit: без этого в вишлист уезжает описание вместо имени.
    stripped = _DESCRIPTOR.sub("", artist).strip()
    if stripped and stripped != artist and len(stripped) >= 2:
        artist = stripped
    # «Listen to» / «Watch» — это про уже вышедшее, а не про анонс.
    if re.match(r"^(?:listen|watch|hear|stream)\b", artist, re.I):
        return None

    kind = "ep" if m.group("what").lower() == "ep" else "album"
    return Announcement(artist=artist, title=_clean_title(m.group("tail")), kind=kind)
