"""Отложенная запись BBC-эфиров.

У выпусков BBC есть время начала эфира. Владелец ставит «отложенную запись» —
задача ложится в очередь со статусом ``scheduled`` и зеркалируется в
``bbc_scheduled.json`` рядом с конфигом. Цикл ``run_loop()`` (поднимается в
lifespan) ждёт наступления времени и переводит задачу в ``queued`` — дальше её
ведёт обычный ``process_queue``, то есть история, манифест и доставка ничем не
отличаются от любой другой загрузки.

Время хранится в UTC. Файл пишется атомарно (tmp + os.replace), чтобы обрыв
посреди записи не оставил полфайла вместо всех планов.
"""
from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from ripster.i18n_msg import imsg
from ripster.task_state import TaskStatus

STORE_VERSION = 1
MAX_LIVE_MINUTES = 8 * 60          # физический предел: дольше марафона BBC не пишем
_GRACE_SECONDS = 600               # «уже поздно» — эфир начался слишком давно
_LATENESS_SECONDS = 10             # допуск на дрейф часов и паузу цикла


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_utc(value: str | int | float) -> datetime:
    """ISO-строка (с смещением или без — без смещения считаем UTC) или unix-time."""
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    raw = (value or "").strip()
    if not raw:
        raise ValueError("empty time")
    iso = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def fmt_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Хранилище ────────────────────────────────────────────────────────────────

class ScheduledStore:
    """Список запланированных записей в JSON-файле рядом с config.yaml."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._items: list[dict] = []
        self.loaded = False

    # ── чтение/запись ──

    def load(self) -> list[dict]:
        self.loaded = True
        try:
            if not self.path.exists():
                self._items = []
                return self._items
            raw = json.loads(self.path.read_text(encoding="utf-8")) or []
            rows = raw.get("recordings") if isinstance(raw, dict) else raw
            self._items = [r for r in (rows or []) if isinstance(r, dict) and r.get("id")]
        except Exception as e:
            print(f"[bbc-schedule] load failed ({self.path}): {e}. Starting empty.",
                  flush=True)
            self._items = []
        return self._items

    def save(self) -> None:
        """Атомарно: пишем временный файл рядом, ставим, затем os.replace."""
        payload = {"version": STORE_VERSION, "recordings": self._items}
        tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except Exception as e:
            print(f"[bbc-schedule] save failed: {e}", flush=True)
            try:
                tmp.unlink()
            except OSError:
                pass

    # ── доступ ──

    def _ensure(self) -> list[dict]:
        if not self.loaded:
            self.load()
        return self._items

    def all(self) -> list[dict]:
        return [dict(r) for r in self._ensure()]

    def get(self, sid: str) -> dict | None:
        return next((r for r in self._ensure() if r.get("id") == sid), None)

    def pending(self) -> list[dict]:
        return [r for r in self._ensure() if r.get("status") == "pending"]

    def mark(self, sid: str, **fields) -> None:
        row = self.get(sid)
        if row is not None:
            row.update(fields)
            self.save()

    # ── изменение ──

    def add(self, *, channel: str, start_utc: datetime, duration: int,
            title: str = "", subtitle: str = "", cover: str = "",
            task_id: str = "") -> dict:
        """Поставить план. Дубликат (тот же канал и то же время эфира, ещё не
        записанный) — ValueError: вторая запись того же эфира была бы просто
        вторым экземпляром файла."""
        rows = self._ensure()
        if not channel:
            raise ValueError(imsg("err.bbc_live_no_channel", "Не указан канал эфира"))
        duration = int(duration or 0)
        if duration <= 0:
            raise ValueError(imsg("err.bbc_live_bad_duration",
                                  "Нужна длительность эфира в секундах"))
        if duration > MAX_LIVE_MINUTES * 60:
            raise ValueError(imsg("err.bbc_live_too_long",
                                  f"Дольше {MAX_LIVE_MINUTES * 60 // 60} часов эфир не пишем"))
        start = start_utc.astimezone(timezone.utc)
        if (start - utcnow()).total_seconds() < -_GRACE_SECONDS:
            raise ValueError(imsg("err.bbc_live_past",
                                  "Время эфира уже прошло — записать можно только будущий эфир"))
        same = [r for r in rows
                if r.get("channel") == channel
                and r.get("status") == "pending"
                and abs((_dt(r) - start).total_seconds()) < 30]
        if same:
            raise ValueError(imsg("err.bbc_live_duplicate",
                                  "Запись этого эфира уже запланирована"))
        row = {
            "id":         uuid.uuid4().hex[:8],
            "channel":    channel,
            "start_utc":  fmt_utc(start),
            "duration":   duration,
            "title":      title,
            "subtitle":   subtitle,
            "cover":      cover,
            "task_id":    task_id,
            "status":     "pending",
            "created_utc": fmt_utc(utcnow()),
        }
        rows.append(row)
        self.save()
        return dict(row)

    def remove(self, sid: str) -> bool:
        rows = self._ensure()
        keep = [r for r in rows if r.get("id") != sid]
        if len(keep) == len(rows):
            return False
        self._items = keep
        self.save()
        return True


def _dt(row: dict) -> datetime:
    try:
        return parse_utc(row.get("start_utc") or "")
    except Exception:
        return utcnow()


# ── Контекст приложения ──────────────────────────────────────────────────────

_store: ScheduledStore | None = None
_queue:    list = []
_qs        = None
_lock:     asyncio.Lock | None = None
_cfg:      dict = {}
_broadcast = None
_process_queue = None
_make_task = None
_queue_snapshot = None
save_state       = None      # callable — сброс планов на диск (тесты подменяют)


def install(*, store: ScheduledStore, queue: list, qs, config: dict,
            broadcast, process_queue, make_task, queue_snapshot) -> None:
    global _store, _queue, _qs, _lock, _cfg, _broadcast, _process_queue
    global _make_task, _queue_snapshot, save_state
    _store         = store
    _queue         = queue
    _qs            = qs
    _lock          = qs.lock
    _cfg           = config
    _broadcast     = broadcast
    _process_queue = process_queue
    _make_task     = make_task
    _queue_snapshot = queue_snapshot
    save_state     = store.save


def get_store() -> ScheduledStore:
    if _store is None:
        raise RuntimeError("bbc_schedule.install() has not run yet")
    return _store


def task_for(store: ScheduledStore, row: dict) -> dict:
    """Карточка «запланировано» в общей очереди. url — псевдо-ссылка эфира:
    движок по ней понимает, какой канал писать, а статус ``scheduled`` не даёт
    процессу очереди её трогать, пока не наступит время."""
    from ripster import bbc_live_channels as _ch
    channel = row.get("channel") or ""
    task = _make_task(f"https://www.bbc.co.uk/sounds/live/{channel}",
                      "live320", "bbc_live", "bbc", source="bbc_schedule")
    task["id"]     = row["task_id"] = row["id"]
    task["status"] = TaskStatus.SCHEDULED.value
    task["scheduled_for"] = row["start_utc"]
    task["schedule_id"]   = row["id"]
    task["meta"] = {
        "service":  "bbc",
        "title":    row.get("title") or _ch.label(channel),
        "artist":   _ch.label(channel),
        "artworkUrl": row.get("cover") or "",
        "duration": int(row.get("duration") or 0),
        "channel":  channel,
        "scheduled_for": row["start_utc"],
    }
    return task


def schedule_recording(*, channel: str, start_utc: datetime, duration: int,
                       title: str = "", subtitle: str = "", cover: str = "") -> dict:
    """Создать план и задачу-«ожидание» в очереди (один вызов — обе записи)."""
    store = get_store()
    if store is None:
        raise RuntimeError("bbc_schedule.install() has not run yet")
    rows = store.add(channel=channel, start_utc=start_utc, duration=duration,
                     title=title, subtitle=subtitle, cover=cover)
    try:
        _queue.append(task_for(store, rows))
    except Exception as e:
        print(f"[bbc-schedule] карточка очереди не создана: {e}", flush=True)
    return rows


def cancel_recording(sid: str) -> bool:
    """Снять план. Снимается и карточка в очереди, пока задача не началась."""
    store = get_store()
    row = store.get(sid)
    if row is None:
        return False
    store.remove(sid)
    for t in list(_queue):
        if t.get("id") == sid and t.get("status") in (
                TaskStatus.SCHEDULED.value, TaskStatus.QUEUED.value):
            _queue.remove(t)
        elif t.get("id") == sid and t.get("status") == TaskStatus.RUNNING.value:
            # Запись уже идёт — отменять процесс не будем, только план.
            break
    return True


def restore() -> int:
    """После перезапуска: у каждого живого плана в очереди обязана быть карточка.

    ``queue_pending.json`` их обычно сохраняет сам, но очередь могли и
    почистить — а план живёт в своём файле и переживает любую чистку.
    Наступившие во время простоя строки не трогаем: их разберёт ``run_loop``
    (он же честно пропустит слишком опоздавшие)."""
    store = get_store()
    ids = {t.get("id") for t in _queue}
    n = 0
    for row in store.pending():
        if row["id"] in ids:
            continue
        try:
            _queue.append(task_for(store, row))
            n += 1
        except Exception as e:
            print(f"[bbc-schedule] restore {row.get('id')}: {e}", flush=True)
    return n


# ── Цикл проверки ────────────────────────────────────────────────────────────

def due_rows(store: ScheduledStore, now: datetime | None = None) -> list[dict]:
    now = now or utcnow()
    return [r for r in store.pending() if (now - _dt(r)).total_seconds() >= -_LATENESS_SECONDS]


async def fire(row: dict) -> bool:
    """Наступило время — переводим задачу в обычную очередь."""
    store = get_store()
    late = (utcnow() - _dt(row)).total_seconds()
    if late > _GRACE_SECONDS:
        # Эфир начался слишком давно: писать его с этого момента — значит
        # снять не то, что человек планировал. Снимаем план как пропущенный.
        store.remove(row["id"])
        for t in list(_queue):
            if t.get("id") == row.get("id") and t.get("status") == TaskStatus.SCHEDULED.value:
                _queue.remove(t)
        print(f"[bbc-schedule] {row.get('id')}: эфир начался {int(late // 60)} мин назад — "
              f"пропущен, план снят", flush=True)
        if _broadcast:
            await _broadcast({"type": "queue_update", "queue": _queue_snapshot()})
        return False
    task = next((t for t in _queue if t.get("id") == row.get("id")), None)
    if task is None:
        # Карточку удалили руками (✕ в очереди) — план больше нечем исполнить.
        store.remove(row["id"])
        print(f"[bbc-schedule] {row.get('id')}: задачи в очереди нет — план снят", flush=True)
        return False
    try:
        task["status"] = TaskStatus.QUEUED.value
    except Exception:
        pass
    store.mark(row["id"], status="fired", fired_utc=fmt_utc(utcnow()))
    if _broadcast:
        await _broadcast({"type": "queue_update", "queue": _queue_snapshot()})
    if _process_queue and _qs:
        async with _lock:
            should_start = _qs.start()
        if should_start:
            asyncio.create_task(_process_queue())
            await _broadcast({"type": "queue_started"})
    print(f"[bbc-schedule] пора писать эфир {row.get('channel')} ({row.get('id')})", flush=True)
    return True


async def run_loop(period: float = 5.0) -> None:
    store = get_store()
    while True:
        try:
            for row in due_rows(store):
                await fire(row)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[bbc-schedule] cycle error: {type(e).__name__}: {e}", flush=True)
        await asyncio.sleep(period)
