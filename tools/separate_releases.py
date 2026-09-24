# -*- coding: utf-8 -*-
"""Разнести по релизам папки, где манифест записал несколько РАЗНЫХ релизов.

Наследие коллизии 24.09.2026 (NORTHERN EXPOSURE REDUX: MIXED uvxkonsapjsbb и
UNMIXED e6r7vcsf378jq легли в одну папку, часть файлов затёрта и потеряна):
исправление именования защищает БУДУЩИЕ загрузки, эта утилита раскладывает
НАСЛЕДИЕ. Делает ровно одно: для каждой папки с >1 идентичностью релиза
оставляет старейшую задачу в покое, а файлы остальных ПЕРЕНОСИТ (не удаляет)
в соседнюю папку «<имя> [qobuz-<id>]» и правит `dir` в манифесте.

Гарантии:
  * переносим ТОЛЬКО файлы, названные в files[] конкретной задачи манифеста;
  * файл, который называют несколько задач (слепой glob когда-то записал
    соседей), остаётся в исходной папке — никто не угадывает владельца;
  * в новой папке существующий файл не перетирается никогда;
  * по умолчанию — сух прогон; писать начинает только с --apply.

Запускать при НЕ занятом манифесте (приложение может перезаписать кэш):
    .venv\\Scripts\\python.exe tools/separate_releases.py            # отчёт
    .venv\\Scripts\\python.exe tools/separate_releases.py --apply    # выполнить
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors="replace")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from ripster.release_folders import fmt_suffix  # noqa: E402
from ripster.download_manifest import entry_release_id  # noqa: E402

MF = ROOT / "downloads_manifest.json"


def _rid(ent: dict) -> str:
    """Идент релиза записи (общая функция с движком и health-check'ом)."""
    return entry_release_id(ent)


def _key(d: str) -> str:
    try:
        return str(Path(d).resolve()).lower()
    except Exception:
        return d.lower()


def groups(data: dict) -> dict:
    """{папка-ключ: [task_id,…]} — только папки с >1 РАЗНОЙ идентичностью."""
    by_dir: dict = {}
    for tid, ent in (data or {}).items():
        d = (ent or {}).get("dir") or ""
        if d:
            by_dir.setdefault(_key(d), []).append(tid)
    out = {}
    for k, tids in by_dir.items():
        rids = {_rid(data[t]) for t in tids}
        rids.discard("")
        if len(rids) > 1:
            out[k] = tids
    return out


def plan(data: dict) -> list:
    """[(task_id, src_dir, dst_dir, [имена…])] — что перенесём. Старейшая
    задача папки остаётся; остальные уходят в папки с суффиксом идента."""
    moves = []
    for gkey, tids in groups(data).items():
        # одна и та же папа на диске у нескольких задач; берём dir любой
        src = Path(data[tids[0]]["dir"])
        if not src.is_dir():
            print(f"⚠ папка исчезла, пропускаем: {src}")
            continue
        # сколько задач называют каждый файл — «спорные» не трогает никто
        claims: dict = {}
        for t in tids:
            for fn in (data[t].get("files") or []):
                claims[str(fn).lower()] = claims.get(str(fn).lower(), 0) + 1
        tids_sorted = sorted(tids, key=lambda t: data[t].get("ts", 0))
        for t in tids_sorted[1:]:
            ent = data[t]
            rid = _rid(ent)
            if not rid:
                continue
            svc = (ent.get("service") or "release").lower()
            suffix = fmt_suffix(svc, rid, "")
            if not suffix:
                continue
            dst = src.parent / (src.name + suffix)
            names = [fn for fn in (ent.get("files") or [])
                     if claims.get(str(fn).lower(), 0) == 1
                     and (src / str(fn)).is_file()
                     and not (dst / str(fn)).exists()]
            if not names:
                print(f"  {t[:8]}: нечего уносить (все файлы спорные/нет на месте)")
                continue
            moves.append((t, src, dst, names))
    return moves


def main() -> int:
    apply = "--apply" in sys.argv[2:] or "--apply" in sys.argv[1:]
    try:
        data = json.loads(MF.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"манифест не читается ({MF}): {e}")
        return 2
    g = groups(data)
    if not g:
        print("Смешанных папок нет — разносить нечего.")
        return 0
    print(f"Смешанных папок: {len(g)}")
    moves = plan(data)
    if not moves:
        print("Переносить нечего (файлы спорные или уже на месте у своей записи).")
        return 0
    for t, src, dst, names in moves:
        amb = len(data[t].get("files") or []) - len(names)
        print(f"• {t[:8]} → {dst.name}  ({len(names)} файл(ов)"
              + (f", {amb} спорных остаётся" if amb else "") + ")")
    if not apply:
        print("\nСУХОЙ ПРОГОН. Чтобы выполнить: --apply")
        return 0
    done = 0
    for t, src, dst, names in moves:
        try:
            dst.mkdir(parents=True, exist_ok=True)
            n = 0
            for fn in names:
                try:
                    if (dst / fn).exists():
                        continue
                    (src / fn).rename(dst / fn)
                    n += 1
                except Exception as e:
                    print(f"  ✗ {fn}: {e}")
            if n:
                data[t]["dir"] = str(dst)
                data[t]["files"] = [f for f in data[t].get("files") or []
                                    if str(f).lower() in {str(x).lower() for x in names}]
                done += n
        except Exception as e:
            print(f"  ✗ {dst}: {e}")
    if done:
        tmp = MF.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        import os
        os.replace(tmp, MF)
    print(f"\nПеренесено файлов: {done}. Пустые оболочки исходных папок не удалялись — "
          f"проверьте вручную (часть файлов была затёрта до фикса и не вернётся).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
