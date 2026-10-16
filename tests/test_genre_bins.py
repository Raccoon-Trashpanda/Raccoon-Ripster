"""Корзина остатков — ответ последней надежды, а не ответ.

«Experimental» у Discogs и «Electronica» у Beatport — не жанры, а полки,
куда сваливают всё, что не разложилось. Штраф множителем их не останавливал,
и вот почему: корзину называет САМЫЙ доверенный источник, а настоящее имя —
самый слабый. 0.95 × 0.35 перевешивает 0.5 × 0.5, и арифметика тут ни при
чём — виноват порядок разбора, а не числа.

Замер 06.09.2026: Caribou и Kraftwerk получали «Experimental», хотя в тех же
данных лежали «folktronica» и «krautrock».

Правило: сказано хоть что-то настоящее — корзина уступает. Она остаётся
ответом ровно тогда, когда сказать больше нечего, и это не поражение:
назвать корзину честнее, чем промолчать или выдумать.
"""
from ripster.genre_resolver import GenreResolver


class TestTheBinYieldsToARealName:
    def test_folktronica_beats_experimental(self):
        votes = {"beatport": ["Electronica"], "discogs": ["Experimental"],
                 "bandcamp": ["electronic"],
                 "musicbrainz": ["electronic", "folktronica", "neo-psychedelia"]}
        assert GenreResolver().resolve(votes)["genre"] == "folktronica"

    def test_a_real_name_from_the_weakest_source_still_wins(self):
        """Настоящее имя сказал только MusicBrainz — самый слабый из четырёх.
        Этого достаточно: корзина не конкурент, она отсутствие ответа."""
        votes = {"beatport": ["Electronica"], "discogs": ["Experimental"],
                 "musicbrainz": ["electronic", "synth-pop", "krautrock"]}
        got = GenreResolver().resolve(votes)["genre"]
        assert got in ("synth-pop", "krautrock"), got


class TestTheBinStaysWhenNothingElseWasSaid:
    def test_only_bins_means_the_bin_is_the_answer(self):
        """Промолчать было бы хуже: человек спросил, и «Experimental» —
        это всё, что о пластинке известно. Отвечаем тем, что есть."""
        out = GenreResolver().resolve({"discogs": ["Experimental"],
                                       "beatport": ["Electronica"]})
        assert out["genre"] == "Experimental"
        assert out["confidence"] > 0.5

    def test_a_lone_stray_tag_does_not_hijack_the_answer(self):
        """У правила есть порог. Одинокий случайный тег в самом хвосте не
        должен вытеснять пусть и пустой, но согласованный ответ — иначе
        лекарство станет новым способом соврать."""
        votes = {"discogs": ["Experimental"], "beatport": ["Electronica"],
                 "bandcamp": ["electronic", "electronic", "electronic", "k-pop remix"]}
        assert GenreResolver().resolve(votes)["genre"] != "k-pop remix"


class TestRealNamesAreLeftAlone:
    """Правило трогает ТОЛЬКО корзины. Всё, что и так является ответом,
    остаётся на месте — иначе лекарство хуже болезни."""

    def test_alternative_rock_is_a_genre_not_a_bin(self):
        votes = {"discogs": ["Alternative Rock"],
                 "musicbrainz": ["alternative rock", "noise rock", "no wave"]}
        assert GenreResolver().resolve(votes)["genre"] == "Alternative Rock"

    def test_contemporary_jazz_is_a_genre_though_contemporary_is_not(self):
        votes = {"discogs": ["Contemporary Jazz"], "musicbrainz": ["jazz"]}
        assert GenreResolver().resolve(votes)["genre"] == "Contemporary Jazz"

    def test_a_modifier_yields_the_same_way_a_bucket_does(self):
        """«Acoustic» описывает состав, а не направление: Ali Farka Touré."""
        votes = {"discogs": ["Acoustic"],
                 "musicbrainz": ["african blues", "world", "desert blues"]}
        assert GenreResolver().resolve(votes)["genre"] == "african blues"
