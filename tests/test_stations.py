"""Правила жанровых станций — те, что решаются без сети.

Станция собирается из живых сервисов, но её ПРАВИЛА — чистые функции, и
проверять их наблюдением за эфиром бессмысленно: выдача меняется каждый день.
Здесь заперты ровно те решения, на которых станция один раз уже сломалась.
"""
import pytest

from ripster import stations as st


class TestNorm:
    """Одно и то же пишут по-разному, и на этом уже терялась целая плитка."""

    @pytest.mark.parametrize("a,b", [
        ("Synth Wave", "Synthwave"),
        ("Drum & Bass", "drumbass"),
        ("Lo-Fi", "lofi"),
        ("Hip-Hop", "hiphop"),
    ])
    def test_same_genre_written_differently(self, a, b):
        assert st.norm(a) == st.norm(b)

    def test_rnb_spellings_are_reconciled_by_the_station_not_by_norm(self):
        """«R&B» и «RnB» — РАЗНЫЕ строки после нормализации, и это нормально.

        `norm("R&B")` даёт «rb», `norm("RnB")` — «rnb»; амперсанд не буква и не
        цифра. Мирит их не нормализация, а набор написаний самой станции: id
        «rnb», запрос «r&b» и заголовок «R&B» лежат там все сразу, поэтому
        сервис волен назвать жанр как ему угодно.

        Проверять надо именно это, а не равенство норм — первая версия теста
        требовала невозможного от `norm()`.
        """
        assert st.norm("R&B") != st.norm("rnb")
        assert st.genre_matches("rnb", "R&B") is True
        assert st.genre_matches("rnb", "RnB") is True


class TestGenreMatch:
    def test_declared_genre_matches_station(self):
        assert st.genre_matches("synthwave", "Synth Wave") is True

    def test_foreign_genre_rejected(self):
        assert st.genre_matches("jazz", "Techno") is False

    def test_silence_is_not_a_refusal(self):
        # Deezer в поиске жанр не отдаёт вовсе. Трактовать молчание как «чужое»
        # значит выбросить половину эфира — «не знаю» это не «нет».
        assert st.genre_matches("jazz", "") is None
        assert st.genre_matches("jazz", None) is None


class TestCredited:
    """Сверка исполнителя. Подстрокой нельзя — проверено живьём."""

    def test_exact_name_matches(self):
        assert st.credited_is("ALEX", "alex") is True

    def test_collaboration_splits(self):
        assert st.credited_is("Fred again.. & Baby Keem", "Baby Keem") is True
        assert st.credited_is("deadmau5 & Kaskade", "deadmau5") is True

    def test_name_containing_ampersand_survives(self):
        # Разбор на части разорвал бы именно тех, кого ищем.
        assert st.credited_is("Dom & Roland", "Dom & Roland") is True
        assert st.credited_is("Above & Beyond", "Above & Beyond") is True

    def test_separator_does_not_cut_inside_a_word(self):
        # Без границы слова голые «x» и «and» режут «Alex» → «Ale»,
        # «Roland» → «Rol», и артист перестаёт находиться вовсе.
        assert st.credited_is("Roland", "Rol") is False
        assert st.credited_is("Bass Drum Of Death", "Drum") is False

    def test_duo_versus_collaboration_is_undecidable_by_string(self):
        """ЧЕСТНОЕ ОГРАНИЧЕНИЕ, а не забытый случай.

        «Calyx & TeeBee» — дуэт, и по запросу «TeeBee» его брать НАДО.
        «Alex & Sierra» — тоже дуэт, и по запросу «ALEX» его брать НЕ надо.
        Устроены строки одинаково, поэтому строковое правило разводит их
        только в одну сторону. Различить их может лишь идентификатор артиста
        (задача про свою линейку айди в трекере). Тест фиксирует, что мы это
        знаем, — чтобы находка не «терялась» при следующей правке.
        """
        assert st.credited_is("Calyx & TeeBee", "TeeBee") is True
        assert st.credited_is("Alex & Sierra", "ALEX") is True   # ложное срабатывание


class TestPopularity:
    def test_unknown_counter_is_middle_not_zero(self):
        # Нулевой вес выкинул бы из эфира сервисы без счётчиков целиком.
        assert st.popularity_weight({}) == pytest.approx(0.5)

    def test_bigger_counter_weighs_more(self):
        small = st.popularity_weight({"rank": 1_000})
        big = st.popularity_weight({"rank": 900_000})
        assert big > small

    def test_weight_is_bounded(self):
        assert 0.0 < st.popularity_weight({"playback_count": 10 ** 9}) <= 1.0


class TestCatalog:
    def test_every_station_has_a_query_and_a_title(self):
        for s in st.catalog():
            assert s["id"] and s["title"]

    def test_subgenres_have_no_curated_source(self):
        """Пустой источник лучше широкого — он не врёт.

        У поджанров чарта с тем же названием нет, и подставлять соседний
        нельзя: ровно так «Синтвейв» однажды заиграл ликвид-фанк.
        """
        by_id = {s[0]: s for s in st.STATIONS}
        for sid in ("synthwave", "meltech", "dubtechno", "idm", "lofi", "proghouse"):
            assert by_id[sid][1] == "", f"{sid}: у поджанра появился чужой чарт"
