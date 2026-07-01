"""
Streaming proxy and cover art routes.

  GET /api/stream/qobuz/{track_id}  — Qobuz pre-signed CDN URL
  GET /api/stream/tidal/{track_id}  — Tidal stream URL
  GET /api/stream/deezer/{track_id} — Deezer BF-decrypted streaming proxy
  GET /api/cover/best               — artwork URL resizer

Install: streaming.install(app, cfg)
"""
from __future__ import annotations

import re
import time
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

router = APIRouter()

_cfg: dict = {}
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# ── Deezer play-latency caches ─────────────────────────────────────────────
# Opening a Deezer track did 3 sequential Deezer API round-trips (getUserData →
# song.getListData → get_url) on EVERY request — and the browser <audio> fires a
# probe request plus a Range-seek request, so a single play paid ~6 round-trips
# before any audio. We cache the two reusable pieces with short TTLs and fall
# back to the full flow on any miss/expiry, so correctness is unchanged.
#   _DZ_AUTH: account-wide api_token + license_token (valid for the session).
#   _DZ_URL : per-(track_id, quality) resolved CDN URL + track dict — the signed
#             URL stays valid long enough to serve the immediate probe+range and
#             quick replays without re-resolving.
_DZ_AUTH: dict = {"ts": 0.0, "api_token": "", "license_token": ""}
_DZ_AUTH_TTL = 300.0      # 5 min
_DZ_URL: dict = {}        # (track_id, quality) -> {ts, cdn_url, track, fmt_str, q}
_DZ_URL_TTL = 120.0       # 2 min


def install(app, ctx) -> None:
    global _cfg
    _cfg = ctx.config
    app.include_router(router)


def _stream_origin(request: Request) -> tuple[str, str]:
    """Client IP for stream attribution (public build has no guest sessions)."""
    ip = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip() \
        or (request.client.host if request.client else "")
    return "", ip


def _display_name(artist: str, name: str, fallback: str) -> str:
    """Build a human-readable 'Artist — Title' for the stats log."""
    parts = [p.strip() for p in (artist, name) if p and p.strip()]
    return " — ".join(parts) if parts else fallback


def _record_listen(stream_type: str, request: Request, track_id: str,
                   name: str, artist: str, url: str) -> None:
    # Public build ships no analytics collector — listens are not recorded.
    return None


# ── Generic CDN proxy ─────────────────────────────────────────────────────
# Pipes audio bytes from any CDN through our backend with Range support.
# Used by Qobuz / Tidal / SoundCloud progressive streams so the browser sees
# a same-origin response — Web Audio API decodeAudioData then works, unlocking
# AbsoluteZero gapless playback (and equaliser / visualizer later).
async def proxy_cdn_stream(cdn_url: str, request: Request, mime: str,
                           filename: str) -> StreamingResponse:
    range_hdr = request.headers.get("range") or request.headers.get("Range") or ""
    # Probe Content-Length with a HEAD so we can advertise correct sizes.
    total_size = 0
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0),
                                     follow_redirects=True,
                                     headers={"User-Agent": _UA}) as probe:
            hr = await probe.head(cdn_url)
            if hr.status_code in (200, 206):
                cl = hr.headers.get("content-length", "")
                if cl.isdigit():
                    total_size = int(cl)
    except Exception:
        pass

    fwd_headers = {"User-Agent": _UA}
    if range_hdr:
        fwd_headers["Range"] = range_hdr

    async def _pipe():
        try:
            async with httpx.AsyncClient(
                headers=fwd_headers,
                timeout=httpx.Timeout(300.0, connect=10.0),
                follow_redirects=True,
            ) as c:
                async with c.stream("GET", cdn_url) as resp:
                    if resp.status_code not in (200, 206):
                        print(f"[proxy] CDN HTTP {resp.status_code} for {cdn_url[:80]}",
                              flush=True)
                        return
                    async for chunk in resp.aiter_bytes(64 * 1024):
                        yield chunk
        except Exception as e:
            print(f"[proxy] stream error: {e}", flush=True)

    out_headers = {
        "Accept-Ranges":       "bytes",
        "Cache-Control":       "no-cache",
        "Content-Disposition": f'inline; filename="{filename}"',
        # CORS for self-hosted multi-host deploys.
        "Access-Control-Allow-Origin":  "*",
        "Access-Control-Allow-Headers": "Range",
        "Access-Control-Expose-Headers":"Accept-Ranges, Content-Length, Content-Range",
    }
    status = 200
    if range_hdr and total_size:
        m = re.match(r"bytes=(\d+)-(\d*)", range_hdr)
        if m:
            rs = int(m.group(1))
            re_ = int(m.group(2)) if m.group(2) else total_size - 1
            out_headers["Content-Range"] = f"bytes {rs}-{re_}/{total_size}"
            out_headers["Content-Length"] = str(re_ - rs + 1)
            status = 206
    elif total_size:
        out_headers["Content-Length"] = str(total_size)

    return StreamingResponse(_pipe(), status_code=status, media_type=mime,
                             headers=out_headers)


# ── Stream proxy entrypoint ──────────────────────────────────────────────
# Frontend hits /api/proxy?u=<base64url>&svc=<service>&mime=<mime>
# Service backends (Qobuz/Tidal/SC) build this URL and return it instead of
# the raw CDN URL, so the audio element + Web Audio see a same-origin source.
@router.get("/api/proxy")
async def cdn_proxy(request: Request, u: str = "", svc: str = "any",
                    mime: str = "audio/mpeg", name: str = "", artist: str = ""):
    if not u:
        raise HTTPException(400, "u (CDN URL) required")
    import base64 as _b64
    try:
        cdn_url = _b64.urlsafe_b64decode(u + "==").decode("utf-8")
    except Exception:
        # Fallback — accept plain URL too (URL-encoded by caller)
        cdn_url = u
    if not cdn_url.startswith(("http://", "https://")):
        raise HTTPException(400, "invalid URL")
    # Safety: only allow specific CDN domains — parsed hostname, not substring.
    # Substring check is bypassable via https://evil.com/sndcdn.com/payload.
    from urllib.parse import urlparse as _urlparse
    try:
        _host = _urlparse(cdn_url).hostname or ""
    except Exception:
        _host = ""
    _safe_suffixes = (
        ".sndcdn.com", ".sndcdn.cloud",
        ".dzcdn.net",
        ".qobuz.com",
        ".akamaized.net",   # Qobuz Akamai edge nodes (streaming-qobuz-std.akamaized.net)
        ".tidal.com",
        ".itunes.apple.com",  # Apple Music 30-sec previews (audio-ssl.itunes.apple.com)
        ".mzstatic.com",      # Apple preview/asset CDN
    )
    _safe_exact = {"sndcdn.com", "sndcdn.cloud", "dzcdn.net", "qobuz.com", "tidal.com"}
    _host_ok = _host in _safe_exact or any(_host.endswith(s) for s in _safe_suffixes)
    if not _host_ok:
        raise HTTPException(403, f"host not allowed: {_host or cdn_url[:40]}")
    track_id = ""
    if "/tracks/" in cdn_url:
        try: track_id = cdn_url.rsplit("/tracks/", 1)[1].split("/", 1)[0]
        except Exception: pass
    _record_listen(svc, request, track_id, name, artist, cdn_url)
    ext = "flac" if "flac" in mime else ("m4a" if "mp4" in mime else "mp3")
    return await proxy_cdn_stream(cdn_url, request, mime, f"{svc}_{track_id or 'track'}.{ext}")


# ── Qobuz ─────────────────────────────────────────────────────────────────────

@router.get("/api/stream/qobuz/{track_id}")
async def stream_qobuz(track_id: str, request: Request, format_id: int = 27,
                       name: str = "", artist: str = ""):
    """Return a Qobuz pre-signed CDN URL for the given track_id.

    format_id: 5=MP3·320, 6=FLAC·16/44, 7=HiRes·24/96, 27=HiRes·24/192
    """
    import hashlib as _hashlib

    app_id = (_cfg.get("qobuz-app-id") or "").strip() or "312369995"
    token  = (_cfg.get("qobuz-auth-token") or "").strip()
    secret = (_cfg.get("qobuz-secrets") or _cfg.get("qobuz-secret") or "").strip()

    if not token:
        raise HTTPException(400, "Qobuz auth-token не настроен (Settings → Qobuz)")
    if not secret:
        raise HTTPException(400, "Qobuz secret не настроен (Settings → Qobuz)")

    ts  = str(int(time.time()))
    sig = _hashlib.md5(
        f"trackgetFileUrl"
        f"format_id{format_id}"
        f"intentstream"
        f"track_id{track_id}"
        f"{ts}{secret}"
        .encode()
    ).hexdigest()

    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(
                "https://www.qobuz.com/api.json/0.2/track/getFileUrl",
                params={
                    "track_id":    track_id,
                    "format_id":   format_id,
                    "intent":      "stream",
                    "request_ts":  ts,
                    "request_sig": sig,
                    "app_id":      app_id,
                },
                headers={"X-User-Auth-Token": token},
            )
            data = r.json()
    except Exception as e:
        print(f"[qobuz] stream API error for track={track_id}: {e}", flush=True)
        raise HTTPException(502, f"Qobuz API error: {e}")

    url = data.get("url")
    if not url:
        msg = data.get("message") or data.get("status") or "No URL"
        print(f"[qobuz] stream rejected for track={track_id}: {msg}", flush=True)
        raise HTTPException(400, f"Qobuz stream error: {msg}")

    mime = data.get("mime_type") or ("audio/flac" if format_id >= 6 else "audio/mpeg")
    fmt  = "flac" if format_id >= 6 else "mp3"
    _record_listen("qobuz", request, track_id, name, artist, url)
    _meta = {"duration": data.get("duration"),
             "sampling_rate": data.get("sampling_rate"),
             "bit_depth": data.get("bit_depth")}
    # Everyone (incl. guests) goes through /api/proxy — direct CDN playback was
    # unreliable for guests. Wrap the CDN URL so the browser sees a same-origin
    # response — also unlocks Web Audio decodeAudioData (gapless / equaliser etc).
    import base64 as _b64
    import urllib.parse as _up
    enc = _b64.urlsafe_b64encode(url.encode()).decode().rstrip("=")
    proxy_url = (f"/api/proxy?u={enc}&svc=qobuz&mime={_up.quote(mime)}"
                 f"&name={_up.quote(name)}&artist={_up.quote(artist)}")
    return {"url": proxy_url, "format": fmt, "mime": mime,
            "_cdn": url, **_meta}     # original for debug only


# ── Tidal ─────────────────────────────────────────────────────────────────────

async def _tidal_stream_token(track_id: str) -> tuple[str, str]:
    """(access_token, countryCode) — prefer the AUTO-REFRESHING OrpheusDL TV token
    (minted from a long-lived refresh_token, cached ~4h), fall back to the pasted
    `tidal-token`. The pasted token dies in ~16h; the refresh path never needs a
    manual re-login."""
    try:
        from ripster.engines.tidal import _tidal_token_country
        token, country = await _tidal_token_country(_cfg)
    except Exception:
        token, country = "", ""
    if not token:
        token   = (_cfg.get("tidal-token") or "").strip()
        country = (_cfg.get("tidal-country") or "US").strip().upper()
    return token, (country or "US").upper()


# Tidal's audioquality codes for playbackinfopostpaywall (map our ids → theirs).
_TIDAL_PBQ = {"low": "LOW", "high": "HIGH", "mp3": "HIGH", "aac": "HIGH",
              "lossless": "LOSSLESS", "flac": "LOSSLESS",
              "hires": "HI_RES_LOSSLESS", "hi_res": "HI_RES_LOSSLESS",
              "master": "HI_RES_LOSSLESS", "hifi": "HI_RES_LOSSLESS"}


async def _tidal_playbackinfo(track_id: str, quality: str, token: str, country: str):
    """GET the current playbackinfopostpaywall (the v1 streamUrl endpoint was
    RETIRED by Tidal — it now 401s 'Asset is not ready' for EVERY token, which
    read as 'token expired' even though the token was fresh). Returns the httpx
    response."""
    aq = _TIDAL_PBQ.get((quality or "").lower(), "LOSSLESS")
    async with httpx.AsyncClient(timeout=12) as c:
        return await c.get(
            f"https://api.tidal.com/v1/tracks/{track_id}/playbackinfopostpaywall",
            params={"audioquality": aq, "playbackmode": "STREAM",
                    "assetpresentation": "FULL", "countryCode": country},
            headers={"Authorization": f"Bearer {token}"},
        )


def _tidal_parse_dash(xml: str) -> tuple[str, list[str]] | None:
    """Parse a Tidal DASH manifest → (init_url, [segment_urls]). Tidal ships a
    single Representation with a SegmentTemplate (init + $Number$ media) and a
    SegmentTimeline of <S d=.. r=..> runs; total segments = Σ(r+1)."""
    init  = re.search(r'initialization="([^"]+)"', xml)
    media = re.search(r'media="([^"]+)"', xml)
    if not (init and media):
        return None
    m = re.search(r'startNumber="(\d+)"', xml)
    start_n = int(m.group(1)) if m else 1
    total = 0
    for stag in re.finditer(r'<S\b([^>]*?)/?>', xml):
        attrs = stag.group(1)
        if 'd="' not in attrs:
            continue
        rep = re.search(r'r="(\d+)"', attrs)
        total += (int(rep.group(1)) + 1) if rep else 1
    if total <= 0:
        return None
    tmpl = media.group(1)
    segs = [tmpl.replace("$Number$", str(start_n + i)) for i in range(total)]
    return init.group(1), segs


@router.get("/api/stream/tidal/{track_id}")
async def stream_tidal(track_id: str, request: Request, quality: str = "LOSSLESS",
                       name: str = "", artist: str = ""):
    """Resolve a Tidal track to a playable Ripster URL.

    Tidal retired the old ``/v1/tracks/{id}/streamUrl`` endpoint (it 401s for any
    token now). We use ``playbackinfopostpaywall`` instead — it returns a base64
    DASH manifest (segmented fMP4/FLAC) or, for some tiers, a BTS manifest with a
    direct URL. DASH is streamed through our segment-concat proxy so the browser
    <audio> plays it as one progressive fMP4."""
    import base64 as _b64
    import json as _json
    import urllib.parse as _up

    token, country = await _tidal_stream_token(track_id)
    if not token:
        raise HTTPException(400, "Tidal token не настроен (Settings → Tidal)")
    try:
        r = await _tidal_playbackinfo(track_id, quality, token, country)
    except Exception as e:
        print(f"[tidal] playbackinfo error for track={track_id}: {e}", flush=True)
        raise HTTPException(502, f"Tidal API error: {e}")
    if r.status_code == 401:
        print(f"[tidal] playbackinfo 401 for track={track_id} (token rejected)", flush=True)
        raise HTTPException(401, "Tidal: сессия отклонена (401). Переустанови вход Tidal в Настройки → Tidal.")
    if r.status_code in (404, 400):
        print(f"[tidal] playbackinfo {r.status_code} for track={track_id}: {r.text[:120]}", flush=True)
        raise HTTPException(404, "Tidal: трек недоступен в твоём регионе или снят.")
    if r.status_code != 200:
        print(f"[tidal] playbackinfo {r.status_code} for track={track_id}: {r.text[:120]}", flush=True)
        raise HTTPException(502, f"Tidal API {r.status_code}")

    data = r.json()
    mime_type = (data.get("manifestMimeType") or "").lower()
    manifest  = data.get("manifest") or ""
    try:
        decoded = _b64.b64decode(manifest).decode("utf-8", errors="replace")
    except Exception:
        decoded = ""

    # BTS manifest → direct CDN URL(s): stream through the generic /api/proxy.
    if "bts" in mime_type:
        try:
            urls = _json.loads(decoded).get("urls") or []
        except Exception:
            urls = []
        if urls:
            url = urls[0]
            _record_listen("tidal", request, track_id, name, artist, url)
            enc = _b64.urlsafe_b64encode(url.encode()).decode().rstrip("=")
            return {"url": f"/api/proxy?u={enc}&svc=tidal&mime=audio/mp4"
                           f"&name={_up.quote(name)}&artist={_up.quote(artist)}",
                    "format": "mp4", "mime": "audio/mp4", "quality": quality}

    # DASH manifest → segment-concat proxy (the common FLAC/lossless case).
    if _tidal_parse_dash(decoded):
        _record_listen("tidal", request, track_id, name, artist, "")
        return {"url": f"/api/stream/tidal-dash/{track_id}"
                       f"?quality={_up.quote(quality)}&name={_up.quote(name)}&artist={_up.quote(artist)}",
                "format": "flac", "mime": "audio/mp4", "quality": quality}

    print(f"[tidal] unsupported manifest ({mime_type}) for track={track_id}", flush=True)
    raise HTTPException(502, "Tidal: неизвестный формат манифеста.")


@router.get("/api/stream/tidal-dash/{track_id}")
async def stream_tidal_dash(track_id: str, request: Request, quality: str = "LOSSLESS",
                            name: str = "", artist: str = ""):
    """Stream a Tidal DASH track as ONE progressive fMP4: fetch the init segment
    then each media fragment in order and concatenate. Chromium/WebView2 play the
    concatenated fMP4 (FLAC-in-MP4) via a plain <audio> element."""
    token, country = await _tidal_stream_token(track_id)
    if not token:
        raise HTTPException(400, "Tidal token не настроен (Settings → Tidal)")
    r = await _tidal_playbackinfo(track_id, quality, token, country)
    if r.status_code != 200:
        raise HTTPException(r.status_code if r.status_code in (401, 404) else 502,
                            "Tidal: не удалось получить манифест.")
    import base64 as _b64
    try:
        decoded = _b64.b64decode(r.json().get("manifest") or "").decode("utf-8", errors="replace")
    except Exception:
        decoded = ""
    parsed = _tidal_parse_dash(decoded)
    if not parsed:
        raise HTTPException(502, "Tidal: манифест не разобран.")
    init_url, seg_urls = parsed

    urls = [init_url] + seg_urls

    async def _gen():
        # Pipelined fetch: keep WINDOW segment requests in flight and yield them
        # IN ORDER. Sequential one-at-a-time made the browser wait ~1–2 s to buffer
        # enough to start ("Tidal opens slowly"); a small look-ahead window fills
        # the buffer several× faster while preserving fMP4 fragment order.
        import asyncio as _aio
        WINDOW = 5
        async with httpx.AsyncClient(timeout=None, headers={"User-Agent": _UA}) as c:
            async def _fetch(i):
                return await c.get(urls[i])
            inflight: dict = {}
            nxt = 0
            for i in range(min(WINDOW, len(urls))):
                inflight[i] = _aio.create_task(_fetch(i)); nxt = i + 1
            for i in range(len(urls)):
                try:
                    resp = await inflight.pop(i)
                except Exception as e:
                    print(f"[tidal] dash segment error track={track_id}: {e}", flush=True)
                    return
                if resp.status_code != 200:
                    print(f"[tidal] dash segment {resp.status_code} track={track_id}", flush=True)
                    return
                if nxt < len(urls):
                    inflight[nxt] = _aio.create_task(_fetch(nxt)); nxt += 1
                yield resp.content

    return StreamingResponse(_gen(), media_type="audio/mp4",
                             headers={"Cache-Control": "no-store",
                                      "Accept-Ranges": "none"})


# ── Deezer ────────────────────────────────────────────────────────────────────

@router.get("/api/stream/deezer/{track_id}")
async def stream_deezer(track_id: str, request: Request, quality: int = 3,
                        name: str = "", artist: str = ""):
    """Server-side Deezer streaming proxy with on-the-fly Blowfish decryption.

    Uses the modern media.deezer.com/v1/get_url API: the legacy
    ``e-cdns-proxy-*.dzcdn.net`` subdomain pattern was deprecated by Deezer in
    2024 and no longer resolves on public DNS. The new flow gets a signed CDN
    URL via license_token + TRACK_TOKEN.

    quality: 1=MP3·128  3=MP3·320  9=FLAC
    """
    print(f"[deezer] REQUEST track={track_id} q={quality} "
          f"range='{request.headers.get('range','')}'", flush=True)
    try:
        from Crypto.Cipher import Blowfish as _BF
    except ImportError:
        try:
            from Cryptodome.Cipher import Blowfish as _BF
        except ImportError:
            raise HTTPException(500,
                "pycryptodome not installed — run: pip install pycryptodome")

    import hashlib as _hl

    arl = (_cfg.get("deezer-arl") or "").strip()
    if not arl:
        raise HTTPException(400, "Deezer ARL не настроен (Settings → Deezer)")

    _GW  = "https://www.deezer.com/ajax/gw-light.php"
    _MED = "https://media.deezer.com/v1/get_url"
    _UA  = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
    _JAR = {"arl": arl}

    # Map our integer quality to the new API's format string + filesize key.
    _FMT_MAP = {1: ("MP3_128", "FILESIZE_MP3_128"),
                3: ("MP3_320", "FILESIZE_MP3_320"),
                9: ("FLAC",    "FILESIZE_FLAC")}

    # Fast path: a freshly resolved CDN URL for this (track, quality) serves the
    # browser's probe + Range-seek (and quick replays) with zero round-trips.
    _now = time.time()
    _ukey = (track_id, quality)
    _cached = _DZ_URL.get(_ukey)
    if _cached and (_now - _cached["ts"]) < _DZ_URL_TTL:
        cdn_url = _cached["cdn_url"]
        track   = _cached["track"]
        fmt_str = _cached["fmt_str"]
        q       = _cached["q"]
    else:
        try:
            async with httpx.AsyncClient(cookies=_JAR, headers={"User-Agent": _UA},
                                         timeout=15) as c:
                # 1. Auth: reuse cached api_token + license_token when fresh.
                if (_now - _DZ_AUTH["ts"]) < _DZ_AUTH_TTL and _DZ_AUTH["api_token"]:
                    api_token     = _DZ_AUTH["api_token"]
                    license_token = _DZ_AUTH["license_token"]
                else:
                    r = await c.post(_GW, params={
                        "method": "deezer.getUserData", "api_version": "1.0",
                        "api_token": "null", "input": "3",
                    })
                    ud            = (r.json() or {}).get("results", {}) or {}
                    api_token     = ud.get("checkForm", "")
                    license_token = ((ud.get("USER") or {}).get("OPTIONS") or {}).get("license_token", "")
                    if not api_token:
                        raise HTTPException(400,
                            "Deezer: не удалось получить api_token. ARL возможно истёк — "
                            "обнови в Settings → Deezer.")
                    if not license_token:
                        raise HTTPException(403,
                            "Deezer: нет license_token (нужна Premium-подписка для стриминга).")
                    _DZ_AUTH.update(ts=_now, api_token=api_token,
                                    license_token=license_token)

                # 2. Track data: TRACK_TOKEN + filesize flags. getListData can come
                #    back EMPTY under a transient rate-limit (burst of plays) even
                #    for a perfectly available track → the old code mis-reported
                #    that as 404/region. Re-auth with fresh tokens + refetch once.
                async def _get_song():
                    rr = await c.post(_GW, params={
                        "method": "song.getListData", "api_version": "1.0",
                        "api_token": api_token, "input": "3",
                    }, json={"sng_ids": [int(track_id)]})
                    d = (rr.json() or {}).get("results", {}).get("data", [])
                    return d[0] if d else None

                track = await _get_song()
                if not track or not track.get("TRACK_TOKEN"):
                    print(f"[deezer] getListData empty track={track_id} "
                          f"(rate-limit?) — re-auth+retry", flush=True)
                    _DZ_AUTH["ts"] = 0.0
                    rr = await c.post(_GW, params={
                        "method": "deezer.getUserData", "api_version": "1.0",
                        "api_token": "null", "input": "3"})
                    ud2 = (rr.json() or {}).get("results", {}) or {}
                    api_token = ud2.get("checkForm", "") or api_token
                    license_token = (((ud2.get("USER") or {}).get("OPTIONS") or {})
                                     .get("license_token", "")) or license_token
                    if api_token and license_token:
                        _DZ_AUTH.update(ts=_now, api_token=api_token,
                                        license_token=license_token)
                    track = await _get_song()
                if not track:
                    raise HTTPException(404, f"Deezer: трек {track_id} не найден")
                track_token = track.get("TRACK_TOKEN", "")
                if not track_token:
                    raise HTTPException(503,
                        "Deezer: TRACK_TOKEN пуст — повтори (возможно рейт-лимит)")

                # Pick best available format ≤ requested quality.
                q = quality
                for candidate in [quality, 3, 1]:
                    _, fk = _FMT_MAP.get(candidate, _FMT_MAP[1])
                    if str(track.get(fk, "0") or "0") not in ("0", ""):
                        q = candidate
                        break
                fmt_str, _ = _FMT_MAP[q]

                # 3. Resolve signed CDN URL. get_url is the step Deezer rate-limits
                #    under a burst (e.g. fast track-skipping) → it can briefly return
                #    non-200 or empty sources even though the track is perfectly
                #    available. On any such failure, re-authenticate with FRESH
                #    tokens and retry once IN THIS REQUEST, so a transient hiccup
                #    never surfaces as a bogus "track unavailable" to the user.
                async def _resolve_cdn(lic_tok, trk_tok):
                    payload = {
                        "license_token": lic_tok,
                        "media": [{"type": "FULL",
                                   "formats": [{"cipher": "BF_CBC_STRIPE", "format": fmt_str}]}],
                        "track_tokens": [trk_tok],
                    }
                    mr = await c.post(_MED, json=payload)
                    if mr.status_code != 200:
                        return None, f"HTTP {mr.status_code}"
                    d0 = ((mr.json() or {}).get("data") or [{}])[0]
                    if d0.get("errors"):
                        return None, "; ".join(f"{e.get('code')}: {e.get('message','?')}"
                                               for e in d0["errors"])
                    s = ((d0.get("media") or [{}])[0].get("sources") or [])
                    if not s or not s[0].get("url"):
                        return None, "no sources"
                    return s[0]["url"], ""

                cdn_url, why = await _resolve_cdn(license_token, track_token)
                if not cdn_url:
                    print(f"[deezer] get_url failed track={track_id}: {why} — re-auth+retry",
                          flush=True)
                    # Force-fresh auth + track token, then retry once.
                    _DZ_AUTH["ts"] = 0.0
                    rr = await c.post(_GW, params={
                        "method": "deezer.getUserData", "api_version": "1.0",
                        "api_token": "null", "input": "3"})
                    ud2 = (rr.json() or {}).get("results", {}) or {}
                    api_token = ud2.get("checkForm", "") or api_token
                    license_token = (((ud2.get("USER") or {}).get("OPTIONS") or {})
                                     .get("license_token", "")) or license_token
                    if api_token and license_token:
                        _DZ_AUTH.update(ts=_now, api_token=api_token, license_token=license_token)
                    r2b = await c.post(_GW, params={
                        "method": "song.getListData", "api_version": "1.0",
                        "api_token": api_token, "input": "3"}, json={"sng_ids": [int(track_id)]})
                    sd = (r2b.json() or {}).get("results", {}).get("data", [])
                    if sd and sd[0].get("TRACK_TOKEN"):
                        track_token = sd[0]["TRACK_TOKEN"]
                    cdn_url, why = await _resolve_cdn(license_token, track_token)
                    if not cdn_url:
                        print(f"[deezer] get_url retry FAILED track={track_id}: {why}", flush=True)
                        raise HTTPException(503,
                            f"Deezer get_url: {why} (рейт-лимит/токен) — попробуй ещё раз")
                # Drop expired entries so the cache stays small (bounded by the
                # number of distinct tracks played within the 2-min window).
                if len(_DZ_URL) > 256:
                    for k in [k for k, v in _DZ_URL.items()
                              if (_now - v["ts"]) >= _DZ_URL_TTL]:
                        _DZ_URL.pop(k, None)
                _DZ_URL[_ukey] = {"ts": _now, "cdn_url": cdn_url, "track": track,
                                  "fmt_str": fmt_str, "q": q}
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(502, f"Deezer API error: {e}")

    print(f"[deezer] track={track_id} fmt={fmt_str} "
          f"url={cdn_url[:90]}…", flush=True)

    id_md5 = _hl.md5(str(track_id).encode()).hexdigest().encode()
    bf_key = bytes(id_md5[i] ^ id_md5[i + 16] ^ b"g4el58wc0zvf9na1"[i]
                   for i in range(16))

    mime = "audio/flac" if q == 9 else "audio/mpeg"
    ext  = "flac"       if q == 9 else "mp3"

    # ── Range support ─────────────────────────────────────────────────────
    # The browser <audio> element needs Accept-Ranges + 206 partial responses
    # to seek. Without it, audio.currentTime = N just bounces back to 0.
    # Deezer encrypts every 3rd 2048-byte block (BF_CBC_STRIPE), so we
    # serve range starts aligned to 2048 boundaries and trim the leading bytes.
    _BLOCK = 2048
    _FSIZE_KEY = {1: "FILESIZE_MP3_128", 3: "FILESIZE_MP3_320", 9: "FILESIZE_FLAC"}
    try:
        total_size = int(track.get(_FSIZE_KEY.get(q, "FILESIZE_MP3_128"), 0) or 0)
    except (TypeError, ValueError):
        total_size = 0

    # FILESIZE is sometimes 0 in the track metadata (region/edge cases). Without a
    # known size we can't advertise Accept-Ranges, so the browser <audio> shows no
    # duration and seeking is dead. Probe the signed CDN URL for the real
    # Content-Length (cached per resolved URL so a seek/replay doesn't re-probe).
    _centry = _DZ_URL.get(_ukey)
    if not total_size and _centry and _centry.get("size"):
        total_size = _centry["size"]
    if not total_size:
        try:
            async with httpx.AsyncClient(timeout=10, follow_redirects=True) as _hc:
                _pr = await _hc.head(cdn_url, headers={"User-Agent": _UA})
                total_size = int(_pr.headers.get("content-length", 0) or 0)
                if not total_size:
                    _pr2 = await _hc.get(cdn_url,
                                         headers={"User-Agent": _UA, "Range": "bytes=0-0"})
                    _m = re.search(r"/(\d+)\s*$", _pr2.headers.get("content-range", ""))
                    if _m:
                        total_size = int(_m.group(1))
        except Exception as _e:
            print(f"[deezer] size probe failed for {track_id}: {_e}", flush=True)
            total_size = 0
        if total_size and _centry is not None:
            _centry["size"] = total_size

    range_hdr   = request.headers.get("range") or request.headers.get("Range") or ""
    has_range   = False
    range_start = 0
    range_end   = total_size - 1 if total_size else None
    rm = re.match(r"bytes=(\d+)-(\d*)", range_hdr)
    if rm and total_size:
        has_range = True
        range_start = int(rm.group(1))
        if rm.group(2):
            range_end = min(int(rm.group(2)), total_size - 1)
        else:
            range_end = total_size - 1
        if range_start > range_end:
            raise HTTPException(416, "Bad range")

    aligned_start   = (range_start // _BLOCK) * _BLOCK
    skip_first      = range_start - aligned_start
    start_chunk_abs = aligned_start // _BLOCK
    content_length  = (range_end - range_start + 1) if total_size else None

    async def _generate():
        try:
            req_headers = {"User-Agent": _UA}
            if total_size:
                # Always ask CDN for our aligned slice — keeps memory bounded
                req_headers["Range"] = f"bytes={aligned_start}-{range_end}"
            async with httpx.AsyncClient(
                headers=req_headers,
                timeout=httpx.Timeout(300.0, connect=10.0),
                follow_redirects=True,
            ) as c2:
                async with c2.stream("GET", cdn_url) as resp:
                    if resp.status_code not in (200, 206):
                        print(f"[deezer] CDN HTTP {resp.status_code} for track {track_id}",
                              flush=True)
                        return
                    chunk_idx_local = 0
                    bytes_yielded = 0
                    buf = b""
                    async for raw in resp.aiter_bytes(_BLOCK):
                        buf += raw
                        while len(buf) >= _BLOCK:
                            block = buf[:_BLOCK]
                            buf   = buf[_BLOCK:]
                            abs_idx = start_chunk_abs + chunk_idx_local
                            if abs_idx % 3 == 0:
                                cipher = _BF.new(bf_key, _BF.MODE_CBC,
                                                 b"\x00\x01\x02\x03\x04\x05\x06\x07")
                                block = cipher.decrypt(block)
                            # First chunk: trim leading bytes to land on range_start
                            if chunk_idx_local == 0 and skip_first:
                                block = block[skip_first:]
                            chunk_idx_local += 1
                            if content_length is not None:
                                remaining = content_length - bytes_yielded
                                if len(block) > remaining:
                                    block = block[:remaining]
                                bytes_yielded += len(block)
                                yield block
                                if bytes_yielded >= content_length:
                                    return
                            else:
                                yield block
                    if buf:
                        # trailing partial — defensive
                        if content_length is not None:
                            remaining = content_length - bytes_yielded
                            if remaining > 0:
                                yield buf[:remaining]
                        else:
                            yield buf
        except Exception as e:
            print(f"[deezer] stream error: {e}", flush=True)

    _record_listen("deezer", request, track_id, name, artist, cdn_url)
    print(f"[deezer] serve track={track_id} range='{range_hdr}' total_size={total_size} "
          f"has_range={has_range} clen={content_length} status={206 if has_range else 200}",
          flush=True)
    headers = {
        "Accept-Ranges":       "bytes" if total_size else "none",
        "Cache-Control":       "no-cache",
        "Content-Disposition": f'inline; filename="deezer_{track_id}.{ext}"',
    }
    if content_length is not None:
        headers["Content-Length"] = str(content_length)
    if has_range:
        headers["Content-Range"] = f"bytes {range_start}-{range_end}/{total_size}"
    return StreamingResponse(
        _generate(),
        status_code=(206 if has_range else 200),
        media_type=mime,
        headers=headers,
    )


# ── Cover art ─────────────────────────────────────────────────────────────────

@router.get("/api/cover/best")
async def cover_best(url: str, target: int = 3000):
    if "mzstatic.com" in url or "itunes.apple.com" in url:
        m = re.search(r"/(\d+)x(\d+)bb", url)
        src    = int(m.group(1)) if m else 9999
        actual = min(target, src)
        new_url = re.sub(r"/(\d+)x(\d+)bb", f"/{actual}x{actual}bb", url)
        return {"url": new_url, "size": actual, "upscaled": False}
    if "dzcdn.net" in url:
        new_url = re.sub(r"/(\d+)x(\d+)", f"/{target}x{target}", url)
        return {"url": new_url, "size": target}
    return {"url": url, "size": None}
