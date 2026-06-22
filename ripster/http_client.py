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


def aclient() -> httpx.AsyncClient:
    """Shared pooled AsyncClient (lazily created in the running event loop)."""
    global _async
    if _async is None or _async.is_closed:
        _async = httpx.AsyncClient(http2=_HTTP2, limits=_LIMITS,
                                   follow_redirects=True,
                                   headers={"User-Agent": _UA}, timeout=_TIMEOUT)
    return _async


def client() -> httpx.Client:
    """Shared pooled sync Client."""
    global _sync
    if _sync is None or _sync.is_closed:
        _sync = httpx.Client(http2=_HTTP2, limits=_LIMITS, follow_redirects=True,
                             headers={"User-Agent": _UA}, timeout=_TIMEOUT)
    return _sync


def http2_enabled() -> bool:
    return _HTTP2


@asynccontextmanager
async def ashared():
    """Drop-in for `async with httpx.AsyncClient(...) as c:` — yields the shared
    pooled client and does NOT close it. Only the constructor call changes:
        async with httpx.AsyncClient(timeout=T) as c:  →  async with ashared() as c:
    Pass per-request `timeout=`/`headers=` on the .get()/.post() calls as needed.
    NOTE: don't use this where the call needs client-level cookies/custom headers
    (keep a dedicated client there)."""
    yield aclient()


@contextmanager
def shared():
    """Sync counterpart of `ashared()`."""
    yield client()


@atexit.register
def _close_sync() -> None:
    try:
        if _sync is not None and not _sync.is_closed:
            _sync.close()
    except Exception:
        pass
