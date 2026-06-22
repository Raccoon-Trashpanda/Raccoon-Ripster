"""Coder tab: the route (`ripster_coder`) is ffmpeg-transcode orchestration; its
pure helper is `_parse_folder_name`. The conversion engine is `mixcue` (clean
names / cue helpers already covered in test_helpers); here we add its template
filename formatter `_fmt_name`."""
from ripster.routes.ripster_coder import _parse_folder_name
from ripster.mixcue import _fmt_name


# ── _parse_folder_name ───────────────────────────────────────────────────────
def test_parse_folder_name():
    assert _parse_folder_name("Artist - Album") == ("Artist", "Album")
    assert _parse_folder_name("JustName") == ("", "JustName")
    assert _parse_folder_name("A - B - C") == ("A", "B - C")   # split once


# ── mixcue._fmt_name ─────────────────────────────────────────────────────────
def test_fmt_name_zero_pads_track():
    out = _fmt_name("{tracknumber}. {artist} - {title}",
                    {"track": "3/12", "artist": "DJ", "title": "Song", "album": "Alb"})
    assert out == "03. DJ - Song"


def test_fmt_name_non_numeric_track():
    assert _fmt_name("{track}-{title}", {"track": "A", "title": "X"}) == "A-X"


def test_fmt_name_empty_render_falls_back():
    # empty render → mixcue._sanitize("") itself returns "mix" (its own fallback),
    # so _fmt_name yields "mix" rather than reaching the title fallback.
    assert _fmt_name("{artist}", {"artist": "", "title": "Fallback"}) == "mix"
