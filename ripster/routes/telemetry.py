"""
Telemetry routes.

  POST /api/telemetry/ingest          — PUBLIC: tester builds push batches of
                                        warn/error lines here. Публичная константа
                                        из сборки принимается, но под жёсткими
                                        лимитами (пустой/чужой токен — отказ).
  GET  /api/telemetry/instances       — OWNER: list reporting instances.
  GET  /api/telemetry/instance/{id}   — OWNER: stored lines for one instance.
  DELETE /api/telemetry/instance/{id} — OWNER: forget one instance.

Install: telemetry.install(app, ctx)  (call add_public_path for ingest in app.py).
"""
from __future__ import annotations

from fastapi import APIRouter, Request

from ripster import telemetry as _t

router = APIRouter()

# Soft anti-abuse: cap ingest body + rate windows.
_MAX_BODY = 256 * 1024
_MAX_REPORT = 12 * 1024 * 1024      # полный архив логов, а не строчки
_rate: dict = {}          # сырой peer -> {"win": t, "n": всего, "inst": {iid: [t, n]}}
_RATE_MAX = 30            # батчей в минуту на экземпляр
_RATE_MAX_PEER = 300      # и суммарно на сырой peer — потолок при переборе id
_RATE_WIN = 60            # seconds
_RATE_KEYS_PRUNE = 500    # после этого чистим протёкшие id из бакета

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


def _rate_ok(peer: str, iid=None) -> bool:
    """Пускаем ли запрос. Бакет — по СЫРОМУ peer: X-Forwarded-For задаёт сам
    клиент, и лимит по нему обходился вращением заголовка (тест 1743a3a).
    Внутри бакета два окна: общее на peer (iid=None; считается ДО чтения тела,
    чтобы флуд не выкачивал память) и на instance id (iid=…; уже после разбора).

    Окно на экземпляр нужно потому, что за туннелем ВСЕ тестеры приходят как
    127.0.0.1: без него один болтливый клиент выжигал общий бюджет и молча
    глушил диагностику остальных. Окно на peer держит флуд, когда ids перебирают
    (мусорные id сваливаются в один бакет «-», а не плодят свежие)."""
    import time
    now = time.time()
    rec = _rate.get(peer)
    if not rec or now - rec["win"] > _RATE_WIN:
        rec = _rate[peer] = {"win": now, "n": 0, "inst": {}}
    if iid is None:
        rec["n"] += 1
        return rec["n"] <= _RATE_MAX_PEER
    if len(rec["inst"]) > _RATE_KEYS_PRUNE:      # перебор id не раздувает память
        rec["inst"] = {k: v for k, v in rec["inst"].items() if now - v[0] <= _RATE_WIN}
    w = rec["inst"].get(iid)
    if not w or now - w[0] > _RATE_WIN:
        w = rec["inst"][iid] = [now, 0]
    w[1] += 1
    return rec["n"] <= _RATE_MAX_PEER and w[1] <= _RATE_MAX


def _owner_ok(request: Request) -> bool:
    """Владелец по НЕПОДДЕЛЫВАЕМОЙ сессийной куке (HMAC over session-secret), а
    не по «похож на localhost»: за туннелем любой чужой запрос приходит как
    127.0.0.1 (uvicorn без proxy_headers) — разбор 18.09.2026, `/api/pair/*`.
    Нужен здесь, потому что интерфейс владельца токена не знает — у него есть
    кука, и по ней его запись идёт свободным ярусом (см. telemetry.token_tier)."""
    try:
        from ripster import auth as _auth
        return bool(_auth.verify_session_cookie(request.cookies.get("ripster-session", "")))
    except Exception:
        return False


@router.post("/api/telemetry/ingest")
async def ingest(request: Request):
    """Public ingest for tester builds. Validated + token-gated inside the store."""
    # Лимит — по СЫРОМУ пиру, не по X-Forwarded-For: XFF задаёт клиент, и, крутя
    # его, обходил лимит → заливка мегабайтных архивов = disk-fill DoS у владельца.
    # За туннелем все внешние приходят как 127.0.0.1 (общий бакет) — для телеметрии
    # (редкие батчи) это допустимо и закрывает DoS. XFF оставляем ТОЛЬКО как
    # отображаемую атрибуцию, не как границу безопасности. Security-фикс 18.09.2026.
    peer = (request.client.host if request.client else "")
    if not _rate_ok(peer):                       # до чтения тела: флуд не качаем
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
    # второй рубеж — уже по instance id (валидированному: мусорные id в один бакет)
    if not _rate_ok(peer, _t._clean_iid(payload.get("instance_id")) or "-"):
        return {"ok": False, "error": "rate"}
    ip = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip() or peer
    return _t.store_ingest(payload, client_ip=ip, owner=_owner_ok(request))


@router.post("/api/telemetry/report")
async def report_ingest(request: Request):
    """PUBLIC (token-gated): приём полного архива логов от чужой установки.

    Отдельно от /ingest, потому что тут не строчки, а zip на мегабайты — свой
    лимит и своё хранилище. Метаданные идут заголовками, тело — сам архив.
    """
    from ripster import diagnostics as _diag
    # Лимит по СЫРОМУ пиру (см. ingest выше): XFF подделывается → крутя его,
    # атакующий обходил лимит и заливал 12-МБ архивы = disk-fill DoS. Security 18.09.
    peer = (request.client.host if request.client else "")
    if not _rate_ok(peer):                       # до чтения тела: 12 МБ не качаем
        return {"ok": False, "error": "rate"}
    blob = await request.body()
    if len(blob) > _MAX_REPORT:
        return {"ok": False, "error": "too big"}
    h = request.headers
    if not _rate_ok(peer, _t._clean_iid(h.get("x-ripster-instance")) or "-"):
        return {"ok": False, "error": "rate"}
    ip = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip() or peer
    meta = {
        "token":       h.get("x-ripster-token", ""),
        "instance_id": h.get("x-ripster-instance", ""),
        "app_version": h.get("x-ripster-version", ""),
        "platform":    h.get("x-ripster-platform", ""),
        "name":        _diag.decode_hdr(h.get("x-ripster-name", "")),
        "note":        _diag.decode_hdr(h.get("x-ripster-note", "")),
    }
    return _t.store_report(meta, blob, client_ip=ip, owner=_owner_ok(request))


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


@router.post("/api/telemetry/reports/push-bot")
async def reports_push_bot(body: dict | None = None):
    """Дослать владельцу в Telegram уже накопленные отчёты.

    Нужно для тех, что пришли до появления доставки, и как ручной повтор, если
    бот в тот момент лежал. `code` — один конкретный, без него — все.
    """
    code = str((body or {}).get("code") or "").strip().upper()
    items = _t.list_reports()
    if code:
        items = [r for r in items if str(r.get("code", "")).upper() == code]
    if not items:
        return {"ok": False, "error_key": "err.nothing_to_send", "error": "нечего отправлять"}
    sent, failed = [], []
    for rec in reversed(items):            # от старых к новым — читать по порядку
        fp = _t.report_path(str(rec.get("code") or ""))
        if fp is None:
            # Ключ `info`, а не `error`: именно `info` читает тост
            # (telemetry_ui.js → r.failed[0].info). Пока здесь стоял `error`,
            # причина не доезжала вообще — человек видел «Не отправилось: —»
            # на единственном отказе, у которого причина точно известна.
            # `info_key` — контракт для СПИСКОВ: `api()` разворачивает только
            # верхнеуровневый `error_key`, до элементов он не добирается, поэтому
            # разворачиваем на месте отрисовки.
            failed.append({"code": rec.get("code"),
                           "info_key": "err.report_archive_missing",
                           "info": "архив не найден"})
            continue
        ok, why = await _t.notify_owner_bot(rec, fp)
        (sent if ok else failed).append({"code": rec.get("code"), "info": why})
    return {"ok": not failed, "sent": sent, "failed": failed}


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
