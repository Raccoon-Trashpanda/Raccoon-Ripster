"""BBC Sounds metadata — populates the queue/history card (title, artist,
cover) at enqueue time, before the download even starts. Same programmes.json
endpoint the download preflight (ripster.runner._bbc_preflight) re-queries
right before the actual fetch — cheap, public, no auth."""
from __future__ import annotations

import re

import httpx

_RE_PID = re.compile(r'/sounds/play/([a-zA-Z0-9]+)')


async def fetch_meta_bbc(url: str) -> dict | None:
    m = _RE_PID.search(url or "")
    if not m:
        return None
    pid = m.group(1)
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"https://www.bbc.co.uk/programmes/{pid}.json")
            if r.status_code != 200:
                return None
            prog = (r.json() or {}).get("programme") or {}
    except Exception:
        return None

    title  = prog.get("title") or (prog.get("display_title") or {}).get("title") or pid
    artist = (((prog.get("parent") or {}).get("programme") or {}).get("title")
              or ((prog.get("ownership") or {}).get("service") or {}).get("title")
              or "BBC Radio")
    img_pid = (prog.get("image") or {}).get("pid", "")
    cover   = f"https://ichef.bbci.co.uk/images/ic/600x600/{img_pid}.jpg" if img_pid else ""
    versions = prog.get("versions") or []
    duration = int((versions[0].get("duration") if versions else 0) or 0)
    date = (prog.get("first_broadcast_date") or "")[:10]

    return {
        "id":          pid,
        "type":        "episode",
        "title":       title,
        "artist":      artist,
        "album":       artist,
        "year":        date[:4],
        "date":        date,
        "artworkUrl":  cover,
        "duration":    duration * 1000,
        "trackCount":  1,
        "totalTracks": 1,
        "service":     "bbc",
    }
