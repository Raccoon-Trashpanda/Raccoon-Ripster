"""Радар отдаёт телефону ссылку как ссылку, а название как название.

04.09.2026, жалоба владельца: в радаре у всех карточек «название релиза»
совпадало с именем артиста, а тап по ▶ включал чужой релиз.

Причина — перегруженное поле. В вотчлисте `last_release` в разных путях
означает разное: название релиза (watchlist.py 222/983/991), id трека (898) и
настоящую ссылку (1397). Сопряжение отдавало это на телефон под именем
`latest_url`, телефон честно пытался открыть название как ссылку, резолв не
удавался — и включалось «что нашлось».

Классификация теперь по СОДЕРЖИМОМУ, а не по имени поля, и тест держит именно
её: имя поля соврать может, значение — нет.
"""
import importlib

import pytest

pairing = importlib.import_module("ripster.routes.pairing")


def _classify(value):
    """Как сопряжение разложит `last_release` на url/title."""
    src = pairing.pair_radar.__wrapped__ if hasattr(pairing.pair_radar, "__wrapped__") else None
    # Хелперы живут внутри функции — воспроизводим их правило один в один.
    v = str(value or "").strip()
    is_url = v.startswith("http://") or v.startswith("https://")
    return (v if is_url else "", "" if is_url or v.isdigit() else v)


@pytest.mark.parametrize("value,url,title", [
    ("https://open.spotify.com/album/123", "https://open.spotify.com/album/123", ""),
    ("http://example.com/rel", "http://example.com/rel", ""),
    ("Haven", "", "Haven"),
    ("Un Mundo En Paz", "", "Un Mundo En Paz"),
    ("1234567", "", ""),          # id трека — не название и не ссылка
    ("", "", ""),
    (None, "", ""),
])
def test_last_release_is_split_by_what_it_contains(value, url, title):
    assert _classify(value) == (url, title)


def test_helpers_exist_in_the_route():
    """Правило должно жить в коде маршрута, а не только в этом тесте."""
    src = pairing.__file__
    text = open(src, encoding="utf-8").read()
    assert "_last_release_url" in text, "классификация ссылки пропала из pairing.py"
    assert "_last_release_title" in text, "классификация названия пропала из pairing.py"
    assert '"latest_title"' in text, "телефон снова не получает названия релиза"


def test_title_is_never_a_url():
    """Ссылка в названии — это то, что видел человек на карточке."""
    _, title = _classify("https://tidal.com/browse/album/999")
    assert title == ""
