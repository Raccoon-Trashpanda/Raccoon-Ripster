"""
Маршруты единого входа в сервисы.

  GET    /api/login/targets           — какие сервисы поддержаны и где уже есть токен
  POST   /api/login/{service}/start   — открыть окно входа
  GET    /api/login/{service}/status  — что происходит (фронт опрашивает)
  DELETE /api/login/{service}/cancel  — отменить

Install: service_login.install(app, ctx)
"""
from __future__ import annotations

from fastapi import APIRouter, Request

from ripster import service_login as _sl

router = APIRouter()


@router.post("/api/open-url")
async def open_url(request: Request, body: dict):
    """Открыть адрес системным браузером, когда окно приложения не может.

    Окно Ripster.exe — это WebView2, а он режет `window.open()` наглухо. В коде
    было девять таких вызовов, и почти все — входы в сервисы: Apple, Spotify,
    Яндекс, device-flow. То есть из окна приложения не работал НИ ОДИН внешний
    вход, хотя из браузерного ярлыка работали все. Нашлось 28.07.2026.

    Ограничения: только с этой машины (через туннель браузер хозяина открывать
    нельзя) и только http/https — `file:`/протокольные ссылки запускают
    посторонние программы.
    """
    # Туннель приходит С LOCALHOST — cloudflared/serveo подключаются к серверу
    # петлёй, поэтому request.client.host у удалённого гостя равен 127.0.0.1, и
    # проверка ниже одна обещание докстринга не выполняет (тот же разбор, что в
    # auth.py:_client_ip). Признак туннеля — заголовки пересылки, которые ставит
    # сам туннель.
    #
    # Да, эти заголовки подделываются кем угодно — и именно поэтому здесь их
    # можно читать. В auth.py доверять XFF нельзя, потому что там он ОСЛАБЛЯЕТ
    # ограничение (даёт атакующему свежий счётчик). Здесь он только УЖЕСТОЧАЕТ:
    # подделка приводит к отказу, а не к разрешению. Правило на отказ безопасно
    # строить на недоверенных данных, правило на допуск — нет.
    fwd = [k for k in request.headers.keys() if k.lower().startswith("x-forwarded-")]
    host = (request.client.host if request.client else "") or ""
    if fwd or host not in ("127.0.0.1", "::1", "localhost"):
        return {"ok": False, "error_key": "err.browser_local_only_short", "error": "Открыть браузер можно только на этой машине"}
    url = str((body or {}).get("url") or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        return {"ok": False, "error_key": "err.only_http_urls", "error": "Разрешены только адреса http/https"}
    if len(url) > 2000:
        return {"ok": False, "error_key": "err.url_too_long", "error": "Слишком длинный адрес"}

    def _open() -> bool:
        import webbrowser
        try:
            if webbrowser.open(url):
                return True
        except Exception:
            pass
        try:
            import os
            os.startfile(url)                     # type: ignore[attr-defined]
            return True
        except Exception:
            return False

    import asyncio
    if not await asyncio.to_thread(_open):
        return {"ok": False, "error_key": "err.browser_open_failed", "error": "Не удалось открыть браузер"}
    return {"ok": True}


def install(app, ctx) -> None:
    _sl.configure(ctx.config, ctx.save_config, ctx.broadcast)
    app.include_router(router)


@router.get("/api/login/targets")
async def targets():
    return {"targets": [
        {"service": k, "title": v["title"], "hint": v["hint"],
         "config_key": v["config_key"], "has_token": _sl.has_token(k)}
        for k, v in _sl.TARGETS.items()
    ], "browser": _sl.find_browser()}


@router.post("/api/login/{service}/start")
async def start(service: str):
    return await _sl.start(service)


@router.get("/api/login/{service}/status")
async def status(service: str):
    return _sl.status(service)


@router.delete("/api/login/{service}/cancel")
async def cancel(service: str):
    return _sl.cancel(service)
