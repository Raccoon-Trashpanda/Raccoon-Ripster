"""
Queue management — state + lifecycle in one place.
No global variables; everything lives on QueueService instance.
"""
from __future__ import annotations
import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional, Callable, Awaitable


# ── Task model ────────────────────────────────────────────────────────────

@dataclass
class Task:
    url:      str
    quality:  str
    id:       str           = field(default_factory=lambda: uuid.uuid4().hex[:8])
    status:   str           = "queued"   # queued | running | done | error | cancelled
    progress: int           = 0
    log:      list[str]     = field(default_factory=list)
    meta:     dict          = field(default_factory=dict)
    engine:   str           = ""
    source:   str           = "manual"  # manual | watchlist | batch | search

    def snapshot(self) -> dict:
        """Return serialisable dict without full log."""
        return {k: v for k, v in self.__dict__.items() if k != "log"}

    def is_active(self) -> bool:
        return self.status in ("queued", "running")


# ── Queue state ───────────────────────────────────────────────────────────

class QueueService:
    """
    Owns the download queue and its lifecycle.
    All mutations go through this object.
    """

    def __init__(self) -> None:
        self._tasks:      list[Task]                      = []
        self._lock:       asyncio.Lock                    = asyncio.Lock()
        self._running:    bool                            = False
        self._paused:     bool                            = False
        self.active_proc: Optional[asyncio.subprocess.Process] = None
        # Injected worker coroutine
        self._worker_fn:  Optional[Callable[[Task], Awaitable[None]]] = None
        self._worker_task: Optional[asyncio.Task] = None

    # ── Public accessors ──────────────────────────────────────────────────

    @property
    def running(self) -> bool:
        return self._running

    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def tasks(self) -> list[Task]:
        return self._tasks

    def snapshot(self) -> list[dict]:
        return [t.snapshot() for t in self._tasks]

    def get(self, task_id: str) -> Optional[Task]:
        return next((t for t in self._tasks if t.id == task_id), None)

    # ── Mutation ──────────────────────────────────────────────────────────

    async def add(self, url: str, quality: str,
                  source: str = "manual", **meta) -> Task:
        """Add task; reject duplicates that are still active."""
        async with self._lock:
            if any(t.url == url and t.is_active() for t in self._tasks):
                raise ValueError(f"Already in queue: {url}")
            t = Task(url=url, quality=quality, source=source, meta=meta)
            self._tasks.append(t)
        return t

    def update_task(self, task_id: str, **kwargs) -> None:
        t = self.get(task_id)
        if t:
            for k, v in kwargs.items():
                if hasattr(t, k):
                    setattr(t, k, v)

    def append_log(self, task_id: str, line: str) -> None:
        t = self.get(task_id)
        if t:
            t.log.append(line)

    async def remove(self, task_id: str) -> bool:
        async with self._lock:
            before = len(self._tasks)
            self._tasks = [t for t in self._tasks if t.id != task_id]
            return len(self._tasks) < before

    async def clear_done(self) -> int:
        async with self._lock:
            before = len(self._tasks)
            self._tasks = [t for t in self._tasks if t.status == "queued" or t.status == "running"]
            return before - len(self._tasks)

    async def clear_all(self) -> None:
        async with self._lock:
            self._tasks.clear()

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def set_worker(self, fn: Callable[[Task], Awaitable[None]]) -> None:
        """Register the coroutine that processes a single task."""
        self._worker_fn = fn

    async def start(self) -> bool:
        """Start processing; returns False if already running."""
        async with self._lock:
            if self._running:
                return False
            has_work = any(t.status == "queued" for t in self._tasks)
            if not has_work:
                return False
            self._running = True
            self._paused  = False
        self._worker_task = asyncio.create_task(self._loop())
        return True

    async def pause(self) -> None:
        self._paused = True

    async def resume(self) -> None:
        self._paused = False

    async def stop(self) -> None:
        """Stop after current task finishes."""
        self._running = False
        self._paused  = False
        if self.active_proc:
            try:
                self.active_proc.terminate()
            except Exception:
                pass

    # ── Internal loop ─────────────────────────────────────────────────────

    async def _loop(self) -> None:
        """Main worker loop — runs tasks one at a time."""
        while self._running:
            if self._paused:
                await asyncio.sleep(0.5)
                continue
            pending = [t for t in self._tasks if t.status == "queued"]
            if not pending:
                break
            task = pending[0]
            task.status = "running"
            if self._worker_fn:
                try:
                    await self._worker_fn(task)
                except Exception as e:
                    task.status  = "error"
                    task.log.append(f"Worker error: {e}")
                    import traceback
                    traceback.print_exc()
            await asyncio.sleep(0.1)

        async with self._lock:
            self._running = False
        self.active_proc = None
