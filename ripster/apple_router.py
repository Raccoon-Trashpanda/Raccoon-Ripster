"""Smart Apple download router.

Apple content needs a different decryption path per requested quality, and each
path depends on a resource that may or may not be available right now:

  - Video (mv)      : gamdl + cookies + gamdl's bundled Widevine CDM. No wrapper.
  - AAC (lossy)     : gamdl + cookies. No wrapper.
  - ALAC/Atmos/AC3  : a *wrapper* is mandatory (lossless/spatial keys). Either
        • AMD     → public wrapper-manager (``amd-instance-url``, e.g. wm.wol.moe);
                    needs NO Docker and NO Apple ID, OR
        • zhaarey → local Docker wrapper (``decrypt-port``, default 10020).

``route_apple()`` probes what is actually reachable (public wrapper HTTP, local
wrapper TCP port, cookies file) and returns the best engine+quality to satisfy
the request — degrading to the best lossy result (AAC via cookies) only when no
wrapper at all is available, so every task yields the maximum possible output.
"""
from __future__ import annotations

import re
import socket
import time
from pathlib import Path

import httpx

# music.apple.com/<storefront>/album/... → the 2-letter region segment.
_RE_STOREFRONT = re.compile(r"music\.apple\.com/([a-z]{2})/", re.I)


def url_storefront(url: str) -> str:
    m = _RE_STOREFRONT.search(url or "")
    return m.group(1).lower() if m else ""


# ── Availability-aware region resolution (pre-release handling) ───────────────
# A release can be live in one storefront before another (e.g. out in /nz/ days
# before our /gb/ account). iTunes flags this per region via `isStreamable`. If
# the link's region can't stream it yet, we find a region that CAN and rewrite
# the storefront — AMD's public wrapper carries multi-region accounts, so it
# pulls the pre-release from there. (Verified earlier: a foreign-region URL
# downloads fine via AMD.)
_REGION_PROBE = ["nz", "au", "us", "ca", "jp", "gb", "de", "fr", "ie", "nl"]
_AVAIL_CACHE: dict = {}        # apple_id -> (ts, url_or_None)
_AVAIL_TTL = 1800.0


def _apple_id(url: str) -> str:
    m = (re.search(r"[?&]i=(\d+)", url or "")
         or re.search(r"/(?:album|song|music-video)/[^/]+/(\d+)", url or "")
         or re.search(r"/(\d+)(?:\?|$)", url or ""))
    return m.group(1) if m else ""


def _rewrite_storefront(url: str, cc: str) -> str:
    return re.sub(r"(music\.apple\.com/)[a-z]{2}(/)", rf"\g<1>{cc}\g<2>", url, count=1, flags=re.I)


async def resolve_available_url(url: str, config: dict):
    """If the Apple link isn't streamable in its own storefront, return a URL
    rewritten to a region that CAN stream it (pre-release case). Returns
    ``(url, note)`` — url unchanged when already streamable or on any error.
    Music videos: gamdl is region-locked to the cookies account, so a foreign
    link (e.g. /nz/music-video/…) 404s. The video ID is GLOBAL, so rewrite the
    link's storefront to the account's region — same video, reachable region."""
    if is_apple_music_video(url):
        acct = (config.get("storefront") or "us").lower()
        sf = url_storefront(url)
        if sf and sf != acct:
            return _rewrite_storefront(url, acct), f"🎬 видео: регион '{sf}'→'{acct}' (cookies-аккаунт)"
        return url, ""
    aid = _apple_id(url)
    if not aid:
        return url, ""
    now = time.time()
    hit = _AVAIL_CACHE.get(aid)
    if hit and now - hit[0] < _AVAIL_TTL:
        return (hit[1] or url), ("" if not hit[1] or hit[1] == url else
                                 f"⚠ пре-релиз: регион '{url_storefront(hit[1])}' (публичный wrapper)")
    url_sf = url_storefront(url) or (config.get("storefront") or "us").lower()

    async def _streamable(client, cc):
        try:
            r = await client.get("https://itunes.apple.com/lookup",
                                  params={"id": aid, "country": cc, "entity": "song"},
                                  timeout=8)
            for x in (r.json().get("results") or []):
                if x.get("kind") == "song" or x.get("wrapperType") == "track":
                    return bool(x.get("isStreamable"))
        except Exception:
            return None
        return None

    try:
        from ripster.http_client import aclient
        c = aclient()
        if await _streamable(c, url_sf):
            _AVAIL_CACHE[aid] = (now, url)
            return url, ""
        for cc in _REGION_PROBE:
            if cc == url_sf:
                continue
            if await _streamable(c, cc):
                new = _rewrite_storefront(url, cc)
                _AVAIL_CACHE[aid] = (now, new)
                return new, f"⚠ недоступно в '{url_sf}' — беру регион '{cc}' (пре-релиз, публичный wrapper)"
    except Exception:
        pass
    _AVAIL_CACHE[aid] = (now, url)
    return url, ""


def is_apple_music_video(url: str) -> bool:
    """True for an Apple Music *music video* link (``/music-video/…``).

    Video can only be handled by gamdl (zhaarey/amd are audio-only) at the ``mv``
    quality — ``route_apple`` forces both when it sees such a URL.
    """
    u = (url or "").lower()
    return "music.apple.com" in u and "/music-video/" in u

# Quality ids that REQUIRE a wrapper (lossless / spatial).
_LOSSLESS = {"alac", "alac-hires", "atmos", "ec3", "ac3", "aac-binaural", "aac-downmix"}
# Quality ids that mean "music video".
_VIDEO = {"mv", "music-video", "video"}

# Probe results are cached briefly so a burst of queue adds doesn't hammer the
# network / re-open sockets on every single call.
_probe_cache: dict[str, tuple[float, bool]] = {}
_TTL = 45.0


def _cached(key: str, fn) -> bool:
    now = time.time()
    hit = _probe_cache.get(key)
    if hit and now - hit[0] < _TTL:
        return hit[1]
    try:
        val = bool(fn())
    except Exception:
        val = False
    _probe_cache[key] = (now, val)
    return val


def _public_wrapper_ok(config: dict) -> bool:
    # Honour the pool health gate first — the server can answer HTTP fine while
    # its instance pool has nobody connected (see public_wrapper_healthy below).
    if not public_wrapper_healthy():
        return False
    host = (config.get("amd-instance-url") or "").strip()
    if not host:
        return False
    scheme = "https" if config.get("amd-instance-secure", True) else "http"
    url = f"{scheme}://{host}"
    return _cached(f"pub:{url}", lambda: httpx.get(url, timeout=6.0).status_code < 500)


# ── Local wrapper CKC health gate ─────────────────────────────────────────────
# A TCP-open check is NOT enough: the local docker wrapper's port can be open
# while its saved Apple session can't mint content keys (logs "Invalid CKC
# error", the Go side dies with "decryptFragment: EOF"). When the zhaarey engine
# sees such a decrypt failure it calls ``mark_local_wrapper_unhealthy()`` so the
# router stops sending lossless work to a wrapper that only produces garbage —
# it auto-routes to the public wrapper instead until the session is re-logged in.
_local_unhealthy_until: float = 0.0


def mark_local_wrapper_unhealthy(ttl: float = 900.0) -> None:
    """Flag the local docker wrapper as unable to decrypt (bad/expired Apple
    session) for ``ttl`` seconds — the router will skip it meanwhile."""
    global _local_unhealthy_until
    _local_unhealthy_until = time.time() + ttl


def local_wrapper_healthy() -> bool:
    """False while the local wrapper is in its post-CKC-failure cooldown."""
    return time.time() >= _local_unhealthy_until


# ── Public wrapper-manager pool health gate ───────────────────────────────────
# The plain HTTP reachability check (``_public_wrapper_ok``) only proves the
# wm.wol.moe server itself answers — it says nothing about whether the POOL
# behind it has any actual wrapper instance online. Confirmed 2026-07-22: the
# gRPC channel opens fine, but every real request fails with
# "WrapperManagerException: no healthy and ready instances available" — a
# volunteer-hosted pool with zero connected instances at that moment. The AMD
# engine calls ``mark_public_wrapper_unhealthy()`` on that exact error so the
# router stops sending traffic into a ~28s guaranteed-fail retry loop until
# the cooldown expires and it's worth probing again.
_public_unhealthy_until: float = 0.0


def mark_public_wrapper_unhealthy(ttl: float = 300.0) -> None:
    """Flag the public wm.wol.moe pool as having no ready instances for ``ttl``
    seconds — the router treats it as down meanwhile instead of retrying blind."""
    global _public_unhealthy_until
    _public_unhealthy_until = time.time() + ttl


def public_wrapper_healthy() -> bool:
    """False while the public pool is in its post-failure cooldown."""
    return time.time() >= _public_unhealthy_until


def _local_wrapper_ok(config: dict) -> bool:
    # Honour the CKC health gate first — a wrapper that just failed to decrypt
    # is treated as down even though its socket is still listening.
    if not local_wrapper_healthy():
        return False
    raw = str(config.get("decrypt-port") or "127.0.0.1:10020")
    host, _, port_s = raw.rpartition(":")
    host = host or "127.0.0.1"
    try:
        port = int(port_s)
    except ValueError:
        return False

    def _probe() -> bool:
        s = socket.socket()
        s.settimeout(1.0)
        try:
            s.connect((host, port))
            return True
        finally:
            s.close()

    return _cached(f"loc:{host}:{port}", _probe)


def _cookies_ok(config: dict) -> bool:
    p = (config.get("gamdl-cookies-path") or "").strip() or "cookies.txt"
    try:
        return Path(p).is_file() and Path(p).stat().st_size > 0
    except OSError:
        return False


def route_apple(quality: str, config: dict, url: str = "") -> dict:
    """Pick the best (engine, quality) for an Apple download of ``quality``.

    Returns ``{engine, quality, degraded, note}``. ``degraded`` is True when the
    requested quality could not be delivered and a lower one was substituted.

    REGION RULE: the cookies-based engine (gamdl) can only reach the catalog of
    the *account's* storefront. When the link points at a DIFFERENT region (e.g.
    a release that's already out in /nz/ but not yet in our /gb/ account), gamdl
    would 404 — so we steer such audio to AMD, whose public wrapper-manager
    carries many regional accounts. (Video stays gamdl-only and can't cross
    regions; ALAC/Atmos already go to AMD.)
    """
    q = (quality or "").lower().strip()
    # A /music-video/ link is always video, regardless of the requested codec.
    if is_apple_music_video(url):
        q = "mv"
    cookies = _cookies_ok(config)
    acct_sf = (config.get("storefront") or "us").lower()
    url_sf  = url_storefront(url)
    foreign = bool(url_sf and url_sf != acct_sf)

    # When the owner forces the local wrapper, return it immediately and NEVER
    # probe the public wm.wol.moe (keeps it out of the logs and the path entirely).
    if q in _LOSSLESS and (config.get("apple-wrapper") or "auto").strip().lower() == "local":
        return {"engine": "zhaarey", "quality": q, "degraded": False,
                "note": f"{q.upper()} · локальный wrapper (премиум)"}

    pub_ok  = _public_wrapper_ok(config)

    # ── Video — gamdl only (cookies + bundled CDM, no wrapper) ───────────────
    if q in _VIDEO:
        note = "" if cookies else "⚠ нет cookies.txt — видео не скачается"
        if foreign:
            note = (note + " · " if note else "") + f"⚠ видео региона '{url_sf}' недоступно — cookies-аккаунт = '{acct_sf}'"
        return {"engine": "gamdl", "quality": "mv", "degraded": False, "note": note}

    # ── Lossless / spatial — a wrapper is mandatory ──────────────────────────
    # Policy: KEEP lossless (never silently fall back to lossy AAC). Prefer the
    # public wrapper (multi-region, reliably subscribed); use the local docker
    # wrapper only when it's same-region AND CKC-healthy (see the health gate).
    # If no wrapper looks ready we still hand it to AMD's public wrapper and let
    # it queue — quality over speed, by the user's choice.
    if q in _LOSSLESS:
        # Which wrapper to use is the OWNER's choice (Settings → Apple → Wrapper):
        #   "local"  → always the local docker wrapper (owner's premium account);
        #   "public" → always the public wm.wol.moe wrapper-manager;
        #   "auto"   → local for same-region (reliable premium), public otherwise.
        # Nothing is hard-wired: each mode just works when selected. Foreign-region
        # links can only be served by the multi-region public wrapper, so a "local"
        # choice still uses public for those (the local account can't see them).
        pref = (config.get("apple-wrapper") or "auto").strip().lower()
        local_ok = _local_wrapper_ok(config)

        if pref == "public":
            if pub_ok:
                return {"engine": "amd", "quality": q, "degraded": False,
                        "note": f"{q.upper()} · публичный wrapper" + (f" · регион {url_sf}" if foreign else "")}
            return {"engine": "amd", "quality": q, "degraded": False,
                    "note": f"{q.upper()} · публичный wrapper в очереди"}

        if pref == "local":
            # Explicit user choice: ALWAYS the local wrapper, NEVER public — even
            # if the local wrapper looks down (it'll queue/retry on its own). The
            # owner does not want the public pool used under any circumstances.
            return {"engine": "zhaarey", "quality": q, "degraded": False,
                    "note": f"{q.upper()} · локальный wrapper (премиум)"}

        # auto: prefer the local premium wrapper whenever it's up (reliable),
        # public pool as fallback.
        if local_ok:
            return {"engine": "zhaarey", "quality": q, "degraded": False,
                    "note": f"{q.upper()} · локальный wrapper (премиум)"}
        if pub_ok:
            return {"engine": "amd", "quality": q, "degraded": False,
                    "note": f"{q.upper()} · публичный wrapper" + (f" · регион {url_sf}" if foreign else "")}
        return {"engine": "amd", "quality": q, "degraded": False,
                "note": f"{q.upper()} · wrapper в очереди — ждём lossless"}

    # ── AAC / lossy ──────────────────────────────────────────────────────────
    # Cookies (gamdl) for the account's own region; AMD's multi-region public
    # wrapper for foreign-region links the cookies-account can't see yet.
    if q in ("aac", "aac-legacy", ""):
        if foreign and pub_ok:
            return {"engine": "amd", "quality": "aac", "degraded": False,
                    "note": f"AAC · публичный wrapper · регион {url_sf} (вне аккаунта '{acct_sf}')"}
        if cookies:
            return {"engine": "gamdl", "quality": q or "aac", "degraded": False, "note": ""}
        if pub_ok:
            return {"engine": "amd", "quality": "aac", "degraded": False, "note": "AAC · AMD"}
        return {"engine": "gamdl", "quality": q or "aac", "degraded": False, "note": ""}

    # ── Unknown quality id — keep the configured engine, no override ─────────
    return {"engine": config.get("engine", "zhaarey"), "quality": quality,
            "degraded": False, "note": ""}
