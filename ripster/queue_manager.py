"""
QueueManager — replaces the _qs bag-of-flags dict.

State machine:
    IDLE  ──start()──►  RUNNING  ──pause()──►  PAUSED
      ▲                    │                     │
      └────────stop()──────┘◄────resume()────────┘

Public API (use these going forward):
    qm.start()            → bool (False if already running)
    qm.stop()             → None
    qm.toggle_pause()     → bool (new is_paused value)
    qm.is_running         → bool (RUNNING or PAUSED)
    qm.is_paused          → bool
    qm.state              → QueueState
    qm.lock               → asyncio.Lock (for atomic start/stop)
    qm.register_runner(tid, runner)
    qm.unregister_runner(tid)
    qm.get_runner(tid)    → runner | None
    qm.runners            → dict[str, runner]
    qm.active_tasks       → dict[str, asyncio.Task]
    qm.proc / qm.proc=    → legacy single-process slot
    await qm.cancel_all() → cancel every runner + asyncio task

Backward-compat dict interface (for gradual migration):
    qm["running"] / qm["paused"] / qm["runners"] / qm["active_tasks"]
    qm["running"] = True/False  →  start() / stop()
    qm["paused"]  = True/False  →  pause state update
    qm.get(key, default)
"""
from __future__ import annotations

import asyncio
from enum import Enum
from typing import Any, Optional


class QueueState(Enum):
    IDLE    = "idle"
    RUNNING = "running"
    PAUSED  = "paused"


class QueueManager:
    def __init__(self) -> None:
        self._state: QueueState = QueueState.IDLE
        self._lock  = asyncio.Lock()
        self._runners:      dict[str, Any]          = {}
        self._active_tasks: dict[str, asyncio.Task] = {}
        self._proc:   Any = None   # legacy single-process slot
        self._runner: Any = None   # legacy single-runner backward compat

    # ── State machine ──────────────────────────────────────────────────────

    @property
    def state(self) -> QueueState:
        return self._state

    @property
    def is_running(self) -> bool:
        """True while queue is active (running or paused)."""
        return self._state in (QueueState.RUNNING, QueueState.PAUSED)

    @property
    def is_paused(self) -> bool:
        return self._state == QueueState.PAUSED

    @property
    def lock(self) -> asyncio.Lock:
        return self._lock

    def start(self) -> bool:
        """Transition IDLE → RUNNING. Returns True if state changed."""
        if self._state == QueueState.IDLE:
            self._state = QueueState.RUNNING
            return True
        return False

    def stop(self) -> None:
        """Transition any state → IDLE."""
        self._state = QueueState.IDLE

    def toggle_pause(self) -> bool:
        """Toggle RUNNING↔PAUSED. Returns new is_paused value."""
        if self._state == QueueState.RUNNING:
            self._state = QueueState.PAUSED
            return True
        if self._state == QueueState.PAUSED:
            self._state = QueueState.RUNNING
            return False
        return False

    # ── Runner registry ────────────────────────────────────────────────────

    @property
    def runners(self) -> dict[str, Any]:
        return self._runners

    @property
    def active_tasks(self) -> dict[str, asyncio.Task]:
        return self._active_tasks

    def register_runner(self, tid: str, runner: Any) -> None:
        self._runners[tid] = runner
        self._runner = runner   # backward compat

    def unregister_runner(self, tid: str) -> None:
        self._runners.pop(tid, None)
        if not self._runners:
            self._runner = None

    def get_runner(self, tid: str) -> Optional[Any]:
        return self._runners.get(tid)

    # ── Legacy proc slot ───────────────────────────────────────────────────

    @property
    def proc(self) -> Any:
        return self._proc

    @proc.setter
    def proc(self, value: Any) -> None:
        self._proc = value

    # ── Bulk cancel ───────────────────────────────────────────────────────

    async def cancel_all(self) -> None:
        """Cancel every active ProcessRunner and asyncio task."""
        runners = dict(self._runners)
        tasks   = dict(self._active_tasks)
        proc    = self._proc

        for runner in runners.values():
            try:
                await runner.cancel()
            except Exception:
                pass

        for at in tasks.values():
            if not at.done():
                at.cancel()

        # Legacy: single-proc fallback
        if not runners and proc is not None:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
            except Exception as err:
                print(f"[queue] terminate failed: {err}", flush=True)
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            except Exception:
                pass

    # ── Backward-compat dict interface ─────────────────────────────────────
    # Allows existing  _qs["running"] / _qs["paused"] = ...  code to keep
    # working unchanged during the incremental migration.

    def __getitem__(self, key: str) -> Any:
        if key == "running":       return self.is_running
        if key == "paused":        return self.is_paused
        if key == "proc":          return self._proc
        if key == "runner":        return self._runner
        if key == "runners":       return self._runners
        if key == "active_tasks":  return self._active_tasks
        raise KeyError(key)

    def __setitem__(self, key: str, value: Any) -> None:
        if key == "running":
            if value and self._state == QueueState.IDLE:
                self._state = QueueState.RUNNING
            elif not value:
                self._state = QueueState.IDLE
            return
        if key == "paused":
            if value and self._state == QueueState.RUNNING:
                self._state = QueueState.PAUSED
            elif not value and self._state == QueueState.PAUSED:
                self._state = QueueState.RUNNING
            return
        if key == "proc":          self._proc = value;          return
        if key == "runner":        self._runner = value;        return
        if key == "runners":       self._runners = value;       return
        if key == "active_tasks":  self._active_tasks = value;  return
        raise KeyError(key)

    def get(self, key: str, default: Any = None) -> Any:
        try:
            return self[key]
        except KeyError:
            return default
