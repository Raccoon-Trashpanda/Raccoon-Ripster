"""Tidal engine (OrpheusDL): URL normalisation, cover URLs, quality list, and the
log parsers. `is_finished` only counts the post-download marker `=== Track <id>
downloaded ===` — counting the start marker caused phantom-success on cut DASH
downloads, so that behaviour is pinned here."""
import pytest

from ripster.engines import tidal as t
from ripster.engines.tidal import TidalEngine


# ── _to_orpheus_url ──────────────────────────────────────────────────────────
@pytest.mark.parametrize("url,expected", [
    ("https://listen.tidal.com/album/12345", "https://tidal.com/browse/album/12345"),
    ("https://tidal.com/browse/track/999", "https://tidal.com/browse/track/999"),
    ("https://tidal.com/track/777", "https://tidal.com/browse/track/777"),
    ("https://tidal.com/Album/5", "https://tidal.com/browse/album/5"),   # type lowercased
    ("https://example.com/x", "https://example.com/x"),                  # unchanged
])
def test_to_orpheus_url(url, expected):
    assert t._to_orpheus_url(url) == expected


# ── _tidal_cover ─────────────────────────────────────────────────────────────
def test_tidal_cover():
    assert t._tidal_cover("a-b-c") == "https://resources.tidal.com/images/a/b/c/160x160.jpg"
    assert t._tidal_cover("a-b-c", 320) == "https://resources.tidal.com/images/a/b/c/320x320.jpg"
    assert t._tidal_cover("") == ""


# ── quality-code mapping (regression: front-end codes must not silently fall
#    back to lossless → "asked for AAC 320, got FLAC") ─────────────────────────
@pytest.mark.parametrize("code,orpheus", [
    ("hi_res", "hifi"),
    ("hires", "hifi"),       # front-end variant without underscore — was falling to lossless
    ("lossless", "lossless"),
    ("high", "high"),        # AAC 320
    ("320", "high"),
    ("aac", "high"),
    ("mp3", "high"),         # player.js "High" code — was falling to lossless
    ("low", "low"),
])
def test_quality_orpheus_covers_frontend_codes(code, orpheus):
    assert t._QUALITY_ORPHEUS.get(code) == orpheus


def test_no_frontend_tidal_code_falls_back_to_lossless():
    # every code any front-end emits must be an explicit key (not defaulted)
    frontend_codes = {"hires", "hi_res", "lossless", "high", "low", "320", "mp3", "aac", "atmos"}
    missing = [c for c in frontend_codes if c not in t._QUALITY_ORPHEUS]
    assert not missing, f"unmapped Tidal quality codes (silently → lossless): {missing}"


# ── qualities ────────────────────────────────────────────────────────────────
def test_tidal_qualities():
    qs = TidalEngine().qualities()
    assert len(qs) == 5
    assert all(q["engine"] == "tidal" for q in qs)
    assert {"hi_res", "atmos", "lossless", "high", "low"} == {q["id"] for q in qs}


# ── classify_line ────────────────────────────────────────────────────────────
@pytest.mark.parametrize("line,expected", [
    ("Error: download failed", "error"),
    ("HTTP 401 unauthorized", "error"),     # auth-fail also classifies as error
    ("skipping, already exist", "warn"),
    ("Downloading track file", "success"),
    ("some neutral line", "stdout"),
])
def test_tidal_classify_line(line, expected):
    assert TidalEngine().classify_line(line) == expected


# ── parse_progress ───────────────────────────────────────────────────────────
@pytest.mark.parametrize("line,expected", [
    ("Track 3/12", (2, 12)),    # 0-based: 3 → index 2
    ("50%|####", (50, 100)),
    ("nothing", (4, 9)),        # unchanged
])
def test_tidal_parse_progress(line, expected):
    assert TidalEngine().parse_progress(line, 4, 9) == expected


# ── is_finished ──────────────────────────────────────────────────────────────
def test_tidal_finished_real_completion():
    r = TidalEngine().is_finished("=== Track 123 downloaded ===")
    assert r.success is True and r.tracks_ok == 1


def test_tidal_finished_counts_errors_alongside():
    r = TidalEngine().is_finished("=== Track 1 downloaded ===\nan error occurred")
    assert r.success is True and r.tracks_ok == 1 and r.tracks_err == 1


def test_tidal_finished_not_authed():
    r = TidalEngine().is_finished("TIDAL_NOT_AUTHED")
    assert r.success is False and "TIDAL_NOT_AUTHED" in r.error


def test_tidal_finished_auth_fail():
    r = TidalEngine().is_finished("HTTP 401 unauthorized")
    assert r.success is False and "сесси" in r.error.lower()


def test_tidal_finished_no_marker_is_failure():
    # the anti-phantom-success guard: output but no completion marker → failure
    r = TidalEngine().is_finished("Downloading track file ... (then VPN dropped)")
    assert r.success is False


def test_tidal_finished_empty_output():
    assert TidalEngine().is_finished("").success is False


def test_tidal_finished_region_locked():
    # real cause surfaced (not the misleading "DASH/network" message)
    r = TidalEngine().is_finished("TidalError: Album [25056487] not found. This might be region-locked.")
    assert r.success is False and "регион" in r.error.lower()


def test_tidal_finished_skip_with_rc0():
    r = TidalEngine().is_finished("track already exists", rc=0)
    assert r.success is True and r.tracks_ok == 0
