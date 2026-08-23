# -*- coding: utf-8 -*-
"""Кнопка «Fix gamdl deps» обязана закреплять construct и проверять ИМПОРТОМ.

Зачем тест на текст функции, а не на её поведение. Эта правка — главное
содержание релиза 3.6.3: без пина `construct==2.8.8` pywidevine не импортируется,
а AMD импортирует его на уровне модуля, поэтому Apple умирает на этапе boot, до
единого сетевого запроса. Правка была написана (89487d7) и **молча исчезла**
через пять часов (30bfb0b): следующая правка того же файла писалась поверх
устаревшей копии и унесла 34 строки заодно со своей целью. Поймать это можно было
только чтением diff'а, а его никто не читал — установщик уже собирали.

Гонять сам pip в тесте нельзя (сеть, минуты, порча окружения), поэтому проверяем
ИСХОДНИК функции. Тест ловит ровно тот класс отказа, который случился: запись
поверх устаревшей копии. Подмена pip'а моком его бы НЕ поймала — снесённого кода
просто нет, и мок отработал бы по оставшимся вызовам без единой жалобы.
"""
import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ripster.routes.setup import fix_gamdl_deps

SRC = inspect.getsource(fix_gamdl_deps)


def test_construct_is_pinned_to_288():
    assert "construct==2.8.8" in SRC, (
        "пин construct==2.8.8 пропал из fix-gamdl-deps — Apple не будет качать "
        "на свежей установке (см. 3.6.3 / 89487d7)"
    )


def test_pin_uses_no_deps():
    # Без --no-deps pip тянет за construct зависимости и может снова сдвинуть
    # ту самую версию, ради которой всё и делается.
    i = SRC.index("construct==2.8.8")
    assert "--no-deps" in SRC[i:i + 300], "пин construct поставлен без --no-deps"


def test_verified_by_import_not_by_file():
    assert "from pywidevine.device import Device" in SRC, (
        "проверка импортом пропала — наличие файла ничего не доказывает, "
        "ломается именно импорт"
    )


def test_import_failure_is_reported_as_error():
    # Проверка обязана уметь ПРОВАЛИТЬСЯ и сказать об этом. «Зелёный отчёт при
    # сломанной подсистеме» — повторяющийся класс багов этого проекта.
    i = SRC.index("from pywidevine.device import Device")
    tail = SRC[i:i + 900]
    assert '"error"' in tail, "провал импорта pywidevine не сообщается как ошибка"
