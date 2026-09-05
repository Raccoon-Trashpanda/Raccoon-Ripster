"""Разбор анонсов релизов — на ЖИВЫХ заголовках, снятых 05.09.2026.

Замысел владельца: радар грядущего должен ловить альбомы, которые ещё не вышли,
но уже объявлены, и ставить их в вишлист. Из этого следует главное требование к
разбору: лучше пропустить анонс, чем завести в вишлист мусор — человек проверяет
список глазами, и одна «шляпа» обесценивает весь.

Заголовки ниже не выдуманы: они взяты из лент The Quietus, Pitchfork,
Brooklyn Vegan, XLR8R и Clash в день разработки.
"""
from ripster.upcoming_radar import SOURCES, parse_announcement


class TestRealHeadlines:
    def test_quietus_form_with_quoted_title(self):
        a = parse_announcement("Pet Shop Boys Detail New Album, ‘A Man From The Future’")
        assert a is not None
        assert a.artist == "Pet Shop Boys"
        assert a.title == "A Man From The Future"
        assert a.kind == "album"

    def test_announce_verb_and_live_album(self):
        a = parse_announcement("Manic Street Preachers Announce New Live Album, ‘Holy Bible Live’")
        assert a.artist == "Manic Street Preachers"
        assert a.title == "Holy Bible Live"

    def test_title_without_quotes(self):
        """Pitchfork пишет название просто следом, без кавычек."""
        a = parse_announcement("M83 Announces New Album I Wrote You a Letter")
        assert a.artist == "M83"
        assert a.title == "I Wrote You a Letter"

    def test_words_between_verb_and_album(self):
        a = parse_announcement("Andy Stott Unveils First Album in Five Years, ‘Late Loop’")
        assert a.artist == "Andy Stott"
        assert a.title == "Late Loop"

    def test_two_artists_stay_together(self):
        a = parse_announcement("Saint Abdullah & Eomac Announce New Album ‘When The Sandbox Ends’")
        assert a.artist == "Saint Abdullah & Eomac"
        assert a.title == "When The Sandbox Ends"

    def test_announcement_without_a_title(self):
        """«Bjarki Returns with New Album» — анонс есть, названия нет.
        Это по-прежнему то, что стоит ждать."""
        a = parse_announcement("Bjarki Returns with New Album")
        assert a.artist == "Bjarki"
        assert a.title == ""

    def test_reveals_verb(self):
        a = parse_announcement("Shit And Shine Reveals New Album, ‘0 aa Riot’")
        assert a.artist == "Shit And Shine"
        assert a.title == "0 aa Riot"


class TestNoise:
    def test_concert_announcement_is_not_a_release(self):
        """Тот же глагол, другое событие: «Cypress Hill announce ‘Haunted’
        shows in NYC». В вишлист грядущих релизов концерт попасть не должен."""
        assert parse_announcement("Cypress Hill announce ‘Haunted’ shows in NYC & Colorado") is None

    def test_tour_dates_are_not_a_release(self):
        assert parse_announcement("Radiohead announce 2027 European tour dates") is None

    def test_a_review_is_not_an_announcement(self):
        assert parse_announcement("Portishead – Dummy: album of the week") is None

    def test_already_released_is_not_an_announcement(self):
        assert parse_announcement("Listen to the new album from Aphex Twin") is None

    def test_plain_news_is_ignored(self):
        assert parse_announcement("The White Hotel Unveils Final Autumn Music Programme") is None

    def test_empty_input(self):
        assert parse_announcement("") is None
        assert parse_announcement("   ") is None


class TestKind:
    def test_ep_is_recognised_separately(self):
        a = parse_announcement("Klara Lewis Announces New EP ‘Thankful’")
        assert a.kind == "ep"
        assert a.artist == "Klara Lewis"

    def test_mixtape_counts_as_a_release(self):
        a = parse_announcement("Some Artist Announces New Mixtape ‘Tape’")
        assert a is not None
        assert a.kind == "album"


class TestSources:
    def test_every_source_carries_its_measurement(self):
        """Список собран замерами, а не представлением о том, кто пишет про
        музыку. Источник без замера — это догадка, и она сюда не попадает."""
        assert len(SOURCES) >= 5
        for s in SOURCES:
            assert s.url.startswith("https://")
            assert "/" in s.measured, f"{s.name}: нет замера плотности анонсов"

    def test_sources_are_unique(self):
        assert len({s.url for s in SOURCES}) == len(SOURCES)


class TestDefectsFoundOnLiveFeeds:
    """Дефекты, которые показал прогон по живым лентам 05.09.2026.

    Каждый из них — это строка, которая попала бы в вишлист владельца и
    выглядела бы там мусором.
    """

    def test_a_time_span_is_not_a_title(self):
        """«First Album in Five Years» без кавычек: срок, а не название.
        Разбор выдавал название «in Five Years»."""
        a = parse_announcement("Andy Stott Unveils First Album in Five Years")
        assert a is not None
        assert a.artist == "Andy Stott"
        assert a.title == ""

    def test_a_retold_news_sentence_is_not_an_artist(self):
        """«Miley Cyrus Rebrands as Miley, Announces New Album…» — в поле
        артиста уезжала целая фраза с запятой на конце."""
        assert parse_announcement(
            "Miley Cyrus Rebrands as Miley, Announces New Album Bass Persuades"
        ) is None

    def test_a_shared_song_is_not_an_album_title(self):
        a = parse_announcement("Bonnie Prince Billy Shares Song From New Album")
        assert a is None or a.title == ""

    def test_trailing_comma_is_trimmed(self):
        a = parse_announcement("Fontaines D.C. Announce New Album Dopamine Chamber, Out In March")
        assert a is not None
        assert a.title == "Dopamine Chamber"

    def test_comma_inside_the_quotes_is_trimmed(self):
        """Живой заголовок Brooklyn Vegan: «‘Dopamine Chamber,’ out in March» —
        запятая стояла ВНУТРИ кавычек и уезжала в название."""
        a = parse_announcement("Fontaines D.C. Announce New Album ‘Dopamine Chamber,’ Out In March")
        assert a is not None
        assert a.title == "Dopamine Chamber"

    def test_a_description_is_stripped_from_the_artist(self):
        """Живой заголовок The Line of Best Fit: «Alternative metal band
        Prodigal announce EP…» — в вишлисте нужен артист, а не кто он такой."""
        a = parse_announcement("Alternative metal band Prodigal announce EP ‘The Floating World’")
        assert a is not None
        assert a.artist == "Prodigal"
        assert a.title == "The Floating World"

    def test_of_is_not_a_title(self):
        """«Remix EP of Latrec’s Kutika» — «of …» это не название нового
        релиза, а указание на чужой."""
        a = parse_announcement("Viscera Transmissions announce Remix EP of Latrec’s Kutika")
        assert a is not None
        assert a.title == ""

    def test_a_dangling_feat_is_not_part_of_the_title(self):
        """Живой Stereogum: «…New Album Bloodwork Feat. Someone» — разрез по
        точке оставлял в названии висящее «Feat»."""
        a = parse_announcement("Gun Announce New Album Bloodwork Feat. Someone Else")
        assert a is not None
        assert a.title == "Bloodwork"
