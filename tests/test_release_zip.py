import sys
import zipfile
from pathlib import Path

from ripster import release_zip as RZ
from ripster import lyrics_embed as LE

for _s in (sys.stdout, sys.stderr):
    try: _s.reconfigure(errors="replace")
    except Exception: pass


def test_archive_name_and_pack(tmp_path, monkeypatch):
    rel = tmp_path / "Artist" / "Album"
    rel.mkdir(parents=True)
    a = rel / "01. A.flac"; a.write_bytes(b"x")
    (rel / "01. A.lrc").write_text("[00:01.00]hi")
    (rel / "cover.jpg").write_bytes(b"j")
    (rel / "other.lrc").write_text("чужой")
    monkeypatch.setattr(RZ, "file_quality", lambda p: "FLAC 24-96")
    monkeypatch.setattr(RZ, "_tags", lambda p: ("Daft: Punk", "Discovery?"))
    z = RZ.pack_release([a], rel, None, "Album")
    assert z.name == "Daft_ Punk - Discovery_ [FLAC 24-96].zip"
    assert z.parent == rel.parent
    names = set(zipfile.ZipFile(z).namelist())
    assert names == {"01. A.flac", "01. A.lrc", "cover.jpg"}
    z2 = RZ.pack_release([a], rel, None, "Album")
    assert z2 != z and "(2)" in z2.name


def test_word_lyrics_tags_are_line_level():
    got = {"words": [{"s": 1000, "words": [{"t": 1000, "w": "hi", "sp": True},
                                           {"t": 1500, "w": "there"}]}]}
    comp = LE.compose(got)
    assert comp["kind"] == "word"
    assert "<" in comp["lrc"]
    assert "<" not in comp["lrc_line"] and "hi" in comp["lrc_line"]


def test_out_dir_keeps_release_folder_clean(tmp_path, monkeypatch):
    rel = tmp_path / "Album"; rel.mkdir()
    a = rel / "01. A.flac"; a.write_bytes(b"x")
    out = tmp_path / "tmpzip"
    monkeypatch.setattr(RZ, "file_quality", lambda p: "MP3 320")
    monkeypatch.setattr(RZ, "_tags", lambda p: ("X", "Y"))
    z = RZ.pack_release([a], rel, None, "Album", out)
    assert z.parent == out and z.name == "X - Y [MP3 320].zip"
    assert not list(tmp_path.glob("*.zip"))
