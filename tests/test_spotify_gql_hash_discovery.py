"""Runtime discovery of Spotify persisted-query hashes.

The radar used to carry four hand-copied sha256 constants; when Spotify ships a
new web-player bundle the old hash stops being registered and api-partner answers
HTTP 200 `{"errors":[{"message":"PersistedQueryNotFound"}]}` — which the crawl
read as "this artist released nothing". These tests pin the replacement: read the
triples out of the bundles the browser itself downloads, keep the hand-copied
value as a last resort only, and recover from a rotation with ONE retry.

No network: every fetch is served from the redacted fixtures in tests/fixtures/.
"""
import asyncio
import json
import time
from pathlib import Path

import pytest

import ripster.routes.spotify as sp
from ripster import spotify_gql_hashes as g

FIX = Path(__file__).resolve().parent / "fixtures"
ENTRY_JS = (FIX / "spotify_gql_entry_bundle.js").read_text(encoding="utf-8")
CHUNK_JS = (FIX / "spotify_gql_chunk_artist.js").read_text(encoding="utf-8")
CDN = g.CDN_PREFIX
PAGE_HTML = ('<html><script src="' + CDN + 'web-player.86442c64.js"></script>'
             '<script src="' + CDN + 'vendor~web-player.00da8f6b.js"></script></html>')

OP = "queryWhatsNewFeed"
LIVE_SHA = "1" * 64          # what today's bundle ships
OLD_SHA = "9" * 64           # what our constant used to say


class _Resp:
    """The subset of httpx.Response the code under test touches."""

    def __init__(self, payload=None, status=200):
        self.status_code = status
        self._payload = payload if payload is not None else {"data": {"ok": True}}
        self.content = json.dumps(self._payload).encode()

    def json(self):
        return self._payload


NOT_FOUND = _Resp({"errors": [{"message": "PersistedQueryNotFound"}]})
OK = _Resp({"data": {"whatsNewFeedItems": {"items": [{"id": "x"}]}}})


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Every test starts from "nothing discovered yet", writes its cache into
    tmp_path, and is cut off from the network."""
    monkeypatch.setattr(g, "_table", {})
    monkeypatch.setattr(g, "_shallow_ts", 0.0)
    monkeypatch.setattr(g, "_deep_ts", 0.0)
    monkeypatch.setattr(g, "_last_forced", 0.0)
    monkeypatch.setattr(g, "_fail_logged", {})
    monkeypatch.setattr(g, "_sweeping", False)
    monkeypatch.setattr(g, "_lock", None)
    g.configure(tmp_path / "spotify_gql_hashes.json")
    yield


def _serve(mapping):
    calls = []

    async def _get_text(client, url):
        calls.append(url)
        if url not in mapping:
            raise RuntimeError(f"HTTP 404 for {url.rsplit('/', 1)[-1]}")
        return mapping[url]
    _get_text.calls = calls
    return _get_text


BUNDLE_MAP = {
    g.PAGE_URL: PAGE_HTML,
    CDN + "web-player.86442c64.js": ENTRY_JS,
    CDN + "vendor~web-player.00da8f6b.js": "",
    CDN + "xpui-routes-artist.44e273a5.js": CHUNK_JS,
    CDN + "xpui-pip-mini-player.c6340921.js": "",
    CDN + "1049.9b83acfa.js": "",
}


# ── parsing ─────────────────────────────────────────────────────────────────

def test_parse_reads_triples_whatever_the_constructor_looks_like():
    found = g.parse_hashes(ENTRY_JS)
    assert found[OP] == [LIVE_SHA]
    # the `new (a(2700)).l(` spelling — a `new X.l(`-anchored regex loses these
    assert found["addFakeToLibrary"] == ["3" * 64]
    assert "whatsNewFeedNewItems" in found
    assert len(found) == 3, found


def test_parse_ignores_64_hex_that_is_not_a_persisted_query():
    # the fixture carries a bare content hash in an object literal; calling it an
    # operation would invent table entries nobody can query
    assert "4" * 64 not in json.dumps(g.parse_hashes(ENTRY_JS))


def test_chunk_urls_follow_the_bundle_its_own_builder():
    urls = g.chunk_urls(ENTRY_JS)
    assert CDN + "xpui-routes-artist.44e273a5.js" in urls
    # an id with a hash but no name keeps webpack's fallback: the numeric id
    assert CDN + "1049.9b83acfa.js" in urls
    assert urls, "empty chunk map means discovery silently stops at the entry bundle"


def test_chunk_urls_empty_on_a_bundle_without_a_builder():
    assert g.chunk_urls("var a = 1;") == []


def test_chunk_urls_uses_the_real_builder_not_its_quoting_in_a_comment():
    # the fixture header quotes the builder shape in a comment — a parser that
    # stops at the first match would read `{ids:names}` and conclude there are
    # no chunks, which is exactly how a chunk-only operation becomes unresolvable
    commented = ('// .u=e=>""+({ids:names})[e]+"."+({ids:hash})[e]+".js"\n' + ENTRY_JS)
    urls = g.chunk_urls(commented)
    assert CDN + "xpui-routes-artist.44e273a5.js" in urls


# ── resolution policy ───────────────────────────────────────────────────────

def test_hand_copied_hash_wins_only_while_the_bundle_still_ships_it():
    g._table = {OP: [LIVE_SHA, OLD_SHA]}
    assert g.hash_for(OP, OLD_SHA) == OLD_SHA       # our selection set, still valid


def test_discovered_hash_wins_when_ours_is_gone():
    g._table = {OP: [LIVE_SHA]}
    assert g.hash_for(OP, OLD_SHA) == LIVE_SHA


def test_fallback_is_the_last_resort_and_is_not_disguised():
    g._table = {}
    assert g.hash_for(OP, OLD_SHA) == OLD_SHA       # nothing discovered: ours or nothing


def test_mark_bad_stops_re_offering_a_refused_hash():
    g._table = {OP: [OLD_SHA, LIVE_SHA]}
    g.mark_bad(OP, OLD_SHA)
    assert g.hash_for(OP, OLD_SHA) == LIVE_SHA
    assert json.loads(g._cache_path.read_text(encoding="utf-8"))["hashes"][OP] == [LIVE_SHA]


def test_cache_round_trips_across_a_restart(tmp_path):
    g._table = {OP: [LIVE_SHA]}
    g._deep_ts = 123.0
    g._save()
    g._table, g._deep_ts = {}, 0.0
    g.configure(tmp_path / "spotify_gql_hashes.json")
    assert g.hash_for(OP, "") == LIVE_SHA
    assert g._deep_ts == 123.0


# ── recognising a rotated hash ──────────────────────────────────────────────

def test_stale_hash_detected_in_the_shape_spotify_actually_uses():
    assert g.stale_hash_error(NOT_FOUND) is True
    assert g.stale_hash_error(_Resp({"errors": [{"message": "PersistedQueryNotSupported"}]})) is True
    assert g.stale_hash_error(_Resp({}, status=412)) is True


def test_healthy_and_unrelated_answers_are_not_a_rotation():
    assert g.stale_hash_error(OK) is False
    assert g.stale_hash_error(_Resp({"errors": [{"message": "Forbidden"}]})) is False
    assert g.stale_hash_error(_Resp({"data": {}}, status=401)) is False
    assert g.stale_hash_error(_Resp({"data": {"albumUnion": {"__typename": "NotFound"}}})) is False


def test_response_without_a_json_body_does_not_raise():
    class _Broken:
        status_code = 200

        def json(self):
            raise ValueError("no json")
    assert g.stale_hash_error(_Broken()) is False


# ── the sweep ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_deep_sweep_resolves_an_operation_that_lives_only_in_a_chunk(monkeypatch):
    _last_resort_is_old_sha(monkeypatch)
    monkeypatch.setattr(g, "_get_text", _serve(BUNDLE_MAP))
    n = await g.refresh(deep=True, targets=("queryArtistDiscographyAll",))
    assert n >= 4
    # entry bundle operations AND the chunk-only one the crawl actually needs
    assert g.hash_for(OP, "") == LIVE_SHA
    assert g._table["queryArtistDiscographyAll"] == ["5" * 64]
    assert g._deep_ts and g._shallow_ts


@pytest.mark.asyncio
async def test_shallow_sweep_never_sees_the_chunk_operations(monkeypatch):
    """Honest limit, pinned: the entry bundle ships queryWhatsNewFeed but not the
    crawl's queryArtistDiscographyAll — that one lives in a lazy chunk. So a
    shallow-only sweep leaves the constant serving the radar, which is why the
    first run must go deep."""
    monkeypatch.setattr(g, "_get_text", _serve(BUNDLE_MAP))
    await g.refresh(deep=False)
    assert g._table.get(OP) == [LIVE_SHA]
    assert not g._table.get("queryArtistDiscographyAll")
    unknown_op = "5" * 64
    assert g.hash_for("queryArtistDiscographyAll", unknown_op) == unknown_op
    assert g._deep_ts == 0.0


@pytest.mark.asyncio
async def test_maybe_refresh_goes_deep_once_then_waits(monkeypatch):
    seen = []

    async def _spy(deep=False, targets=()):
        seen.append(deep)
        return 0
    monkeypatch.setattr(g, "refresh", _spy)
    await g.maybe_refresh((OP,))
    assert seen == [True], "never swept → deep sweep, chunk-only ops need it"
    now = time.time()
    g._deep_ts = now
    g._shallow_ts = now
    await g.maybe_refresh((OP,))
    assert seen == [True], "fresh cache must not sweep again"
    g._shallow_ts = now - g.SHALLOW_TTL - 1
    await g.maybe_refresh((OP,))
    assert seen == [True, False], "shallow on the bundle clock"
    g._deep_ts = now - g.DEEP_TTL - 1
    await g.maybe_refresh((OP,))
    assert seen == [True, False, True], "weekly deep sweep"


@pytest.mark.asyncio
async def test_dead_network_logs_once_and_keeps_the_fallback(monkeypatch, capsys):
    async def _boom(client, url):
        raise RuntimeError("network down")
    monkeypatch.setattr(g, "_get_text", _boom)
    assert await g.refresh(deep=True) == 0
    assert g.hash_for(OP, OLD_SHA) == OLD_SHA
    await g.refresh(deep=True)
    await g.refresh(deep=True)
    out = capsys.readouterr().out
    assert out.count("gql-hashes: discovery failed") == 1, out
    assert "network down" in out


@pytest.mark.asyncio
async def test_a_page_without_bundles_says_so(monkeypatch, capsys):
    monkeypatch.setattr(g, "_get_text", _serve({g.PAGE_URL: "<html></html>"}))
    await g.refresh(deep=True)
    assert "no web-player bundle" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_two_loops_do_not_sweep_twice_at_once(monkeypatch):
    hits = []

    async def _slow(client, url):
        hits.append(url)
        await asyncio.sleep(0.01)
        return PAGE_HTML if url == g.PAGE_URL else BUNDLE_MAP.get(url, "")
    monkeypatch.setattr(g, "_get_text", _slow)
    await asyncio.gather(g.refresh(deep=True), g.refresh(deep=True), g.refresh(deep=True))
    assert hits.count(g.PAGE_URL) == 1, hits


# ── rediscovery after a refusal ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_rediscover_uses_a_candidate_it_already_has_without_the_network(monkeypatch):
    async def _no_network(deep=False, targets=()):
        raise AssertionError("a known-good candidate needs no sweep")
    monkeypatch.setattr(g, "refresh", _no_network)
    g._table = {OP: [OLD_SHA, LIVE_SHA]}
    assert await g.rediscover(OP, OLD_SHA) == LIVE_SHA
    assert g._table[OP] == [LIVE_SHA], "the refused hash must not be offered again"


@pytest.mark.asyncio
async def test_rediscover_sweeps_the_bundles_when_ours_was_the_only_candidate(monkeypatch):
    _last_resort_is_old_sha(monkeypatch)
    monkeypatch.setattr(g, "_get_text", _serve(BUNDLE_MAP))
    g._table = {OP: [OLD_SHA]}
    assert await g.rediscover(OP, OLD_SHA) == LIVE_SHA
    assert "queryArtistDiscographyAll" in g._table, "came for one op, kept the rest"


@pytest.mark.asyncio
async def test_forced_sweeps_are_throttled(monkeypatch):
    calls = []

    async def _spy(deep=False, targets=()):
        calls.append(deep)
        return 0
    monkeypatch.setattr(g, "refresh", _spy)
    g._table = {OP: [OLD_SHA]}
    assert await g.rediscover(OP, OLD_SHA) is None       # throttled, nothing known
    assert calls == [True]
    g._table = {OP: [OLD_SHA]}
    assert await g.rediscover(OP, OLD_SHA) is None
    assert calls == [True], "a rotation must not put a sweep on every request"


@pytest.mark.asyncio
async def test_rediscover_admits_when_the_operation_is_gone(monkeypatch, capsys):
    async def _empty(client, url):
        raise RuntimeError("network down")
    monkeypatch.setattr(g, "_get_text", _empty)
    g._table = {OP: [OLD_SHA]}
    assert await g.rediscover(OP, OLD_SHA) is None
    assert "not present in today's bundle" in capsys.readouterr().out


# ── the wire: routes/spotify.py actually calls all this ─────────────────────

class _FakeGC:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    async def get(self, url, params=None):
        # a copy, not the dict itself: the retry rewrites `extensions` in place,
        # and a recorded call has to keep the params that were actually sent
        self.calls.append(dict(params or {}))
        if self.replies:
            return self.replies.pop(0)
        return NOT_FOUND      # honest: never silently reuse the last good answer


def _sha_of(params):
    return json.loads(params["extensions"])["persistedQuery"]["sha256Hash"]


def _last_resort_is_old_sha(monkeypatch):
    """Make the hand-copied constant the route falls back to equal OLD_SHA, so a
    test can say which hash belongs on the wire without hard-coding Spotify's
    real constants (which do rotate — that is the point of this module)."""
    hashes = dict(sp._SP_GQL_HASHES)
    hashes[OP] = OLD_SHA
    monkeypatch.setattr(sp, "_SP_GQL_HASHES", hashes)


@pytest.mark.asyncio
async def test_route_retries_once_with_the_discovered_hash(monkeypatch):
    """The whole point: a rotated hash costs one extra request, not a release
    feed that silently goes empty."""
    _last_resort_is_old_sha(monkeypatch)
    monkeypatch.setattr(sp._gqlh, "_table", {OP: [OLD_SHA]})
    monkeypatch.setattr(sp._gqlh, "_shallow_ts", 1e12)      # no opportunistic sweep
    monkeypatch.setattr(sp._gqlh, "_deep_ts", 1e12)
    monkeypatch.setattr(sp._gqlh, "_get_text", _serve(BUNDLE_MAP))
    gc = _FakeGC([NOT_FOUND, OK])
    r = await sp._sp_gql_get(gc, OP, {"offset": 0})
    assert [c["operationName"] for c in gc.calls] == [OP, OP]
    assert _sha_of(gc.calls[0]) == OLD_SHA
    assert _sha_of(gc.calls[1]) == LIVE_SHA, "repeat must carry the fresh hash"
    assert r is OK
    assert gc.replies == [], "exactly one retry — no loop"


@pytest.mark.asyncio
async def test_route_reports_one_request_when_no_hash_exists(monkeypatch, capsys):
    _last_resort_is_old_sha(monkeypatch)
    monkeypatch.setattr(sp._gqlh, "_table", {OP: [OLD_SHA]})
    monkeypatch.setattr(sp._gqlh, "_shallow_ts", 1e12)
    monkeypatch.setattr(sp._gqlh, "_deep_ts", 1e12)

    async def _dead(client, url):
        raise RuntimeError("network down")
    monkeypatch.setattr(sp._gqlh, "_get_text", _dead)
    gc = _FakeGC([NOT_FOUND, NOT_FOUND, NOT_FOUND])
    assert await sp._sp_gql_get(gc, OP, {}) is None
    assert len(gc.calls) == 1, gc.calls
    out = capsys.readouterr().out
    assert "gql-hashes" in out and OP in out, out


@pytest.mark.asyncio
async def test_healthy_query_costs_exactly_one_request(monkeypatch):
    _last_resort_is_old_sha(monkeypatch)
    monkeypatch.setattr(sp._gqlh, "_table", {OP: [LIVE_SHA, OLD_SHA]})
    monkeypatch.setattr(sp._gqlh, "_shallow_ts", 1e12)
    monkeypatch.setattr(sp._gqlh, "_deep_ts", 1e12)
    gc = _FakeGC([OK])
    r = await sp._sp_gql_get(gc, OP, {"offset": 0})
    assert r is OK and len(gc.calls) == 1
    assert _sha_of(gc.calls[0]) == OLD_SHA      # bundle still ships it → keep ours


@pytest.mark.asyncio
async def test_appears_on_shelf_failure_is_not_reported_as_an_empty_shelf(monkeypatch):
    """_gql_appears_on decides whether the artist gets stamped as checked. A
    refused hash must NOT count as "this artist appears on nothing"."""
    monkeypatch.setattr(sp, "_sp_gql_get",
                        lambda gc, op, var: _refused(op, var))
    items, tc = await sp._gql_appears_on(_FakeGC([]), "abc123")
    assert items == [] and tc < 0


async def _refused(op, var):
    return None
