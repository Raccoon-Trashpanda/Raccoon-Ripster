"""SoundCloud route pure helpers: artwork-size upgrade, description tracklist
parsing, and `_sc_host_ok` — the allow-list that gates the m3u8/key/license
proxies (an SSRF guard), so its accept/reject cases are pinned."""
import pytest

from ripster.routes import soundcloud as sc


# ── _artwork ─────────────────────────────────────────────────────────────────
def test_artwork_upgrade():
    assert sc._artwork("https://i1.sndcdn.com/art-large.jpg") == "https://i1.sndcdn.com/art-t500x500.jpg"  # default size
    assert sc._artwork("https://i1.sndcdn.com/art-t67x67.jpg", "original") == "https://i1.sndcdn.com/art-original.jpg"
    assert sc._artwork("") == ""
    assert sc._artwork("https://x/plain.jpg") == "https://x/plain.jpg"   # no size token → unchanged


# ── _split_artist_title ──────────────────────────────────────────────────────
@pytest.mark.parametrize("body,expected", [
    ("Artist - Title", ("Artist", "Title")),
    ("Artist – Title", ("Artist", "Title")),     # en dash
    ("Just A Title", ("", "Just A Title")),
])
def test_split_artist_title(body, expected):
    assert sc._split_artist_title(body) == expected


# ── _parse_sc_tracklist ──────────────────────────────────────────────────────
def test_parse_tracklist_numbered():
    out = sc._parse_sc_tracklist("01. A - T1\n02. B - T2\n03. C - T3")
    assert len(out) == 3
    assert out[0] == {"timestamp": "", "artist": "A", "title": "T1"}


def test_parse_tracklist_timestamped():
    out = sc._parse_sc_tracklist("1:23 A - T1\n2:34 B - T2\n3:45 C - T3")
    assert len(out) == 3
    assert out[0]["timestamp"] == "1:23"


def test_parse_tracklist_too_short_is_empty():
    assert sc._parse_sc_tracklist("01. A - T1\n02. B - T2") == []


def test_parse_tracklist_drops_url_lines():
    # 2 real + 1 url line → only 2 valid → below the 3-line threshold → []
    assert sc._parse_sc_tracklist("01. A - T1\n02. B - T2\n03. http://x") == []


# ── _sc_host_ok (SSRF allow-list) ────────────────────────────────────────────
@pytest.mark.parametrize("url,ok", [
    ("https://cf-media.sndcdn.com/abc.m3u8", True),
    ("https://api.soundcloud.com/x", True),
    ("https://soundcloud.com/x", True),
    ("https://evil.com/x", False),
    ("https://sndcdn.com.evil.com/x", False),   # suffix-spoof must be rejected
    ("not a url", False),
])
def test_sc_host_ok(url, ok):
    assert sc._sc_host_ok(url) is ok


# ── _norm_track ──────────────────────────────────────────────────────────────
def test_norm_track():
    t = {
        "id": 1, "title": "Song",
        "user": {"username": "DJ", "avatar_url": "https://i.sndcdn.com/av-large.jpg"},
        "duration": 180000, "permalink_url": "https://soundcloud.com/dj/song",
        "genre": "House", "created_at": "2024-05-01T10:00:00Z",
    }
    out = sc._norm_track(t)
    assert out["title"] == "Song"
    assert out["artist"] == "DJ"
    assert out["duration"] == 180
    assert out["date"] == "2024-05-01"
    assert out["artwork_sm"] == "https://i.sndcdn.com/av-t500x500.jpg"
    assert out["has_tracklist"] is False
