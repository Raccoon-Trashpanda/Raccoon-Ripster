"""Queue route: the handlers are thin async orchestration over injected deps
(runner/resolver/service_layer — covered by their own tests + test_app_builds).
The one pure, contract-bearing helper is `_make_task` — the single factory that
guarantees add_to_queue and queue_batch emit identical task shapes."""
from ripster.routes.queue import _make_task


def test_make_task_shape():
    t = _make_task("https://x/y", "lossless", "tidal", "tidal")
    # the contract: a fixed key set so every producer yields the same shape
    assert set(t) == {
        "id", "url", "quality", "engine", "service", "status", "progress",
        "meta", "log", "added", "source", "session_id",
    }
    assert t["status"] == "queued"
    assert t["progress"] == 0
    assert t["url"] == "https://x/y"
    assert t["quality"] == "lossless"
    assert t["service"] == "tidal"
    assert t["meta"] == {"service": "tidal"}
    assert t["source"] == "manual"      # default
    assert t["session_id"] == ""        # default = owner


def test_make_task_guest_session_and_source():
    t = _make_task("u", "flac", "deezer", "deezer", source="guest", session_id="abc")
    assert t["source"] == "guest"
    assert t["session_id"] == "abc"     # non-empty = guest


def test_make_task_unique_ids():
    a = _make_task("u", "q", "e", "s")
    b = _make_task("u", "q", "e", "s")
    assert a["id"] != b["id"]           # uuid-based, must differ
    assert len(a["id"]) == 8
