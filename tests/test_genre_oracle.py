"""Справочник жанров: у источника есть область компетенции.

Владелец: «у эпла, споти жанров нет… есть только у битпорта». Замер
05.09.2026 это подтвердил — Apple на половину запросов отвечает «Dance, Music»,
где «Music» это корневая категория каталога.

Но замер же показал две вещи, которые легко пропустить и которые здесь и
стерегутся:

  · спрашивать надо про ТРЕК, а не про артиста: по каталогу артиста Anyma
    выходит «Psy-Trance» (тёзка), а по трекам — Melodic House & Techno;
  · за пределами своей области Beatport не «менее точен», а неверен:
    Massive Attack у него «Pop», Aretha Franklin «R&B». Такой ответ надо
    ОТБРАСЫВАТЬ, а не понижать в весе.
"""
from ripster import genre_oracle as go


class TestDomainOfCompetence:
    def test_shop_buckets_are_rejected(self):
        """«Pop» у Massive Attack и «R&B» у Aretha Franklin — это полки
        магазина, а не жанры этих артистов."""
        for g in ("Pop", "Rock", "R&B", "Dance / Pop", "Country", "Latin"):
            assert go.is_catch_all(g), g

    def test_realGenresPass(self):
        for g in ("Melodic House & Techno", "Techno (Peak Time / Driving)",
                  "Minimal / Deep Tech", "Organic House", "Downtempo",
                  "Indie Dance", "Afro House", "Electronica"):
            assert not go.is_catch_all(g), g

    def test_caseAndSpacingDoNotMatter(self):
        assert go.is_catch_all("  dance / POP ")
        assert go.is_catch_all("r&b")

    def test_anUnknownGenreIsNotTreatedAsABucket(self):
        """Новый жанр, которого нет в нашем списке ведёр, должен проходить.
        Иначе появление у Beatport новой полки молча вырежет её из обучения."""
        assert not go.is_catch_all("Совершенно новый жанр 2031")
        assert not go.is_catch_all("Hyperpop / Deconstructed")


class TestTrackMatching:
    def test_theArtistMatchesInBothDirections(self):
        """У нас «ARTBAT», у Beatport «ARTBAT, WhoMadeWho» — и наоборот."""
        assert go.matches("ARTBAT", "Closer", ["ARTBAT", "WhoMadeWho"], "Closer feat. WhoMadeWho")
        assert go.matches("ARTBAT, WhoMadeWho", "Closer", ["ARTBAT"], "Closer")

    def test_titleTailsDoNotBreakTheMatch(self):
        """У Beatport в названии живут «(Extended Mix)», «feat. …» — по ним
        сверять нельзя, иначе не совпадёт почти ничего."""
        assert go.matches("Monolink", "Return to Oz",
                          ["Monolink"], "Return to Oz (ARTBAT Remix)")

    def test_aDifferentTrackIsNotAMatch(self):
        assert not go.matches("Anyma", "Voices In My Head", ["Anyma"], "Eternity")

    def test_aDifferentArtistIsNotAMatch(self):
        assert not go.matches("Anyma", "Astral", ["Tale Of Us"], "Astral")

    def test_emptyInputNeverMatches(self):
        """«Не знаю» не должно случайно совпасть с чем угодно."""
        assert not go.matches("", "Astral", ["Tale Of Us"], "Astral")
        assert not go.matches("Tale Of Us", "", ["Tale Of Us"], "Astral")


class TestNormalisation:
    def test_punctuationAndCaseAreIgnored(self):
        assert go.norm("Tale Of Us") == go.norm("tale of us")
        assert go.norm("R&B / Soul") == go.norm("rbsoul")

    def test_normalisingNothingIsEmpty(self):
        assert go.norm("") == "" and go.norm(None) == ""
