"""Shared pooled HTTP clients.

Creating a fresh ``httpx.Client``/``AsyncClient`` per call (the project did this
in 115 places) means a new TCP + TLS handshake every request. Measured cost to a
TLS host: ~888 ms/req fresh vs ~171 ms/req on a reused keep-alive client — a 5.2x
speedup (~700 ms saved per request). These singletons keep connections warm and
pooled across the whole app, which speeds up stream resolution, search/parsing
and metadata loads everywhere they're adopted.

Usage (drop-in for `async with httpx.AsyncClient(...) as c:` blocks):

    from ripster.http_client import aclient
    r = await aclient().get(url, headers=..., timeout=...)

Per-request `headers`/`timeout`/`params` still work and override the defaults.
Do NOT close the returned client — it's process-lifetime and shared.

Proxy channels (087):
    Config keys: ``proxy-api``, ``proxy-download``, ``proxy-metadata``,
    ``proxy`` (fallback). Each channel can have its own proxy URL.
    When a proxy is configured for a channel, ALL traffic for that channel
    goes through it — no silent fallback to direct connection (IP leak).
"""
from __future__ import annotations

import atexit
from contextlib import asynccontextmanager, contextmanager

import httpx

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# HTTP/2 intentionally DISABLED: some upstreams (notably iTunes/Apple) drop
# pooled h2 connections with GOAWAY → httpx surfaces `ConnectionTerminated
# error_code:4` as a search failure. Plain HTTP/1.1 keep-alive pooling already
# delivers the ~5x handshake-reuse speedup and is rock-solid here.
_HTTP2 = False

_LIMITS = httpx.Limits(max_keepalive_connections=30, max_connections=100,
                       keepalive_expiry=30.0)
_TIMEOUT = httpx.Timeout(15.0, connect=8.0)

_async: httpx.AsyncClient | None = None
_sync: httpx.Client | None = None
_async_proxied: dict[str, httpx.AsyncClient] = {}
_sync_proxied: dict[str, httpx.Client] = {}

_config: dict = {}

# Channels: api (service APIs), download (content), metadata (external providers)
_CHANNELS = ("api", "download", "metadata")


def install(cfg: dict) -> None:
    """Inject config for proxy channel resolution."""
    global _config
    _config = cfg or {}


def proxy_url(channel: str = "") -> str:
    """Return the proxy URL for *channel*, or "" if no proxy is configured.
    Checks ``proxy-<channel>`` first, then falls back to ``proxy``.
    Never logs the proxy URL (may contain credentials)."""
    ch = (channel or "").strip().lower()
    if ch and ch in _CHANNELS:
        url = str(_config.get(f"proxy-{ch}") or "").strip()
        if url:
            return url
    return str(_config.get("proxy") or "").strip()


def _make_async(proxy: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(http2=_HTTP2, limits=_LIMITS,
                             follow_redirects=True,
                             headers={"User-Agent": _UA}, timeout=_TIMEOUT,
                             proxy=proxy or None)


def _make_sync(proxy: str) -> httpx.Client:
    return httpx.Client(http2=_HTTP2, limits=_LIMITS, follow_redirects=True,
                        headers={"User-Agent": _UA}, timeout=_TIMEOUT,
                        proxy=proxy or None)


def aclient(channel: str = "") -> httpx.AsyncClient:
    """Shared pooled AsyncClient. If *channel* has a proxy configured, returns
    a separate pooled client for that channel (never mixes proxied and direct
    traffic on the same client)."""
    global _async
    px = proxy_url(channel)
    if px:
        if px not in _async_proxied:
            _async_proxied[px] = _make_async(px)
        return _async_proxied[px]
    if _async is None or _async.is_closed:
        _async = _make_async("")
    return _async


def client(channel: str = "") -> httpx.Client:
    """Shared pooled sync Client (see ``aclient`` for proxy channel semantics)."""
    global _sync
    px = proxy_url(channel)
    if px:
        if px not in _sync_proxied:
            _sync_proxied[px] = _make_sync(px)
        return _sync_proxied[px]
    if _sync is None or _sync.is_closed:
        _sync = _make_sync("")
    return _sync


def http2_enabled() -> bool:
    return _HTTP2


@asynccontextmanager
async def ashared(channel: str = ""):
    """Drop-in for `async with httpx.AsyncClient(...) as c:` — yields the shared
    pooled client and does NOT close it. Only the constructor call changes:
        async with httpx.AsyncClient(timeout=T) as c:  →  async with ashared() as c:
    Pass per-request `timeout=`/`headers=` on the .get()/.post() calls as needed.
    *channel* selects the proxy route ("" = direct/default pool).
    NOTE: don't use this where the call needs client-level cookies/custom headers
    (keep a dedicated client there)."""
    yield aclient(channel)


@contextmanager
def shared(channel: str = ""):
    """Sync counterpart of ``ashared()``."""
    yield client(channel)


@atexit.register
def _close_sync() -> None:
    try:
        if _sync is not None and not _sync.is_closed:
            _sync.close()
    except Exception:
        pass
    for c in _sync_proxied.values():
        try:
            if not c.is_closed:
                c.close()
        except Exception:
            pass
