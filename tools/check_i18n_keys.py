r"""Проверка ключей i18n: и в JS (`t()`/`ti()`), и в разметке (`data-i18n*`).

Зачем. Если ключ упомянут, но в таблице его НЕТ, `applyLang()` не показывает
сырой ключ — он оставляет авторский текст, который у нас русский. То есть
пропущенный ключ выглядит РОВНО как захардкоженная строка, и владелец дважды
сообщал «опять хардкод», хотя разметка была правильная.

Отдельно проверяется английская таблица: ключ, который есть только в `ru`,
отдаёт в английском интерфейсе русскую строку (fallback locale → en → ru).
Это второй способ получить «хардкод», не имея хардкода.

Запуск:  python tools/check_i18n_keys.py
Код возврата: 0 — чисто, 1 — есть проблемы (годится как гейт перед сборкой).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_TREES = ("static", "github_setup/static")

_BLOCK = re.compile(r"^\s*(ru|en|hi|ja|zh)\s*:\s*\{", re.M)
_ENTRY = re.compile(r"'([a-zA-Z][a-zA-Z0-9_.]*)'\s*:")
# t('key') / ti('key', {...}) — только литералы; t('cc.' + x) намеренно не ловим
_CALL = re.compile(r"\b(?:t|ti)\(\s*'([a-zA-Z][a-zA-Z0-9_.]*)'\s*[,)]")
_ATTR = re.compile(r'data-i18n(?:-html|-ph|-title)?="([a-zA-Z][a-zA-Z0-9_.]*)"')


def tables(i18n: Path) -> dict[str, set[str]]:
    """Ключи по языковым блокам: {'ru': {...}, 'en': {...}, ...}."""
    src = i18n.read_text(encoding="utf-8")
    marks = [(m.group(1), m.start()) for m in _BLOCK.finditer(src)]
    out: dict[str, set[str]] = {name: set() for name, _ in marks}
    for m in _ENTRY.finditer(src):
        prior = [n for n, p in marks if p < m.start()]
        if prior:
            out[prior[-1]].add(m.group(1))
    return out


def scan(tree: Path) -> list[str]:
    i18n = tree / "js" / "i18n.js"
    if not i18n.exists():
        return [f"{tree}: нет js/i18n.js"]
    tbl = tables(i18n)
    ru, en = tbl.get("ru", set()), tbl.get("en", set())
    problems: list[str] = []

    used: dict[str, set[str]] = {}
    for f in sorted(tree.glob("js/*.js")):
        if f.name == "i18n.js":
            continue
        src = f.read_text(encoding="utf-8", errors="replace")
        # Строчные комментарии выбрасываем: в них живут ПРИМЕРЫ вида t('i18n.key'),
        # и без этого сканер стабильно даёт одну ложную находку, а инструмент,
        # который всегда красный, перестают читать.
        src = re.sub(r"^\s*//.*$", "", src, flags=re.M)
        for k in _CALL.findall(src):
            used.setdefault(k, set()).add(f.name)
    for f in sorted([tree / "index.html", *tree.glob("views/*.html")]):
        if not f.exists():
            continue
        for k in _ATTR.findall(f.read_text(encoding="utf-8", errors="replace")):
            used.setdefault(k, set()).add(f.name)

    for k, where in sorted(used.items()):
        src = ", ".join(sorted(where))
        if k not in ru and k not in en:
            problems.append(f"{tree}: ключа '{k}' НЕТ в таблице ({src}) — "
                            f"в интерфейсе останется авторский текст")
        elif k not in en:
            problems.append(f"{tree}: ключ '{k}' только в ru ({src}) — "
                            f"английский интерфейс получит русскую строку")
        elif k not in ru:
            problems.append(f"{tree}: ключ '{k}' только в en ({src}) — "
                            f"русский интерфейс получит английскую строку")
    return problems


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    all_problems: list[str] = []
    for rel in _TREES:
        p = _REPO / rel
        if not p.exists():
            print(f"пропуск: нет {rel}")
            continue
        found = scan(p)
        print(f"{rel}: {'ПРОБЛЕМЫ (' + str(len(found)) + ')' if found else 'чисто'}")
        all_problems += found
    for line in all_problems:
        print("  ✗ " + line)
    if all_problems:
        print(f"\nвсего проблем: {len(all_problems)} — см. скилл ripster-i18n")
        return 1
    print("\nвсе упомянутые ключи есть и в ru, и в en")
    return 0


if __name__ == "__main__":
    sys.exit(main())
