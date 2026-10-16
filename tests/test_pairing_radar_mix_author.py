"""Карточка микса уезжает на телефон с НАСТОЯЩИМ автором и id релиза.

24.09.2026, жалоба владельца: радар показывал DJ-микс «obli presents: Earth
Day 2026 (DJ Mix)» как релиз Four Tet (на ПК) и как релиз HNNY (на телефоне)
— двое слежимых артистов, у каждого по одному треку в чужом миксе, а настоя-
щий куратор (obli) на карточке не было вовсе.

Телефон чинит это сам (главная строка = alb_artist, дубли схлопываются по
release_id), но только если ПК отдаёт оба поля. Этот тест держит именно про-
вод: `/api/pair/radar` обязан нести `alb_artist` и `release_id` и в вотчлист-
ной ветке, и в spotify-подмешивании.
"""
import asyncio
import importlib
from types import SimpleNamespace

import pytest

pairing = importlib.import_module("ripster.routes.pairing")
spotify = importlib.import_module("ripster.routes.spotify")


@pytest.fixture
def door(monkeypatch):
    """Дверь сопряжения без токенов: вызываем сам маршрут напрямую."""
    monkeypatch.setattr(pairing, "_token_valid", lambda tok: True)
    req = SimpleNamespace(headers={"authorization": "Bearer test"})
    return req


OBLI_URL_APPLE = "https://music.apple.com/us/album/obli-presents-earth-day/1891503615?i=1891503616"
OBLI_URL_SPOTIFY = "https://open.spotify.com/album/7GiIJlhrejk3E1Jfu2kiA1"


def _call(req):
    return asyncio.new_event_loop().run_until_complete(pairing.pair_radar(req))


def test_watchlist_branch_carries_alb_artist_and_release_id(door, monkeypatch):
    monkeypatch.setattr(pairing, "_s", {"watchlist": [{
        "name": "Four Tet", "service": "apple", "artist_id": "35888604",
        "last_release": OBLI_URL_APPLE,
        "last_release_title": "obli presents: Earth Day 2026 (DJ Mix)",
        "last_release_date": "2026-04-22",
        "last_release_alb_artist": "obli",
    }]})
    monkeypatch.setattr(spotify, "_build_feed",
                        lambda *a, **k: {"releases": []})
    rel = _call(door)
    item = next(i for i in rel["items"] if i["name"] == "Four Tet")
    assert item["alb_artist"] == "obli"
    # id релиза, а не трека из ?i= — по нему телефон схлопывает дубли
    assert item["release_id"] == "1891503615"


def test_spotify_branch_carries_alb_artist_and_release_id(door, monkeypatch):
    monkeypatch.setattr(pairing, "_s", {"watchlist": []})
    monkeypatch.setattr(spotify, "_build_feed", lambda *a, **k: {"releases": [
        {"artist": "HNNY", "artist_id": "45021",
         "alb_artist": "obli",
         "title": "obli presents: Earth Day 2026 (DJ Mix)",
         "url": OBLI_URL_SPOTIFY, "date": "2026-04-22",
         "cover": "", "type": "mix"},
    ]})
    item = next(i for i in _call(door)["items"] if i["name"] == "HNNY")
    assert item["alb_artist"] == "obli"
    assert item["release_id"] == "7GiIJlhrejk3E1Jfu2kiA1"


def test_one_mix_from_two_watcheds_still_two_wire_items_with_same_release_id(
        door, monkeypatch):
    """ПК НЕ схлопает карточки сам: дубли схлопывает телефон по release_id.

    Провод обязан донести обе записи с ОДНИМ release_id — иначе телефон
    склеить не сможет и вернётся баг «одна карточка на каждого артиста».
    """
    monkeypatch.setattr(pairing, "_s", {"watchlist": [
        {"name": "Four Tet", "service": "apple",
         "last_release": OBLI_URL_APPLE, "last_release_alb_artist": "obli",
         "last_release_date": "2026-04-22"},
        {"name": "HNNY", "service": "apple",
         "last_release": OBLI_URL_APPLE, "last_release_alb_artist": "obli",
         "last_release_date": "2026-04-22"},
    ]})
    monkeypatch.setattr(spotify, "_build_feed",
                        lambda *a, **k: {"releases": []})
    mine = [i for i in _call(door)["items"] if i["release_id"] == "1891503615"]
    assert {i["name"] for i in mine} == {"Four Tet", "HNNY"}
    assert all(i["alb_artist"] == "obli" for i in mine)
