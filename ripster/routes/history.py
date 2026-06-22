"""
History routes — GET / clear / delete by ID.

Install: history.install(app, ctx)
"""
from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()
_s: dict = {}  # items, save


def install(app, ctx) -> None:
    _s["items"] = ctx.download_history
    _s["save"]  = ctx.save_history
    app.include_router(router)


@router.get("/api/history")
async def api_history(limit: int = 100, service: str = ""):
    h = _s["items"]
    if service:
        h = [x for x in h if x.get("service") == service]
    return {"items": h[:limit], "total": len(_s["items"])}


@router.delete("/api/history")
async def api_history_clear(hours: float = 0, days: float = 0):
    """Clear history. No args → wipe everything (back-compat). With hours/days →
    prune only entries OLDER than that age, keeping the recent ones, so the user
    can clear by a chosen window (hours and days add up). Entries with an
    unparseable/missing timestamp are KEPT in a windowed clear (never delete
    something we can't age)."""
    items = _s["items"]
    window = (float(hours) * 3600.0) + (float(days) * 86400.0)
    if window <= 0:
        items.clear()
        _s["save"]([])
        return {"ok": True, "removed": "all"}
    from datetime import datetime
    now = datetime.now()

    def _too_old(x) -> bool:
        ts = x.get("ts")
        if not ts:
            return False
        try:
            return (now - datetime.fromisoformat(str(ts))).total_seconds() > window
        except Exception:
            return False

    before = len(items)
    items[:] = [x for x in items if not _too_old(x)]
    _s["save"](items)
    return {"ok": True, "removed": before - len(items), "kept": len(items)}


@router.delete("/api/history/{item_id}")
async def api_history_delete(item_id: str):
    items = _s["items"]
    items[:] = [x for x in items if x.get("id") != item_id]
    _s["save"](items)
    return {"ok": True}
