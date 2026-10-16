"""Как задача очереди выглядит для клиента (веб, бот, мобильный).

Отдельный модуль, а не функция в app.py: app.py запускается как __main__, и
`import app` из маршрута выполнил бы весь файл заново."""
from __future__ import annotations


def public_task(t: dict, drop=("log",)) -> dict:
    """«Оседающий» DONE (runner: решение о доборке недостающих треков ещё не
    принято) показывается как «идёт». Иначе бот закрывал задачу раньше, чем раннер
    решал добрать, и добранное никто не получал (26.09, Boards of Canada 5/18)."""
    d = {k: v for k, v in t.items() if k not in drop}
    if t.get("_settling") and d.get("status") == "done":
        d["status"] = "running"
        try:
            d["progress"] = min(int(d.get("progress") or 0), 99)
        except (TypeError, ValueError):
            d["progress"] = 99
    return d
