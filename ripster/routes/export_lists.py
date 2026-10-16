"""Export/import lists (087 п.3).

Owner-only routes:
  GET  /api/export/watchlist?format=json|m3u8  — export watchlist
  GET  /api/export/favorites?format=json|m3u8  — export favorite artists
  GET  /api/export/history?format=json|m3u8    — export download history
  POST /api/import/watchlist                    — import watchlist from JSON
  POST /api/import/favorites                    — import favorites from JSON

Guest gets only their own queue/history (isolation per 084).
JSON format includes version field for forward compatibility.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors="replace")
    except Exception:
        pass

router = APIRouter()

_EXPORT_VERSION = 1


def register(app) -> None:
    from ripster.auth import is_owner_request
    app.include_router(router)

    @router.get("/api/export/watchlist")
    async def export_watchlist(req: Request, format: str = "json"):
        if not is_owner_request(req):
            raise HTTPException(403, "owner-only")
        from ripster.routes.watchlist import _s as _wl
        items_fn = _wl.get("items")
        data = items_fn() if items_fn else []
        if format == "m3u8":
            return _to_m3u8(data, "watchlist")
        return {"version": _EXPORT_VERSION, "type": "watchlist",
                "exported_at": int(time.time()), "items": data}

    @router.get("/api/export/favorites")
    async def export_favorites(req: Request, format: str = "json"):
        if not is_owner_request(req):
            raise HTTPException(403, "owner-only")
        from ripster import config as _cfg_mod
        cfg = _cfg_mod.load()
        artists = cfg.get("digs-favorite-artists") or []
        genres = cfg.get("digs-favorite-genres") or []
        data = {"artists": artists, "genres": genres}
        if format == "m3u8":
            lines = ["#EXTM3U", f"#PLAYLIST:Favorites"]
            for a in artists:
                lines.append(f"#EXTINF:-1,{a}")
                lines.append(f"search://{a}")
            return StreamingResponse(iter(["\n".join(lines)]),
                                     media_type="audio/x-mpegurl",
                                     headers={"Content-Disposition":
                                              'attachment; filename="favorites.m3u8"'})
        return {"version": _EXPORT_VERSION, "type": "favorites",
                "exported_at": int(time.time()), **data}

    @router.get("/api/export/history")
    async def export_history(req: Request, format: str = "json"):
        if not is_owner_request(req):
            raise HTTPException(403, "owner-only")
        from ripster.routes.queue import _queue, _queue_snapshot
        tasks = _queue_snapshot() if _queue_snapshot else list(_queue)
        done = [t for t in tasks if t.get("status") == "done"]
        if format == "m3u8":
            return _tasks_to_m3u8(done, "history")
        return {"version": _EXPORT_VERSION, "type": "history",
                "exported_at": int(time.time()),
                "items": [{"url": t.get("url", ""),
                           "service": t.get("service", ""),
                           "title": t.get("title", ""),
                           "artist": t.get("artist", ""),
                           "quality": t.get("quality", ""),
                           "done_at": t.get("_done_time", 0)}
                          for t in done]}

    @router.get("/api/export/queue")
    async def export_queue(req: Request, format: str = "json"):
        if not is_owner_request(req):
            raise HTTPException(403, "owner-only")
        from ripster.routes.queue import _queue, _queue_snapshot
        tasks = _queue_snapshot() if _queue_snapshot else list(_queue)
        active = [t for t in tasks if t.get("status") in ("queued", "running")]
        if format == "m3u8":
            return _tasks_to_m3u8(active, "queue")
        return {"version": _EXPORT_VERSION, "type": "queue",
                "exported_at": int(time.time()),
                "items": [{"url": t.get("url", ""),
                           "service": t.get("service", ""),
                           "title": t.get("title", ""),
                           "artist": t.get("artist", "")}
                          for t in active]}

    @router.post("/api/import/watchlist")
    async def import_watchlist(req: Request):
        if not is_owner_request(req):
            raise HTTPException(403, "owner-only")
        body = await req.json()
        if body.get("version", 0) > _EXPORT_VERSION:
            raise HTTPException(400, "format version too new")
        items = body.get("items") or []
        if not isinstance(items, list):
            raise HTTPException(400, "items must be a list")
        from ripster.routes.watchlist import _s as _wl
        items_fn = _wl.get("items")
        save_fn = _wl.get("save")
        if not items_fn or not save_fn:
            raise HTTPException(500, "watchlist not initialized")
        existing = items_fn()
        existing_urls = {e.get("url") for e in existing}
        added = 0
        for item in items:
            url = item.get("url", "")
            if url and url not in existing_urls:
                existing.append(item)
                existing_urls.add(url)
                added += 1
        save_fn(existing)
        return {"ok": True, "added": added, "total": len(existing)}

    @router.post("/api/import/favorites")
    async def import_favorites(req: Request):
        if not is_owner_request(req):
            raise HTTPException(403, "owner-only")
        body = await req.json()
        if body.get("version", 0) > _EXPORT_VERSION:
            raise HTTPException(400, "format version too new")
        artists = body.get("artists") or []
        genres = body.get("genres") or []
        if not isinstance(artists, list) or not isinstance(genres, list):
            raise HTTPException(400, "artists and genres must be lists")
        from ripster import config as _cfg_mod
        cfg = _cfg_mod.load()
        old_artists = set(cfg.get("digs-favorite-artists") or [])
        old_genres = set(cfg.get("digs-favorite-genres") or [])
        new_artists = old_artists | set(artists)
        new_genres = old_genres | set(genres)
        cfg["digs-favorite-artists"] = sorted(new_artists)
        cfg["digs-favorite-genres"] = sorted(new_genres)
        _cfg_mod.save(cfg)
        return {"ok": True,
                "artists_added": len(new_artists) - len(old_artists),
                "genres_added": len(new_genres) - len(old_genres)}


def _to_m3u8(data: list, name: str) -> StreamingResponse:
    lines = ["#EXTM3U", f"#PLAYLIST:{name}"]
    for item in data:
        title = item.get("name") or item.get("title") or item.get("url", "")
        url = item.get("url", "")
        lines.append(f"#EXTINF:-1,{title}")
        lines.append(url)
    content = "\n".join(lines)
    return StreamingResponse(iter([content]),
                             media_type="audio/x-mpegurl",
                             headers={"Content-Disposition":
                                      f'attachment; filename="{name}.m3u8"'})


def _tasks_to_m3u8(tasks: list, name: str) -> StreamingResponse:
    lines = ["#EXTM3U", f"#PLAYLIST:{name}"]
    for t in tasks:
        artist = t.get("artist", "")
        title = t.get("title", "")
        label = f"{artist} - {title}" if artist else title
        url = t.get("url", "")
        lines.append(f"#EXTINF:-1,{label}")
        lines.append(url)
    content = "\n".join(lines)
    return StreamingResponse(iter([content]),
                             media_type="audio/x-mpegurl",
                             headers={"Content-Disposition":
                                      f'attachment; filename="{name}.m3u8"'})
