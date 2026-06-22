"""Spectrum tab (`spectrogram` route): duration formatting + the lossless/lossy
verdict (codec + extension heuristics). Plus the Setup package's `_gamdl_flag`
guard (only pass a CLI flag this gamdl build actually supports)."""
import pytest

from ripster.routes import spectrogram as sg
from ripster import setup as st


# ── _format_duration ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("sec,expected", [
    (0, "0:00"),
    (65, "1:05"),
    (3725, "1:02:05"),
])
def test_format_duration(sec, expected):
    assert sg._format_duration(sec) == expected


# ── _verdict ─────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("codec,path,verdict", [
    ("FLAC (Free Lossless Audio Codec)", "x.flac", "lossless"),
    ("mp3", "x.mp3", "lossy"),
    ("alac", "track.m4a", "lossless"),
    ("aac", "track.m4a", "lossy"),
    ("", "rip.wav", "lossless"),       # ext-based: wav is lossless
    ("weirdcodec", "file.xyz", "suspicious"),
])
def test_verdict(codec, path, verdict):
    key, text = sg._verdict({"codec": codec}, path)
    assert key == verdict
    assert isinstance(text, str) and text


# ── setup._gamdl_flag ────────────────────────────────────────────────────────
def test_gamdl_flag_known(monkeypatch):
    monkeypatch.setattr(st, "_detect_gamdl_flags", lambda: {"codec-song", "save-cover"})
    assert st._gamdl_flag("codec-song", "alac") == ["--codec-song", "alac"]


def test_gamdl_flag_unknown_dropped(monkeypatch):
    monkeypatch.setattr(st, "_detect_gamdl_flags", lambda: {"codec-song"})
    assert st._gamdl_flag("does-not-exist") == []


def test_gamdl_flag_empty_detection_is_permissive(monkeypatch):
    # detection failed (empty set) → don't block, pass the flag through
    monkeypatch.setattr(st, "_detect_gamdl_flags", lambda: set())
    assert st._gamdl_flag("anything", "v") == ["--anything", "v"]
