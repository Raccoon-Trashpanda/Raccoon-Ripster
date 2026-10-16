"""SoundCloud Widevine engine (pywidevine L3 CDM). `is_finished` distinguishes
missing/revoked .wvd device from genuine download failures so the user gets an
actionable message."""
import pytest

from ripster.engines.sc_widevine import SoundcloudWidevineEngine


@pytest.fixture
def cdm():
    return SoundcloudWidevineEngine()


def test_scw_qualities(cdm):
    qs = cdm.qualities()
    assert len(qs) == 1
    assert qs[0]["engine"] == "sc_widevine"
    assert qs[0]["id"] == "hq"


@pytest.mark.parametrize("line,expected", [
    ("Failed [1] track", "error"),
    ("error: boom", "error"),
    ("Success [2] track", "success"),
    ("Summary: 3 Success, 0 Failed", "success"),
    ("Found 3 tracks", "info"),
    ("Queued 5 tracks", "info"),
    ("downloading segment 3", "stdout"),
    ("neutral", "stdout"),
])
def test_scw_classify_line(cdm, line, expected):
    assert cdm.classify_line(line) == expected


@pytest.mark.parametrize("line,expected", [
    ("Queued 5 tracks", (0, 5)),
    ("[3/12]", (3, 12)),
    ("nothing", (1, 1)),
])
def test_scw_parse_progress(cdm, line, expected):
    assert cdm.parse_progress(line, 1, 1) == expected


def test_scw_finished_no_wvd(cdm):
    r = cdm.is_finished("device.wvd not found")
    assert r.success is False and "device.wvd" in r.error


def test_scw_finished_revoked(cdm):
    r = cdm.is_finished("device may be revoked by Google")
    assert r.success is False and "отозван" in r.error


def test_scw_finished_summary_ok(cdm):
    r = cdm.is_finished("Summary: 3 Success, 0 Failed")
    assert r.success is True and r.tracks_ok == 3


def test_scw_finished_summary_failed(cdm):
    r = cdm.is_finished("Summary: 1 Success, 2 Failed")
    assert r.success is False and r.tracks_err == 2


def test_scw_finished_rc0_no_summary(cdm):
    assert cdm.is_finished("", rc=0).success is True


def test_scw_finished_no_marker(cdm):
    r = cdm.is_finished("junk", rc=1)
    assert r.success is False and "нет маркера" in r.error
