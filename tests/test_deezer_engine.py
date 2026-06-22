"""Deezer engine (deemix). `is_finished` must not trust deemix's "All done!" —
a free/expired ARL prints it while writing zero files, so the 0-track and
bitrate-gate failure paths are pinned. build_cmd is skipped: it writes the real
deemix config/ARL as a side effect."""
import pytest

from ripster.engines.deezer import DeezerEngine


@pytest.fixture
def dz():
    return DeezerEngine()


def test_deezer_qualities(dz):
    qs = dz.qualities()
    assert len(qs) == 3
    assert all(q["engine"] == "deezer" for q in qs)
    assert {"flac", "mp3_320", "mp3_128"} == {q["id"] for q in qs}


@pytest.mark.parametrize("line,expected", [
    ("An error occurred", "error"),
    ("Invalid ARL token", "error"),
    ("Completed download of track", "success"),
    ("Finished downloading", "success"),
    ("All done!", "success"),
    ("neutral line", "stdout"),
])
def test_deezer_classify_line(dz, line, expected):
    assert dz.classify_line(line) == expected


@pytest.mark.parametrize("line,expected", [
    ("Download at 86%", (86, 100)),
    ("3/12", (3, 12)),
    ("nothing", (5, 9)),
])
def test_deezer_parse_progress(dz, line, expected):
    assert dz.parse_progress(line, 5, 9) == expected


def test_deezer_finished_success(dz):
    r = dz.is_finished("Completed download of Song\nAll done!")
    assert r.success is True and r.tracks_ok == 1


def test_deezer_finished_all_done_but_zero_tracks(dz):
    # anti-phantom: "All done!" with nothing saved is a failure, not success
    r = dz.is_finished("All done!")
    assert r.success is False and "ни один трек" in r.error


def test_deezer_finished_bitrate_blocked(dz):
    r = dz.is_finished("Can't stream the track at the desired bitrate")
    assert r.success is False and "free" in r.error.lower()


def test_deezer_finished_bad_arl(dz):
    r = dz.is_finished("invalid arl provided")
    assert r.success is False and "ARL" in r.error


def test_deezer_finished_unexpected_end(dz):
    r = dz.is_finished("some unrelated output")
    assert r.success is False and "неожиданное" in r.error
