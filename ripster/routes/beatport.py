"""Beatport catalog search, preview, and upcoming releases routes.

  GET  /api/beatport/search?q=...&type=tracks|releases&page=1
  GET  /api/beatport/release/{id}   — release detail + tracklist
  GET  /api/beatport/upcoming        — new/upcoming releases (latest)

Authentication: Beatport OAuth2 password grant using credentials from config.
Token is cached in memory and refreshed on 401.
"""
from __future__ import annotations

import asyncio
import time
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException, Query

router = APIRouter()

_config: dict = {}

_TIMEOUT   = httpx.Timeout(connect=10, read=20, write=10, pool=5)
_BASE      = "https://api.beatport.com/v4"
_CLIENT_ID = "Zy2K9Wvy6DkUds7g8s1GNMHfk17E5Ch2BWHlyaGY"  # Serato DJ Lite
_REDIRECT  = "seratodjlite://beatport"
_UA        = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

_token_cache: dict = {}   # {"access_token": ..., "refresh_token": ..., "expires_at": float}
_token_lock = asyncio.Lock()


def install(app, ctx) -> None:
    global _config
    _config = ctx.config
    app.include_router(router)


# ── Auth helpers ───────────────────────────────────────────────────────────────

async def _auth_full(username: str, password: str) -> Optional[dict]:
    """Full 4-step Beatport OAuth authorization code flow. Returns token dict or None."""
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=False) as c:
            # Step 1: start OAuth — server sets session cookie
            r = await c.get(f"{_BASE}/auth/o/authorize/", params={
                "client_id": _CLIENT_ID, "response_type": "code", "redirect_uri": _REDIRECT,
            }, headers={"User-Agent": _UA})
            if r.status_code != 302:
                return None

            loc      = r.headers.get("location", "")
            base_url = f"{r.request.url.scheme}://{r.request.url.host}"
            referer  = (base_url + loc) if loc.startswith("/") else loc

            # Step 2: post credentials
            r = await c.post(f"{_BASE}/auth/login/", json={"username": username, "password": password},
                             headers={"User-Agent": _UA, "Referer": referer})
            if r.status_code != 200:
                return None

            # Step 3: get authorization code
            r = await c.get(f"{_BASE}/auth/o/authorize/", params={
                "client_id": _CLIENT_ID, "response_type": "code", "redirect_uri": _REDIRECT,
            }, headers={"User-Agent": _UA})
            if r.status_code != 302:
                return None

            loc = r.headers.get("location", "")
            if "code=" not in loc:
                return None
            code = loc.split("code=")[1].split("&")[0]

            # Step 4: exchange code for tokens
            r = await c.post(f"{_BASE}/auth/o/token/", data={
                "client_id": _CLIENT_ID, "code": code,
                "grant_type": "authorization_code", "redirect_uri": _REDIRECT,
            })
            if r.status_code != 200:
                return None
            return r.json()
    except Exception:
        return None


async def _auth_refresh(refresh_token: str) -> Optional[dict]:
    """Exchange a refresh token for a new access token."""
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.post(f"{_BASE}/auth/o/token/", data={
                "client_id":     _CLIENT_ID,
                "grant_type":    "refresh_token",
                "refresh_token": refresh_token,
            })
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None


async def _get_token() -> Optional[str]:
    """Return a valid access token, refreshing if expired."""
    async with _token_lock:
        now = time.time()
        if _token_cache.get("access_token") and _token_cache.get("expires_at", 0) > now + 60:
            return _token_cache["access_token"]

        # Try refresh token first (fast, no credentials needed)
        if _token_cache.get("refresh_token"):
            d = await _auth_refresh(_token_cache["refresh_token"])
            if d and d.get("access_token"):
                _token_cache["access_token"]  = d["access_token"]
                _token_cache["refresh_token"] = d.get("refresh_token", _token_cache["refresh_token"])
                _token_cache["expires_at"]    = now + d.get("expires_in", 3600)
                return _token_cache["access_token"]

        # Full login flow
        username = (_config.get("beatport-username") or "").strip()
        password = (_config.get("beatport-password") or "").strip()
        if not username or not password:
            return None

        d = await _auth_full(username, password)
        if not d or not d.get("access_token"):
            return None
        _token_cache["access_token"]  = d["access_token"]
        _token_cache["refresh_token"] = d.get("refresh_token", "")
        _token_cache["expires_at"]    = now + d.get("expires_in", 3600)
        return _token_cache["access_token"]


def _auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ── Preview URL helper ─────────────────────────────────────────────────────────

def _preview_url(track: dict) -> str:
    """Build geo-samples preview URL from track data."""
    sample = track.get("sample_url") or ""
    if sample:
        return sample
    # Fallback: construct from track ID (format used by beatport)
    tid = track.get("id")
    if tid:
        return f"https://geo-samples.beatport.com/track/{tid}.LOFI.mp3"
    return ""


def _dict_name(val) -> str:
    """Safely extract .name from a dict field that might be a string or None."""
    if isinstance(val, dict):
        return val.get("name") or ""
    if isinstance(val, str):
        return val
    return ""


def _fmt_track(t: dict) -> dict:
    artists  = [a.get("name", "") for a in (t.get("artists") or []) if isinstance(a, dict)]
    remixers = [a.get("name", "") for a in (t.get("remixers") or []) if isinstance(a, dict)]
    key_raw  = t.get("key") or {}
    key      = (_dict_name(key_raw.get("standard")) or _dict_name(key_raw)) if isinstance(key_raw, dict) else str(key_raw)
    bpm      = t.get("bpm") or 0
    release  = t.get("release") if isinstance(t.get("release"), dict) else {}
    genre    = _dict_name(t.get("genre"))
    image    = t.get("image") if isinstance(t.get("image"), dict) else {}
    art_raw  = (release.get("image") if isinstance(release.get("image"), dict) else image or {}).get("uri") or ""
    art      = art_raw.replace("{w}", "400").replace("{h}", "400") if art_raw else ""
    return {
        "id":          t.get("id"),
        "type":        "track",
        "title":       t.get("name") or "",
        "mix":         t.get("mix_name") or "",
        "artist":      ", ".join(artists),
        "remixers":    ", ".join(remixers),
        "genre":       genre,
        "bpm":         bpm,
        "key":         key,
        "year":        (t.get("publish_date") or "")[:4],
        "label":       (t.get("label") or {}).get("name") or "",
        "release":     release.get("name") or "",
        "release_id":  release.get("id"),
        "artworkUrl":  art,
        "previewUrl":  _preview_url(t),
        "url":         f"https://www.beatport.com/track/{t.get('slug','')}/{t.get('id','')}",
        "duration_ms": t.get("duration", {}).get("milliseconds") if isinstance(t.get("duration"), dict) else t.get("length_ms") or 0,
    }


def _fmt_release(r: dict) -> dict:
    artists = [a.get("name", "") for a in (r.get("artists") or [])]
    art_raw = (r.get("image") or {}).get("uri") or ""
    art = art_raw.replace("{w}", "400").replace("{h}", "400") if art_raw else ""
    return {
        "id":          r.get("id"),
        "type":        "release",
        "title":       r.get("name") or "",
        "artist":      ", ".join(artists),
        "year":        (r.get("publish_date") or r.get("new_release_date") or "")[:4],
        "label":       (r.get("label") or {}).get("name") or "",
        "genre":       (r.get("genre") or {}).get("name") or "",
        "trackCount":  r.get("track_count") or 0,
        "artworkUrl":  art,
        "url":         f"https://www.beatport.com/release/{r.get('slug','')}/{r.get('id','')}",
        "is_upcoming": r.get("is_pre_order") or False,
    }


# ── Routes ─────────────────────────────────────────────────────────────────────

@router.get("/api/beatport/search")
async def beatport_search(
    q: str = Query(..., min_length=1),
    result_type: str = Query("tracks", alias="type", pattern="^(tracks|releases)$"),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=5, le=50),
):
    token = await _get_token()
    if not token:
        raise HTTPException(401, "Beatport: настрой логин/пароль в Settings → Beatport")

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.get(f"{_BASE}/catalog/search/",
                            params={"q": q, "type": result_type, "page": page, "per_page": per_page},
                            headers=_auth_headers(token))
        if r.status_code == 401:
            _token_cache.clear()
            raise HTTPException(401, "Beatport: токен истёк — обнови логин в Settings")
        if r.status_code != 200:
            raise HTTPException(502, f"Beatport API: {r.status_code}: {r.text[:120]}")
    except HTTPException:
        raise
    except httpx.RequestError as e:
        raise HTTPException(502, f"Beatport: сетевая ошибка — {e}")

    try:
        data = r.json()
    except Exception as e:
        raise HTTPException(502, f"Beatport вернул не-JSON ответ: {e}")

    # Beatport v4 search returns {"tracks": {"count":N, "data":[...]}, "releases": {...}}
    # Fall back to flat {"count":N, "data":[...]} or {"count":N, "results":[...]}
    section = data.get(result_type) if isinstance(data, dict) else None
    if isinstance(section, dict):
        items = section.get("data") or section.get("results") or []
        count = section.get("count") or len(items)
    elif isinstance(section, list):
        items = section
        count = len(items)
    else:
        # flat response or unexpected shape
        items = data.get("data") or data.get("results") or []
        count = data.get("count") or len(items)

    fmt = _fmt_track if result_type == "tracks" else _fmt_release
    try:
        formatted = [fmt(item) for item in items]
    except Exception as e:
        raise HTTPException(502, f"Beatport: ошибка парсинга ответа — {e}")

    return {"type": result_type, "count": count, "page": page, "results": formatted}


@router.get("/api/beatport/release/{release_id}")
async def beatport_release(release_id: int):
    token = await _get_token()
    if not token:
        raise HTTPException(401, "Beatport: настрой логин/пароль в Settings → Beatport")

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r_rel = await c.get(f"{_BASE}/catalog/releases/{release_id}/", headers=_auth_headers(token))
            r_trk = await c.get(
                f"{_BASE}/catalog/releases/{release_id}/tracks/",
                params={"per_page": 200},
                headers=_auth_headers(token),
            )
    except httpx.RequestError as e:
        raise HTTPException(502, f"Beatport: сетевая ошибка — {e}")

    if r_rel.status_code != 200:
        raise HTTPException(404, "Релиз не найден")

    rel    = r_rel.json()
    tracks = (r_trk.json().get("data") or []) if r_trk.status_code == 200 else []
    return {
        **_fmt_release(rel),
        "tracks": [_fmt_track(t) for t in tracks],
    }


@router.get("/api/beatport/upcoming")
async def beatport_upcoming(
    genre_id: Optional[int] = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=5, le=50),
):
    """Latest / upcoming releases sorted by release date desc."""
    token = await _get_token()
    if not token:
        raise HTTPException(401, "Beatport: настрой логин/пароль в Settings → Beatport")

    params: dict = {"page": page, "per_page": per_page, "order_by": "-publish_date"}
    if genre_id:
        params["genre_id"] = genre_id

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.get(f"{_BASE}/catalog/releases/", params=params, headers=_auth_headers(token))
        if r.status_code != 200:
            raise HTTPException(502, f"Beatport API: {r.status_code}")
    except httpx.RequestError as e:
        raise HTTPException(502, f"Beatport: сетевая ошибка — {e}")

    data  = r.json()
    items = data.get("data") or []
    count = data.get("count") or len(items)
    return {
        "type":    "releases",
        "count":   count,
        "page":    page,
        "results": [_fmt_release(item) for item in items],
    }


@router.get("/api/beatport/genres")
async def beatport_genres():
    token = await _get_token()
    if not token:
        raise HTTPException(401, "Beatport: настрой логин/пароль")

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.get(f"{_BASE}/catalog/genres/", headers=_auth_headers(token))
        if r.status_code != 200:
            return {"genres": []}
    except httpx.RequestError:
        return {"genres": []}

    items = r.json().get("data") or []
    return {"genres": [{"id": g["id"], "name": g["name"]} for g in items]}
