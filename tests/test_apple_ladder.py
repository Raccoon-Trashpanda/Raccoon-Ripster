# -*- coding: utf-8 -*-
"""Лестница витрин Apple: ссылка → своя витрина → другие свои слоты → AMD.

Проверяется не «вызвалась ли ступень», а то, ради чего лестница заводилась:
успех ПОЗДНЕЙ ступени должен доводить задачу до `done`, даже если ранние
ступени успели записать ошибку. Именно этого не хватало 22.08.2026: провал
второй ступени подавался наружу как приговор всей задаче.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ripster.task_state import (TaskStatus, advance, current,
                                revive_for_retry, try_advance)
from ripster.runner import _attempt_succeeded


def _fresh():
    return {"id": "t1", "status": TaskStatus.QUEUED.value, "log": []}


def _rung(task, ok: bool):
    """Одна ступень: как её проводит _run_engine_task — статусом, не возвратом."""
    try_advance(task, TaskStatus.RUNNING)
    try_advance(task, TaskStatus.DONE if ok else TaskStatus.ERROR)


def test_attempt_succeeded_reads_status_not_the_call():
    t = _fresh()
    _rung(t, ok=False)
    assert _attempt_succeeded(t) is False
    t2 = _fresh()
    _rung(t2, ok=True)
    assert _attempt_succeeded(t2) is True


def test_late_rung_success_wins():
    """us не дал ключ → ca нет в каталоге → свой слот взял. Итог: done."""
    t = _fresh()
    _rung(t, ok=False)                      # ступень 1
    assert revive_for_retry(t, "us") is True
    _rung(t, ok=False)                      # ступень 2
    assert revive_for_retry(t, "ca") is True
    _rung(t, ok=True)                       # ступень 3
    assert current(t) is TaskStatus.DONE
    assert _attempt_succeeded(t) is True


def test_all_rungs_fail_stays_error():
    t = _fresh()
    for cc in ("us", "ca", "gb"):
        _rung(t, ok=False)
        revive_for_retry(t, cc)
    _rung(t, ok=False)
    assert current(t) is TaskStatus.ERROR


def test_without_revive_a_later_success_is_lost():
    """Цена наивной правки: без возврата в очередь третья ступень не сможет
    перевести задачу из терминальной ошибки, и человек увидит «ошибка» при
    скачанном альбоме."""
    t = _fresh()
    _rung(t, ok=False)
    _rung(t, ok=True)                       # без revive_for_retry
    assert current(t) is TaskStatus.ERROR


def test_revive_never_touches_done_or_cancelled():
    done = _fresh(); _rung(done, ok=True)
    assert revive_for_retry(done, "x") is False
    assert current(done) is TaskStatus.DONE

    cancelled = _fresh()
    advance(cancelled, TaskStatus.CANCELLED)
    assert revive_for_retry(cancelled, "x") is False
    assert current(cancelled) is TaskStatus.CANCELLED


def test_revive_clears_the_stale_error_text():
    t = _fresh()
    _rung(t, ok=False)
    t["error"] = "Apple: каталог не отдал этот релиз"
    revive_for_retry(t, "ca")
    assert "error" not in t
