"""Вшивание текстов: теги всех форматов, пословный/построчный выбор, произношение."""
import asyncio
import shutil
import subprocess

import mutagen
import pytest

from ripster import lyrics_embed as LE

WORDS = [{"s": 1000, "e": 2000, "words": [
    {"t": 1000, "d": 400, "w": "Call", "sp": True},
    {"t": 1400, "d": 600, "w": "me", "sp": False}]}]
GOT = {"synced": "[00:01.00]Call me", "plain": "Call me", "words": WORDS,
       "roma": "[00:01.10]koru mi", "source": "NetEase (word)"}

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="нет ffmpeg")


def _make(tmp_path, ext, title="Song"):
    p = tmp_path / f"a{ext}"
    codec = {".flac": ["-c:a", "flac"], ".m4a": ["-c:a", "aac"], ".mp3": ["-c:a", "libmp3lame"]}[ext]
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=1", *codec,
                    "-metadata", f"title={title}", "-metadata", "artist=Art", str(p)],
                   check=True)
    return p


def test_compose_word_vs_line():
    w = LE.compose(GOT)
    assert w["kind"] == "word" and w["lrc"].startswith("[00:01.00]<00:01.00>Call <00:01.40>me")
    l = LE.compose(GOT, words_ok=False)
    assert l["kind"] == "line" and "<" not in l["lrc"]
    assert LE.compose({"plain": "x"})["kind"] == "plain"
    assert LE.compose({})["kind"] == ""


def test_pronunciation_goes_after_original_line():
    out = LE.compose(GOT, pronunciation=True)["lrc"].splitlines()
    assert len(out) == 2 and "Call" in out[0] and out[1].endswith("koru mi")
    # без запроса произношения его нет
    assert "koru" not in LE.compose(GOT)["lrc"]


@needs_ffmpeg
@pytest.mark.parametrize("ext", [".flac", ".m4a", ".mp3"])
def test_embed_roundtrip_keeps_audio_untouched(tmp_path, ext):
    p = _make(tmp_path, ext)
    before = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", str(p), "-f", "md5", "-"],
                            capture_output=True, text=True).stdout
    assert LE.embed_into_file(p, LE.compose(GOT)) is True
    after = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", str(p), "-f", "md5", "-"],
                           capture_output=True, text=True).stdout
    assert before == after, "звук изменился — теги не должны его трогать"
    assert "Call" in LE._existing_text(p) or LE._existing_text(p) == "[sylt]"


@needs_ffmpeg
def test_embed_for_files_writes_sidecar_and_never_overwrites(tmp_path):
    p = _make(tmp_path, ".flac")
    calls = []

    async def ladder(**kw):
        calls.append(kw)
        return GOT

    st = asyncio.run(LE.embed_for_files([p], ladder, want_file=True))
    assert st["embedded"] == 1 and st["word"] == 1 and calls[0]["track"] == "Song"
    assert p.with_suffix(".lrc").read_text("utf-8").startswith("[00:01.00]")
    # второй прогон: синхра уже есть → лестницу не зовём, файл не трогаем
    st2 = asyncio.run(LE.embed_for_files([p], ladder, want_file=True))
    assert st2["kept"] == 1 and len(calls) == 1


@needs_ffmpeg
def test_failing_ladder_is_counted_not_raised(tmp_path):
    p = _make(tmp_path, ".mp3")

    async def boom(**kw):
        raise RuntimeError("net")

    st = asyncio.run(LE.embed_for_files([p], boom, log=lambda s: None))
    assert st["failed"] == 1 and st["embedded"] == 0
