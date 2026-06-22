"""
Task state machine.

The old code had ``task["status"] = "running"`` as stringly-typed assignments
scattered across the codebase. Nobody checked that the transition was legal,
so you could go from ``done`` back to ``queued`` by accident — and the UI
would render the task as fresh when it had already finished.

This module makes status a typed enum and forces every transition through
``advance(task, new)``, which validates that the move is actually legal
given the current state.

Illegal transitions raise ``InvalidTransition`` — callers should either fix
the calling code, or use ``try_advance`` when a best-effort move is desired.

The legal-transition table is deliberately conservative. If you need a new
edge (e.g. manual requeue of a failed task), add it explicitly — don't work
around the check by bypassing this module.
"""
from __future__ import annotations

from enum import Enum
from typing import Optional


class TaskStatus(str, Enum):
    QUEUED    = "queued"
    RUNNING   = "running"
    DONE      = "done"
    ERROR     = "error"
    CANCELLED = "cancelled"
    # Transient: the task is queued but blocked on metadata fetch. Not used
    # as a blocker today — kept for future use once enrichment is mandatory.
    PENDING   = "pending"


# Legal transitions. Entries not in this table are rejected by advance().
# Read as: FROM state → {allowed TO states}.
_TRANSITIONS: dict[TaskStatus, set[TaskStatus]] = {
    TaskStatus.PENDING:   {TaskStatus.QUEUED, TaskStatus.CANCELLED},
    # Running→queued is the "stop and requeue" edge used by /api/queue/stop.
    # It's deliberately allowed: pressing Stop should leave pending work that
    # the user can resume by pressing Start again, not force them to re-add
    # every URL. Done→queued (restart a finished task) is still forbidden —
    # that's what the new-task path is for.
    TaskStatus.QUEUED:    {TaskStatus.RUNNING, TaskStatus.CANCELLED},
    TaskStatus.RUNNING:   {TaskStatus.DONE, TaskStatus.ERROR, TaskStatus.CANCELLED, TaskStatus.QUEUED},
    # Terminal states — no further transitions. A task that finished is
    # finished; requeue means *creating a new task*, not mutating this one.
    TaskStatus.DONE:      set(),
    TaskStatus.ERROR:     set(),
    TaskStatus.CANCELLED: set(),
}

TERMINAL_STATES = frozenset({TaskStatus.DONE, TaskStatus.ERROR, TaskStatus.CANCELLED})


class InvalidTransition(ValueError):
    """Raised when code tries to move a task between states that aren't
    connected in ``_TRANSITIONS``. Always a programmer error — fix the caller."""


def current(task: dict) -> TaskStatus:
    """Read the task's current status as an enum. Falls back to QUEUED for
    tasks that don't have a status set yet (fresh-from-dict)."""
    raw = task.get("status", TaskStatus.QUEUED.value)
    try:
        return TaskStatus(raw)
    except ValueError:
        # Unknown string — treat as terminal error, don't pretend it's a known
        # state. This surfaces corrupted history entries rather than hiding them.
        return TaskStatus.ERROR


def can_advance(task: dict, new: TaskStatus) -> bool:
    """True iff the transition from the task's current state to ``new``
    is in the legal-transition table."""
    cur = current(task)
    return new in _TRANSITIONS.get(cur, set())


def advance(task: dict, new: TaskStatus) -> None:
    """Apply a status transition, rejecting illegal moves.

    Mutates ``task['status']`` on success. On illegal moves raises
    ``InvalidTransition`` — this is a loud failure by design: a bad status
    update elsewhere in the code is a bug I want to see, not hide.
    """
    cur = current(task)
    if new == cur:
        return  # no-op
    if new not in _TRANSITIONS.get(cur, set()):
        raise InvalidTransition(
            f"Illegal transition for task {task.get('id','?')}: "
            f"{cur.value} → {new.value}"
        )
    task["status"] = new.value


def try_advance(task: dict, new: TaskStatus) -> bool:
    """Best-effort transition. Returns True if applied, False if the move
    was illegal. Use this when the caller genuinely doesn't mind being
    ignored — e.g. setting a task to ``cancelled`` from a global stop when
    the task may already be done."""
    try:
        advance(task, new)
        return True
    except InvalidTransition:
        return False


def is_terminal(task: dict) -> bool:
    """True iff the task can no longer transition to any other state."""
    return current(task) in TERMINAL_STATES
