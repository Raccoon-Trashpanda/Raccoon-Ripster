"""Golden-вектор Temari: ровно та расшифровка, что проверялась до интеграции.

Фикстуры (`tests/fixtures/lite_golden/`) — дамп `data`-объекта lite `/key` и
кусок реального сэмпла ct→pt. Если Temari не установлена, тесты пропускаются:
в CI без колеса движок Lite всё равно не поднимается.
"""
import json
from pathlib import Path

import pytest

from ripster import lite, lite_shim

FIXTURES = Path(__file__).parent / "fixtures" / "lite_golden"

requires_temari = pytest.mark.skipif(
    not lite_shim._crypto.available(),
    reason="Temari не установлена (pip install temari==0.3.1)")


@requires_temari
def test_golden_template_decrypts_sample():
    template = json.loads((FIXTURES / "template.json").read_bytes())
    ct = (FIXTURES / "sample.ct").read_bytes()
    pt = (FIXTURES / "sample.pt").read_bytes()
    crypto = lite_shim.TemariCrypto()
    assert crypto.decrypt(template, ct) == pt


@requires_temari
def test_aligned_prefix_decrypts_as_in_production():
    """runv2 усечёт хвост до 16 байт и шлёт субсэмпл целиком от нулевого
    байта. Измерено: выровненный префикс сходится, а произвольный серединный
    чанк — нет (keystream считается от начала буфера); тест держит именно
    производственную форму, а не выдуманную «chunk-реплицируемость»."""
    template = json.loads((FIXTURES / "template.json").read_bytes())
    ct = (FIXTURES / "sample.ct").read_bytes()
    pt = (FIXTURES / "sample.pt").read_bytes()
    crypto = lite_shim.TemariCrypto()
    aligned = len(ct) & ~0xf
    assert crypto.decrypt(template, ct[:aligned]) == pt[:aligned]


def test_template_without_ctx_is_honest_error():
    """Проверка ctx/state предшествует импорту Temari — тест живёт и без неё."""
    crypto = lite_shim.TemariCrypto()
    with pytest.raises(lite.LiteError) as ei:
        crypto.decrypt({"contentKey": "aa"}, b"x")
    assert "шаблон" in str(ei.value)
