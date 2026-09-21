"""Планировщик отложенной записи BBC (ripster/bbc_schedule.py).

Что обязана держать эта связка: план переживает перезапуск, отмена снимает
и план, и карточку, прошедшее время не пишется задним числом, дубли одного
эфира не плодят файлы. Хранилище — настоящий JSON-файл в tmp_path, очередь —
обычный список, как в app.py.
"""
import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from ripster import bbc_schedule as bs
from ripster.task_state import TaskStatus


class _QS:
    """Минимум QueueManager, который нужен планировщику: lock и start()."""
    def __init__(self):
        self.lock = asyncio.Lock()
        self.started = 0
    def start(self):
        self.started += 1
        return True


@pytest.fixture
def ctx(tmp_path):
    """Установить модуль с чистым состоянием; вернуть (store, queue, qs, msgs)."""
    store = bs.ScheduledStore(tmp_path / "bbc_scheduled.json")
    queue, qs, msgs = [], _QS(), []

    async def broadcast(msg):
        msgs.append(msg)

    def snapshot():
        return [dict(t) for t in queue]

    from ripster.routes.queue import _make_task

    bs.install(store=store, queue=queue, qs=qs, config={}, broadcast=broadcast,
               process_queue=None, make_task=_make_task, queue_snapshot=snapshot)
    yield store, queue, qs, msgs


def _future(minutes=30):
    return bs.utcnow() + timedelta(minutes=minutes)


# ── Хранение: файл, UTC, атомарность ─────────────────────────────────────────

def test_store_file_utc_and_atomic(ctx, tmp_path):
    store = ctx[0]
    # Смещение +05:00 должно приехать в файл чистым UTC. Дата — относительная:
    # зашитая «2026-09-21» 21.09.2026 стала прошлым, и store.add честно отказал
    # записывать прошедший эфир — тест упал, хотя код был исправен.
    day = (datetime.now(timezone.utc) + timedelta(days=3)).date()
    local = f"{day.isoformat()}T03:00:00+05:00"
    want = (datetime.fromisoformat(local).astimezone(timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%SZ"))
    row = store.add(channel="bbc_radio_three", start_utc=bs.parse_utc(local),
                    duration=7200)
    assert row["start_utc"] == want
    raw = json.loads(store.path.read_text(encoding="utf-8"))
    assert raw["version"] == bs.STORE_VERSION
    assert raw["recordings"][0]["start_utc"] == want
    # атомарная запись: временных файлов после save() не остаётся
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


# ── Переживает перезапуск ────────────────────────────────────────────────────

def test_survives_restart(ctx):
    store, queue = ctx[0], ctx[1]
    row = bs.schedule_recording(channel="bbc_6music", start_utc=_future(),
                                duration=3600, title="Mix")
    assert len(queue) == 1 and queue[0]["status"] == TaskStatus.SCHEDULED.value

    # «Перезапуск»: тот же файл на диске — новый процесс с пустой очередью.
    from ripster.routes.queue import _make_task
    fresh_store = bs.ScheduledStore(store.path)
    fresh_queue = []
    bs.install(store=fresh_store, queue=fresh_queue, qs=_QS(), config={},
               broadcast=None, process_queue=None, make_task=_make_task,
               queue_snapshot=lambda: fresh_queue)
    assert fresh_store.load()          # план читается с диска
    n = bs.restore()
    assert n == 1
    card = fresh_queue[0]
    assert card["id"] == row["id"]
    assert card["status"] == TaskStatus.SCHEDULED.value
    assert card["scheduled_for"] == row["start_utc"]
    assert card["meta"]["channel"] == "bbc_6music"
    assert card["engine"] == "bbc_live"


def test_restore_keeps_existing_cards(ctx):
    store, queue = ctx[0], ctx[1]
    bs.schedule_recording(channel="bbc_6music", start_utc=_future(), duration=60)
    # queue_pending.json вернул карточку сам — restore не должен плодить вторую.
    assert bs.restore() == 0
    assert len(queue) == 1


# ── Отмена ───────────────────────────────────────────────────────────────────

def test_cancel_removes_plan_and_card(ctx):
    store, queue = ctx[0], ctx[1]
    row = bs.schedule_recording(channel="bbc_radio_one", start_utc=_future(), duration=60)
    assert bs.cancel_recording(row["id"]) is True
    assert store.pending() == []
    assert queue == []
    assert bs.cancel_recording(row["id"]) is False


def test_cancel_leaves_running_recording(ctx):
    store, queue = ctx[0], ctx[1]
    row = bs.schedule_recording(channel="bbc_radio_one", start_utc=_future(), duration=60)
    queue[0]["status"] = TaskStatus.RUNNING.value
    assert bs.cancel_recording(row["id"]) is True
    assert len(queue) == 1           # идущую запись отмена плана не трогает
    assert queue[0]["status"] == TaskStatus.RUNNING.value


# ── Прошедшее время ──────────────────────────────────────────────────────────

def test_past_start_rejected(ctx):
    store = ctx[0]
    from datetime import timedelta
    with pytest.raises(ValueError):
        store.add(channel="bbc_radio_two", start_utc=bs.utcnow() - timedelta(hours=1),
                  duration=600)
    # В пределах_grace окна прошлое ещё принимают — опоздание на пару минут
    # это «записать сейчас сколько осталось», а не отказ.
    row = store.add(channel="bbc_radio_two", start_utc=bs.utcnow() - timedelta(minutes=2),
                    duration=600)
    assert row["status"] == "pending"


def test_fire_skips_long_missed(ctx):
    """План, который «настал», пока приложение было выключено (больше grace
    окна назад), не пишется задним числом — снимается."""
    store, queue = ctx[0], ctx[1]
    row = bs.schedule_recording(channel="bbc_world_service", start_utc=_future(),
                                duration=3600)
    # перематываем старт в далёкое прошлое, как после суток простоя
    store.mark(row["id"], start_utc=bs.fmt_utc(bs.utcnow() - timedelta(hours=9)))
    fired = asyncio.run(bs.fire(store.get(row["id"])))
    assert fired is False
    assert store.get(row["id"]) is None
    assert queue == []


def test_fire_queues_task_at_time(ctx):
    store, queue, qs, msgs = ctx
    row = bs.schedule_recording(channel="bbc_world_service",
                                start_utc=bs.utcnow(),   # ровно сейчас — пора
                                duration=3600)
    fired = asyncio.run(bs.fire(store.get(row["id"])))
    assert fired is True
    assert queue[0]["status"] == TaskStatus.QUEUED.value
    assert store.get(row["id"])["status"] == "fired"


def test_fire_without_card_drops_plan(ctx):
    store, queue = ctx[0], ctx[1]
    row = bs.schedule_recording(channel="bbc_1xtra", start_utc=bs.utcnow(), duration=60)
    queue.clear()                       # карточку удалили руками
    assert asyncio.run(bs.fire(store.get(row["id"]))) is False
    assert store.pending() == []


def test_fire_actually_starts_the_queue_processor(ctx):
    """Проба, которую fixture выше не делает: с ``process_queue`` планировщик
    обязан не только перекрасить карточку, но и поднять процесс очереди —
    ``fire()`` берёт ``qs.lock``, и если install() не присвоил её модульному
    глобал, отложенная запись молча умирает на `NoneType`."""
    store, queue, qs = ctx[0], ctx[1], ctx[2]
    started = []

    async def process_queue():
        started.append("go")

    async def broadcast(msg):
        pass

    from ripster.routes.queue import _make_task
    bs.install(store=store, queue=queue, qs=qs, config={}, broadcast=broadcast,
               process_queue=process_queue, make_task=_make_task,
               queue_snapshot=lambda: list(queue))
    row = bs.schedule_recording(channel="bbc_6music", start_utc=bs.utcnow(), duration=60)

    async def go():
        assert await bs.fire(store.get(row["id"])) is True
        await asyncio.sleep(0.05)          # дать create_task отработать
        return started[:]

    assert asyncio.run(go()) == ["go"]
    assert queue[0]["status"] == TaskStatus.QUEUED.value


def test_bbc_routes_wiring_passes_queue_snapshot():
    """install() роутов обязан забирать queue_snapshot из контекста: без него
    POST/DELETE /api/bbc/schedule падают NameError уже после того, как план
    создан, — то есть план есть, а фронта об этом ничего не знает."""
    from types import SimpleNamespace

    from fastapi import FastAPI

    from ripster.routes import bbc as _bbc

    def snap():
        return []

    _bbc.install(FastAPI(), SimpleNamespace(config={}, broadcast=None,
                                            queue_snapshot=snap))
    assert _bbc._queue_snapshot is snap


# ── Дубли ────────────────────────────────────────────────────────────────────

def test_duplicate_rejected(ctx):
    store, queue = ctx[0], ctx[1]
    start = _future()
    bs.schedule_recording(channel="bbc_radio_three", start_utc=start, duration=7200)
    with pytest.raises(ValueError):
        bs.schedule_recording(channel="bbc_radio_three",
                              start_utc=start + timedelta(seconds=10), duration=7200)
    # другой канал в то же время — не дубль
    other = store.add(channel="bbc_radio_one", start_utc=start, duration=60)
    assert other["status"] == "pending"
    # и тот же канал, но другой эфир — тоже
    assert store.add(channel="bbc_radio_three", start_utc=start + timedelta(hours=3),
                     duration=60)["status"] == "pending"
    assert len(queue) == 1              # дубли карточки не создали


def test_bad_duration_rejected(ctx):
    store = ctx[0]
    with pytest.raises(ValueError):
        store.add(channel="bbc_radio_two", start_utc=_future(), duration=0)
    with pytest.raises(ValueError):
        store.add(channel="bbc_radio_two", start_utc=_future(),
                  duration=bs.MAX_LIVE_MINUTES * 60 + 1)
    with pytest.raises(ValueError):
        store.add(channel="", start_utc=_future(), duration=60)
