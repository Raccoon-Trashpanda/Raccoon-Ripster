"""
URL resolver — expands album/playlist URLs to individual per-track URLs.

resolve(url) -> list[dict]
  Each dict: {url, title, artist, artwork_url, track_num, total}

Single track  → list of 1 item (url == original, no HTTP call made)
Album/playlist → list of N items (N HTTP calls may be made)
Error / unknown → [] (caller falls back to single-task behavior)
"""
from __future__ import annotations

import re
import urllib.parse
from typing import Optional

import httpx
from ripster import http_client as _HTTP

_TIMEOUT = httpx.Timeout(connect=6, read=10, write=5, pool=3)
_cfg: dict = {}


def install(cfg: dict) -> None:
    global _cfg
    _cfg = cfg


# ── Quick track-URL detection (no HTTP) ───────────────────────────────────────

def _is_single_track(url: str) -> bool:
    u = url.lower()
    parsed = urllib.parse.urlparse(url)
    qs = urllib.parse.parse_qs(parsed.query)

    if "music.apple.com" in u:
        # album URL with ?i= param is a single track
        if qs.get("i"):
            return True
        # /song/ path, or a standalone music video
        if "/song/" in u or "/music-video/" in u:
            return True
        return False

    if "deezer.com" in u:
        return "/track/" in u

    if "qobuz.com" in u:
        return "/track/" in u

    if "tidal.com" in u:
        return "/track/" in u

    return True   # Unknown service — treat as single track


# ── Public entry point ─────────────────────────────────────────────────────────

async def resolve(url: str) -> list[dict]:
    """
    Return per-track list.  Empty list means caller should treat URL as-is.
    Single-track URLs return immediately without HTTP calls.
    """
    if _is_single_track(url):
        return _single(url)

    u = url.lower()
    try:
        import asyncio
        if "music.apple.com" in u:
            return await asyncio.wait_for(_resolve_apple(url),  timeout=12)
        if "deezer.com" in u:
            return await asyncio.wait_for(_resolve_deezer(url), timeout=12)
        if "qobuz.com" in u:
            return await asyncio.wait_for(_resolve_qobuz(url),  timeout=12)
        if "tidal.com" in u:
            return await asyncio.wait_for(_resolve_tidal(url),  timeout=12)
    except Exception as e:
        print(f"[resolver] {url[:60]} → {e}", flush=True)
    return []


def _single(url: str) -> list[dict]:
    return [{"url": url, "title": "", "artist": "", "artwork_url": "", "track_num": 1, "total": 1}]


# ── Apple Music ────────────────────────────────────────────────────────────────

def _parse_apple(url: str):
    """Return (storefront, type, id) for Apple Music URLs."""
    parsed = urllib.parse.urlparse(url)
    parts  = [p for p in parsed.path.split("/") if p]
    sf     = parts[0] if parts else "us"
    type_  = parts[1] if len(parts) > 1 else "album"
    id_    = next((p for p in reversed(parts) if p.isdigit()), None) or (parts[-1] if parts else "")
    return sf, type_, id_


async def _resolve_apple(url: str) -> list[dict]:
    sf, type_, id_ = _parse_apple(url)

    if type_ == "playlist":
        return await _apple_playlist(url, sf, id_)

    # Album
    return await _apple_album(url, sf, id_)


async def _apple_album(url: str, sf: str, album_id: str) -> list[dict]:
    async with _HTTP.ashared() as c:
        r = await c.get("https://itunes.apple.com/lookup", params={
            "id": album_id, "entity": "song", "country": sf,
        })
    if r.status_code != 200:
        return []
    results = r.json().get("results") or []

    tracks = [item for item in results if item.get("wrapperType") == "track"]
    if not tracks:
        return []

    collection = next((x for x in results if x.get("wrapperType") == "collection"), {})
    art = re.sub(r'\d+x\d+bb', '600x600bb', collection.get("artworkUrl100") or "")

    total = len(tracks)
    out   = []
    for i, t in enumerate(tracks, 1):
        tid  = str(t.get("trackId") or "")
        turl = f"https://music.apple.com/{sf}/album/-/{album_id}?i={tid}" if tid else url
        out.append({
            "url":         turl,
            "title":       t.get("trackName", ""),
            "artist":      t.get("artistName", ""),
            "artwork_url": art,
            "track_num":   t.get("trackNumber", i),
            "total":       total,
        })
    return out


async def _apple_playlist(url: str, sf: str, playlist_id: str) -> list[dict]:
    bearer = _cfg.get("authorization-token", "").strip()
    if not bearer:
        try:
            from ripster.metadata.apple import auto_fetch_bearer
            bearer = await auto_fetch_bearer()
        except Exception:
            pass
    if not bearer:
        return []

    headers = {
        "Authorization": f"Bearer {bearer}",
        "Origin": "https://music.apple.com",
    }
    mut = _cfg.get("media-user-token", "").strip()
    if mut:
        headers["media-user-token"] = mut

    api_url = (f"https://api.music.apple.com/v1/catalog/{sf}"
               f"/playlists/{playlist_id}?include=tracks&limit[tracks]=300")
    async with _HTTP.ashared() as c:
        r = await c.get(api_url, headers=headers)
    if r.status_code != 200:
        return []

    data    = r.json()
    item    = (data.get("data") or [{}])[0]
    rels    = item.get("relationships") or {}
    tracks  = (rels.get("tracks") or {}).get("data") or []
    if not tracks:
        return []

    total = len(tracks)
    out   = []
    for i, track in enumerate(tracks, 1):
        attrs = track.get("attributes") or {}
        artwork = attrs.get("artwork") or {}
        art = artwork.get("url", "").replace("{w}x{h}", "600x600") if artwork else ""
        turl = attrs.get("url") or url
        out.append({
            "url":         turl,
            "title":       attrs.get("name", ""),
            "artist":      attrs.get("artistName", ""),
            "artwork_url": art,
            "track_num":   i,
            "total":       total,
        })
    return out


# ── Deezer ────────────────────────────────────────────────────────────────────

def _parse_path_service(url: str) -> tuple[str, str]:
    """Extract (type, id) from a ``/track|album|playlist/<id>`` style path.
    Deezer, Qobuz and Tidal share this exact URL grammar, so one parser serves
    all three (exposed under per-service aliases below for call-site clarity)."""
    parsed = urllib.parse.urlparse(url)
    parts  = [p for p in parsed.path.split("/") if p]
    for i, p in enumerate(parts):
        if p in ("track", "album", "playlist"):
            rest = [s.split("?")[0] for s in parts[i + 1:]]
            if not rest:
                return p, ""
            # The id is the last purely-numeric segment when present — this
            # handles the qobuz web form /<lang>/album/<slug>/<id>. Otherwise
            # take the segment right after the keyword (deezer/tidal put the id
            # there directly, and tidal playlist ids are non-numeric UUIDs).
            numeric = [s for s in rest if s.isdigit()]
            return p, (numeric[-1] if numeric else rest[0])
    return "", ""


# Deezer / Qobuz / Tidal URL shapes are identical here — alias the one parser.
_parse_deezer = _parse_qobuz = _parse_tidal = _parse_path_service


async def _resolve_deezer(url: str) -> list[dict]:
    tp, id_ = _parse_deezer(url)
    if not tp or not id_:
        return []

    if tp == "album":
        return await _deezer_album(id_)
    if tp == "playlist":
        return await _deezer_playlist(id_)
    return []


async def _deezer_album(album_id: str) -> list[dict]:
    async with _HTTP.ashared() as c:
        r_info, r_tracks = await _gather(
            c.get(f"https://api.deezer.com/album/{album_id}"),
            c.get(f"https://api.deezer.com/album/{album_id}/tracks", params={"limit": 200}),
        )

    art    = ""
    if r_info.status_code == 200:
        ad  = r_info.json()
        art = ad.get("cover_xl") or ad.get("cover_big") or ""

    if r_tracks.status_code != 200:
        return []
    d      = r_tracks.json()
    if "error" in d:
        return []
    tracks = d.get("data") or []
    total  = len(tracks)
    return [
        {
            "url":         f"https://www.deezer.com/track/{t['id']}",
            "title":       t.get("title", ""),
            "artist":      (t.get("artist") or {}).get("name", ""),
            "artwork_url": art,
            "track_num":   i + 1,
            "total":       total,
        }
        for i, t in enumerate(tracks) if t.get("id")
    ]


async def _deezer_playlist(playlist_id: str) -> list[dict]:
    art    = ""
    total  = 0
    # Fetch playlist info for artwork
    async with _HTTP.ashared() as c:
        rp = await c.get(f"https://api.deezer.com/playlist/{playlist_id}")
    if rp.status_code == 200:
        pd    = rp.json()
        art   = pd.get("picture_xl") or pd.get("picture_big") or ""
        total = pd.get("nb_tracks", 0)

    tracks_out = []
    offset, limit = 0, 200
    async with _HTTP.ashared() as c:
        while True:
            r = await c.get(
                f"https://api.deezer.com/playlist/{playlist_id}/tracks",
                params={"limit": limit, "index": offset},
            )
            if r.status_code != 200:
                break
            data = r.json()
            if "error" in data:
                break
            page = data.get("data") or []
            if not page:
                break
            tracks_out.extend(page)
            if len(page) < limit:
                break
            offset += limit
            if total and offset >= total:
                break

    n = len(tracks_out)
    if not n:
        return []
    return [
        {
            "url":         f"https://www.deezer.com/track/{t['id']}",
            "title":       t.get("title", ""),
            "artist":      (t.get("artist") or {}).get("name", ""),
            "artwork_url": art,
            "track_num":   i + 1,
            "total":       n,
        }
        for i, t in enumerate(tracks_out) if t.get("id")
    ]


# ── Qobuz ─────────────────────────────────────────────────────────────────────

_QOBUZ_APP_ID = "798273057"


async def _resolve_qobuz(url: str) -> list[dict]:
    tp, id_ = _parse_qobuz(url)
    if not tp or not id_:
        return []

    app_id  = (_cfg.get("qobuz-app-id") or "").strip() or _QOBUZ_APP_ID
    token   = (_cfg.get("qobuz-auth-token") or "").strip()
    headers = {"X-User-Auth-Token": token} if token else {}

    if tp == "album":
        return await _qobuz_album(id_, app_id, headers)
    if tp == "playlist":
        return await _qobuz_playlist(id_, app_id, headers)
    return []


async def _qobuz_album(album_id: str, app_id: str, headers: dict) -> list[dict]:
    async with _HTTP.ashared() as c:
        r = await c.get(
            "https://www.qobuz.com/api.json/0.2/album/get",
            params={"album_id": album_id, "app_id": app_id},
            headers=headers,
        )
    if r.status_code != 200:
        return []
    d = r.json()
    if d.get("status") == "error":
        return []

    artist_name = (d.get("artist") or {}).get("name", "")
    img = d.get("image") or {}
    art = img.get("large") or img.get("small") or ""
    items = (d.get("tracks") or {}).get("items") or []
    total = len(items)
    return [
        {
            "url":         f"https://open.qobuz.com/track/{t['id']}",
            "title":       t.get("title", ""),
            "artist":      (t.get("performer") or {}).get("name", "") or artist_name,
            "artwork_url": art,
            "track_num":   t.get("track_number", i + 1),
            "total":       total,
        }
        for i, t in enumerate(items) if t.get("id")
    ]


async def _qobuz_playlist(playlist_id: str, app_id: str, headers: dict) -> list[dict]:
    async with _HTTP.ashared() as c:
        r = await c.get(
            "https://www.qobuz.com/api.json/0.2/playlist/get",
            params={"playlist_id": playlist_id, "app_id": app_id,
                    "limit": 500, "extra": "tracks"},
            headers=headers,
        )
    if r.status_code != 200:
        return []
    d = r.json()
    if d.get("status") == "error":
        return []

    images = d.get("images300") or []
    art    = images[0] if images else ""
    items  = (d.get("tracks") or {}).get("items") or []
    total  = len(items)
    return [
        {
            "url":         f"https://open.qobuz.com/track/{t['id']}",
            "title":       t.get("title", ""),
            "artist":      (t.get("performer") or {}).get("name", ""),
            "artwork_url": art,
            "track_num":   i + 1,
            "total":       total,
        }
        for i, t in enumerate(items) if t.get("id")
    ]


# ── Tidal ──────────────────────────────────────────────────────────────────────

_TIDAL_CLIENT_IDS = ["CzET4vdadNUFQ5JU", "7m7Ap0JC9j1cOM3n", "ck3zaWMi8Ka_XdI0"]


async def _resolve_tidal(url: str) -> list[dict]:
    tp, id_ = _parse_tidal(url)
    if not tp or not id_:
        return []

    # Prefer the self-refreshing device-flow (TV) session token — the same one
    # downloads use — so search/metadata keep working without a manually pasted
    # `tidal-token` (which dies in ~16 h and can't be refreshed). Falls back to
    # the pasted token when there's no device-flow session.
    from ripster.engines.tidal import _tidal_token_country
    access, country = await _tidal_token_country(_cfg)
    if not access:
        return []

    for client_id in _TIDAL_CLIENT_IDS:
        hdrs = {
            "Authorization": f"Bearer {access}",
            "X-Tidal-Token":  client_id,
        }
        try:
            if tp == "album":
                result = await _tidal_album(id_, country, hdrs)
            else:
                result = await _tidal_playlist(id_, country, hdrs)
            if result:
                return result
        except Exception:
            continue
    return []


async def _tidal_album(album_id: str, country: str, headers: dict) -> list[dict]:
    async with _HTTP.ashared() as c:
        r_info, r_tracks = await _gather(
            c.get(f"https://api.tidal.com/v1/albums/{album_id}",
                  params={"countryCode": country}, headers=headers),
            c.get(f"https://api.tidal.com/v1/albums/{album_id}/tracks",
                  params={"countryCode": country, "limit": 200}, headers=headers),
        )

    art = ""
    if r_info.status_code == 200:
        ad = r_info.json()
        cid = (ad.get("cover") or "").replace("-", "/")
        if cid:
            art = f"https://resources.tidal.com/images/{cid}/640x640.jpg"

    if r_tracks.status_code != 200:
        return []
    d     = r_tracks.json()
    items = d.get("items") or []
    total = len(items)
    return [
        {
            "url":         f"https://tidal.com/browse/track/{t['id']}",
            "title":       t.get("title", ""),
            "artist":      _tidal_artist(t),
            "artwork_url": art,
            "track_num":   t.get("trackNumber", i + 1),
            "total":       total,
        }
        for i, t in enumerate(items) if t.get("id")
    ]


async def _tidal_playlist(playlist_id: str, country: str, headers: dict) -> list[dict]:
    tracks_out = []
    offset, limit = 0, 100
    async with _HTTP.ashared() as c:
        while True:
            r = await c.get(
                f"https://api.tidal.com/v1/playlists/{playlist_id}/items",
                params={"countryCode": country, "limit": limit, "offset": offset},
                headers=headers,
            )
            if r.status_code != 200:
                break
            d     = r.json()
            items = d.get("items") or []
            if not items:
                break
            for item in items:
                track = item.get("item") or {}
                if track.get("id"):
                    tracks_out.append(track)
            if len(items) < limit:
                break
            offset += limit

    n = len(tracks_out)
    if not n:
        return []
    return [
        {
            "url":         f"https://tidal.com/browse/track/{t['id']}",
            "title":       t.get("title", ""),
            "artist":      _tidal_artist(t),
            "artwork_url": "",
            "track_num":   i + 1,
            "total":       n,
        }
        for i, t in enumerate(tracks_out) if t.get("id")
    ]


def _tidal_artist(track: dict) -> str:
    artists = track.get("artists") or []
    if artists:
        return artists[0].get("name", "")
    return (track.get("artist") or {}).get("name", "")


# ── Helpers ────────────────────────────────────────────────────────────────────

async def _gather(*coros):
    """Run coroutines concurrently, return results in order."""
    import asyncio
    return await asyncio.gather(*coros)
