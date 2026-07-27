"""
Единый вход в сервисы: «нажал кнопку — вошёл — токен получен».

До этого по-человечески работал только Spotify. Для SoundCloud, Яндекса, Apple и
Deezer пользователь обязан был открыть браузер, нажать F12, найти нужную вкладку
DevTools и скопировать оттуда строку — то есть делать работу, которую программа
должна делать за него. Это же и главная причина, по которой люди не доводили
настройку до конца.

КАК: открываем браузер, который уже стоит у человека, в ОТДЕЛЬНОМ пустом профиле
с включённым протоколом отладки, показываем страницу входа сервиса и ждём, пока
после успешного входа появится нужная кукa (или токен в адресе). Забираем,
сохраняем в конфиг, окно закрываем.

Почему именно так, а не иначе:
  · seleniumbase/playwright/browser_cookie3 есть только в окружении разработчика
    и пользователям НЕ отгружаются — строить вход на них нельзя;
  · `websockets` входит в поставку, а CDP — это websocket, так что лишних
    зависимостей не появляется вовсе;
  · отдельный профиль, а не основной: Chrome не отдаёт протокол отладки на
    профиле по умолчанию, и чужую сессию человека мы трогать не должны;
  · пароль пользователя нигде не проходит через нас — он вводит его прямо на
    странице сервиса.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

# ── что и откуда забираем ────────────────────────────────────────────────────
# cookie   — имя куки, которая появляется ПОСЛЕ успешного входа
# url_token — забрать из адреса (OAuth implicit: токен приезжает во фрагменте)
TARGETS: dict[str, dict] = {
    "soundcloud": {
        "title":      "SoundCloud",
        "url":        "https://soundcloud.com/signin",
        "cookie":     "oauth_token",
        "domains":    ("soundcloud.com",),
        "config_key": "soundcloud-oauth-token",
        "hint":       "Войди в SoundCloud — токен подхватится сам.",
    },
    "deezer": {
        "title":      "Deezer",
        "url":        "https://www.deezer.com/login",
        "cookie":     "arl",
        "domains":    ("deezer.com",),
        "config_key": "deezer-arl",
        "hint":       "Войди в Deezer — ARL подхватится сам.",
    },
    "apple": {
        "title":      "Apple Music",
        "url":        "https://music.apple.com/login",
        "cookie":     "media-user-token",
        "domains":    ("apple.com",),
        "config_key": "media-user-token",
        "hint":       "Войди с Apple ID — media-user-token подхватится сам.",
    },
    "yandex": {
        "title":      "Яндекс Музыка",
        # Официальный OAuth Яндекса, implicit-режим: после входа токен приезжает
        # во фрагменте адреса. Фрагмент на сервер не уходит НИКОГДА (так устроен
        # HTTP), поэтому классический локальный redirect-приёмник тут бесполезен —
        # но адрес вкладки нам виден через протокол отладки, и этого достаточно.
        "url":        ("https://oauth.yandex.ru/authorize?response_type=token"
                       "&client_id=23cabbbdc6cd418abb4b39c32c41195d"),
        "url_token":  "access_token",
        "domains":    ("yandex.ru",),
        "config_key": "yandex-token",
        "hint":       "Войди в Яндекс и разреши доступ — токен подхватится сам.",
    },
}

_TIMEOUT   = 300.0        # 5 минут на вход — человеку хватает, зависнуть не даёт
_POLL_SEC  = 1.5

# service -> состояние текущей попытки
_sessions: dict[str, dict] = {}

_cfg: dict = {}
_save_cfg = None
_broadcast = None


def configure(cfg: dict, save_cfg, broadcast) -> None:
    global _cfg, _save_cfg, _broadcast
    _cfg, _save_cfg, _broadcast = cfg, save_cfg, broadcast


# ── браузер ──────────────────────────────────────────────────────────────────
def find_browser() -> str:
    """Путь к Chrome/Edge. Edge есть на любой Windows, так что запасной путь есть
    всегда — но проверяем и его, а не считаем по умолчанию."""
    env = (os.environ.get("RIPSTER_LOGIN_BROWSER") or "").strip()
    if env and Path(env).exists():
        return env
    pf   = os.environ.get("ProgramFiles", r"C:\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    local = os.environ.get("LOCALAPPDATA", "")
    candidates = [
        rf"{pf}\Google\Chrome\Application\chrome.exe",
        rf"{pf86}\Google\Chrome\Application\chrome.exe",
        rf"{local}\Google\Chrome\Application\chrome.exe",
        rf"{pf86}\Microsoft\Edge\Application\msedge.exe",
        rf"{pf}\Microsoft\Edge\Application\msedge.exe",
    ]
    for c in candidates:
        if c and Path(c).exists():
            return c
    for name in ("chrome", "msedge", "chromium", "google-chrome"):
        p = shutil.which(name)
        if p:
            return p
    return ""


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ── CDP ──────────────────────────────────────────────────────────────────────
async def _http_json(port: int, path: str) -> Optional[list | dict]:
    import httpx
    try:
        async with httpx.AsyncClient(timeout=4) as c:
            r = await c.get(f"http://127.0.0.1:{port}{path}")
            if r.status_code == 200:
                return r.json()
    except Exception:
        pass
    return None


async def _cdp_cookies(port: int) -> list:
    """Куки всех контекстов через браузерный эндпоинт протокола отладки."""
    ver = await _http_json(port, "/json/version")
    ws_url = (ver or {}).get("webSocketDebuggerUrl") if isinstance(ver, dict) else None
    if not ws_url:
        return []
    try:
        import websockets
    except Exception:
        return []
    try:
        # Chrome отклоняет подключение с чужим Origin — заголовок не шлём вовсе.
        async with websockets.connect(ws_url, max_size=32 * 1024 * 1024,
                                      open_timeout=6, close_timeout=3) as ws:
            await ws.send(json.dumps({"id": 1, "method": "Storage.getCookies"}))
            while True:
                raw = await asyncio.wait_for(ws.recv(), timeout=8)
                msg = json.loads(raw)
                if msg.get("id") == 1:
                    return (msg.get("result") or {}).get("cookies") or []
    except Exception:
        return []


async def _page_urls(port: int) -> list:
    lst = await _http_json(port, "/json/list")
    if not isinstance(lst, list):
        return []
    return [str(t.get("url") or "") for t in lst if t.get("type") == "page"]


# ── добыча ───────────────────────────────────────────────────────────────────
def _from_url(urls: list, param: str) -> str:
    """Токен из фрагмента адреса (`#access_token=…&token_type=…`)."""
    from urllib.parse import urlparse, parse_qs
    for u in urls:
        if param not in u:
            continue
        frag = urlparse(u).fragment or ""
        val = parse_qs(frag).get(param, [""])[0]
        if not val:                       # бывает и обычным параметром запроса
            val = parse_qs(urlparse(u).query).get(param, [""])[0]
        if val:
            return val
    return ""


def _from_cookies(cookies: list, name: str, domains: tuple) -> str:
    for ck in cookies:
        if ck.get("name") != name:
            continue
        dom = str(ck.get("domain") or "").lstrip(".")
        if any(dom == d or dom.endswith("." + d) for d in domains):
            val = str(ck.get("value") or "")
            if val:
                return val
    return ""


async def _watch(service: str, port: int, proc) -> None:
    """Ждать появления токена, сохранить, закрыть браузер."""
    spec = TARGETS[service]
    sess = _sessions[service]
    deadline = time.time() + _TIMEOUT
    token = ""
    try:
        while time.time() < deadline:
            if sess.get("cancelled"):
                sess["state"] = "cancelled"
                break
            if proc.poll() is not None:          # человек закрыл окно сам
                sess["state"] = "closed"
                sess["error"] = "Окно входа закрыто до завершения"
                break
            if spec.get("url_token"):
                token = _from_url(await _page_urls(port), spec["url_token"])
            else:
                token = _from_cookies(await _cdp_cookies(port),
                                      spec["cookie"], spec["domains"])
            if token:
                sess["state"] = "done"
                break
            await asyncio.sleep(_POLL_SEC)
        else:
            sess["state"] = "timeout"
            sess["error"] = "Не дождались входа за 5 минут"

        if token:
            _cfg[spec["config_key"]] = token
            if _save_cfg:
                _save_cfg()
            sess["saved_key"] = spec["config_key"]
            sess["token_len"] = len(token)
            if _broadcast:
                await _broadcast({"type": "service_authed", "service": service,
                                  "key": spec["config_key"]})
                await _broadcast({"type": "log", "level": "success", "service": service,
                                  "text": f"✓ Вход в {spec['title']} выполнен — "
                                          f"токен сохранён ({len(token)} символов)"})
    except Exception as e:                                    # noqa: BLE001
        sess["state"] = "error"
        sess["error"] = str(e)[:200]
    finally:
        _cleanup(service)


def _cleanup(service: str) -> None:
    sess = _sessions.get(service) or {}
    proc = sess.get("proc")
    if proc is not None:
        try:
            if proc.poll() is None:
                proc.terminate()
            proc.wait(timeout=8)          # пока браузер жив, файлы профиля заняты
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    prof = sess.get("profile")
    if prof:
        # Профиль одноразовый: в нём остаётся живая сессия сервиса, и оставлять
        # её на диске незачем. Chrome отпускает файлы не мгновенно, поэтому
        # повторяем — и проверяем РЕЗУЛЬТАТ, а не отсутствие исключения:
        # rmtree(ignore_errors=True) не бросает никогда, так что цикл повторов
        # без проверки существования выходил с первой попытки и профиль
        # (десятки мегабайт) оставался в темпе навсегда.
        for _ in range(8):
            shutil.rmtree(prof, ignore_errors=True)
            if not os.path.exists(prof):
                break
            time.sleep(0.4)
        if os.path.exists(prof):
            sess["profile_left"] = prof
    sess.pop("proc", None)


async def start(service: str) -> dict:
    spec = TARGETS.get(service)
    if not spec:
        return {"ok": False, "error": f"Неизвестный сервис: {service}"}

    prev = _sessions.get(service)
    if prev and prev.get("state") == "waiting":
        prev["cancelled"] = True
        await asyncio.sleep(0.2)

    browser = find_browser()
    if not browser:
        return {"ok": False, "error": "Не найден Chrome или Edge — вход открыть нечем"}

    port = _free_port()
    profile = tempfile.mkdtemp(prefix=f"ripster_login_{service}_")
    args = [
        browser,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}",
        "--no-first-run", "--no-default-browser-check",
        "--new-window",
        "--window-size=980,760",
        spec["url"],
    ]
    try:
        proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:                                    # noqa: BLE001
        shutil.rmtree(profile, ignore_errors=True)
        return {"ok": False, "error": f"Не удалось запустить браузер: {str(e)[:140]}"}

    # ждём, пока протокол отладки поднимется
    ready = False
    for _ in range(40):
        if await _http_json(port, "/json/version"):
            ready = True
            break
        await asyncio.sleep(0.25)
    if not ready:
        try:
            proc.terminate()
        except Exception:
            pass
        shutil.rmtree(profile, ignore_errors=True)
        return {"ok": False, "error": "Браузер не отдал протокол отладки"}

    _sessions[service] = {"state": "waiting", "proc": proc, "profile": profile,
                          "port": port, "started": time.time(), "cancelled": False}
    asyncio.create_task(_watch(service, port, proc))
    return {"ok": True, "state": "waiting", "hint": spec["hint"], "title": spec["title"]}


def status(service: str) -> dict:
    sess = _sessions.get(service)
    if not sess:
        return {"state": "idle"}
    out = {k: v for k, v in sess.items() if k not in ("proc", "profile")}
    if sess.get("state") == "waiting":
        out["waiting_sec"] = int(time.time() - sess.get("started", time.time()))
    return out


def cancel(service: str) -> dict:
    sess = _sessions.get(service)
    if not sess:
        return {"ok": True}
    sess["cancelled"] = True
    sess["state"] = "cancelled"
    _cleanup(service)
    return {"ok": True}


def has_token(service: str) -> bool:
    spec = TARGETS.get(service) or {}
    return bool(str(_cfg.get(spec.get("config_key", "")) or "").strip())
