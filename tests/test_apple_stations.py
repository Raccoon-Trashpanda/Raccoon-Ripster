"""Редакторские подборки Apple как источник станции.

Владелец: «изучи ещё ротации из эпла, тоже там качественно подборка создаётся».

Изучено, и главный вывод неочевиден: у РАДИОСТАНЦИЙ Apple треклиста нет вовсе
(это поток), строить на них нечего. Годятся РЕДАКТОРСКИЕ ПЛЕЙЛИСТЫ — замер
05.09.2026 по «melodic techno» дал «Melodic Techno Essentials» за подписью
Apple Music с Monolink, ARTBAT, Tale Of Us, Kölsch, Stephan Bodzin.

Дальше выяснилось, что стеречь надо ТРИ разные вещи, и каждая поймана замером,
а не придумана:

  · подпись куратора подделывается — «Apple Music Hip-Hop» оказалась подписью
    подборки самопиара одного артиста. Верить надо `playlistType`, его ставит
    сама Apple;
  · поиск уводит по первому слову — «dub techno» приводило к «Dub Essentials»
    (Apple Music Reggae): настоящая редакционная подборка, но регги;
  · подборка бывает редакционной и при этом состоять из одного человека
    целиком — это концертный сет-лист, а не жанр.
"""
import pytest

from ripster import apple_stations as a


def _pl(name, curator, kind="external"):
    return {"id": name, "attributes": {"name": name, "curatorName": curator,
                                       "playlistType": kind}}


G = "melodic techno"


class TestWhomWeTrust:
    def test_editorial_beats_a_third_party(self):
        items = [_pl("melodic techno 2026", "Future House Music"),
                 _pl("Melodic Techno Essentials", "Apple Music", "editorial")]
        assert a.rank_candidates(items, G)[0]["id"] == "Melodic Techno Essentials"

    def test_the_type_outweighs_the_signature(self):
        """Подпись подделывается, тип — нет. Живой случай 05.09.2026: подборка
        самопиара одного артиста была подписана «Apple Music Hip-Hop», и первая
        версия отбора приняла её за редакционную."""
        items = [_pl("techno by a fan", "Apple Music Hip-Hop", "external"),
                 _pl("techno essentials", "Global Trax", "editorial")]
        assert a.rank_candidates(items, "techno")[0]["id"] == "techno essentials"

    def test_among_editorial_apples_own_signature_wins(self):
        """«Algoriddim djay» тоже editorial, но это чужой бренд."""
        items = [_pl("techno: Algoriddim djay", "Algoriddim djay", "editorial"),
                 _pl("Techno Essentials", "Apple Music Dance", "editorial")]
        assert a.rank_candidates(items, "techno")[0]["id"] == "Techno Essentials"

    def test_a_third_party_is_better_than_nothing(self):
        assert a.rank_candidates([_pl("techno odyssey", "Global Trax")], "techno")

    def test_nothing_found_is_nothing(self):
        assert a.rank_candidates([], G) == []


class TestTheSearchDrifts:
    def test_a_playlist_missing_the_head_word_is_dropped(self):
        """Запрос «dub techno» приводил к «Dub Essentials» за подписью Apple
        Music Reggae — настоящая редакционная подборка, но регги."""
        assert a.name_matches_genre("Dub Essentials", "dub techno") is False
        assert a.name_matches_genre("Melodic Techno Essentials", "melodic techno") is True

    def test_the_head_word_is_the_last_one(self):
        assert a.name_matches_genre("Deep House Classics", "deep house") is True
        assert a.name_matches_genre("Deep Focus", "deep house") is False

    def test_drift_is_filtered_out_of_the_ranking(self):
        items = [_pl("Dub Essentials", "Apple Music Reggae", "editorial"),
                 _pl("Dub Techno Essentials", "Apple Music", "editorial")]
        out = a.rank_candidates(items, "dub techno")
        assert [p["id"] for p in out] == ["Dub Techno Essentials"]


class TestOneArtistIsNotAGenre:
    def test_a_concert_setlist_is_caught(self):
        """Подборка может быть редакционной и при этом состоять из одного
        человека целиком — это сет-лист, а не жанр."""
        tracks = [{"artist": "synthwave man"} for _ in range(9)] + [{"artist": "кто-то"}]
        assert a.artist_share(tracks) > a.MAX_ONE_ARTIST

    def test_a_normal_selection_passes(self):
        tracks = [{"artist": n} for n in ("ARTBAT", "Monolink", "Kölsch", "Tale Of Us")]
        assert a.artist_share(tracks) <= a.MAX_ONE_ARTIST

    def test_an_empty_list_is_not_a_hog(self):
        assert a.artist_share([]) == 0.0

    def test_two_artists_splitting_a_list_are_fine(self):
        tracks = [{"artist": "A"}] * 5 + [{"artist": "B"}] * 5
        assert a.artist_share(tracks) <= a.MAX_ONE_ARTIST


class TestTrackShape:
    def test_everything_the_phone_needs_is_carried(self):
        """Телефон сам Apple не стримит — он разрешает вещь в играбельную копию
        по ISRC. Без него подборка бесполезна."""
        t = a._track({"attributes": {
            "name": "Return to Oz (ARTBAT Remix)", "artistName": "Monolink",
            "albumName": "Return to Oz - Single", "isrc": "DETO31900005",
            "durationInMillis": 480000, "releaseDate": "2019-05-17",
            "artwork": {"url": "https://x/{w}x{h}{f}"},
        }})
        assert t["isrc"] == "DETO31900005"
        assert t["artist"] == "Monolink"
        assert t["durationMs"] == 480000
        assert t["year"] == 2019

    def test_the_artwork_template_is_filled_in(self):
        """Apple отдаёт ссылку с {w}/{h}/{f}. Клиенту нужен рабочий адрес, а
        не шаблон, который он обязан достроить сам."""
        t = a._track({"attributes": {"name": "n", "artistName": "a",
                                     "artwork": {"url": "https://x/{w}x{h}bb.{f}"}}})
        assert "{w}" not in t["artworkUrl"] and "{f}" not in t["artworkUrl"]
        assert "600" in t["artworkUrl"]

    def test_missing_fields_are_absent_not_invented(self):
        t = a._track({"attributes": {"name": "n", "artistName": "a"}})
        assert t["isrc"] == "" and t["durationMs"] is None and t["year"] is None

    def test_a_broken_release_date_does_not_crash(self):
        t = a._track({"attributes": {"name": "n", "artistName": "a", "releaseDate": "soon"}})
        assert t["year"] is None


class TestHonestFailure:
    @pytest.mark.asyncio
    async def test_no_token_says_so_instead_of_returning_nothing(self):
        """«Спросить нечем» и «в жанре ничего нет» — разные новости, и совет
        по ним разный. Пустой список без причины путает их."""
        res = await a.station("techno", {})
        assert res["ok"] is False and res["reason"] == "no_apple_token"
        assert res["tracks"] == []

    @pytest.mark.asyncio
    async def test_an_empty_genre_is_not_a_request(self):
        res = await a.station("  ", {"authorization-token": "x"})
        assert res["reason"] == "no_genre"

    def test_a_placeholder_token_counts_as_no_token(self):
        assert a._headers({"authorization-token": "your-authorization-token"}) is None

    def test_the_storefront_falls_back_to_us(self):
        assert a.storefront({}) == "us"
        assert a.storefront({"storefront": "DE"}) == "de"
