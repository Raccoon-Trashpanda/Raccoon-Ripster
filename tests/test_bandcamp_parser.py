"""Регрессия на сохранённой странице Bandcamp: разбор без сети.

Фикстура — живая страница предзаказа «Between Two Worlds. SEMANTICA 199»
(Oscar Mulero / Semantica Records, выход 25.09.2026), урезанная до того, что
читает парсер: `<title>`, JSON-LD и `data-tralbum`. Заголовок тестовый потому,
что именно этот релиз радар не показал вовсе — см. шапку ripster/bandcamp.py.

Проверяем поля, на которых держится карточка радара, и признак предзаказа:
он есть ТОЛЬКО в data-tralbum, и если парсер его теряет, будущий релиз
уезжает из ленты как «уже вышедший».
"""
from pathlib import Path

import pytest

from ripster import bandcamp as bc

FIX = Path(__file__).parent / "fixtures"
PAGE = (FIX / "bandcamp_semantica_199.html").read_text(encoding="utf-8", errors="replace")
GRID = (FIX / "bandcamp_semantica_music.html").read_text(encoding="utf-8", errors="replace")


def test_parse_album_semantica_199():
    rec = bc.parse_album(PAGE, "https://semanticarecords.bandcamp.com/album/"
                              "between-two-worlds-semantica-199")
    assert rec, "страница релиза не разобралась"
    assert rec["title"] == "Between Two Worlds. SEMANTICA 199"
    assert rec["artist"] == "Oscar Mulero"
    # Издатель из JSON-LD — это ЛЕЙБЛ: ровно то, чем Bandcamp закрывает отказ
    # сверки по каталогам, где релиза ещё нет.
    assert rec["label"] == "Semantica Records"
    assert rec["date"] == "2026-09-25"
    assert rec["date_raw"].startswith("25 Sep 2026")
    assert rec["preorder"] is True
    assert bc.is_future(rec, today="2026-09-24") is True
    assert rec["url"].endswith("/between-two-worlds-semantica-199")
    assert rec["cover"].startswith("https://")
    assert rec["track_count"] == len(rec["tracklist"]) == 11
    assert rec["tracklist"][0]["title"] == "Footprints Behind Fear"
    assert any("Vinyl" in f for f in rec["formats"]), "формат издания потерян"


def test_parse_album_survives_missing_tralbum():
    """Без data-tralbum карточка всё равно собирается из JSON-LD, но
    предзаказа там нет — и выдумывать его нельзя."""
    import re
    page = re.sub(r'data-tralbum="[^"]*"', '', PAGE)
    rec = bc.parse_album(page)
    assert rec and rec["title"] == "Between Two Worlds. SEMANTICA 199"
    assert rec["preorder"] is False
    assert rec["date"] == "2026-09-25"


def test_parse_album_not_a_release_page():
    assert bc.parse_album("<html><body>нет ни JSON-LD, ни tralbum</body></html>") is None
    assert bc.parse_album("") is None


def test_parse_music_grid_lists_album_pages():
    items = bc.parse_music_grid(GRID, "https://semanticarecords.bandcamp.com")
    albums = [i for i in items if i["kind"] == "album"]
    assert albums, "сетка релизов не разобралась"
    one = next((i for i in albums if "semantica-199" in i["url"]), None)
    assert one, "предзаказ потерян из сетки"
    assert one["title"] == "Between Two Worlds. SEMANTICA 199"
    assert one["artist"] == "Oscar Mulero"
    assert one["url"].startswith("https://semanticarecords.bandcamp.com/album/")


def test_parse_bc_date_refuses_invention():
    assert bc.parse_bc_date("25 Sep 2026 00:00:00 GMT") == "2026-09-25"
    assert bc.parse_bc_date("2026-09-25") == "2026-09-25"
    # Голый год и «осень 2026» — не дата выхода. Подставлять её нельзя.
    assert bc.parse_bc_date("2026") == ""
    assert bc.parse_bc_date("Sept 2026") == ""
    assert bc.parse_bc_date("") == ""


@pytest.mark.parametrize("raw,want", [
    ("25 Sep 2026 00:00:00 GMT", "2026-09-25"),
    ("31 Dec 2026 23:00:00 GMT", "2026-12-31"),
])
def test_parse_bc_date_table(raw, want):
    assert bc.parse_bc_date(raw) == want
