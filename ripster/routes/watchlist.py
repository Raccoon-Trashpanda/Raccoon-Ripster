"""
Watchlist routes — CRUD + background new-release checker.

Install: watchlist.install(app, ctx)
"""
from __future__ import annotations

import asyncio
from datetime import datetime

import httpx
from fastapi import APIRouter, HTTPException

router = APIRouter()
_s: dict = {}  # items, save, broadcast, config, queue, queue_snapshot, detect_service


def install(app, ctx) -> None:
    _s.update({
        "items":          ctx.watchlist,
        "save":           ctx.save_watchlist,
        "broadcast":      ctx.broadcast,
        "config":         ctx.config,
        "queue":          ctx.queue,
        "queue_snapshot": ctx.queue_snapshot,
        "detect_service": ctx.detect_service,
    })
    app.include_router(router)


@router.get("/api/watchlist")
async def api_watchlist_get():
    return {"items": _s["items"]}


@router.post("/api/watchlist")
async def api_watchlist_add(body: dict):
    name      = body.get("name", "").strip()
    url       = body.get("url", "").strip()
    service   = body.get("service", _s["detect_service"](url)) or "apple"
    artist_id = body.get("artist_id", "")
    if not name and not url:
        raise HTTPException(400, "name or url required")
    entry = {
        "id":           f"wl_{int(datetime.now().timestamp()*1000)}",
        "name":         name,
        "url":          url,
        "service":      service,
        "artist_id":    artist_id,
        "quality":      body.get("quality", _s["config"].get("quality", "alac")),
        "added":        datetime.now().isoformat(timespec="seconds"),
        "last_check":   None,
        "last_release": None,
        "auto_download": body.get("auto_download", True),
    }
    _s["items"].append(entry)
    _s["save"](_s["items"])
    return {"ok": True, "item": entry}


@router.delete("/api/watchlist/{item_id}")
async def api_watchlist_delete(item_id: str):
    items = _s["items"]
    items[:] = [x for x in items if x.get("id") != item_id]
    _s["save"](items)
    return {"ok": True}


@router.post("/api/watchlist/check")
async def api_watchlist_check():
    asyncio.create_task(_check_watchlist())
    return {"ok": True, "msg": "Checking in background…"}


def _sc_permalink(entry: dict) -> str:
    """Extract a SoundCloud channel permalink from the entry's url/name field —
    accepts either a bare handle or a full soundcloud.com/<handle> link."""
    raw = (entry.get("url") or entry.get("name") or "").strip().strip("/")
    if "soundcloud.com/" in raw:
        raw = raw.split("soundcloud.com/", 1)[1]
    return raw.split("/", 1)[0].split("?", 1)[0]


async def _check_soundcloud_targets(items: list, broadcast, save, cfg, queue, snapshot) -> int:
    """SC channels: newest upload via the same lookup the SC search tab uses
    (sc_user_tracks) — SC has no RSS feed like Apple, so this hits the API
    directly instead."""
    from ripster.routes.soundcloud import sc_user_tracks

    targets = [e for e in items if e.get("service") == "soundcloud"]
    new_found = 0
    for entry in targets:
        permalink = _sc_permalink(entry)
        if not permalink:
            continue
        try:
            r = await sc_user_tracks(permalink=permalink, limit=1)
        except Exception as e:
            print(f"[watchlist] sc:{permalink}: {e}", flush=True)
            continue
        if not r.get("ok") or not r.get("results"):
            continue
        latest = r["results"][0]
        track_id = latest.get("id")
        track_url = latest.get("url", "")
        prev = entry.get("last_release")
        entry["last_check"] = datetime.now().isoformat(timespec="seconds")
        if track_id and str(track_id) != str(prev) and track_url:
            entry["last_release"] = str(track_id)
            new_found += 1
            save(items)
            await broadcast({"type": "watchlist_new_release",
                             "artist": entry.get("name") or permalink,
                             "release": latest.get("title", ""),
                             "url": track_url})
            if entry.get("auto_download"):
                task = {
                    "id":       f"wl_{int(datetime.now().timestamp()*1000)}",
                    "url":      track_url,
                    "quality":  entry.get("quality", cfg.get("quality", "alac")),
                    "status":   "queued",
                    "progress": 0,
                    "log":      [],
                    "source":   "watchlist",
                }
                queue.append(task)
                await broadcast({"type": "queue_update", "queue": snapshot()})
    return new_found


async def _check_watchlist():
    items      = _s["items"]
    broadcast  = _s["broadcast"]
    save       = _s["save"]
    cfg        = _s["config"]
    queue      = _s["queue"]
    snapshot   = _s["queue_snapshot"]

    targets = [e for e in items if e.get("service") == "apple" and e.get("artist_id")]
    sc_count = len([e for e in items if e.get("service") == "soundcloud"])
    total = len(targets) + sc_count
    if total == 0:
        return

    new_found = 0
    await broadcast({"type": "watchlist_check_start", "total": total})

    if sc_count:
        new_found += await _check_soundcloud_targets(items, broadcast, save, cfg, queue, snapshot)

    async with httpx.AsyncClient(timeout=10) as client:
        for i, entry in enumerate(targets):
            artist_id = entry["artist_id"]
            await broadcast({
                "type":    "watchlist_check_progress",
                "current": sc_count + i + 1,
                "total":   total,
                "artist":  entry.get("name", "?"),
            })
            try:
                url = f"https://itunes.apple.com/rss/artistnewreleases/id={artist_id}/limit=1/json"
                r = await client.get(url)
                if r.status_code != 200:
                    continue
                data = r.json()
                feed_items = (data.get("feed") or {}).get("entry") or []
                if not feed_items:
                    continue
                latest = feed_items[0]
                release_url  = (latest.get("id") or {}).get("label", "")
                release_name = (latest.get("im:name") or {}).get("label", "")
                prev = entry.get("last_release")
                entry["last_check"] = datetime.now().isoformat(timespec="seconds")
                if release_url and release_url != prev:
                    entry["last_release"] = release_url
                    new_found += 1
                    save(items)
                    await broadcast({"type":     "watchlist_new_release",
                                     "artist":   entry["name"],
                                     "release":  release_name,
                                     "url":      release_url})
                    if entry.get("auto_download") and release_url:
                        task = {
                            "id":       f"wl_{int(datetime.now().timestamp()*1000)}",
                            "url":      release_url,
                            "quality":  entry.get("quality", cfg.get("quality", "alac")),
                            "status":   "queued",
                            "progress": 0,
                            "log":      [],
                            "source":   "watchlist",
                        }
                        queue.append(task)
                        await broadcast({"type": "queue_update", "queue": snapshot()})
            except Exception as e:
                print(f"[watchlist] {entry['name']}: {e}", flush=True)

    await broadcast({
        "type":    "watchlist_check_done",
        "checked": total,
        "new":     new_found,
    })
