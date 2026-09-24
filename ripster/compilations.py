"""Compilation-awareness — the one blind spot every per-artist release scanner has.

Every release scanner in Ripster (Spotify radar, Apple watchlist checker, and any
future one) is built the same way: for each artist you follow, ask the service for
that artist's releases. That question has a blind spot, and it is not obvious:

    A various-artists compilation is not IN any artist's release list.

Its *album artist* is "Various Artists" or the label — so the artist whose track is
on it is only a track-level credit. Walk that artist's discography and the
compilation simply is not there. The individual tracks show up (each artist put
their track out as a single too), the compilation never does, and nothing errors:
the scanner reports success having never seen it. That is why this went unnoticed —
see [[project_radar_compilation_blindspot]].

The fix is always the same shape, only the transport differs per service:

    Do not ask "which albums is this artist the artist of?"
    Ask "which releases does this artist APPEAR on?" — then keep the compilations.

Per-service transports that answer the second question:

  Spotify   `queryArtistAppearsOn` (api-partner GraphQL) — the web player's own
            "Appears On" shelf. Its items carry no date or type, so each newly
            seen album id needs one `getAlbum` call for metadata.
  Apple     `lookup?id=<artist>&entity=song` — a *track* always carries its parent
            `collectionId`/`collectionName`/`collectionArtistName`, compilations
            included. (`entity=album` returns only albums the artist is credited
            as album artist on, which is exactly the blind spot.)
  Others    Same rule: find the endpoint that answers "appears on" / "tracks by",
            and read the parent release off the track.

This module holds the parts that are genuinely service-independent: deciding
whether a release is a compilation, and merging without duplicates.
"""
from __future__ import annotations

import re

# "Various Artists" as the services actually spell it, across storefront locales.
_VA_NAMES = {
    "various artists", "various", "varios artistas", "vários artistas",
    "verschiedene interpreten", "artistes divers", "artisti vari",
    "различные исполнители", "разные исполнители", "сборник",
    "オムニバス", "群星", "여러 아티스트", "va",
}

# Title shapes that mean "compilation" even when the service labels the release an
# album and credits it to a label rather than to Various Artists. Deliberately
# conservative: each of these is a phrase labels use for a comp, not a normal album.
_COMP_TITLE_RE = re.compile(
    r"\b(?:compilation|sampler|soundtrack|"
    r"(?:various|selected)\s+artists|"
    r"v\.?a\.?\s*[-–—]|"
    r"\d+\s+years\s+(?:of\s+)?|"
    r"best\s+of\s+\d{4}|"
    r"(?:summer|winter|spring|autumn|fall|ibiza|miami|ade)\s+(?:selection|sampler|compilation)"
    r")\b",
    re.IGNORECASE,
)


# Работа, под одной обложкой которой стоит несколько исполнителей, НЕ может
# доказывать, что два каталога говорят об одном человеке: в чужом диджей-мите
# или лейбловом «presents» твой заголовок — случайный сосед. Ровно так
# 21.09.2026 испанский госпел-однофамилец привязался к «Solomon Grey» через
# «Group Therapy Anjuna25 Special with Above & Beyond (DJ Mix)».
_MIX_TITLE_RE = re.compile(
    r"\b(?:dj[ -]?mix(?:es)?|dj[ -]?set|mixtape|mixed by|mix 0?\d|\bmix\b"
    r"|presents|curated by|selected by|compiled by)\b",
    re.IGNORECASE,
)


# Непрерывный диджей-мит — компиляция ЧУЖИХ треков, даже когда на обложке стоит
# один артист-сводчик (Anjunadeep Open Air Prague «(DJ Mix)» подписан 16BL, но
# внутри 17 дорожек разных авторов). В отличие от _MIX_TITLE_RE (там голый «mix»
# и «presents» — слишком широко для вердикта «не доказывает личность»), этот
# распознаватель консервативен: только явные формы микса.
_DJMIX_TITLE_RE = re.compile(
    r"\b(?:dj[ -]?mix(?:es)?|mixed by|dj[ -]?set|mixtape)\b",
    re.IGNORECASE,
)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def is_various_artists(name: str) -> bool:
    """Is this credit the services' placeholder for "lots of people"?"""
    return _norm(name) in _VA_NAMES


def is_dj_mix(title: str = "", album_type: str = "") -> bool:
    """A continuous DJ set/mix — a crowd work even when one DJ headlines it."""
    if _norm(album_type) in ("compilation", "mix"):
        return True
    return bool(_DJMIX_TITLE_RE.search(title or ""))


def build_alias_group(names) -> frozenset:
    """Нормализованное множество написаний ОДНОГО артиста (16BL, «16 Bit
    Lolitas», «16 Bit Lolita's»). Пустое — значит «алиасов не знаем», тогда
    `same_artist` сводится к строгому равенству имён."""
    return frozenset(n for n in (_norm(x) for x in (names or [])) if n)


def same_artist(a: str, b: str, alias_group=frozenset()) -> bool:
    """Один и тот же артист под двумя написаниями?

    Равны нормализованно, или обе формы лежат в одном алиас-группе (переименованный
    дуэт: «16BL» ≡ «16 Bit Lolitas»). Без этого чужой трек-кредит под прежним
    именем читается как другой человек, и собственная работа артиста уезжает в
    «участие у постороннего».
    """
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    return bool(alias_group) and na in alias_group and nb in alias_group


def is_compilation(album_type: str = "", album_artist: str = "",
                   title: str = "", track_artist: str = "") -> bool:
    """Should this release be presented to the user as a compilation?

    Checked in order of how much we trust the signal:
      1. the service says so outright (album_type)
      2. the album is credited to Various Artists
      3. the album artist differs from the artist we were scanning for AND the
         title reads like a comp — this is the label-branded case ("Hospital
         Records presents…", "15 Years Sound Avenue")
    """
    if _norm(album_type) == "compilation":
        return True
    if is_various_artists(album_artist):
        return True
    if title and _COMP_TITLE_RE.search(title):
        # A release the scanned artist headlines is their own record even if the
        # title mentions a year count — only call it a comp when someone else
        # (a label, another artist) is the album artist.
        if not track_artist or _norm(album_artist) != _norm(track_artist):
            return True
    return False


def proves_identity(album_type: str = "", album_artist: str = "",
                    title: str = "", track_artist: str = "") -> bool:
    """May this release be used as evidence that two catalogs mean ONE person?

    `is_compilation` answers a presentation question ("put it under Сборники").
    This answers an epistemic one, and the bar is higher: a work that carries a
    crowd — a comp, a sampler, a soundtrack, a DJ mix, a label showcase — has
    titles two unrelated artists can share by accident, so agreeing on it proves
    nothing about who is who. Rows like that are dropped from the shared-work
    evidence in `artist_identity.classify`.

    Safe on empty input: an unnamed row cannot be evidence either way, and the
    caller decides what to do with a title it never saw.
    """
    if is_compilation(album_type, album_artist, title, track_artist):
        return False
    if _MIX_TITLE_RE.search(title or ""):
        if not track_artist or _norm(album_artist) != _norm(track_artist):
            return False
    return bool(_norm(title))


def classify(album_type: str = "", album_artist: str = "",
             title: str = "", track_artist: str = "") -> tuple[str, str]:
    """→ (type, group) for the release store.

    Compilations are stored with group="compilation" rather than "appears_on"
    on purpose: the UI already has a "Сборники" filter, and a VA compilation is
    what a user means by one. Plain guest appearances (being featured on another
    artist's album) stay "appears_on" so they only show when explicitly asked for.
    """
    if is_compilation(album_type, album_artist, title, track_artist):
        return "compilation", "compilation"
    return (_norm(album_type) or "album"), "appears_on"


def merge_releases(existing: list, incoming: list) -> int:
    """Add `incoming` releases that aren't already in `existing` (by id).
    Returns how many were genuinely new. Mutates `existing`."""
    have = {r.get("id") for r in existing if r.get("id")}
    added = 0
    for r in incoming:
        rid = r.get("id")
        if not rid or rid in have:
            continue
        have.add(rid)
        existing.append(r)
        added += 1
    return added


def _own_type(track_count: int, album_artist: str, artist_name: str,
              title: str, alias_group, collection_type: str = "") -> tuple[str, str]:
    """(type, group) for a release already credited in the artist's own list.

    Свой сингл/EP/альбом — по числу дорожек. Но если пластинка помечена сборником,
    подписана «Various Artists» или это непрерывный диджей-мит — это компиляция,
    даже когда на обложке стоит сам артист: внутри чужие треки, и показывать её
    надо целиком как сборник, а не как «ещё один альбом».
    """
    a_low = _norm(album_artist)
    own = same_artist(album_artist, artist_name, alias_group)
    compilation = (
        _norm(collection_type) == "compilation"
        or is_various_artists(album_artist)
        or is_dj_mix(title=title)
        or (not own and is_compilation(album_artist=album_artist, title=title,
                                       track_artist=artist_name))
    )
    if compilation:
        return "compilation", "compilation"
    if not own and a_low and a_low != _norm(artist_name):
        # Гостевое участие на чужом альбоме — видно только по явному запросу.
        return "single" if track_count <= 3 else "appears_on", "appears_on"
    if track_count <= 3:
        return "single", "album"
    if track_count <= 6:
        return "ep", "album"
    return "album", "album"


def scan_artist_releases(album_rows: list, song_rows: list, artist_name: str,
                         alias_group=frozenset(), wanted: set | None = None,
                         service: str = "apple") -> list:
    """Собрать дискографию из двух iTunes-проходов (album + song).

    `album_rows` — коллекции, где артист значится альбом-артистом. `song_rows` —
    его треки; у каждого есть родительская коллекция (collectionId/Name/Artist),
    и именно там живут миксы и VA-сборники, в которых у артиста одна дорожка:
    на них entity=album не отвечает никогда. Родительские коллекции, отсутствующие
    среди album_rows, добавляются как компиляции/участие с НАСТОЯЩИМ альбом-
    артистом, настоящей обложкой и подсветкой «вот трек(и) этого артиста».

    Возвращает списки релизов; id = collectionId, поэтому `openAlbumPage` открывает
    ВЕСЬ релиз, а не отдельный трек. Чистая функция — сеть не трогает, чтобы её
    можно было тестировать на фикстурах сервиса.
    """
    def cover(row):
        return (row.get("artworkUrl100", "") or "").replace("100x100", "600x600")

    releases: list = []
    by_id: dict = {}
    for a in album_rows:
        cid = str(a.get("collectionId", ""))
        if not cid:
            continue
        album_artist = a.get("artistName", "") or ""
        title = a.get("collectionName", "") or ""
        rtype, group = _own_type(int(a.get("trackCount") or 0), album_artist,
                                 artist_name, title, alias_group,
                                 a.get("collectionType", ""))
        r = {
            "id": cid,
            "title": title,
            "cover": cover(a),
            "year": (a.get("releaseDate", "") or "")[:4],
            "date": (a.get("releaseDate", "") or "")[:10],
            "tracks": a.get("trackCount") or 0,
            "type": rtype,
            "group": group,
            "url": a.get("collectionViewUrl", ""),
            "explicit": a.get("collectionExplicitness") == "explicit",
            "album_artist": album_artist,
            "is_compilation": rtype == "compilation",
            # Для своего микса/сборника весь треклист принадлежит артисту —
            # подсветка не нужна (пусто). Для чужой компилы заполнится ниже,
            # когда из трекового прохода известны trackId именно его дорожек.
            "highlight": [],
            "service": service,
        }
        by_id[cid] = r
        releases.append(r)

    # Родительские коллекции из трекового прохода: компиляции, где артист — одна
    # дорожка. Считаем, какие треки принадлежат ЭТОМУ артисту (с учётом алиасов) и
    # под каким написанием он там указан.
    parents: dict = {}
    for s in song_rows:
        if s.get("wrapperType") != "track":
            continue
        cid = str(s.get("collectionId") or "")
        if not cid or cid in by_id:
            continue
        track_artist = s.get("artistName", "") or ""
        is_own = same_artist(track_artist, artist_name, alias_group)
        p = parents.get(cid)
        if p is None:
            p = parents[cid] = {
                "id": cid,
                "title": s.get("collectionName", "") or "",
                "cover": cover(s),
                "year": (s.get("releaseDate", "") or "")[:4],
                "date": (s.get("releaseDate", "") or "")[:10],
                "tracks": 0,
                "url": s.get("collectionViewUrl", ""),
                "explicit": s.get("trackExplicitness") == "explicit",
                "album_artist": (s.get("collectionArtistName") or track_artist or ""),
                "highlight": [],
                "credited_as": "",
                "appears_as": "",
                "service": service,
            }
        p["tracks"] += 1
        if is_own:
            tid = str(s.get("trackId") or "")
            if tid and tid not in p["highlight"]:
                p["highlight"].append(tid)
            # Название СВОЕГО трека на чужом релизе — телефон показывает его в
            # подписи карточки («… · как <трек>»), иначе пользователю видно только
            # чужой альбом-артист и неясно, причём тут вообще его артист.
            if not p["appears_as"]:
                p["appears_as"] = s.get("trackName", "") or ""
            # Артист указан ПРЕЖНИМ именем внутри чужого микса — показать это
            # написание («как 16 Bit Lolitas»), но не чужим артистом.
            if track_artist and _norm(track_artist) != _norm(artist_name):
                p["credited_as"] = track_artist

    for cid, p in parents.items():
        album_artist = p["album_artist"]
        own_headline = same_artist(album_artist, artist_name, alias_group)
        # Релиз, где у артиста хотя бы один СВОЙ трек, но заголовок — не он
        # (VA/лейбл/другой диджей) или это непрерывный микс — компиляция.
        compilation = (
            is_various_artists(album_artist)
            or is_dj_mix(title=p["title"])
            or (not own_headline and p["highlight"])
            or is_compilation(album_artist=album_artist, title=p["title"],
                               track_artist=artist_name)
        )
        if compilation:
            p["type"], p["group"] = "compilation", "compilation"
            p["is_compilation"] = True
        elif own_headline:
            p["type"], p["group"] = _own_type(p["tracks"], album_artist, artist_name,
                                              p["title"], alias_group)
            p["is_compilation"] = p["type"] == "compilation"
        else:
            p["type"], p["group"] = "appears_on", "appears_on"
            p["is_compilation"] = False
        p.setdefault("year", p.get("year", ""))
        releases.append(p)
        by_id[cid] = p

    if wanted and wanted != {"all"}:
        releases = [r for r in releases if r.get("type") in wanted]
    releases.sort(key=lambda r: r.get("date", ""), reverse=True)
    return releases
