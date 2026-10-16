"""Режим источника ПК и «Премиум Ripster»: состояние, активация ключа поддержки, отвязка.

  GET  /api/premium/state     — {mode, configured, needs_choice, active}
  POST /api/premium/activate  — {key, mode?}: обменять ключ на токен устройства; mode = premium|both
  POST /api/premium/revoke    — забыть токен на этом ПК

Выбор режима «свои ключи» делается обычным POST /api/config {"source-mode": "own"} (ключ в белом списке).
Всё закрыто общей прослойкой auth и проверкой роли владельца (владелец ПК = человек за этим ПК).
Ключ поддержки не сохраняется и не логируется; наружу не уходит ни ключ, ни токен.
"""
from __future__ import annotations

import socket

from fastapi import Request
from fastapi.responses import JSONResponse

_cfg: dict = {}

_ERRORS = {"invalid_key": 400, "busy": 429, "flex_disabled": 403, "unreachable": 502, "no_token": 401}


def own_has(cfg) -> dict:
    from ripster.source_mode import OWN_KEYS
    return {svc: bool(str(cfg.get(key) or "").strip()) for svc, key in OWN_KEYS.items()}


def install(app, ctx) -> None:
    global _cfg
    _cfg = ctx.config
    from ripster import auth as _auth
    from pathlib import Path
    from ripster import flex_client, source_mode
    flex_client.init(Path(ctx.base_dir))
    source_mode.set_flex_active_hook(flex_client.is_active)

    def _owner(request: Request) -> bool:
        fn = getattr(_auth, "is_owner_role", None)
        if fn is not None:
            return fn(request)
        # Публичная сборка: ролей нет. Пароль не задан — за этим ПК сидит сам владелец; задан — нужна его кука/токен.
        if not _auth.is_enabled():
            return True
        return _auth.is_owner_request(request)

    @app.get("/api/premium/state")
    async def premium_state(request: Request):
        if not _owner(request):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        configured = str(_cfg.get(source_mode.CONFIG_KEY) or "").strip().lower()
        mode = source_mode.effective_mode(configured, own_has(_cfg))
        return {"mode": mode, "configured": configured if configured in source_mode.MODES else "",
                "needs_choice": mode is None, "active": flex_client.is_active()}

    @app.post("/api/premium/activate")
    async def premium_activate(request: Request):
        if not _owner(request):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        try:
            body = await request.json()
        except Exception:  # catch-all-ok
            return JSONResponse({"error": "invalid_body"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "invalid_body"}, status_code=400)
        mode = str(body.get("mode") or "premium")
        if mode not in ("premium", "both"):
            return JSONResponse({"error": "invalid_body"}, status_code=400)
        try:
            await flex_client.activate(str(body.get("key") or ""), name=socket.gethostname()[:40])
        except flex_client.FlexClientError as e:
            return JSONResponse({"error": e.code, "detail": e.detail if e.code == "unreachable" else ""},
                                status_code=_ERRORS.get(e.code, 502))
        # Режим сохраняет интерфейс обычным POST /api/config после успешной активации:
        # «Премиум» без ключа ничего бы не давал, а запись config.yaml — дело штатного пути.
        return {"ok": True, "mode": mode, "active": True}

    @app.post("/api/premium/revoke")
    async def premium_revoke(request: Request):
        if not _owner(request):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        flex_client.revoke()
        from ripster import premium_search
        premium_search.forget()
        return {"ok": True, "active": False}
