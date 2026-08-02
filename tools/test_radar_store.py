# -*- coding: utf-8 -*-
"""Склад радара: увиденное однажды не пропадает, когда источник замолчал.

У Spotify есть склад по артистам и снимки лент. У BBC/SoundCloud/Apple был
только 15-минутный кэш того, что источник отдаёт ПРЯМО СЕЙЧАС, — и всё, что
выпало из его окна, исчезало навсегда. Именно так «пропала» карточка,
найденная 24 июля (разбор 01.08.2026).

Запуск:  python tools/test_radar_store.py
"""
from __future__ import annotations

import pathlib
import sys
import tempfile
from datetime import datetime, timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from ripster.routes import radar as R  # noqa: E402

_fails: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("  OK  " if ok else "ПРОВАЛ") + f"  {name}")
    if not ok:
        _fails.append(name)
        if detail:
            print(f"        {detail}")


def d(days_ago: int) -> str:
    return (datetime.now() - timedelta(days=days_ago)).strftime("%Y-%m-%d")


tmp = pathlib.Path(tempfile.mkdtemp(prefix="ripster_store_"))
R._s["store_file"] = tmp / "radar_store.json"

OLD = {"service": "apple", "id": "a1", "title": "Найден 8 дней назад",
       "artist": "PROFF", "date": d(8)}
NEW = {"service": "apple", "id": "a2", "title": "Свежий", "artist": "Someone", "date": d(1)}

# 1. Первый проход: источник отдал обе записи.
out = R._durable_merge("apple", [OLD, NEW], days=30)
check("обе записи в выдаче", {r["id"] for r in out} == {"a1", "a2"},
      f"вышло: {[r['id'] for r in out]}")
check("склад лёг на диск", (tmp / "radar_store.json").exists())

# 2. Источник «забыл» старую — она обязана остаться.
out = R._durable_merge("apple", [NEW], days=30)
check("источник забыл — запись осталась", "a1" in {r["id"] for r in out},
      "именно это и терялось: карточка была и пропала")

# 3. Источник замолчал совсем.
out = R._durable_merge("apple", [], days=30)
check("источник молчит — склад отдаёт обе", {r["id"] for r in out} == {"a1", "a2"})

# 4. Окно фильтра уважается.
out = R._durable_merge("apple", [], days=3)
check("окно 3 дня оставляет только свежую", {r["id"] for r in out} == {"a2"},
      f"вышло: {[r['id'] for r in out]}")

# 5. Свежая версия записи обновляет складскую, а не задваивает.
upd = dict(OLD, title="Название уточнилось")
out = R._durable_merge("apple", [upd], days=30)
same = [r for r in out if r["id"] == "a1"]
check("повтор не задваивает", len(same) == 1, f"копий: {len(same)}")
check("данные обновились", same and same[0]["title"] == "Название уточнилось")

# 6. Источники не смешиваются.
R._durable_merge("bbc", [{"service": "bbc", "id": "b1", "title": "Эпизод",
                          "artist": "Essential Mix", "date": d(2)}], days=30)
out = R._durable_merge("apple", [], days=30)
check("склад bbc не течёт в apple", "b1" not in {r["id"] for r in out})

# 7. Сортировка — свежее сверху.
out = R._durable_merge("apple", [], days=30)
check("свежее сверху", out and out[0]["date"] >= out[-1]["date"])

# 8. Записи старше окна хранения выпадают.
R._durable_merge("apple", [{"service": "apple", "id": "old", "title": "Древность",
                            "artist": "X", "date": d(R._STORE_WINDOW_DAYS + 30)}], days=30)
out = R._durable_merge("apple", [], days=3650)
check("древности не копятся", "old" not in {r["id"] for r in out})

import shutil
shutil.rmtree(tmp, ignore_errors=True)

print()
if _fails:
    print(f"ПРОВАЛЕНО: {len(_fails)} — {', '.join(_fails)}")
    sys.exit(1)
print("Все проверки пройдены — радар помнит найденное всеми источниками.")
