"""ISRC cross-service resolver.

  POST /api/isrc/resolve   — take a URL, fetch ISRC, search Qobuz + Tidal + Deezer

Body:
  url   : source track/album URL
  skip  : service name to exclude from results (the source service)

Returns:
  {ok, isrc, title, artist, matches: {qobuz|tidal|deezer: MatchResult}}

MatchResult fields:
  service, track_id, track_url, album_id, url (album URL),
  title, artist, album, hires (Qobuz), bit_depth, sample_rate,
  audio_quality (Tidal), cover
"""
from __future__ import annotations

import asyncio
from typing import Optional

import httpx
from ripster import http_client as _HTTP
from fastapi import APIRouter

router = APIRouter()

_config: dict = {}
_detect_service = None
_fetch_meta_any = None

_QOBUZ_API  = "https://www.qobuz.com/api.json/0.2"
_TIDAL_API  = "https://api.tidal.com/v1"
_DEEZER_API = "https://api.deezer.com"
_TIMEOUT    = httpx.Timeout(connect=8, read=12, write=8, pool=5)


def install(app, ctx) -> None:
    global _config, _detect_service, _fetch_meta_any
    _config         = ctx.config
    _detect_service = ctx.detect_service
    _fetch_meta_any = ctx.fetch_meta
    app.include_router(router)


# ── Per-service ISRC searches ─────────────────────────────────────────────────

async def _qobuz_search_isrc(isrc: str, app_id: str, token: str) -> Optional[dict]:
    if not token:
        return None
    headers = {"X-User-Auth-Token": token, "X-App-Id": app_id}
    try:
        async with _HTTP.ashared() as c:
            r = await c.get(f"{_QOBUZ_API}/track/search",
                params={"query": isrc, "limit": 5, "app_id": app_id},
                headers=headers)
            if r.status_code != 200:
                return None
            items = (r.json().get("tracks") or {}).get("items") or []
            for t in items:
                ext_ids = t.get("external_ids") or {}
                t_isrc = (ext_ids.get("isrc") or "").upper()
                if t_isrc == isrc.upper():
                    alb      = t.get("album") or {}
                    alb_id   = str(alb.get("id", ""))
                    track_id = str(t.get("id", ""))
                    img_raw  = (alb.get("image") or {})
                    cover    = img_raw.get("large") or img_raw.get("small") or ""
                    return {
                        "service":     "qobuz",
                        "track_id":    track_id,
                        "track_url":   f"https://open.qobuz.com/track/{track_id}",
                        "album_id":    alb_id,
                        "url":         alb.get("url") or f"https://open.qobuz.com/album/{alb_id}",
                        "title":       t.get("title", ""),
                        "artist":      (t.get("performer") or (alb.get("artist") or {})).get("name", ""),
                        "album":       alb.get("title", ""),
                        "hires":       bool(t.get("hires") or t.get("hires_streamable")),
                        "bit_depth":   t.get("maximum_bit_depth") or t.get("bit_depth") or 16,
                        "sample_rate": t.get("maximum_sampling_rate") or t.get("sampling_rate") or 44.1,
                        "cover":       cover,
                    }
    except Exception:
        pass
    return None


async def _tidal_search_isrc(isrc: str, token: str, cc: str) -> Optional[dict]:
    if not token:
        return None
    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with _HTTP.ashared() as c:
            r = await c.get(f"{_TIDAL_API}/tracks",
                params={"isrc": isrc, "countryCode": cc or "US"},
                headers=headers)
            if r.status_code != 200:
                return None
            items = r.json().get("items") or []
            if not items:
                return None
            t       = items[0]
            alb     = t.get("album") or {}
            alb_id  = str(alb.get("id", ""))
            tid     = str(t.get("id", ""))
            cover_uuid = (alb.get("cover") or "").replace("-", "/")
            cover = f"https://resources.tidal.com/images/{cover_uuid}/320x320.jpg" if cover_uuid else ""
            return {
                "service":       "tidal",
                "track_id":      tid,
                "track_url":     f"https://listen.tidal.com/track/{tid}",
                "album_id":      alb_id,
                "url":           f"https://listen.tidal.com/album/{alb_id}",
                "title":         t.get("title", ""),
                "artist":        (t.get("artist") or {}).get("name", ""),
                "album":         alb.get("title", ""),
                "audio_quality": t.get("audioQuality", ""),
                "cover":         cover,
            }
    except Exception:
        pass
    return None


async def _deezer_search_isrc(isrc: str) -> Optional[dict]:
    """Deezer public ISRC endpoint — no auth required."""
    try:
        async with _HTTP.ashared() as c:
            r = await c.get(f"{_DEEZER_API}/track/isrc:{isrc}")
            if r.status_code != 200:
                return None
            t = r.json()
            if t.get("error") or not t.get("id"):
                return None
            alb    = t.get("album") or {}
            alb_id = str(alb.get("id", ""))
            tid    = str(t.get("id", ""))
            return {
                "service":   "deezer",
                "track_id":  tid,
                "track_url": f"https://www.deezer.com/track/{tid}",
                "album_id":  alb_id,
                "url":       f"https://www.deezer.com/album/{alb_id}",
                "title":     t.get("title", ""),
                "artist":    (t.get("artist") or {}).get("name", ""),
                "album":     alb.get("title", ""),
                "cover":     alb.get("cover_medium") or alb.get("cover") or "",
            }
    except Exception:
        pass
    return None


async def _deezer_search_upc(upc: str) -> Optional[dict]:
    """Deezer public UPC endpoint (album-level exact match) — no auth required."""
    upc = (upc or "").strip()
    if not upc:
        return None
    try:
        async with _HTTP.ashared() as c:
            r = await c.get(f"{_DEEZER_API}/album/upc:{upc}")
            if r.status_code != 200:
                return None
            alb = r.json()
            if alb.get("error") or not alb.get("id"):
                return None
            alb_id = str(alb.get("id", ""))
            return {
                "service":   "deezer",
                "album_id":  alb_id,
                "url":       alb.get("link") or f"https://www.deezer.com/album/{alb_id}",
                "title":     alb.get("title", ""),
                "artist":    (alb.get("artist") or {}).get("name", ""),
                "album":     alb.get("title", ""),
                "cover":     alb.get("cover_medium") or alb.get("cover") or "",
            }
    except Exception:
        pass
    return None


# ── Endpoint ──────────────────────────────────────────────────────────────────

@router.post("/api/isrc/resolve")
async def resolve_isrc(body: dict):
    url  = (body.get("url") or "").strip()
    skip = (body.get("skip") or "").lower().strip()
    if not url:
        return {"ok": False, "error": "URL is required"}

    svc  = _detect_service(url) if _detect_service else ""
    meta = await _fetch_meta_any(url, svc) if _fetch_meta_any else None

    isrc = ((meta or {}).get("isrc") or "").strip().upper()
    if not isrc:
        return {"ok": False, "error": "ISRC не найден в метаданных"}

    qobuz_token = (_config.get("qobuz-auth-token") or "").strip()
    qobuz_appid = (_config.get("qobuz-app-id") or "312369995").strip()
    tidal_token = (_config.get("tidal-token") or "").strip()
    tidal_cc    = (_config.get("tidal-country") or "US").strip().upper()

    # Run all three searches concurrently; skip the source service
    tasks = {
        "qobuz":  _qobuz_search_isrc(isrc, qobuz_appid, qobuz_token) if skip != "qobuz"  else _noop(),
        "tidal":  _tidal_search_isrc(isrc, tidal_token, tidal_cc)    if skip != "tidal"  else _noop(),
        "deezer": _deezer_search_isrc(isrc)                          if skip != "deezer" else _noop(),
    }
    results = await asyncio.gather(*tasks.values())
    matches = {svc: r for svc, r in zip(tasks.keys(), results) if r}

    return {
        "ok":      True,
        "isrc":    isrc,
        "title":   (meta or {}).get("title", ""),
        "artist":  (meta or {}).get("artist", ""),
        "matches": matches,
    }


async def _noop() -> None:
    return None


# ── Smart release resolver: pick the BEST available source ───────────────────
def _norm(s: str) -> str:
    import re
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def verified_match(results: list[dict], title: str, artist: str) -> Optional[dict]:
    """Return the first *results* item whose normalized title+artist actually
    overlaps *title*/*artist*, or None if nothing passes. A plain text search
    (no ISRC/UPC available) can rank a same-word-different-release result
    first for common titles/artists — accepting result[0] unconditionally
    served flatly wrong releases (e.g. a Spotify→Deezer conversion picking an
    unrelated track that merely shares a word). Same title-overlap +
    optional-artist-overlap heuristic used for Apple's multi-region match."""
    tnorm, anorm = _norm(title), _norm(artist)
    if not tnorm:
        return None
    for it in results:
        it_t, it_a = _norm(it.get("title", "")), _norm(it.get("artist", ""))
        t_ok = tnorm and (tnorm in it_t or it_t in tnorm or
                          len(set(tnorm.split()) & set(it_t.split())) >= max(1, len(tnorm.split()) // 2))
        a_ok = (not anorm) or (anorm in it_a or it_a in anorm or
                               bool(set(anorm.split()) & set(it_a.split())))
        if t_ok and a_ok:
            return it
    return None


async def _apple_match(title: str, artist: str):
    """Find the release on Apple via the multi-region search (account region +
    NZ), so pre-releases out in NZ before the account region are caught. Returns
    an apple match dict or None."""
    if not title:
        return None
    try:
        from ripster.routes.discovery import _search_apple
        q = (f"{artist} {title}" if artist else title).strip()
        res = (await _search_apple(q, "album", 8, "")).get("results") or []
    except Exception:
        return None
    best = verified_match(res, title, artist)
    if not best:
        return None
    return {
        "service": "apple", "url": best.get("url", ""), "quality": "alac",
        "title": best.get("title", ""), "artist": best.get("artist", ""),
        "album": best.get("title", ""), "cover": best.get("cover", ""),
        "region": "nz" if "/nz/" in best.get("url", "") else "",
    }


@router.post("/api/release/smart-resolve")
async def smart_resolve(body: dict):
    """Pick the best available source for a release and (optionally) queue it.

    Priority: Apple first (multi-region → catches NZ pre-releases, downloads via
    the public wrapper with no account), then Qobuz (Hi-Res), Tidal, Deezer.
    Body: {url?|isrc?, title?, artist?}. Returns {ok, chosen, matches}."""
    url    = (body.get("url") or "").strip()
    title  = (body.get("title") or "").strip()
    artist = (body.get("artist") or "").strip()
    isrc   = (body.get("isrc") or "").strip().upper()

    if url and (not isrc or not title):
        try:
            svc  = _detect_service(url) if _detect_service else ""
            meta = await _fetch_meta_any(url, svc) if _fetch_meta_any else None
            if meta:
                isrc   = isrc   or (meta.get("isrc")   or "").strip().upper()
                title  = title  or (meta.get("title")  or "")
                artist = artist or (meta.get("artist") or "")
        except Exception:
            pass

    matches: dict = {}
    # Apple (always available via wrapper; multi-region catches NZ pre-releases)
    apple_task = asyncio.create_task(_apple_match(title, artist))
    # Qobuz / Tidal / Deezer by ISRC (only when we have an ISRC)
    if isrc:
        qtoken = (_config.get("qobuz-auth-token") or "").strip()
        qappid = (_config.get("qobuz-app-id") or "312369995").strip()
        ttoken = (_config.get("tidal-token") or "").strip()
        tcc    = (_config.get("tidal-country") or "US").strip().upper()
        q, t, d = await asyncio.gather(
            _qobuz_search_isrc(isrc, qappid, qtoken),
            _tidal_search_isrc(isrc, ttoken, tcc),
            _deezer_search_isrc(isrc),
        )
        if q: q["quality"] = "27";       matches["qobuz"]  = q
        if t: t["quality"] = "lossless"; matches["tidal"]  = t
        if d: d["quality"] = "flac";     matches["deezer"] = d
    ap = await apple_task
    if ap:
        matches["apple"] = ap

    order  = ["apple", "qobuz", "tidal", "deezer"]
    chosen = next((matches[s] for s in order if s in matches), None)
    return {"ok": bool(chosen), "isrc": isrc, "title": title, "artist": artist,
            "chosen": chosen, "matches": matches}
