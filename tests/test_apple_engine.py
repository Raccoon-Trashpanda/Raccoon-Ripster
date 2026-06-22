"""Apple engine: URL helpers (apple_router) + the AMD/zhaarey log parsers.
`is_finished` is the high-value target — it is where the "phantom success with an
empty folder" bug class lives, so its OK/failed/no-asset paths are pinned here."""
import sys
from pathlib import Path

import pytest

from ripster import apple_router as ar
from ripster.engines.amd import AMDEngine
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


# ── AMDEngine ────────────────────────────────────────────────────────────────
@pytest.fixture
def amd():
    return AMDEngine()


def test_amd_qualities_tagged(amd):
    qs = amd.qualities()
    assert len(qs) == 8
    assert all(q["engine"] == "amd" for q in qs)
    assert {"alac", "atmos", "aac"} <= {q["id"] for q in qs}


def test_amd_build_cmd_strips_affiliate_keeps_i(amd):
    cmd = amd.build_cmd("https://music.apple.com/us/album/x/1?i=2&uo=4", "alac", {})
    assert cmd[0] == sys.executable
    assert cmd[2] == "https://music.apple.com/us/album/x/1?i=2"   # uo=4 dropped, i kept
    assert cmd[-2:] == ["alac", "en-US"]


def test_amd_build_cmd_codec_map(amd):
    assert amd.build_cmd("u", "atmos", {})[-2] == "ec3"
    assert amd.build_cmd("u", "unknown", {})[-2] == "alac"   # fallback


def test_amd_sanitize():
    assert AMDEngine._amd_sanitize('a<b>c:d/e\\f|g?h*i') == "abcdefghi"
    assert AMDEngine._amd_sanitize("name.. ") == "name"


def test_amd_extract_save_dir():
    log = "noise\n[AMD] t [OK] OK SAVE_DIR: dir=C:/Music/Album\nmore"
    assert AMDEngine().extract_save_dir(log) == "C:/Music/Album"


def test_amd_is_finished_conn_fail(amd):
    r = amd.is_finished("Unable to connect to wm.wol.moe")
    assert r.success is False and "unreachable" in r.error


def test_amd_is_finished_summary_ok(amd):
    r = amd.is_finished("DONE: Finished in 5s — tasks: 5 total, 5 OK, 0 failed, 0 cancelled")
    assert r.success is True and r.tracks_ok == 5 and r.tracks_err == 0


def test_amd_is_finished_summary_all_failed(amd):
    r = amd.is_finished("DONE: tasks: 3 total, 0 OK, 3 failed")
    assert r.success is False and r.tracks_err == 3


def test_amd_is_finished_no_lossless_asset(amd):
    # the phantom-success guard: an asset that doesn't exist must NOT read as OK
    r = amd.is_finished("Audio does not exist for this track")
    assert r.success is False and "lossless" in r.error.lower()


def test_amd_is_finished_per_track_success(amd):
    r = amd.is_finished("SUCCESS - Finished ripping\nAll done")
    assert r.success is True and r.tracks_ok == 1


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
