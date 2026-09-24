"""
Owner-only кнопка «Открыть внешний плеер» (трекер #37).

  POST /api/player-window/open  — поднять OS-окно с панелью: фокус живого,
                                  просьба к лаунчеру или standalone-процесс
                                  `python -m ripster.player_window`
  POST /api/player-window/close — закрыть standalone-окно (панель сама
                                  попросила ✕, хост-страница relay-транспорта)

Гейт: только хозяин (кука ИЛИ bearer, как в /api/tools/emulator). Прослойка
auth не пускает сюда гостя, но проверка в роуте — второй слой: кнопка стоит
в плеере, который видят и гостиные ссылки, и случайная правка allowlist не
должна превращать «поднять окно на ПК» в общую ручку.

CSRF — общий для всех хозяйских POST (Origin-vs-Host в ripster.auth),
отдельного токена нет.

Install: player_window.install(app, ctx)
"""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from ripster import auth as _auth
from ripster import player_window as _pw

router = APIRouter()

_ctx: Any = None


def _require_owner(request: Request) -> None:
    if not _auth.is_owner_request(request):
        if _auth.is_enabled():
            raise HTTPException(403, "Player window is owner-only")


def install(app, ctx) -> None:
    global _ctx
    _ctx = ctx
    app.include_router(router)


@router.post("/api/player-window/open")
async def player_window_open(request: Request):
    _require_owner(request)
    base = _ctx.base_dir
    # Окно грузит ТОТ ЖЕ сервер, с которого нажали кнопку: request.base_url
    # честнее конфига — владелец может сидеть через туннель на другом имени.
    url = _pw.panel_url(str(request.base_url))
    res = await asyncio.to_thread(_pw.open_external, base, url)
    return res


@router.post("/api/player-window/close")
async def player_window_close(request: Request):
    _require_owner(request)
    res = await asyncio.to_thread(_pw.close_external, _ctx.base_dir)
    return res
