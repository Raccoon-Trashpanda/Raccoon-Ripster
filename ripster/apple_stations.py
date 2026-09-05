"""Редакторские подборки Apple Music как источник станции.

Владелец 05.09.2026: «изучи ещё ротации из эпла, тоже там качественно подборка
создаётся». Изучил — и вот что выяснилось на замерах.

У РАДИОСТАНЦИЙ Apple треклиста нет вообще: это поток, `catalog/{sf}/stations`
отдаёт название и описание, но не список вещей. Строить станцию на них нельзя.

Зато у Apple есть РЕДАКТОРСКИЕ ПЛЕЙЛИСТЫ, и вот они дают именно то, ради чего
всё затевалось. Замер 05.09.2026 по «melodic techno»: плейлист «Melodic Techno
Essentials» за подписью Apple Music содержит Monolink, ARTBAT, Tale Of Us,
Kölsch, Stephan Bodzin, Kevin de Vries. Для сравнения, наш собственный канон по
тегам MusicBrainz на том же жанре давал вперемешку настоящих артистов и Лану
Дель Рей — потому что тег там ставит кто угодно, а плейлист собирает редактор.

Отделять редакцию от чужих подборок пришлось не по подписи. Первая версия
верила `curatorName` — и тут же попалась: подборка самопиара одного артиста
была подписана «Apple Music Hip-Hop». Решает `playlistType`: `editorial` ставит
сама Apple, подделать его нельзя. Подпись остаётся вторым признаком — внутри
редакционных предпочитаем саму Apple, потому что «Algoriddim djay» тоже
editorial, но это чужой бренд.

Ещё две ловушки, обе пойманы замером и обе закрыты:
  · поиск уводит по первому слову — «dub techno» приводил к «Dub Essentials»
    (Apple Music Reggae): настоящая редакционная подборка, но регги;
  · подборка может быть редакционной и при этом состоять из одного артиста
    целиком — это не жанр, а концертный сет-лист.

У каждой вещи есть ISRC, длительность, год и обложка, поэтому телефон может
разрешить её в играбельную копию у своего сервиса: сам Apple он не стримит.
"""
from __future__ import annotations

import re

import httpx

_API = "https://api.music.apple.com/v1"

# Подписи редакции Apple. Сравнение по началу строки: у Apple десятки
# подразделений («Apple Music Dance», «Apple Music Hits»), и перечислять их
# поимённо значит отстать при первом же новом.
_APPLE_CURATOR = re.compile(r"^apple music\b", re.I)


def _headers(config: dict) -> dict | None:
    """Заголовки для каталога Apple, или None если токена нет.

    None означает ровно «спросить нечем», и вызывающий обязан сказать это
    вслух, а не выдать пустую подборку за отсутствие музыки в жанре.
    """
    bearer = (config.get("authorization-token") or "").strip()
    if not bearer or bearer == "your-authorization-token":
        return None
    auth = bearer if bearer.lower().startswith("bearer") else "Bearer " + bearer
    h = {"Authorization": auth, "Origin": "https://music.apple.com"}
    mut = (config.get("media-user-token") or "").strip()
    if mut:
        h["Music-User-Token"] = mut
    return h


def storefront(config: dict) -> str:
    return (str(config.get("storefront") or "").strip().lower() or "us")


def name_matches_genre(playlist_name: str, genre: str) -> bool:
    """Название подборки должно нести ГЛАВНОЕ слово жанра.

    Поиск Apple охотно уводит в сторону по первому слову: запрос «dub techno»
    приводил к «Dub Essentials» за подписью Apple Music Reggae — настоящая
    редакционная подборка, но регги, а не техно. Главное слово в русской и
    английской записи жанра стоит последним («dub techno», «melodic techno»,
    «deep house»), по нему и сверяем.
    """
    head = (genre or "").strip().split()[-1:] or [""]
    return head[0].lower() in (playlist_name or "").lower()


def rank_candidates(items: list[dict], genre: str) -> list[dict]:
    """Подборки в порядке доверия.

    `playlistType` — поле каталога, а не подпись: `editorial` ставит Apple, и
    подделать его нельзя. Именно поэтому оно главнее `curatorName`, которым
    пользователь может назваться как угодно: замер 05.09.2026 нашёл подборку
    самопиара, подписанную «Apple Music Hip-Hop».

    Внутри редакционных предпочитаем подписи самой Apple: «Algoriddim djay»
    тоже помечен editorial, но это чужой бренд.

    Подборки, чьё название не несёт главного слова жанра, из списка выпадают —
    см. name_matches_genre.
    """
    def key(p: dict) -> tuple:
        a = p.get("attributes") or {}
        editorial = str(a.get("playlistType") or "").lower() == "editorial"
        apple = bool(_APPLE_CURATOR.match(str(a.get("curatorName") or "")))
        return (0 if (editorial and apple) else 1 if editorial else 2,)

    fit = [p for p in items
           if name_matches_genre(str((p.get("attributes") or {}).get("name") or ""), genre)]
    return sorted(fit, key=key)


def artist_share(tracks: list[dict]) -> float:
    """Доля самого частого артиста. 1.0 — весь список одного человека."""
    if not tracks:
        return 0.0
    counts: dict[str, int] = {}
    for t in tracks:
        k = (t.get("artist") or "").strip().lower()
        if k:
            counts[k] = counts.get(k, 0) + 1
    return (max(counts.values()) / len(tracks)) if counts else 0.0


# Выше этой доли одного артиста подборка перестаёт быть подборкой жанра.
# Живой случай: «synthwave man’s Darkwave Cyberpunk … Concert Set List» —
# помечен Apple как editorial, а внутри один и тот же человек целиком.
MAX_ONE_ARTIST = 0.6


def _track(t: dict) -> dict:
    a = t.get("attributes") or {}
    art = (a.get("artwork") or {}).get("url") or ""
    # Шаблон обложки Apple содержит {w}/{h}/{f} — подставляем сами, иначе
    # клиент получит неработающую ссылку.
    if art:
        art = art.replace("{w}", "600").replace("{h}", "600").replace("{f}", "jpg")
    year = None
    rd = str(a.get("releaseDate") or "")
    if len(rd) >= 4 and rd[:4].isdigit():
        year = int(rd[:4])
    return {
        "title": a.get("name") or "",
        "artist": a.get("artistName") or "",
        "album": a.get("albumName") or "",
        "isrc": a.get("isrc") or "",
        "durationMs": a.get("durationInMillis"),
        "year": year,
        "artworkUrl": art,
    }


async def station(genre: str, config: dict, limit: int = 40) -> dict:
    """Подборка Apple по жанру.

    Возвращает `{"ok", "reason", "playlist", "curator", "editorial", "tracks"}`.
    `ok=False` всегда сопровождается причиной: «не нашли подборку» и «нечем
    спрашивать» — разные новости, и совет по ним разный.
    """
    genre = (genre or "").strip()
    if not genre:
        return {"ok": False, "reason": "no_genre", "tracks": []}
    h = _headers(config)
    if h is None:
        return {"ok": False, "reason": "no_apple_token", "tracks": []}
    sf = storefront(config)

    try:
        async with httpx.AsyncClient(timeout=25) as cl:
            r = await cl.get(f"{_API}/catalog/{sf}/search", headers=h,
                             params={"term": genre, "types": "playlists", "limit": 10})
            if r.status_code != 200:
                return {"ok": False, "reason": f"search_http_{r.status_code}", "tracks": []}
            items = ((r.json().get("results") or {}).get("playlists") or {}).get("data") or []
            ranked = rank_candidates(items, genre)
            if not ranked:
                return {"ok": False, "reason": "no_playlist", "tracks": []}

            # Перебираем по порядку доверия, пока подборка не окажется годной.
            # Потолок в три запроса: дальше по списку идёт уже что попало, и
            # платить за это ещё тремя обращениями незачем.
            rejected: list[str] = []
            for cand in ranked[:3]:
                attrs = cand.get("attributes") or {}
                r2 = await cl.get(f"{_API}/catalog/{sf}/playlists/{cand['id']}", headers=h,
                                  params={"include": "tracks", "limit[tracks]": limit})
                if r2.status_code != 200:
                    rejected.append(f"{attrs.get('name')}:http_{r2.status_code}")
                    continue
                data = (r2.json().get("data") or [{}])[0]
                raw = ((data.get("relationships") or {}).get("tracks") or {}).get("data") or []
                tracks = [_track(t) for t in raw]
                tracks = [t for t in tracks if t["title"] and t["artist"]]
                if not tracks:
                    rejected.append(f"{attrs.get('name')}:empty")
                    continue
                share = artist_share(tracks)
                if share > MAX_ONE_ARTIST:
                    rejected.append(f"{attrs.get('name')}:one_artist_{share:.0%}")
                    continue
                curator = str(attrs.get("curatorName") or "")
                return {
                    "ok": True,
                    "reason": "",
                    "playlist": attrs.get("name") or "",
                    "curator": curator,
                    "editorial": str(attrs.get("playlistType") or "").lower() == "editorial",
                    "storefront": sf,
                    "rejected": rejected,
                    "tracks": tracks,
                }
            # Ни одна не прошла — говорим, ЧТО именно отбраковали.
            return {"ok": False, "reason": "no_usable_playlist",
                    "rejected": rejected, "tracks": []}
    except Exception as e:
        return {"ok": False, "reason": f"network:{type(e).__name__}", "tracks": []}
