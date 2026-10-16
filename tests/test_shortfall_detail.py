"""Поимённая недостача («пришло M из N») работает не только у Deezer.

Раньше канонический треклист умел добывать только Deezer, и для Apple/Qobuz/
Tidal карточка показывала одну общую причину без имён треков. Теперь список
берётся у общего `resolver.resolve` — того же, которым строится дерево задачи в
очереди, чтобы карточка и дерево не спорили о составе релиза.

Ключевая осторожность: у не-Deezer доступность трека НЕИЗВЕСТНА (`None`), и
неизвестность обязана вести к повтору у того же сервиса, а не к заявлению
«трека нет» — иначе владельцу предложат искать на стороне то, что просто
не докачалось.
"""
import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from ripster import release_diff as RD  # noqa: E402
from ripster import runner as RU  # noqa: E402

ALBUM = [{"title": "Give Life Back to Music", "track_num": 1},
         {"title": "The Game of Love", "track_num": 2},
         {"title": "Giorgio by Moroder", "track_num": 3}]


def _stub(monkeypatch, items):
    from ripster import resolver as rs

    async def _resolve(url):
        return items

    monkeypatch.setattr(rs, "resolve", _resolve)


def test_tracklist_comes_from_the_shared_resolver(monkeypatch):
    _stub(monkeypatch, ALBUM)
    can = asyncio.run(RU._canonical_via_resolver("https://music.apple.com/us/album/x/1"))
    assert [t["title"] for t in can] == [t["title"] for t in ALBUM]
    assert all(t["available"] is None for t in can), "доступность знает только Deezer"


def test_single_track_has_no_shortfall(monkeypatch):
    _stub(monkeypatch, [{"title": "", "track_num": 1}])
    assert asyncio.run(RU._canonical_via_resolver("https://music.apple.com/us/album/x/1?i=2")) is None


def test_titleless_tracklist_is_not_used(monkeypatch):
    """Список без названий сверить не с чем — честнее ничего не утверждать."""
    _stub(monkeypatch, [{"title": "", "track_num": 1}, {"title": "", "track_num": 2}])
    assert asyncio.run(RU._canonical_via_resolver("https://tidal.com/browse/album/1")) is None


def test_resolver_failure_never_blocks_finalize(monkeypatch):
    from ripster import resolver as rs

    async def _boom(url):
        raise RuntimeError("сеть легла")

    monkeypatch.setattr(rs, "resolve", _boom)
    assert asyncio.run(RU._canonical_via_resolver("https://www.qobuz.com/album/x")) is None


def test_unknown_availability_routes_to_retry_not_to_another_service(monkeypatch):
    _stub(monkeypatch, ALBUM)
    can = asyncio.run(RU._canonical_via_resolver("https://music.apple.com/us/album/x/1"))
    missing = RD.diff_tracklist(can, ["01. Daft Punk - Give Life Back to Music.m4a"])
    assert len(missing) == 2

    cross, retry = RD.classify_missing(missing, "postprocess")
    assert not cross and len(retry) == 2, "просто не докачалось → повтор здесь же"

    cross, retry = RD.classify_missing(missing, "region")
    assert len(cross) == 2 and not retry, "гео-ограничение источника → другой сервис"
