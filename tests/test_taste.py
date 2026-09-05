"""Вкус владельца: два источника разной природы в одном профиле.

Владелец: «чтобы раскопки учитывались и чтобы учитывались подписки споти».

Проверяется главное, что здесь легко испортить: подписка и прослушивание — НЕ
одно и то же. Раскопки дают измеренный вес (замер 05.09.2026: у верхнего
артиста 88, у нижнего 15), подписка — двоичный сигнал, которых 5761 штука.
Уравнять их значит утопить реально слушаемое в тысячах «мне интересен».
"""
import json

import pytest

from ripster import taste


def _digs(*pairs):
    return {
        "artists": [{"name": n, "score": s, "genre": g} for n, s, g in pairs],
        "genres": [{"genre": "Electronic", "score": 100.0, "share": 60.0}],
    }


class TestTwoSourcesStayDistinct:
    def test_a_measured_weight_outranks_a_subscription(self):
        """Ради этого всё и разделено: тысячи подписок не должны обогнать
        того, кого человек правда слушает."""
        p = taste.merge(_digs(("Massive Attack", 78.0, "Electronic")), ["Кто-то", "Ещё кто-то"])
        assert p["artists"][0]["name"] == "Massive Attack"
        assert p["artists"][1]["weight"] == taste.FOLLOW_WEIGHT

    def test_listening_and_following_is_stronger_than_either(self):
        p = taste.merge(_digs(("Bonobo", 32.4, "Electronic")), ["Bonobo"])
        row = next(a for a in p["artists"] if a["name"] == "Bonobo")
        assert row["weight"] == pytest.approx(32.4 + taste.FOLLOW_WEIGHT)
        assert row["source"] == "digs+follow"

    def test_every_row_says_where_it_came_from(self):
        """Цифра без происхождения непроверяема: на той стороне должно быть
        видно, измерение это или подписка."""
        p = taste.merge(_digs(("A", 10.0, "Pop")), ["B"])
        assert {a["source"] for a in p["artists"]} == {"digs", "follow"}

    def test_matching_ignores_case(self):
        p = taste.merge(_digs(("Massive Attack", 78.0, None)), ["massive attack"])
        assert p["counts"]["total"] == 1
        assert p["artists"][0]["source"] == "digs+follow"


class TestHonestGaps:
    def test_a_missing_genre_is_null_not_empty_string(self):
        """«Жанр неизвестен» и «жанр пустой» на той стороне ведут себя
        по-разному: null честно означает «не знаю»."""
        p = taste.merge(_digs(("A", 1.0, "")), [])
        assert p["artists"][0]["genre"] is None

    def test_one_source_failing_does_not_cancel_the_other(self):
        assert taste.merge(None, ["A", "B"])["counts"]["follows"] == 2
        assert taste.merge(_digs(("A", 5.0, "Pop")), [])["counts"]["digs"] == 1

    def test_an_empty_profile_still_explains_itself(self):
        p = taste.merge(None, [])
        assert p["artists"] == [] and p["counts"]["total"] == 0

    def test_radio_shows_are_not_artists(self):
        """Подписка на подкаст лейбла — жанровый сигнал, но не исполнитель;
        Раскопки помечают такие строки отдельно."""
        d = {"artists": [{"name": "Anjunadeep Edition", "score": 40.0, "is_show": True},
                         {"name": "Lane 8", "score": 20.0}], "genres": []}
        names = [a["name"] for a in taste.merge(d, [])["artists"]]
        assert names == ["Lane 8"]


class TestVolume:
    def test_follows_are_capped_by_size_not_by_a_fake_ranking(self):
        """Замер: 5761 подписка, это ~120 КБ на запрос. Режем по объёму и
        честно говорим сколько — а не делаем вид, что выбрали лучших."""
        p = taste.merge(None, [f"Артист {i}" for i in range(5000)], follow_limit=10)
        assert p["counts"]["follows"] == 10

    def test_the_cap_never_drops_a_measured_artist(self):
        """Потолок — про подписки. Того, кого человек слушает, он не касается."""
        d = _digs(*[(f"Слушаю {i}", 50.0 - i, "Pop") for i in range(30)])
        p = taste.merge(d, [f"Подписка {i}" for i in range(100)], follow_limit=1)
        assert p["counts"]["digs"] == 30


class TestReadingTheFollowFile:
    def test_a_missing_file_is_not_a_crash(self, tmp_path):
        assert taste._spotify_follows(tmp_path) == []

    def test_a_broken_file_is_not_a_crash(self, tmp_path):
        (tmp_path / "spotify_artist_state.json").write_text("{не json", encoding="utf-8")
        assert taste._spotify_follows(tmp_path) == []

    def test_names_are_read_and_blanks_skipped(self, tmp_path):
        (tmp_path / "spotify_artist_state.json").write_text(json.dumps({
            "followed": {"artists": [{"id": "1", "name": "Lane 8"},
                                     {"id": "2", "name": "  "},
                                     {"id": "3"}]}
        }), encoding="utf-8")
        assert taste._spotify_follows(tmp_path) == ["Lane 8"]
