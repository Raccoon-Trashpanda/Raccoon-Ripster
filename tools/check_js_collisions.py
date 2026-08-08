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

# views.js fetches a fragment for every name in _VIEW_FILES that has a matching
# `id="view-<name>"` container in index.html. A container without a file on disk
# is a 404 at boot — and until 08.08.2026 that killed the WHOLE app for public
# users (Promise.all rejected → the first await in app.js's load handler threw →
# no WebSocket, no version, black content area). Гейт против повтора.
_VIEW_LIST = re.compile(r"_VIEW_FILES\s*=\s*\[(.*?)\]", re.S)
_VIEW_NAME = re.compile(r"'([a-z0-9-]+)'")
_VIEW_SLOT = re.compile(r'id="view-([a-z0-9-]+)"')


def scan_views(index_path: Path) -> list[str]:
    """Каждый контейнер view-* в этом index.html должен иметь файл фрагмента."""
    problems: list[str] = []
    root = index_path.parent
    views_js = root / "js" / "views.js"
    if not views_js.exists():
        return [f"{index_path}: нет js/views.js — фрагменты не проверены"]
    m = _VIEW_LIST.search(views_js.read_text(encoding="utf-8"))
    if not m:
        return [f"{views_js}: не разобрал _VIEW_FILES — фрагменты не проверены"]
    known = set(_VIEW_NAME.findall(m.group(1)))
    slots = _VIEW_SLOT.findall(index_path.read_text(encoding="utf-8"))
    for name in slots:
        if name not in known:
            continue  # контейнер есть, но views.js его не грузит — не наша забота
        if not (root / "views" / f"{name}.html").exists():
            problems.append(
                f"{index_path}: есть контейнер view-{name}, но НЕТ файла "
                f"views/{name}.html — 404 на старте (см. ripster-frontend-file-drift)"
            )
    return problems


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
    # Вывод содержит ✗ и кириллицу: под cp1251-консолью Windows print падал
    # UnicodeEncodeError и прятал НАЙДЕННЫЕ проблемы за трейсбеком.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    all_problems: list[str] = []
    for rel in _INDEXES:
        p = _REPO / rel
        if not p.exists():
            print(f"пропуск: нет {rel}")
            continue
        found = scan(p) + scan_views(p)
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
