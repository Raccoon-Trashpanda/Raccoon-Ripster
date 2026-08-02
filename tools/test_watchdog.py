# -*- coding: utf-8 -*-
"""Сторож поднимает упавшее — и НЕ поднимает то, что от этого только хуже.

Опасность сторожа ровно одна: цикл перезапусков. 01.08.2026 такой цикл съел
слоты устройств Apple ID насовсем — каждый заход занимал ещё один. Поэтому
здесь проверяется в первую очередь не «поднял», а «вовремя остановился».

Запуск:  python tools/test_watchdog.py
"""
from __future__ import annotations

import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from ripster import watchdog as W  # noqa: E402

_fails: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("  OK  " if ok else "ПРОВАЛ") + f"  {name}")
    if not ok:
        _fails.append(name)
        if detail:
            print(f"        {detail}")


class Svc:
    """Поддельный сервис: сам решает, когда быть живым."""
    def __init__(self, alive=True, revives=True):
        self.alive, self.revives, self.tries = alive, revives, 0

    async def is_alive(self):
        return self.alive

    async def revive(self):
        self.tries += 1
        if self.revives:
            self.alive = True
        return self.revives


async def tick(name, svc, blocked=""):
    await W._tick(name, svc.is_alive, svc.revive, lambda: blocked)


async def main():
    W._broadcast = None
    W._state.clear()

    # 1. Никогда не был жив — не трогаем (возможно, выключен намеренно).
    s = Svc(alive=False)
    await tick("нетронутый", s)
    check("не поднимаем то, что не видели живым", s.tries == 0, f"попыток {s.tries}")

    # 2. Был жив, упал — поднимаем.
    W._state.clear()
    s = Svc(alive=True)
    await tick("обычный", s)          # запомнили, что живой
    s.alive = False
    await tick("обычный", s)
    check("упавшее поднимается", s.tries == 1 and s.alive, f"попыток {s.tries}, жив {s.alive}")

    # 3. Успех сбрасывает счётчик.
    st = W._st("обычный")
    check("после подъёма счётчик сброшен", st["tries"] == 0, str(st))

    # 4. Не лечится — сдаёмся после _MAX_TRIES, а не долбим вечно.
    W._state.clear()
    s = Svc(alive=True, revives=False)
    await tick("упрямый", s)
    s.alive = False
    for _ in range(8):
        W._st("упрямый")["next_try"] = 0.0        # обходим паузу, проверяем ПОТОЛОК
        await tick("упрямый", s)
    check("больше трёх попыток не делает", s.tries <= W._MAX_TRIES,
          f"попыток {s.tries}, потолок {W._MAX_TRIES}")
    check("после потолка сдался", W._st("упрямый")["given_up"] > 0)

    # 5. Пауза между попытками соблюдается.
    W._state.clear()
    s = Svc(alive=True, revives=False)
    await tick("паузa", s)
    s.alive = False
    await tick("паузa", s)
    before = s.tries
    await tick("паузa", s)            # сразу же — не должен пробовать снова
    check("между попытками выдерживает паузу", s.tries == before,
          f"было {before}, стало {s.tries}")

    # 6. ГЛАВНОЕ: причина, которую перезапуск не лечит — не трогаем вовсе.
    W._state.clear()
    s = Svc(alive=True, revives=True)
    await tick("лимит", s)
    s.alive = False
    await tick("лимит", s, blocked="у Apple ID исчерпан лимит устройств")
    check("при запрещающей причине НЕ поднимает", s.tries == 0,
          f"попыток {s.tries} — именно так сжигаются слоты устройств")
    check("и сразу сдаётся, а не ждёт", W._st("лимит")["given_up"] > 0)

    # 7. Настоящие диагнозы Apple распознаются по журналу.
    src = pathlib.Path(W.__file__).read_text(encoding="utf-8")
    for needle in ("device limit", "concurrent playing devices", "lease code 3062"):
        check(f"знает признак «{needle}»", needle in src)

    print()
    if _fails:
        print(f"ПРОВАЛЕНО: {len(_fails)} — {', '.join(_fails)}")
        sys.exit(1)
    print("Все проверки пройдены — сторож поднимает своё и не жжёт чужое.")


asyncio.run(main())
