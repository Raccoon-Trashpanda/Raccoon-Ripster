"""URL detection + parsing — the core, pure, network-free logic that decides
how every download link is interpreted. Misparsing here = wrong/empty downloads."""
import pytest

from ripster import resolver


# ── _is_single_track ────────────────────────────────────────────────────────
@pytest.mark.parametrize("url,expected", [
    # Apple: album with ?i= is a single track; bare album is not
    ("https://music.apple.com/us/album/foo/123?i=456", True),
    ("https://music.apple.com/us/album/foo/123", False),
    ("https://music.apple.com/us/song/foo/123", True),
    ("https://music.apple.com/us/music-video/foo/123", True),
    # Deezer / Qobuz / Tidal: only /track/ is single
    ("https://www.deezer.com/track/123", True),
    ("https://www.deezer.com/album/123", False),
    ("https://open.qobuz.com/track/123", True),
    ("https://open.qobuz.com/album/123", False),
    ("https://tidal.com/browse/track/123", True),
    ("https://tidal.com/browse/album/123", False),
    # Unknown service → treat as single track
    ("https://example.com/whatever", True),
])
def test_is_single_track(url, expected):
    assert resolver._is_single_track(url) is expected


# ── _parse_apple ────────────────────────────────────────────────────────────
@pytest.mark.parametrize("url,expected", [
    ("https://music.apple.com/gb/album/name/123?i=456", ("gb", "album", "123")),
    ("https://music.apple.com/us/album/name/999", ("us", "album", "999")),
    ("https://music.apple.com/us/playlist/foo/pl.abc123", ("us", "playlist", "pl.abc123")),
])
def test_parse_apple(url, expected):
    assert resolver._parse_apple(url) == expected


# ── _parse_deezer / _parse_qobuz / _parse_tidal share the same shape ─────────
@pytest.mark.parametrize("fn,url,expected", [
    (resolver._parse_deezer, "https://www.deezer.com/en/album/12345", ("album", "12345")),
    (resolver._parse_deezer, "https://www.deezer.com/track/999", ("track", "999")),
    (resolver._parse_deezer, "https://www.deezer.com/", ("", "")),
    (resolver._parse_qobuz, "https://open.qobuz.com/album/777", ("album", "777")),
    # qobuz web form carries a slug before the numeric id → take the id
    (resolver._parse_qobuz, "https://www.qobuz.com/us-en/album/some-slug/888", ("album", "888")),
    (resolver._parse_tidal, "https://tidal.com/browse/album/444", ("album", "444")),
    (resolver._parse_tidal, "https://tidal.com/browse/track/555", ("track", "555")),
    # tidal playlists use non-numeric UUIDs — must survive the numeric-id rule
    (resolver._parse_tidal, "https://tidal.com/browse/playlist/36ea71a8-445e-41a4-82ab-6628c581535d",
     ("playlist", "36ea71a8-445e-41a4-82ab-6628c581535d")),
    (resolver._parse_tidal, "https://tidal.com/browse/artist/1", ("", "")),
])
def test_parse_service(fn, url, expected):
    assert fn(url) == expected


def test_single_returns_one_track():
    out = resolver._single("https://example.com/x")
    assert len(out) == 1
    assert out[0]["url"] == "https://example.com/x"
    assert out[0]["total"] == 1
