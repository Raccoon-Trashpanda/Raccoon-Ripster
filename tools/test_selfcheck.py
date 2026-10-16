# -*- coding: utf-8 -*-
"""Самопроверка должна ЛОВИТЬ поломки, а не просто зеленеть.

Проверка, которая всегда говорит «всё сходится», бесполезна и опаснее
отсутствующей — на неё начинают полагаться. Поэтому здесь каждая проверка
проверяется дважды: на исправном дереве и на нарочно испорченном.

Запуск:  python tools/test_selfcheck.py
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from ripster import selfcheck as SC  # noqa: E402

_fails: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("  OK  " if ok else "ПРОВАЛ") + f"  {name}")
    if not ok:
        _fails.append(name)
        if detail:
            print(f"        {detail}")


# 1. На настоящем дереве всё должно сходиться.
res = SC.run(verbose=False)
bad = [(n, d) for n, ok, d in res if not ok]
check("на живом дереве расхождений нет", not bad, str(bad))
check("проверок не меньше шести", len(res) >= 6, f"их {len(res)}")

# 2. Каждая проверка обязана падать, когда её условие нарушено.
real_read = SC._read


def with_fake(path_part: str, replace_from: str, replace_to: str):
    """Подсунуть испорченную версию одного файла."""
    def fake(p: pathlib.Path) -> str:
        txt = real_read(p)
        if path_part in str(p):
            return txt.replace(replace_from, replace_to, 1)
        return txt
    return fake


# внешний скрипт вернулся
SC._read = with_fake("index.html", "<head",
                     '<script src="https://cdn.example.com/x.js"></script><head')
ok, detail = SC._check_external_assets()
check("ловит внешний скрипт", not ok, detail)

# подключён несуществующий файл
SC._read = with_fake("index.html", "<head",
                     '<script src="/static/js/нет-такого.js"></script><head')
ok, detail = SC._check_script_files()
check("ловит отсутствующий скрипт", not ok, detail)

# переключатель с ключом, которого нет в белом списке
SC._read = with_fake("settings.html", "<div", "<div onclick=\"saveSetting('nikogda-ne-belyj-klyuch',1)\"")
ok, detail = SC._check_settings_keys()
check("ловит несохраняемый переключатель", not ok, detail)

SC._read = real_read

# 3. Печать не должна ронять запуск даже на консоли без юникода.
try:
    SC._say("проверка ✓ ✗ — юникод")
    check("печать не падает на юникоде", True)
except Exception as e:
    check("печать не падает на юникоде", False, str(e))

print()
if _fails:
    print(f"ПРОВАЛЕНО: {len(_fails)} — {', '.join(_fails)}")
    sys.exit(1)
print("Все проверки пройдены — самопроверка и зеленеет по делу, и краснеет по делу.")
