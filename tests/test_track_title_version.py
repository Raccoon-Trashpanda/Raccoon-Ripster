"""Список треков Qobuz/Tidal: версия трека не должна теряться.

Баг владельца (30.09.2026): на странице релиза четыре трека называются
одинаково «Sky Falls Down» при длительностях 7:04, 3:53, 4:11, 7:13 — Qobuz и
Tidal отдают версию отдельным полем `version`, а движки брали только `title`.
"""
import asyncio

from ripster.engines.base import title_with_version


def test_version_is_appended_in_parentheses():
    assert title_with_version("Sky Falls Down", "Extended Mix") == "Sky Falls Down (Extended Mix)"


def test_no_version_keeps_title_unchanged():
    assert title_with_version("Sky Falls Down", "") == "Sky Falls Down"
    assert title_with_version("Sky Falls Down", None) == "Sky Falls Down"


def test_version_already_inside_title_is_not_duplicated():
    # часть каталогов кладёт версию в сам title — «… (Mix) (Mix)» получаться не должно
    assert title_with_version("Sky Falls Down (Extended Mix)", "Extended Mix") == "Sky Falls Down (Extended Mix)"
    assert title_with_version("Sky Falls Down (extended mix)", "Extended Mix") == "Sky Falls Down (extended mix)"


def test_whitespace_is_trimmed():
    assert title_with_version("  Sky Falls Down ", " Radio Edit ") == "Sky Falls Down (Radio Edit)"


# ── через настоящие get_album обоих движков (HTTP подменён) ─────────────────────────

class _Resp:
    status_code = 200

    def __init__(self, data):
        self._d = data

    def json(self):
        return self._d


class _Client:
    def __init__(self, routes):
        self.routes = routes

    async def get(self, url, **kw):
        for k, v in self.routes.items():
            if k in url:
                return _Resp(v)
        raise AssertionError(f"неожиданный запрос: {url}")


class _Ctx:
    def __init__(self, c):
        self.c = c

    async def __aenter__(self):
        return self.c

    async def __aexit__(self, *a):
        return False


def test_qobuz_get_album_keeps_track_versions(monkeypatch):
    from ripster.engines import qobuz as qb
    album = {"id": "a1", "title": "Sky Falls Down (Downtempo Version)",
             "artist": {"name": "Above & Beyond"}, "tracks_count": 3,
             "tracks": {"items": [
                 {"id": 1, "title": "Sky Falls Down", "version": "Extended Mix", "duration": 424, "track_number": 1},
                 {"id": 2, "title": "Sky Falls Down", "version": "Radio Edit", "duration": 233, "track_number": 2},
                 {"id": 3, "title": "Sky Falls Down", "duration": 251, "track_number": 3}]}}
    monkeypatch.setattr(qb._HTTP, "ashared", lambda: _Ctx(_Client({"album/get": album})))
    out = asyncio.run(qb.QobuzEngine().get_album("a1", {}))
    assert [t["title"] for t in out["tracks"]] == [
        "Sky Falls Down (Extended Mix)", "Sky Falls Down (Radio Edit)", "Sky Falls Down"]


def test_tidal_get_album_keeps_track_versions(monkeypatch):
    from ripster.engines import tidal as td

    class _AlbResp(_Resp):
        pass

    async def fake_api_get(c, url, params, config):
        return _AlbResp({"id": 9, "title": "Sky Falls Down", "artist": {"name": "Above & Beyond"}}), "tok", "US"

    tracks = {"items": [
        {"id": 1, "title": "Sky Falls Down", "version": "Extended Mix", "trackNumber": 1, "duration": 424},
        {"id": 2, "title": "Sky Falls Down", "version": "Radio Edit", "trackNumber": 2, "duration": 233}]}
    monkeypatch.setattr(td, "_api_get", fake_api_get)
    monkeypatch.setattr(td._HTTP, "ashared", lambda: _Ctx(_Client({"/tracks": tracks})))
    out = asyncio.run(td.TidalEngine().get_album("9", {}))
    assert [t["title"] for t in out["tracks"]] == [
        "Sky Falls Down (Extended Mix)", "Sky Falls Down (Radio Edit)"]
