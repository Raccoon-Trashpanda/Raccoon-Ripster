"""Beatport engine (OrpheusDL) + route formatting helpers. The `is_finished`
subscription-gate ordering is the key correctness point: Orpheus prints
"Professional subscription detected" on EVERY good run, so success must be
checked before the subscription gate (else real downloads read as failures)."""
import pytest

from ripster.engines.orpheus_beatport import OrpheusBeatportEngine
from ripster.routes import beatport as bp


# ── engine ───────────────────────────────────────────────────────────────────
@pytest.fixture
def be():
    return OrpheusBeatportEngine()


def test_beatport_qualities(be):
    qs = be.qualities()
    assert len(qs) == 3
    assert all(q["engine"] == "orpheus_beatport" for q in qs)
    assert {"hifi", "high", "minimum"} == {q["id"] for q in qs}


@pytest.mark.parametrize("line,expected", [
    ("error: boom", "error"),
    ("HTTP 401", "error"),
    ("Professional subscription detected", "warn"),
    ("skipping already exist", "warn"),
    ("Downloading track file", "success"),
    ("neutral", "stdout"),
])
def test_beatport_classify_line(be, line, expected):
    assert be.classify_line(line) == expected


def test_beatport_parse_progress(be):
    assert be.parse_progress("3/12", 0, 0) == (2, 12)
    assert be.parse_progress("nope", 1, 1) == (1, 1)


def test_beatport_finished_not_authed(be):
    assert be.is_finished("BEATPORT_NOT_AUTHED").success is False
    assert be.is_finished("HTTP 401 forbidden").success is False


def test_beatport_finished_success_despite_subscription_word(be):
    # the ordering guard: "Professional subscription detected" + a real download
    # must read as success, not as a subscription error
    r = be.is_finished("Professional subscription detected\nDownloading track file")
    assert r.success is True and r.tracks_ok == 1


def test_beatport_finished_real_subscription_error(be):
    r = be.is_finished("subscription required for this tier", rc=1)
    assert r.success is False and "Professional" in r.error


def test_beatport_finished_skip_rc0(be):
    r = be.is_finished("already exists", rc=0)
    assert r.success is True and r.tracks_ok == 0


def test_beatport_finished_empty(be):
    assert be.is_finished("", rc=0).success is False


# ── route helpers ────────────────────────────────────────────────────────────
def test_auth_headers():
    assert bp._auth_headers("tok") == {"Authorization": "Bearer tok"}


def test_preview_url():
    assert bp._preview_url({"sample_url": "http://x.mp3"}) == "http://x.mp3"
    assert bp._preview_url({"id": 123}) == "https://geo-samples.beatport.com/track/123.LOFI.mp3"
    assert bp._preview_url({}) == ""


@pytest.mark.parametrize("val,expected", [
    ({"name": "Techno"}, "Techno"),
    ("House", "House"),
    (None, ""),
    (123, ""),
])
def test_dict_name(val, expected):
    assert bp._dict_name(val) == expected


def test_fmt_track():
    t = {
        "id": 5, "name": "Song", "mix_name": "Original Mix",
        "artists": [{"name": "A"}, {"name": "B"}],
        "genre": {"name": "Techno"},
        "image": {"uri": "http://img/{w}x{h}.jpg"},
        "publish_date": "2024-01-02", "slug": "song", "bpm": 128,
    }
    out = bp._fmt_track(t)
    assert out["title"] == "Song"
    assert out["artist"] == "A, B"
    assert out["genre"] == "Techno"
    assert out["artworkUrl"] == "http://img/400x400.jpg"
    assert out["year"] == "2024"
    assert out["previewUrl"] == "https://geo-samples.beatport.com/track/5.LOFI.mp3"
    assert out["url"] == "https://www.beatport.com/track/song/5"
