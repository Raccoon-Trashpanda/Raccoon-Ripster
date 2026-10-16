"""Shared cross-service error classifier — region-lock vs phantom/removed link,
so the bot/UI tells the user WHY a download failed instead of a generic 'error'."""
import pytest

from ripster.engines.errors import classify_download_error


@pytest.mark.parametrize("log,category", [
    ("TidalError: Album [25056487] not found. This might be region-locked.", "region"),
    ("This track is region-locked", "region"),
    ("not available in your country", "region"),
    ("geo-blocked content", "region"),
    ("track no longer available", "gone"),
    ("HTTP 404 not found", "gone"),
    ("the release has been removed", "gone"),
    ("does not exist on the server", "gone"),
])
def test_classify_matches(log, category):
    res = classify_download_error(log)
    assert res is not None and res[0] == category
    assert isinstance(res[1], str) and res[1]      # a non-empty human message


@pytest.mark.parametrize("log", [
    "Downloading track file",
    "everything is fine here",
    "",
])
def test_classify_no_match(log):
    assert classify_download_error(log) is None


def test_region_wins_over_gone():
    # "not found ... region-locked" → the actionable cause is the region wall
    res = classify_download_error("Album not found. This might be region-locked.")
    assert res[0] == "region"
