"""Spotify persisted-query (GraphQL) hashes discovered at runtime.

api-partner `pathfinder/v1/query` accepts a query only by its sha256 — the hash
of the query text Spotify's own web player ships. Those hashes used to live here
as constants copied out of a bundle by hand, and a rotation silently turns every
consumer into "empty result": the answer to an unknown hash is HTTP 200 with
`{"errors":[{"message":"PersistedQueryNotFound"}]}`, i.e. indistinguishable from
"this artist has no new releases" unless you look at the error body.

So: fetch what the browser fetches (open.spotify.com → its web-player bundles →
the lazy chunks they reference) and read the triples the bundle itself carries:

    new i.l("queryWhatsNewFeed","query","d889c8…630e",null)

No credentials are involved — the bundles are public static assets, requested
without any Authorization/client-token/cookie header, so nothing secret can leak
into these logs or into the cache file.

Policy (see `hash_for`): a hash the live bundle still ships always beats the
hardcoded constant; the constant is only a fallback for the window where no
discovery result exists (first start, offline, or Spotify changing the page).
Hardcoded values are NOT allowed to silently win over a fresh discovery.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path

from ripster import http_client as _HTTP

PAGE_URL = "https://open.spotify.com/"
_HEADERS = {
    "Accept": "*/*",
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"),
    "Referer": PAGE_URL,
}
CDN_PREFIX = "https://open.spotifycdn.com/cdn/build/web-player/"

# Entry bundles: <script src="…/cdn/build/web-player/web-player.86442c64.js">.
_ENTRY_RE = re.compile(r'https://open\.spotifycdn\.com/cdn/build/web-player/[\w~.\-]+\.js')
# "opName","query"|,"mutation"  ,  "<64 hex>"   — the bundle-side triple. Written
# without the `new X.l(` prefix on purpose: minifiers emit both `new i.l(` and
# `new (a(2700)).l(` and the middle of that expression is exactly what changes
# between Spotify builds.
_TRIPLE_RE = re.compile(
    r'"([A-Za-z][\w.]{2,60})"\s*,\s*"(?:query|mutation)"\s*,\s*"([0-9a-f]{64})"')
# webpack chunk-name and chunk-hash maps, read out of the chunk-URL builder
# `.u=e=>""+({1328:"xpui-pip-mini-player",…})[e]+"."+({1049:"9b83acfa",…})[e]+".js"`
_MAP_ENTRY_RE = re.compile(r'"?(\d+)"?\s*:\s*"([^"]+)"')

SHALLOW_TTL = 6 * 3600          # entry bundles: cheap, 4-6 requests
DEEP_TTL = 7 * 24 * 3600        # full chunk sweep: ~160 requests, ~6 MB
FORCE_MIN_GAP = 15 * 60         # …but never sweep twice in one bad hour
MAX_BODY = 16 * 1024 * 1024     # a bundle we cannot read in one go is not ours
MAX_CHUNKS = 250
CONCURRENCY = 8
SHALLOW_TIMEOUT = 60.0
DEEP_TIMEOUT = 180.0

_STALE_MARKS = ("persistedquerynotfound", "persistedquerynotsupported",
                "persisted query not found", "extensions validation failed")

_table: dict = {}          # opName -> [sha256, …] as the bundle lists them
_shallow_ts: float = 0.0
_deep_ts: float = 0.0
_cache_path: Path | None = None
_lock: asyncio.Lock | None = None
_sweeping = False
_last_forced: float = 0.0
_fail_logged: dict = {}    # reason -> ts, so a dead network logs once, not per call


def configure(path) -> None:
    """Point the cache at a file next to the other Spotify state (repo convention:
    everything under base_dir is git-ignored by the whitelist .gitignore)."""
    global _cache_path, _table, _shallow_ts, _deep_ts
    _cache_path = Path(path) if path else None
    _load()


def table() -> dict:
    return _table


def parse_hashes(text: str) -> dict:
    """operationName -> [sha256, …] out of one bundle. A name can legitimately
    map to several hashes: the same operation exists in more than one selection
    set (album page vs. a shelf), which is why the resolver prefers the hash we
    already know over an arbitrary bundle order."""
    out: dict = {}
    for op, sha in _TRIPLE_RE.findall(text):
        lst = out.setdefault(op, [])
        if sha not in lst:
            lst.append(sha)
    return out


def chunk_urls(text: str, base: str = CDN_PREFIX) -> list:
    """URLs of the lazy chunks an entry bundle references.

    Every `.u=` candidate is tried, not just the first: a bundle can carry the
    builder's shape inside a comment, and stopping there would leave discovery
    convinced that there are no chunks at all — the chunk-only operations would
    then never be resolvable."""
    for m in re.finditer(r'\.u\s*=\s*\w+\s*=>', text):
        seg = text[m.end():m.end() + 400_000]
        end = re.search(r'\+\s*"\.js"', seg)
        if not end:
            continue
        lits = _object_literals(seg[:end.start()], limit=2)
        if len(lits) < 2:
            continue
        names = dict(_MAP_ENTRY_RE.findall(lits[0]))
        hashes = dict(_MAP_ENTRY_RE.findall(lits[1]))
        urls = [f"{base}{names.get(cid, cid)}.{h}.js" for cid, h in hashes.items()]
        if urls:
            return urls
    return []


def _object_literals(seg: str, limit: int = 2) -> list:
    """Inner text of the first `limit` balanced {...} groups in seg."""
    out = []
    for m in re.finditer(r'\{', seg):
        depth = 0
        for k in range(m.start(), len(seg)):
            c = seg[k]
            if c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
                if depth == 0:
                    out.append(seg[m.start() + 1:k])
                    break
        if len(out) >= limit:
            break
    return out


def merge(found: dict) -> int:
    """Fold a parsed bundle into the table. Sweeps only add: a shallow sweep
    cannot see the lazy chunks, so it must not erase hashes a deep sweep found."""
    added = 0
    for op, lst in found.items():
        cur = _table.setdefault(op, [])
        for sha in lst:
            if sha not in cur:
                cur.append(sha)
                added += 1
    return added


def hash_for(op: str, fallback: str = "") -> str:
    """The hash to send for `op`.

    A fallback the live bundle still ships is used — it is the selection set our
    parsing was written against. If the bundle knows the operation under other
    hashes only, ours has rotated: take a discovered one. With nothing discovered
    for the operation the fallback is all there is; a refusal of it is what
    triggers `rediscover`, which logs honestly when the operation is gone."""
    lst = _table.get(op) or []
    if not lst:
        return fallback
    if fallback and fallback in lst:
        return fallback
    return lst[0]


def mark_bad(op: str, sha: str) -> None:
    """Drop a hash the server just refused, so the resolver stops re-offering it.
    A later sweep re-adds it only if Spotify's bundle still ships it."""
    lst = _table.get(op)
    if not lst:
        return
    _table[op] = [h for h in lst if h != sha]
    _save()


def stale_hash_error(resp) -> bool:
    """True when the answer means "this sha256 is not registered any more".
    200-with-errors is the shape Spotify actually uses; 412 is the one the
    spec-adjacent deployments return. Anything else is a network/auth problem."""
    try:
        if getattr(resp, "status_code", 0) == 412:
            return True
        if getattr(resp, "status_code", 0) != 200:
            return False
        errs = (resp.json() or {}).get("errors") or []
    except Exception:
        return False
    for e in errs:
        msg = str((e or {}).get("message") or "").lower()
        if any(k in msg for k in _STALE_MARKS):
            return True
    return False


# ── network ─────────────────────────────────────────────────────────────────

def _log_fail(why: str) -> None:
    """Honest, once-per-window: a failure that is invisible looks like success."""
    now = time.time()
    if now - _fail_logged.get(why, 0.0) < SHALLOW_TTL:
        return
    _fail_logged[why] = now
    print(f"[spotify] gql-hashes: discovery failed — {why}", flush=True)


async def _get_text(client, url: str) -> str:
    r = await client.get(url, headers=_HEADERS, timeout=25.0)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code} for {url.rsplit('/', 1)[-1]}")
    if len(r.content) > MAX_BODY:
        raise RuntimeError(f"payload too large ({len(r.content)} B) for {url.rsplit('/', 1)[-1]}")
    return r.content.decode("utf-8", "replace")


async def _sweep(deep: bool, targets: tuple) -> int:
    """One discovery run. Merges as it goes: a sweep cut off by its own deadline
    still leaves the hashes it already read."""
    global _shallow_ts, _deep_ts
    from ripster.http_client import ashared
    found = 0
    async with ashared() as client:
        try:
            page = await _get_text(client, PAGE_URL)
        except Exception as e:
            _log_fail(f"page fetch: {type(e).__name__}: {e}")
            return 0
        entries = sorted(set(_ENTRY_RE.findall(page)))
        if not entries:
            _log_fail("no web-player bundle referenced by the page")
            return 0
        texts = []
        for i in range(0, len(entries), CONCURRENCY):
            batch = await asyncio.gather(
                *[_get_text(client, u) for u in entries[i:i + CONCURRENCY]],
                return_exceptions=True)
            for b in batch:
                if isinstance(b, Exception):
                    _log_fail(f"bundle fetch: {type(b).__name__}: {b}")
                else:
                    texts.append(b)
                    found += merge(parse_hashes(b))
        _shallow_ts = time.time()
        _save()
        if deep:
            todo, seen = [], set()
            for t in texts:
                for u in chunk_urls(t):
                    if u not in seen:
                        seen.add(u)
                        todo.append(u)
            if not todo:
                _log_fail("chunk map not found in the entry bundle")
            for i in range(0, len(todo[:MAX_CHUNKS]), CONCURRENCY):
                batch = await asyncio.gather(
                    *[_get_text(client, u) for u in todo[i:i + CONCURRENCY]],
                    return_exceptions=True)
                for b in batch:
                    if not isinstance(b, Exception):
                        found += merge(parse_hashes(b))
                        # one 404 chunk is not a discovery failure; neither is a
                        # chunk Spotify renamed since this page was generated
                _shallow_ts = time.time()
                _deep_ts = _shallow_ts
                _save()
                if targets and all(_table.get(t) for t in targets):
                    break        # everything we came for is already known
    return found


async def refresh(deep: bool = False, targets: tuple = ()) -> int:
    """Sweep, but never two at a time — the radar polls from several loops."""
    global _lock, _sweeping
    if _sweeping:
        return 0
    if _lock is None:
        _lock = asyncio.Lock()
    async with _lock:
        _sweeping = True
        try:
            try:
                return await asyncio.wait_for(
                    _sweep(deep, tuple(targets)),
                    DEEP_TIMEOUT if deep else SHALLOW_TIMEOUT)
            except asyncio.TimeoutError:
                _log_fail("sweep deadline exceeded (partial results are kept)")
            except Exception as e:
                _log_fail(f"sweep: {type(e).__name__}: {e}")
        finally:
            _sweeping = False
        return 0


async def maybe_refresh(targets: tuple = ()) -> None:
    """Opportunistic refresh, called from the product's own GQL path so nothing
    needs a scheduler: once a week (and on the very first call, when nothing is
    known yet) sweep the chunks too; in between, the entry bundles are enough to
    notice that Spotify shipped a new build."""
    now = time.time()
    if (now - _deep_ts) > DEEP_TTL:
        await refresh(deep=True, targets=targets)
    elif (now - _shallow_ts) > SHALLOW_TTL:
        await refresh(deep=False, targets=targets)


async def rediscover(op: str, bad_hash: str = "") -> str | None:
    """Called with the hash the server just refused. Returns a hash to retry
    with, or None when there is nothing better to try. Throttled: a rotated hash
    fails on every request, and an unbounded retry storm on Spotify's CDN is
    neither our style nor our defence."""
    global _last_forced
    if bad_hash:
        mark_bad(op, bad_hash)
    if _table.get(op):
        return _table[op][0]             # the table already holds another candidate
    now = time.time()
    if now - _last_forced < FORCE_MIN_GAP:
        return None
    _last_forced = now
    await refresh(deep=True, targets=(op,))
    new = hash_for(op, "")
    if not new:
        _log_fail(f"operation '{op}' not present in today's bundle")
    return new or None


# ── cache file ──────────────────────────────────────────────────────────────

def _load() -> None:
    global _table, _shallow_ts, _deep_ts
    if not _cache_path or not _cache_path.exists():
        return
    try:
        d = json.loads(_cache_path.read_text(encoding="utf-8"))
        t = d.get("hashes") or {}
        if isinstance(t, dict):
            _table = {str(k): [str(h) for h in (v if isinstance(v, list) else [v])]
                      for k, v in t.items()}
        _shallow_ts = float(d.get("shallow_ts") or 0.0)
        _deep_ts = float(d.get("deep_ts") or 0.0)
    except Exception as e:
        print(f"[spotify] gql-hashes: cache unreadable ({type(e).__name__})", flush=True)


def _save() -> None:
    if not _cache_path:
        return
    try:
        _cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = _cache_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({
            "shallow_ts": _shallow_ts, "deep_ts": _deep_ts,
            "saved": time.strftime("%Y-%m-%d %H:%M:%S"),
            "hashes": _table,
        }, ensure_ascii=False), encoding="utf-8")
        tmp.replace(_cache_path)
    except Exception as e:
        print(f"[spotify] gql-hashes: cache not written ({type(e).__name__})", flush=True)
