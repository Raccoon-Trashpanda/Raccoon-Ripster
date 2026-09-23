"""ripster.metadata — service-agnostic metadata fetching.

Public surface:
    fetch_meta_any(url, service="")   — dispatcher, routes to the right backend
    fetch_meta_apple(url)             — Apple Music catalog
    fetch_meta_deezer(url)            — Deezer public API
    fetch_meta_qobuz(url)             — Qobuz public API
    fetch_meta_spotify(url)           — Spotify Web API (PKCE token)
    fetch_meta_mixesdb(query)         — MixesDB MediaWiki search

Setup (call once at app startup, before any fetch):
    install(cfg, broadcast_fn, save_config_fn, detect_service_fn, spotify_token_getter)
"""
from __future__ import annotations

from typing import Optional, Callable

from .apple   import fetch_meta as fetch_meta_apple, auto_fetch_bearer, install as _install_apple
from .deezer  import fetch_meta_deezer
from .qobuz   import fetch_meta_qobuz, install as _install_qobuz
from .spotify import fetch_meta_spotify, install as _install_spotify
from .mixesdb import search_mixesdb, fetch_mix_detail
from .bbc     import fetch_meta_bbc


_detect_service:  Callable[[str], str] = lambda url: "unknown"
_broadcast_fn:    Callable = None   # type: ignore[assignment]
_queue_snapshot:  Callable = None   # type: ignore[assignment]
_cfg:             dict = {}          # set in install(); used by yandex meta


# ── Normalise: every metadata dict gets all standard fields ──────────────────
# Keep in sync with what the frontend reads (buildQueueItem / updateQueueItem).
_META_DEFAULTS: dict = {
    "id":          "",
    "type":        "",
    "title":       "",
    "artist":      "",
    "album":       "",
    "year":        "",
    "date":        "",
    "label":       "",
    "genre":       "",
    "upc":         "",
    "isrc":        "",
    "trackNumber": None,
    "discNumber":  None,
    "trackCount":  0,
    "totalTracks": 0,
    "artworkUrl":  "",
    "explicit":    False,
    "service":     "",
    "enriched":    True,   # tells the UI that enrichment finished (even if sparse)
}


def _normalize(meta: dict) -> dict:
    """Return a copy of *meta* with every standard key present (defaults filled)."""
    out = dict(_META_DEFAULTS)
    out.update(meta)
    # Ensure trackCount / totalTracks are consistent when only one is set
    if out["trackCount"] and not out["totalTracks"]:
        out["totalTracks"] = out["trackCount"]
    elif out["totalTracks"] and not out["trackCount"]:
        out["trackCount"] = out["totalTracks"]
    if "tracks" in out and "tracksComplete" not in out:
        _tc = out.get("trackCount") or 0
        out["tracksComplete"] = not _tc or len(out["tracks"] or []) >= _tc
    return out


# ── Wiring ────────────────────────────────────────────────────────────────────

def install(
    cfg: dict,
    broadcast_fn,
    save_config_fn,
    detect_service_fn: Callable[[str], str] | None = None,
    spotify_token_getter=None,
    queue_snapshot_fn: Callable | None = None,
) -> None:
    """Wire up all metadata backends. Call once at app startup."""
    global _detect_service, _broadcast_fn, _queue_snapshot, _cfg
    _cfg = cfg
    _install_apple(cfg, broadcast_fn, save_config_fn)
    _install_qobuz(cfg)
    if detect_service_fn is not None:
        _detect_service = detect_service_fn
    if spotify_token_getter is not None:
        _install_spotify(spotify_token_getter, cfg)
    if broadcast_fn is not None:
        _broadcast_fn = broadcast_fn
    if queue_snapshot_fn is not None:
        _queue_snapshot = queue_snapshot_fn


async def fetch_meta_any(url: str, service: str = "") -> Optional[dict]:
    """Route to the correct metadata backend based on service or URL."""
    svc = service or _detect_service(url)
    meta: Optional[dict] = None

    if svc == "deezer":
        meta = await fetch_meta_deezer(url)
    elif svc == "apple":
        meta = await fetch_meta_apple(url)
    elif svc == "qobuz":
        meta = await fetch_meta_qobuz(url)
    elif svc == "spotify":
        meta = await fetch_meta_spotify(url)
    elif svc == "yandex":
        meta = await fetch_meta_yandex(url)
    elif svc == "tidal":
        meta = await fetch_meta_tidal(url)
    elif svc == "soundcloud":
        meta = await fetch_meta_soundcloud(url)
    elif svc == "beatport":
        meta = await fetch_meta_beatport(url)
    elif svc == "bbc":
        meta = await fetch_meta_bbc(url)
    elif svc == "jiosaavn":
        meta = await fetch_meta_jiosaavn(url)
    elif svc == "amazon":
        # Amazon Music serves a JS-only SPA shell (no OpenGraph/SEO tags) and has
        # no anonymous catalog API, so there's no progress-card metadata source.
        # The card fills from the downloaded file tags on success (amz writes a
        # tagged FLAC + cover). Return None → stays sparse during download only.
        return None
    elif svc == "orpheus_spotify":
        return None   # no metadata API — return None so enrich_meta merges instead of overwrites

    if meta is None:
        return None
    return _normalize(meta)


async def fetch_meta_yandex(url: str) -> Optional[dict]:
    """Metadata for a Yandex album/track URL — via the yandex engine (yandex_music)."""
    import re as _re
    m = _re.search(r"/album/(\d+)", url)
    if not m:
        return None
    album_id = m.group(1)
    try:
        from ripster.engines import get_engine
        d = await get_engine("yandex").get_album(album_id, _cfg)
    except Exception:
        return None
    if not d or d.get("error"):
        return None
    alb = d.get("album") or {}
    tracks = d.get("tracks") or []
    tr = _re.search(r"/track/(\d+)", url)
    title = alb.get("title", "")
    if tr:
        for t in tracks:
            if str(t.get("id")) == tr.group(1):
                title = t.get("title") or title
                break
    return {
        "id":         album_id,
        "title":      title,
        "artist":     alb.get("artist", ""),
        "album":      alb.get("title", ""),
        "artworkUrl": alb.get("cover", ""),
        "trackCount": alb.get("tracks") or len(tracks),
        "tracks":     [{"num": t.get("track_no") or i,
                        "title": t.get("title") or "",
                        "artist": t.get("artist") or "",
                        "dur": t.get("duration") or 0}
                       for i, t in enumerate(tracks, 1)],
        "type":       "track" if tr else "albums",
        "year":       alb.get("year", ""),
        "service":    "yandex",
    }


async def fetch_meta_jiosaavn(url: str) -> Optional[dict]:
    """Карточка для трека/альбома/плейлиста JioSaavn через публичный webapi.get.

    Логина у сервиса нет, и мета-API не гео-заблокирован (в отличие от
    аудиоконсоли saavncdn) — с нашего IP он честно отвечает и из России.
    Удалённый/битый id сервис отдаёт НЕ 404, а 200 с карточкой-заглушкой
    (albumid "0", без названия) — её считаем отсутствием меты, как движок.
    Best-effort: любая неудача → None, карточка остаётся пустой, не падает."""
    import urllib.parse
    try:
        from ripster.engines.orpheus_jiosaavn import (_webapi_get, _saavn_token,
                                                      _un, _big_cover)
    except Exception:
        return None
    path = urllib.parse.urlparse(url).path.lower()
    if   "/album/" in path:    typ = "album"
    elif "/song/" in path:     typ = "song"
    elif "/playlist/" in path or "/featured/" in path: typ = "playlist"
    else:
        return None
    try:
        d = await _webapi_get(_saavn_token(url), typ)
    except Exception:
        return None
    if d.get("error"):
        return None

    if typ == "album":
        if not (d.get("title") or d.get("name")) or \
                str(d.get("albumid") or "0") in ("", "0"):
            return None
        songs = d.get("songs") or []
        return {
            "id":         _saavn_token(url),
            "type":       "albums",
            "title":      _un(d.get("title") or d.get("name")),
            "artist":     _un(d.get("primary_artists")),
            "album":      _un(d.get("title") or d.get("name")),
            "artworkUrl": _big_cover(d.get("image", "")),
            "trackCount": len(songs),
            "year":       str(d.get("year") or ""),
            "date":       str(d.get("release_date") or "")[:10],
            "label":      _un((songs[0] if songs else {}).get("label")),
            "service":    "jiosaavn",
        }

    if typ == "playlist":
        songs = d.get("songs") or []
        if not d.get("listname"):
            return None
        return {
            "id":         _saavn_token(url),
            "type":       "playlist",
            "title":      _un(d.get("listname")),
            "artist":     _un(d.get("firstname") or d.get("username")),
            "artworkUrl": _big_cover(d.get("image", "")),
            "trackCount": len(songs),
            "service":    "jiosaavn",
        }

    # song: webapi.get type=song отдаёт {'songs': {...}} — словарь одного трека
    s = d.get("songs")
    if isinstance(s, list):
        s = s[0] if s else None
    if not isinstance(s, dict) or not s.get("song"):
        return None
    return {
        "id":         _saavn_token(url),
        "type":       "track",
        "title":      _un(s.get("song")),
        "artist":     _un(s.get("primary_artists")),
        "album":      _un(s.get("album")),
        "artworkUrl": _big_cover(s.get("image", "")),
        "trackCount": 1,
        "year":       str(s.get("year") or ""),
        "date":       str(s.get("release_date") or "")[:10],
        "label":      _un(s.get("label")),
        "service":    "jiosaavn",
    }


async def fetch_meta_tidal(url: str) -> Optional[dict]:
    """Metadata for a Tidal track/album URL via api.tidal.com (Bearer token).
    Uses blocking urllib in a thread (system resolver — respects the user's VPN,
    unlike aiodns). Best-effort: a proxy reset / 401 returns None and the card
    stays sparse rather than crashing."""
    import re as _re, json as _json, ssl, asyncio
    import urllib.request
    # Prefer the fresh access_token minted from OrpheusDL's self-refreshing
    # session — the pasted `tidal-token` dies in ~16 h and then every card came
    # back sparse ("track · <id>", no cover). Fall back to the pasted token.
    tok, cc_orph = "", ""
    try:
        from ripster.engines.tidal import _orpheus_access_token
        tok, cc_orph = await _orpheus_access_token()
    except Exception:
        pass
    if not tok:
        tok = str(_cfg.get("tidal-token") or "").strip()
    if not tok:
        return None
    cc = (cc_orph or str(_cfg.get("tidal-country") or "US").strip().upper() or "US")
    m_tr = _re.search(r"/track/(\d+)", url)
    m_al = _re.search(r"/album/(\d+)", url)
    if m_tr:
        kind, _id, typ = "tracks", m_tr.group(1), "track"
    elif m_al:
        kind, _id, typ = "albums", m_al.group(1), "albums"
    else:
        return None

    def _get() -> Optional[dict]:
        api = f"https://api.tidal.com/v1/{kind}/{_id}?countryCode={cc}"
        req = urllib.request.Request(
            api, headers={"Authorization": f"Bearer {tok}", "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=10, context=ssl.create_default_context()) as r:
            return _json.load(r)

    try:
        d = await asyncio.to_thread(_get)
    except Exception:
        return None
    if not d:
        return None
    try:
        from ripster.engines.tidal import _tidal_cover
    except Exception:
        _tidal_cover = lambda u, s=640: ""  # noqa: E731
    artist = (d.get("artist") or {}).get("name") or \
        ", ".join(a.get("name", "") for a in (d.get("artists") or []) if a.get("name"))
    album = d.get("album") or {}
    cover_uuid = album.get("cover") or d.get("cover") or ""
    return {
        "id":         str(_id),
        "type":       typ,
        "title":      d.get("title", ""),
        "artist":     artist,
        "album":      album.get("title", "") if typ == "track" else d.get("title", ""),
        "artworkUrl": _tidal_cover(cover_uuid, 640) if cover_uuid else "",
        "trackCount": d.get("numberOfTracks", 0) if typ == "albums" else 0,
        "isrc":       d.get("isrc", ""),
        "year":       (d.get("releaseDate") or d.get("streamStartDate") or "")[:4],
        "service":    "tidal",
    }


def _sc_hi_res(art: str) -> str:
    """SoundCloud's artwork_url defaults to the 100×100 '-large' variant; swap to
    the 500×500 't500x500' size so cards/covers aren't blurry. No-op if empty."""
    if not art:
        return ""
    return (art.replace("-large.jpg", "-t500x500.jpg")
               .replace("-large.png", "-t500x500.png"))


async def fetch_meta_soundcloud(url: str) -> Optional[dict]:
    """Metadata for a SoundCloud track/playlist URL via api-v2 ``/resolve``.

    SoundCloud has no public metadata API *key*, so the dispatcher used to skip it
    entirely — every SC card came back blank ('soundcloud · <id>', no cover). But
    the player already scrapes a working ``client_id`` (ripster.routes.soundcloud),
    and ``/resolve?url=…`` returns the full track/playlist object (title, user,
    artwork) for any public content. Reuse that here. Best-effort: a failed
    resolve returns None and the card stays sparse rather than crashing."""
    import httpx
    try:
        from ripster.routes.soundcloud import _get_client_id, _API, _UA
    except Exception:
        return None
    cid = await _get_client_id()
    if not cid:
        return None

    async def _resolve(client_id: str):
        async with httpx.AsyncClient(timeout=10, headers={"User-Agent": _UA}) as c:
            return await c.get(f"{_API}/resolve",
                               params={"url": url, "client_id": client_id})

    try:
        r = await _resolve(cid)
        if r.status_code in (401, 403):
            # client_id rotated/expired — re-scrape once and retry.
            cid = await _get_client_id(force=True)
            r = await _resolve(cid)
        if r.status_code != 200:
            return None
        d = r.json()
    except Exception:
        return None

    user   = d.get("user") or {}
    artist = user.get("username", "")
    pub    = d.get("publisher_metadata") or {}
    date   = (d.get("display_date") or d.get("created_at") or "")
    art    = _sc_hi_res(d.get("artwork_url") or "")

    if d.get("kind") == "playlist":
        tracks = d.get("tracks") or []
        if not art and tracks:
            art = _sc_hi_res((tracks[0] or {}).get("artwork_url") or "")
        if not art:
            art = _sc_hi_res(user.get("avatar_url") or "")
        return {
            "id":         str(d.get("id", "")),
            "type":       "album" if d.get("is_album") else "playlist",
            "title":      d.get("title", ""),
            "artist":     pub.get("artist") or artist,
            "album":      d.get("title", ""),
            "artworkUrl": art,
            "trackCount": d.get("track_count") or len(tracks),
            # SoundCloud отдаёт вложенные треки прямо в ответе /resolve;
            # duration у него в миллисекундах.
            "tracks":     [{"num": i,
                            "title": (t or {}).get("title") or "",
                            "artist": ((t or {}).get("user") or {}).get("username") or artist,
                            "dur": int(((t or {}).get("duration") or 0) / 1000)}
                           for i, t in enumerate(tracks, 1)],
            "year":       date[:4],
            "date":       date[:10],
            "genre":      d.get("genre", ""),
            "service":    "soundcloud",
        }

    # default: a single track. SC track titles often embed the artist
    # ("Flume - Never Be Like You"); publisher_metadata.artist is the cleaner
    # value when present, so prefer it over the uploader username.
    if not art:
        art = _sc_hi_res(user.get("avatar_url") or "")
    return {
        "id":         str(d.get("id", "")),
        "type":       "track",
        "title":      d.get("title", ""),
        "artist":     pub.get("artist") or artist,
        "album":      pub.get("album_title", ""),
        "artworkUrl": art,
        "trackCount": 1,
        "isrc":       pub.get("isrc", ""),
        "year":       date[:4],
        "date":       date[:10],
        "genre":      d.get("genre", ""),
        "service":    "soundcloud",
    }


async def fetch_meta_beatport(url: str) -> Optional[dict]:
    """Metadata for a Beatport track/release URL via the authenticated catalog
    API (api.beatport.com/v4). Reuses the access_token minted from OrpheusDL's
    saved Beatport session — Beatport has no public/anonymous metadata API, which
    is why the dispatcher used to skip it and every card came back depersonalised
    ('beatport · <id>', no cover). Best-effort: returns None on any failure."""
    import re as _re, httpx
    try:
        from ripster.engines.orpheus_beatport import _beatport_access_token
    except Exception:
        return None
    tok = await _beatport_access_token()
    if not tok:
        return None

    m_tr = _re.search(r"/track/(?:[^/]+/)?(\d+)", url)
    m_rl = _re.search(r"/release/(?:[^/]+/)?(\d+)", url)
    if m_tr:
        endpoint, _id, typ = f"catalog/tracks/{m_tr.group(1)}", m_tr.group(1), "track"
    elif m_rl:
        endpoint, _id, typ = f"catalog/releases/{m_rl.group(1)}", m_rl.group(1), "album"
    else:
        return None

    api = "https://api.beatport.com/v4/"
    headers = {"user-agent": "libbeatport/v2.8.2", "authorization": f"Bearer {tok}"}
    try:
        # Beatport 301-redirects /tracks/<id> → /tracks/<id>/ (trailing slash);
        # httpx does NOT follow redirects by default (requests does), so without
        # this every call came back 301 and the card stayed blank.
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as c:
            r = await c.get(api + endpoint, headers=headers)
            if r.status_code == 401:
                # Stored token just expired — force a refresh and retry once.
                from ripster.engines.orpheus_beatport import _BP_AT_CACHE
                _BP_AT_CACHE["exp"] = 0.0
                tok = await _beatport_access_token()
                headers["authorization"] = f"Bearer {tok}"
                r = await c.get(api + endpoint, headers=headers)
            if r.status_code != 200:
                return None
            d = r.json()
    except Exception:
        return None

    def _artists(o: dict) -> str:
        return ", ".join(a.get("name", "") for a in (o.get("artists") or []) if a.get("name"))

    def _img(o: dict) -> str:
        uri = (o.get("image") or {}).get("uri", "") if isinstance(o.get("image"), dict) else ""
        # Beatport encodes the size in the path (…/image_size/500x500/…). The
        # release object embedded in a track only carries 500px — bump every card
        # cover to 1400px so it isn't blurry on retina/web.
        return _re.sub(r"/image_size/\d+x\d+/", "/image_size/1400x1400/", uri) if uri else ""

    if typ == "track":
        rel   = d.get("release") or {}
        genre = d.get("genre") or {}
        date  = str(d.get("new_release_date") or d.get("publish_date") or "")
        # Include the mix name so the card reads "Strobe (Layton Giordani Remix)"
        # instead of a bare "Strobe" — Beatport's whole identity is the mix.
        title = d.get("name", "")
        if d.get("mix_name"):
            title = f"{title} ({d['mix_name']})"
        return {
            "id":         str(_id),
            "type":       "track",
            "title":      title,
            "artist":     _artists(d),
            "album":      rel.get("name", ""),
            "artworkUrl": _img(rel) or _img(d),   # release art is 1400px; track art 500px
            "trackCount": 1,
            "isrc":       d.get("isrc", ""),
            "genre":      genre.get("name", "") if isinstance(genre, dict) else "",
            "label":      (rel.get("label") or {}).get("name", "") if isinstance(rel.get("label"), dict) else "",
            "year":       date[:4],
            "date":       date[:10],
            "service":    "beatport",
        }

    # release / album
    date = str(d.get("new_release_date") or d.get("publish_date") or "")
    return {
        "id":         str(_id),
        "type":       "album",
        "title":      d.get("name", ""),
        "artist":     _artists(d),
        "album":      d.get("name", ""),
        "artworkUrl": _img(d),
        "trackCount": d.get("track_count", 0),
        "label":      (d.get("label") or {}).get("name", "") if isinstance(d.get("label"), dict) else "",
        "year":       date[:4],
        "date":       date[:10],
        "service":    "beatport",
    }


async def enrich_meta(task: dict) -> None:
    """Fetch metadata for *task* in-place and broadcast a queue_update."""
    try:
        meta = await fetch_meta_any(task["url"], task.get("service", ""))
    except Exception as exc:
        meta = None
        err_msg = str(exc)
        print(f"[enrich_meta] {task.get('service','')} exception: {err_msg}", flush=True)
        if _broadcast_fn:
            await _broadcast_fn({"type": "log", "text": f"[meta] {err_msg}", "level": "warn"})
        task.setdefault("meta", {})["meta_error"] = err_msg
    if meta:
        # Preserve caller-supplied keys the fetch doesn't return (e.g. tg_user /
        # tg_name from the Telegram bot, or pre-filled cover/source tiles).
        old = task.get("meta") or {}
        task["meta"] = {**old, **meta}
    else:
        task.setdefault("meta", {})["enriched"] = True
    # Треклист добывается из ТОГО ЖЕ ответа, что и карточка, где это возможно
    # (Qobuz, Deezer, Spotify, SoundCloud, Яндекс отдают списки треков сразу) —
    # иначе там, где меты нет, всё равно уезжает отдельный запрос.
    await ensure_tracks(task, broadcast=False)
    if _broadcast_fn and _queue_snapshot:
        await _broadcast_fn({"type": "queue_update", "queue": _queue_snapshot()})


# ── Треклист задачи (дерево в карточке очереди) ───────────────────────────────
# Едет полем задачи, а не только отдельной ручкой: человек должен видеть, ЧТО
# именно качается, в первую же секунду, а не постфактум по тегам скачанных
# файлов. Балласт ограничен: `_MAX_TRACKLIST` записей, в записи только строки и
# секунды — обложек и URL здесь нет намеренно.
_MAX_TRACKLIST = 200


def _compact_tracks(rows) -> list:
    """Любая форма трек-записи (резолвер, мета сервиса, Beatport) → {num,title,artist,dur}.

    Строки с пустым названием СОХРАНЯЮТСЯ: дерево очереди помечает готовность по
    позиции, и выбросив один трек из двенадцати, мы соврём про остальные одиннадцать.
    """
    out: list = []
    for i, t in enumerate(rows or [], 1):
        if not isinstance(t, dict):
            continue
        try:
            num = int(t.get("num") or t.get("track_num") or t.get("trackNumber") or i)
        except (TypeError, ValueError):
            num = i
        try:
            dur = int(t.get("dur") or t.get("duration") or 0)
        except (TypeError, ValueError):
            dur = 0
        title = str(t.get("title") or t.get("name") or "")[:160]
        artist = str(t.get("artist") or "")[:120]
        row = {"num": num, "title": title, "artist": artist}
        if dur:
            row["dur"] = dur
        out.append(row)
        if len(out) >= _MAX_TRACKLIST:
            break
    if not any(r["title"] or r["artist"] for r in out):
        return []
    return out


async def _tracks_from_source(url: str, service: str) -> list:
    """Треклист, которого нет в уже полученной метаданной: общий resolver
    (Apple, Deezer, Qobuz, Tidal, JioSaavn) или учётная ручка Beatport.

    Одиночный трек resolver отдаёт одной записью с пустым названием — это не
    список, а его отсутствие."""
    if not url:
        return []
    if service == "beatport" or "beatport.com" in url:
        import re as _re
        m = _re.search(r"beatport\.com/(?:[a-z]{2}/)?release/[^/]+/(\d+)", url)
        if not m:
            return []
        from ripster.routes.beatport import beatport_release
        rel = await beatport_release(int(m.group(1)))
        rows = []
        for i, x in enumerate((rel or {}).get("tracks") or [], 1):
            title = (x.get("title") or "")
            # Микс — часть названия у Beatport: без него ремиксы неразличимы.
            if x.get("mix"):
                title = f"{title} ({x['mix']})"
            rows.append({"num": i, "title": title, "artist": x.get("artist") or ""})
        return rows
    from ripster import resolver as _rs
    if not _rs._cfg:
        _rs.install(_cfg)
    rows = await _rs.resolve(url)
    return rows if len(rows or []) > 1 else []


async def ensure_tracks(task: dict, *, broadcast: bool = True) -> bool:
    """Поставить на задачу `tracks` — [{num,title,artist,dur}] — если их ещё нет.

    Никогда не бросает: треклист — украшение карточки, а загрузка обязана идти
    и при полностью недоступном API сервиса. Возвращает True, только когда список
    действительно лёг на задачу."""
    if task.get("tracks"):
        return False
    meta = task.get("meta") or {}
    try:
        rows = meta.get("tracks") or []
        # `tracksComplete` ставит сам сервис: Deezer отдаёт в карточке альбома
        # первые 25 треков из сорока, и такой список надо допросить; Apple же
        # теряет пару позиций из-за региона — допрос был бы тем же ответом.
        # Там, где resolver бессилен (Spotify, SoundCloud), честны и те, что есть.
        if not rows or not meta.get("tracksComplete", True):
            rows = await _tracks_from_source(task.get("url") or "",
                                             task.get("service") or "") or rows
        tracks = _compact_tracks(rows)
    except Exception as exc:
        print(f"[tracks] {task.get('service', '')}: {type(exc).__name__}: {exc}",
              flush=True)
        return False
    if not tracks:
        return False
    task["tracks"] = tracks
    meta.pop("tracks", None)          # дубль на канале не нужен
    if broadcast and _broadcast_fn and _queue_snapshot:
        await _broadcast_fn({"type": "queue_update", "queue": _queue_snapshot()})
    return True


__all__ = [
    "install",
    "fetch_meta_any",
    "fetch_meta_apple",
    "fetch_meta_deezer",
    "fetch_meta_qobuz",
    "fetch_meta_spotify",
    "search_mixesdb",
    "fetch_mix_detail",
    "auto_fetch_bearer",
    "enrich_meta",
    "ensure_tracks",
]
