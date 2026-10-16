"""Станция как СЕССИЯ: пачки, перестройка по фидбеку, глубина прослушивания.

Зачем эти тесты. Всё ниже — про поведение, которое НЕ видно по одному запросу:
станция «работает» и с одним выстрелом на 30 треков, и с кулдауном, который сам
себя отменяет, и с `session_feedback()`, которую никто не зовёт. Поэтому каждый
тест утверждает ЧИСЛАМИ — порядок пачки, место артиста в очереди после скипа,
долю свежих в эфире, — а не «ошибки нет».

Мокаем СЕТЬ (`asyncio.gather` внутри `stations.pool`), а не правила: пул
кандидатов задаём руками, и видно, кто и почему куда попал.
"""
import asyncio
import random

import pytest

from ripster import station_events as SE
from ripster import station_sessions as SS
from ripster import stations as ST


def _item(artist, title, **kw):
    """Страница сервиса, проходящая сверку жанра: `from_artist` — трек артиста
    жанра, его пул не выбрасывает при неизвестном жанре."""
    row = {"artist": artist, "title": title, "service": "deezer",
           "id": f"{artist}{title}", "from_artist": True}
    row.update(kw)
    return row


def _cand(artist, title, *, fresh=True, heard="", **kw):
    """Кандидат уже собранный, без сети: ровно то, что лежит в сессии."""
    row = _item(artist, title, **kw)
    return {"item": row, "key": SE.track_key(row), "artist": ST.norm(artist),
            "base": ST.base_weight(row), "pop": ST.popularity_weight(row),
            "fresh": fresh, "heard": heard}


def _pool(monkeypatch, items):
    """Витрины → всегда один и тот же пул; считаем, сколько раз к ним шли."""
    calls = {"n": 0}

    async def fake_gather(*tasks, **kw):
        calls["n"] += 1
        for t in tasks:
            if asyncio.iscoroutine(t):
                t.close()
        return [list(items)]

    async def no_artists(*a, **k):
        return []

    monkeypatch.setattr(ST.asyncio, "gather", fake_gather)
    monkeypatch.setattr(ST, "artists_for_genre", no_artists)
    monkeypatch.setattr(ST, "_preview_put", lambda *a, **k: None)   # не гадим кэш превью
    return calls


@pytest.fixture()
def db(tmp_path):
    SE.init(tmp_path)
    yield
    SE._DB = None
    SS._SESSIONS.clear()


def _keys(tracks):
    return [SE.track_key(t) for t in tracks]


# ── 1. Сессия: пачки, резерв, ни одного лишнего сетевого вызова ──────────────

def test_session_gives_batches_without_repeats(db, monkeypatch):
    """Треки пачки N не возвращаются в пачке N+1, и эфир не кончается на 30-м."""
    _pool(monkeypatch, [_item(f"Artist {i}", f"Track {i}") for i in range(24)])

    async def run():
        first = await SS.open_station("techno", batch=5)
        rest = [SS.next_batch(first["session_id"]) for _ in range(4)]
        return first, rest

    first, rest = asyncio.run(run())
    assert first["ok"] and len(first["tracks"]) == 5
    sid = first["session_id"]
    assert first["batch_id"] == f"{sid}-1" and first["reserve"] == 19
    assert [r["sequence"] for r in rest] == [2, 3, 4, 5]
    assert [r["batch_id"] for r in rest] == [f"{sid}-{i}" for i in (2, 3, 4, 5)]
    given = _keys(first["tracks"]) + [k for r in rest for k in _keys(r["tracks"])]
    assert len(given) == 24, f"пачки не покрыли пул: {len(given)} из 24"
    assert len(given) == len(set(given)), f"повтор внутри сессии: {given}"
    assert rest[-1]["exhausted"] and rest[-1]["reserve"] == 0
    assert rest[-1]["suggest"] == "new_session", "пустой пул обязан просить новую сессию"


def test_next_batch_touches_no_stores(db, monkeypatch):
    """`next` берёт candidates из сессии. MusicBrainz держит «не чаще запроса в
    секунду», а опрос витрин стоит секунды: с сетью внутри дозаказа ограничитель
    встал бы посреди эфира."""
    calls = _pool(monkeypatch, [_item(f"Artist {i}", f"Track {i}") for i in range(24)])

    async def run():
        return await SS.open_station("techno", batch=6)

    sid = asyncio.run(run())["session_id"]
    assert calls["n"] == 1, "первая пачка сходила в витрины больше одного раза"
    for _ in range(3):
        assert SS.next_batch(sid)["ok"]
    assert calls["n"] == 1, "следующая пачка полезла в витрины"


def test_unknown_session_is_stale_not_an_error(db):
    """Истёкшая сессия — не красная строка на экране, а «открой новую»."""
    r = SS.next_batch("нет-такой-сессии")
    assert r["ok"] is False and r["stale"] is True and r["tracks"] == []
    assert "есси" in r["reason"]


# ── 2. Фидбек ПЕРЕСТАВЛЯЕТ очередь — то, ради чего всё писалось ──────────────

def test_feedback_reorders_the_next_batch(db, monkeypatch):
    """Скип в первой пачке опускает артиста в очереди ближайших пачек.

    Утверждаем ИЗМЕНЕНИЕ ПОРЯДКА в `next_up`, а не «вызов не упал»: до этой
    стадии `session_feedback()` была написана, покрыта тестом и никем из
    рабочего кода не вызывалась — тест, который просто дёрнул бы её, прошёл бы,
    ничего не доказав.
    """
    hot = [_item("Hated", f"t{i}", playback_count=10 ** 7) for i in range(6)]
    warm = [_item("Loved", f"t{i}") for i in range(6)]
    _pool(monkeypatch, hot + warm)
    first = asyncio.run(SS.open_station("techno", batch=2))
    sid = first["session_id"]

    def order():
        return [x["artist"] for x in SS.state(sid)["next_up"]]

    assert order()[0] == "Hated", f"без фидбека популярное ведёт: {order()}"
    # обе записи приходят телом запроса `next` — как `feedbacks` у Яндекса
    r = SS.next_batch(sid, events=[
        {"event": "skip", "artist": "Hated", "title": "t0", "played_s": 3, "length_s": 300},
        {"event": "track_finished", "artist": "Loved", "title": "t1",
         "played_s": 295, "length_s": 300}])
    assert r["ok"]
    after = order()
    assert after[0] == "Loved", f"фидбек не переставил очередь: было Hated, стало {after}"
    rows = SE._rows("SELECT session_id, batch_id FROM station_events", ())
    assert {r[0] for r in rows} == {sid}, "событие записано не в эту сессию"
    assert {r[1] for r in rows} == {first["batch_id"]}, "batch_id не доехал до базы"


def test_session_feedback_is_the_wire(db, monkeypatch):
    """Рабочий код ЗОВЁТ `session_feedback()` при пересборке пачки.

    Проверка на проводе, а не на модуле (скилл ripster-setting-with-no-wire):
    функция работала и до нас, мёртвым был вызов."""
    called = {}
    real = SE.session_feedback

    def spy(session_id, **kw):
        called["sid"], called["kw"] = session_id, kw
        return real(session_id, **kw)

    monkeypatch.setattr(SE, "session_feedback", spy)
    _pool(monkeypatch, [_item("A", "t"), _item("B", "t")])
    sid = asyncio.run(SS.open_station("techno", batch=1))["session_id"]
    SS.next_batch(sid)
    assert called.get("sid") == sid, "при пересборке пачки фидбек не спросили"
    # Гостевой барьер внутри ОДНОЙ сессии означал бы, что гостевой эфир никогда
    # не исправляется: session_id уникален на слушателя, см. _session_feedback()
    assert called["kw"] == {"owner_only": False}


# ── 3. Оценка по ГЛУБИНЕ прослушивания, а не по числу скипов ────────────────

def test_depth_scores_listen_seconds_not_skip_rows(db):
    """Два артиста с одинаковым «100 % скипов по строкам» — разные по глубине.

    Прежняя формула `1.3 − 0.95·skip_rate` по СТРОКАМ давала им одинаковый вес:
    «сбежал на 5-й секунде из 300» и «дослушал до 290-й и переключил» —
    противоположные сигналы и один и тот же вклад. Именно так у Яндекса
    (`totalPlayedSeconds/trackLengthSeconds`), Spotify (`time_played_ms/
    track_length_ms`) и Apple (`itemEndPosition/itemDuration`).
    """
    for i in range(3):
        SE.record({"event": "skip", "session_id": "s", "artist": "Ran", "title": f"r{i}",
                   "played_s": 5, "length_s": 300})
        SE.record({"event": "skip", "session_id": "s", "artist": "Stayed", "title": f"t{i}",
                   "played_s": 290, "length_s": 300})
        SE.record({"event": "track_finished", "session_id": "s", "artist": "Loved",
                   "title": f"l{i}", "played_s": 300, "length_s": 300})
    st = SE.artist_stats()
    assert st["Ran"]["skip_rate"] == st["Stayed"]["skip_rate"] == 1.0, "строки сломались"
    assert st["Ran"]["depth"] < 0.1 < st["Stayed"]["depth"]
    assert st["Loved"]["depth"] > st["Stayed"]["depth"]

    bias = ST.taste_bias()
    assert bias["stayed"] > bias["ran"], "глубина не дошла до веса: скип равняется дослушанному"
    assert bias["loved"] > bias["stayed"] > 1.0 > bias["ran"] > 0.2


def test_unknown_length_is_not_punished(db):
    """«Не знаем длину» — середина шкалы, а не «не любит»: то же правило, что
    `_NEUTRAL_WEIGHT` здесь и `StationRanker.UNKNOWN = 0.45` на телефоне."""
    for i in range(3):
        SE.record({"event": "skip", "session_id": "s", "artist": "Silent", "title": f"t{i}",
                   "played_s": 4})
    st = SE.artist_stats()
    assert st["Silent"]["depth"] is None
    assert ST.taste_bias().get("silent", 1.0) == 1.0
    assert st["Silent"]["skip_rate"] == 1.0, "строки-скипы считаться не перестали"


# ── 4. Кулдаун: дозаполнение, а не отмена правила целиком ───────────────────

def test_cooldown_tops_up_with_the_oldest_repeats():
    """Свежие — всегда; недостающее дозабирается ДАВНИМ, а не первым попавшимся.

    Прежнее `if len(fresh) >= limit` на типичном эфире отменяло всё правило
    молча. Намерение владельца — «лучше повтор, чем пустая плитка» — остаётся,
    но повтор теперь самый давний из возможных, и о нём сказано вслух.
    """
    fresh = [_cand(f"Fresh {i}", "t") for i in range(3)]
    stale = [_cand(f"Old{i}", "t", fresh=False, heard=f"2026-09-{10 + i:02d}T00:00:00")
             for i in range(7)]
    picked = ST.draw(ST.rank(fresh + stale), 6, random.Random(7), cap=0, gap=1)
    got = [e["cand"]["item"]["artist"] for e in picked]
    assert [g for g in got if g.startswith("Fresh")] == ["Fresh 0", "Fresh 1", "Fresh 2"], \
        f"свежие не все попали в эфир: {got}"
    assert [g for g in got if g.startswith("Old")] == ["Old0", "Old1", "Old2"], \
        f"повторы выбраны не по давности: {got}"


def test_cooldown_never_leaves_an_empty_tile(db, monkeypatch):
    """Пустая плитка хуже повтора: эфир обязан собраться и ЧЕСТНО сказать,
    сколько в нём повторов (`repeats`), а не выдать случайное перемешивание."""
    items = [_item("Old A", "1"), _item("Old B", "2"), _item("Fresh C", "3")]
    _pool(monkeypatch, items)
    for it in items[:2]:
        SE.record({"event": "track_started", "session_id": "s", "artist": it["artist"],
                   "title": it["title"]})

    res = asyncio.run(ST.build("techno", limit=3, seed=5))
    assert res["ok"] and len(res["tracks"]) == 3, "кулдаун оголил эфир"
    assert [t["artist"] for t in res["tracks"]][0] == "Fresh C", \
        f"свежее не ушло вперёд повторов: {[t['artist'] for t in res['tracks']]}"
    assert res["repeats"] == 2, f"о повторах молчим: {res['repeats']}"

    SE.record({"event": "track_started", "session_id": "s", "artist": "Fresh C",
               "title": "3"})
    res = asyncio.run(ST.build("techno", limit=3, seed=5))
    assert len(res["tracks"]) == 3 and res["repeats"] == 3, "сплошные повторы оголили эфир"


# ── 5. Бан, разнос артистов внутри пачки ─────────────────────────────────────

def test_dislike_ban_survives_and_batch_spreads_artists(db, monkeypatch):
    """Дизлайкнутый трек не возвращается и в новой пачке; один артист не
    занимает пачку целиком (прежде запрещалось только СОСЕДСТВО, и артист
    спокойно брал 1-ю и 4-ю позиции)."""
    items = [_item("Banned", "hated", isrc="US-BAN-26-0001")] + \
            [_item("Same", f"t{i}") for i in range(4)] + \
            [_item(f"Other {i}", "t") for i in range(6)]
    _pool(monkeypatch, items)
    SE.record({"event": "dislike", "session_id": "s", "artist": "Banned",
               "title": "hated", "isrc": "USBAN260001"})
    first = asyncio.run(SS.open_station("techno", batch=5))
    got = [t["artist"] for t in first["tracks"]]
    assert "Banned" not in got, "дизлайкнутый трек ушёл в пачку"
    assert got.count("Same") <= SS.ARTIST_CAP, f"артист захватил пачку: {got}"
    pos = [i for i, g in enumerate(got) if g == "Same"]
    assert all(b - a >= SS.ARTIST_GAP for a, b in zip(pos, pos[1:])), \
        f"артист ближе {SS.ARTIST_GAP} позиций: {got}"


# ── 6. Ручки: что из них настоящее ──────────────────────────────────────────

def test_knob_report_is_honest_about_noops(db):
    """Энергия и язык — no-op: признаков bpm/energy у станционных треков нет,
    язык трека определить нечем. Отдать «active» ручке, которая ничего не
    меняет, — тот самый врущий контрол."""
    rep = ST.knob_report({"energy": "calm", "language": "russian", "diversity": "discover"})
    assert rep["energy"]["effect"] == "no-op" and rep["language"]["effect"] == "no-op"
    assert rep["energy"]["why"] and rep["language"]["why"], "no-op без объяснения — то же враньё"
    assert rep["diversity"]["effect"] == "active"
    assert ST.knob_report({})["diversity"]["effect"] == "no-op", "default ничего не меняет"
    # незнакомое значение не ломает эфир и не притворяется рабочим
    assert ST.knobs_norm({"diversity": "favourite"})["diversity"] == "default"


def test_diversity_moves_the_share_of_known_artists(db):
    """`diversity` — единственная ручка, у которой УЖЕ есть данные: доля «своих»
    артистов (тех, о ком у владельца есть события станции), и знак веса
    популярности."""
    for i in range(3):
        SE.record({"event": "track_finished", "session_id": "s", "artist": "Known",
                   "title": f"k{i}", "played_s": 200, "length_s": 200})
    bias = ST.taste_bias()
    assert bias["known"] > 1.0
    cands = [_cand("Known", "k9", playback_count=10 ** 4),
             _cand("Stranger", "s9", playback_count=10 ** 4)]

    def top(div):
        return ST.rank(cands, bias=bias, knobs={"diversity": div})[0]["cand"]["artist"]

    assert top("favorite") == ST.norm("Known")
    assert top("discover") == ST.norm("Stranger"), "discover не выпустил незнакомое вперёд"
    # default — прежняя формула дословно: база × популярность × долгий профиль
    default = ST.rank(cands, bias=bias)
    assert [round(e["weight"], 6) for e in default] == \
        [round(c["base"] * c["pop"] * bias.get(c["artist"], 1.0), 6) for c in cands]


def test_knobs_apply_to_a_live_session_and_are_recorded(db, monkeypatch):
    """Смена ручки в живом эфире пересобирает ОСТАТОК пула и остаётся в истории:
    у Яндекса настройка пересобирает очередь в той же сессии, а не «со
    следующего запуска»."""
    items = [_item("Stranger", f"s{i}", playback_count=10 ** 4) for i in range(4)] + \
            [_item("Known", f"k{i}", playback_count=10 ** 4) for i in range(4)]
    _pool(monkeypatch, items)
    for i in range(3):
        SE.record({"event": "track_finished", "session_id": "другая", "artist": "Known",
                   "title": f"k{i}", "played_s": 200, "length_s": 200})

    first = asyncio.run(SS.open_station("techno", batch=1, knobs={"diversity": "favorite"}))
    sid = first["session_id"]
    # `next_up` — порядок, а не выдача: выдача взвешенно-случайная (правило 6),
    # и утверждать «первым лёгKnown» значило бы требовать argmax.
    assert SS.state(sid)["next_up"][0]["artist"] == "Known", \
        f"favorite не поднял своего артиста: {SS.state(sid)['next_up']}"
    r = SS.apply_knobs(sid, {"diversity": "discover"})
    assert r["ok"] and r["knobs"]["diversity"] == "discover"
    assert SS.state(sid)["next_up"][0]["artist"] == "Stranger", \
        f"discover не развернул порядок: {SS.state(sid)['next_up']}"
    assert r["tracks"] == [], "ручка пересобирает остаток, а не выдаёт трек заново"
    assert "settings_changed" in {row[0] for row in
                                  SE._rows("SELECT event FROM station_events", ())}


def test_guest_session_listens_to_itself_but_not_to_owner_taste(db, monkeypatch):
    """Гость перестраивает СВОЙ эфир по СВОИМ скипам, но вкус владельца не
    двигает ни на грамм — тот же барьер, что в приёмнике событий."""
    _pool(monkeypatch, [_item("Hated", "t", playback_count=10 ** 7), _item("Loved", "t")])
    first = asyncio.run(SS.open_station("techno", batch=1, is_guest=True))
    sid = first["session_id"]
    SS.next_batch(sid, events=[{"event": "skip", "artist": "Hated", "title": "t",
                                "played_s": 2, "length_s": 300}])
    assert {r[0] for r in SE._rows("SELECT DISTINCT is_guest FROM station_events", ())} == {1}, \
        "гостевая сессия записала событие как владельческое"
    assert not SE.artist_stats(), "гостевой скип полез во вкус владельца"
    assert SE.session_feedback(sid, owner_only=False), "гость не может перестроить СВОЙ эфир"
    assert not SE.session_feedback(sid), "гостевые события прочитаны как владельческие"


# ── 7. Запрашиваем только то, чем клиент способен играть ─────────────────────

def _fake_sources(monkeypatch):
    """Вся сеть пула — два счётчика: к какому сервису ходили и что он отдал."""
    asked = []

    async def fake_search(svc, query, limit):
        asked.append(svc)
        return [{"artist": f"{svc} artist", "title": f"{svc} track",
                 "service": svc, "id": svc, "from_artist": True}]

    async def fake_chart(slug, limit):
        asked.append("soundcloud")
        return [{"artist": "DJ Label", "title": "hour-long set",
                 "service": "soundcloud", "id": "sc1", "from_artist": True}]

    async def no_artists(*a, **k):
        return []

    monkeypatch.setattr(ST, "_service_search", fake_search)
    monkeypatch.setattr(ST, "_sc_chart", fake_chart)
    monkeypatch.setattr(ST, "artists_for_genre", no_artists)
    monkeypatch.setattr(ST, "_preview_put", lambda *a, **k: None)
    return asked


def test_pool_fetches_only_the_requested_services(db, monkeypatch):
    """`services` снимает запрос к витрине, а не выбрасывает его результат.

    Раньше фронт молча выбрасывал строки apple/yandex/soundcloud ПОСЛЕ того,
    как сервер заплатил за них сетью: `reserve` считал неиграбельное и дозаказ
    стартовал позже, чем надо.
    """
    asked = _fake_sources(monkeypatch)
    p = asyncio.run(ST.pool("techno", want=20,
                            services=["deezer", "qobuz", "tidal"]))
    assert p["ok"]
    assert set(asked) == {"deezer", "qobuz", "tidal"}, f"ходили туда, что не просят: {asked}"
    got = {c["item"]["service"] for c in p["candidates"]}
    assert got == {"deezer", "qobuz", "tidal"}, f"в пул попало неиграбельное: {got}"


def test_pool_without_services_still_asks_everything(db, monkeypatch):
    """Фильтр — отказ от запроса, а не способ остаться без музыки: вызов без
    `services` (старый `/api/station`, мобильный клиент, тесты) собирает пул
    как собирал."""
    asked = _fake_sources(monkeypatch)
    p = asyncio.run(ST.pool("techno", want=20))
    assert "apple" in asked and "yandex" in asked and "soundcloud" in asked, asked
    assert {c["item"]["service"] for c in p["candidates"]} >= {"apple", "soundcloud"}


def test_open_station_passes_services_into_the_pool(db, monkeypatch):
    """Сессия обязана унаследовать игровое ограничение клиента: `reserve` в
    ответе — это то, что реально можно сыграть, а не то, что нашли."""
    asked = _fake_sources(monkeypatch)
    r = asyncio.run(SS.open_station("techno", batch=3, services=["deezer"]))
    assert r["ok"] and asked == ["deezer"], asked
    assert {t["service"] for t in r["tracks"]} == {"deezer"}
    assert r["reserve"] == 0, f"резерв считает неиграбельное: {r['reserve']}"
