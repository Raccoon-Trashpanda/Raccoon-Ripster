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
_MAX_REPORT = 12 * 1024 * 1024      # полный архив логов, а не строчки
_rate: dict = {}          # ip -> [window_start, count]
_RATE_MAX = 30            # batches per window
_RATE_WIN = 60            # seconds

_ctx = None


def install(app, ctx) -> None:
    global _ctx
    _ctx = ctx
    app.include_router(router)


def _ctx_cfg() -> dict:
    return getattr(_ctx, "config", None) or {}


def _ctx_base_dir():
    from pathlib import Path
    return getattr(_ctx, "base_dir", None) or Path(".")


def _ctx_version() -> str:
    return str((getattr(_ctx, "app_info", None) or {}).get("version") or "")


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


@router.post("/api/telemetry/report")
async def report_ingest(request: Request):
    """PUBLIC (token-gated): приём полного архива логов от чужой установки.

    Отдельно от /ingest, потому что тут не строчки, а zip на мегабайты — свой
    лимит и своё хранилище. Метаданные идут заголовками, тело — сам архив.
    """
    from ripster import diagnostics as _diag
    ip = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip() or \
         (request.client.host if request.client else "")
    if not _rate_ok(ip):
        return {"ok": False, "error": "rate"}
    blob = await request.body()
    if len(blob) > _MAX_REPORT:
        return {"ok": False, "error": "too big"}
    h = request.headers
    meta = {
        "token":       h.get("x-ripster-token", ""),
        "instance_id": h.get("x-ripster-instance", ""),
        "app_version": h.get("x-ripster-version", ""),
        "platform":    h.get("x-ripster-platform", ""),
        "name":        _diag.decode_hdr(h.get("x-ripster-name", "")),
        "note":        _diag.decode_hdr(h.get("x-ripster-note", "")),
    }
    return _t.store_report(meta, blob, client_ip=ip)


@router.post("/api/diag/send-report")
async def diag_send_report(body: dict | None = None):
    """Действие ПОЛЬЗОВАТЕЛЯ: собрать свои логи и отправить разработчику.

    Всегда явное нажатие — фоновой отправки архивов нет и не должно быть.
    """
    from ripster import diagnostics as _diag
    note = str((body or {}).get("note") or "")
    return await _diag.send_report(_ctx_cfg(), _ctx_base_dir(), _ctx_version(), note)


@router.post("/api/client-log")
async def client_log(body: dict):
    """Ошибка со страницы → в общий консольный лог.

    Раньше падения фронта жили только в DevTools, которые пользователь не
    открывает, поэтому в логах их не было вовсе — а видел он именно их.
    """
    text = str((body or {}).get("text") or "")[:1000]
    if not text:
        return {"ok": False}
    where = str((body or {}).get("url") or "")[:120]
    bc = getattr(_ctx, "broadcast", None)
    if bc:
        await bc({"type": "log", "level": "error", "service": "ui",
                  "text": _t.redact(text + (f"  [{where}]" if where else ""))})
    return {"ok": True}


@router.get("/api/telemetry/reports")
async def reports_list():
    return {"reports": _t.list_reports()}


@router.get("/api/telemetry/report/{code}")
async def report_download(code: str):
    from fastapi import Response
    fp = _t.report_path(code)
    if fp is None:
        return {"ok": False, "error": "not found"}
    return Response(content=fp.read_bytes(), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{fp.name}"'})


@router.get("/api/telemetry/instances")
async def instances():
    return {"instances": _t.list_instances(), "ingest_enabled": bool(_t._cfg.get("telemetry-ingest-enabled"))}


@router.get("/api/telemetry/instance/{iid}")
async def instance_lines(iid: str, limit: int = 500, level: str = ""):
    return {"instance_id": iid, "lines": _t.get_instance_lines(iid, limit=limit, level=level)}


@router.post("/api/telemetry/instance/{iid}/label")
async def instance_label(iid: str, body: dict):
    return {"ok": _t.set_label(iid, str(body.get("label") or ""))}


@router.delete("/api/telemetry/instance/{iid}")
async def instance_clear(iid: str):
    return {"ok": _t.clear_instance(iid)}
