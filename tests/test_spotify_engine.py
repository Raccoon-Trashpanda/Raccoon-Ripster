"""Spotify engine (OrpheusDL + librespot). `is_finished` must surface token/auth
death as failure (else the bot shows "готово" with zero files) while still
delivering partial downloads — both pinned here."""
import pytest

from ripster.engines.orpheus_spotify import OrpheusSpotifyEngine


@pytest.fixture
def sp():
    return OrpheusSpotifyEngine()


def test_spotify_qualities(sp):
    qs = sp.qualities()
    assert len(qs) == 3
    assert all(q["engine"] == "orpheus_spotify" for q in qs)
    assert {"hifi", "high", "normal"} == {q["id"] for q in qs}


@pytest.mark.parametrize("line,expected", [
    ("error: boom", "error"),
    ("premium required", "warn"),
    ("skipping, already exist", "warn"),
    ("New settings detected", "warn"),
    ("Downloading track file", "success"),
    ("neutral line", "stdout"),
])
def test_spotify_classify_line(sp, line, expected):
    assert sp.classify_line(line) == expected


def test_spotify_parse_progress(sp):
    assert sp.parse_progress("Track 3/12", 0, 0) == (2, 12)   # 0-based
    assert sp.parse_progress("nothing", 1, 4) == (1, 4)


def test_spotify_finished_not_authed(sp):
    assert sp.is_finished("ORPHEUS_NOT_AUTHED").success is False
    assert sp.is_finished("Logging into Spotify").success is False


def test_spotify_finished_new_settings(sp):
    r = sp.is_finished("New settings detected")
    assert r.success is False and "настройк" in r.error.lower()


def test_spotify_finished_librespot_fail(sp):
    r = sp.is_finished("Librespot: BadCredentials")
    assert r.success is False and "ORPHEUS_NOT_AUTHED" in r.error


def test_spotify_finished_token_401(sp):
    r = sp.is_finished("getAlbum unauthorized (401)")
    assert r.success is False and "401" in r.error


def test_spotify_finished_partial_success(sp):
    r = sp.is_finished("Downloading track file\nDownloading track file")
    assert r.success is True and r.tracks_ok == 2


def test_spotify_finished_skip_rc0(sp):
    r = sp.is_finished("track already exists", rc=0)
    assert r.success is True and r.tracks_ok == 0


def test_spotify_finished_empty_output(sp):
    assert sp.is_finished("", rc=0).success is False


def test_spotify_finished_premium_required(sp):
    r = sp.is_finished("premium subscription needed", rc=1)
    assert r.success is False and "Premium" in r.error
