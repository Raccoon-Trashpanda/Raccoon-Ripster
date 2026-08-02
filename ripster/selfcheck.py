"""Самопроверка при запуске: ловит целые классы поломок до того, как их найдёт человек.

ЗАЧЕМ ИМЕННО ТАК. За один день 01–02.08.2026 три поломки оказались из разряда
«всё на месте, но не соединено», и ни одна не падала с ошибкой:

* `hls.js` тянулся с CDN — при провале DNS плеер BBC умирал молча;
* переключатель настроек писал ключ, которого нет в белом списке `/api/config`,
  — нажатие просто ничего не делало;
* Tidal был во всех движках и не был в списке сервисов поиска.

Общее у них: связь между двумя местами разорвана, а каждое место по отдельности
исправно. Тесты такого не ловят, а проверка связей — ловит, и стоит миллисекунды.

ЧТО ЭТО НЕ ДЕЛАЕТ. Не ходит в сеть, не проверяет живость токенов и не чинит
ничего само. Задача — назвать расхождение и не задержать запуск: любая проверка
здесь работает по файлам на диске.
"""
from __future__ import annotations

import re
from pathlib import Path

_BASE = Path(__file__).resolve().parent.parent

# Адрес чужого ресурса, зашитый в код: 'https://…/что-то.js' в любых кавычках.
# Вынесен отдельно — внутри f-строки и вложенных кавычек он нечитаем и ломается.
_EXT_RE = re.compile(r"""['"](https?://[^'"\s]+\.(?:js|css|mjs))['"]""")


def _read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8")
    except Exception:
        return ""


def _check_external_assets() -> tuple[bool, str]:
    """Функциональные ресурсы должны лежать локально.

    Шрифты — исключение: их отсутствие косметическое, подставится системный.
    Скрипт же с чужого домена — это работоспособность, зависящая от чужой сети.
    """
    bad = []

    # 1. Разметка: подключённые теги.
    html = _read(_BASE / "static" / "index.html")
    for u in re.findall(r'<script[^>]+src="(https?://[^"]+)"', html):
        bad.append(("index.html", u))

    # 2. КОД: адрес, зашитый в скрипт, тегом не виден. Ровно так в player.js
    #    жили ДВЕ независимые ссылки на jsdelivr за hls.js — проверка молчала
    #    «внешних скриптов нет», а миксы ждали чужой CDN на каждом переходе
    #    (02.08.2026). Правило одно на весь проект и не зависит от того, какой
    #    именно ресурс и каким способом тянут.
    allow = ("fonts.googleapis.com", "fonts.gstatic.com")   # шрифты — косметика
    for f in sorted((_BASE / "static" / "js").glob("*.js")):
        for u in re.findall(_EXT_RE, _read(f)):
            host = u.split("/")[2] if "//" in u else u
            if not any(a in host for a in allow):
                bad.append((f.name, u))

    if bad:
        return False, "внешние ресурсы: " + ", ".join(
            f"{f} → {u.split('/')[2]}" for f, u in bad[:4])
    return True, "внешних ресурсов нет ни в разметке, ни в коде"


def _check_script_files() -> tuple[bool, str]:
    """Каждый подключённый скрипт существует на диске."""
    html = _read(_BASE / "static" / "index.html")
    refs = re.findall(r'<script[^>]+src="/static/([^"?]+)', html)
    missing = [r for r in refs if not (_BASE / "static" / r).exists()]
    if missing:
        return False, "нет файлов: " + ", ".join(missing[:5])
    return True, f"{len(refs)} скриптов на месте"


def _check_settings_keys() -> tuple[bool, str]:
    """Переключатели настроек должны попадать в белый список `/api/config`.

    Иначе нажатие молча ничего не делает — ровно это случилось с подсказками
    в простое (02.08.2026).
    """
    from ripster.security import config_key_allowed
    txt = ""
    for name in ("settings.html",):
        txt += _read(_BASE / "static" / "views" / name)
    keys = set(re.findall(r"saveSetting\('([a-z0-9\-]+)'", txt))
    bad = sorted(k for k in keys if not config_key_allowed(k))
    if bad:
        return False, "не сохранятся: " + ", ".join(bad[:6])
    return True, f"{len(keys)} ключей записываемы"


def _check_i18n_parity() -> tuple[bool, str]:
    """Каждый русский ключ должен иметь английский.

    Полу-переведённый ключ выглядит как работающий, пока интерфейс не
    переключат — и тогда человек видит чужой язык посреди своего.
    """
    js = _read(_BASE / "static" / "js" / "i18n.js")
    try:
        ru = js.split("\n  ru: {", 1)[1].split("\n  en: {", 1)[0]
        en = js.split("\n  en: {", 1)[1].split("\n  hi: {", 1)[0]
    except IndexError:
        return False, "не разобрать словарь"
    kr = set(re.findall(r"'([\w.\-]+)'\s*:", ru))
    ke = set(re.findall(r"'([\w.\-]+)'\s*:", en))
    miss = sorted(kr - ke)
    if miss:
        return False, f"без английского {len(miss)}: " + ", ".join(miss[:5])
    return True, f"{len(kr)} ключей в двух языках"


def _check_engines() -> tuple[bool, str]:
    """Движки загрузки регистрируются."""
    try:
        # Движок регистрируется при импорте СВОЕГО модуля, а не пакета: без
        # явного обхода реестр пуст в любом процессе, кроме самого приложения,
        # и проверка «падала» на здоровом дереве (поймано тестом 02.08.2026).
        import importlib
        from ripster.engines.registry import REGISTRY
        for f in sorted((_BASE / "ripster" / "engines").glob("*.py")):
            if f.stem.startswith("_") or f.stem == "registry":
                continue
            try:
                importlib.import_module(f"ripster.engines.{f.stem}")
            except Exception:
                pass                       # один сломанный движок не прячет остальные
        if not REGISTRY:
            return False, "реестр пуст"
        return True, f"{len(REGISTRY)}: " + ", ".join(sorted(REGISTRY)[:8])
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def _check_search_services() -> tuple[bool, str]:
    """Сервис, который умеет искать, должен предлагаться в поиске.

    Tidal умел с самого начала и год не значился в списке (01.08.2026).
    """
    disco = _read(_BASE / "ripster" / "routes" / "discovery.py")
    can = set(re.findall(r'service == "([a-z]+)"', disco))
    ui = set(re.findall(r"value: '([a-z]+)'", _read(_BASE / "static" / "js" / "cookies_ui.js")))
    gap = sorted(s for s in can if s and s not in ui and s not in ("label",))
    if gap:
        return False, "умеем, но не предлагаем: " + ", ".join(gap)
    return True, f"{len(ui)} сервисов в поиске"


CHECKS = (
    ("локальные ресурсы", _check_external_assets),
    ("скрипты на диске",  _check_script_files),
    ("ключи настроек",    _check_settings_keys),
    ("языки",             _check_i18n_parity),
    ("движки",            _check_engines),
    ("сервисы поиска",    _check_search_services),
)


_last: list = []


def last() -> list:
    """Результат последнего прогона. Нужен, чтобы интерфейс показал ТОТ ЖЕ
    отчёт, что ушёл в консоль при запуске, а не считал заново своё."""
    return list(_last)


def run(verbose: bool = True) -> list:
    """Прогнать проверки. Возвращает [(имя, ок, подробность)]."""
    global _last
    out = []
    for name, fn in CHECKS:
        try:
            ok, detail = fn()
        except Exception as e:
            ok, detail = False, f"{type(e).__name__}: {e}"
        out.append((name, ok, detail))
    if verbose:
        bad = [x for x in out if not x[1]]
        _say("[самопроверка] " + ("всё сходится" if not bad else f"расхождений: {len(bad)}"))
        for name, ok, detail in out:
            if not ok or verbose == "full":
                _say(f"[самопроверка] {'[ок]' if ok else '[!!]'} {name}: {detail}")
    _last = out
    return out


def _say(line: str) -> None:
    """Печать, которая не может уронить запуск.

    Консоль Windows нередко в cp1251, и один непечатаемый символ роняет вывод
    целиком — поймано на этой же проверке 02.08.2026, когда галочки `✓/✗` дали
    UnicodeEncodeError. Самопроверка, которая ломает старт, хуже отсутствующей,
    поэтому и маркеры теперь ASCII, и печать под защитой.
    """
    try:
        print(line, flush=True)
    except Exception:
        try:
            import sys
            enc = getattr(sys.stdout, "encoding", None) or "ascii"
            print(line.encode(enc, "replace").decode(enc, "replace"), flush=True)
        except Exception:
            pass
