"""Save-path / quality-folder resolution. This module is the source of the
notorious nested-folder bug (`ALAC (Lossless)\\ALAC…\\ALAC…`), so the Apple
no-double-subfolder behaviour is pinned explicitly below."""
import os

import pytest

from ripster import service_config as sc


# ── _quality_folder_name ─────────────────────────────────────────────────────
def test_quality_folder_basic():
    assert sc._quality_folder_name("apple", "alac") == "ALAC (Lossless)"
    assert sc._quality_folder_name("deezer", "flac") == "FLAC"
    assert sc._quality_folder_name("tidal", "lossless") == "FLAC CD"


def test_quality_folder_high_is_per_service():
    assert sc._quality_folder_name("tidal", "high") == "AAC 320"
    assert sc._quality_folder_name("spotify", "high") == "OGG 160"
    assert sc._quality_folder_name("beatport", "high") == "AAC 256"
    assert sc._quality_folder_name("apple", "high") == "MP3 320"


def test_quality_folder_beatport_hifi_is_flac():
    # beatport hifi is FLAC, not the OGG that "hifi" means for Spotify
    assert sc._quality_folder_name("beatport", "hifi") == "FLAC"
    assert sc._quality_folder_name("spotify", "hifi") == "OGG 320"


def test_quality_folder_unknown_falls_back():
    assert sc._quality_folder_name("x", "weirdq") == "weirdq"
    assert sc._quality_folder_name("x", "") == "other"


# ── get_save_path — UNIFIED <base>/<service>/<quality> ───────────────────────
@pytest.mark.parametrize("svc,quality,expected_tail", [
    ("deezer", "flac", ("deezer", "FLAC")),
    ("beatport", "high", ("beatport", "AAC 256")),
    ("apple", "alac", ("apple", "ALAC (Lossless)")),
    ("tidal", "lossless", ("tidal", "FLAC CD")),
    ("qobuz", "27", ("qobuz", "FLAC HiRes 24-192")),
])
def test_save_path_unified_layout(svc, quality, expected_tail):
    got = sc.get_save_path({"save-path": "D"}, svc, quality)
    assert got == os.path.join("D", *expected_tail)


def test_save_path_no_quality_is_service_only():
    assert sc.get_save_path({"save-path": "D"}, "deezer") == os.path.join("D", "deezer")


def test_save_path_default_base():
    assert sc.get_save_path({}, "qobuz", "27") == os.path.join("downloads", "qobuz", "FLAC HiRes 24-192")


def test_save_path_ignores_legacy_per_service_keys():
    # per-service paths are GONE — a stale key in config must NOT override the
    # unified <base>/<service>/<quality> layout.
    cfg = {"save-path": "D", "deezer-save-path": "OLD", "alac-save-folder": "OLD2"}
    assert sc.get_save_path(cfg, "deezer", "flac") == os.path.join("D", "deezer", "FLAC")
    assert sc.get_save_path(cfg, "apple", "alac") == os.path.join("D", "apple", "ALAC (Lossless)")


def test_save_path_empty_service_defaults_apple():
    assert sc.get_save_path({"save-path": "D"}, "", "alac") == os.path.join("D", "apple", "ALAC (Lossless)")
