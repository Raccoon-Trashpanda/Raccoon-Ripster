"""Правила должны стоять НА ПУТИ ПРОДУКТА, а не рядом с ним.

06.09.2026, находка по собственному же правилу «отличать полезное от
галлюцинации полезности». Четыре захода я строил и замерял разборщик жанров:
область компетенции магазина, корзины остатков, уточнение подвида, свёртка
синонимов. Сорок треков, все зелёные.

А продукт всё это время звал `best_genre`, где стояло «Beatport, а если
молчит — Discogs». Ни одного из правил. То есть каждая ошибка, которую я
считал исправленной, приходила человеку ровно так же: Curtis Mayfield как
«House», Sun Ra как «Nu Disco / Disco».

Модуль, который никто не вызывает, не исправил ничего. Здесь стережётся сам
провод: что вход продукта идёт через разборщик и что все четыре источника
спрошены.
"""
import asyncio

import pytest

from ripster import genre_oracle as go


@pytest.fixture
def sources(monkeypatch):
    """Подменяем сеть: замеряем ПРОВОД, а не источники."""
    asked: list[str] = []

    async def bp(artist, title, token):
        asked.append("beatport")
        return "House"                      # клубный эдит — Beatport не по адресу

    async def dg(artist, title):
        asked.append("discogs")
        return "Soul"

    async def bc(artist, title):
        asked.append("bandcamp")
        return ["afro house", "electronic"]

    async def mb(artist):
        asked.append("musicbrainz")
        return ["soul", "funk", "chicago soul"]

    monkeypatch.setattr(go, "genre_for", bp)
    monkeypatch.setattr(go, "discogs_genre", dg)
    from ripster import genre_sources as gs
    monkeypatch.setattr(gs, "bandcamp_tags", bc)
    monkeypatch.setattr(gs, "musicbrainz_tags", mb)
    go._RESOLVER = None                     # без накопленного доверия прошлых прогонов
    return asked


class TestTheProductPathGoesThroughTheRules:
    def test_curtis_mayfield_is_not_house(self, sources):
        """Тот самый живой случай. До провода продукт отвечал «House»."""
        label, src = asyncio.run(go.best_genre("Curtis Mayfield", "Move On Up", "tok"))
        assert label == "Soul", f"продукт снова отдал {label!r}"
        assert src == "discogs"

    def test_every_source_is_asked(self, sources):
        asyncio.run(go.best_genre("Curtis Mayfield", "Move On Up", "tok"))
        assert set(sources) == {"beatport", "discogs", "bandcamp", "musicbrainz"}

    def test_without_a_token_the_other_three_still_answer(self, sources):
        label, _ = asyncio.run(go.best_genre("Curtis Mayfield", "Move On Up", ""))
        assert label == "Soul"
        assert "beatport" not in sources


class TestSilenceStaysSilence:
    def test_nobody_answered_means_i_do_not_know(self, monkeypatch):
        """Пустой ответ — законный. Придумывать жанр нельзя даже правдоподобный."""
        async def none2(*a):
            return None

        async def none1(*a):
            return []

        monkeypatch.setattr(go, "discogs_genre", none2)
        from ripster import genre_sources as gs
        monkeypatch.setattr(gs, "bandcamp_tags", none1)
        monkeypatch.setattr(gs, "musicbrainz_tags", none1)
        go._RESOLVER = None
        assert asyncio.run(go.best_genre("Кто-то", "Что-то", "")) == (None, "")

    def test_a_source_that_throws_does_not_take_the_answer_with_it(self, monkeypatch):
        """Один упавший источник не должен обнулять ответ остальных: это была
        бы та же подмена факта, только тихая."""
        async def boom(*a):
            raise RuntimeError("сеть")

        async def dg(artist, title):
            return "Soul"

        async def empty(*a):
            return []

        monkeypatch.setattr(go, "discogs_genre", dg)
        from ripster import genre_sources as gs
        monkeypatch.setattr(gs, "bandcamp_tags", boom)
        monkeypatch.setattr(gs, "musicbrainz_tags", empty)
        go._RESOLVER = None
        label, _ = asyncio.run(go.best_genre("Curtis Mayfield", "Move On Up", ""))
        assert label == "Soul"
