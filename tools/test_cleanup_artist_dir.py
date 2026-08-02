# -*- coding: utf-8 -*-
"""Пустая папка артиста должна исчезать, а всё остальное — оставаться.

Раскладка: `<сервис>/<качество>/<Артист>/<Артист> - <Альбом>/`. Удаляли только
альбом, и от каждой вычищенной загрузки оставался пустой каталог с именем
артиста. Владелец: «папку сервиса и качества ладно, но папку артиста тоже
удаляй».

Опасность ровно одна — увлечься и снести общего родителя: уровень качества
делят ВСЕ альбомы. Поэтому проверяем и то, что мы туда не поднимаемся.

Запуск:  python tools/test_cleanup_artist_dir.py
"""
from __future__ import annotations

import pathlib
import shutil
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from ripster.auto_cleanup import prune_empty_artist_dir  # noqa: E402

_fails: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("  OK  " if ok else "ПРОВАЛ") + f"  {name}")
    if not ok:
        _fails.append(name)
        if detail:
            print(f"        {detail}")


root = pathlib.Path(tempfile.mkdtemp(prefix="ripster_cleanup_"))
try:
    roots = [root]
    qual = root / "deezer" / "FLAC"

    # 1. Обычный случай: у артиста был один альбом, его удалили.
    a1 = qual / "Ronski Speed" / "Ronski Speed - Nuru Mpya"
    a1.mkdir(parents=True)
    shutil.rmtree(a1)                                  # как это делает уборка
    gone = prune_empty_artist_dir(a1, roots)
    check("пустая папка артиста удалена", gone is not None and not (qual / "Ronski Speed").exists(),
          f"вернулось {gone}")
    check("уровень качества уцелел", qual.exists())

    # 2. У артиста остался ещё альбом — не трогаем.
    a2 = qual / "PROFF" / "PROFF - Sopia"
    a3 = qual / "PROFF" / "PROFF - Tolika"
    a2.mkdir(parents=True); a3.mkdir(parents=True)
    shutil.rmtree(a2)
    gone = prune_empty_artist_dir(a2, roots)
    check("папка артиста с другими альбомами НЕ удалена",
          gone is None and (qual / "PROFF").exists() and a3.exists())

    # 3. Не подниматься к общему родителю: удалили саму папку артиста —
    #    уровень качества делят все, его сносить нельзя.
    solo = qual / "OneHit"
    solo.mkdir(parents=True)
    shutil.rmtree(solo)
    before = qual.exists()
    gone = prune_empty_artist_dir(solo, roots)
    check("уровень качества не сносится, даже когда пуст",
          qual.exists() and before, f"вернулось {gone}")

    # 4. Вне библиотеки — не наше дело.
    outside = pathlib.Path(tempfile.mkdtemp(prefix="ripster_outside_"))
    vic = outside / "Artist" / "Album"
    vic.mkdir(parents=True)
    shutil.rmtree(vic)
    gone = prune_empty_artist_dir(vic, roots)
    check("вне библиотеки ничего не трогаем",
          gone is None and (outside / "Artist").exists())
    shutil.rmtree(outside, ignore_errors=True)

    # 5. Корень библиотеки неприкосновенен.
    top = root / "Album"
    top.mkdir(parents=True)
    shutil.rmtree(top)
    check("корень библиотеки не удаляется",
          prune_empty_artist_dir(top, roots) is None and root.exists())

    # 6. Ничего не падает на несуществующем пути.
    check("несуществующий путь не роняет",
          prune_empty_artist_dir(root / "нет" / "такого", roots) is None)
finally:
    shutil.rmtree(root, ignore_errors=True)

print()
if _fails:
    print(f"ПРОВАЛЕНО: {len(_fails)} — {', '.join(_fails)}")
    sys.exit(1)
print("Все проверки пройдены — папка артиста уходит, общие уровни остаются.")
