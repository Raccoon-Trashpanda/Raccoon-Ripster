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
# Словари, которые надо сложить, прежде чем судить о ключах: продукт и панель.
_DICTS = ("js/i18n.js", "panel/i18n.panel.js")
# Где ищутся ключи. Панель живёт отдельным документом со своей разметкой, и
# пока её файл не в списке, пропущенная строка в мобильном UI не ловилась ничем.
_SRC_GLOBS = ("js/*.js", "panel/*.js")
_DICT_NAMES = {Path(p).name for p in _DICTS}

_BLOCK = re.compile(r"^\s*(?:var\s+|const\s+|let\s+)?(ru|en|hi|ja|zh)\s*[:=]\s*\{", re.M)
# Ключи таблиц пишут и в '', и в "" (так живёт блок ga.*). Regex только с ''
# объявлял существующие двойные кавычки «отсутствующими ключами» — 54 ложных
# находки на ровном месте.
_ENTRY = re.compile(r"""['"]([a-zA-Z][a-zA-Z0-9_.]*)['"]\s*:""")
# t('key') / ti('key', {...}) — только литералы; t('cc.' + x) намеренно не ловим
_CALL = re.compile(r"\b(?:t|ti)\(\s*'([a-zA-Z][a-zA-Z0-9_.]*)'\s*[,)]")
_ATTR = re.compile(r'data-i18n(?:-html|-ph|-title)?="([a-zA-Z][a-zA-Z0-9_.]*)"')


def tables(paths) -> dict[str, set[str]]:
    """Ключи по языковым блокам: {'ru': {...}, 'en': {...}, ...}.

    Файлов словаря может быть несколько: продукт (js/i18n.js) и мобильная
    панель (panel/i18n.panel.js) дополняют LANG одним Object.assign, поэтому и
    проверять их надо одним множеством — иначе ключи панели считались бы
    отсутствующими.
    """
    out: dict[str, set[str]] = {}
    for i18n in paths:
        src = i18n.read_text(encoding="utf-8")
        marks = [(m.group(1), m.start()) for m in _BLOCK.finditer(src)]
        for name, _ in marks:
            out.setdefault(name, set())
        for m in _ENTRY.finditer(src):
            prior = [n for n, p in marks if p < m.start()]
            if prior:
                out[prior[-1]].add(m.group(1))
    return out


def scan(tree: Path) -> list[str]:
    dicts = [tree / rel for rel in _DICTS if (tree / rel).exists()]
    if not dicts:
        return [f"{tree}: нет js/i18n.js"]
    tbl = tables(dicts)
    ru, en = tbl.get("ru", set()), tbl.get("en", set())
    problems: list[str] = []

    used: dict[str, set[str]] = {}
    for pat in _SRC_GLOBS:
        for f in sorted(tree.glob(pat)):
            if f.name in _DICT_NAMES:
                continue
            src = f.read_text(encoding="utf-8", errors="replace")
            # Строчные комментарии выбрасываем: в них живут ПРИМЕРЫ вида t('i18n.key'),
            # и без этого сканер стабильно даёт одну ложную находку, а инструмент,
            # который всегда красный, перестают читать.
            src = re.sub(r"^\s*//.*$", "", src, flags=re.M)
            for k in _CALL.findall(src):
                used.setdefault(k, set()).add(f"{f.parent.name}/{f.name}")
    for f in sorted([tree / "index.html", *tree.glob("views/*.html"),
                     *tree.glob("panel/*.html")]):
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
