"""События станций: запись, гостевой барьер, ранкер.

Зачем эти тесты. Станция подстраивается под человека ТОЛЬКО через эти события:
скип с секундами, дослушивание, лайк, скачивание, бан. Если запись тихо
перестанет работать (сменится имя события, поле, путь к базе), станция снова
станет случайной выборкой по жанру — ровно то, на что жаловался владелец
19.09.2026 («станции багованные и собирают что попало»). Молчаливая поломка
здесь неотличима от «человек просто мало слушал», поэтому проверяем цифрами.
"""
import tempfile

import pytest

from ripster import station_events as SE


@pytest.fixture()
def db(tmp_path, monkeypatch):
    SE.init(tmp_path)
    yield
    SE._DB = None


def test_unknown_event_is_refused(db):
    """Словарь событий закрытый: опечатка в имени не должна тихо писаться."""
    assert SE.record({"event": "skipped", "session_id": "s"}) is False
    assert SE.counts()["events"] == 0


def test_skip_and_finish_are_counted_separately(db):
    SE.record({"event": "track_finished", "session_id": "s", "artist": "A",
               "played_s": 300, "length_s": 330})
    for i in range(3):
        SE.record({"event": "skip", "session_id": "s", "artist": "B",
                   "title": f"t{i}", "played_s": 4, "length_s": 300})
    st = SE.artist_stats()
    assert st["A"]["finished"] == 1 and st["A"]["skips"] == 0
    assert st["B"]["skips"] == 3
    # Доля скипов считается только при трёх и более прослушиваниях: один скип
    # из одного — случайность, а не вкус.
    assert st["B"]["skip_rate"] == 1.0
    assert st["A"]["skip_rate"] is None


def test_guest_never_shapes_owner_taste(db):
    """Гость слушает своё. Его скипы не должны переучивать станции владельца."""
    SE.record({"event": "skip", "session_id": "g", "artist": "Гость",
               "played_s": 2, "length_s": 200, "is_guest": True})
    assert "Гость" not in SE.artist_stats()
    assert SE.counts()["guest_events"] == 1


def test_dislike_bans_the_track_until_undone(db):
    SE.record({"event": "dislike", "session_id": "s", "artist": "A",
               "title": "t", "isrc": "GB-ABC-12-34567"})
    key = SE.track_key({"isrc": "gbabc1234567"})
    assert key in SE.banned_track_keys()
    SE.record({"event": "undislike", "session_id": "s", "artist": "A",
               "title": "t", "isrc": "GBABC1234567"})
    assert key not in SE.banned_track_keys()


def test_isrc_key_survives_formatting(db):
    """Один и тот же ISRC в разном написании — один ключ, иначе бан и кулдаун
    промахиваются мимо того же трека с другого сервиса."""
    assert SE.track_key({"isrc": "gb-abc-12-34567"}) == SE.track_key({"isrc": "GBABC1234567"})
    # Без ISRC ключ собирается из имени: сервисы часто его не отдают.
    assert SE.track_key({"artist": " Jeff Mills ", "title": "The Bells"}).startswith("name:")


def test_session_feedback_weights_match_the_signals(db):
    SE.record({"event": "skip", "session_id": "s1", "artist": "A", "played_s": 5, "length_s": 300})
    SE.record({"event": "track_finished", "session_id": "s1", "artist": "B",
               "played_s": 290, "length_s": 300})
    SE.record({"event": "download", "session_id": "s1", "artist": "C"})
    w = SE.session_feedback("s1")
    assert w["A"] < 0 < w["B"] < w["C"]          # скип минус, дослушал плюс, скачал больше всех


def test_recent_keys_are_the_cooldown_list(db):
    SE.record({"event": "track_started", "session_id": "s", "artist": "A", "title": "t"})
    assert SE.track_key({"artist": "A", "title": "t"}) in SE.recent_track_keys(7)


def test_broken_row_does_not_raise(db):
    """Потеря одной строки статистики не стоит прерванного прослушивания."""
    assert SE.record({"event": "skip", "session_id": "s", "artist": "A",
                      "played_s": "много"}) is True          # мусорные секунды → None, но запись есть
    assert SE.record({}) is False


def test_ranker_uses_history(db, monkeypatch):
    """Ранкер обязан опускать того, кого постоянно скипают, и поднимать того,
    чьи треки скачивают. Отбор взвешенно-случайный, поэтому судим не по одному
    эфиру, а по десяти: один прогон ничего не доказывает (на том и горели
    прошлые «проверки» станций)."""
    import asyncio

    from ripster import stations as ST

    for i in range(4):
        SE.record({"event": "skip", "session_id": "s", "artist": "Skipped",
                   "title": f"t{i}", "played_s": 3, "length_s": 300})
    SE.record({"event": "download", "session_id": "s", "artist": "Loved", "title": "x"})

    # Пул из многих артистов намеренно: с двумя правило «не два трека одного
    # артиста подряд» чередует их поровну, и любой вес был бы не виден.
    items = ([{"artist": "Skipped", "title": f"s{i}", "service": "deezer", "from_artist": True}
              for i in range(6)]
             + [{"artist": "Loved", "title": f"l{i}", "service": "deezer", "from_artist": True}
                for i in range(6)]
             + [{"artist": f"Neutral{n}", "title": f"n{n}", "service": "deezer", "from_artist": True}
                for n in range(18)])

    async def fake_gather(*tasks, **kw):
        for t in tasks:                      # не оставляем корутины неожидаными
            if asyncio.iscoroutine(t):
                t.close()
        return [list(items)]

    async def fake_artists(*a, **k):
        return []

    monkeypatch.setattr(ST.asyncio, "gather", fake_gather)
    monkeypatch.setattr(ST, "artists_for_genre", fake_artists)

    loved = skipped = 0
    for seed in range(24):
        res = asyncio.run(ST.build("techno", limit=10, seed=seed))
        got = [t["artist"] for t in res["tracks"]]
        assert got, "станция не собралась вовсе"
        loved += got.count("Loved")
        skipped += got.count("Skipped")
    assert loved > skipped, f"скипаемый не опустился: любимых {loved}, скипаемых {skipped}"


async def _async(v):
    return v
