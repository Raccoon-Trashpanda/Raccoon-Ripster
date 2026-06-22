"""Apple Music metadata URL parsing — single-track vs album, station rejection.

These guard two real bugs:
  * a /song/ or /album/…?i= link must resolve to ONE track (trackCount logic), and
  * Apple radio stations / DJ-mix episodes (/station/…/ra.<id>) are a DRM radio
    stream, NOT a catalog track/album — they must be rejected with a clear reason
    instead of being queued and failing cryptically (see task #8).
"""
import pytest

from ripster.metadata.apple import _parse_apple_url


@pytest.mark.parametrize("url,expected", [
    ("https://music.apple.com/us/album/x/123456",            ("us", "albums", "123456")),
    ("https://music.apple.com/nz/song/lane-8-on-higher/1782953118", ("nz", "songs", "1782953118")),
    ("https://music.apple.com/us/album/x/123?i=789",         ("us", "songs", "789")),
    ("https://music.apple.com/us/music-video/x/555",         ("us", "music-videos", "555")),
])
def test_parse_apple_url_kinds(url, expected):
    assert _parse_apple_url(url) == expected


@pytest.mark.parametrize("url", [
    "https://music.apple.com/nz/station/lane-8/ra.1344044807",
    "https://music.apple.com/nz/station/martin-solveig-lane-8/ra.1490647032",
])
def test_parse_apple_url_rejects_stations(url):
    # ra.* radio stations cannot be downloaded — must raise a human reason.
    with pytest.raises(ValueError) as e:
        _parse_apple_url(url)
    assert "ra.*" in str(e.value) or "станци" in str(e.value).lower()
