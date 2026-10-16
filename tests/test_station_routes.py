"""HTTP-контракт станции-сессии: то, по чему пишется фронт.

Зачем. `ripster/station_sessions.py` целиком проверен юнит-тестами, но у модуля
есть вторая половина — приёмники в `ripster/routes/stations.py`, и их до сих пор
не трогал НИКТО: тесты звали функции модуля напрямую, минуя форму тела,
`request`, гостевой барьер и то, что истёкшая сессия обязана прийти как 200 с
`stale`, а не как 500. Фронт будет звать адрес, а не функцию, — значит адрес и
надо проверить запросом.

Играем ЧЕРЕЗ `TestClient`, сеть витрин подменена: проверяется проводка, а не
сервисы чужих компаний.
"""
import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ripster import station_events as SE
from ripster import station_sessions as SS
from ripster import stations as ST
from ripster.routes import station_events as SE_routes
from ripster.routes import stations as ST_routes


def _item(artist, title, **kw):
    row = {"artist": artist, "title": title, "service": "deezer",
           "id": f"{artist}{title}", "from_artist": True}
    row.update(kw)
    return row


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """Настоящие роуты, настоящая база событий, подменённые витрины."""
    items = [_item("Hated", f"t{i}", playback_count=10 ** 7) for i in range(6)] + \
            [_item("Loved", f"t{i}", playback_count=10 ** 2) for i in range(6)]

    async def fake_gather(*tasks, **kw):
        for t in tasks:
            if asyncio.iscoroutine(t):
                t.close()
        return [list(items)]

    async def no_artists(*a, **k):
        return []

    monkeypatch.setattr(ST.asyncio, "gather", fake_gather)
    monkeypatch.setattr(ST, "artists_for_genre", no_artists)
    monkeypatch.setattr(ST, "_preview_put", lambda *a, **k: None)

    class Ctx:
        config: dict = {}
        base_dir = str(tmp_path)

    app = FastAPI()
    ST_routes.install(app, Ctx())
    SE_routes.install(app, Ctx())
    with TestClient(app) as c:
        yield c
    SE._DB = None
    SS._SESSIONS.clear()


def test_open_session_route_returns_the_documented_shape(client):
    """Фронт не угадывает имена полей: контракт шапки модуля проверяется ключами."""
    r = client.post("/api/station/session", json={"id": "techno", "batch": 3})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] and len(body["tracks"]) == 3
    for key in ("session_id", "station_id", "title", "batch_id", "sequence",
                "reserve", "reserve_min", "exhausted", "suggest", "sources",
                "artists", "knobs", "knob_report", "took_ms"):
        assert key in body, f"контракт обещает `{key}`, ответ его не даёт"
    sid = body["session_id"]
    assert body["batch_id"] == f"{sid}-1" and body["sequence"] == 1
    assert body["knob_report"]["energy"]["effect"] == "no-op"
    # карточка трека — та же форма, что у старого /api/station, иначе плееру
    # придётся различать два эфира
    assert {"artist", "title", "service"} <= set(body["tracks"][0])


def test_missing_station_id_and_unknown_station_explain_themselves(client):
    for body in ({}, {"id": "нет-такой-станции"}):
        r = client.post("/api/station/session", json=body)
        assert r.status_code == 200 and r.json()["ok"] is False
        assert r.json()["tracks"] == [] and r.json()["reason"], "пустой ответ без причины"


def test_next_route_reorders_from_events_posted_to_the_events_route(client):
    """Событие ушло ОТДЕЛЬНЫМ роутом (как делает плеер на каждый трек), а
    перестройку видит `next`. Это и есть петля обратной связи на живом проводе:
    два разных обработчика, одна база, один эфир.
    """
    sid = client.post("/api/station/session", json={"id": "techno", "batch": 2}).json()["session_id"]
    before = [x["artist"] for x in client.get(f"/api/station/session/{sid}").json()["next_up"]]
    assert before[0] == "Hated", f"без фидбека popular ведёт: {before}"

    ev = {"event": "skip", "session_id": sid, "batch_id": f"{sid}-1",
          "artist": "Hated", "title": "t0", "played_s": 4, "length_s": 300}
    assert client.post("/api/stations/event", json=ev).status_code == 200

    nxt = client.post(f"/api/station/session/{sid}/next", json={}).json()
    assert nxt["ok"] and nxt["sequence"] == 2 and nxt["batch_id"] == f"{sid}-2"
    after = [x["artist"] for x in client.get(f"/api/station/session/{sid}").json()["next_up"]]
    assert after[0] == "Loved", f"событие с роута событий не переставило эфир: {after}"


def test_banned_artist_row_is_rejected_not_silently_dropped(client):
    """Неизвестное событие — 400: молчаливое «записали» означало бы, что плеер
    годами шлёт то, чего в словаре нет."""
    assert client.post("/api/stations/event", json={"event": "nope"}).status_code == 400


def test_stale_session_answers_200_with_stale_not_500(client):
    """Перезапуск приложения — не красная строка на экране плеера."""
    r = client.post("/api/station/session/dead-beef/next", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False and body["stale"] is True and body["tracks"] == []
    assert body["reason"]


def test_knobs_route_rebuilds_the_live_reserve(client):
    """Ручка в живом эфире меняет порядок ОСТАТКА, не выдавая новых треков, и
    остаётся в истории событием `settings_changed`."""
    opened = client.post("/api/station/session",
                         json={"id": "techno", "batch": 2,
                               "knobs": {"diversity": "favorite"}}).json()
    sid = opened["session_id"]
    assert opened["knobs"]["diversity"] == "favorite"
    r = client.post(f"/api/station/session/{sid}/knobs",
                    json={"knobs": {"diversity": "discover"}}).json()
    assert r["ok"] and r["knobs"]["diversity"] == "discover" and r["tracks"] == []
    kinds = {row[0] for row in SE._rows("SELECT event FROM station_events", ())}
    assert "settings_changed" in kinds, "смена ручки пропала в истории эфира"


def test_no_track_is_handed_twice_over_http(client):
    """Антиповтор между пачками держится и через HTTP: хвост очереди с клиента
    не нужен (его нет ни в одном запросе выше)."""
    opened = client.post("/api/station/session", json={"id": "techno", "batch": 4}).json()
    sid = opened["session_id"]
    first = [SE.track_key(t) for t in opened["tracks"]]
    got = []
    for _ in range(3):
        body = client.post(f"/api/station/session/{sid}/next", json={}).json()
        got += [SE.track_key(t) for t in body["tracks"]]
    # В пуле теста 12 записей, открытие эфира уже выдало 4 — значит `next` может
    # отдать не больше восьми. Суть проверки не в их числе, а в том, что ни одна
    # запись не пришла дважды: ни внутри пачек, ни между пачкой и открытием.
    assert len(got) == 8, f"остаток пула роздан не полностью: {got}"
    assert len(set(got)) == len(got), f"через роуты эфир повторился: {got}"
    assert not (set(got) & set(first)), "выданное при открытии вернулось в next"
