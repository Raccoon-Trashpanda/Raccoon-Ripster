"""
AppContext — single container for shared application runtime state.

Replaces the service-locator anti-pattern where install() functions
received 5–14 individual arguments. Each module now receives one object
and reads only the fields it actually needs.

Usage:
    from ripster.app_context import AppContext
    def install(app, ctx: AppContext) -> None:
        global _cfg, _broadcast
        _cfg       = ctx.config
        _broadcast = ctx.broadcast
        app.include_router(router)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from ripster.config_service import ConfigService
from ripster.queue_manager import QueueManager


@dataclass
class AppContext:
    # ── Config ────────────────────────────────────────────────────────────
    config:         ConfigService               # mutable; modules read/write via save_config
    save_config:    Callable[[ConfigService], None]  # persist config.yaml atomically

    # ── Queue ─────────────────────────────────────────────────────────────
    queue:          list                        # live task list
    queue_manager:  QueueManager               # state machine + runner registry

    # ── History ───────────────────────────────────────────────────────────
    download_history: list
    save_history:   Callable[[list], None]

    # ── WebSocket ─────────────────────────────────────────────────────────
    broadcast:      Callable                   # async broadcast(msg: dict)

    # ── Service / URL ─────────────────────────────────────────────────────
    detect_service: Callable[[str], str]       # url → "apple" | "qobuz" | …

    # ── File system ───────────────────────────────────────────────────────
    base_dir:       Path
    is_windows:     bool

    # ── Optional: watchlist ───────────────────────────────────────────────
    watchlist:          list                  = field(default_factory=list)
    save_watchlist:     Optional[Callable]    = None

    # ── Optional: auth / session ──────────────────────────────────────────
    owner_auth_fn:      Optional[Callable]    = None   # guest.py: verify owner session cookie

    # ── Optional: meta / engine helpers (core.py, isrc.py, queue.py) ─────
    fetch_meta:         Optional[Callable]    = None   # async fetch_meta(url, svc) → dict
    get_engine:         Optional[Callable]    = None   # get_engine(name) → EngineBase
    get_qualities:      Optional[Callable]    = None   # get_qualities(svc) → list[str]
    auto_fetch_bearer:  Optional[Callable]    = None   # async auto_fetch_bearer()
    load_html:          Optional[Callable]    = None   # load_html() → str
    app_info:           dict                  = field(default_factory=dict)

    # ── Optional: queue pipeline helpers ─────────────────────────────────
    queue_snapshot:     Optional[Callable]    = None   # queue_snapshot() → list (safe copy)
    validate_url:       Optional[Callable]    = None   # validate_url(url) → (ok, msg)
    enrich_meta:        Optional[Callable]    = None   # async enrich_meta(task) → None
    default_quality:    Optional[Callable]    = None   # default_quality(svc) → str
    engine_for_svc:     Optional[Callable]    = None   # engine_for_svc(svc) → str
    process_queue:      Optional[Callable]    = None   # async process_queue() coroutine
