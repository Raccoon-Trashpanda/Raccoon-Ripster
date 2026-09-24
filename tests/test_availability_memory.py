"""Матрица не должна забывать то, что уже выяснила.

Один релиз спрашивают из разных мест с разной полнотой данных: карточка — со
штрихкодом и названием, рантайм после загрузки — только со штрихкодом. Пока
запись ПЕРЕПИСЫВАЛАСЬ, бедный вызов затирал ISRC, добытый богатым, и следующая
проверка отвечала «не спрашивал, нет ISRC» про то, что уже знала.
"""
import pytest

from ripster import availability as av


@pytest.fixture(autouse=True)
def _clean(tmp_path):
    av.configure({}, tmp_path)
    av._cache, av._loaded = {}, True
    yield
    av._cache, av._loaded = {}, False


async def _no_probe(service, upc, isrc, title="", artist="", expect=None):
    return {"available": False, "reason": av.REASON_NOT_YET, "checked_ts": 0}


@pytest.mark.asyncio
async def test_poorer_call_does_not_erase_what_a_richer_one_learned(monkeypatch):
    monkeypatch.setattr(av, "_probe_one", _no_probe)
    await av.matrix(upc="885288068405", isrc="GXHJ62600008",
                    title="U & I", artist="Culture Shock")
    # Ключ спрашиваем у самого модуля, а не пишем строкой: с 06.09.2026
    # штрихкод нормализуется до GTIN-14 (`upc:00885288068405`), потому что
    # разрядность зависит от того, кто назвал релиз, и один альбом жил в
    # кэше под тремя ключами сразу. Строка в тесте застыла бы на старой
    # форме и проверяла бы уже не то.
    rec = av._cache[av._key("885288068405", "", "", "")]
    assert rec["isrc"] == "GXHJ62600008"

    # Рантайм после загрузки знает только штрихкод.
    await av.matrix(upc="885288068405")
    rec = av._cache[av._key("885288068405", "", "", "")]
    assert rec["isrc"] == "GXHJ62600008", "ISRC затёрт бедным вызовом"
    assert rec["title"] == "U & I", "название затёрто бедным вызовом"
    assert rec["artist"] == "Culture Shock"


@pytest.mark.asyncio
async def test_known_isrc_is_used_even_when_its_source_became_unavailable(monkeypatch):
    """ISRC — свойство ЗАПИСИ, а не сервиса.

    Раньше он поднимался из кэша только при живом «доступен» у сервиса-донора, и
    стоило донору отказать по правам, как Qobuz с Tidal снова получали «нечем
    спросить» — при том что нужный ISRC лежал в этой же записи.
    """
    asked = {}

    async def _probe(service, upc, isrc, title="", artist="", expect=None):
        asked[service] = isrc
        return {"available": False, "reason": av.REASON_NOT_YET, "checked_ts": 0}

    monkeypatch.setattr(av, "_probe_one", _probe)
    await av.matrix(upc="1", isrc="ISRC1", title="T")
    # Донор отказал по правам — запись свежая, перепроверять его не будем.
    av.record_outcome("beatport", "entitlement", upc="1")
    asked.clear()

    await av.matrix(upc="1")
    assert asked.get("qobuz") == "ISRC1"
    assert asked.get("tidal") == "ISRC1"


@pytest.mark.asyncio
async def test_download_verdict_survives_a_later_catalog_probe(monkeypatch):
    """«Каталог говорит есть» не должно перебивать «мы пробовали и не смогли»
    раньше, чем истечёт срок вердикта."""
    async def _probe_says_available(service, upc, isrc, title="", artist="", expect=None):
        return {"available": True, "url": "u", "checked_ts": 0}

    monkeypatch.setattr(av, "_probe_one", _probe_says_available)
    av.record_outcome("beatport", "entitlement", upc="2", title="T")
    m = await av.matrix(upc="2")
    assert m["services"]["beatport"]["reason"] == av.REASON_NO_RIGHTS
    assert "beatport" not in m["available_in"]
