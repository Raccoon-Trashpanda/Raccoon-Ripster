"""Search (discovery) route: pure cover-URL builders. Most of discovery is async
network search (covered indirectly); the unit-testable surface is the cover
helpers. NOTE: `_ym_cover`/`_tidal_cover` functionally duplicate the same-named
engine helpers (differ only in default size / docstring) — flagged for the final
cross-cutting dedup pass, not merged here (cross-module coupling, small gain)."""
from ripster.routes import discovery as d


def test_ym_cover_default_400():
    assert d._ym_cover("avatars.yandex/x%%.jpg") == "https://avatars.yandex/x400x400.jpg"
    assert d._ym_cover("http://x%%.jpg", "200x200") == "http://x200x200.jpg"
    assert d._ym_cover("") == ""


def test_tidal_cover():
    assert d._tidal_cover("a-b-c") == "https://resources.tidal.com/images/a/b/c/160x160.jpg"
    assert d._tidal_cover("a-b-c", 320) == "https://resources.tidal.com/images/a/b/c/320x320.jpg"
    assert d._tidal_cover("") == ""


def test_sp_cover_picks_medium():
    imgs = [{"url": "big-640"}, {"url": "med-300"}, {"url": "small-64"}]
    assert d._sp_cover(imgs) == "med-300"          # index 1 (medium) to cut traffic


def test_sp_cover_single_image():
    assert d._sp_cover([{"url": "only"}]) == "only"


def test_sp_cover_empty():
    assert d._sp_cover([]) == ""
