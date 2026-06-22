"""SoundCloud browse routes — search via the public SoundCloud API v2.

  GET /api/soundcloud/search?q=...&kind=all|tracks|playlists
  GET /api/soundcloud/status

A ``client_id`` is scraped from soundcloud.com's JS bundle and cached. The
actual download still goes through the Lucida engine — this module only
powers the browse/search side of the SoundCloud tab.

Install: soundcloud.install(app, ctx)
"""
from __future__ import annotations

import re
import time
import urllib.parse as _urlparse

import httpx
from ripster import http_client as _HTTP
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response

router = APIRouter()
_cfg: dict = {}
_save_cfg = None

_API = "https://api-v2.soundcloud.com"
_UA  = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# Scraped client_id cache
_client_id: str      = ""
_client_id_ts: float = 0.0
_CLIENT_ID_TTL       = 12 * 3600   # re-scrape twice a day

_RE_SCRIPT = re.compile(r'<script[^>]+src="(https://[^"]+\.js)"')
_RE_CLIENT = re.compile(r'(?:client_id|clientId)\s*[=:]\s*"([0-9A-Za-z]{28,40})"')
_RE_ART    = re.compile(r'-(large|original|badge|small|tiny|mini|crop|t\d+x\d+)\.')


def install(app, ctx) -> None:
    global _cfg, _save_cfg
    _cfg      = ctx.config
    _save_cfg = getattr(ctx, "save_config", None)
    app.include_router(router)


# ── client_id ──────────────────────────────────────────────────────────────────

async def _get_client_id(force: bool = False) -> str:
    """Return a working SoundCloud client_id — config override, else scrape+cache.

    Bundles are fetched in parallel so a cold scrape takes ~2-4 s instead of
    the serial 10-20 s it used to take when checking each bundle sequentially.
    """
    global _client_id, _client_id_ts
    override = (_cfg.get("soundcloud-client-id") or "").strip()
    if override:
        return override
    now = time.time()
    if _client_id and not force and (now - _client_id_ts) < _CLIENT_ID_TTL:
        return _client_id
    import asyncio as _asyncio
    try:
        async with httpx.AsyncClient(timeout=12, headers={"User-Agent": _UA},
                                     follow_redirects=True) as c:
            r = await c.get("https://soundcloud.com/")
            scripts = _RE_SCRIPT.findall(r.text)
            # client_id lives in one of the later JS bundles — check them all in
            # parallel (up to 8 at once) so we don't wait for each one serially.
            scripts_rev = list(reversed(scripts))

            async def _try_bundle(url: str):
                try:
                    jr = await c.get(url)
                    m = _RE_CLIENT.search(jr.text)
                    return m.group(1) if m else None
                except Exception:
                    return None

            batch_size = 8
            for i in range(0, len(scripts_rev), batch_size):
                batch = scripts_rev[i:i + batch_size]
                results = await _asyncio.gather(*(_try_bundle(u) for u in batch))
                found = next((x for x in results if x), None)
                if found:
                    _client_id, _client_id_ts = found, now
                    print("[soundcloud] client_id scraped ok", flush=True)
                    return _client_id
    except Exception as e:
        print(f"[soundcloud] client_id scrape failed: {e}", flush=True)
    return _client_id   # possibly stale or empty


async def _prewarm_client_id() -> None:
    """Pre-warm the client_id cache at server startup so first SC play is instant."""
    try:
        cid = await _get_client_id()
        if cid:
            print(f"[soundcloud] client_id pre-warmed ({cid[:8]}…)", flush=True)
        else:
            print("[soundcloud] client_id pre-warm: no id found yet", flush=True)
    except Exception as e:
        print(f"[soundcloud] client_id pre-warm error: {e}", flush=True)


# ── Result normalisation ───────────────────────────────────────────────────────

def _artwork(url: str, size: str = "t500x500") -> str:
    """Upgrade a SoundCloud artwork URL to a larger size."""
    if not url:
        return ""
    return _RE_ART.sub(f"-{size}.", url)


def _norm_track(t: dict) -> dict:
    user = t.get("user") or {}
    art  = t.get("artwork_url") or user.get("avatar_url", "")
    # Search already returns the full description → parse the uploader's tracklist
    # right here so the UI gets it for free (no per-card follow-up request).
    tl = _parse_sc_tracklist(t.get("description") or "")
    return {
        "id":         t.get("id"),
        "kind":       "track",
        "title":      t.get("title", ""),
        "artist":     user.get("username", ""),
        "artwork":    _artwork(art, "original"),    # best available
        "artwork_sm": _artwork(art, "t500x500"),    # reliable fallback
        "duration": round((t.get("duration") or 0) / 1000),
        "url":      t.get("permalink_url", ""),
        "genre":    t.get("genre", ""),
        "plays":    t.get("playback_count"),
        "date":     (t.get("created_at") or "")[:10],
        "tracklist":     tl,
        "has_tracklist": bool(tl),
    }


def _norm_playlist(p: dict) -> dict:
    user = p.get("user") or {}
    art  = p.get("artwork_url") or user.get("avatar_url", "")
    # SC distinguishes album / ep / single / compilation / (user) playlist via
    # `set_type`. We pass it through so the UI can label them honestly — a
    # user-curated "Mixes" sampler is NOT the same as a release album.
    set_type = (p.get("set_type") or "").lower() or "playlist"
    return {
        "id":         p.get("id"),
        "kind":       "playlist",
        "set_type":   set_type,
        "title":      p.get("title", ""),
        "artist":     user.get("username", ""),
        "artwork":    _artwork(art, "original"),
        "artwork_sm": _artwork(art, "t500x500"),
        "duration": round((p.get("duration") or 0) / 1000),
        "url":      p.get("permalink_url", ""),
        "tracks":   p.get("track_count"),
        "genre":    p.get("genre", ""),
        "date":     (p.get("created_at") or "")[:10],
    }


# ── Routes ─────────────────────────────────────────────────────────────────────

@router.get("/api/soundcloud/search")
async def sc_search(q: str = Query(""), kind: str = Query("all"),
                    limit: int = Query(30)):
    q = (q or "").strip()
    if not q:
        return {"ok": False, "error": "Пустой запрос", "results": []}

    cid = await _get_client_id()
    if not cid:
        return {"ok": False,
                "error": "Не удалось получить client_id SoundCloud — попробуй позже.",
                "results": []}

    limit = max(1, min(limit, 50))
    ep = {"tracks":    "/search/tracks",
          "playlists": "/search/playlists",
          "albums":    "/search/albums"}.get(kind, "/search")

    async def _hit(client_id: str):
        async with _HTTP.ashared() as c:
            return await c.get(f"{_API}{ep}",
                               params={"q": q, "client_id": client_id, "limit": limit})

    # Up to 3 attempts:
    #   1. Use cached/scraped client_id.
    #   2. On 401/403 — re-scrape and retry (stale id).
    #   3. On any other failure (network / 5xx) — sleep a bit and retry once more.
    # First-call hiccups (cold scrape, fresh-id rate-limit) used to surface as
    # "поиск не работает" on the very first query of the session — now they're
    # masked transparently.
    import asyncio
    data = None
    last_err = ""
    for attempt in range(3):
        try:
            r = await _hit(cid)
        except Exception as e:
            last_err = f"Сеть: {e}"
            print(f"[soundcloud] search attempt {attempt+1}: {last_err}", flush=True)
            await asyncio.sleep(0.6)
            continue
        if r.status_code in (401, 403):
            last_err = f"SoundCloud API {r.status_code} (re-scraping client_id)"
            print(f"[soundcloud] search attempt {attempt+1}: {last_err}", flush=True)
            cid = await _get_client_id(force=True)
            if not cid:
                break
            continue
        if r.status_code == 200:
            try:
                data = r.json()
                break
            except Exception as e:
                last_err = f"JSON: {e}"
                print(f"[soundcloud] search attempt {attempt+1}: {last_err}", flush=True)
                await asyncio.sleep(0.4)
                continue
        last_err = f"SoundCloud API {r.status_code}"
        print(f"[soundcloud] search attempt {attempt+1}: {last_err}", flush=True)
        await asyncio.sleep(0.5)
    if data is None:
        return {"ok": False, "error": last_err or "Не удалось получить ответ", "results": []}

    results: list[dict] = []
    for item in data.get("collection") or []:
        k = item.get("kind")
        if k == "track":
            results.append(_norm_track(item))
        elif k == "playlist":
            results.append(_norm_playlist(item))

    return {"ok": True, "query": q, "results": results}


# ── Tracklist from the track's own description ───────────────────────────────────
# Uploaders (labels like Anjunadeep) list the real tracklist in the description.
# This is the authoritative source for THIS specific mix — far more reliable than
# fuzzy-matching MixesDB/YouTube, which can return a stranger's set.

_RE_TL_NUM = re.compile(r'^\s*(\d{1,3})\s*[.)]\s+(.+?)\s*$')
_RE_TL_TS  = re.compile(r'^\s*\[?(\d{1,2}:\d{2}(?::\d{2})?)\]?\s+(.+?)\s*$')


def _split_artist_title(body: str) -> tuple[str, str]:
    for sep in (" - ", " – ", " — "):
        if sep in body:
            a, t = body.split(sep, 1)
            return a.strip(), t.strip()
    return "", body.strip()


def _parse_sc_tracklist(desc: str) -> list[dict]:
    """Parse a numbered (``01. Artist - Title``) or timestamped (``1:23 Artist - Title``)
    tracklist out of a SoundCloud description. Returns [] when no plausible list."""
    out: list[dict] = []
    for raw in (desc or "").splitlines():
        ln = raw.strip()
        if not ln:
            continue
        ts, body = "", ""
        m = _RE_TL_NUM.match(ln)
        if m:
            body = m.group(2)
        else:
            m2 = _RE_TL_TS.match(ln)
            if m2:
                ts, body = m2.group(1), m2.group(2)
            else:
                continue
        artist, title = _split_artist_title(body)
        # Drop obvious non-track lines that slipped through (URLs, "Follow ...").
        if not title or title.lower().startswith(("http", "www.")):
            continue
        out.append({"timestamp": ts, "artist": artist, "title": title})
    # Require a real list, not one stray numbered line.
    return out if len(out) >= 3 else []


@router.get("/api/soundcloud/tracklist/{track_id}")
async def sc_tracklist(track_id: str):
    """Return the tracklist parsed from a SoundCloud track's own description."""
    cid = await _get_client_id()
    if not cid:
        return {"found": False}
    try:
        async with _HTTP.ashared() as c:
            r = await c.get(f"{_API}/tracks/{track_id}", params={"client_id": cid})
            if r.status_code in (401, 403):
                cid = await _get_client_id(force=True)
                r = await c.get(f"{_API}/tracks/{track_id}", params={"client_id": cid})
            if r.status_code != 200:
                return {"found": False}
            track = r.json()
    except Exception as e:
        print(f"[soundcloud] tracklist fetch failed: {e}", flush=True)
        return {"found": False}
    tl = _parse_sc_tracklist(track.get("description") or "")
    return {
        "found":          bool(tl),
        "tracklist":      tl,
        "has_timestamps": any(x["timestamp"] for x in tl),
        "source":         "soundcloud",
    }


@router.post("/api/soundcloud/tracklist-1001")
async def sc_tracklist_1001(request: Request):
    """Authoritative tracklist (with cue times) from 1001Tracklists, verified
    against the mix via the multi-stage matcher. Disk-cached 10 days, so this
    rarely hits the site. Body: {title, artist, dur, id, sc_tracklist?,
    source_urls?}. Returns {found, tracks, url, match, cached, error?}."""
    from fastapi.concurrency import run_in_threadpool
    from ripster import tl1001
    try:
        body = await request.json()
    except Exception:
        body = {}
    title = (body.get("title") or "").strip()
    if not title:
        return {"found": False, "error": "no title"}
    res = await run_in_threadpool(
        tl1001.tracklist_for,
        title, (body.get("artist") or "").strip(), int(body.get("dur") or 0),
        body.get("sc_tracklist") or [], body.get("source_urls") or [],
        str(body.get("id") or ""),
    )
    out = {"found": bool(res.get("ok")), "cached": res.get("cached", False),
           "source": "1001tracklists"}
    if res.get("ok"):
        out["tracks"] = res["tracks"]
        out["url"] = res.get("url")
        out["match"] = res.get("match")
        out["has_timestamps"] = any(t.get("seconds") is not None for t in res["tracks"])
    else:
        out["error"] = res.get("error")
        out["challenged"] = res.get("challenged", 0)
    return out


@router.get("/api/soundcloud/playlist/{playlist_id}")
async def sc_playlist(playlist_id: str):
    """Expand a SoundCloud playlist/album to a list of streamable tracks —
    used by the player to build a play queue from one click."""
    cid = await _get_client_id()
    if not cid:
        raise HTTPException(400, "Не удалось получить client_id SoundCloud")
    try:
        _to = httpx.Timeout(connect=4.0, read=6.0, write=6.0, pool=6.0)
        async with httpx.AsyncClient(timeout=_to, headers={"User-Agent": _UA}) as c:
            r = await c.get(f"{_API}/playlists/{playlist_id}", params={"client_id": cid})
            if r.status_code in (401, 403):
                cid = await _get_client_id(force=True)
                r = await c.get(f"{_API}/playlists/{playlist_id}", params={"client_id": cid})
            if r.status_code != 200:
                raise HTTPException(404, f"SoundCloud: плейлист не найден ({r.status_code})")
            data = r.json()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"SoundCloud API error: {e}")

    user = data.get("user") or {}
    raw_tracks = data.get("tracks") or []
    print(f"[soundcloud] playlist {playlist_id}: {len(raw_tracks)} raw tracks, "
          f"title={data.get('title', '?')!r}", flush=True)

    # SoundCloud sometimes returns stub track objects (id-only); resolve them in
    # a single batched call so the playlist plays end-to-end without per-track
    # round trips.
    stub_ids = [str(t.get("id")) for t in raw_tracks
                if t.get("id") and not (t.get("title") or t.get("user"))]
    resolved: dict[int, dict] = {}
    if stub_ids:
        try:
            import asyncio as _aio
            _to = httpx.Timeout(connect=4.0, read=6.0, write=6.0, pool=6.0)
            async with httpx.AsyncClient(timeout=_to, headers={"User-Agent": _UA}) as c:
                # /tracks?ids=A,B,C max 50 at a time — fire chunks in parallel.
                chunks = [",".join(stub_ids[i:i + 50]) for i in range(0, len(stub_ids), 50)]
                async def _fetch_chunk(chunk):
                    try:
                        rr = await c.get(f"{_API}/tracks",
                                         params={"ids": chunk, "client_id": cid})
                        return rr.json() if rr.status_code == 200 else []
                    except Exception:
                        return []
                results = await _aio.gather(*[_fetch_chunk(ch) for ch in chunks])
                for batch in results:
                    for t in batch:
                        resolved[t.get("id")] = t
        except Exception:
            pass

    tracks: list[dict] = []
    for t in raw_tracks:
        tid = t.get("id")
        if not (t.get("title") or t.get("user")):
            t = resolved.get(tid, t)
        if not t.get("title") and not tid:
            continue
        tracks.append(_norm_track(t))

    print(f"[soundcloud] playlist {playlist_id}: returning {len(tracks)} normalised tracks",
          flush=True)

    return {
        "ok":       True,
        "id":       data.get("id"),
        "title":    data.get("title", ""),
        "artist":   user.get("username", ""),
        "artwork":  _artwork(data.get("artwork_url") or user.get("avatar_url", ""), "original"),
        "duration": round((data.get("duration") or 0) / 1000),
        "tracks":   tracks,
    }


async def _resolve_drm_hls(c, trans, cid, is_full_fn, prefer: str = "ctr"):
    """Resolve a DRM-encrypted HLS stream URL from SoundCloud transcodings.

    prefer='ctr'  → try CENC/Widevine (ctr-encrypted-hls) first — works in Chrome/Edge/Firefox.
    prefer='cbc'  → try FairPlay CBCS (cbc-encrypted-hls) first — works in Safari.
    Always falls back to the other format if the preferred one is unavailable.
    Returns (m3u8_url, license_auth_token, protocol) or ('', '', '') on failure.
    """
    order = (("ctr-encrypted-hls", "cbc-encrypted-hls") if prefer == "ctr"
             else ("cbc-encrypted-hls", "ctr-encrypted-hls"))
    for proto in order:
        enc = (next((t for t in trans
                     if (t.get("format") or {}).get("protocol") == proto
                     and is_full_fn(t)), None) or
               next((t for t in trans
                     if (t.get("format") or {}).get("protocol") == proto), None))
        if not enc:
            continue
        try:
            sr = await c.get(enc["url"], params={"client_id": cid})
            if sr.status_code == 200:
                j = sr.json() or {}
                m3u8_url = j.get("url", "")
                lat = j.get("licenseAuthToken") or ""
                if m3u8_url:
                    if lat:
                        print(f"[soundcloud] DRM {proto} resolved ok, licenseAuthToken present", flush=True)
                    else:
                        print(f"[soundcloud] DRM {proto}: resolved (no licenseAuthToken — key may be inline)", flush=True)
                    return m3u8_url, lat, proto
            else:
                print(f"[soundcloud] DRM {proto} → {sr.status_code}: {sr.text[:120]}", flush=True)
        except Exception as e:
            print(f"[soundcloud] DRM resolve error ({proto}): {e}", flush=True)
    return "", "", ""


def _drm_response(m3u8_url: str, lat: str, proto: str, track_json: dict) -> dict:
    """Build the JSON dict returned to the player for a DRM-HLS stream."""
    art_raw = (track_json.get("artwork_url") or
               (track_json.get("user") or {}).get("avatar_url", ""))
    return {
        "url":           m3u8_url,
        "format":        "drm-hls-cbc" if "cbc" in proto else "drm-hls-ctr",
        "license_token": lat,
        "artwork":       _artwork(art_raw, "t500x500") if art_raw else "",
    }


@router.get("/api/stream/soundcloud/{track_id}")
async def sc_stream(track_id: str, request: Request, name: str = "", artist: str = "",
                    prefer: str = Query("ctr", pattern="^(ctr|cbc)$")):
    """Resolve a playable stream URL for a SoundCloud track — progressive MP3
    preferred (direct, <audio>-friendly), HLS as fallback. Used by the player.

    If the user has saved an oauth_token (Settings → SoundCloud) we include it
    as ``Authorization: OAuth …`` so Go+ / region-restricted tracks resolve.
    """
    cid = await _get_client_id()
    if not cid:
        raise HTTPException(400, "Не удалось получить client_id SoundCloud")
    oauth = (_cfg.get("soundcloud-oauth-token") or "").strip()
    headers = {"User-Agent": _UA}
    if oauth:
        headers["Authorization"] = (oauth if oauth.lower().startswith("oauth ")
                                    else f"OAuth {oauth}")
    prog = hls = None
    chosen = None
    try:
        # Balanced timeouts: 2s connect caused false ConnectTimeout on RU networks
        # where SC's CDN sometimes needs 3-4s for first TCP handshake. 4s connect
        # is still ~3x tighter than the old 15s blanket and barely user-visible.
        # We also retry once on ConnectTimeout — first try usually wakes up DNS/CDN.
        import asyncio as _aio
        _to = httpx.Timeout(connect=4.0, read=6.0, write=6.0, pool=6.0)
        async with httpx.AsyncClient(timeout=_to, headers=headers) as c:
            async def _get_track():
                for attempt in range(2):
                    try:
                        return await c.get(f"{_API}/tracks/{track_id}",
                                           params={"client_id": cid})
                    except httpx.ConnectTimeout:
                        if attempt == 1:
                            raise
                        await _aio.sleep(0.3)
                return None
            r = await _get_track()
            if r.status_code in (401, 403):
                cid = await _get_client_id(force=True)
                r = await c.get(f"{_API}/tracks/{track_id}", params={"client_id": cid})
            if r.status_code != 200:
                print(f"[soundcloud] /tracks/{track_id} → {r.status_code}: "
                      f"{r.text[:160]}", flush=True)
                raise HTTPException(404,
                    f"SoundCloud: трек не найден ({r.status_code})")
            track_json = r.json()
            trans = ((track_json.get("media") or {}).get("transcodings") or [])
            # Prefer non-preview transcodings (SC sometimes returns 30-sec snippets)
            def _is_full(t): return not (t.get("snipped") or
                                          (t.get("quality") == "preview"))
            prog = next((t for t in trans
                         if (t.get("format") or {}).get("protocol") == "progressive"
                         and _is_full(t)), None) or next(
                        (t for t in trans
                         if (t.get("format") or {}).get("protocol") == "progressive"), None)
            hls  = next((t for t in trans
                         if (t.get("format") or {}).get("protocol") == "hls"
                         and _is_full(t)), None) or next(
                        (t for t in trans
                         if (t.get("format") or {}).get("protocol") == "hls"), None)
            # Try transcodings in priority order: progressive first (direct MP3),
            # then HLS as fallback. If a transcoding URL fails to resolve, fall
            # through to the next instead of immediately raising 502 — many
            # tracks return one stale-signed URL while the alternative works.
            candidates = [t for t in (prog, hls) if t]
            has_encrypted = any(
                "encrypted-hls" in ((t.get("format") or {}).get("protocol") or "")
                for t in trans)
            if not candidates:
                if has_encrypted and oauth:
                    _d_url, _d_lat, _d_proto = await _resolve_drm_hls(c, trans, cid, _is_full, prefer)
                    if _d_url:
                        return _drm_response(_d_url, _d_lat, _d_proto, track_json)
                if has_encrypted:
                    raise HTTPException(451,
                        "Трек защищён DRM — добавь OAuth-токен в Settings → SoundCloud.")
                print(f"[soundcloud] no transcodings for {track_id} "
                      f"(streamable={track_json.get('streamable')}, "
                      f"policy={track_json.get('policy')})", flush=True)
                raise HTTPException(404, "SoundCloud: трек недоступен для стриминга")

            # Resolve all candidates in parallel — first successful wins. This
            # cuts user-facing latency: progressive(MP3) + HLS used to take 2x
            # the slowest request when one returned 404 before the other could
            # resolve. Now we wait for whichever returns first.
            import asyncio as _aio

            async def _try_cand(cand: dict, cur_cid: str) -> "tuple[str, dict, int, str, str]":
                cu = cand["url"]
                try:
                    sr = await c.get(cu, params={"client_id": cur_cid})
                    if sr.status_code in (401, 403):
                        new_cid = await _get_client_id(force=True)
                        sr = await c.get(cu, params={"client_id": new_cid})
                        cur_cid = new_cid
                    if sr.status_code == 200:
                        return ((sr.json() or {}).get("url", ""), cand,
                                sr.status_code, sr.text[:160], cur_cid)
                    return ("", cand, sr.status_code, sr.text[:160], cur_cid)
                except Exception as _e:
                    return ("", cand, -1, str(_e)[:160], cur_cid)

            stream_url = ""
            chosen = None
            last_status = 0
            last_body   = ""
            results = await _aio.gather(*[_try_cand(cand, cid) for cand in candidates])
            # Preserve preference order: progressive first → HLS fallback.
            for r_url, r_cand, r_code, r_body, r_cid in results:
                cid = r_cid
                if r_url:
                    stream_url, chosen = r_url, r_cand
                    break
                last_status, last_body = r_code, r_body
                print(f"[soundcloud] candidate "
                      f"{(r_cand.get('format') or {}).get('protocol')} → "
                      f"{r_code}", flush=True)
            if not stream_url:
                # All plain transcodings failed — fall back to DRM stream if available.
                _has_enc2 = any(
                    "encrypted-hls" in ((t.get("format") or {}).get("protocol") or "")
                    for t in trans)
                if _has_enc2 and oauth:
                    _d_url, _d_lat, _d_proto = await _resolve_drm_hls(c, trans, cid, _is_full, prefer)
                    if _d_url:
                        return _drm_response(_d_url, _d_lat, _d_proto, track_json)
                if _has_enc2:
                    print(f"[soundcloud] {track_id} DRM-only, no OAuth set", flush=True)
                    raise HTTPException(451,
                        "Трек защищён DRM — добавь OAuth-токен в Settings → SoundCloud.")
                print(f"[soundcloud] all transcodings failed for {track_id} "
                      f"(last={last_status} body={last_body})", flush=True)
                raise HTTPException(502,
                    f"SoundCloud stream {last_status} — "
                    f"возможно нужен OAuth (Settings → SoundCloud)")
    except HTTPException:
        raise
    except Exception as e:
        print(f"[soundcloud] stream error {track_id}: {e!r}", flush=True)
        raise HTTPException(502, f"SoundCloud API error: {e}")

    if not stream_url:
        raise HTTPException(502, "SoundCloud не вернул URL потока")

    fmt = "mp3" if chosen is prog else "hls"
    mime = "audio/mpeg" if fmt == "mp3" else "application/vnd.apple.mpegurl"
    # Artwork for the player — returned so lazy-resolved queue items can show a cover.
    _art_raw = track_json.get("artwork_url") or (track_json.get("user") or {}).get("avatar_url", "")
    artwork = _artwork(_art_raw, "t500x500") if _art_raw else ""
    # For progressive MP3 — proxy through our backend so the browser sees a
    # same-origin response (unlocks Web Audio API gapless / equaliser / etc).
    # HLS is left as-is (HLS.js handles the manifest separately).
    # NOTE: guests are proxied too (NOT direct CDN). Direct cross-origin SC CDN
    # playback turned out unreliable for guests (Referer/HLS), so everyone goes
    # through /api/proxy. Large-file DELIVERY (not streaming) uses the Gofile
    # button, which already bypasses the tunnel.
    if fmt == "mp3":
        import base64 as _b64
        import urllib.parse as _up
        enc = _b64.urlsafe_b64encode(stream_url.encode()).decode().rstrip("=")
        proxy_url = (f"/api/proxy?u={enc}&svc=soundcloud&mime={_up.quote(mime)}"
                     f"&name={_up.quote(name)}&artist={_up.quote(artist)}")
        return {"url": proxy_url, "format": fmt, "mime": mime, "_cdn": stream_url, "artwork": artwork}
    return {"url": stream_url, "format": fmt, "mime": mime, "artwork": artwork}


_SC_SAFE_HOSTS = (
    ".sndcdn.com", ".sndcdn.cloud",
    ".media-streaming.soundcloud.cloud",
    ".soundcloud.com",
)


def _sc_host_ok(raw_url: str) -> bool:
    try:
        h = _urlparse.urlparse(raw_url).hostname or ""
    except Exception:
        return False
    return any(h == s.lstrip(".") or h.endswith(s) for s in _SC_SAFE_HOSTS)


@router.get("/api/sc_m3u8")
async def sc_m3u8_proxy(url: str, token: str = ""):
    """Fetch a SoundCloud CBC-encrypted HLS manifest and rewrite its #EXT-X-KEY
    URIs to go through /api/sc_key so the browser never has to cross-origin-fetch
    the decryption keys (SC CDN restricts CORS to soundcloud.com origin)."""
    if not _sc_host_ok(url):
        raise HTTPException(403, "Host not allowed")
    try:
        async with _HTTP.ashared() as c:
            r = await c.get(url, headers={
                "User-Agent": _UA,
                "Origin":     "https://soundcloud.com",
                "Referer":    "https://soundcloud.com/",
            })
        if r.status_code != 200:
            print(f"[soundcloud] m3u8 proxy → {r.status_code} for {url[:120]}", flush=True)
            raise HTTPException(r.status_code, f"SC m3u8 fetch: {r.status_code}")
    except HTTPException:
        raise
    except Exception as e:
        print(f"[soundcloud] m3u8 proxy error: {e}", flush=True)
        raise HTTPException(502, f"SC m3u8 proxy error: {e}")

    enc_tok = _urlparse.quote(token, safe="")

    rewritten_lines = []
    next_is_sub_playlist = False
    for line in r.text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#EXT-X-KEY"):
            # Rewrite only https:// key URIs — leave skd:// FairPlay URIs intact.
            line = re.sub(
                r'URI="(https?://[^"]+)"',
                lambda m: f'URI="/api/sc_key?token={enc_tok}&uri={_urlparse.quote(m.group(1), safe="")}"',
                line,
            )
        elif stripped.startswith("#EXT-X-STREAM-INF"):
            next_is_sub_playlist = True  # URL for this variant stream is on the next line
        elif next_is_sub_playlist and stripped and not stripped.startswith("#"):
            # Master playlist sub-playlist URL — proxy it so key rewrites apply.
            if stripped.startswith("http://") or stripped.startswith("https://"):
                line = f"/api/sc_m3u8?token={enc_tok}&url={_urlparse.quote(stripped, safe='')}"
            next_is_sub_playlist = False
        else:
            next_is_sub_playlist = False
        rewritten_lines.append(line)

    text = "\n".join(rewritten_lines)
    return Response(content=text.encode(),
                    media_type="application/vnd.apple.mpegurl",
                    headers={"Cache-Control": "no-cache"})


@router.get("/api/sc_key")
async def sc_aes_key_proxy(uri: str, token: str = ""):
    """Proxy an AES-128 decryption key request for SoundCloud CBC-encrypted HLS.
    HLS.js fetches #EXT-X-KEY URIs — SC's CDN blocks cross-origin key requests
    (CORS restricted to soundcloud.com). We proxy through here so the browser
    sees a same-origin response."""
    if not uri:
        raise HTTPException(400, "Missing uri")
    if not _sc_host_ok(uri):
        raise HTTPException(403, "Host not allowed")
    oauth = (_cfg.get("soundcloud-oauth-token") or "").strip()
    headers: dict = {"User-Agent": _UA, "Origin": "https://soundcloud.com",
                     "Referer": "https://soundcloud.com/"}
    if oauth:
        headers["Authorization"] = (oauth if oauth.lower().startswith("oauth ")
                                     else f"OAuth {oauth}")
    # Some SC key URIs already carry auth params; for those, token is redundant
    # but harmless. For others, pass it as a query param.
    params = {"license_token": token} if token else {}
    try:
        async with _HTTP.ashared() as c:
            r = await c.get(uri, params=params, headers=headers)
        if r.status_code != 200:
            print(f"[soundcloud] sc_key proxy → {r.status_code} for {uri[:80]}", flush=True)
            raise HTTPException(r.status_code, f"SC key server: {r.status_code}")
        return Response(content=r.content, media_type="application/octet-stream")
    except HTTPException:
        raise
    except Exception as e:
        print(f"[soundcloud] sc_key proxy error: {e}", flush=True)
        raise HTTPException(502, f"SC key proxy error: {e}")


@router.post("/api/sc_license")
async def sc_license_proxy(token: str, request: Request):
    """Proxy a Widevine license challenge to SC's license server.
    HLS.js sends the raw Widevine CDM challenge as the POST body; we forward it
    to SC's license server with the short-lived license_token and return the
    license bytes so the CDM can decrypt the stream."""
    if not token:
        raise HTTPException(400, "Missing token")
    challenge = await request.body()
    if not challenge:
        raise HTTPException(400, "Empty challenge body")
    sc_url = (f"https://license.media-streaming.soundcloud.cloud"
              f"/playback/widevine?license_token={token}")
    oauth = (_cfg.get("soundcloud-oauth-token") or "").strip()
    req_headers: dict = {
        "Content-Type": "application/octet-stream",
        "User-Agent":   _UA,
        "Origin":       "https://soundcloud.com",
        "Referer":      "https://soundcloud.com/",
    }
    if oauth:
        req_headers["Authorization"] = (oauth if oauth.lower().startswith("oauth ")
                                         else f"OAuth {oauth}")
    try:
        async with _HTTP.ashared() as c:
            r = await c.post(sc_url, content=challenge, headers=req_headers)
        if r.status_code != 200:
            print(f"[soundcloud] license proxy → {r.status_code}: {r.text[:160]}", flush=True)
            raise HTTPException(r.status_code,
                f"SC license server: {r.status_code} — токен мог истечь")
        return Response(content=r.content, media_type="application/octet-stream")
    except HTTPException:
        raise
    except Exception as e:
        print(f"[soundcloud] license proxy error: {e}", flush=True)
        raise HTTPException(502, f"License proxy error: {e}")


_SC_FPS_BASE = "https://license.media-streaming.soundcloud.cloud/playback/fairplay"
_sc_fps_cert_cache: bytes | None = None


@router.get("/api/sc_fps_cert")
async def sc_fps_cert():
    """Return SC's Apple FairPlay Streaming certificate (DER-encoded).
    Cached in memory after the first fetch."""
    global _sc_fps_cert_cache
    if _sc_fps_cert_cache:
        return Response(content=_sc_fps_cert_cache, media_type="application/octet-stream",
                        headers={"Cache-Control": "public, max-age=86400",
                                 "Access-Control-Allow-Origin": "*"})
    try:
        async with _HTTP.ashared() as c:
            r = await c.get(_SC_FPS_BASE)
        if r.status_code != 200:
            raise HTTPException(502, f"SC FPS cert: {r.status_code}")
        _sc_fps_cert_cache = r.content
        return Response(content=r.content, media_type="application/octet-stream",
                        headers={"Cache-Control": "public, max-age=86400",
                                 "Access-Control-Allow-Origin": "*"})
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"SC FPS cert error: {e}")


@router.post("/api/sc_fps_license")
async def sc_fps_license_proxy(token: str, request: Request):
    """Proxy a FairPlay SPC to SC's FPS license server and return the CKC.

    SC expects: POST ?license_token=... with body spc=<base64(SPC)>
    and Content-Type: application/x-www-form-urlencoded.
    Returns: base64-encoded CKC; we decode it before returning to HLS.js.
    """
    import base64 as _b64
    if not token:
        raise HTTPException(400, "Missing token")
    spc_bytes = await request.body()
    if not spc_bytes:
        raise HTTPException(400, "Empty SPC body")
    # SC FPS server expects SPC as base64 in a form-encoded body
    spc_b64 = _b64.b64encode(spc_bytes).decode()
    oauth = (_cfg.get("soundcloud-oauth-token") or "").strip()
    req_headers: dict = {
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent":   _UA,
        "Origin":       "https://soundcloud.com",
        "Referer":      "https://soundcloud.com/",
    }
    if oauth:
        req_headers["Authorization"] = (oauth if oauth.lower().startswith("oauth ")
                                         else f"OAuth {oauth}")
    try:
        async with _HTTP.ashared() as c:
            r = await c.post(
                f"{_SC_FPS_BASE}?license_token={token}",
                content=f"spc={_urlparse.quote(spc_b64)}".encode(),
                headers=req_headers,
            )
        if r.status_code != 200:
            print(f"[soundcloud] fps_license → {r.status_code}: {r.text[:300]!r} "
                  f"token_len={len(token)} oauth={'yes' if oauth else 'no'} "
                  f"spc_b64_len={len(spc_b64)}", flush=True)
            raise HTTPException(r.status_code,
                f"SC FPS license server: {r.status_code} — токен мог истечь")
        # Response may be base64-encoded CKC — decode if so
        ckc = r.content
        try:
            ckc = _b64.b64decode(ckc)
        except Exception:
            pass  # already binary
        print(f"[soundcloud] fps_license ok, ckc={len(ckc)}B", flush=True)
        return Response(content=ckc, media_type="application/octet-stream")
    except HTTPException:
        raise
    except Exception as e:
        print(f"[soundcloud] fps_license error: {e}", flush=True)
        raise HTTPException(502, f"FPS license proxy error: {e}")


@router.post("/api/sc_fps_log")
async def sc_fps_log(request: Request):
    """Receive FairPlay diagnostic messages from the browser and echo them to stdout."""
    try:
        body = await request.json()
        msg  = str(body.get("msg", ""))[:500]
    except Exception:
        msg = "(bad json)"
    print(f"[SC FPS CLIENT] {msg}", flush=True)
    return {"ok": True}


# ── Password sign-in ──────────────────────────────────────────────────────────
#
# SoundCloud's web sign-in goes through api-auth.soundcloud.com. The endpoint
# that still answers without a JS-bundled signature is /connect/session — it
# accepts a JSON body with credentials and returns {session:{access_token:...}}
# on success. Newer signed-flow accounts (or accounts that triggered CAPTCHA)
# will get a 4xx; we surface a clear message and the cookie fallback in the UI
# still works.

async def _sc_sign_in(email: str, password: str) -> dict:
    """Try every known SoundCloud password sign-in endpoint. SC has been
    chopping these APIs since 2023 — as of mid-2026 all three known routes
    (``connect/session``, ``web-auth/sign-in``, ``api-v2/sign-in``) return
    405 / 404 / require a JS-bundled signature. We still attempt them so a
    future revival isn't blocked; on failure surface a clear cookie-fallback
    message instead of a raw HTTP error."""
    body = {"credentials": {"identifier": email, "password": password}}
    headers = {
        "Content-Type": "application/json",
        "Accept":       "application/json",
        "User-Agent":   _UA,
        "Origin":       "https://soundcloud.com",
        "Referer":      "https://soundcloud.com/",
    }
    endpoints = [
        "https://api-auth.soundcloud.com/connect/session/password",
        "https://api-auth.soundcloud.com/web-auth/sign-in/password",
        "https://api-v2.soundcloud.com/sign-in/password",
    ]
    last = None
    for url in endpoints:
        try:
            async with _HTTP.ashared() as c:
                r = await c.post(url, json=body, headers=headers)
        except Exception as e:
            last = ("net", str(e)); continue
        if r.status_code == 200:
            try:
                data = r.json()
            except Exception:
                last = ("badjson", "")
                continue
            session = data.get("session") or {}
            token   = session.get("access_token") or session.get("oauth_token") or ""
            if token:
                return {"ok": True, "token": token}
            last = ("notoken", str(data)[:120])
            continue
        last = (str(r.status_code), (r.text or "")[:160])
    # All endpoints failed — SC removed the password API. Tell the user how to
    # recover via the cookie path.
    code, body_snip = last or ("?", "")
    return {
        "ok": False,
        "removed_api": True,
        "error": ("SoundCloud отключил вход по паролю (все 3 endpoint'а мертвы). "
                  "Скопируй oauth_token из браузера: F12 → Application → Cookies "
                  "→ soundcloud.com → oauth_token — и вставь в поле ниже.")
    }


_PROBE_PSSH = (   # fixed sample PSSH from a known-public SC track used to probe
    "AAAAa3Bzc2gAAAAA7e+LqXnWSs6jyCfc1R0h7QAAAEsSEC+VWx9p0VEjotc57sNgYAQa"
    "C2J1eWRybWtleW9zIiQ2ODFiZDk3Mi1iOTE1LTQwODktOTlhYi0yNTk4Y2UxNGJkOTFI49yVmwY="
)


@router.post("/api/wv-wrapper/key")
async def wv_wrapper_key(body: dict):
    """Public Widevine wrapper — accepts a PSSH + SC license_token from any
    Ripster peer, runs the handshake locally using OUR .wvd, returns the key.

    Lets one user with a working device act as a key-issuer for friends who
    don't have their own device. Same idea as wm.wol.moe for Apple FairPlay.

    Body: {pssh_b64, license_token, oauth?}
    Returns: {ok, kid_hex, key_hex} or {ok:false, error}
    """
    pssh_b64 = (body.get("pssh_b64") or "").strip()
    lic_tok  = (body.get("license_token") or "").strip()
    oauth    = (body.get("oauth") or _cfg.get("soundcloud-oauth-token") or "").strip()
    if not pssh_b64 or not lic_tok:
        raise HTTPException(400, "pssh_b64 and license_token required")
    # Resolve our local device
    from pathlib import Path as _P
    import sys as _sys
    base = _P(_sys.argv[0]).resolve().parent if _sys.argv else _P(".").resolve()
    p_cfg = (_cfg.get("sc-widevine-device") or "").strip()
    wvd_p = _P(p_cfg) if p_cfg and _P(p_cfg).is_file() else (
            base / "tools" / "widevine" / "device.wvd")
    if not wvd_p.is_file():
        return {"ok": False, "error": "no local device on this wrapper"}
    try:
        from pywidevine.cdm import Cdm
        from pywidevine.device import Device
        from pywidevine.pssh import PSSH
        device = Device.load(wvd_p)
        cdm = Cdm.from_device(device)
        sess = cdm.open()
        try:
            challenge = cdm.get_license_challenge(sess, PSSH(pssh_b64),
                                                   privacy_mode=False)
            hdr = {"Content-Type": "application/octet-stream",
                   "User-Agent": _UA,
                   "Origin":  "https://soundcloud.com",
                   "Referer": "https://soundcloud.com/"}
            if oauth:
                hdr["Authorization"] = (oauth if oauth.lower().startswith("oauth ")
                                        else f"OAuth {oauth}")
            async with _HTTP.ashared() as c:
                r = await c.post(
                    "https://license.media-streaming.soundcloud.cloud/playback/widevine",
                    params={"license_token": lic_tok},
                    content=challenge, headers=hdr)
            if r.status_code != 200:
                return {"ok": False,
                        "error": f"SC license server {r.status_code}: {r.text[:120]}"}
            cdm.parse_license(sess, r.content)
            keys = [k for k in cdm.get_keys(sess) if k.type == "CONTENT"]
            if not keys:
                return {"ok": False,
                        "error": "no CONTENT key (device may be revoked)"}
            k = keys[0]
            kid_hex = k.kid.hex if not callable(getattr(k.kid, "hex", None)) else k.kid.hex()
            return {"ok": True, "kid_hex": kid_hex, "key_hex": k.key.hex()}
        finally:
            try: cdm.close(sess)
            except Exception: pass
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@router.get("/api/wv-wrapper/probe")
async def wv_wrapper_probe(url: str = ""):
    """Health-check a Widevine wrapper. If ``url`` is empty, probe our own
    local wrapper (uses the bundled probe PSSH). Otherwise ping <url>/probe
    on the remote peer and return its status.

    Same shape as the Apple wrapper probe so the UI can render the same
    'green/red dot + region' indicator.
    """
    if url:
        try:
            async with _HTTP.ashared() as c:
                r = await c.get(url.rstrip("/") + "/api/wv-wrapper/probe")
            if r.status_code != 200:
                return {"ready": False, "status": False,
                        "error": f"HTTP {r.status_code}"}
            j = r.json()
            return {"ready": bool(j.get("ready")),
                    "status": True,
                    "instance": url,
                    "kid_hex": j.get("kid_hex", ""),
                    "client_count": j.get("client_count", 0)}
        except Exception as e:
            return {"ready": False, "status": False, "error": str(e)}

    # Local probe
    res = await wv_wrapper_key({
        "pssh_b64":      _PROBE_PSSH,
        "license_token": "PROBE_NO_TOKEN",   # will fail at SC server — that's OK
        "oauth":         _cfg.get("soundcloud-oauth-token", ""),
    })
    # The probe license request will 4xx (no valid token); what we're proving is
    # that the local CDM at least loads + builds a challenge.
    from pathlib import Path as _P
    import sys as _sys
    base = _P(_sys.argv[0]).resolve().parent if _sys.argv else _P(".").resolve()
    wvd_p = _P(_cfg.get("sc-widevine-device") or "") if _cfg.get("sc-widevine-device") \
            else (base / "tools" / "widevine" / "device.wvd")
    has_local = wvd_p.is_file()
    return {"ready":   has_local and "no CONTENT" not in str(res.get("error", "")),
            "status":  True,
            "instance": "local",
            "error":   res.get("error") if not res.get("ok") else "",
            "device_path": str(wvd_p) if has_local else "",
            "size":    wvd_p.stat().st_size if has_local else 0}


@router.post("/api/soundcloud/upload-wvd")
async def sc_upload_wvd(body: dict):
    """Upload a Widevine L3 device.wvd to enable DRM downloads.
    Body: {content_b64: '...'} — base64-encoded .wvd bytes.
    """
    import base64 as _b64
    b64 = (body.get("content_b64") or "").strip()
    if not b64:
        raise HTTPException(400, "content_b64 is required")
    try:
        raw = _b64.b64decode(b64)
    except Exception as e:
        raise HTTPException(400, f"invalid base64: {e}")
    if len(raw) < 100 or len(raw) > 100_000:
        raise HTTPException(400, f"file size {len(raw)}B out of expected range")
    # Sanity-check via pywidevine
    try:
        from pywidevine.device import Device
        from io import BytesIO
        # Device.load expects a path or bytes
        Device.loads(raw) if hasattr(Device, "loads") else Device.load(BytesIO(raw))
    except Exception as e:
        raise HTTPException(400, f"not a valid .wvd: {e}")
    from pathlib import Path as _P
    import sys as _sys
    base = _P(_sys.argv[0]).resolve().parent if _sys.argv else _P(".").resolve()
    wvd_dir = base / "tools" / "widevine"
    wvd_dir.mkdir(parents=True, exist_ok=True)
    wvd_path = wvd_dir / "device.wvd"
    wvd_path.write_bytes(raw)
    print(f"[soundcloud] wvd installed at {wvd_path} ({len(raw)}B)", flush=True)
    return {"ok": True, "path": str(wvd_path), "size": len(raw)}


@router.get("/api/soundcloud/wvd-status")
async def sc_wvd_status():
    from pathlib import Path as _P
    import sys as _sys
    base = _P(_sys.argv[0]).resolve().parent if _sys.argv else _P(".").resolve()
    p_cfg = (_cfg.get("sc-widevine-device") or "").strip()
    candidates = [_P(p_cfg)] if p_cfg else []
    candidates.append(base / "tools" / "widevine" / "device.wvd")
    for c in candidates:
        if c and c.is_file():
            try:
                from pywidevine.device import Device
                Device.load(c)
                return {"installed": True, "path": str(c), "size": c.stat().st_size,
                        "valid": True}
            except Exception as e:
                return {"installed": True, "path": str(c), "size": c.stat().st_size,
                        "valid": False, "error": str(e)}
    return {"installed": False}


@router.post("/api/soundcloud/login")
async def sc_login(body: dict):
    email = (body.get("email") or "").strip()
    pwd   = body.get("password") or ""
    if not email or not pwd:
        raise HTTPException(400, "email и password обязательны")
    res = await _sc_sign_in(email, pwd)
    if not res.get("ok"):
        # Pass full payload back so UI can show captcha hint
        return res
    _cfg["soundcloud-oauth-token"] = res["token"]
    if _save_cfg:
        try:
            _save_cfg(_cfg)
        except Exception as e:
            print(f"[soundcloud-login] save_config failed: {e}", flush=True)
    print(f"[soundcloud-login] ok — token saved ({len(res['token'])} chars)",
          flush=True)
    return {"ok": True, "token_length": len(res["token"])}
