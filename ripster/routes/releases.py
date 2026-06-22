"""
Multi-service release radar — Qobuz and Tidal.

Uses asyncio concurrency (semaphore) so 200 artists scan in ~15s instead of hours.
"""
from __future__ import annotations

import asyncio
import base64
import json
from datetime import datetime, timedelta

import httpx
from fastapi import APIRouter

router     = APIRouter()
_config: dict = {}
_broadcast    = None

_TIDAL_API   = "https://api.tidal.com/v1"
_QOBUZ_API   = "https://www.qobuz.com/api.json/0.2"
_CONCURRENCY = 10   # parallel artist→album requests per service
_TIMEOUT     = httpx.Timeout(connect=10, read=20, write=10, pool=5)


def install(app, ctx) -> None:
    global _config, _broadcast
    _config    = ctx.config
    _broadcast = ctx.broadcast
    app.include_router(router)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _cutoff(days: int) -> str:
    return (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

def _qobuz_app_id() -> str:
    return (_config.get("qobuz-app-id") or "").strip() or "798273057"

def _qobuz_token() -> str:
    return (_config.get("qobuz-auth-token") or "").strip()

def _tidal_token() -> str:
    return (_config.get("tidal-token") or "").strip()

def _tidal_country() -> str:
    return (_config.get("tidal-country") or "US").strip().upper() or "US"

def _tidal_cover(uuid: str, size: int = 320) -> str:
    if not uuid:
        return ""
    return f"https://resources.tidal.com/images/{uuid.replace('-', '/')}/{size}x{size}.jpg"

def _decode_jwt(token: str) -> dict:
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    padded = parts[1] + "=" * (4 - len(parts[1]) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(padded))
    except Exception:
        return {}

async def _tidal_user_id() -> str:
    token = _tidal_token()
    if not token:
        return ""
    uid = str(_decode_jwt(token).get("uid", "") or "")
    if uid:
        return uid
    return (_config.get("tidal-user-id") or "").strip()


# ── Qobuz ─────────────────────────────────────────────────────────────────────

async def _qobuz_fetch_artist(sem: asyncio.Semaphore, c: httpx.AsyncClient,
                               artist: dict, app_id: str, headers: dict, cutoff: str) -> list[dict]:
    async with sem:
        try:
            r = await c.get(f"{_QOBUZ_API}/artist/get",
                params={"artist_id": artist["id"], "extra": "albums",
                        "limit": 100, "app_id": app_id},
                headers=headers)
            if r.status_code != 200:
                return []
            items = (r.json().get("albums") or {}).get("items") or []
            out   = []
            for alb in items:
                date = (alb.get("release_date_original") or "")[:10]
                if not date or date < cutoff:
                    continue
                alb_id  = str(alb.get("id", ""))
                lbl_raw = alb.get("label")
                label   = lbl_raw.get("name", "") if isinstance(lbl_raw, dict) else str(lbl_raw or "")
                img_raw = alb.get("image")
                cover   = img_raw.get("large", "") if isinstance(img_raw, dict) else ""
                out.append({
                    "id":      alb_id,
                    "title":   alb.get("title", ""),
                    "artist":  artist.get("name", ""),
                    "type":    (alb.get("release_type") or "album").lower(),
                    "date":    date, "year": date[:4],
                    "tracks":  alb.get("tracks_count"),
                    "label":   label, "cover": cover,
                    "url":     alb.get("url", "") or f"https://open.qobuz.com/album/{alb_id}",
                    "hires":   alb.get("hires", False),
                    "service": "qobuz",
                })
            return out
        except Exception:
            return []


@router.get("/api/releases/qobuz")
async def qobuz_releases(days: int = 30):
    app_id = _qobuz_app_id()
    token  = _qobuz_token()
    if not token:
        return {"ok": False, "error": "Qobuz auth-token не настроен (Settings → Qobuz)", "releases": []}

    headers = {"X-User-Auth-Token": token, "X-App-Id": app_id}
    cutoff  = _cutoff(days)

    try:
        # 1 — paginate followed artists
        artists: list[dict] = []
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            offset = 0
            while True:
                r = await c.get(f"{_QOBUZ_API}/favorite/getUserFavorites",
                    params={"type": "artists", "limit": 50, "offset": offset, "app_id": app_id},
                    headers=headers)
                if r.status_code == 401:
                    return {"ok": False, "error": "Qobuz: токен истёк. Обнови qobuz-auth-token в Settings.", "releases": []}
                if r.status_code != 200:
                    return {"ok": False, "error": f"Qobuz API {r.status_code}", "releases": []}
                data  = r.json()
                items = (data.get("artists") or {}).get("items") or []
                artists.extend(items)
                total_rep = (data.get("artists") or {}).get("total", len(artists))
                if len(items) < 50 or len(artists) >= total_rep:
                    break
                offset += 50

        if not artists:
            return {"ok": True, "releases": [], "artists_checked": 0}

        total = len(artists)
        if _broadcast:
            await _broadcast({"type": "releases_scan_start", "phase": "albums",
                               "total": total, "service": "qobuz"})

        # 2 — concurrent fan-out: 10 artists at a time
        completed  = [0]
        found_so_far = [0]
        step = max(1, total // 20)

        async with httpx.AsyncClient(timeout=_TIMEOUT, limits=httpx.Limits(max_connections=20)) as c:
            sem = asyncio.Semaphore(_CONCURRENCY)

            async def _fetch_one(artist: dict) -> list[dict]:
                result = await _qobuz_fetch_artist(sem, c, artist, app_id, headers, cutoff)
                completed[0] += 1
                found_so_far[0] += len(result)
                if _broadcast and (completed[0] % step == 0 or completed[0] == total):
                    await _broadcast({
                        "type": "releases_scan_progress",
                        "current": completed[0], "total": total,
                        "artist": artist.get("name", "?"),
                        "found":  found_so_far[0],
                        "service": "qobuz",
                    })
                return result

            batches = await asyncio.gather(*[_fetch_one(a) for a in artists])

        releases: list[dict] = []
        seen: set[str]       = set()
        for batch in batches:
            for rel in batch:
                if rel["id"] not in seen:
                    seen.add(rel["id"])
                    releases.append(rel)

        releases.sort(key=lambda x: x["date"], reverse=True)

        if _broadcast:
            await _broadcast({"type": "releases_scan_done", "artists_checked": total,
                               "releases_count": len(releases), "service": "qobuz"})

        return {"ok": True, "releases": releases, "artists_checked": total}

    except Exception as e:
        return {"ok": False, "error": str(e), "releases": []}


# ── Tidal ─────────────────────────────────────────────────────────────────────

async def _tidal_fetch_artist(sem: asyncio.Semaphore, c: httpx.AsyncClient,
                               artist: dict, cc: str, hdr: dict, cutoff: str) -> list[dict]:
    async with sem:
        try:
            # paginate — some artists have 100+ releases
            items: list[dict] = []
            offset = 0
            while True:
                r = await c.get(f"{_TIDAL_API}/artists/{artist['id']}/albums",
                    params={"limit": 100, "offset": offset, "countryCode": cc, "filter": "ALL"},
                    headers=hdr)
                if r.status_code != 200:
                    break
                page = r.json().get("items") or []
                items.extend(page)
                if len(page) < 100:
                    break
                offset += 100
            out = []
            for alb in items:
                date = (alb.get("releaseDate") or "")[:10]
                if not date or date < cutoff:
                    continue
                alb_id    = str(alb.get("id", ""))
                alb_type  = alb.get("type", "ALBUM").lower()
                type_norm = {"album": "album", "ep": "ep", "single": "single",
                             "compilation": "compilation"}.get(alb_type, "album")
                out.append({
                    "id":      alb_id,
                    "title":   alb.get("title", ""),
                    "artist":  artist.get("name", ""),
                    "type":    type_norm,
                    "date":    date, "year": date[:4],
                    "tracks":  alb.get("numberOfTracks"),
                    "label":   "",
                    "cover":   _tidal_cover(alb.get("cover", "")),
                    "url":     f"https://listen.tidal.com/album/{alb_id}",
                    "service": "tidal",
                })
            return out
        except Exception:
            return []


@router.get("/api/releases/tidal")
async def tidal_releases(days: int = 30):
    token = _tidal_token()
    if not token:
        return {"ok": False, "error": "Tidal token не настроен (Settings → Tidal)", "releases": []}

    hdr    = {"Authorization": f"Bearer {token}"}
    cc     = _tidal_country()
    cutoff = _cutoff(days)

    try:
        user_id = await _tidal_user_id()
        if not user_id:
            return {"ok": False, "error": "Не удалось определить Tidal user_id", "releases": []}

        # 1 — paginate favourite artists
        artists: list[dict] = []
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            offset = 0
            while True:
                r = await c.get(f"{_TIDAL_API}/users/{user_id}/favorites/artists",
                    params={"limit": 100, "offset": offset, "countryCode": cc},
                    headers=hdr)
                if r.status_code == 401:
                    return {"ok": False, "error": "Tidal: токен истёк. Обнови в Settings → Tidal.", "releases": []}
                if r.status_code != 200:
                    return {"ok": False, "error": f"Tidal API {r.status_code}", "releases": []}
                data  = r.json()
                items = data.get("items") or []
                for it in items:
                    a = it.get("item") or it
                    if a.get("id"):
                        artists.append(a)
                if len(items) < 100:
                    break
                offset += 100

        if not artists:
            return {"ok": True, "releases": [], "artists_checked": 0}

        total = len(artists)
        if _broadcast:
            await _broadcast({"type": "releases_scan_start", "phase": "albums",
                               "total": total, "service": "tidal"})

        # 2 — concurrent fan-out
        completed    = [0]
        found_so_far = [0]
        step = max(1, total // 20)

        async with httpx.AsyncClient(timeout=_TIMEOUT, limits=httpx.Limits(max_connections=20)) as c:
            sem = asyncio.Semaphore(_CONCURRENCY)

            async def _fetch_one(artist: dict) -> list[dict]:
                result = await _tidal_fetch_artist(sem, c, artist, cc, hdr, cutoff)
                completed[0] += 1
                found_so_far[0] += len(result)
                if _broadcast and (completed[0] % step == 0 or completed[0] == total):
                    await _broadcast({
                        "type": "releases_scan_progress",
                        "current": completed[0], "total": total,
                        "artist": artist.get("name", "?"),
                        "found":  found_so_far[0],
                        "service": "tidal",
                    })
                return result

            batches = await asyncio.gather(*[_fetch_one(a) for a in artists])

        releases: list[dict] = []
        seen: set[str]       = set()
        for batch in batches:
            for rel in batch:
                if rel["id"] not in seen:
                    seen.add(rel["id"])
                    releases.append(rel)

        releases.sort(key=lambda x: x["date"], reverse=True)

        if _broadcast:
            await _broadcast({"type": "releases_scan_done", "artists_checked": total,
                               "releases_count": len(releases), "service": "tidal"})

        return {"ok": True, "releases": releases, "artists_checked": total}

    except Exception as e:
        return {"ok": False, "error": str(e), "releases": []}
