"""Spotify metadata fetcher — uses the app's PKCE OAuth token."""
from __future__ import annotations
import re
import time
from typing import Optional, Callable, Awaitable
import httpx
from ripster import http_client as _HTTP

_SPOTIFY_RE = re.compile(
    r'open\.spotify\.com/(track|album|playlist|artist)/([A-Za-z0-9]+)', re.I
)

_token_getter: Optional[Callable[[], Awaitable[Optional[str]]]] = None
_config: dict = {}

# Client credentials token cache (expires in ~1h, good for metadata-only calls)
_cc_token: str = ""
_cc_expires_at: float = 0.0


def install(token_getter_fn: Callable[[], Awaitable[Optional[str]]], cfg: dict | None = None) -> None:
    global _token_getter, _config
    _token_getter = token_getter_fn
    if cfg is not None:
        _config = cfg


async def _get_cc_token() -> Optional[str]:
    """Client credentials flow — no user auth needed, works for public content."""
    global _cc_token, _cc_expires_at
    cid = (_config.get("spotify-client-id") or "").strip()
    csec = (_config.get("spotify-client-secret") or "").strip()
    if not cid or not csec:
        return None
    now = time.time()
    if _cc_token and _cc_expires_at > now + 60:
        return _cc_token
    try:
        async with _HTTP.ashared() as c:
            r = await c.post(
                "https://accounts.spotify.com/api/token",
                data={"grant_type": "client_credentials"},
                auth=(cid, csec),
            )
            if r.status_code != 200:
                return None
            d = r.json()
            _cc_token = d.get("access_token", "")
            _cc_expires_at = now + d.get("expires_in", 3600)
            return _cc_token or None
    except Exception:
        return None


async def _oembed_fallback(url: str) -> Optional[dict]:
    """No-auth fallback: Spotify oembed gives title + thumbnail."""
    try:
        async with _HTTP.ashared() as client:
            r = await client.get("https://open.spotify.com/oembed", params={"url": url})
            if r.status_code != 200:
                return None
            d = r.json()
            return {
                "title":      d.get("title", ""),
                "artist":     "",
                "artworkUrl": d.get("thumbnail_url"),
                "service":    "spotify",
            }
    except Exception:
        return None


async def _request_meta(client, kind: str, item_id: str, token: str) -> tuple[Optional[dict], int]:
    """One Spotify Web API call for *kind*/*item_id* with *token*.

    Returns ``(meta, status)``. ``meta`` is None on any non-200 — the caller
    inspects ``status`` to decide whether to retry with another token (401/403 =
    dead token) or give up (404 = not found, retry won't help)."""
    headers = {"Authorization": f"Bearer {token}"}
    api = "https://api.spotify.com/v1"

    if kind == "track":
        r = await client.get(f"{api}/tracks/{item_id}", headers=headers)
        if r.status_code != 200:
            return None, r.status_code
        d = r.json()
        album = d.get("album") or {}
        images = album.get("images") or []
        ext = d.get("external_ids") or {}
        return {
            "title":       d.get("name", ""),
            "artist":      ", ".join(a["name"] for a in d.get("artists", [])),
            "album":       album.get("name", ""),
            "artworkUrl":  images[0]["url"] if images else None,
            "year":        (album.get("release_date") or "")[:4],
            "date":        album.get("release_date", ""),
            "trackCount":  1,
            "totalTracks": 1,
            "type":        "track",
            "service":     "spotify",
            "isrc":        ext.get("isrc", ""),
        }, 200

    elif kind == "album":
        r = await client.get(f"{api}/albums/{item_id}", headers=headers)
        if r.status_code != 200:
            return None, r.status_code
        d = r.json()
        images = d.get("images") or []
        ext = d.get("external_ids") or {}
        return {
            "title":       d.get("name", ""),
            "artist":      ", ".join(a["name"] for a in d.get("artists", [])),
            "album":       d.get("name", ""),
            "artworkUrl":  images[0]["url"] if images else None,
            "year":        (d.get("release_date") or "")[:4],
            "date":        d.get("release_date", ""),
            "trackCount":  d.get("total_tracks", 0),
            "totalTracks": d.get("total_tracks", 0),
            "label":       d.get("label", ""),
            "type":        d.get("album_type", "album"),
            "service":     "spotify",
            "upc":         ext.get("upc", ""),
        }, 200

    elif kind == "playlist":
        r = await client.get(
            f"{api}/playlists/{item_id}",
            headers=headers,
            params={"fields": "name,owner,images,tracks.total"},
        )
        if r.status_code != 200:
            return None, r.status_code
        d = r.json()
        images = d.get("images") or []
        return {
            "title":      d.get("name", ""),
            "artist":     (d.get("owner") or {}).get("display_name", ""),
            "artworkUrl": images[0]["url"] if images else None,
            "trackCount": (d.get("tracks") or {}).get("total", 0),
            "type":       "playlist",
            "service":    "spotify",
        }, 200

    elif kind == "artist":
        r = await client.get(f"{api}/artists/{item_id}", headers=headers)
        if r.status_code != 200:
            return None, r.status_code
        d = r.json()
        images = d.get("images") or []
        return {
            "title":      d.get("name", ""),
            "artist":     d.get("name", ""),
            "artworkUrl": images[0]["url"] if images else None,
            "type":       "artist",
            "service":    "spotify",
        }, 200

    return None, 0


async def fetch_meta_spotify(url: str) -> Optional[dict]:
    m = _SPOTIFY_RE.search(url)
    if not m:
        return None
    kind, item_id = m.group(1).lower(), m.group(2)

    # Try tokens in order: the user's PKCE token first (gives the same public
    # metadata), then client-credentials. The user token frequently dies
    # overnight because the browser extension only refreshes it while the tab is
    # active (see #8); when it 401s we MUST fall through to client-credentials —
    # which works for all public content and is independent of the tab — instead
    # of degrading to the artist-less oembed thumbnail. That made Spotify cards
    # come back with a cover but no artist/title after the token lapsed.
    tokens: list[str] = []
    if _token_getter:
        try:
            ut = await _token_getter()
        except Exception:
            ut = None
        if ut:
            tokens.append(ut)
    cc = await _get_cc_token()
    if cc and cc not in tokens:
        tokens.append(cc)
    if not tokens:
        return await _oembed_fallback(url)

    try:
        async with _HTTP.ashared() as client:
            for tok in tokens:
                meta, status = await _request_meta(client, kind, item_id, tok)
                if meta is not None:
                    return meta
                # Non-auth failure (e.g. 404 not found) — another token won't help.
                if status not in (401, 403):
                    break
    except Exception:
        pass
    return await _oembed_fallback(url)
