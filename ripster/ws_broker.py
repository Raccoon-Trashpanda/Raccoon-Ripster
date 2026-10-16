"""Per-client WebSocket fan-out broker.

The naive broadcast loop — ``for ws in clients: await ws.send_json(msg)`` —
blocks the whole fan-out on the slowest client: one stalled browser delays
realtime updates for everyone, and a half-dead socket can hang the loop.

This broker gives every client its own bounded queue and a dedicated sender
task. ``broadcast`` only ever *enqueues* (non-blocking), so a slow client can
never delay the others. On queue overflow the OLDEST message is dropped —
for a realtime UI the freshest state matters most, stale frames are noise.

Ownership: the broker owns the per-client send channels and their tasks.
Callers own the connection lifecycle and call register()/unregister().
"""
from __future__ import annotations

import asyncio
from typing import Any, Callable, Optional


class _Channel:
    """One client's outgoing queue + sender task."""
    __slots__ = ("ws", "queue", "task", "dropped")

    def __init__(self, ws: Any, maxsize: int) -> None:
        self.ws    = ws
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self.task: Optional[asyncio.Task] = None
        self.dropped = 0


class WebSocketBroker:
    def __init__(self, maxsize: int = 256) -> None:
        self._maxsize  = maxsize
        self._channels: dict[Any, _Channel] = {}
        self._on_dead: Optional[Callable[[Any], None]] = None

    def set_dead_handler(self, fn: Callable[[Any], None]) -> None:
        """Register a callback invoked when a client's socket dies, so the
        caller can drop it from its own bookkeeping."""
        self._on_dead = fn

    # ── Lifecycle ──────────────────────────────────────────────────────────

    def register(self, ws: Any) -> None:
        """Start a send channel for *ws*. Must run inside the event loop."""
        if ws in self._channels:
            return
        ch = _Channel(ws, self._maxsize)
        ch.task = asyncio.create_task(self._sender(ch))
        self._channels[ws] = ch

    def unregister(self, ws: Any) -> None:
        ch = self._channels.pop(ws, None)
        if ch and ch.task and not ch.task.done():
            ch.task.cancel()

    @property
    def clients(self) -> list:
        return list(self._channels.keys())

    def count(self) -> int:
        return len(self._channels)

    # ── Send ───────────────────────────────────────────────────────────────

    def enqueue(self, ws: Any, msg: dict) -> None:
        """Queue *msg* for one client. Non-blocking; drops the oldest queued
        message if the client is backed up (slow/stalled)."""
        ch = self._channels.get(ws)
        if ch is None:
            return
        try:
            ch.queue.put_nowait(msg)
        except asyncio.QueueFull:
            try:
                ch.queue.get_nowait()       # drop oldest, keep newest
                ch.dropped += 1
                ch.queue.put_nowait(msg)
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                pass

    # ── Sender task ────────────────────────────────────────────────────────

    async def _sender(self, ch: _Channel) -> None:
        try:
            while True:
                msg = await ch.queue.get()
                try:
                    await ch.ws.send_json(msg)
                except Exception:
                    break          # socket gone — stop draining
        except asyncio.CancelledError:
            return
        finally:
            self._channels.pop(ch.ws, None)
            if self._on_dead:
                try:
                    self._on_dead(ch.ws)
                except Exception:
                    pass
