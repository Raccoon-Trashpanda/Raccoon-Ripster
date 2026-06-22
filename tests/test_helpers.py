"""Assorted pure helpers across modules — each maps to a real past bug class:
region rewrite (video region-lock), quality detection, scoring tiers, byte
formatting, name normalization/sanitization."""
import pytest

from ripster.apple_router import _rewrite_storefront
from ripster.engines.qobuz import _detect_actual_quality
from ripster.routes.isrc import _norm
from ripster import tracklist_match
from ripster import mixcue


# ── apple_router._rewrite_storefront ─────────────────────────────────────────
def test_rewrite_storefront():
    assert (_rewrite_storefront("https://music.apple.com/nz/music-video/x/123", "us")
            == "https://music.apple.com/us/music-video/x/123")
    # only the first storefront token is rewritten
    assert (_rewrite_storefront("https://music.apple.com/gb/album/a/1", "de")
            == "https://music.apple.com/de/album/a/1")


# ── qobuz._detect_actual_quality ─────────────────────────────────────────────
@pytest.mark.parametrize("log,expected", [
    ("downloaded [MP3] file", "5"),
    ("[FLAC][24B-192kHz]", "27"),
    ("[FLAC][24B-96kHz]", "7"),
    ("[FLAC][16B-44.1kHz]", "6"),
    ("no quality marker here", ""),
])
def test_detect_actual_quality(log, expected):
    assert _detect_actual_quality(log) == expected


# ── isrc._norm ───────────────────────────────────────────────────────────────
def test_norm():
    assert _norm("Hello, World!") == "hello world"
    assert _norm("  A.B-C  ") == "a b c"
    assert _norm("") == ""
    assert _norm(None) == ""


# ── tracklist_match._tier ────────────────────────────────────────────────────
@pytest.mark.parametrize("score,backlink,expected", [
    (0.1, True, "definitive"),
    (0.9, False, "high"),
    (0.7, False, "medium"),
    (0.5, False, "low"),
    (0.2, False, "reject"),
])
def test_tier(score, backlink, expected):
    assert tracklist_match._tier(score, backlink) == expected


# ── mixcue cue helpers ───────────────────────────────────────────────────────
@pytest.mark.parametrize("sec,expected", [
    (0.0, "00:00:00"),
    (1.0, "00:01:00"),     # 75 frames = 1 second
    (61.0, "01:01:00"),
])
def test_cue_time(sec, expected):
    assert mixcue._cue_time(sec) == expected


def test_cue_escape():
    assert mixcue._cue_escape('say "hi"') == "say 'hi'"
    assert mixcue._cue_escape("") == ""


# ── mixcue name cleaning ─────────────────────────────────────────────────────
def test_clean_mix_name():
    assert mixcue.clean_mix_name("Connecting The Dots (DJ Mix)", "oskø") == "oskø - Connecting The Dots"
    assert mixcue.clean_mix_name("obli presents: Earth Day 2026 (DJ Mix)", "obli") == "obli - Earth Day 2026"


def test_clean_mix_title():
    # presenter == artist → strip the "presents" prefix
    assert mixcue.clean_mix_title("obli presents: Earth Day 2026 (DJ Mix)", "obli") == "Earth Day 2026"
    # presenter != artist → keep the full series title
    assert mixcue.clean_mix_title("NAINA Presents: Vol 58 (DJ Mix)", "Daniel Avery") == "NAINA Presents: Vol 58"
