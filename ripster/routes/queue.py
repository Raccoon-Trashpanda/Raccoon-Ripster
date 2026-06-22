"""
Queue management routes.

  GET    /api/queue
  POST   /api/queue/add
  DELETE /api/queue/{task_id}
  POST   /api/queue/clear
  POST   /api/queue/start
  POST   /api/queue/pause
  POST   /api/queue/stop
  POST   /api/queue/batch
  GET    /api/stats

Install: queue.install(app, ctx)
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request


def _make_task(url: str, quality: str, engine: str, svc: str,
               source: str = "manual", session_id: str = "") -> dict:
    """Single factory so add_to_queue and queue_batch always produce identical shapes."""
    return {
        "id":         str(uuid.uuid4())[:8],
        "url":        url,
        "quality":    quality,
        "engine":     engine,
        "service":    svc,
        "status":     "queued",
        "progress":   0,
        "meta":       {"service": svc},
        "log":        [],
        "added":      datetime.now().strftime("%H:%M:%S"),
        "source":     source,
        "session_id": session_id,   # "" = owner, non-empty = guest session
    }

router = APIRouter()

_queue:   list  = []
_qs      = None   # QueueManager — set by install()
_lock:    asyncio.Lock | None = None
_cfg:     dict  = {}
_broadcast       = None
_process_queue   = None
_queue_snapshot  = None
_validate_url    = None
_enrich_meta     = None
_history: list  = []
_detect_service  = None
_default_quality = None
_engine_for_svc  = None


def install(app, ctx) -> None:
    global _queue, _qs, _lock, _cfg, _broadcast
    global _process_queue, _queue_snapshot, _validate_url, _enrich_meta
    global _history, _detect_service, _default_quality, _engine_for_svc
    _queue          = ctx.queue
    _qs             = ctx.queue_manager
    _lock           = ctx.queue_manager.lock
    _cfg            = ctx.config
    _broadcast      = ctx.broadcast
    _process_queue  = ctx.process_queue
    _queue_snapshot = ctx.queue_snapshot
    _validate_url   = ctx.validate_url
    _enrich_meta    = ctx.enrich_meta
    _history        = ctx.download_history
    _detect_service = ctx.detect_service
    _default_quality = ctx.default_quality
    _engine_for_svc  = ctx.engine_for_svc
    app.include_router(router)


# ── Routes ─────────────────────────────────────────────────────────────────

def _guest_session_id(request: Request) -> str:
    """Public build has no guest mode — every request is the owner/local user."""
    return ""


@router.get("/api/queue")
async def get_queue(request: Request):
    sid = _guest_session_id(request)
    if sid:
        return [
            {k: v for k, v in t.items() if k not in ("log", "session_id")}
            for t in _queue
            if t.get("session_id") == sid
        ]
    return _queue_snapshot()


@router.post("/api/queue/delivered/{task_id}")
async def mark_task_delivered(task_id: str):
    """Bot acks that it has uploaded EVERY file of a task to the chat. Stamps the
    manifest so auto_cleanup may reclaim the folder; until acked, the folder is
    protected from the time-sweep (it can't be eaten mid-delivery)."""
    from ripster import download_manifest as _dm
    ok = _dm.mark_delivered(task_id)
    return {"ok": bool(ok)}


@router.post("/api/queue/add")
async def add_to_queue(body: dict, request: Request):
    url = (body.get("url") or "").strip()
    if not url:
        raise HTTPException(400, "URL is required")
    if _validate_url and not _validate_url(url):
        raise HTTPException(400,
            "URL not from a supported service. Supported: Apple Music, "
            "Deezer, Qobuz, Tidal, Spotify, SoundCloud, Beatport, Yandex Music")

    svc     = _detect_service(url) if _detect_service else "apple"

    # Apple radio stations / DJ-mix episodes (/station/.../ra.<id>) are a
    # DRM-protected RADIO STREAM (playParams.kind=radioStation, hasDrm=true), NOT
    # a catalog track/album — none of the engines (gamdl/zhaarey/amd) can download
    # them. Reject up front with an honest reason so the bot/web/guest get a clear
    # message instead of a queued task that fails cryptically. (See task #8.)
    _ul = url.lower()
    if svc == "apple" and ("/station/" in _ul
                           or _ul.rstrip("/").split("/")[-1].split("?")[0].startswith("ra.")):
        raise HTTPException(422,
            "Apple-радио и станции (DJ-миксы, ссылки ra.*) скачать нельзя — это "
            "защищённый DRM radio-поток, а не трек/альбом каталога. Дай ссылку на "
            "трек, альбом или плейлист.")

    sid = ""
    quality = body.get("quality") or (_default_quality(svc) if _default_quality else _cfg.get("quality", "alac"))
    engine  = body.get("engine")  or (_engine_for_svc(svc)  if _engine_for_svc  else _cfg.get("engine", "zhaarey"))

    # Guard: an Apple-centric quality (alac/atmos/aac…) sent for a non-Apple
    # service is meaningless — the engine would silently drop to MP3. Fall back
    # to that service's own default (Deezer→flac, Qobuz→27, Tidal→lossless).
    _APPLE_ONLY = {"alac", "atmos", "ac3", "aac", "aac-legacy", "aac-lc",
                   "aac-binaural", "aac-downmix"}
    if svc not in ("apple", "spotify") and (quality or "").lower() in _APPLE_ONLY:
        quality = (_default_quality(svc) if _default_quality
                   else {"deezer": "flac", "qobuz": "27", "tidal": "lossless"}.get(svc, "flac"))

    _BEATPORT_QUALS = {"hifi", "high", "minimum", "lossless"}
    if svc == "beatport" and (quality or "").lower() not in _BEATPORT_QUALS:
        quality = (_default_quality(svc) if _default_quality else "hifi")

    # Smart Apple routing: pick the engine + wrapper that can actually deliver
    # the requested quality right now — video→gamdl (cookies+bundled CDM),
    # ALAC/Atmos→amd (public wrapper) or zhaarey (local wrapper), aac→gamdl —
    # degrading to the best available result when no wrapper is reachable.
    # Music-video URLs always force the video path regardless of selected codec.
    _route_note = ""
    if svc == "apple":
        # Single source of truth for Apple routing: route_apple inspects the URL
        # (music-video detection + region) and the requested quality, then picks
        # the engine + wrapper that can deliver it (see ripster/apple_router.py).
        from ripster.apple_router import route_apple, resolve_available_url
        # Pre-release / regional availability: if the link's storefront can't
        # stream it yet, rewrite to a region that can (public wrapper pulls it).
        url, _avail_note = await resolve_available_url(url, _cfg)
        # route_apple may do a sync httpx probe (up to ~6s) of the public wrapper
        # on a cache miss — run it off the event loop so adding an Apple URL can
        # never freeze the whole server.
        routed = await asyncio.to_thread(route_apple, quality, _cfg, url)
        engine, quality = routed["engine"], routed["quality"]
        _route_note = routed.get("note", "")
        if _avail_note:
            _route_note = (_avail_note + " · " + _route_note) if _route_note else _avail_note

    source  = body.get("source", "manual")

    if svc == "spotify" and engine != "orpheus_spotify" and not body.get("allow_spotify"):
        return {"ok": False, "msg": "Spotify URLs must be converted first",
                "spotify": True, "url": url}

    # ── Deezer album → single album task (NOT per-track) ──────────────────────
    # We deliberately download Deezer albums/playlists as ONE task in deemix's
    # native album mode. The old per-track expansion (one queue item per track)
    # caused three problems the user hit:
    #   1. N cards per album instead of one — the queue became unreadable.
    #   2. Duplicate files (e.g. "… _2.m4a"): every per-track task re-ran the
    #      folder-wide post-processing (transcode / retag / rename) over the
    #      same album folder, so renames collided and produced second copies.
    #   3. "downloads per-track instead of the album" — exactly what was asked
    #      to stop.
    # deemix album mode keeps the whole release in one folder, names tracks
    # natively (no collisions), and skips tracks that are unavailable in the
    # ARL's region rather than aborting — so a single task is both cleaner and
    # robust. If a fresh release comes back short, retry grabs the gaps.
    # Expansion is intentionally disabled; resolve() is no longer called here.
    expanded: list[dict] = []

    # Caller-supplied metadata (SoundCloud tiles, release radar, etc. already
    # have cover/title/duration) — merge so the queue card shows it instantly.
    pre_meta = body.get("meta") if isinstance(body.get("meta"), dict) else None

    def _enqueue_one(turl: str, tmeta: dict | None, mark_enriched: bool):
        """Create + append one task. Returns the task, or None if it's an exact dupe."""
        # Same URL is OK at a different quality OR engine — a deliberate second
        # copy (e.g. FLAC vs MP3). Only block exact dupes that are still active.
        if any(t["url"] == turl
               and (t.get("quality") or "") == (quality or "")
               and (t.get("engine")  or "") == (engine  or "")
               and t["status"] in ("queued", "running") for t in _queue):
            return None
        t = _make_task(turl, quality, engine, svc, source, session_id=sid)
        if _route_note:
            t.setdefault("meta", {})["route_note"] = _route_note
        if tmeta:
            t["meta"].update({k: v for k, v in tmeta.items() if v not in (None, "")})
            if mark_enriched:
                t["meta"]["enriched"] = True
        _queue.append(t)
        return t

    added: list = []
    if expanded:
        total = len(expanded)
        for it in expanded:
            turl = it.get("url")
            if not turl:
                continue
            # Per-track meta is already complete (title/artist/cover) — mark it
            # enriched so the enricher doesn't re-fetch N times and hammer the API.
            t = _enqueue_one(turl, {
                "service":     svc,
                "title":       it.get("title", ""),
                "artist":      it.get("artist", ""),
                "artworkUrl":  it.get("artwork_url", ""),
                "trackNumber": it.get("track_num"),
                "totalTracks": total,
            }, mark_enriched=True)
            if t is not None:
                added.append(t)
    else:
        t = _enqueue_one(url, pre_meta, mark_enriched=True)
        if t is not None:
            added.append(t)
            if _enrich_meta:
                asyncio.create_task(_enrich_meta(t))

    if not added:
        return {"ok": False, "msg": "Already in queue", "duplicate": True}

    if _broadcast:
        await _broadcast({"type": "queue_update", "queue": _queue_snapshot()})

    if _cfg.get("queue-autostart", True):
        async with _lock:
            should_start = _qs.start()
        if should_start and _process_queue:
            asyncio.create_task(_process_queue())

    if expanded:
        return {"ok": True, "id": added[0]["id"], "ids": [t["id"] for t in added],
                "count": len(added), "service": svc, "engine": engine,
                "quality": quality, "expanded": True}
    return {"ok": True, "id": added[0]["id"], "service": svc,
            "engine": engine, "quality": quality}


@router.delete("/api/queue/{task_id}")
async def remove_task(task_id: str, request: Request):
    sid = _guest_session_id(request)
    if sid:
        # Guests may only remove their own tasks
        task = next((t for t in _queue if t["id"] == task_id), None)
        if task and task.get("session_id") != sid:
            from fastapi.responses import JSONResponse
            return JSONResponse({"error": "forbidden"}, status_code=403)
    _queue[:] = [t for t in _queue if t["id"] != task_id]
    if _broadcast:
        await _broadcast({"type": "queue_update", "queue": _queue_snapshot()})
    return {"ok": True}


@router.post("/api/queue/move")
async def move_task(body: dict, request: Request):
    """Reorder a QUEUED task for priority — the runner picks queued tasks in
    list order, so moving one earlier makes it start sooner (when its service
    lane frees). to ∈ top | up | down | bottom. Owner-only (admin)."""
    if _guest_session_id(request):
        from fastapi.responses import JSONResponse
        return JSONResponse({"error": "forbidden"}, status_code=403)
    tid = str(body.get("task_id") or "")
    where = (body.get("to") or "top").lower()
    idx = next((i for i, t in enumerate(_queue) if t["id"] == tid), -1)
    if idx < 0:
        raise HTTPException(404, "task not found")
    task = _queue.pop(idx)
    if where == "top":
        pos = next((i for i, t in enumerate(_queue) if t["status"] == "queued"), len(_queue))
        _queue.insert(pos, task)
    elif where == "bottom":
        _queue.append(task)
    elif where == "up":
        _queue.insert(max(0, idx - 1), task)
    else:  # down
        _queue.insert(min(len(_queue), idx + 1), task)
    if _broadcast:
        await _broadcast({"type": "queue_update", "queue": _queue_snapshot()})
    return {"ok": True}


@router.post("/api/queue/hold")
async def hold_task(body: dict, request: Request):
    """Pause / resume a single QUEUED task. A held task gets status 'paused' so
    the runner skips it (it only starts status=='queued'); resume puts it back.
    Running tasks can't be paused mid-download — use stop (DELETE). Owner-only."""
    if _guest_session_id(request):
        from fastapi.responses import JSONResponse
        return JSONResponse({"error": "forbidden"}, status_code=403)
    tid = str(body.get("task_id") or "")
    resume = bool(body.get("resume"))
    task = next((t for t in _queue if t["id"] == tid), None)
    if not task:
        raise HTTPException(404, "task not found")
    if resume and task["status"] == "paused":
        task["status"] = "queued"
    elif not resume and task["status"] == "queued":
        task["status"] = "paused"
    elif not resume and task["status"] == "running":
        # Pause a RUNNING task: flag it, then cancel its asyncio task. The runner's
        # CancelledError handler sees the flag and parks it as 'paused' (not
        # cancelled). Resume re-queues it; skip-existing makes the re-run fast.
        task["_pause_requested"] = True
        try:
            at = (getattr(_qs, "active_tasks", {}) or {}).get(tid)
            if at is not None:
                at.cancel()
        except Exception:
            pass
    if _broadcast:
        await _broadcast({"type": "queue_update", "queue": _queue_snapshot()})
    return {"ok": True, "status": task["status"]}


@router.post("/api/queue/clear")
async def clear_queue(request: Request):
    sid = _guest_session_id(request)
    if sid:
        from fastapi.responses import JSONResponse
        return JSONResponse({"error": "forbidden"}, status_code=403)
    _queue[:] = [t for t in _queue if t["status"] == "running"]
    if _broadcast:
        await _broadcast({"type": "queue_update", "queue": _queue_snapshot()})
    return {"ok": True}


@router.post("/api/queue/start")
async def start_queue(request: Request):
    if _guest_session_id(request):
        from fastapi.responses import JSONResponse
        return JSONResponse({"error": "forbidden"}, status_code=403)
    async with _lock:
        if _qs.is_running:
            return {"ok": False, "msg": "Already running"}
        if not any(t["status"] == "queued" for t in _queue):
            return {"ok": False, "msg": "No queued items"}
        _qs.start()
    if _process_queue:
        asyncio.create_task(_process_queue())
    if _broadcast:
        await _broadcast({"type": "queue_started"})
    return {"ok": True}


@router.post("/api/queue/pause")
async def pause_queue(request: Request):
    if _guest_session_id(request):
        from fastapi.responses import JSONResponse
        return JSONResponse({"error": "forbidden"}, status_code=403)
    async with _lock:
        now_paused = _qs.toggle_pause()
        state = "paused" if now_paused else "resumed"
    if _broadcast:
        await _broadcast({"type": "queue_" + state})
    return {"ok": True, "paused": now_paused}


@router.post("/api/queue/stop")
async def stop_queue(request: Request):
    if _guest_session_id(request):
        from fastapi.responses import JSONResponse
        return JSONResponse({"error": "forbidden"}, status_code=403)
    async with _lock:
        _qs.stop()
        from ripster.task_state import TaskStatus, current as _task_status, try_advance as _try_advance
        for t in _queue:
            if _task_status(t) == TaskStatus.RUNNING:
                _try_advance(t, TaskStatus.QUEUED)
                t["progress"] = 0

    await _qs.cancel_all()

    if _broadcast:
        await _broadcast({"type": "queue_stopped"})
        await _broadcast({"type": "queue_update", "queue": _queue_snapshot()})
    return {"ok": True}


@router.post("/api/queue/batch")
async def queue_batch(body: dict, request: Request):
    if _guest_session_id(request):
        from fastapi.responses import JSONResponse
        return JSONResponse({"error": "forbidden"}, status_code=403)
    text            = body.get("text", "")
    fallback_quality = body.get("quality") or _cfg.get("quality", "alac")
    urls = [ln.strip() for ln in text.splitlines() if ln.strip().startswith("http")]
    if not urls:
        return {"ok": False, "added": 0, "error": "No URLs found"}
    added = 0
    for url in urls[:50]:
        if _validate_url and not _validate_url(url):
            continue
        svc     = _detect_service(url) if _detect_service else "apple"
        quality = _default_quality(svc) if _default_quality and svc != "apple" else fallback_quality
        engine  = _engine_for_svc(svc) if _engine_for_svc else _cfg.get("engine", "zhaarey")
        task = _make_task(url, quality, engine, svc, "batch")
        _queue.append(task)
        if _enrich_meta:
            asyncio.create_task(_enrich_meta(task))
        added += 1
    if _broadcast:
        await _broadcast({"type": "queue_update", "queue": _queue_snapshot()})
    if added:
        async with _lock:
            should_start = _qs.start()
        if should_start and _process_queue:
            asyncio.create_task(_process_queue())
    return {"ok": True, "added": added}


@router.post("/api/queue/retry/{task_id}")
async def retry_task(task_id: str, request: Request):
    """Retry an error/cancelled task.

    If the task is still in the live queue, reset it *in place* — same id,
    same card, same position — so the user doesn't get a confusing second
    entry. Only when the task is gone from the queue (e.g. after a server
    restart) is it re-created from history.
    """
    sid = _guest_session_id(request)

    async def _kick_queue() -> None:
        if _cfg.get("queue-autostart", True):
            async with _lock:
                should_start = _qs.start()
            if should_start and _process_queue:
                asyncio.create_task(_process_queue())

    # ── In-place retry: task still in the queue ──────────────────────────────
    live = next((t for t in _queue if t["id"] == task_id), None)
    if live is not None:
        if sid and live.get("session_id") != sid:
            from fastapi.responses import JSONResponse
            return JSONResponse({"error": "forbidden"}, status_code=403)
        if live.get("status") in ("queued", "running"):
            return {"ok": False, "msg": "Задача уже в очереди", "duplicate": True}
        live["status"]   = "queued"
        live["progress"] = 0
        live["log"]      = []
        for k in ("_start_time", "_done_time", "_save_dir", "_retry_count",
                  "_auto_retry", "_amd_fallback", "_prog_total", "_prog_current"):
            live.pop(k, None)
        if _broadcast:
            await _broadcast({"type": "queue_update", "queue": _queue_snapshot()})
        await _kick_queue()
        return {"ok": True, "id": task_id, "reused": True}

    # ── Fallback: task no longer in the queue → re-create from history ───────
    original = next((h for h in _history if h.get("id") == task_id), None)
    if original is None:
        raise HTTPException(404, "Task not found")
    if sid and original.get("session_id") != sid:
        from fastapi.responses import JSONResponse
        return JSONResponse({"error": "forbidden"}, status_code=403)

    url     = original.get("url", "")
    quality = original.get("quality") or (_default_quality(original.get("service","")) if _default_quality else _cfg.get("quality","alac"))
    engine  = original.get("engine")  or (_engine_for_svc(original.get("service","")) if _engine_for_svc else _cfg.get("engine","zhaarey"))
    svc     = original.get("service", "apple")

    if any(t["url"] == url and t["status"] in ("queued", "running") for t in _queue):
        return {"ok": False, "msg": "Already in queue", "duplicate": True}

    task = _make_task(url, quality, engine, svc, "retry", session_id=original.get("session_id",""))
    task["meta"] = dict(original.get("meta") or {})
    _queue.append(task)

    if _broadcast:
        await _broadcast({"type": "queue_update", "queue": _queue_snapshot()})
    if _enrich_meta:
        asyncio.create_task(_enrich_meta(task))
    await _kick_queue()

    return {"ok": True, "id": task["id"]}


