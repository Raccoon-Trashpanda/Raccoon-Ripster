"""У магазина одна полка, и на неё кладут всё, что он продаёт.

Beatport торгует танцевальной электроникой. Спроси его про «Move On Up»
Кёртиса Мэйфилда 1970 года — он найдёт клубный эдит и ответит «House» с
доверием 1.00, потому что другого ответа у него не бывает. В замере
06.09.2026 он назвал Sun Ra — «Nu Disco / Disco», Fugees — тем же самым,
и оба раза побеждал: доверие к нему выставлено высшим, и заслуженно —
ВНУТРИ танцевальной музыки он точнее всех остальных вместе взятых.

Ошибка была не в Beatport, а в том, кто спрашивает его про то, чем он не
торгует. В резолвере для этого с самого начала лежал параметр `area_hint`,
но его никто никогда не вычислял: сторож, который не мог сработать.

Правило, которое здесь стережётся: голос магазина о танцевальном жанре —
довод о ПОДВИДЕ, но не довод о том, что пластинка вообще танцевальная.
Установить это может только каталог, который описывает издания, а не полки.
"""
from ripster.genre_resolver import GenreResolver, out_of_area_shops


class TestTheShopIsSetAsideOutsideItsTrade:
    def test_a_disco_edit_does_not_make_sun_ra_disco(self):
        votes = {"beatport": ["Nu Disco / Disco"],
                 "discogs": ["Free Jazz"],
                 "bandcamp": ["jazz", "avant-garde", "big band", "cosmic jazz"]}
        assert out_of_area_shops(votes) == {"beatport"}
        assert GenreResolver().resolve(votes)["genre"] == "Free Jazz"

    def test_a_club_edit_does_not_make_curtis_mayfield_house(self):
        """Здесь мимо смотрят ОБА магазина сразу: у Bandcamp тот же эдит."""
        votes = {"beatport": ["House"],
                 "discogs": ["Soul"],
                 "bandcamp": ["electronic", "move on up", "afro house"]}
        assert out_of_area_shops(votes) == {"beatport", "bandcamp"}
        assert GenreResolver().resolve(votes)["genre"] == "Soul"

    def test_a_namesake_does_not_make_kendrick_deep_house(self):
        votes = {"discogs": ["Conscious"],
                 "bandcamp": ["deep house", "electronic", "tech house"]}
        assert "bandcamp" in out_of_area_shops(votes)
        assert GenreResolver().resolve(votes)["genre"] == "Conscious"


class TestTheShopKeepsItsVoiceWhereItTrades:
    """Правило снимает голос ровно там, где магазин не по адресу. Внутри
    танцевальной музыки Beatport остаётся главным источником — иначе
    лекарство было бы хуже болезни."""

    def test_techno_is_beatports_own_trade(self):
        votes = {"beatport": ["Techno"], "discogs": ["Techno"],
                 "musicbrainz": ["techno", "detroit techno"]}
        assert out_of_area_shops(votes) == set()
        assert GenreResolver().resolve(votes)["genre"] == "Techno"

    def test_downtempo_counts_as_the_dance_shelf(self):
        votes = {"beatport": ["Downtempo"], "discogs": ["Downtempo"],
                 "bandcamp": ["chillout", "downtempo"]}
        assert out_of_area_shops(votes) == set()

    def test_one_dance_word_from_the_catalogue_is_enough(self):
        """Каталог назвал танцевальное хотя бы раз — магазин по адресу,
        даже если остальные его слова про другое."""
        votes = {"beatport": ["Electro"],
                 "discogs": ["Synth-pop"],
                 "musicbrainz": ["electronic", "electro", "krautrock"]}
        assert out_of_area_shops(votes) == set()


class TestSilenceIsNotAVerdict:
    def test_without_the_catalogue_nobody_is_set_aside(self):
        """Каталог промолчал — мы не знаем, где стоит пластинка. Незнание не
        повод снимать единственный голос: это была бы подмена ответа."""
        assert out_of_area_shops({"beatport": ["Techno"],
                                  "bandcamp": ["techno"]}) == set()

    def test_an_empty_catalogue_answer_counts_as_silence(self):
        assert out_of_area_shops({"beatport": ["House"], "discogs": []}) == set()

    def test_a_shop_speaking_non_dance_is_left_alone(self):
        """Снимается не источник, а его танцевальный ответ не по адресу.
        Если Bandcamp сам говорит «soul» — возражать не о чем."""
        votes = {"bandcamp": ["soul", "funk"], "discogs": ["Soul"]}
        assert out_of_area_shops(votes) == set()
