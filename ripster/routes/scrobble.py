"""Scrobble route — receives play events from the frontend player (087 п.2).

POST /api/scrobble  — owner-only, queues a listen for ListenBrainz submission.
GET  /api/scrobble/status — returns whether scrobbling is configured.
POST /api/scrobble/token  — owner-only, saves ListenBrainz token to tokens/.
"""
from __future__ import annotations

import sys

from fastapi import APIRouter, HTTPException, Request

from ripster.auth import is_owner_request

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors="replace")
    except Exception:
        pass

router = APIRouter()


@router.post("/api/scrobble")
async def scrobble_listen(req: Request):
    if not is_owner_request(req):
        raise HTTPException(403, "owner-only")
    body = await req.json()
    artist = str(body.get("artist") or "").strip()
    title = str(body.get("title") or "").strip()
    if not artist or not title:
        raise HTTPException(400, "artist and title required")
    from ripster import scrobble
    ok = scrobble.queue_listen(
        artist=artist,
        title=title,
        album=str(body.get("album") or ""),
        releasedate=str(body.get("releasedate") or ""),
        duration_ms=int(body.get("duration_ms") or 0),
        listened_at=int(body.get("listened_at") or 0),
    )
    return {"ok": ok, "queued": scrobble.queue_size()}


@router.get("/api/scrobble/status")
async def scrobble_status(req: Request):
    if not is_owner_request(req):
        raise HTTPException(403, "owner-only")
    from ripster import scrobble
    return {"configured": scrobble.is_configured(),
            "queue_size": scrobble.queue_size()}


@router.post("/api/scrobble/token")
async def scrobble_save_token(req: Request):
    """Save ListenBrainz token to tokens/listenbrainz.token. Owner-only."""
    if not is_owner_request(req):
        raise HTTPException(403, "owner-only")
    body = await req.json()
    token = str(body.get("token") or "").strip()
    from ripster import scrobble as _sc
    tp = _sc._TOKEN_PATH
    if not token:
        try:
            tp.unlink(missing_ok=True)
        except OSError:
            pass
        return {"ok": True, "configured": False}
    tp.parent.mkdir(parents=True, exist_ok=True)
    tp.write_text(token, encoding="utf-8")
    return {"ok": True, "configured": _sc.is_configured()}


def register(app) -> None:
    app.include_router(router)
