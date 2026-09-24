"""Планировщик отложенной записи BBC (ripster/bbc_schedule.py).

Что обязана держать эта связка: план переживает перезапуск, отмена снимает
и план, и карточку, прошедшее время не пишется задним числом, дубли одного
эфира не плодят файлы. Хранилище — настоящий JSON-файл в tmp_path, очередь —
обычный список, как в app.py.
"""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

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
    """План переживает перезапуск в СВОЁМ файле, а не карточкой в очереди.

    Переписан 22.09.2026 вместе со сменой контракта. Раньше карточка
    создавалась при планировании и восстанавливалась при старте — тест это и
    проверял. Теперь календарь и очередь разделены: `bbc_scheduled.json`
    хранит план, а очередь узнаёт о нём только когда наступит час. Поэтому
    после «перезапуска» план обязан читаться с диска, а очередь — остаться
    пустой.
    """
    store, queue = ctx[0], ctx[1]
    row = bs.schedule_recording(channel="bbc_6music", start_utc=_future(),
                                duration=3600, title="Mix")
    assert queue == [], "план не вешает карточку заранее"

    # «Перезапуск»: тот же файл на диске — новый процесс с пустой очередью.
    from ripster.routes.queue import _make_task
    fresh_store = bs.ScheduledStore(store.path)
    fresh_queue = []
    bs.install(store=fresh_store, queue=fresh_queue, qs=_QS(), config={},
               broadcast=None, process_queue=None, make_task=_make_task,
               queue_snapshot=lambda: fresh_queue)
    assert fresh_store.load()          # план читается с диска
    assert asyncio.run(bs.restore()) == 0, "снимать нечего — карточек и не было"
    assert fresh_queue == []

    survived = fresh_store.get(row["id"])
    assert survived is not None and survived["status"] == "pending"
    assert survived["start_utc"] == row["start_utc"]
    assert survived["channel"] == "bbc_6music"


def test_restore_drops_legacy_cards_from_old_builds(ctx):
    """`restore()` теперь снимает карточки, а не воссоздаёт их.

    Смысл поменялся на противоположный 22.09.2026, и тест переписан целиком.
    Старые сборки вешали в очередь карточку «запланировано на …» с нулём
    прогресса, и `queue_pending.json` послушно возвращает её на каждом старте —
    она висела бы сутками и не давала очереди опустеть. После обновления такую
    карточку надо снять один раз; сам план при этом не трогается и дождётся
    своего часа в календаре.
    """
    store, queue = ctx[0], ctx[1]
    row = bs.schedule_recording(channel="bbc_6music", start_utc=_future(), duration=60)

    # Карточка из старой сборки, какой её вернул бы queue_pending.json.
    queue.append({"id": row["id"], "source": "bbc_schedule",
                  "status": TaskStatus.SCHEDULED.value, "engine": "bbc_live"})

    assert asyncio.run(bs.restore()) == 1, "наследие прошлой сборки снято"
    assert queue == []
    assert store.get(row["id"])["status"] == "pending", "сам план не пострадал"


def test_restore_is_idempotent(ctx):
    """Второй запуск не должен ничего находить — иначе на каждом старте
    пересчитывалось бы и писалось в queue_pending.json впустую."""
    store, queue = ctx[0], ctx[1]
    bs.schedule_recording(channel="bbc_6music", start_utc=_future(), duration=60)
    assert asyncio.run(bs.restore()) == 0
    assert asyncio.run(bs.restore()) == 0


# ── Отмена ───────────────────────────────────────────────────────────────────

def test_cancel_removes_plan_and_card(ctx):
    store, queue = ctx[0], ctx[1]
    row = bs.schedule_recording(channel="bbc_radio_one", start_utc=_future(), duration=60)
    assert bs.cancel_recording(row["id"]) is True
    assert store.pending() == []
    assert queue == []
    assert bs.cancel_recording(row["id"]) is False


def test_cancel_leaves_running_recording(ctx):
    """Отмена плана не трогает запись, которая УЖЕ идёт.

    Тест переписан 22.09.2026 вместе со сменой контракта: раньше карточка
    появлялась в очереди сразу при планировании, и тест просто красил её в
    RUNNING. Теперь до наступления часа карточки нет вовсе (см. докстринг
    модуля), поэтому идущую запись надо честно родить через fire().
    Проверяемое поведение не изменилось — изменился способ до него добраться.
    """
    store, queue = ctx[0], ctx[1]
    row = bs.schedule_recording(channel="bbc_radio_one",
                                start_utc=bs.utcnow(), duration=60)
    asyncio.run(bs.fire(store.get(row["id"])))      # час настал — задача создана
    assert len(queue) == 1
    queue[0]["status"] = TaskStatus.RUNNING.value

    # План уже сработал, и в календаре отменять нечего — отсюда False. Это не
    # отказ, а честный ответ: остановить ИДУЩУЮ запись — дело кнопки отмены в
    # очереди, а не планировщика. Главное, что он её не трогает.
    assert bs.cancel_recording(row["id"]) is False
    assert len(queue) == 1
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
    """Пропущенный эфир не пишется задним числом — но и НЕ исчезает.

    Контракт изменён 22.09.2026, и это не косметика. Раньше план просто
    снимался, и человек, вернувшись к выключенному на сутки компьютеру, не
    находил ни записи, ни следа того, что он вообще что-то планировал. Теперь
    план остаётся в календаре с вердиктом «пропущен» и числом минут опоздания
    (``_miss``), и это видно на вкладке BBC. Ровно этот случай произошёл
    22.09.2026: машина стояла с 01:36 до 17:02.
    """
    store, queue = ctx[0], ctx[1]
    row = bs.schedule_recording(channel="bbc_world_service", start_utc=_future(),
                                duration=3600)
    # перематываем старт в далёкое прошлое, как после суток простоя
    store.mark(row["id"], start_utc=bs.fmt_utc(bs.utcnow() - timedelta(hours=9)))
    fired = asyncio.run(bs.fire(store.get(row["id"])))
    assert fired is False
    assert queue == []                       # задним числом не пишем

    kept = store.get(row["id"])
    assert kept is not None, "план обязан остаться — иначе пропажа немая"
    assert kept["status"] == "missed"
    assert kept["verdict"]["state"] == "missed"
    assert kept["verdict"]["late_minutes"] >= 8 * 60, "опоздание считается честно"
    assert row["id"] not in [r["id"] for r in store.pending()], \
        "в ожидающих ему больше не место — час прошёл"


def test_fire_queues_task_at_time(ctx):
    store, queue, qs, msgs = ctx
    row = bs.schedule_recording(channel="bbc_world_service",
                                start_utc=bs.utcnow(),   # ровно сейчас — пора
                                duration=3600)
    fired = asyncio.run(bs.fire(store.get(row["id"])))
    assert fired is True
    assert queue[0]["status"] == TaskStatus.QUEUED.value
    assert store.get(row["id"])["status"] == "fired"


def test_fire_works_with_an_empty_queue(ctx):
    """Пустая очередь до срабатывания — это НОРМА, а не сбой.

    Тест раньше назывался `test_fire_without_card_drops_plan` и проверял
    обратное: карточка создавалась при планировании, и её отсутствие считалось
    признаком того, что человек удалил задачу руками, — план тогда снимался.
    С 22.09.2026 карточки до наступления часа нет ни у кого и никогда, поэтому
    прежняя проверка запрещала бы единственный штатный путь.
    """
    store, queue = ctx[0], ctx[1]
    row = bs.schedule_recording(channel="bbc_1xtra", start_utc=bs.utcnow(), duration=60)
    assert queue == [], "план не должен создавать карточку заранее"
    assert asyncio.run(bs.fire(store.get(row["id"]))) is True
    assert len(queue) == 1
    assert store.get(row["id"])["status"] == "fired"


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
    # Карточек нет ни одной — их вообще не создают заранее (контракт 22.09.2026).
    # Раньше здесь стояло `len(queue) == 1`: планирование сразу вешало карточку,
    # и проверка сторожила, что дубль не повесит вторую. Сторожить теперь нечего,
    # но сам смысл теста — «отказ по дублю не наплодил лишнего» — остаётся.
    assert queue == []


def test_bad_duration_rejected(ctx):
    store = ctx[0]
    with pytest.raises(ValueError):
        store.add(channel="bbc_radio_two", start_utc=_future(), duration=0)
    with pytest.raises(ValueError):
        store.add(channel="bbc_radio_two", start_utc=_future(),
                  duration=bs.MAX_LIVE_MINUTES * 60 + 1)
    with pytest.raises(ValueError):
        store.add(channel="", start_utc=_future(), duration=60)


# ── Вердикт по записанному файлу: он лежит РЯДОМ с планом ─────────────────────
#
# Планировщик обязан не только снять эфир, но и сказать, чем он вышел: план
# доживает до history, а «live320» в названии задачи — намерение. Поэтому
# спрашиваем файл (ffprobe в settle_result), а храним ответ — в строке плана.

import ripster.bbc_quality as _Q                          # noqa: E402


def _fired(ctx, tmp_path, *, forecast=None, duration=3600, status="done",
           audio="mix.m4a", error=""):
    """План в статусе fired + его задача в очереди + папка, где лежит файл.

    Возвращает (store, sid, save_dir, момент-после-конца-эфира)."""
    store, queue = ctx[0], ctx[1]
    row = bs.schedule_recording(channel="bbc_radio_one", start_utc=bs.utcnow(),
                                duration=duration, title="Essential Mix",
                                pid="m00315gp", forecast=forecast or {})
    sid = row["id"]
    store.mark(sid, status="fired")
    d = tmp_path / "out"; d.mkdir(exist_ok=True)
    if audio:
        (d / audio).write_bytes(b"\0" * 4096)
    task = {"id": sid, "status": status, "_save_dir": str(d), "quality": "live320"}
    if error:
        task["error"] = error
    queue[:] = [t for t in queue if t.get("id") != sid] + [task]
    end = bs.utcnow() + timedelta(seconds=duration + bs._SETTLE_GRACE + 5)
    return store, sid, d, end


def test_recorded_file_gets_a_verdict_beside_the_plan(ctx, tmp_path, monkeypatch):
    store, sid, d, end = _fired(ctx, tmp_path)
    monkeypatch.setattr(_Q, "verdict", lambda p, k: {
        "state": "as_promised", "promised_kbps": k, "kbps": 320, "codec": "AAC-LC",
        "measured": {"profile": "AAC-LC", "avg_kbps": 320, "sample_rate": 48000,
                     "channels": 2, "duration": 3600.0},
        "measured_utc": bs.fmt_utc(end)})
    assert bs.settle_finished(now=end) == 1
    row = store.get(sid)
    assert row["status"] == "recorded"
    v = row["verdict"]
    assert (v["state"], v["kbps"], v["codec"]) == ("as_promised", 320, "AAC-LC")
    # имя файла — тоже часть вердикта: человеку надо знать, что именно мерили
    assert v["file"] == "mix.m4a" and str(d) not in v["file"]
    # и это доживает до перезапуска: вердикт в JSON-е, а не только в памяти
    on_disk = json.loads(store.path.read_text(encoding="utf-8"))["recordings"][0]
    assert on_disk["verdict"]["state"] == "as_promised"
    assert on_disk["verdict"]["measured"]["channels"] == 2


def test_the_promise_is_what_the_ladder_measured_not_the_live320_label(ctx, tmp_path,
                                                                       monkeypatch):
    """Обещание для сверки — best_kbps из прогноза, снятого ДО записи. Канал, у
    которого вечером не было ступени 320, нельзя судить по ярлыку «live320» —
    иначе честная запись на 128 выглядела бы провалом."""
    asked = []
    store, sid, d, end = _fired(ctx, tmp_path, forecast={"best_kbps": 128})
    monkeypatch.setattr(_Q, "verdict", lambda p, k: asked.append(k) or {
        "state": "as_promised", "promised_kbps": k, "kbps": 128, "codec": "AAC-LC",
        "measured": {}, "measured_utc": bs.fmt_utc(end)})
    bs.settle_finished(now=end)
    assert asked == [128]
    # без прогноза (план ставили со старого фронта) — спрашиваем про 320, как и писали
    asked2 = []
    store2, sid2, _d2, end2 = _fired(ctx, tmp_path, duration=60)
    monkeypatch.setattr(_Q, "verdict", lambda p, k: asked2.append(k) or {
        "state": "below", "promised_kbps": k, "kbps": 96, "codec": "HE-AAC",
        "measured": {}, "measured_utc": bs.fmt_utc(end2)})
    bs.settle_finished(now=end2)
    assert asked2 == [320]


def test_a_thin_file_stays_thin_after_settling(ctx, tmp_path, monkeypatch):
    """Ниже обещания — не «recorded». План остаётся в списке с другим статусом и
    с числом, которое человек увидит в карточке: 96 из 320 — это дефект ночи,
    а не успех с плохим названием."""
    store, sid, d, end = _fired(ctx, tmp_path)
    monkeypatch.setattr(_Q, "verdict", lambda p, k: {
        "state": "below", "promised_kbps": 320, "kbps": 96, "codec": "HE-AAC",
        "ratio": 0.3, "measured": {"profile": "HE-AAC", "avg_kbps": 96},
        "measured_utc": bs.fmt_utc(end)})
    assert bs.settle_finished(now=end) == 1
    row = store.get(sid)
    assert row["status"] == "finished"
    assert row["verdict"]["state"] == "below" and row["verdict"]["kbps"] == 96
    assert row["verdict"]["ratio"] == 0.3


def test_a_failed_recording_keeps_the_reason(ctx, tmp_path):
    store, sid, d, end = _fired(ctx, tmp_path, status="error", audio="",
                                error="эфир «Radio 1» не открылся")
    assert bs.settle_finished(now=end) == 1
    v = store.get(sid)["verdict"]
    assert v["state"] == "failed" and "не открылся" in v["reason"]
    assert store.get(sid)["status"] == "finished"


def test_a_finished_task_without_a_file_is_no_file_not_unmeasured(ctx, tmp_path):
    """Разница важна человеку: «файла нет» — запись не дошла до диска,
    «померить не удалось» — дошла, но ffprobe её не читает. Смешивать их в одну
    строку значит отправлять владельца смотреть не туда."""
    store, sid, d, end = _fired(ctx, tmp_path, audio="")
    assert bs.settle_finished(now=end) == 1
    v = store.get(sid)["verdict"]
    assert v["state"] == "no_file" and "save-dir" in v["reason"]


def test_settle_waits_for_a_recording_that_is_still_running(ctx, tmp_path, monkeypatch):
    """Рано — значит тихо промолчать, а не выставить вердикт. Пока задача ещё
    пишется, плана это не касается; «unmeasured» планировщик напишет лишь через
    6 часов молчания — когда ждать уже бессмысленно."""
    store, sid, d, end = _fired(ctx, tmp_path, duration=3600, status="running")
    called = []
    monkeypatch.setattr(_Q, "verdict",
                        lambda p, k: called.append((p, k)) or {
                            "state": "as_promised", "promised_kbps": k, "kbps": 320,
                            "codec": "AAC-LC", "measured": {},
                            "measured_utc": bs.fmt_utc(end)})
    assert bs.settle_finished(now=end - timedelta(hours=1)) == 0
    assert store.get(sid).get("verdict") is None and called == []
    assert bs.settle_finished(now=end + timedelta(minutes=5)) == 0
    assert called == []                              # эфир кончился, но запись идёт
    card = next(t for t in ctx[1] if t.get("id") == sid)
    card["status"] = "done"
    assert bs.settle_finished(now=end + timedelta(minutes=20)) == 1
    v = store.get(sid)["verdict"]
    assert v["state"] == "as_promised" and Path(v["file"]).name == "mix.m4a"


def test_a_vanished_task_is_eventually_admitted_not_left_pending(ctx, tmp_path):
    """Очередь могли почистить, а план живёт в своём файле. Через 6 часов после
    конца эфира планировщик обязан сдаться и сказать «unmeasured» числом, а не
    держать строку вечно «в работе»."""
    store, sid, d, end = _fired(ctx, tmp_path, duration=60)
    ctx[1][:] = []                                  # задача исчезла
    assert bs.settle_finished(now=end + timedelta(hours=1)) == 0
    assert store.get(sid).get("verdict") is None
    assert bs.settle_finished(now=end + timedelta(hours=7)) == 1
    row = store.get(sid)
    v = row["verdict"]
    assert v["state"] == "unmeasured" and v["reason"] == "no task"
    # строка закрыта и больше не перепроверяется (иначе цикл будет тыкать ffprobe
    # в несуществующую задачу вечно)
    assert row["status"] == "finished"
    assert bs.settle_finished(now=end + timedelta(hours=8)) == 0
