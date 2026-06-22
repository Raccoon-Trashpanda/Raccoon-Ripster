"""SoundCloud engine (Lucida/Node runner). `is_finished` parses the runner's
`Summary: N Success, M Failed` line and special-cases FairPlay-encrypted HLS
(ffmpeg can't decrypt) — both pinned."""
import pytest

from ripster.engines.soundcloud import SoundcloudEngine


@pytest.fixture
def sc():
    return SoundcloudEngine()


def test_sc_qualities(sc):
    qs = sc.qualities()
    assert len(qs) == 2
    assert all(q["engine"] == "soundcloud" for q in qs)
    assert {"mp3", "hq"} == {q["id"] for q in qs}


@pytest.mark.parametrize("line,expected", [
    ("Failed [1] track", "error"),
    ("error: boom", "error"),
    ("Success [2] track", "success"),
    ("Summary: 3 Success, 0 Failed", "success"),
    ("Found 5 tracks", "info"),
    ("Queued 5 tracks", "info"),
    ("downloading file", "stdout"),
    ("neutral", "stdout"),
])
def test_sc_classify_line(sc, line, expected):
    assert sc.classify_line(line) == expected


@pytest.mark.parametrize("line,expected", [
    ("Queued 5 tracks", (0, 5)),
    ("[3/12]", (3, 12)),
    ("nothing", (2, 4)),
])
def test_sc_parse_progress(sc, line, expected):
    assert sc.parse_progress(line, 2, 4) == expected


def test_sc_extract_save_dir(sc):
    log = "Summary: 1 Success, 0 Failed\nOutput dir: C:/Music/Mix\n"
    assert sc.extract_save_dir(log) == "C:/Music/Mix"


def test_sc_finished_all_ok(sc):
    r = sc.is_finished("Summary: 5 Success, 0 Failed")
    assert r.success is True and r.tracks_ok == 5


def test_sc_finished_some_failed(sc):
    r = sc.is_finished("Summary: 2 Success, 3 Failed")
    assert r.success is False and r.tracks_ok == 2 and r.tracks_err == 3


def test_sc_finished_drm_fairplay(sc):
    log = ("Summary: 0 Success, 4 Failed\n"
           "Invalid data found when processing input (cbcs)")
    r = sc.is_finished(log)
    assert r.success is False and "FairPlay" in r.error


def test_sc_finished_rc0_no_summary(sc):
    assert sc.is_finished("", rc=0).success is True


def test_sc_finished_no_marker(sc):
    r = sc.is_finished("garbage", rc=1)
    assert r.success is False and "нет маркера" in r.error
