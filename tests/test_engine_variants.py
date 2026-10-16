"""Remaining engine variants: gamdl (Apple MV/AAC), spotiflac, zotify, and the
engine registry. Zotify's BadCredentials path deletes creds as a side effect, so
that test monkeypatches `_delete_creds`."""
import pytest

from ripster.engines.gamdl import GamdlEngine
from ripster.engines.spotiflac import SpotiflacEngine
from ripster.engines import zotify as zot
from ripster.engines.zotify import ZotifyEngine
from ripster.engines import registry


# ════════════════════════════ gamdl ════════════════════════════
@pytest.fixture
def gd():
    return GamdlEngine()


def test_gamdl_qualities(gd):
    qs = gd.qualities()
    assert qs and all(q["engine"] == "gamdl" for q in qs)


@pytest.mark.parametrize("line,expected", [
    ("an error here", "error"),
    ("warning: skipping", "warn"),
    ("Finished ripping", "success"),
    ("saving file", "success"),
    ("neutral", "stdout"),
])
def test_gamdl_classify(gd, line, expected):
    assert gd.classify_line(line) == expected


def test_gamdl_parse_progress(gd):
    assert gd.parse_progress("[Track 3/12]", 0, 0) == (3, 12)
    assert gd.parse_progress("nope", 1, 1) == (1, 1)


def test_gamdl_finished_drm(gd):
    r = gd.is_finished("KeyError: 'AUDIO-SESSION-KEY-IDS'")
    assert r.success is False and "wrapper" in r.error.lower()


def test_gamdl_finished_ok(gd):
    r = gd.is_finished("Finished with 0 errors")
    assert r.success is True and r.tracks_err == 0


def test_gamdl_finished_with_errors(gd):
    r = gd.is_finished("Finished with 2 errors")
    assert r.success is False and r.tracks_err == 2


def test_gamdl_finished_no_marker(gd):
    assert gd.is_finished("random").success is False


# ════════════════════════════ spotiflac ════════════════════════════
@pytest.fixture
def sf():
    return SpotiflacEngine()


def test_spotiflac_qualities(sf):
    qs = sf.qualities()
    assert len(qs) == 1 and qs[0]["engine"] == "spotiflac" and qs[0]["id"] == "flac"


@pytest.mark.parametrize("line,expected", [
    ("Failed [1]", "error"),
    ("error: x", "error"),
    ("Success [2]", "success"),
    ("Summary: 1 Success, 0 Failed", "success"),
    ("queued 3", "info"),
    ("neutral", "stdout"),
])
def test_spotiflac_classify(sf, line, expected):
    assert sf.classify_line(line) == expected


def test_spotiflac_parse_progress(sf):
    assert sf.parse_progress("Queued 5 tracks", 0, 0) == (0, 5)
    assert sf.parse_progress("[3/12]", 0, 0) == (3, 12)


def test_spotiflac_finished(sf):
    assert sf.is_finished("Summary: 3 Success, 0 Failed").success is True
    r = sf.is_finished("Summary: 1 Success, 2 Failed")
    assert r.success is False and r.tracks_err == 2
    assert sf.is_finished("", rc=0).success is True
    assert sf.is_finished("junk", rc=1).success is False


# ════════════════════════════ zotify ════════════════════════════
@pytest.fixture
def zo():
    return ZotifyEngine()


def test_zotify_qualities(zo):
    qs = zo.qualities()
    assert qs and all(q["engine"] == "zotify" for q in qs)


@pytest.mark.parametrize("line,expected", [
    ("BadCredentials", "error"),
    ("HTTP 429 rate limit", "warn"),
    ("an error occurred", "error"),
    ("skipping track", "warn"),
    ("downloaded ok", "success"),
    ("neutral", "stdout"),
])
def test_zotify_classify(zo, line, expected):
    assert zo.classify_line(line) == expected


def test_zotify_parse_progress(zo):
    assert zo.parse_progress("Downloading 5 Songs", 0, 0) == (0, 5)
    assert zo.parse_progress("3 / 12", 0, 0) == (3, 12)


def test_zotify_finished_ok(zo):
    r = zo.is_finished("downloaded\nsaving", rc=0)
    assert r.success is True and r.tracks_ok == 2


def test_zotify_finished_premium(zo):
    r = zo.is_finished("PremiumRequired")
    assert r.success is False and "Premium" in r.error


def test_zotify_finished_silent(zo):
    assert zo.is_finished("", rc=0).success is False


def test_zotify_finished_bad_creds_monkeypatched(zo, monkeypatch):
    # avoid the real _delete_creds side effect
    monkeypatch.setattr(zot, "_delete_creds", lambda: None)
    r = zo.is_finished("BadCredentials")
    assert r.success is False


# ════════════════════════════ registry ════════════════════════════
def test_registry_get_known_engine():
    eng = registry.get_engine("gamdl")
    assert eng.name == "gamdl"


def test_registry_unknown_raises():
    with pytest.raises(KeyError):
        registry.get_engine("definitely-not-an-engine")
