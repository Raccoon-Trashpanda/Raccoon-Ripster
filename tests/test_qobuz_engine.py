"""Qobuz engine (streamrip) + the shared streamrip_utils parsers (also used by
Deezer). `is_finished` distinguishes a real download from streamrip's silent
0-track no-op (exit 0) — the core correctness concern — so those paths are pinned."""
import pytest

from ripster.engines import streamrip_utils as su
from ripster.engines.qobuz import QobuzEngine


# ── shared streamrip_utils parsers ───────────────────────────────────────────
@pytest.mark.parametrize("line,expected", [
    ("error: bad thing", "error"),
    ("Downloaded track 1", "success"),
    ("neutral line", "stdout"),
])
def test_streamrip_classify(line, expected):
    assert su.classify_line(line) == expected


def test_streamrip_parse_progress():
    assert su.parse_progress("Downloaded track", 1, 5) == (2, 5)
    assert su.parse_progress("50%", 0, 0) == (50, 100)     # percent only when total==0
    assert su.parse_progress("nothing", 3, 7) == (3, 7)


# ── QobuzEngine.qualities ────────────────────────────────────────────────────
def test_qobuz_qualities():
    qs = QobuzEngine().qualities()
    assert len(qs) == 4
    assert all(q["engine"] == "qobuz" for q in qs)
    assert {"27", "7", "6", "5"} == {q["id"] for q in qs}


# ── QobuzEngine.is_finished ──────────────────────────────────────────────────
@pytest.fixture
def qb():
    return QobuzEngine()


def test_qobuz_finished_artwork_permerror_is_success(qb):
    # Windows: streamrip raises PermissionError cleaning __artwork AFTER the file
    # is already saved → must still count as success.
    r = qb.is_finished("track saved\nPermissionError: ... __artwork ...")
    assert r.success is True and r.tracks_ok >= 1


def test_qobuz_finished_ineligible_free_account(qb):
    r = qb.is_finished("IneligibleError: Free accounts are not eligible")
    assert r.success is False and "бесплатн" in r.error.lower()


def test_qobuz_finished_auth_fail(qb):
    r = qb.is_finished("HTTP 401 unauthorized")
    assert r.success is False and "токен" in r.error.lower()


def test_qobuz_finished_download_header_success(qb):
    r = qb.is_finished("Downloading Some Album")
    assert r.success is True and r.tracks_ok == 1


def test_qobuz_finished_already_exists(qb):
    r = qb.is_finished("track already exists, skipping")
    assert r.success is True


def test_qobuz_finished_zero_tracks_is_failure(qb):
    # the key guard: streamrip exit-0 with no "Downloading" header = 0 tracks
    r = qb.is_finished("rip finished doing nothing useful")
    assert r.success is False and "0 треков" in r.error


def test_qobuz_finished_explicit_error(qb):
    r = qb.is_finished("Failed to download track 5")
    assert r.success is False
