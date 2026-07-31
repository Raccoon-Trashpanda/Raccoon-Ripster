r"""Сканер коллизий фронтенда: ловит класс багов «один лишний скрипт убивает целый файл».

Два `<script>` объявляют одно имя через let/const/class на верхнем уровне — второй
падает с `SyntaxError: Identifier 'X' has already been declared` и НЕ ВЫПОЛНЯЕТСЯ
ЦЕЛИКОМ, унося все свои функции. Падает при этом совсем другое место (`X is not
defined`), поэтому по симптому причина не ищется.

Так 31.07.2026 у каждого публичного пользователя был мёртв `cookies_ui.js`: в
зеркале `github_setup/` остался слитый локально `search.js`, и оба объявляли
`let _srchItems`.

`node --check` это НЕ ловит: он проверяет файлы поодиночке, а коллизия существует
только в совокупности загруженных скриптов.

Заодно проверяет, что каждый подключённый файл вообще есть на диске.

Запуск:  python tools/check_js_collisions.py
Код возврата: 0 — чисто, 1 — есть проблемы (годится как гейт перед сборкой).
"""
from __future__ import annotations

import collections
import re
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_INDEXES = ("static/index.html", "github_setup/static/index.html")

# Только объявления с НАЧАЛА строки: вложенные let/const внутри функций живут в
# своей области видимости и не конфликтуют.
_DECL = re.compile(r"^(?:let|const|class)\s+([A-Za-z_$][\w$]*)", re.M)
_SCRIPT = re.compile(r'<script src="/static/(js/[^"?]+)')


def scan(index_path: Path) -> list[str]:
    problems: list[str] = []
    root = index_path.parent
    order = _SCRIPT.findall(index_path.read_text(encoding="utf-8"))
    owner: dict[str, str] = {}
    for rel in order:
        f = root / rel
        if not f.exists():
            problems.append(f"{index_path}: подключён несуществующий файл {rel}")
            continue
        src = f.read_text(encoding="utf-8", errors="replace")
        for m in _DECL.finditer(src):
            name = m.group(1)
            first = owner.get(name)
            if first is not None and first != rel:
                problems.append(
                    f"{index_path}: '{name}' объявлен и в {first}, и в {rel} — "
                    f"{rel} упадёт ЦЕЛИКОМ (SyntaxError) и потеряет все свои функции"
                )
            else:
                owner.setdefault(name, rel)
    return problems


def main() -> int:
    all_problems: list[str] = []
    for rel in _INDEXES:
        p = _REPO / rel
        if not p.exists():
            print(f"пропуск: нет {rel}")
            continue
        found = scan(p)
        print(f"{rel}: {'ПРОБЛЕМЫ' if found else 'чисто'}")
        all_problems += found
    for line in all_problems:
        print("  ✗ " + line)
    if all_problems:
        print(f"\nвсего проблем: {len(all_problems)} — см. скилл ripster-frontend-file-drift")
        return 1
    print("\nколлизий верхнеуровневых let/const/class нет, все подключённые файлы на месте")
    return 0


if __name__ == "__main__":
    sys.exit(main())
