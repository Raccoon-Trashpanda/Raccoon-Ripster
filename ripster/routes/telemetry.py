"""
Telemetry routes.

  POST /api/telemetry/ingest          — PUBLIC (token-gated): tester builds push
                                        batches of warn/error lines here.
  GET  /api/telemetry/instances       — OWNER: list reporting instances.
  GET  /api/telemetry/instance/{id}   — OWNER: stored lines for one instance.
  DELETE /api/telemetry/instance/{id} — OWNER: forget one instance.

Install: telemetry.install(app, ctx)  (call add_public_path for ingest in app.py).
"""
from __future__ import annotations

from fastapi import APIRouter, Request

from ripster import telemetry as _t

router = APIRouter()

# Soft anti-abuse: cap ingest body + a tiny per-IP rate window.
_MAX_BODY = 256 * 1024
_rate: dict = {}          # ip -> [window_start, count]
_RATE_MAX = 30            # batches per window
_RATE_WIN = 60            # seconds


def install(app, ctx) -> None:
    app.include_router(router)


def _rate_ok(ip: str) -> bool:
    import time
    now = time.time()
    w = _rate.get(ip)
    if not w or now - w[0] > _RATE_WIN:
        _rate[ip] = [now, 1]
        return True
    w[1] += 1
    return w[1] <= _RATE_MAX


@router.post("/api/telemetry/ingest")
async def ingest(request: Request):
    """Public ingest for tester builds. Validated + token-gated inside the store."""
    ip = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip() or \
         (request.client.host if request.client else "")
    if not _rate_ok(ip):
        return {"ok": False, "error": "rate"}
    body = await request.body()
    if len(body) > _MAX_BODY:
        return {"ok": False, "error": "too big"}
    try:
        import json
        payload = json.loads(body or b"{}")
    except Exception:
        return {"ok": False, "error": "bad json"}
    if not isinstance(payload, dict):
        return {"ok": False, "error": "bad payload"}
    return _t.store_ingest(payload, client_ip=ip)


@router.get("/api/telemetry/instances")
async def instances():
    return {"instances": _t.list_instances(), "ingest_enabled": bool(_t._cfg.get("telemetry-ingest-enabled"))}


@router.get("/api/telemetry/instance/{iid}")
async def instance_lines(iid: str, limit: int = 500, level: str = ""):
    return {"instance_id": iid, "lines": _t.get_instance_lines(iid, limit=limit, level=level)}


@router.delete("/api/telemetry/instance/{iid}")
async def instance_clear(iid: str):
    return {"ok": _t.clear_instance(iid)}
