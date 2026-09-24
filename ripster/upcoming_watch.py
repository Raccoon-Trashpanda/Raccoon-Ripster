"""Фоновый обход предзаказов: Bandcamp и Beatport по watched-лейблам.

Почему циклом, а не в ответе радара: страница Bandcamp отдаётся медленно, а
правило вежливости — не чаще запроса в 2 секунды (`ripster/bandcamp.py`).
Сотня подписок × по 8 страниц = десятки минут, вешать это на HTTP-запрос
нельзя. Цикл складывает найденное в ТОТ ЖЕ склад `upcoming_store.json`, который
читает радар, — отдельного списка не появляется, а ответ радара остаётся
быстрым: он только достаёт склад.

Порядок обхода — круговой, по несколько имён за проход, с указателем на диске:
за день проходится весь вотчлист, а первый же проход успевает дойти до новых
подписок, а не до алфавитного хвоста.
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime
from pathlib import Path

from ripster import upcoming as _up
from ripster import upcoming_preorders as _upo
from ripster import upcoming_taste as _ut

#: Подписок за один проход. 8 имён × 8 страниц Bandcamp при паузе 2 с — это
#: ~9 минут на проход; чаще и не нужно: предзаказы появляются раз в неделю.
BATCH = 8
#: Между проходами.
EVERY = 30 * 60.0
#: Новый высокобалльный анонс — одно сообщение в бота; не чаще раза в час,
#: иначе первый же день с несколькими анонсами превратится в спам.
DIGEST_MIN_GAP = 60 * 60.0


def _state_file(base: Path) -> Path:
    return base / "upcoming_preorder_state.json"


def _store_file(base: Path) -> Path:
    # Ровно то же имя, что у радара (`routes/radar.py::install`) — один склад,
    # одна лента.
    return base / "upcoming_store.json"


def _load_state(base: Path) -> dict:
    try:
        d = json.loads(_state_file(base).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save_state(base: Path, st: dict) -> None:
    try:
        _state_file(base).write_text(json.dumps(st, ensure_ascii=False, indent=1),
                                     encoding="utf-8")
    except Exception as e:
        print(f"[preorders] state save: {e}", flush=True)


def names(watchlist: list) -> list:
    """Имена подписок: лейблы первыми — у них предзаказы и есть главный сигнал."""
    labels = [str(e.get("name") or "").strip() for e in watchlist or []
              if e.get("kind") == "label" and str(e.get("name") or "").strip()]
    artists = [str(e.get("name") or "").strip() for e in watchlist or []
               if e.get("kind") != "label" and str(e.get("name") or "").strip()]
    seen, out = set(), []
    for n in labels + artists:
        k = n.lower()
        if k not in seen:
            seen.add(k)
            out.append(n)
    return out


def next_batch(watchlist: list, st: dict, size: int = BATCH) -> list:
    """Следующая порция имён по круговому указателю."""
    all_names = names(watchlist)
    if not all_names:
        return []
    at = int(st.get("cursor") or 0) % len(all_names)
    picked = [all_names[(at + i) % len(all_names)] for i in range(min(size, len(all_names)))]
    return picked


async def pass_once(base_dir, watchlist: list, notify: bool = True) -> dict:
    """Один проход: собрать предзаказы, положить в склад, сообщить о новом.

    Возвращает счётчики — обход без числа новых записей неотличим от обхода
    вхолостую (урок вишлиста, 22.08.2026).
    """
    base = Path(base_dir)
    _ut.configure(base)
    st = _load_state(base)
    batch = next_batch(watchlist, st)
    if not batch:
        return {"ok": True, "batch": [], "added": 0, "notified": 0}

    t0 = time.time()
    try:
        records, log = await _upo.collect(watchlist, only=batch)
    except Exception as e:
        print(f"[preorders] collect failed: {type(e).__name__}: {e}", flush=True)
        records, log = [], {"error": str(e)}

    path = _store_file(base)
    store = _up.load(path)
    before_keys = set(store)
    added = _up.put(store, records)
    released = _up.promote_released(store, datetime.now().strftime("%Y-%m-%d"))
    _up.save(path, store)

    # Новое за этот проход — то, чего в складе до него не было. Ранжируем и
    # сообщаем именно по этому подмножеству: пересказывать вчерашний анонс —
    # спам, а счётчик `added` без самих записей не даёт, О ЧЁМ сообщить.
    fresh = [r for r in records if _up.identity(r) not in before_keys]

    # Кто из новых артистов выпускается на этом лейбле — сигнал «свой» для
    # будущих анонсов (ранжирует `upcoming_taste`).
    for r in fresh:
        if r.get("label") and r.get("artist"):
            _ut.note_label_artists(r["label"], [r["artist"]])
    # Сообщаем только о НОВЫХ за этот проход и только выше порога: пересказывать
    # вчерашний анонс — спам.
    due = _ut.pending_digest(_ut.rank(fresh, watchlist))
    sent = 0
    last = float(st.get("notified_at") or 0)
    if notify and due and (time.time() - last) >= DIGEST_MIN_GAP:
        text = ("Грядущее, которое ты ждёшь:\n" + _ut.format_digest(due))
        if await asyncio.to_thread(_ut.notify_owner, text):
            _ut.mark_notified(due)
            sent = len(due)
            st["notified_at"] = time.time()
        else:
            print("[preorders] уведомление не ушло — сообщение повторится в следующий проход",
                  flush=True)

    st["cursor"] = (int(st.get("cursor") or 0) + len(batch)) % max(1, len(names(watchlist)))
    st["last_pass"] = datetime.now().isoformat(timespec="seconds")
    st["last_batch"] = batch
    st["last_log"] = log
    st["seconds"] = round(time.time() - t0, 1)
    _save_state(base, st)
    print(f"[preorders] имён {len(batch)}, записей {len(records)}, новых {added}, "
          f"наступило {len(released)}, сообщено {sent}, {st['seconds']} с", flush=True)
    return {"ok": True, "batch": batch, "found": len(records), "added": added,
            "released": len(released), "notified": sent, "log": log}


async def run_loop(config, base_dir, watchlist=None, every: float = EVERY) -> None:
    """Вечный цикл. `watchlist` — живой список приложения (тот же объект).

    Как и остальные обходы (`accounts_watch`, `namesake_audit`): первый проход
    не сразу, а через минуту после старта — приложение в первую минуту занято
    прогревом кэшей и чужими проверками.
    """
    base = Path(base_dir)
    await asyncio.sleep(60)
    while True:
        try:
            wl = watchlist if watchlist is not None else []
            if (config or {}).get("show-upcoming") is True:
                await pass_once(base, wl)
            else:
                print("[preorders] радар грядущего выключен — обход пропущен", flush=True)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[preorders] pass failed: {type(e).__name__}: {e}", flush=True)
        await asyncio.sleep(max(60.0, float(every)))
