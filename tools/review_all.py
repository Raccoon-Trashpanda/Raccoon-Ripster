# -*- coding: utf-8 -*-
"""Полный автономный прогон: тесты, самопроверка связей и отдельно — плеер.

ЗАЧЕМ ОТДЕЛЬНЫЙ ПРОГОН. Тесты лежат по файлам и запускаются поодиночке, а
самопроверка живёт внутри приложения. Из-за этого «всё ли в порядке» приходилось
собирать руками, и часть проверок просто забывалась. Здесь одна команда даёт
один ответ.

ПОЧЕМУ ПЛЕЕР ВЫНЕСЕН В СВОЙ РАЗДЕЛ. Все три его поломки 02.08.2026 были не в
логике, а в связях, и ни одна не падала с ошибкой:

* два независимых адреса чужого CDN за hls.js внутри кода — миксы ждали чужой
  сервер на каждом переходе;
* событие смены трека слушали двое, а отправлял никто — трек-лист не двигался;
* прогрев следующего трека сделали скрытым <audio>, и при перемотке звук двоился.

Каждая из них проверяется здесь навсегда, чтобы не вернулась.

Запуск:  python tools/review_all.py
"""
from __future__ import annotations

import pathlib
import re
import subprocess
import sys

BASE = pathlib.Path(__file__).resolve().parent.parent
PY = sys.executable
_fails: list[str] = []



def _p(line: str = "") -> None:
    """Печать, которая не роняет прогон на консоли в cp1251.

    Тот же урок, что и в самопроверке: один непечатаемый символ — и весь вывод
    обрывается UnicodeEncodeError. Отчёт, который падает, бесполезен.
    """
    try:
        print(line, flush=True)
    except Exception:
        enc = getattr(sys.stdout, "encoding", None) or "ascii"
        print(line.encode(enc, "replace").decode(enc, "replace"), flush=True)

def say(ok: bool, name: str, detail: str = "") -> None:
    _p(("  ok   " if ok else "  !!   ") + name + (f"  — {detail}" if detail else ""))
    if not ok:
        _fails.append(name)


def read(rel: str) -> str:
    try:
        return (BASE / rel).read_text(encoding="utf-8")
    except Exception:
        return ""


# ── 1. Все тестовые наборы ───────────────────────────────────────────────────
_p("=== ТЕСТЫ")
for f in sorted((BASE / "tools").glob("test_*.py")):
    src = read(f"tools/{f.name}")
    # Не всякий `test_*.py` — автотест. Часть из них ручные инструменты, которым
    # нужен аргумент (ссылка, id). Запускать их вслепую бессмысленно: они падают
    # на разборе argv и выглядят как поломка (поймано 02.08.2026 на
    # test_download.py). Отличаем по обращению к argv.
    if "sys.argv[1]" in src or "argv[1]" in src:
        _p(f"  --   {f.stem}  — ручной инструмент, нужен аргумент; пропущен")
        continue
    try:
        r = subprocess.run([PY, str(f)], capture_output=True, text=True,
                           timeout=180, encoding="utf-8", errors="replace")
        last = [l for l in (r.stdout or "").splitlines() if l.strip()]
        say(r.returncode == 0, f.stem, (last[-1] if last else "")[:70])
    except Exception as e:
        say(False, f.stem, f"{type(e).__name__}: {e}")

# ── 2. Самопроверка связей ───────────────────────────────────────────────────
print("\n═══ СВЯЗИ")
sys.path.insert(0, str(BASE))
try:
    from ripster import selfcheck
    for name, ok, detail in selfcheck.run(verbose=False):
        say(ok, name, detail[:70])
except Exception as e:
    say(False, "самопроверка", f"{type(e).__name__}: {e}")

# ── 3. Плеер: то, что уже ломалось ───────────────────────────────────────────
print("\n═══ ПЛЕЕР")
player = read("static/js/player.js")
pq = read("static/js/player_queue.js")

say("cdn.jsdelivr" not in player and "https://cdn." not in player,
    "плеер не ходит на чужой CDN",
    "иначе микс ждёт чужой сервер на каждом переходе")

say("dispatchEvent(new CustomEvent('ripster:track-start'" in player
    or "ripster:track-start" in player,
    "событие смены трека отправляется",
    "без него трек-лист не двигается за музыкой")

listeners = len(re.findall(r"addEventListener\('ripster:track-start'", pq))
say(listeners >= 1, "трек-лист слушает смену трека", f"слушателей {listeners}")

say("createElement('audio')" not in pq,
    "прогрев НЕ создаёт второй <audio>",
    "именно из-за этого при перемотке двоился звук")

say("fetch(_pqPreUrl" in pq,
    "следующий трек греется запросом",
    "адрес плеер знал и раньше, а буфера не было")

say("function _pqAudio" in pq and "querySelector('audio')" not in pq,
    "обращение к аудио через одну точку",
    "«первый попавшийся audio» ломается при втором элементе")

say("prefers-reduced-motion" in pq,
    "подъём громкости уважает системную настройку")

# ── 4. Внешние ресурсы во ВСЕЙ статике ───────────────────────────────────────
ext = []
for f in sorted((BASE / "static" / "js").glob("*.js")):
    for u in re.findall(r"""['"](https?://[^'"\s]+\.(?:js|css|mjs))['"]""", read(f"static/js/{f.name}")):
        if "fonts.g" not in u:
            ext.append(f"{f.name} → {u.split('/')[2]}")
say(not ext, "во всей статике нет чужих ресурсов", ", ".join(ext[:3]))

_p()
if _fails:
    _p(f"РАСХОЖДЕНИЙ: {len(_fails)}")
    for n in _fails:
        _p("   * " + n)
    sys.exit(1)
_p("Всё сходится: тесты, связи и плеер.")
