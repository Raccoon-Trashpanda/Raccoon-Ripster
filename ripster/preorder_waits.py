"""Планы «докачать после выхода предзаказа» — переживают перезапуск.

Модель снята с ripster/bbc_schedule.py: план живёт в JSON-файле рядом с
config.yaml, а НЕ в очереди загрузки (карточка-«запланировано» в очереди
умела только врать), и цикл ``run_loop`` превращает наступивший план в
обычную задачу-загрузку.

Жизнь плана:

  pending  — релиз ещё в будущем (или go_now: вышел на другой учётке, но
             план на ту же секунду — снимется первым же тиком);
  fired    — задача в очереди, id задачи записан в строку;
  done | partial | error | lost | exhausted — исход, в него же уходит
             ОДНО уведомление владельца (не больше: планов много, а
             владелец один).

Если после докачки движок снова увидел предзаказ (релиз отодвинули),
раннер зовёт ``schedule`` ещё раз; больше ``_MAX_REFIRE`` кругов — это уже
не «подождать», а бесконечный цикл, и он обрывается честным сообщением.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import json
import os
import uuid
from pathlib import Path

from ripster import i18n as _i18n

STORE_VERSION = 1
_MAX_REFIRE   = 3          # сколько раз релиз мог «отодвинуться»
_LOST_AFTER   = 15 * 60    # fired-задача исчезла из очереди надолго → lost
_KEEP_ROWS    = 200        # файл не должен разрастаться до бесконечности
_PRUNE_AGE    = 14 * 24 * 3600

_version = STORE_VERSION
_rows: list[dict] = []
_loaded = False
_path: Path | None = None

_queue: list = []
_qs = None
_cfg: dict = {}
_broadcast = None
_process_queue = None
_queue_snapshot = None
_make_task = None


def utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)


def _iso(dt_: _dt.datetime) -> str:
    return dt_.replace(microsecond=0).isoformat() + "Z"


def _parse(raw) -> "_dt.datetime | None":
    try:
        return _dt.datetime.fromisoformat(str(raw).rstrip("Z"))
    except (TypeError, ValueError):
        return None


def install(*, path, queue: list, qs, config: dict, broadcast,
            process_queue, make_task, queue_snapshot) -> None:
    """Контекст приложения — та же форма, что у bbc_schedule.install()."""
    global _path, _queue, _qs, _cfg, _broadcast, _process_queue
    global _make_task, _queue_snapshot
    _path            = Path(path)
    _queue           = queue
    _qs              = qs
    _cfg             = config
    _broadcast       = broadcast
    _process_queue   = process_queue
    _make_task       = make_task
    _queue_snapshot  = queue_snapshot


# ── Хранилище ────────────────────────────────────────────────────────────────

def load(force: bool = False) -> list[dict]:
    global _rows, _loaded
    if _loaded and not force:
        return _rows
    _loaded = True
    out: list[dict] = []
    try:
        if _path and _path.exists():
            raw = json.loads(_path.read_text(encoding="utf-8")) or {}
            rows = raw.get("plans") if isinstance(raw, dict) else raw
            out = [r for r in (rows or []) if isinstance(r, dict) and r.get("id")]
    except Exception as e:                                       # noqa: BLE001
        print(f"[preorder] load failed ({_path}): {e}. Starting empty.", flush=True)
    _rows = out
    return _rows


def save() -> None:
    if _path is None:
        return
    now = utcnow()
    rows = [r for r in _rows
            if r.get("status") == "pending"
            or (now - (_parse(r.get("finished_utc")) or now)).total_seconds() < _PRUNE_AGE
            or not _parse(r.get("finished_utc"))]
    _rows[:] = sorted(rows, key=lambda r: str(r.get("created_utc") or ""))[-_KEEP_ROWS:]
    payload = {"version": STORE_VERSION, "plans": _rows}
    tmp = _path.with_name(f".{_path.name}.{os.getpid()}.tmp")
    try:
        _path.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, _path)
    except Exception as e:                                       # noqa: BLE001
        print(f"[preorder] save failed: {e}", flush=True)
        try:
            tmp.unlink()
        except OSError:
            pass


def pending() -> list[dict]:
    return [r for r in load() if r.get("status") == "pending"]


# ── Постановка плана ─────────────────────────────────────────────────────────

def schedule(task: dict, info: dict) -> "dict | None":
    """Записать план «дожать после выхода». Повторный зов для того же URL
    обновляет момент (релиз могли отодвинуть), дублей pending не заводит."""
    url = str(task.get("url") or "").strip()
    if not url or not info or not info.get("release_utc"):
        return None
    rows = load()
    meta = task.get("meta") or {}
    refire = sum(1 for r in rows
                 if r.get("url") == url and r.get("status") != "pending")
    for r in rows:
        if r.get("url") == url and r.get("status") == "pending":
            r.update(release_utc=info["release_utc"],
                     cc=info.get("cc") or "", slot=info.get("slot"),
                     go_now=bool(info.get("go_now")))
            save()
            return r
    if refire >= _MAX_REFIRE:
        # Релиз отодвигали уже столько раз, что «подождать ещё» стало бы
        # обещанием из тех, которые не выполняют. Сказать и остановиться.
        _notify_owner_async(_i18n.tr(
            "preorder.notify_slipped",
            title=meta.get("title") or url,
            date=str(info.get("release_date") or ""),
            n=refire + 1, max=_MAX_REFIRE))
        return None
    row = {
        "id":         uuid.uuid4().hex[:8],
        "url":        url,
        "quality":    task.get("quality") or "",
        "engine":     task.get("engine") or "",
        "service":    task.get("service") or "",
        "session_id": task.get("session_id") or "",
        "title":      meta.get("title") or "",
        "artist":     meta.get("artist") or "",
        "cover":      meta.get("artworkUrl") or "",
        "release_utc": info["release_utc"],
        "cc":         info.get("cc") or "",
        "slot":       info.get("slot"),
        "go_now":     bool(info.get("go_now")),
        "from_task":  task.get("id") or "",
        "status":     "pending",
        "notified":   False,
        "created_utc": _iso(utcnow()),
    }
    rows.append(row)
    save()
    print(f"[preorder] ждём выход {meta.get('artist') or ''} — "
          f"{meta.get('title') or url} до {row['release_utc']} UTC "
          f"(витрина {row['cc'] or '?'})", flush=True)
    return row


# ── Срабатывание ─────────────────────────────────────────────────────────────

def due_rows(now: "_dt.datetime | None" = None) -> list[dict]:
    now = now or utcnow()
    out = []
    for r in pending():
        when = _parse(r.get("release_utc"))
        if when is not None and when <= now:
            out.append(r)
    return out


def build_task(row: dict) -> dict:
    """Обычная задача-загрузка из плана. Ничем от ручной не отличается —
    уже скачанное движки пропустят, доберутся недостающие."""
    task = _make_task(row["url"], row.get("quality") or "",
                      row.get("engine") or "", row.get("service") or "",
                      source="preorder")
    task["session_id"] = row.get("session_id") or ""
    task["_preorder_wait"] = row["id"]
    task["meta"] = {"service": row.get("service") or "",
                    "title": row.get("title") or "",
                    "artist": row.get("artist") or "",
                    "artworkUrl": row.get("cover") or ""}
    # Apple — единственный движок, где слот учётки пинится задачей (_force_slot,
    # его читает apple_router). Для tidal/qobuz-пунов пин есть только через
    # очередь предпочтений пула — там к нужной витрине доведёт фейловер.
    if row.get("slot") not in (None, "") and row.get("service") == "apple":
        task["_force_slot"] = row["slot"]
    return task


async def fire(row: dict) -> bool:
    """Наступило время (или go_now) — план становится задачей в очереди."""
    task = build_task(row)
    row["status"] = "fired"
    row["task_id"] = task["id"]
    row["fired_utc"] = _iso(utcnow())
    row["refire"] = int(row.get("refire") or 0) + 1
    save()
    _queue.append(task)
    if _broadcast and _queue_snapshot:
        await _broadcast({"type": "queue_update", "queue": _queue_snapshot()})
    if _process_queue and _qs:
        async with _qs.lock:
            should_start = _qs.start()
        if should_start:
            asyncio.create_task(_process_queue())
            if _broadcast:
                await _broadcast({"type": "queue_started"})
    print(f"[preorder] релиз наступил — докачиваем {row.get('artist')} — "
          f"{row.get('title') or row.get('url')}", flush=True)
    return True


def _find_task(tid: str) -> "dict | None":
    for t in _queue:
        if t.get("id") == tid:
            return t
    return None


def _counts(task: dict) -> tuple[int, int]:
    meta = task.get("meta") or {}
    try:
        expected = int(meta.get("trackCount") or meta.get("totalTracks") or 0)
    except (TypeError, ValueError):
        expected = 0
    got = len(task.get("_files") or [])
    return got, expected


async def _notify(text: str) -> None:
    """Владелец получает ОДНО сообщение на план. Сеть — не в цикле событий."""
    try:
        from ripster.upcoming_taste import notify_owner
        await asyncio.to_thread(notify_owner, text)
    except Exception as e:                                       # noqa: BLE001
        print(f"[preorder] notify failed: {type(e).__name__}", flush=True)


def _notify_owner_async(text: str) -> None:
    try:
        asyncio.get_running_loop().create_task(_notify(text))
    except RuntimeError:
        pass                    # нет цикла (тест/CLI) — и ладно, текст в логе бы не жил


async def reconcile() -> int:
    """Досмотреть fired-планы: задача кончилась — уведомить и закрыть."""
    now = utcnow()
    n = 0
    for r in load():
        if r.get("status") != "fired":
            continue
        task = _find_task(r.get("task_id") or "")
        if task is None:
            fired_at = _parse(r.get("fired_utc")) or now
            if (now - fired_at).total_seconds() > _LOST_AFTER:
                r["status"] = "lost"
                r["finished_utc"] = _iso(now)
                await _finish_notify(r, "preorder.notify_error",
                                     err="задача исчезла из очереди")
                n += 1
            continue
        st = task.get("status")
        if st == "cancelled":
            r["status"] = "cancelled"        # человек отменил — не о чём трубить
            r["notified"] = True
            r["finished_utc"] = _iso(now)
            n += 1
            continue
        if st not in ("done", "error"):
            continue
        got, expected = _counts(task)
        if st == "error":
            r["status"] = "error"
            await _finish_notify(r, "preorder.notify_error",
                                 err=str(task.get("error") or "")[:120])
        elif task.get("_preorder"):
            # Раннер уже записал новый план (релиз отодвинулся) — это НЕ итог.
            r["status"] = "deferred"
            r["finished_utc"] = _iso(now)
        elif task.get("_partial"):
            r["status"] = "partial"
            await _finish_notify(r, "preorder.notify_partial", got=got,
                                 expected=expected or "?")
        else:
            r["status"] = "done"
            await _finish_notify(r, "preorder.notify_done", got=got,
                                 expected=expected or "?")
        r["finished_utc"] = _iso(now)
        n += 1
    if n:
        save()
    return n


async def _finish_notify(row: dict, key: str, **params) -> None:
    if row.get("notified"):
        return
    row["notified"] = True
    text = _i18n.tr(key, artist=row.get("artist") or "",
                    title=row.get("title") or row.get("url") or "", **params)
    await _notify(text)


async def run_loop(period: float = 60.0) -> None:
    """Тик раз в минуту: снять наступившие планы и досмотреть fired-задачи.
    Проспанные за простой окна срабатывают сразу — в отличие от эфира BBC,
    «докачать после релиза» не протухает."""
    while True:
        try:
            for row in due_rows():
                await fire(row)
            await reconcile()
        except asyncio.CancelledError:
            raise
        except Exception as e:                                   # noqa: BLE001
            print(f"[preorder] cycle error: {type(e).__name__}: {e}", flush=True)
        await asyncio.sleep(period)
