"""Apple engine: URL helpers (apple_router) + the zhaarey log parser.
`is_finished` is the high-value target — it is where the "phantom success with an
empty folder" bug class lives, so its OK/failed/no-asset paths are pinned here."""
import sys
from pathlib import Path

import pytest

from ripster import apple_router as ar
from ripster.engines.zhaarey import ZhaereyEngine


# ── apple_router pure helpers ────────────────────────────────────────────────
@pytest.mark.parametrize("url,expected", [
    ("https://music.apple.com/gb/album/x/1", "gb"),
    ("https://music.apple.com/US/album/x/1", "us"),   # case-insensitive, lowered
    ("https://example.com/x", ""),
])
def test_url_storefront(url, expected):
    assert ar.url_storefront(url) == expected


@pytest.mark.parametrize("url,expected", [
    ("https://music.apple.com/us/album/name/123?i=456", "456"),   # ?i= wins
    ("https://music.apple.com/us/album/name/123", "123"),
    ("https://music.apple.com/us/song/name/789", "789"),
    ("https://example.com/no-id-here", ""),
])
def test_apple_id(url, expected):
    assert ar._apple_id(url) == expected


def test_is_apple_music_video():
    assert ar.is_apple_music_video("https://music.apple.com/us/music-video/x/1") is True
    assert ar.is_apple_music_video("https://music.apple.com/us/album/x/1") is False
    assert ar.is_apple_music_video("") is False


# ── ZhaereyEngine ────────────────────────────────────────────────────────────
@pytest.fixture
def zh():
    return ZhaereyEngine()


def test_zhaarey_qualities_tagged(zh):
    qs = zh.qualities()
    assert qs and all(q["engine"] == "zhaarey" for q in qs)


@pytest.mark.parametrize("line,expected", [
    ("ERROR: panic occurred", "error"),
    ("warning: no codec found", "warn"),
    ("Completed: 3/3", "success"),
    ("just a line", "stdout"),
])
def test_zhaarey_classify_line(zh, line, expected):
    assert zh.classify_line(line) == expected


@pytest.mark.parametrize("line,expected", [
    ("Track 3 of 12", (3, 12)),
    ("Completed: 5/10", (5, 10)),
    ("nothing here", (1, 2)),   # unchanged
])
def test_zhaarey_parse_progress(zh, line, expected):
    assert zh.parse_progress(line, 1, 2) == expected


def test_zhaarey_is_finished():
    zh = ZhaereyEngine()
    r = zh.is_finished("Completed: 5/10")
    assert r.success is True and r.tracks_ok == 5 and r.tracks_err == 5
    assert zh.is_finished("no codec found").success is False
    assert zh.is_finished("Failed to get token").success is False
    assert zh.is_finished("", rc=0).success is True
    assert zh.is_finished("", rc=1).success is False


@pytest.mark.parametrize("log", [
    "Invalid CKC error",
    "panic: decryptFragment: EOF",
    "Failed to run v2 wrapper",
])
def test_zhaarey_is_finished_invalid_ckc(log):
    # Local wrapper SESSION dead (expired/unsubscribed) — must surface the real,
    # cookies-vs-wrapper-distinct reason for the card/bot/guest, NOT "unknown
    # finish state", and must say cookies are not the cause.
    r = ZhaereyEngine().is_finished(log)
    assert r.success is False
    assert "Invalid CKC" in r.error
    assert "уки" in r.error  # mentions cookies are unrelated
    assert "unknown finish state" not in (r.error or "")


def test_zhaarey_extract_save_dir():
    import os
    log = 'building...\n[{"path": "C:/Music/Album/01 Track.m4a"}]\n'
    out = ZhaereyEngine().extract_save_dir(log)
    assert out and os.path.basename(out) == "Album"
