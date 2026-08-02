# -*- coding: utf-8 -*-
"""Качество одного сервиса не должно переписывать настройку другого.

Разбор 01.08.2026: в очередь приехала задача с Apple-ссылкой и качеством `27` —
это формат Qobuz. Восстановить путь по логам не удалось, но в коде нашлась мина:

    saveSetting(keyMap[svc] || 'quality', sel.value)

Глобальный ключ `quality` — это качество APPLE. Любая карточка сервиса без
собственного ключа (BBC, SoundCloud, Spotify и любая будущая) переписывала им
настройку Apple, и следующая загрузка Apple уходила с чужим качеством.

Проверяем сам файл: запасного пути на глобальный ключ быть не должно, а список
сервисов со своим ключом — совпадать с тем, что предлагает интерфейс.

Запуск:  python tools/test_quality_key.py
"""
from __future__ import annotations

import pathlib
import re
import sys

BASE = pathlib.Path(__file__).resolve().parent.parent
_fails: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("  OK  " if ok else "ПРОВАЛ") + f"  {name}")
    if not ok:
        _fails.append(name)
        if detail:
            print(f"        {detail}")


src = (BASE / "static" / "js" / "sc_tab.js").read_text(encoding="utf-8")
fn = src.split("function _relSetQuality", 1)[1].split("\n}", 1)[0]
# Комментарии выбрасываем: в них ЦИТИРУЕТСЯ убранная мина, и проверка ловила
# собственное пояснение вместо кода (поймано первым же прогоном 02.08.2026).
code = "\n".join(ln for ln in fn.splitlines() if not ln.strip().startswith("//"))

check("мины `|| 'quality'` больше нет в КОДЕ", "|| 'quality'" not in code,
      "любой сервис без своего ключа снова переписывал бы качество Apple")
check("без своего ключа ничего не сохраняем", "if (!key)" in code and "return;" in code)
check("apple по-прежнему пишет в свой ключ", "apple: 'quality'" in code)

# Сервисы, у которых есть собственный ключ качества.
own = set(re.findall(r"(\w+): '([\w-]+)-quality'", code))
names = {n for n, _ in own} | {"apple"}
check("ключи есть у пяти сервисов минимум", len(names) >= 6, str(sorted(names)))

# У всех, кого предлагает поиск, качество либо своё, либо фиксированное.
ui = set(re.findall(r"value: '([a-z]+)'",
                    (BASE / "static" / "js" / "cookies_ui.js").read_text(encoding="utf-8")))
rq = (BASE / "static" / "js" / "lightbox.js").read_text(encoding="utf-8")
fixed = set(re.findall(r"(\w+): '(?:mp3|hq)'", rq))
gap = sorted(s for s in ui if s not in names and s not in fixed and s != "spotify")
check("у каждого сервиса поиска качество определено", not gap, f"без качества: {gap}")

print()
if _fails:
    print(f"ПРОВАЛЕНО: {len(_fails)} — {', '.join(_fails)}")
    sys.exit(1)
print("Все проверки пройдены — качество одного сервиса не течёт в другой.")
