"""BBC route pure helpers: image URLs, timecode/duration parsing, name sanitize,
match tokenization, the MixesDB relevance scorer (episode-number gating — what
stops a stranger's tracklist from matching), and CUE generation."""
from ripster.routes import bbc


# ── _img ─────────────────────────────────────────────────────────────────────
def test_img():
    assert bbc._img("http://x/{recipe}.jpg") == "http://x/320x320.jpg"
    assert bbc._img("http://x/{recipe}.jpg", 640) == "http://x/640x640.jpg"
    assert bbc._img("") == ""


# ── _parse_timecodes ─────────────────────────────────────────────────────────
def test_parse_timecodes():
    out = bbc._parse_timecodes("00:00 Intro\n03:45 Track Two\n1:02:30 Track Three")
    assert [t["seconds"] for t in out] == [0, 225, 3750]
    assert out[0]["title"] == "Intro"
    assert out[2]["title"] == "Track Three"


# ── _parse_dur ───────────────────────────────────────────────────────────────
def test_parse_dur():
    assert bbc._parse_dur(7200) == 7200
    assert bbc._parse_dur({"value": 3600, "label": "1h"}) == 3600
    assert bbc._parse_dur(None) == 0
    assert bbc._parse_dur({}) == 0


# ── _safe ────────────────────────────────────────────────────────────────────
def test_safe():
    assert bbc._safe("Hello/World:Mix") == "Hello_World_Mix"
    assert bbc._safe(' a*b?c "x" ') == "a_b_c _x_"   # only special chars → _, spaces kept


# ── _match_toks / _match_nums ────────────────────────────────────────────────
def test_match_toks():
    toks = bbc._match_toks("Anjunadeep 500 Live")
    assert "anjunadeep" in toks and "500" in toks   # lowercased alnum tokens
    assert "a" not in bbc._match_toks("a bb")        # single-char dropped


def test_match_nums():
    assert bbc._match_nums("Edition 500") == {"500"}
    assert bbc._match_nums("a1b22c") == {"1", "22"}


# ── _score_mixesdb_hit (episode-number gating) ───────────────────────────────
def test_score_right_episode_beats_wrong():
    q_title, q_artist = "Anjunadeep Edition 500", ""
    right = bbc._score_mixesdb_hit(q_title, q_artist, {"artist": "", "show": "Anjunadeep Edition 500"})
    wrong = bbc._score_mixesdb_hit(q_title, q_artist, {"artist": "", "show": "Anjunadeep Edition 499"})
    assert right > 0.5 > wrong       # right confident, wrong rejected by number gate
    assert right > wrong


# ── _sec_to_ts ───────────────────────────────────────────────────────────────
def test_sec_to_ts():
    assert bbc._sec_to_ts(0) == "0:00"
    assert bbc._sec_to_ts(225) == "3:45"
    assert bbc._sec_to_ts(3750) == "1:02:30"


# ── _build_cue ───────────────────────────────────────────────────────────────
def test_build_cue():
    cue = bbc._build_cue("My Mix", "DJ", [
        {"offset": 0, "title": "T1", "artist": "A1"},
        {"offset": 225, "title": "T2"},   # no artist → defaults to mix artist
    ])
    assert 'TITLE "My Mix"' in cue
    assert 'PERFORMER "DJ"' in cue
    assert "TRACK 01 AUDIO" in cue and "TRACK 02 AUDIO" in cue
    assert 'PERFORMER "A1"' in cue        # per-track artist
    assert "INDEX 01 00:00:00" in cue     # track 1 at 0s
    assert "INDEX 01 03:45:00" in cue     # track 2 at 225s
