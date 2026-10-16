"""Запасные источники сверки лейбла: 403 у Spotify — не приговор.

Регрессия ровно на ту болячку, из-за которой SEMANTICA 199 не попала в ленту:
`/v1/albums` нашему client-credentials токену отвечает 403, и до этого модуля
отказ выглядел как «лейбл не подтверждён ни у одного релиза», а `last_check`
оставался `null` навечно. Здесь — проверка цепочки: если один источник молчит,
другой обязан подтвердить и поставить отметку.

Сеть не трогаем: `_FUNCS` подменяем заглушками, которые ведут себя как живой
источник — отвечают, отказывают, отдают только метаданные.
"""
from __future__ import annotations

import pytest

from ripster import label_sources as ls


def _seed(src: str, title: str, artist: str, *, date: str = "",
          upc: str = "", metadata_only: bool = False) -> dict:
    s = {"src": src, "ident": f"{src}-{title}", "title": title, "artist": artist,
         "url": f"https://example.invalid/{src}/{title}", "label": "Semantica Records",
         "date": date, "upc": upc}
    if metadata_only:
        s["metadata_only"] = True
    return s


def _st(status: str, *, candidates: int = 0, confirmed: int = 0,
        error: str = "") -> dict:
    return {"status": status, "candidates": candidates, "confirmed": confirmed,
            **({"error": error} if error else {})}


async def _ok(seeds: list, status: str = "ok"):
    confirmed = len(seeds)
    cands = max(confirmed, 1) if confirmed else 0
    return seeds, _st(status, candidates=cands, confirmed=confirmed)


async def _refused(label: str, limit: int, **_kw):
    return [], _st("refused", error="Spotify 403")


async def _empty(label: str, limit: int, **_kw):
    return [], _st("empty")


async def _boom(label: str, limit: int, **_kw):
    raise RuntimeError("симуляция network error")


@pytest.fixture
def stubbed(monkeypatch):
    """Подменить все `_FUNCS` сразу: тесты не должны зависеть от реестра.

    Заодно обнуляем память маршрута («кто отвечал в прошлый раз») — иначе
    `_route_put` из одного теста переставляет порядок обхода в другом, и
    проверка порядка превращается в лотерею.
    """
    monkeypatch.setattr(ls, "_route_cache", {}, raising=False)
    monkeypatch.setattr(ls, "_route_file", None, raising=False)

    def apply(mapping: dict):
        # `label_releases_any` читает `_FUNCS` напрямую через глобаль — так что
        # monkeypatch.setattr на сам словарь, а не на модульную переменную.
        for name in ("bandcamp", "deezer", "beatport", "musicbrainz", "discogs", "tidal"):
            fn = mapping.get(name)
            if fn is None:
                async def _unsupported(_l, _n, **_kw):
                    return [], {"status": "unsupported", "candidates": 0, "confirmed": 0}
                fn = _unsupported
                # замыкание на имя — иначе все `fn` укажут на последнюю заглушку
                fn.__name__ = name
            monkeypatch.setitem(ls._FUNCS, name, fn)
    return apply


@pytest.mark.asyncio
async def test_bandcamp_fills_in_when_everything_else_refuses(stubbed):
    """SEMANTICA 199 есть только на Bandcamp — цепочка обязана его достать."""
    def bc(label, limit, band_url=""):
        return _ok([_seed("bandcamp", "Between Two Worlds. SEMANTICA 199",
                          "Oscar Mulero", date="2026-09-25",
                          upc="196589905993")])
    stubbed({"bandcamp": bc, "deezer": _refused, "beatport": _refused,
             "musicbrainz": _refused, "discogs": _refused})
    seeds, info = await ls.label_releases_any("Semantica Records", limit=6)
    assert seeds and seeds[0]["title"].startswith("Between Two Worlds")
    assert info["via"] == ["bandcamp"], \
        "Bandcamp с датой обязан попасть в `via` — только он ставит last_check"
    assert info["health"] == ("", {})


@pytest.mark.asyncio
async def test_deezer_after_spotify_refusal_and_bandcamp_empty(stubbed):
    """Порядок обхода: Bandcamp первым, но если он пуст — спрашиваем Deezer."""
    async def deezer(label, limit):
        return await _ok([_seed("deezer", "SEMANTICA 198", "Oscar Mulero",
                                date="2026-06-15")])
    stubbed({"bandcamp": _empty, "deezer": deezer})
    seeds, info = await ls.label_releases_any("Semantica Records", limit=6)
    assert seeds and seeds[0]["src"] == "deezer"
    assert info["via"] == ["deezer"]


@pytest.mark.asyncio
async def test_refused_is_not_the_same_as_empty(stubbed):
    """`refused` и `empty` для владельца означают разное: источник сломан vs
    у лейбла нет релизов. Молчаливый пропуск — ровно та ошибка, из-за которой
    «лейбл не найден» и «мы не спрашивали» неразличимы."""
    stubbed({"bandcamp": _boom, "deezer": _boom, "beatport": _boom,
             "musicbrainz": _boom, "discogs": _boom})
    seeds, info = await ls.label_releases_any("Semantica Records", limit=6)
    assert seeds == []
    health_key, _ = info["health"]
    assert health_key == "lbl.health_sources_down", \
        "отказ всех источников — «источники лежат», не «лейбл пустой»"


@pytest.mark.asyncio
async def test_no_creds_says_which_service_needs_login(stubbed):
    """«Нет учётки» — отдельный вердикт от «лейбл пуст». Если хотя бы один
    источник ответил пусто, он имеет право сказать: мы всё-таки спросили и
    там ничего нет, а не «некому было спрашивать»."""
    async def no_creds(label, limit, band_url=""):
        return [], _st("no_creds", error="учётка не настроена")
    stubbed({"bandcamp": no_creds, "deezer": no_creds, "beatport": no_creds,
             "musicbrainz": no_creds, "discogs": no_creds})
    _seeds, info = await ls.label_releases_any("Semantica Records", limit=6)
    assert info["health"][0] == "lbl.health_no_creds"


@pytest.mark.asyncio
async def test_mixed_empty_and_nocreds_is_not_pure_nocreds(stubbed):
    """Deezer ответил пусто, Beatport — «нет учётки». Вердикт не имеет права
    выглядеть как «нужны ключи»: мы СПРОСИЛИ и там пусто. Это «не
    подтверждено», и честно сказать именно это."""
    async def no_creds(label, limit):
        return [], _st("no_creds", error="учётка Beatport не настроена")
    stubbed({"bandcamp": _empty, "deezer": _empty, "beatport": no_creds,
             "musicbrainz": _empty, "discogs": _empty})
    _seeds, info = await ls.label_releases_any("Semantica Records", limit=6)
    assert info["health"][0] == "lbl.health_unverified"


@pytest.mark.asyncio
async def test_metadata_only_does_not_stop_the_chain(stubbed):
    """MusicBrainz подтверждает ФАКТ релиза, но датирует его по стране издания.
    Право называть «когда» у него нет — значит `via` от него пустеет, и цепочка
    идёт дальше."""
    async def mb(label, limit):
        return await _ok([_seed("musicbrainz", "Old Release", "Oscar Mulero",
                                metadata_only=True)])
    async def later(label, limit):
        return await _ok([_seed("deezer", "Between Two Worlds. SEMANTICA 199",
                                "Oscar Mulero", date="2026-09-25")])
    stubbed({"bandcamp": _empty, "musicbrainz": mb, "deezer": later})
    seeds, info = await ls.label_releases_any("Semantica Records", limit=6,
                                              sources=("bandcamp", "musicbrainz",
                                                       "deezer", "discogs"))
    assert "musicbrainz" in info["metadata"]
    assert info["via"] == ["deezer"], "метаданные не имеют права ставить точку"
    titles = {s["title"] for s in seeds}
    assert "Old Release" in titles and any(t.startswith("Between Two") for t in titles), \
        "цепочка не должна терять подтверждённый факт релиза ради даты"


@pytest.mark.asyncio
async def test_stop_after_first_skips_later_sources_when_dated_answer_exists(stubbed):
    """Радар зовёт цепочку коротко: есть источник с датой — и ладно. Фоновый
    обход предзаказов (`stop_after_first=False`) спрашивает всех — там важна
    будущая дата, а её у первого ответившего может и не быть."""
    calls: list = []

    async def bc(label, limit, band_url=""):
        calls.append("bandcamp")
        return await _ok([_seed("bandcamp", "SEMANTICA 199", "Oscar Mulero",
                                date="2026-09-25")])
    async def dz(label, limit):
        calls.append("deezer")
        return await _ok([_seed("deezer", "SEMANTICA 198", "Oscar Mulero",
                                date="2026-06-15")])
    stubbed({"bandcamp": bc, "deezer": dz})

    calls.clear()
    await ls.label_releases_any("Semantica Records", limit=6,
                                stop_after_first=True)
    assert calls == ["bandcamp"]

    calls.clear()
    seeds, info = await ls.label_releases_any("Semantica Records", limit=6,
                                              stop_after_first=False)
    assert calls == ["bandcamp", "deezer"]
    assert info["via"] == ["bandcamp", "deezer"]
    assert len(seeds) == 2


@pytest.mark.asyncio
async def test_stamp_label_checks_writes_last_check_when_via_present(stubbed):
    """Самоизлечение: лейблы с `last_check=null` получают отметку, как только
    ХОТЬ ОДИН источник дал дату. Иначе они навечно выглядят непроверенными."""
    async def bc(label, limit, band_url=""):
        return await _ok([_seed("bandcamp", "SEMANTICA 199", "Oscar Mulero",
                                date="2026-09-25")])
    stubbed({"bandcamp": bc})
    _seeds, info = await ls.label_releases_any("Semantica Records")
    wl = [{"kind": "label", "name": "Semantica Records", "last_check": None},
          {"kind": "label", "name": "Other Label", "last_check": None},
          {"kind": "artist", "name": "Semantica Records"}]
    assert ls.stamp_label_checks(wl, "Semantica Records", info) is True
    assert wl[0]["last_check"], "last_check так и остался бы null"
    assert wl[0].get("label_verified_via") == "bandcamp"
    assert wl[1]["last_check"] is None, "чужой лейбл задет быть не мог"
    assert "last_check" not in wl[2], "артиста не чем отмечать — это не лейбл"


@pytest.mark.asyncio
async def test_stamp_label_checks_does_nothing_when_chain_was_empty(stubbed):
    """Отказ источника не имеет права выглядеть как «проверено, пусто»."""
    stubbed({"bandcamp": _refused, "deezer": _refused})
    _seeds, info = await ls.label_releases_any("Semantica Records")
    wl = [{"kind": "label", "name": "Semantica Records", "last_check": None}]
    assert ls.stamp_label_checks(wl, "Semantica Records", info) is False
    assert wl[0]["last_check"] is None
