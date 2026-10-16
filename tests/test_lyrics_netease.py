"""NetEase YRC: разбор пословного текста и правило «лучший, а не первый»."""
import asyncio

from ripster import lyrics_netease as NE

YRC = (
    '{"t":0,"c":[{"tx":"作词: ","li":"x"}]}\n'
    "[0,1000](0,1000,0)作词 : (1000,0,0)Katy Perry\n"
    "[1000,1000](1000,1000,0)Lyrics by : (1000,0,0)Someone\n"
    "[12000,3000](12000,500,0)Call (12500,500,0)me (13000,700,0)your (13700,800,0)ba(14500,500,0)by\n"
    "[16000,2000](16000,900,0)Oh (16900,1100,0)no\n"
)


def test_parse_yrc_words_and_credits():
    lines = NE.parse_yrc(YRC)
    # Шапка с авторами срезана, текст песни остался.
    assert [l["s"] for l in lines] == [12000, 16000]
    w = lines[0]["words"]
    assert [x["w"] for x in w] == ["Call", "me", "your", "ba", "by"]
    # Пробел NetEase лежит в хвосте слова: «ba|by» — один слог-пара без пробела.
    assert [x["sp"] for x in w] == [True, True, True, False, False]
    assert w[0]["t"] == 12000 and w[0]["d"] == 500


def test_enhanced_lrc_marks_every_word():
    out = NE.words_to_enhanced_lrc(NE.parse_yrc(YRC)).splitlines()[0]
    assert out.startswith("[00:12.00]<00:12.00>Call <00:12.50>me")
    assert "<00:13.70>ba<00:14.50>by" in out


def test_credit_line_inside_song_is_kept():
    # «Lyrics:» посреди песни — слова, а не титры.
    yrc = ("[1000,1000](1000,1000,0)Hello\n"
           "[3000,1000](3000,1000,0)Lyrics: (3500,500,0)la la\n")
    assert len(NE.parse_yrc(yrc)) == 2


def test_from_netease_never_raises(monkeypatch):
    def boom():
        raise RuntimeError("net down")
    monkeypatch.setattr(NE, "_client", boom)
    assert asyncio.run(NE.from_netease("a", "b", 100)) == {}


def test_flac_embedded_lyrics_are_readable(tmp_path):
    """У FLAC `tags.get("©lyr")` бросает ValueError — общий except прятал LYRICS."""
    import shutil, subprocess
    import pytest
    if shutil.which("ffmpeg") is None:
        pytest.skip("нет ffmpeg")
    p = tmp_path / "a.flac"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=1", "-c:a", "flac",
                    "-metadata", "LYRICS=[00:01.00]hello", str(p)], check=True)
    from ripster.routes import discovery as D
    from ripster.routes import library as L
    orig = L._safe_resolve
    L._safe_resolve = lambda x: __import__("pathlib").Path(x)
    try:
        got = D._lyrics_from_file(str(p))
    finally:
        L._safe_resolve = orig
    assert got.get("synced", "").startswith("[00:01.00]hello")
