"""Задача может провалиться ДО старта, и это должно записываться.

04.09.2026: в логах за сутки четыре строки «SAFETY NET: task … left run_task as
'running' — forced to error», а владелец на карточке видел «Задача завершилась
без результата (внутреннее состояние «queued»)» вместо причины.

Механика была такая: ранние отказы (ссылка не разобралась, движка для сервиса
нет, учётку не выдали) честно звали `try_advance(ERROR)`, но в таблице
переходов из QUEUED вели только RUNNING и CANCELLED. Переход отвергался, задача
уходила из run_task нетерминальной, и страховочная сетка проставляла статус
ПРЯМЫМ присваиванием — тем самым обходом, который модуль и заведён запрещать.

Ребро QUEUED→ERROR добавлено. Тест держит и его, и то, что уже запрещённое не
разрешилось заодно: правило «терминальное состояние окончательно» дороже
удобства.
"""
import pytest

from ripster.task_state import (
    InvalidTransition,
    TaskStatus,
    advance,
    try_advance,
)


def _task(status: TaskStatus) -> dict:
    return {"status": status.value}


def test_task_can_fail_before_it_starts():
    t = _task(TaskStatus.QUEUED)
    advance(t, TaskStatus.ERROR)
    assert t["status"] == TaskStatus.ERROR.value


def test_try_advance_reports_success_for_early_failure():
    """Раньше здесь возвращался False — и причина отказа терялась молча."""
    t = _task(TaskStatus.QUEUED)
    assert try_advance(t, TaskStatus.ERROR) is True


def test_normal_start_still_works():
    t = _task(TaskStatus.QUEUED)
    advance(t, TaskStatus.RUNNING)
    assert t["status"] == TaskStatus.RUNNING.value


@pytest.mark.parametrize("frm,to", [
    (TaskStatus.DONE, TaskStatus.QUEUED),
    (TaskStatus.DONE, TaskStatus.RUNNING),
    (TaskStatus.ERROR, TaskStatus.RUNNING),
    (TaskStatus.CANCELLED, TaskStatus.RUNNING),
])
def test_terminal_states_stay_terminal(frm, to):
    """Новое ребро не должно было открыть дорогу обратно из финала."""
    t = _task(frm)
    with pytest.raises(InvalidTransition):
        advance(t, to)


def test_safety_net_no_longer_needs_to_bypass_the_model():
    """Раннер обязан проставлять отказ ЧЕРЕЗ модель, а не присваиванием."""
    import pathlib
    src = (pathlib.Path(__file__).resolve().parents[1] / "ripster" / "runner.py").read_text(encoding="utf-8")
    i = src.find("SAFETY NET")
    assert i > 0, "страховочная сетка исчезла — проверь, чем её заменили"
    window = src[i:i + 1600]
    assert "_try_advance_task(task, TaskStatus.ERROR)" in window, (
        "страховка снова ставит статус в обход task_state"
    )
