"""
Owner-only кнопка «Открыть внешний плеер» (трекер #37).

  POST /api/player-window/open  — поднять OS-окно с панелью: фокус живого,
                                  просьба к лаунчеру или standalone-процесс
                                  `python -m ripster.player_window`
  POST /api/player-window/close — закрыть standalone-окно (панель сама
                                  попросила ✕, хост-страница relay-транспорта)
  POST /api/player-window/claim — окно меняет разовый токен запуска на
                                  хозяйскую куку (см. ripster.launch_token)
  GET  /api/player-window/relay-status — сколько хостов/панелей в релее и
                                  что показывает панель (диагностика моста)

Гейт: только хозяин (кука ИЛИ bearer, как в /api/tools/emulator). Прослойка
auth не пускает сюда гостя, но проверка в роуте — второй слой: кнопка стоит
в плеере, который видят и гостиные ссылки, и случайная правка allowlist не
должна превращать «поднять окно на ПК» в общую ручку.

CSRF — общий для всех хозяйских POST (Origin-vs-Host в ripster.auth),
отдельного токена нет.

Зачем тут claim. Окно плеера — отдельный процесс WebView2, и в его профиле
хозяйской куки может не быть вовсе. До 24.09.2026 именно это молча убивало
внешний плеер: /ws отвергался (403), окно не регистрировалось панелью в
ripster.rp_relay и не видело состояние. Ранняя проверка этого не поймала,
потому что проверялась вкладками, которые куку делили. Теперь сервер выдаёт
окну разовый пропуск, а окно обменивает его на обычную сессию.

Install: player_window.install(app, ctx)
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from ripster import auth as _auth
from ripster import launch_token as _lt
from ripster import player_window as _pw

router = APIRouter()

_ctx: Any = None
_relay: Any = None            # ripster.rp_relay.RpRelay (снаружи, через set_relay)

# Публичный путь: окно приходит БЕЗ куки — за ней и идёт. Сам допуск держится
# на разовом токене + loopback-привязке, а не на анонимности пути.
CLAIM_PATH = "/api/player-window/claim"

_REAUTH_POLL_S = 2.0          # окно попросило допуск — сервер отвечает за это


def _require_owner(request: Request) -> None:
    if not _auth.is_owner_request(request):
        if _auth.is_enabled():
            raise HTTPException(403, "Player window is owner-only")


def set_relay(relay) -> None:
    """Подключить ретранслятор панели: без него relay-status честно пустой."""
    global _relay
    _relay = relay


def _loopback(request: Request) -> bool:
    host = request.client.host if request.client else ""
    return _lt.is_loopback(host)


def install(app, ctx) -> None:
    global _ctx
    _ctx = ctx
    _auth.add_public_path(CLAIM_PATH)
    app.include_router(router)


async def _player_open(request: Request) -> dict:
    """Общая часть: выдать разовый пропуск и отдать его окну."""
    base = _ctx.base_dir
    # Вкладка браузера просит НЕ звать лаунчер (body {"launcher": false}):
    # её панель всё равно не смогла бы управляться из лаунчерова окна — там
    # мост oswin замкнут на главное окно лаунчера. Окно лаунчера зовёт его.
    ask_launcher = True
    try:
        body = await request.json()
        if isinstance(body, dict) and body.get("launcher") is False:
            ask_launcher = False
    except Exception:
        pass
    # Окно грузит ТОТ ЖЕ сервер, с которого нажали кнопку: request.base_url
    # честнее конфига — владелец может сидеть через туннель на другом имени.
    token = _lt.mint()
    url = _pw.panel_url(str(request.base_url), token)
    return await asyncio.to_thread(_pw.open_external, base, url, ask_launcher, token)


@router.post("/api/player-window/open")
async def player_window_open(request: Request):
    _require_owner(request)
    return await _player_open(request)


@router.post("/api/player-window/close")
async def player_window_close(request: Request):
    _require_owner(request)
    return await asyncio.to_thread(_pw.close_external, _ctx.base_dir)


@router.post(CLAIM_PATH)
async def player_window_claim(request: Request):
    """Разовый пропуск запуска → обычная хозяйская кука.

    403 — не loopback (токен, утёкший наружу, чужой машине ничего не даёт).
    401 — токена нет, он не наш, просрочен или уже истрачен; различать их
    снаружи незачем, а подсказывать атакующему — нечем.

    Кука та же, что у /api/login (HttpOnly, SameSite=Strict, Secure при
    https): окно — полноценный клиент хозяина, отдельного сорта доступа у
    него нет и не будет. SameSite=Strict, а не Lax как у входа: окну не
    нужны переходы «с чужой страницы с кукой», а строгий вариант дешевле в
    разборе.
    """
    if not _loopback(request):
        raise HTTPException(403, "Launch tokens are loopback-only")
    try:
        body = await request.json()
    except Exception:
        body = {}
    token = body.get("token") if isinstance(body, dict) else None
    if not _lt.consume(token if isinstance(token, str) else "",
                       request.client.host if request.client else ""):
        raise HTTPException(401, "Launch token is invalid, used or expired")
    resp = JSONResponse({"ok": True, "owner": True})
    resp.set_cookie(
        _auth.SESSION_COOKIE, _auth.new_session_value(),
        max_age=_auth.SESSION_MAX_AGE,
        httponly=True,
        samesite="strict",
        secure=_auth.session_secure(request),
    )
    return resp


@router.get("/api/player-window/relay-status")
async def player_window_relay_status(request: Request):
    """Диагностика моста глазами сервера: сколько хостов и панелей в релее,
    кто из вкладок активен и что в последний раз показала панель. Нужна,
    чтобы «плеер работает» проверялось по факту регистрации, а не по тому,
    что окно открылось."""
    _require_owner(request)
    out: dict[str, Any] = {"relay": False, "hosts": 0, "panels": 0,
                           "active_host": None, "last_panel_ack": None,
                           "launch_pending": _lt.pending(),
                           "ts": int(time.time())}
    if _relay is None:
        return out
    out.update(_relay.status())
    out["relay"] = True
    return out


# ── «допуск просрочен»: окно просит новый запуск ─────────────────────────────
async def _reauth_once() -> bool:
    """Один шаг сторожа: окно подняло флаг reauth — выдать ему новый пропуск.

    Окно само себе допуск выписать не может (это решение сервера и только по
    хозяйской кнопке), поэтому ходит через файл-флаг. Отвечаем только живому
    окну: мёртвому некому читать.
    """
    paths = _pw.win_paths(_ctx.base_dir)
    flag = paths["reauth"]
    if not flag.exists():
        return False
    try:
        flag.unlink()
    except Exception:
        pass
    if _pw.running_pid(paths) is None:
        return False                            # окно мертво — гоним флаг прочь
    return _pw.write_launch(paths, _lt.mint())


async def _reauth_watch() -> None:
    while True:
        await asyncio.sleep(_REAUTH_POLL_S)
        try:
            await _reauth_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            pass


def start_reauth_watcher():
    """Запустить сторож «допуск истёк». Зовётся из lifespan app.py: со СВОИМ
    lifespan-контекстом FastAPI-шные on_startup не отрабатывают вовсе, и
    молча повесить задачу на них — значит оставить окно без ответа."""
    try:
        return asyncio.get_running_loop().create_task(_reauth_watch())
    except RuntimeError:                        # нет цикла (тест без сервера)
        return None
