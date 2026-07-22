"""
Discovery routes — search, artist page, album page.

Three services:
  * Apple Music — via public iTunes Search / Lookup API (no auth)
  * Deezer     — via public api.deezer.com (no auth)
  * Qobuz      — via www.qobuz.com/api.json (needs app_id + optional auth-token)

Each endpoint returns a uniform JSON shape so the frontend doesn't care which
service answered. Search returns ``{results: [...]}``, artist pages return
``{artist, releases}``, album pages return ``{album, tracks}``.

Installation: ``install(app, config)`` — attach the router and inject the
app's config dict (used by Qobuz helpers for ``app_id`` and ``auth-token``).
"""
from __future__ import annotations

import time as _time

from ripster import http_client as _HTTP
from fastapi import APIRouter, HTTPException, Request

router = APIRouter()
_config: dict = {}   # populated by install()

_QOBUZ_DEFAULT_APP_ID = "312369995"   # public QobuzDownloaderX-compatible app_id (old 798273057 now 401s); works for search without auth

# ── In-process caches ────────────────────────────────────────────────────────
_sp_app_token:   dict = {"token": "", "expires_at": 0.0}
_sp_album_cache: dict = {}   # album_id         -> (result_dict, expire_ts)
_sp_artist_cache: dict = {}  # "artist_id:types" -> (result_dict, expire_ts)
_SP_ALBUM_TTL  = 1800        # 30 min
_SP_ARTIST_TTL = 900         # 15 min
_sp_rate_limit_until: float = 0.0   # epoch-seconds; Spotify API blocked until this time


def _record_sp_rate_limit(retry_after: int) -> None:
    global _sp_rate_limit_until
    _sp_rate_limit_until = _time.time() + max(retry_after, 5)


def _sp_rate_limit_msg() -> str:
    """Human-readable 'wait until …' string; call only when rate-limited."""
    remaining = int(_sp_rate_limit_until - _time.time())
    if remaining <= 0:
        return "лимит запросов Spotify"
    if remaining < 120:
        return f"лимит запросов Spotify, подожди {remaining}с"
    if remaining < 3600:
        mins = (remaining + 59) // 60
        return f"лимит запросов Spotify, подожди {mins} мин"
    hrs  = remaining // 3600
    mins = (remaining % 3600) // 60
    tail = f" {mins} мин" if mins else ""
    return f"лимит запросов Spotify — блокировка на {hrs} ч{tail} (слишком много запросов к API)"


def _sp_is_rate_limited() -> bool:
    return _time.time() < _sp_rate_limit_until


def install(app, ctx) -> None:
    global _config
    _config = ctx.config
    app.include_router(router)


# ── Lyrics (LRCLIB) ────────────────────────────────────────────────────────
@router.get("/api/lyrics")
async def lyrics(artist: str = "", track: str = "", album: str = "", duration: int = 0):
    """Fetch synced + plain lyrics from lrclib.net. Free, no auth needed.

    Returns {synced: "..lrc text..", plain: "...", source: "lrclib"} or
    {synced: "", plain: "", source: ""} if not found.
    """
    if not artist or not track:
        return {"synced": "", "plain": "", "source": ""}
    try:
        async with _HTTP.ashared() as c:
            params = {"artist_name": artist, "track_name": track}
            if album:    params["album_name"] = album
            if duration: params["duration"]   = str(duration)
            r = await c.get("https://lrclib.net/api/get", params=params)
            if r.status_code == 404:
                # Try the search fallback (returns best match)
                sr = await c.get("https://lrclib.net/api/search",
                                 params={"track_name": track, "artist_name": artist})
                if sr.status_code == 200:
                    arr = sr.json() or []
                    if arr:
                        data = arr[0]
                        return {
                            "synced": data.get("syncedLyrics") or "",
                            "plain":  data.get("plainLyrics")  or "",
                            "source": "lrclib-search",
                        }
                return {"synced": "", "plain": "", "source": ""}
            if r.status_code != 200:
                return {"synced": "", "plain": "", "source": "",
                        "error": f"lrclib {r.status_code}"}
            data = r.json()
            return {
                "synced": data.get("syncedLyrics") or "",
                "plain":  data.get("plainLyrics")  or "",
                "source": "lrclib",
            }
    except Exception as e:
        return {"synced": "", "plain": "", "source": "", "error": str(e)}


# ── Search ────────────────────────────────────────────────────────────────
@router.get("/api/search")
async def api_search(q: str, service: str = "apple", type: str = "album", limit: int = 20, country: str = "",
                     request: Request = None):
    """
    Search across services.
    Apple: iTunes Search API (public, no auth)
    Deezer: Deezer API (public, no auth)
    Qobuz: Qobuz API (needs app_id + secret from settings)
    """
    if not q.strip():
        return {"results": []}

    if service == "apple":
        # iTunes direct (_search_apple): includes 30-sec previewUrl (the zhaarey
        # engine search omitted it → cards had no ▶ play), supports the music-video
        # entity, and is a plain reliable REST call. The zhaarey engine search was
        # a worse duplicate, so Apple search always goes through iTunes now.
        return await _search_apple(q, type, limit, country)
    elif service in ("deezer", "qobuz"):
        # Delegate to engine.search() — engines are the source of truth for
        # service-specific search (BaseSourceAdapter pattern).
        try:
            from ripster.engines import get_engine
            eng = get_engine(service)
            results = await eng.search(q, type, limit, _config)
            if results:
                return {"results": results}
        except (KeyError, Exception):
            pass
        # Fallback to inline implementations if engine search unavailable
        if service == "deezer":
            return await _search_deezer(q, type, limit)
        return await _search_qobuz(q, type, limit)
    elif service == "tidal":
        try:
            from ripster.engines import get_engine
            eng = get_engine("tidal")
            results = await eng.search(q, type, limit, _config)
            if results:
                return {"results": results}
        except (KeyError, Exception):
            pass
        return await _search_tidal(q, type, limit)
    elif service == "spotify":
        return await _search_spotify(q, type, limit)
    elif service == "yandex":
        return await _search_yandex(q, type, limit)
    else:
        return {"results": [], "error": f"Unknown service: {service}"}

_AMP_TYPE = {"album": "albums", "track": "songs", "song": "songs",
             "artist": "artists", "video": "music-videos", "playlist": "playlists"}


def _amp_art(attrs: dict, size: int = 200) -> str:
    art = (attrs.get("artwork") or {}).get("url") or ""
    if not art:
        return ""
    return (art.replace("{w}", str(size)).replace("{h}", str(size))
               .replace("{f}", "jpg").replace("{c}", "bb"))


async def _search_apple_ampapi(q: str, ent: str, limit: int, country: str) -> dict:
    """Live Apple Music catalog search via amp-api. Unlike the iTunes Search
    index, this reflects the catalog immediately, so a brand-new album (whose
    lead singles are the only thing the iTunes index has indexed so far) shows
    up. Needs the developer bearer (+ media-user-token); on any failure the
    caller falls back to the auth-free iTunes path. Returns {"results": [...]}
    (empty on any problem — never raises)."""
    bearer = (_config.get("authorization-token") or "").strip()
    if not bearer or bearer == "your-authorization-token":
        return {"results": []}
    mut  = (_config.get("media-user-token") or "").strip()
    lang = _config.get("language", "en-US")
    cc   = lang.split("-")[-1].upper() if "-" in lang else "US"
    sf   = (country or cc or "US").lower()
    type_key = _AMP_TYPE.get(ent, "albums")
    is_track = ent in ("track", "song", "video")
    headers = {"Authorization": f"Bearer {bearer}", "Origin": "https://music.apple.com"}
    if mut:
        headers["media-user-token"] = mut
    try:
        r = await _HTTP.aclient().get(
            f"https://amp-api.music.apple.com/v1/catalog/{sf}/search",
            params={"term": q, "types": type_key, "limit": min(max(limit, 1), 25),
                    "include[albums]": "artists", "include[songs]": "artists"},
            headers=headers,
        )
        if r.status_code != 200:
            return {"results": []}
        data = ((r.json().get("results") or {}).get(type_key) or {}).get("data") or []
    except Exception:
        return {"results": []}
    out = []
    for it in data:
        a = it.get("attributes") or {}
        full_date = (a.get("releaseDate") or "")[:10]
        previews  = a.get("previews") or []
        artist_rel = (((it.get("relationships") or {}).get("artists") or {}).get("data") or [])
        artist_id  = str(artist_rel[0].get("id", "")) if artist_rel else ""
        out.append({
            "id":      str(it.get("id", "")),
            "title":   a.get("name", ""),
            "artist":  a.get("artistName", ""),
            "artist_id": artist_id,
            "album":   a.get("albumName", "") if is_track else "",
            "type":    ent,
            "url":     a.get("url", ""),
            "cover":   _amp_art(a, 200),
            "year":    full_date[:4],
            "date":    full_date,
            "label":   a.get("recordLabel", "") or a.get("copyright", ""),
            "tracks":  a.get("trackCount"),
            "preview": (previews[0].get("url", "") if previews else ""),
            "service": "apple",
        })
    return {"results": out}


async def _search_apple(q: str, ent: str, limit: int, country: str) -> dict:
    """Apple search: live catalog (amp-api) first, iTunes Search index as
    fallback. amp-api surfaces fresh releases the iTunes index hasn't picked up
    yet (the "new album missing, only its singles show" symptom); iTunes is the
    auth-free safety net when the bearer is absent/expired."""
    amp = await _search_apple_ampapi(q, ent, limit, country)
    if amp.get("results"):
        return {"results": amp["results"][: max(limit * 2, 24)]}
    return await _search_apple_itunes(q, ent, limit, country)


async def _search_apple_itunes(q: str, ent: str, limit: int, country: str) -> dict:
    """iTunes Search API — completely public, no auth.

    Searches the account region AND New Zealand (where releases often go live
    first) and merges, so pre-releases not yet available in the account region
    still appear. Each result's URL carries its own region storefront, so the
    download path pulls it from wherever it's streamable."""
    import asyncio
    entity_map = {"album":"album","track":"song","artist":"musicArtist",
                  "playlist":"playlist","video":"musicVideo"}
    entity = entity_map.get(ent, "album")
    media  = "musicVideo" if entity == "musicVideo" else "music"
    lang = _config.get("language", "en-US")
    _cc = lang.split("-")[-1].upper() if "-" in lang else "US"
    sf = (country or _cc or "US").lower()
    regions = [sf] + (["nz"] if sf != "nz" else [])

    _is_track = ent in ("track", "song", "video")
    def _map(item):
        full_date = (item.get("releaseDate") or "")[:10]
        if _is_track:
            # TRACK search → identify by the SONG, not its album. Using collection*
            # here showed album names as "tracks" and made the download grab the
            # whole album / "some track from it".
            _id    = str(item.get("trackId") or item.get("collectionId") or item.get("artistId",""))
            _title = item.get("trackName") or item.get("collectionName") or item.get("artistName","")
            _url   = item.get("trackViewUrl") or item.get("collectionViewUrl") or item.get("artistViewUrl","")
        else:
            _id    = str(item.get("collectionId") or item.get("artistId") or item.get("trackId",""))
            _title = item.get("collectionName") or item.get("trackName") or item.get("artistName","")
            _url   = item.get("collectionViewUrl") or item.get("trackViewUrl") or item.get("artistViewUrl","")
        return {
            "id":       _id,
            "title":    _title,
            "artist":   item.get("artistName",""),
            "artist_id": str(item.get("artistId","")),
            "album":    item.get("collectionName","") if _is_track else "",
            "type":     ent,
            "url":      _url,
            "cover":    (item.get("artworkUrl100") or "").replace("100x100","200x200"),
            "year":     full_date[:4],
            "date":     full_date,
            "label":    item.get("copyright", ""),
            "tracks":   item.get("trackCount"),
            "preview":  item.get("previewUrl", ""),
            "service":  "apple",
        }

    async def _one(cc):
        try:
            r = await _HTTP.aclient().get("https://itunes.apple.com/search", params={
                "term": q, "entity": entity, "limit": limit, "country": cc, "media": media
            })
            return [_map(x) for x in (r.json().get("results") or [])]
        except Exception:
            return []

    try:
        batches = await asyncio.gather(*[_one(cc) for cc in regions])
        seen, merged = set(), []
        for batch in batches:                     # account region first, then NZ-only extras
            for it in batch:
                k = it["id"] or it["url"]
                if not k or k in seen:
                    continue
                seen.add(k)
                merged.append(it)
        return {"results": merged[: max(limit * 2, 24)]}
    except Exception as e:
        return {"results": [], "error": str(e)}

# ── Yandex Music search (via the yandex_music library — sync, run in threadpool) ──
_ym_client = None        # cached Client; re-init only when the token changes
_ym_client_token = ""


def _ym_cover(uri: str, size: str = "400x400") -> str:
    if not uri:
        return ""
    u = uri.replace("%%", size)
    return u if u.startswith("http") else ("https://" + u)


async def _search_yandex(q: str, ent: str, limit: int) -> dict:
    token = (_config.get("yandex-token") or "").strip()
    if not token:
        return {"results": [], "error": "Укажи токен Яндекса в Settings → Яндекс"}
    from fastapi.concurrency import run_in_threadpool

    def _do():
        global _ym_client, _ym_client_token
        from yandex_music import Client
        if _ym_client is None or _ym_client_token != token:
            _ym_client = Client(token).init()
            _ym_client_token = token
        t = {"album": "album", "track": "track", "artist": "artist",
             "playlist": "playlist"}.get(ent, "album")
        res = _ym_client.search(q, type_=t)
        out: list[dict] = []
        if t == "album" and res and res.albums:
            for a in (res.albums.results or [])[:limit]:
                out.append({
                    "id": str(a.id), "title": a.title or "",
                    "artist": ", ".join(ar.name for ar in (a.artists or [])),
                    "type": "album", "url": f"https://music.yandex.ru/album/{a.id}",
                    "cover": _ym_cover(a.cover_uri), "year": str(a.year or ""),
                    "tracks": a.track_count, "service": "yandex",
                })
        elif t == "track" and res and res.tracks:
            for tr in (res.tracks.results or [])[:limit]:
                alb = (tr.albums or [None])[0]
                aid = alb.id if alb else ""
                out.append({
                    "id": str(tr.id), "title": tr.title or "",
                    "artist": ", ".join(ar.name for ar in (tr.artists or [])),
                    "type": "track",
                    "url": f"https://music.yandex.ru/album/{aid}/track/{tr.id}",
                    "cover": _ym_cover(tr.cover_uri), "service": "yandex",
                })
        elif t == "artist" and res and res.artists:
            for ar in (res.artists.results or [])[:limit]:
                cov = getattr(ar, "cover", None)
                out.append({
                    "id": str(ar.id), "title": ar.name or "", "artist": ar.name or "",
                    "type": "artist", "url": f"https://music.yandex.ru/artist/{ar.id}",
                    "cover": _ym_cover(getattr(cov, "uri", "") if cov else ""),
                    "service": "yandex",
                })
        return out

    try:
        results = await run_in_threadpool(_do)
        return {"results": results}
    except Exception as e:
        return {"results": [], "error": f"Yandex: {e}"}


async def _search_deezer(q: str, ent: str, limit: int) -> dict:
    """Deezer API — completely public, no auth needed."""
    ent_map = {"album":"album","track":"track","artist":"artist","playlist":"playlist"}
    ep = ent_map.get(ent, "album")
    try:
        async with _HTTP.ashared() as c:
            r = await c.get(f"https://api.deezer.com/search/{ep}", params={"q": q, "limit": limit})
            data = r.json()
        results = []
        for item in data.get("data") or []:
            if ep == "album":
                results.append({
                    "id":     str(item.get("id","")),
                    "title":  item.get("title",""),
                    "artist": (item.get("artist") or {}).get("name",""),
                    "artist_id": str((item.get("artist") or {}).get("id","")),
                    "type":   ent,
                    "url":    item.get("link",""),
                    "cover":  item.get("cover_medium","") or item.get("cover_big","") or item.get("cover",""),
                    "year":   (item.get("release_date") or "")[:4],
                    "date":   item.get("release_date",""),
                    "label":  item.get("label",""),
                    "tracks": item.get("nb_tracks"),
                    "service":"deezer",
                })
            elif ep == "track":
                results.append({
                    "id":     str(item.get("id","")),
                    "title":  item.get("title",""),
                    "artist": (item.get("artist") or {}).get("name",""),
                    "artist_id": str((item.get("artist") or {}).get("id","")),
                    "type":   ent,
                    "url":    item.get("link",""),
                    "cover":  (item.get("album") or {}).get("cover_medium","") or (item.get("album") or {}).get("cover_small","") or (item.get("album") or {}).get("cover",""),
                    "service":"deezer",
                })
            elif ep == "artist":
                results.append({
                    "id":     str(item.get("id","")),
                    "title":  item.get("name",""),
                    "artist": item.get("name",""),
                    "type":   ent,
                    "url":    item.get("link",""),
                    "cover":  item.get("picture_medium",""),
                    "service":"deezer",
                })
        return {"results": results}
    except Exception as e:
        return {"results": [], "error": str(e)}

async def _search_qobuz(q: str, ent: str, limit: int) -> dict:
    """Qobuz search.

    Uses the public ``app_id`` that streamrip also defaults to when none is
    set — this makes search work out of the box, no Settings required.
    The user can override via ``qobuz-app-id`` if the public one gets revoked.

    The ``X-User-Auth-Token`` header is only sent if the user has a
    ``qobuz-auth-token`` — without it we get non-personalised results, which
    is fine for search. (Key name note: the setting is ``qobuz-auth-token``,
    not ``qobuz-token`` — the latter was a typo that never picked anything up.)
    """
    app_id = (_config.get("qobuz-app-id") or "").strip() or _QOBUZ_DEFAULT_APP_ID
    token  = (_config.get("qobuz-auth-token") or "").strip()
    # URL path uses singular form; response JSON uses plural as top-level key.
    ep_url_map = {"album": "album",  "track": "track",  "artist": "artist"}
    ep_key_map = {"album": "albums", "track": "tracks", "artist": "artists"}
    ep_url = ep_url_map.get(ent, "album")
    ep_key = ep_key_map.get(ent, "albums")
    try:
        async with _HTTP.ashared() as c:
            r = await c.get(f"https://www.qobuz.com/api.json/0.2/{ep_url}/search", params={
                "query": q, "limit": limit, "app_id": app_id
            }, headers={"X-User-Auth-Token": token} if token else {})
            data = r.json()
        results = []
        items = (data.get(ep_key) or {}).get("items") or []
        for item in items:
            full_date = (item.get("release_date_original") or "")
            # Qobuz returns album covers at `item.image` for album/artist hits
            # but only at `item.album.image` for TRACK hits. Try both.
            img_obj = item.get("image") if isinstance(item.get("image"), dict) else None
            if not img_obj:
                alb_img = (item.get("album") or {}).get("image") if isinstance(item.get("album"), dict) else None
                img_obj = alb_img if isinstance(alb_img, dict) else {}
            cover_url = img_obj.get("large", "") or img_obj.get("small", "")
            _alb = item.get("album") if isinstance(item.get("album"), dict) else {}
            _artist_obj = (item.get("artist") if isinstance(item.get("artist"), dict) else None) \
                or (item.get("performer") if isinstance(item.get("performer"), dict) else None) \
                or (_alb.get("artist") if isinstance(_alb.get("artist"), dict) else None) or {}
            results.append({
                "id":     str(item.get("id","")),
                "title":  item.get("title") or item.get("name",""),
                # tracks carry the artist in `performer`, not `artist`
                "artist": _artist_obj.get("name",""),
                "artist_id": str(_artist_obj.get("id","")),
                "type":   ent,
                "url":    item.get("url","") or f"https://www.qobuz.com/album/{item.get('id','')}",
                "cover":  cover_url,
                "year":   full_date[:4],
                "date":   full_date,
                "label":  (item.get("label") or {}).get("name","") if isinstance(item.get("label"), dict) else "",
                "hires":  item.get("hires", False),
                "tracks": item.get("tracks_count"),
                "service":"qobuz",
            })
        return {"results": results}
    except Exception as e:
        return {"results": [], "error": str(e)}

# ── Artist / Album detail pages ───────────────────────────────────────────
# These power the "artist page" UI (future iteration). They unify response
# shape across Apple / Deezer / Qobuz so the frontend doesn't have to care
# which service it's talking to.

_ENGINE_SERVICES = {"apple": "zhaarey", "deezer": "deezer", "qobuz": "qobuz",
                    "tidal": "tidal", "spotify": "spotify"}


@router.get("/api/artist/{service}/{artist_id}")
async def api_artist_detail(service: str, artist_id: str, types: str = "album,single,compilation"):
    """Return artist info + their releases, filtered by types."""
    eng_name = _ENGINE_SERVICES.get(service)
    if not eng_name:
        raise HTTPException(400, f"Unsupported service: {service}")
    from ripster.engines import get_engine
    return await get_engine(eng_name).get_artist(artist_id, types, _config)


@router.get("/api/album/{service}/{album_id}")
async def api_album_detail(service: str, album_id: str):
    """Return album metadata + full track list with preview URLs (if any)."""
    eng_name = _ENGINE_SERVICES.get(service)
    if not eng_name:
        raise HTTPException(400, f"Unsupported service: {service}")
    from ripster.engines import get_engine
    return await get_engine(eng_name).get_album(album_id, _config)


_RE_ALBUM_ID = {
    "spotify": _time.__class__,  # placeholder, real regex set below
}

import re as _re_mod
_RE_SP_ALBUM = _re_mod.compile(r'open\.spotify\.com/album/([A-Za-z0-9]+)')
_RE_QB_ALBUM = _re_mod.compile(r'qobuz\.com/(?:[a-z-]+/)?album/[^/]+/([A-Za-z0-9]+)')
_RE_TD_ALBUM = _re_mod.compile(r'tidal\.com/(?:browse/)?album/(\d+)')
_RE_DZ_ALBUM = _re_mod.compile(r'deezer\.com/(?:[a-z]+/)?album/(\d+)')
_RE_AP_ALBUM = _re_mod.compile(r'music\.apple\.com/(?:[a-z]+/)?album/[^/]+/(\d+)')


@router.get("/api/release/expand")
async def api_release_expand(service: str, url: str):
    """Expand a release URL → {album, tracks} via the appropriate engine.

    Used by the Releases tab's ▶ play button. Returns the same shape as
    /api/album/{service}/{id} so the frontend can reuse handlers.
    """
    service = (service or "").lower().strip()
    url = (url or "").strip()
    if not url:
        raise HTTPException(400, "Missing url")

    # SoundCloud has no numeric album_id in its URLs (soundcloud.com/user/sets/…),
    # so resolve the URL to its object, then reuse the playlist expander.
    if service == "soundcloud":
        from ripster.routes.soundcloud import (
            _get_client_id, _API, _UA, _norm_track, _artwork, sc_playlist,
        )
        cid = await _get_client_id()
        if not cid:
            raise HTTPException(400, "Не удалось получить client_id SoundCloud")
        try:
            async with _HTTP.ashared() as c:
                rr = await c.get(f"{_API}/resolve", params={"url": url, "client_id": cid})
                if rr.status_code in (401, 403):
                    cid = await _get_client_id(force=True)
                    rr = await c.get(f"{_API}/resolve", params={"url": url, "client_id": cid})
                if rr.status_code != 200:
                    raise HTTPException(404, f"SoundCloud: не найдено ({rr.status_code})")
                obj = rr.json()
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(502, f"SoundCloud resolve error: {e}")
        kind = obj.get("kind")
        if kind == "playlist":
            return await sc_playlist(str(obj.get("id")))
        if kind == "track":
            return {"ok": True, "title": obj.get("title", ""),
                    "artist": (obj.get("user") or {}).get("username", ""),
                    "artwork": _artwork(obj.get("artwork_url") or "", "original"),
                    "tracks": [_norm_track(obj)]}
        raise HTTPException(400, f"SoundCloud URL не плейлист/трек (kind={kind})")

    rx = {
        "spotify": _RE_SP_ALBUM, "qobuz": _RE_QB_ALBUM,
        "tidal":   _RE_TD_ALBUM, "deezer": _RE_DZ_ALBUM,
        "apple":   _RE_AP_ALBUM,
    }.get(service)
    if not rx:
        raise HTTPException(400, f"Unsupported service: {service}")
    m = rx.search(url)
    if not m:
        raise HTTPException(400, f"Не удалось извлечь album_id из URL ({service})")
    album_id = m.group(1)
    eng_name = _ENGINE_SERVICES.get(service)
    if not eng_name:
        raise HTTPException(400, f"Unsupported service: {service}")
    from ripster.engines import get_engine
    d = await get_engine(eng_name).get_album(album_id, _config)
    if not isinstance(d, dict):
        raise HTTPException(502, "Bad engine response")
    if d.get("error"):
        raise HTTPException(502, d["error"])
    if service == "spotify":
        await _attach_playable_sources(d.get("tracks") or [], (d.get("album") or {}).get("upc", ""))
    return {"ok": True, **d}


async def _attach_playable_sources(tracks: list, upc: str) -> None:
    """Spotify has no /api/stream proxy — streaming its own audio through our
    backend would risk the account's token getting rate-limited/banned. Resolve
    each track to the EXACT same physical copy on Deezer via the album's UPC
    (barcode) — not fuzzy title/artist matching. Per-track ISRC would be the
    usual cross-service key, but Spotify's /v1/tracks batch endpoint 403s for
    this app's credentials; the album UPC is already in hand from the album
    fetch (no extra request) and is just as exact: same barcode = same
    physical release, so matching by (disc, track_no) inside that release is
    deterministic — never "similar", only the same track. Mutates each track
    dict in place with playable_service/playable_id when a match is found; the
    frontend keeps showing "Spotify" — it never surfaces the real source."""
    upc = "".join(ch for ch in (upc or "") if ch.isdigit())
    if not upc or not tracks:
        return
    try:
        async with _HTTP.ashared() as c:
            # Deezer stores 12-digit UPC-A; Spotify's external_ids.upc is often
            # a 13-digit EAN-13 with a leading zero pad — strip it and retry.
            candidates = [upc] if len(upc) <= 12 else [upc.lstrip("0") or upc, upc]
            alb = None
            for cand in dict.fromkeys(candidates):
                r = await c.get(f"https://api.deezer.com/album/upc:{cand}")
                if r.status_code == 200:
                    j = r.json()
                    if not j.get("error") and j.get("id"):
                        alb = j
                        break
            if not alb:
                return
            tr = await c.get(f"https://api.deezer.com/album/{alb['id']}/tracks", params={"limit": 200})
            if tr.status_code != 200:
                return
            dz_tracks = (tr.json() or {}).get("data") or []
    except Exception:
        return

    by_pos = {(t.get("disk_number") or 1, t.get("track_position")): t
              for t in dz_tracks if t.get("track_position") and t.get("id")}
    for t in tracks:
        if not isinstance(t, dict):
            continue
        hit = by_pos.get((t.get("disc") or 1, t.get("track_no")))
        if hit:
            t["playable_service"] = "deezer"
            t["playable_id"] = str(hit["id"])

# ── Tidal (authenticated API — needs tidal-token in config) ──────────────

_TIDAL_API = "https://api.tidal.com/v1"


def _tidal_headers() -> dict | None:
    token = (_config.get("tidal-token") or "").strip()
    if not token:
        return None
    return {"Authorization": f"Bearer {token}"}


def _tidal_country() -> str:
    return (_config.get("tidal-country") or "US").strip().upper() or "US"


def _tidal_cover(uuid: str, size: int = 160) -> str:
    """Convert Tidal's UUID cover format to CDN URL."""
    if not uuid:
        return ""
    return f"https://resources.tidal.com/images/{uuid.replace('-', '/')}/{size}x{size}.jpg"


async def _search_tidal(q: str, ent: str, limit: int) -> dict:
    hdr = _tidal_headers()
    if not hdr:
        return {"results": [], "error": "Tidal access_token не настроен. Добавь в Settings → Tidal."}
    cc = _tidal_country()
    type_map = {"album": "albums", "track": "tracks", "artist": "artists"}
    t_type = type_map.get(ent, "albums")
    try:
        async with _HTTP.ashared() as c:
            r = await c.get(f"{_TIDAL_API}/search/{t_type}", headers=hdr,
                            params={"query": q, "limit": limit, "countryCode": cc})
            if r.status_code == 401:
                return {"results": [], "error": "Tidal: токен истёк. Обнови access_token в Settings → Tidal."}
            data = r.json()
        results = []
        for item in data.get("items") or []:
            if t_type == "albums":
                alb_id = str(item.get("id", ""))
                results.append({
                    "id":      alb_id,
                    "title":   item.get("title", ""),
                    "artist":  (item.get("artist") or {}).get("name", ""),
                    "artist_id": str((item.get("artist") or {}).get("id", "")),
                    "type":    ent,
                    "url":     f"https://listen.tidal.com/album/{alb_id}",
                    "cover":   _tidal_cover(item.get("cover", "")),
                    "year":    (item.get("releaseDate") or "")[:4],
                    "tracks":  item.get("numberOfTracks"),
                    "service": "tidal",
                })
            elif t_type == "tracks":
                tr_id = str(item.get("id", ""))
                results.append({
                    "id":      tr_id,
                    "title":   item.get("title", ""),
                    "artist":  (item.get("artist") or {}).get("name", ""),
                    "artist_id": str((item.get("artist") or {}).get("id", "")),
                    "type":    ent,
                    "url":     f"https://listen.tidal.com/track/{tr_id}",
                    "cover":   _tidal_cover((item.get("album") or {}).get("cover", "")),
                    "service": "tidal",
                })
            elif t_type == "artists":
                art_id = str(item.get("id", ""))
                results.append({
                    "id":      art_id,
                    "title":   item.get("name", ""),
                    "artist":  item.get("name", ""),
                    "type":    ent,
                    "url":     f"https://listen.tidal.com/artist/{art_id}",
                    "cover":   _tidal_cover(item.get("picture", "")),
                    "service": "tidal",
                })
        return {"results": results}
    except Exception as e:
        return {"results": [], "error": str(e)}


# ── Spotify (Web API — needs client_id + client_secret from config) ──────────

async def _get_spotify_app_token() -> str:
    now = _time.time()
    if _sp_app_token["token"] and now < _sp_app_token["expires_at"] - 60:
        return _sp_app_token["token"]
    cid    = (_config.get("spotify-client-id")     or "").strip()
    secret = (_config.get("spotify-client-secret") or "").strip()
    if not cid or not secret:
        return ""
    import base64
    creds = base64.b64encode(f"{cid}:{secret}".encode()).decode()
    try:
        async with _HTTP.ashared() as c:
            r = await c.post(
                "https://accounts.spotify.com/api/token",
                headers={"Authorization": f"Basic {creds}",
                         "Content-Type": "application/x-www-form-urlencoded"},
                data={"grant_type": "client_credentials"},
            )
            if r.status_code != 200:
                return ""
            data = r.json()
            _sp_app_token["token"]      = data.get("access_token", "")
            _sp_app_token["expires_at"] = now + data.get("expires_in", 3600)
            return _sp_app_token["token"]
    except Exception:
        return ""


def _sp_cover(images: list) -> str:
    # Spotify returns images largest-first (~640px). Cards only need ~300px — use
    # the medium image to cut search-result traffic/load time.
    if not images:
        return ""
    pick = images[1] if len(images) >= 2 else images[0]
    return pick.get("url", "")


async def _search_spotify(q: str, ent: str, limit: int) -> dict:
    if _sp_is_rate_limited():
        return {"results": [], "error": _sp_rate_limit_msg()}
    token = await _get_spotify_app_token()
    if not token:
        return {"results": [], "error": "Spotify API не настроен — добавь Client ID/Secret в Settings → Spotify"}
    type_map = {"album": "album", "track": "track", "artist": "artist"}
    sp_type  = type_map.get(ent, "album")
    try:
        async with _HTTP.ashared() as c:
            r = await c.get("https://api.spotify.com/v1/search",
                            headers={"Authorization": f"Bearer {token}"},
                            # Found 2026-07-22: this client_id/app returns a
                            # flat 400 "Invalid limit" for any limit > 10 on
                            # /v1/search — NOT the documented 1-50 range,
                            # presumably a quota-tier restriction Spotify
                            # applies to this app. Silently broke ALL Spotify
                            # search results (empty array, no visible error)
                            # since the caller's default limit=20 always hit
                            # it. Clamp instead of trusting the docs.
                            params={"q": q, "type": sp_type, "limit": min(max(limit, 1), 10)})
            if r.status_code == 401:
                _sp_app_token["token"] = ""
                return {"results": [], "error": "Spotify: токен истёк, попробуй снова"}
            if r.status_code == 429:
                _record_sp_rate_limit(int(r.headers.get("Retry-After", 30)))
                return {"results": [], "error": _sp_rate_limit_msg()}
            if not r.content:
                return {"results": [], "error": f"Spotify API: HTTP {r.status_code}"}
            data = r.json()
            # Any other non-2xx (e.g. the "Invalid limit" 400 above, before it was
            # clamped) must surface as an error, not a silent empty result — that
            # silence is exactly what let the limit bug above go unnoticed.
            if r.status_code >= 300:
                return {"results": [], "error": f"Spotify API: HTTP {r.status_code} — "
                                                f"{(data.get('error') or {}).get('message', data)}"}
        results = []
        if sp_type == "album":
            for item in (data.get("albums") or {}).get("items") or []:
                if not item:
                    continue
                results.append({
                    "id":     item["id"],
                    "title":  item.get("name", ""),
                    "artist": ", ".join(a.get("name", "") for a in item.get("artists") or []),
                    "type":   ent,
                    "url":    item.get("external_urls", {}).get("spotify",
                              f"https://open.spotify.com/album/{item['id']}"),
                    "cover":  _sp_cover(item.get("images")),
                    "year":   (item.get("release_date") or "")[:4],
                    "date":   item.get("release_date", ""),
                    "tracks": item.get("total_tracks"),
                    "service": "spotify",
                })
        elif sp_type == "track":
            for item in (data.get("tracks") or {}).get("items") or []:
                if not item:
                    continue
                results.append({
                    "id":      item["id"],
                    "title":   item.get("name", ""),
                    "artist":  ", ".join(a.get("name", "") for a in item.get("artists") or []),
                    "type":    ent,
                    "url":     item.get("external_urls", {}).get("spotify",
                               f"https://open.spotify.com/track/{item['id']}"),
                    "cover":   _sp_cover((item.get("album") or {}).get("images")),
                    "preview": item.get("preview_url", ""),
                    "service": "spotify",
                })
        elif sp_type == "artist":
            for item in (data.get("artists") or {}).get("items") or []:
                if not item:
                    continue
                results.append({
                    "id":     item["id"],
                    "title":  item.get("name", ""),
                    "artist": item.get("name", ""),
                    "type":   ent,
                    "url":    item.get("external_urls", {}).get("spotify",
                              f"https://open.spotify.com/artist/{item['id']}"),
                    "cover":  _sp_cover(item.get("images")),
                    "service": "spotify",
                })
        return {"results": results}
    except Exception as e:
        return {"results": [], "error": str(e)}


async def _artist_spotify(artist_id: str, types: str) -> dict:
    cache_key = f"{artist_id}:{types}"
    now = _time.time()
    cached, exp = _sp_artist_cache.get(cache_key, (None, 0))
    if cached and now < exp:
        return cached
    if _sp_is_rate_limited():
        if cached: return cached
        return {"error": _sp_rate_limit_msg(), "releases": []}

    token = await _get_spotify_app_token()
    if not token:
        return {"error": "Spotify API не настроен", "releases": []}
    wanted = {t.strip() for t in types.split(",") if t.strip()} if types else set()
    try:
        async with _HTTP.ashared() as c:
            info_r = await c.get(f"https://api.spotify.com/v1/artists/{artist_id}",
                                 headers={"Authorization": f"Bearer {token}"})
            if info_r.status_code == 401:
                _sp_app_token["token"] = ""
                return {"error": "Spotify: токен истёк, попробуй снова", "releases": []}
            if info_r.status_code == 429:
                _record_sp_rate_limit(int(info_r.headers.get("Retry-After", 30)))
                if cached: return cached
                return {"error": _sp_rate_limit_msg(), "releases": []}
            if not info_r.content:
                return {"error": "Spotify API: пустой ответ", "releases": []}
            info = info_r.json()
            alb_r = await c.get(f"https://api.spotify.com/v1/artists/{artist_id}/albums",
                                headers={"Authorization": f"Bearer {token}"},
                                # Same quota-tier limit>10 → 400 as /v1/search, see there.
                                params={"limit": 10, "include_groups": "album,single,compilation"})
            if alb_r.status_code == 429:
                _record_sp_rate_limit(int(alb_r.headers.get("Retry-After", 30)))
                if cached: return cached
                return {"error": _sp_rate_limit_msg(), "releases": []}
            if not alb_r.content:
                return {"error": "Spotify API: пустой ответ", "releases": []}
            alb_data = alb_r.json()
        type_map = {"album": "album", "single": "single", "compilation": "compilation", "appears_on": "appears_on"}
        releases = []
        for a in alb_data.get("items") or []:
            rtype = type_map.get(a.get("album_group") or a.get("album_type", "album"), "album")
            if wanted and rtype not in wanted:
                continue
            releases.append({
                "id":     a["id"],
                "title":  a.get("name", ""),
                "cover":  _sp_cover(a.get("images")),
                "year":   (a.get("release_date") or "")[:4],
                "date":   a.get("release_date", ""),
                "tracks": a.get("total_tracks"),
                "type":   rtype,
                "url":    a.get("external_urls", {}).get("spotify",
                          f"https://open.spotify.com/album/{a['id']}"),
                "service": "spotify",
            })
        releases.sort(key=lambda r: r.get("date", ""), reverse=True)
        result = {
            "artist": {
                "id":      artist_id,
                "name":    info.get("name", ""),
                "picture": _sp_cover(info.get("images")),
                "fans":    (info.get("followers") or {}).get("total"),
                "url":     info.get("external_urls", {}).get("spotify",
                           f"https://open.spotify.com/artist/{artist_id}"),
                "service": "spotify",
            },
            "releases": releases,
        }
        _sp_artist_cache[cache_key] = (result, now + _SP_ARTIST_TTL)
        return result
    except Exception as e:
        return {"error": str(e), "releases": []}


async def _album_spotify(album_id: str) -> dict:
    now = _time.time()
    cached, exp = _sp_album_cache.get(album_id, (None, 0))
    if cached and now < exp:
        return cached
    if _sp_is_rate_limited():
        if cached: return cached
        return {"error": _sp_rate_limit_msg()}

    token = await _get_spotify_app_token()
    if not token:
        return {"error": "Spotify API не настроен"}
    try:
        async with _HTTP.ashared() as c:
            r = await c.get(f"https://api.spotify.com/v1/albums/{album_id}",
                            headers={"Authorization": f"Bearer {token}"})
            if r.status_code == 401:
                _sp_app_token["token"] = ""
                return {"error": "Spotify: токен истёк, попробуй снова"}
            if r.status_code == 429:
                _record_sp_rate_limit(int(r.headers.get("Retry-After", 10)))
                if cached:
                    return cached
                return {"error": _sp_rate_limit_msg()}
            if not r.content:
                return {"error": f"Spotify API: пустой ответ (HTTP {r.status_code})"}
            a = r.json()
            if a.get("error"):
                return {"error": f"Spotify: {a['error'].get('message', str(a['error']))}"}
        tracks = []
        for t in (a.get("tracks") or {}).get("items") or []:
            tracks.append({
                "id":       t["id"],
                "title":    t.get("name", ""),
                "artist":   ", ".join(ar.get("name", "") for ar in t.get("artists") or []),
                "duration": (t.get("duration_ms") or 0) // 1000,
                "track_no": t.get("track_number"),
                "disc":     t.get("disc_number"),
                "preview":  t.get("preview_url") or "",
                "explicit": t.get("explicit", False),
                "url":      t.get("external_urls", {}).get("spotify",
                            f"https://open.spotify.com/track/{t['id']}"),
            })
        real_id = a.get("id", album_id)
        result = {
            "album": {
                "id":     real_id,
                "title":  a.get("name", ""),
                "artist": ", ".join(ar.get("name", "") for ar in a.get("artists") or []),
                "cover":  _sp_cover(a.get("images")),
                "year":   (a.get("release_date") or "")[:4],
                "date":   a.get("release_date", ""),
                "label":  a.get("label", ""),
                "upc":    (a.get("external_ids") or {}).get("upc", ""),
                "tracks": a.get("total_tracks"),
                "url":    a.get("external_urls", {}).get("spotify",
                          f"https://open.spotify.com/album/{real_id}"),
                "service": "spotify",
            },
            "tracks": tracks,
        }
        _sp_album_cache[album_id] = (result, now + _SP_ALBUM_TTL)
        return result
    except Exception as e:
        return {"error": str(e)}


# ── ISRC upgrade: find a Spotify track on lossless services ──────────────────

_ARTIST_STOPWORDS = frozenset({
    "the", "a", "an", "feat", "ft", "featuring", "and", "with", "x", "vs",
    "of", "de", "la", "le", "los", "las", "el", "y", "e", "di", "das", "der", "und",
})


def _artist_tokens(s: str) -> set:
    import re
    toks = re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).split()
    return {t for t in toks if t and t not in _ARTIST_STOPWORDS}


def _same_artist(src: str, cand: str) -> bool:
    """True when a fuzzy candidate plausibly belongs to the source artist.

    Lenient on purpose: a single shared significant token (stopwords like
    'the'/'feat' dropped) is enough, so feat./collab/transliteration variants
    still pass — while a same-titled release by a wholly different artist
    ("New Love" by Kloyd vs Ziggy Alberts) is rejected. When the source artist
    is unknown (Spotify Web API geo-blocked → only the oembed title survived)
    we cannot verify, so we ALLOW rather than block every result."""
    st = _artist_tokens(src)
    if not st:
        return True            # no source artist to compare against → don't guard
    ct = _artist_tokens(cand)
    if not ct:
        return False           # we know the artist, candidate has none → unconfirmed
    return bool(st & ct)


async def _resolve_spotify_album(sp_url: str) -> dict:
    """Resolve a Spotify ALBUM to the same release on lossless services: Deezer &
    Apple exact via UPC barcode, Qobuz/Tidal/Yandex via name+artist (Qobuz also
    tries UPC). Best 1 candidate per service. Mirrors the track resolver so the
    bot's convert-first picker works for full releases, not just single tracks."""
    alb_id = sp_url.split("/album/")[-1].split("?")[0].strip()
    upc = name = artist = ""
    token = await _get_spotify_app_token()
    if not token:
        try:
            from ripster.engines.orpheus_spotify import _creds_path as _osp_creds
            import json as _json
            p = _osp_creds()
            if p.exists():
                token = _json.loads(p.read_text(encoding="utf-8")).get("access_token", "")
        except Exception:
            pass
    if token:
        try:
            async with _HTTP.ashared() as c:
                r = await c.get(f"https://api.spotify.com/v1/albums/{alb_id}",
                                headers={"Authorization": f"Bearer {token}"})
                if r.status_code == 200:
                    ad = r.json()
                    upc    = (ad.get("external_ids") or {}).get("upc", "") or ""
                    name   = ad.get("name", "") or ""
                    artist = ", ".join(a.get("name", "") for a in (ad.get("artists") or [])) or ""
        except Exception:
            pass
    # Spotify Web API geo-blocks some regions (403 "Spotify is unavailable in
    # this country" — e.g. behind a RU VPN), leaving name+upc empty and making the
    # convert picker wrongly report "no lossless analog". Fall back to the PUBLIC
    # oembed endpoint (no auth, not geo-blocked) for at least the release name, so
    # the fuzzy lossless search still runs.
    if not name:
        try:
            async with _HTTP.ashared() as c:
                r = await c.get("https://open.spotify.com/oembed", params={"url": sp_url})
                if r.status_code == 200:
                    name = (r.json().get("title") or "").strip()
        except Exception:
            pass

    if not name and not upc:
        return {"results": [], "upc": ""}

    candidates: dict[str, dict] = {}

    def _norm(s: str) -> str:
        return "".join(ch for ch in (s or "").lower() if ch.isalnum())

    _nn = _norm(name)

    def _title_matches(cand_title: str) -> bool:
        ct = _norm(cand_title)
        return bool(ct and _nn and (ct == _nn or ct in _nn or _nn in ct))

    def _add(entry: dict) -> None:
        if not entry.get("url"):
            return
        # A FUZZY album match (search by artist+name) can return a DIFFERENT
        # album by the same artist — or pure garbage (e.g. "DE STAAT" → "Mozart:
        # Requiem"). Reject any fuzzy candidate whose title isn't the requested
        # release, so the user never gets the wrong album. Exact (UPC) is trusted.
        if entry.get("match") == "fuzzy":
            if _nn and not _title_matches(entry.get("title", "")):
                return
            # ...and whose ARTIST isn't the requested one. Same-titled albums by
            # DIFFERENT artists ("New Love" by Kloyd vs Ziggy Alberts) otherwise
            # pass the title check — and worse, the wrong Deezer hit's UPC gets
            # borrowed below to poison the Apple/Qobuz exact lookups. Skipped only
            # when the source artist is unknown (Spotify geo-block, oembed title).
            if not _same_artist(artist, entry.get("artist", "")):
                return
        svc = entry["service"]
        prev = candidates.get(svc)
        if prev is None or (entry["match"] == "exact" and prev["match"] != "exact"):
            candidates[svc] = entry

    q = f"{artist} {name}".strip()

    # Deezer — UPC exact, then fuzzy album search
    try:
        async with _HTTP.ashared() as c:
            if upc:
                r = await c.get(f"https://api.deezer.com/2.0/album/upc:{upc}")
                d = r.json()
                if not d.get("error") and d.get("id"):
                    _add({"service": "deezer", "url": d.get("link", ""),
                          "title": d.get("title", name),
                          "artist": (d.get("artist") or {}).get("name", artist),
                          "quality": "FLAC", "match": "exact"})
            if "deezer" not in candidates and q:
                r = await c.get("https://api.deezer.com/search/album",
                                params={"q": q, "limit": 3})
                for item in (r.json().get("data") or [])[:1]:
                    _add({"service": "deezer", "url": item.get("link", ""),
                          "title": item.get("title", ""),
                          "artist": (item.get("artist") or {}).get("name", ""),
                          "quality": "FLAC", "match": "fuzzy"})
    except Exception:
        pass

    # Spotify gave no UPC (geo-block)? Borrow it from the Deezer match we just
    # found, so the Apple/Qobuz exact-UPC lookups below work WITHOUT the
    # (geo-blocked) Spotify API. Deezer search items omit upc → fetch the album.
    if not upc and "deezer" in candidates:
        try:
            import re as _re
            m = _re.search(r"/album/(\d+)", candidates["deezer"].get("url", ""))
            if m:
                async with _HTTP.ashared() as c:
                    dd = (await c.get(f"https://api.deezer.com/album/{m.group(1)}")).json()
                if not dd.get("error") and dd.get("upc"):
                    upc = str(dd["upc"])
        except Exception:
            pass

    # Apple (iTunes) — UPC lookup, then fuzzy
    try:
        async with _HTTP.ashared() as c:
            if upc:
                r = await c.get("https://itunes.apple.com/lookup",
                                params={"upc": upc, "entity": "album", "limit": 1})
                for s in [x for x in (r.json().get("results") or [])
                          if x.get("wrapperType") == "collection"][:1]:
                    _add({"service": "apple", "url": s.get("collectionViewUrl", ""),
                          "title": s.get("collectionName", ""), "artist": s.get("artistName", ""),
                          "quality": "ALAC", "match": "exact"})
            if "apple" not in candidates and q:
                r = await c.get("https://itunes.apple.com/search",
                                params={"term": q, "entity": "album", "limit": 3, "media": "music"})
                for s in (r.json().get("results") or [])[:1]:
                    _add({"service": "apple", "url": s.get("collectionViewUrl", ""),
                          "title": s.get("collectionName", ""), "artist": s.get("artistName", ""),
                          "quality": "ALAC", "match": "fuzzy"})
    except Exception:
        pass

    # Qobuz — UPC as query first, then name+artist
    qz_token  = (_config.get("qobuz-auth-token") or "").strip()
    qz_app_id = (_config.get("qobuz-app-id")     or "").strip() or _QOBUZ_DEFAULT_APP_ID
    if qz_token:
        try:
            async with _HTTP.ashared() as c:
                def _qz_alb(item: dict, match: str) -> dict:
                    aid = item.get("id", "")
                    return {"service": "qobuz",
                            "url": item.get("url") or f"https://open.qobuz.com/album/{aid}",
                            "title": item.get("title", ""),
                            "artist": (item.get("artist") or {}).get("name", ""),
                            "quality": "Hi-Res FLAC", "match": match}
                if upc:
                    r = await c.get("https://www.qobuz.com/api.json/0.2/album/search",
                                    params={"query": upc, "limit": 3, "app_id": qz_app_id},
                                    headers={"X-User-Auth-Token": qz_token})
                    for item in (r.json().get("albums", {}).get("items") or []):
                        _add(_qz_alb(item, "exact" if item.get("upc") == upc else "fuzzy"))
                if "qobuz" not in candidates and q:
                    r = await c.get("https://www.qobuz.com/api.json/0.2/album/search",
                                    params={"query": q, "limit": 3, "app_id": qz_app_id},
                                    headers={"X-User-Auth-Token": qz_token})
                    for item in (r.json().get("albums", {}).get("items") or [])[:1]:
                        _add(_qz_alb(item, "fuzzy"))
        except Exception:
            pass

    # Tidal — fuzzy album search
    try:
        if _tidal_headers() and q:
            td = await _search_tidal(q, "album", 3)
            for item in (td.get("results") or [])[:1]:
                _add({"service": "tidal", "url": item.get("url", ""),
                      "title": item.get("title", ""), "artist": item.get("artist", ""),
                      "quality": "Lossless", "match": "fuzzy"})
    except Exception:
        pass

    # Yandex — fuzzy album search
    try:
        if (_config.get("yandex-token") or "").strip() and q:
            ym = await _search_yandex(q, "album", 3)
            for item in (ym.get("results") or [])[:1]:
                _add({"service": "yandex", "url": item.get("url", ""),
                      "title": item.get("title", ""), "artist": item.get("artist", ""),
                      "quality": "FLAC", "match": "fuzzy"})
    except Exception:
        pass

    order = ["deezer", "apple", "qobuz", "tidal", "yandex"]
    return {"results": [candidates[s] for s in order if s in candidates], "upc": upc}


async def _resolve_release_id(url: str) -> str:
    """Map any supported release URL to a CANONICAL content id, so the bot can
    cache/dedupe by the actual release rather than the (decorated, per-service)
    link. Track → ``isrc:<ISRC>``, album → ``upc:<UPC>``. Falls back to
    ``url:<host+path>`` when the content id can't be determined.

    The same release from a different service/link resolves to the same id
    (Deezer/Apple/Qobuz/Spotify share UPC/ISRC), so a hit is reliable."""
    import re as _re
    from urllib.parse import urlparse
    u = (url or "").strip()
    host = (urlparse(u).hostname or "").lower()

    def _fallback() -> str:
        from urllib.parse import parse_qs
        p = urlparse(u)
        base = (p.netloc + p.path.rstrip("/")).lower()
        # Apple single-track links share the ALBUM path and only differ by the
        # ?i=<track-id> query marker (music.apple.com/<sf>/album/<name>/<album-id>?i=<track-id>).
        # Strip the query (default urlparse fallback) and a one-track request
        # collapses onto the whole-album cache id → cache.has() is wrongly True
        # and the track never gets mirrored to the cache channel. Keep ?i= so a
        # single track gets its own id, distinct from the full album.
        i = (parse_qs(p.query).get("i") or [""])[0]
        if i:
            base += "?i=" + i.lower()
        return "url:" + base

    try:
        if "spotify.com" in host:
            m = _re.search(r"/(track|album)/([A-Za-z0-9]+)", u)
            if not m:
                return _fallback()
            kind, sid = m.group(1), m.group(2)
            token = await _get_spotify_app_token()
            if not token:
                return _fallback()
            async with _HTTP.ashared() as c:
                ep = "tracks" if kind == "track" else "albums"
                r = await c.get(f"https://api.spotify.com/v1/{ep}/{sid}",
                                headers={"Authorization": f"Bearer {token}"})
                if r.status_code == 200:
                    ext = r.json().get("external_ids") or {}
                    if kind == "track" and ext.get("isrc"):
                        return f"isrc:{ext['isrc']}"
                    if kind == "album" and ext.get("upc"):
                        return f"upc:{ext['upc']}"
            return _fallback()

        if "deezer.com" in host:
            m = _re.search(r"/(track|album)/(\d+)", u)
            if not m:
                return _fallback()
            kind, did = m.group(1), m.group(2)
            async with _HTTP.ashared() as c:
                d = (await c.get(f"https://api.deezer.com/{kind}/{did}")).json()
            if not d.get("error"):
                if kind == "track" and d.get("isrc"):
                    return f"isrc:{d['isrc']}"
                if kind == "album" and d.get("upc"):
                    return f"upc:{d['upc']}"
            return _fallback()

        if "qobuz.com" in host:
            qz_token = (_config.get("qobuz-auth-token") or "").strip()
            qz_app   = (_config.get("qobuz-app-id") or "").strip() or _QOBUZ_DEFAULT_APP_ID
            hdr = {"X-User-Auth-Token": qz_token} if qz_token else {}
            mt = _re.search(r"/track/(\d+)", u)
            ma = _re.search(r"/album/(?:-/)?([A-Za-z0-9]+)", u)
            async with _HTTP.ashared() as c:
                if mt:
                    r = await c.get("https://www.qobuz.com/api.json/0.2/track/get",
                                    params={"track_id": mt.group(1), "app_id": qz_app}, headers=hdr)
                    if r.status_code == 200 and r.json().get("isrc"):
                        return f"isrc:{r.json()['isrc']}"
                elif ma:
                    r = await c.get("https://www.qobuz.com/api.json/0.2/album/get",
                                    params={"album_id": ma.group(1), "app_id": qz_app}, headers=hdr)
                    if r.status_code == 200 and r.json().get("upc"):
                        return f"upc:{r.json()['upc']}"
            return _fallback()

        if "tidal.com" in host:
            # Use the FRESH access_token minted from OrpheusDL's self-refreshing
            # session — the pasted tidal-token dies in ~16 h, and when it did the
            # isrc/upc lookup 401'd and fell back to a url: id. That made Tidal
            # releases cache under url:tidal.com/… instead of isrc:/upc:, so they
            # never cross-service-deduped. Fall back to the pasted token.
            hdr = None
            try:
                from ripster.engines.tidal import _orpheus_access_token
                _tok, _cc = await _orpheus_access_token()
                if _tok:
                    hdr = {"Authorization": f"Bearer {_tok}"}
            except Exception:
                hdr = None
            if hdr is None:
                hdr = _tidal_headers()
            if hdr:
                m = _re.search(r"/(track|album)/(\d+)", u)
                if m:
                    kind, tid = m.group(1), m.group(2)
                    ep = "tracks" if kind == "track" else "albums"
                    async with _HTTP.ashared() as c:
                        r = await c.get(f"{_TIDAL_API}/{ep}/{tid}", headers=hdr,
                                        params={"countryCode": _tidal_country()})
                        if r.status_code == 200:
                            d = r.json()
                            if kind == "track" and d.get("isrc"):
                                return f"isrc:{d['isrc']}"
                            if kind == "album" and d.get("upc"):
                                return f"upc:{d['upc']}"
            return _fallback()

        if "music.apple.com" in host:
            # Apple Catalog API exposes isrc (songs) / upc (albums) — resolve them
            # so an Apple release dedupes against the SAME release from Qobuz/
            # Deezer/Tidal/Spotify (shared UPC/ISRC). Best-effort: falls back to a
            # url: id (with ?i= track marker preserved) when the catalog/bearer is
            # unavailable. See ripster/metadata/apple.content_id.
            try:
                from ripster.metadata import apple as _apple
                cid = await _apple.content_id(u)
                if cid:
                    return cid
            except Exception:
                pass
            return _fallback()

        # Yandex / SoundCloud: no cheap content-id endpoint — the normalized URL
        # still dedupes the common "same link again" case.
        return _fallback()
    except Exception:
        return _fallback()


@router.post("/api/release-id")
async def release_id(body: dict):
    """URL → canonical release id (``isrc:…`` / ``upc:…`` / ``url:…``) for the
    bot's cache keying."""
    return {"id": await _resolve_release_id((body.get("url") or "").strip())}


@router.post("/api/isrc-upgrade")
async def isrc_upgrade(body: dict):
    """Search for a Spotify track on lossless services by title+artist (+ ISRC if available).
    Returns best 1 candidate per service: {service, url, title, artist, quality, isrc, match}.
    A Spotify ALBUM url is delegated to the UPC-based album resolver.
    """
    title  = (body.get("title")  or "").strip()
    artist = (body.get("artist") or "").strip()
    sp_url = (body.get("url")    or "").strip()

    # Full release → match the whole album on other services by UPC/name.
    if "open.spotify.com/album/" in sp_url:
        return await _resolve_spotify_album(sp_url)

    # A Spotify track URL is enough on its own — title/artist are derived from it
    # below (so the bot can resolve a release with just the link).
    if not title and not artist and "open.spotify.com/track/" not in sp_url:
        raise HTTPException(400, "title or artist required")

    isrc: str = ""
    sp_upc: str = ""      # Spotify album UPC — lets us prefer the SAME album on
    sp_album: str = ""    # other services (not a random compilation appearance)

    # 1. Fetch ISRC from Spotify — app token preferred (auto-refreshed), OrpheusDL token as fallback
    if "open.spotify.com/track/" in sp_url:
        tid = sp_url.split("/track/")[-1].split("?")[0].strip()
        token = await _get_spotify_app_token()
        if not token:
            try:
                from ripster.engines.orpheus_spotify import _creds_path as _osp_creds
                import json as _json
                p = _osp_creds()
                if p.exists():
                    token = _json.loads(p.read_text(encoding="utf-8")).get("access_token", "")
            except Exception:
                pass
        if token:
            try:
                async with _HTTP.ashared() as c:
                    r = await c.get(
                        f"https://api.spotify.com/v1/tracks/{tid}",
                        headers={"Authorization": f"Bearer {token}"},
                    )
                    if r.status_code == 200:
                        _td = r.json()
                        isrc = (_td.get("external_ids") or {}).get("isrc", "")
                        # Derive title/artist from the URL too, so callers (the bot)
                        # can resolve with just a Spotify link — no title/artist needed.
                        if not title:
                            title = _td.get("name", "") or title
                        if not artist:
                            artist = ", ".join(a.get("name", "") for a in (_td.get("artists") or [])) or artist
                        # Capture the track's album + UPC so each service can return
                        # the track AS IT APPEARS ON THAT ALBUM (correct cover),
                        # instead of a random compilation that shares the ISRC.
                        _alb = _td.get("album") or {}
                        sp_album = _alb.get("name", "") or ""
                        _alb_id = _alb.get("id", "")
                        if _alb_id:
                            try:
                                ra = await c.get(
                                    f"https://api.spotify.com/v1/albums/{_alb_id}",
                                    headers={"Authorization": f"Bearer {token}"})
                                if ra.status_code == 200:
                                    sp_upc = (ra.json().get("external_ids") or {}).get("upc", "") or ""
                            except Exception:
                                pass
            except Exception:
                pass

    # Spotify Web API geo-blocked (403) → no title/isrc. Fall back to the public
    # oembed endpoint (no auth, not geo-blocked) for the track name, so the fuzzy
    # lossless search still runs under a VPN. ISRC is then borrowed from the Deezer
    # match below (Deezer search items carry isrc), so Apple/Qobuz exact still work.
    if not title and "open.spotify.com/track/" in sp_url:
        try:
            async with _HTTP.ashared() as c:
                r = await c.get("https://open.spotify.com/oembed", params={"url": sp_url})
                if r.status_code == 200:
                    title = (r.json().get("title") or "").strip()
        except Exception:
            pass

    def _norm(s: str) -> str:
        return "".join(ch for ch in (s or "").lower() if ch.isalnum())

    # candidates[service] -> best dict (exact preferred over fuzzy)
    candidates: dict[str, dict] = {}
    _ntitle = _norm(title)

    def _add(entry: dict) -> None:
        # Reject a FUZZY title+artist match whose title isn't the requested track
        # (a search can return a different song by the same artist). ISRC-exact
        # matches are trusted. Prevents delivering the wrong recording.
        if entry.get("match") == "fuzzy":
            if _ntitle:
                ct = _norm(entry.get("title", ""))
                if not (ct and (ct == _ntitle or ct in _ntitle or _ntitle in ct)):
                    return
            # ...and whose ARTIST differs from the requested one (a same-titled
            # song by a different artist). Skipped when the source artist is
            # unknown (Spotify geo-block left only the oembed title).
            if not _same_artist(artist, entry.get("artist", "")):
                return
        svc = entry["service"]
        prev = candidates.get(svc)
        if prev is None or (entry["match"] == "exact" and prev["match"] != "exact"):
            candidates[svc] = entry

    # 2. Deezer — ISRC exact first, then fuzzy title+artist
    try:
        async with _HTTP.ashared() as c:
            if isrc:
                r = await c.get(f"https://api.deezer.com/2.0/track/isrc:{isrc}")
                d = r.json()
                if not d.get("error") and d.get("id"):
                    dz_url = d.get("link", "")
                    # /track/isrc returns ONE appearance — often a compilation, so
                    # the cover/album are wrong. Prefer the same album as Spotify
                    # (by UPC): pull that album and use its track with this title.
                    dz_alb = _norm((d.get("album") or {}).get("title", ""))
                    if sp_upc and _norm(sp_album) and dz_alb != _norm(sp_album):
                        try:
                            ar = await c.get(f"https://api.deezer.com/2.0/album/upc:{sp_upc}")
                            ad = ar.json()
                            if not ad.get("error") and ad.get("id"):
                                for tr in ((ad.get("tracks") or {}).get("data") or []):
                                    if _norm(tr.get("title", "")) == _norm(title):
                                        dz_url = tr.get("link", dz_url)
                                        break
                        except Exception:
                            pass
                    _add({"service": "deezer", "url": dz_url,
                          "title": d.get("title", ""),
                          "artist": (d.get("artist") or {}).get("name", ""),
                          "quality": "FLAC", "isrc": isrc, "match": "exact"})
            if "deezer" not in candidates:
                q = f'artist:"{artist}" track:"{title}"' if artist and title else (title or artist)
                r = await c.get("https://api.deezer.com/search", params={"q": q, "limit": 3})
                for item in (r.json().get("data") or []):
                    _add({"service": "deezer", "url": item.get("link", ""),
                          "title": item.get("title", ""),
                          "artist": (item.get("artist") or {}).get("name", ""),
                          "quality": "FLAC", "isrc": item.get("isrc", ""), "match": "fuzzy"})
    except Exception:
        pass

    # Spotify gave no ISRC (geo-block)? Borrow it from the Deezer match (its search
    # items carry isrc) so the Apple/Qobuz ISRC-exact lookups below work WITHOUT
    # the geo-blocked Spotify API.
    if not isrc:
        isrc = ((candidates.get("deezer") or {}).get("isrc") or "").strip()

    # 3. Apple Music (iTunes) — ISRC lookup first, then fuzzy
    try:
        async with _HTTP.ashared() as c:
            if isrc:
                r = await c.get("https://itunes.apple.com/lookup",
                                params={"isrc": isrc, "entity": "song", "limit": 1})
                for s in [x for x in (r.json().get("results") or [])
                          if x.get("wrapperType") == "track"][:1]:
                    ap_url = s.get("trackViewUrl", "")
                    # Prefer the same album as Spotify (right cover) over whatever
                    # the ISRC lookup happened to return (often a compilation).
                    if sp_upc and _norm(sp_album) and _norm(s.get("collectionName", "")) != _norm(sp_album):
                        try:
                            ra = await c.get("https://itunes.apple.com/lookup",
                                             params={"upc": sp_upc, "entity": "song"})
                            for tr in (ra.json().get("results") or []):
                                if (tr.get("wrapperType") == "track"
                                        and _norm(tr.get("trackName", "")) == _norm(title)):
                                    ap_url = tr.get("trackViewUrl", ap_url)
                                    break
                        except Exception:
                            pass
                    _add({"service": "apple", "url": ap_url,
                          "title": s.get("trackName", ""), "artist": s.get("artistName", ""),
                          "quality": "ALAC", "isrc": isrc, "match": "exact"})
            if "apple" not in candidates:
                q = f"{artist} {title}".strip()
                r = await c.get("https://itunes.apple.com/search",
                                params={"term": q, "entity": "song", "limit": 3, "media": "music"})
                for s in (r.json().get("results") or [])[:1]:
                    _add({"service": "apple", "url": s.get("trackViewUrl", ""),
                          "title": s.get("trackName", ""), "artist": s.get("artistName", ""),
                          "quality": "ALAC", "isrc": "", "match": "fuzzy"})
    except Exception:
        pass

    # 4. Qobuz — ISRC search first (search API accepts ISRC as query), then title+artist
    qz_token  = (_config.get("qobuz-auth-token") or "").strip()
    qz_app_id = (_config.get("qobuz-app-id")     or "").strip() or _QOBUZ_DEFAULT_APP_ID
    if qz_token:
        try:
            async with _HTTP.ashared() as c:
                def _qz_item(item: dict, match: str) -> dict:
                    tid  = item.get("id", "")
                    turl = item.get("url") or f"https://open.qobuz.com/track/{tid}"
                    return {"service": "qobuz", "url": turl,
                            "title":  item.get("title", ""),
                            "artist": (item.get("performer") or {}).get("name", ""),
                            "quality": "Hi-Res FLAC",
                            "isrc":   item.get("isrc", ""), "match": match}
                if isrc:
                    r = await c.get("https://www.qobuz.com/api.json/0.2/track/search",
                                    params={"query": isrc, "limit": 3, "app_id": qz_app_id},
                                    headers={"X-User-Auth-Token": qz_token})
                    its = (r.json().get("tracks", {}).get("items") or [])
                    # Among the appearances that share this ISRC, prefer the one on
                    # the SAME album as Spotify (right cover) — _add keeps the first
                    # exact, so add the album-matching one first.
                    exact_its = [it for it in its if it.get("isrc", "") == isrc]
                    if sp_upc and _norm(sp_album):
                        exact_its.sort(key=lambda it: 0 if _norm((it.get("album") or {}).get("title", "")) == _norm(sp_album) else 1)
                    for item in exact_its:
                        _add(_qz_item(item, "exact"))
                    for item in its:
                        if item.get("isrc", "") != isrc:
                            _add(_qz_item(item, "fuzzy"))
                if "qobuz" not in candidates:
                    q = f"{artist} {title}".strip()
                    r = await c.get("https://www.qobuz.com/api.json/0.2/track/search",
                                    params={"query": q, "limit": 3, "app_id": qz_app_id},
                                    headers={"X-User-Auth-Token": qz_token})
                    for item in (r.json().get("tracks", {}).get("items") or [])[:1]:
                        _add(_qz_item(item, "fuzzy"))
        except Exception:
            pass

    # 5. Tidal — fuzzy title+artist (the v1 user API has no clean ISRC endpoint)
    try:
        if _tidal_headers():
            q = f"{artist} {title}".strip()
            td = await _search_tidal(q, "track", 3)
            for item in (td.get("results") or [])[:1]:
                if item.get("url"):
                    _add({"service": "tidal", "url": item.get("url", ""),
                          "title": item.get("title", ""), "artist": item.get("artist", ""),
                          "quality": "Lossless", "isrc": "", "match": "fuzzy"})
    except Exception:
        pass

    # 6. Yandex Music — fuzzy title+artist (no ISRC in the search API)
    try:
        if (_config.get("yandex-token") or "").strip():
            q = f"{artist} {title}".strip()
            ym = await _search_yandex(q, "track", 3)
            for item in (ym.get("results") or [])[:1]:
                if item.get("url"):
                    _add({"service": "yandex", "url": item.get("url", ""),
                          "title": item.get("title", ""), "artist": item.get("artist", ""),
                          "quality": "FLAC", "isrc": "", "match": "fuzzy"})
    except Exception:
        pass

    order   = ["deezer", "apple", "qobuz", "tidal", "yandex"]
    results = [candidates[s] for s in order if s in candidates]
    return {"results": results, "isrc": isrc}
