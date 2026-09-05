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

import json
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

import httpx

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


# ── сеть: сбор анонсов из лент ──────────────────────────────────────────────

#: Обычный браузерный UA и следование редиректам — без них половина списка
#: молча пустая (см. готчу в шапке модуля).
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")


def feed_titles(url: str, timeout: float = 20.0) -> list[str]:
    """Заголовки ленты. Пустой список — лента не ответила или не разобралась.

    Отказ ОДНОЙ ленты не должен ронять обход: источников много, и радар обязан
    отдать то, что собралось, а не ничего.
    """
    try:
        r = httpx.get(url, headers={"User-Agent": _UA}, follow_redirects=True, timeout=timeout)
        if r.status_code != 200 or not r.content:
            return []
        root = ET.fromstring(r.content)
    except Exception:
        return []
    # Первый <title> — название самой ленты, а не запись.
    return [(t.text or "").strip() for t in root.iter("title")][1:]


@dataclass
class Upcoming:
    """Один грядущий релиз, каким его знает радар."""

    artist: str
    title: str
    kind: str = "album"
    #: Ленты, в которых он попался. Одно и то же объявляют несколько изданий —
    #: и чем их больше, тем очевиднее, что релиза ЖДУТ.
    sources: list[str] = field(default_factory=list)
    #: Дата выхода по MusicBrainz, ISO. Пусто — пока не подтверждена.
    release_date: str = ""
    #: Жанры артиста по тегам MusicBrainz, от сильного к слабому.
    genres: list[str] = field(default_factory=list)
    first_seen: float = 0.0
    # ── то, из чего собирается обычная карточка релиза ──────────────────────
    #: Обложка. Пусто — её ещё нигде нет: у неизданного это обычное дело.
    artwork: str = ""
    #: Лейбл по данным MusicBrainz.
    label: str = ""
    #: Сколько вещей на релизе. 0 — треклист ещё не опубликован.
    track_count: int = 0
    #: Полная строка авторства: «A feat. B», совместные работы.
    credits: str = ""
    #: Идентификатор релиз-группы MusicBrainz — по нему берётся обложка.
    mbid: str = ""

    @property
    def key(self) -> str:
        return _key(self.artist, self.title)


def _key(artist: str, title: str) -> str:
    """Ключ склейки. Название часто не названо — тогда ключ по одному артисту:
    иначе «Andy Stott без названия» и «Andy Stott — Late Loop» из разных лент
    остались бы двумя разными ожиданиями одного и того же альбома."""
    a = re.sub(r"[^a-z0-9]+", "", artist.lower())
    t = re.sub(r"[^a-z0-9]+", "", title.lower())
    return f"{a}|{t}" if t else a


def collect(sources: tuple[Source, ...] = SOURCES) -> list[Upcoming]:
    """Обойти ленты и собрать анонсы. Сеть, поэтому без тестов — правило
    разбора проверяется отдельно ([parse_announcement])."""
    found: dict[str, Upcoming] = {}
    now = time.time()
    for s in sources:
        for headline in feed_titles(s.url):
            a = parse_announcement(headline)
            if not a:
                continue
            k = _key(a.artist, a.title)
            cur = found.get(k)
            if cur is None:
                found[k] = Upcoming(artist=a.artist, title=a.title, kind=a.kind,
                                    sources=[s.name], first_seen=now)
            else:
                if s.name not in cur.sources:
                    cur.sources.append(s.name)
                # Название могло быть названо только в одной ленте.
                if not cur.title and a.title:
                    cur.title = a.title
    return list(found.values())


# ── подтверждение: дата выхода и жанр ───────────────────────────────────────

#: MusicBrainz просит представляться и не чаще запроса в секунду.
_MB_UA = "Ripster/1.0 (https://github.com/Raccoon-Trashpanda/Raccoon-Ripster)"
_MB_GAP = 1.05
_mb_last = 0.0


def _mb_get(path: str, params: dict) -> dict:
    """Запрос к MusicBrainz с обязательной паузой между вызовами."""
    global _mb_last
    wait = _MB_GAP - (time.time() - _mb_last)
    if wait > 0:
        time.sleep(wait)
    _mb_last = time.time()
    try:
        r = httpx.get(f"https://musicbrainz.org/ws/2/{path}",
                      params={**params, "fmt": "json"},
                      headers={"User-Agent": _MB_UA}, timeout=20.0)
        return r.json() if r.status_code == 200 else {}
    except Exception:
        return {}


def _words(s: str) -> str:
    """Название без пунктуации — для сравнения и для запроса."""
    return re.sub(r"[^a-z0-9 ]+", " ", s.lower()).strip()
    

def _fill_card(item: Upcoming) -> None:
    """Дособрать карточку: лейбл, число вещей, авторство, обложка.

    Владелец: «по возможности обложка, дата релиза, лейбл, количество треков,
    фиты или коллабы — карточки релизов как обычные». Всё это MusicBrainz
    отдаёт на самом релизе, а обложку — Cover Art Archive по той же
    релиз-группе, бесплатно и без ключа.

    «По возможности» здесь буквально: у неизданного часто нет ни обложки, ни
    треклиста. Пустое поле честнее выдуманного — карточка просто не покажет
    строку, которой нет.
    """
    if not item.mbid:
        return
    rel = _mb_get("release", {"release-group": item.mbid,
                              "inc": "labels+recordings+artist-credits", "limit": 3})
    for r in rel.get("releases", [])[:1]:
        labels = [(l.get("label") or {}).get("name") for l in (r.get("label-info") or [])]
        item.label = next((x for x in labels if x), "")
        item.track_count = sum(m.get("track-count") or 0 for m in (r.get("media") or []))
        item.credits = ", ".join(
            a.get("name", "") for a in (r.get("artist-credit") or []) if a.get("name")
        )
    # Обложка существует не всегда — проверяем, а не подставляем ссылку вслепую:
    # битая картинка в карточке выглядит как поломка приложения.
    url = f"https://coverartarchive.org/release-group/{item.mbid}/front-500"
    try:
        if httpx.head(url, follow_redirects=True, timeout=12.0).status_code == 200:
            item.artwork = url
    except Exception:
        pass


def confirm(item: Upcoming) -> Upcoming:
    """Подтвердить анонс датой выхода и жанрами артиста.

    Новость говорит, что релиз БУДЕТ; MusicBrainz говорит, КОГДА и в каком
    жанре работает артист. Ни то ни другое не выдумывается: не нашлось —
    поля остаются пустыми, и в ленте так и будет написано «дата не объявлена».

    Дата берётся только БУДУЩАЯ. Совпадение по названию со старым альбомом
    того же артиста — обычное дело (переиздания, концертники), и подставить
    прошлогоднюю дату под анонс значит соврать в самом главном поле.
    """
    today = time.strftime("%Y-%m-%d")

    if item.title:
        # Точный запрос по названию ломается о пунктуацию: у MusicBrainz этот
        # альбом записан как «It’s Nothing Personal.» — типографский апостроф и
        # точка на конце, — и поиск по строке из новости не находил НИЧЕГО.
        # Поэтому спрашиваем по словам, а совпадение проверяем сами.
        words = _words(item.title)
        if words:
            q = f'artist:"{item.artist}" AND releasegroup:"{words}"'
            data = _mb_get("release-group", {"query": q, "limit": 8})
            for rg in data.get("release-groups", []):
                got = _words(rg.get("title") or "")
                if not got or (words not in got and got not in words):
                    continue
                d = (rg.get("first-release-date") or "").strip()
                if d and d >= today:
                    item.release_date = d
                    item.mbid = rg.get("id") or ""
                    _fill_card(item)
                    break

    data = _mb_get("artist", {"query": f'artist:"{item.artist}"', "limit": 1})
    for a in data.get("artists", [])[:1]:
        tags = sorted(a.get("tags") or [], key=lambda t: -(t.get("count") or 0))
        item.genres = [t["name"] for t in tags[:5] if t.get("name")]
    return item


# ── хранилище ───────────────────────────────────────────────────────────────

def _store_file(base_dir: Path) -> Path:
    return Path(base_dir) / "upcoming_releases.json"


def load(base_dir: Path) -> list[Upcoming]:
    try:
        raw = json.loads(_store_file(base_dir).read_text(encoding="utf-8"))
    except Exception:
        return []
    out = []
    for d in raw if isinstance(raw, list) else []:
        try:
            out.append(Upcoming(**d))
        except Exception:
            continue
    return out


def save(base_dir: Path, items: list[Upcoming]) -> None:
    try:
        _store_file(base_dir).write_text(
            json.dumps([i.__dict__ for i in items], ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
    except Exception as e:
        print(f"[upcoming] save failed: {e}", flush=True)


def merge(old: list[Upcoming], fresh: list[Upcoming]) -> list[Upcoming]:
    """Слить свежий обход с накопленным.

    Накопленное НЕ затирается: новость живёт в ленте пару дней, а ждать релиз
    можно месяцами. Пропал из ленты — не значит отменён.
    """
    by_key = {i.key: i for i in old}
    for f in fresh:
        cur = by_key.get(f.key)
        if cur is None:
            by_key[f.key] = f
            continue
        for s in f.sources:
            if s not in cur.sources:
                cur.sources.append(s)
        if not cur.title and f.title:
            cur.title = f.title
    return _fold_untitled(list(by_key.values()))


def _fold_untitled(items: list[Upcoming]) -> list[Upcoming]:
    """Склеить «артист без названия» с «артист + название».

    Одно издание пишет «Andy Stott Unveils First Album in Five Years», другое —
    «…, ‘Late Loop’». Это ОДИН альбом, а в ленте он выглядел двумя ожиданиями:
    ключ у безымянной записи строится по одному артисту и с ключом названной
    не совпадает. Поймано на живом обходе 05.09.2026.
    """
    by_artist: dict[str, Upcoming] = {}
    for i in items:
        if i.title:
            by_artist.setdefault(_key(i.artist, ""), i)

    out: list[Upcoming] = []
    for i in items:
        if not i.title:
            named = by_artist.get(i.key)
            if named is not None:
                for s in i.sources:
                    if s not in named.sources:
                        named.sources.append(s)
                continue
        out.append(i)
    return out


def drop_released(items: list[Upcoming], today: str | None = None) -> list[Upcoming]:
    """Убрать то, что уже вышло: у радара ГРЯДУЩЕГО этому места нет.

    Записи без подтверждённой даты остаются — «дата неизвестна» не то же самое,
    что «уже вышло».
    """
    t = today or time.strftime("%Y-%m-%d")
    return [i for i in items if not i.release_date or i.release_date >= t]
