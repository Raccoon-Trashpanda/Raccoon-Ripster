"""Радио «иссякло» при 24 найденных похожих (01.10.2026, Nu:Logic): поиск трека шёл по `search/track?q=artist:"…"`,
а первые пять результатов у нишевых артистов — ремиксы, где они соавторы; сверка имени отбрасывала все.
Теперь артиста ищем как артиста и берём его топ-треки."""
import asyncio

from ripster import digs_radio as R


class _Resp:
    def __init__(self, data, code=200):
        self._d, self.status_code = data, code

    def json(self):
        return self._d


class _Client:
    """Deezer на подмене: у «Calibre» в поиске треков одни чужие ремиксы, а топ артиста есть."""

    def __init__(self):
        self.urls = []

    async def get(self, url, params=None):
        self.urls.append(url)
        if url.endswith("/search/artist"):
            return _Resp({"data": [{"id": 7, "name": "Calibre Tribute Band"}, {"id": 42, "name": "Calibre"}]})
        if url.endswith("/artist/42/top"):
            return _Resp({"data": [{"id": 900, "title": "Even If", "readable": True, "album": {"cover_medium": "c"}}]})
        if url.endswith("/search/track"):
            return _Resp({"data": [{"id": 1, "title": "Remix", "artist": {"name": "Kölsch"}}]})
        return _Resp({}, 404)


def test_niche_artist_found_through_artist_top():
    c = _Client()
    tr = asyncio.run(R._top_track(c, "Calibre"))
    assert tr and tr["id"] == "900" and tr["artist"] == "Calibre" and tr["service"] == "deezer"
    assert not any(u.endswith("/search/track") for u in c.urls)       # запасной путь не понадобился


def test_wrong_artist_is_never_returned():
    class C(_Client):
        async def get(self, url, params=None):
            if url.endswith("/search/artist"):
                return _Resp({"data": [{"id": 7, "name": "Someone Else"}]})
            return await super().get(url, params)
    assert asyncio.run(R._top_track(C(), "Calibre")) is None


def test_fallback_to_track_search_when_artist_search_fails():
    class C(_Client):
        async def get(self, url, params=None):
            if url.endswith("/search/artist"):
                return _Resp({}, 500)
            if url.endswith("/search/track"):
                return _Resp({"data": [{"id": 5, "title": "T", "artist": {"name": "Calibre"}}]})
            return _Resp({}, 404)
    assert asyncio.run(R._top_track(C(), "Calibre"))["id"] == "5"
