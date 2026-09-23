"""feature.fm: ISRC / UPC / лейбл и грядущие релизы из кабинета владельца.

Зачем вообще свой кабинет, а не публичный API. Партнёрский API
(`developers.feature.fm`, ключ `x-api-key`) выдаётся по заявке И не отдаёт
ISRC/UPC на выходе — там они только на вход, для сканирования (проверено
12.09.2026). Единственный честный путь к своим же данным — повторить те
запросы, что делает сам кабинет под сессией владельца.

Как это устроено на самом деле (разобрано вживую 12.09.2026 через CDP по
кабинету console.feature.fm, бэкенд — console-api.feature.fm):

  • Вход:    POST /login              {username, password}  → кука сессии
             (`production.connect.sid` на домене .feature.fm).
  • Резолв:  GET  /smartlink-resolver?q=<URL релиза>&service=spotify__deezer__itunes
             → список совпадений по сервисам; в каждом isrc, album_metadata.upc,
             label, треклист. Богаче всего — по ТРЕК-URL (Spotify/Apple).
  • Грядущие: GET /control-room/upcoming?artist=<id>&page&size
             GET /control-room/in-deployment
             — то, что владелец отметил как важное: релизы до даты выхода.

ПРО КУКУ, о чём предупреждал автор способа: сессия живёт на куке и протухает.
Но чинить это руками («проливать») не нужно — у нас есть логин/пароль, и модуль
сам перелогинивается, когда сервер отвечает страницей входа или 401. Поэтому
`SessionExpired` здесь — не тупик, а сигнал к авто-входу; наружу он всплывает
ТОЛЬКО если и свежий вход не помог (сменили пароль, лёг сервис), и тогда это
честная «не смог спросить», а не «релиза нет».

Секрет (логин/пароль) лежит в `tokens/featurefm_credentials.json` (или в
config: `featurefm-username` / `featurefm-password`). Кука кэшируется в
`tokens/featurefm_session.json`, чтобы не логиниться на каждый запрос.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import httpx

_BASE = Path(__file__).resolve().parent.parent
_CRED = _BASE / "tokens" / "featurefm_credentials.json"
_SESSION = _BASE / "tokens" / "featurefm_session.json"
_CACHE = _BASE / "dist" / "featurefm_cache.json"

_API = "https://console-api.feature.fm"
_HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Origin": "https://console.feature.fm",
    "Referer": "https://console.feature.fm/",
    "User-Agent": "Mozilla/5.0",
}
#: Сервисы, по которым просим резолв. Двойное подчёркивание — формат кабинета.
_SERVICES = "spotify__deezer__itunes"

#: Сколько держим разобранный ответ. Идентификаторы релиза не меняются, но
#: карточку кабинет может дополнить — поэтому неделя, а не вечность.
_TTL = 7 * 24 * 3600.0


class NotConfigured(RuntimeError):
    """Нет логина/пароля feature.fm — спросить не у кого."""


class SessionExpired(RuntimeError):
    """Не смогли получить рабочую сессию даже после входа (пароль/сервис)."""


# ── учётка и сессия ─────────────────────────────────────────────────────────

def _load_credentials(cfg: dict | None = None) -> dict:
    """Логин/пароль: сперва tokens-файл, затем config. Пусто — NotConfigured."""
    if _CRED.is_file():
        try:
            d = json.loads(_CRED.read_text(encoding="utf-8"))
            if d.get("username") and d.get("password"):
                return {"username": d["username"], "password": d["password"]}
        except Exception:  # noqa: BLE001
            pass
    if cfg:
        u = cfg.get("featurefm-username") or cfg.get("featurefm_username")
        p = cfg.get("featurefm-password") or cfg.get("featurefm_password")
        if u and p:
            return {"username": u, "password": p}
    raise NotConfigured(
        "нет учётки feature.fm: положи tokens/featurefm_credentials.json "
        '{"username": "...", "password": "..."} или ключи featurefm-username / '
        "featurefm-password в config")


def configured(cfg: dict | None = None) -> bool:
    try:
        _load_credentials(cfg)
        return True
    except NotConfigured:
        return False


def _load_session_cookies() -> dict:
    try:
        d = json.loads(_SESSION.read_text(encoding="utf-8"))
        return dict(d.get("cookies") or {})
    except Exception:  # noqa: BLE001
        return {}


def _save_session_cookies(cookies: dict) -> None:
    try:
        _SESSION.parent.mkdir(parents=True, exist_ok=True)
        _SESSION.write_text(
            json.dumps({"ts": time.time(), "cookies": cookies}, ensure_ascii=False),
            encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def _looks_like_login(status: int, text: str) -> bool:
    """Ответ — это отказ сессии, а не данные?

    Сервисы на куках редко отвечают честным 401: приезжает 200 со страницей
    входа или редирект. Принять такое за «ничего не найдено» — самый дорогой
    способ ошибиться, поэтому распознаём явно.
    """
    if status in (401, 403):
        return True
    if status in (301, 302, 303, 307, 308):
        return True
    low = text[:2000].lower()
    return ("<html" in low and "login" in low and "password" in low)


def _do_login(client: httpx.Client, cred: dict) -> None:
    """Войти и запомнить куку. Бросает SessionExpired, если вход не удался."""
    r = client.post(f"{_API}/login",
                    json={"username": cred["username"], "password": cred["password"]})
    if r.status_code != 200 or not any(
            ck.name == "production.connect.sid" for ck in client.cookies.jar):
        raise SessionExpired(
            f"вход feature.fm не удался (HTTP {r.status_code}) — проверь "
            "логин/пароль в tokens/featurefm_credentials.json")
    _save_session_cookies({ck.name: ck.value for ck in client.cookies.jar})


def _client(cred: dict) -> httpx.Client:
    c = httpx.Client(timeout=40, headers=_HEADERS, follow_redirects=False)
    for k, v in _load_session_cookies().items():
        c.cookies.set(k, v, domain=".feature.fm")
    return c


def _get(path: str, params: dict | None, cred: dict) -> httpx.Response:
    """GET с сессией и одним авто-перелогином при протухшей куке."""
    with _client(cred) as c:
        # если куки нет вовсе — сразу логинимся, не тратя пустой запрос
        if not any(ck.name == "production.connect.sid" for ck in c.cookies.jar):
            _do_login(c, cred)
        r = c.get(f"{_API}{path}", params=params)
        if _looks_like_login(r.status_code, r.text):
            _do_login(c, cred)  # «проливаем» куку сами
            r = c.get(f"{_API}{path}", params=params)
            if _looks_like_login(r.status_code, r.text):
                raise SessionExpired(
                    "feature.fm не пускает даже после свежего входа")
        return r


# ── кэш ──────────────────────────────────────────────────────────────────────

def _cache_read(key: str) -> dict | None:
    # Повреждённый кэш — промах, а не авария: диск чинится сам (следующая
    # запись перезапишет файл), а 500 в роуте за этот мусор заплатил бы юзер.
    try:
        d = json.loads(_CACHE.read_text(encoding="utf-8"))
        ent = d.get(key)
        if not ent or (time.time() - float(ent.get("ts", 0))) > _TTL:
            return None
        return ent.get("data")
    except Exception:  # noqa: BLE001
        return None


def _cache_write(key: str, data: dict) -> None:
    try:
        _CACHE.parent.mkdir(parents=True, exist_ok=True)
        d = {}
        if _CACHE.is_file():
            d = json.loads(_CACHE.read_text(encoding="utf-8"))
        d[key] = {"ts": time.time(), "data": data}
        _CACHE.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


# ── публичное API (синхронное ядро) ──────────────────────────────────────────

def resolve(url: str, *, cfg: dict | None = None, fresh: bool = False) -> list[dict]:
    """Сырые совпадения кабинета по URL релиза (Spotify/Apple/…).

    Возвращает список матчей по сервисам как есть. Пусто — релиз не опознан
    (это НЕ ошибка сессии: её отделяет `_get`, поднимая SessionExpired).
    """
    url = (url or "").strip()
    if not url:
        return []
    cred = _load_credentials(cfg)
    key = f"resolve::{url}"
    if not fresh:
        hit = _cache_read(key)
        if hit is not None:
            return hit.get("matches", [])
    r = _get("/smartlink-resolver", {"q": url, "service": _SERVICES}, cred)
    if r.status_code == 404:
        _cache_write(key, {"matches": []})
        return []
    try:
        data = r.json()
    except Exception:  # noqa: BLE001
        return []
    matches = data if isinstance(data, list) else []
    _cache_write(key, {"matches": matches})
    return matches


def ids_for(url: str, *, cfg: dict | None = None, fresh: bool = False) -> dict:
    """ISRC / UPC / лейбл / кросс-сервис ссылки по URL релиза, слитые воедино.

    Разные сервисы заполняют поля по-разному (у одного есть UPC, у другого
    ISRC), поэтому собираем со ВСЕХ матчей и отдаём объединение. Ничего не
    нашлось — `found=False`, но это ответ, а не отказ.
    """
    matches = resolve(url, cfg=cfg, fresh=fresh)
    isrcs: set[str] = set()
    upcs: set[str] = set()
    labels: set[str] = set()
    title = ""
    artists: list[str] = []
    links: dict[str, str] = {}

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                lk = str(k).lower()
                if isinstance(v, (str, int)) and str(v).strip():
                    if lk == "isrc":
                        isrcs.add(str(v).strip().upper())
                    elif lk in ("upc", "ean", "barcode", "gtin"):
                        upcs.add(str(v).strip())
                    elif lk == "label":
                        labels.add(str(v).strip())
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    for m in matches:
        walk(m)
        # Кабинет отдаёт JSON как есть, и матчем может оказаться не только
        # словарь (walk это переживает). Поля этого элемента — title/artists/
        # webUrl — берём только со словаря, иначе один не-словарь роняет весь
        # ответ, хотя идентификаторы уже собраны.
        if not isinstance(m, dict):
            continue
        if not title:
            title = m.get("title") or m.get("albumName") or ""
        if not artists:
            artists = list(m.get("artists") or [])
        web = m.get("webUrl") or ""
        for host, name in (("spotify", "spotify"), ("music.apple", "apple"),
                           ("deezer", "deezer"), ("tidal", "tidal"),
                           ("music.youtube", "youtube"), ("amazon", "amazon")):
            if host in web and name not in links:
                links[name] = web

    return {
        "found": bool(matches),
        "title": title,
        "artists": artists,
        "isrc": sorted(isrcs),
        "upc": sorted(upcs),
        "label": sorted(labels),
        "links": links,
    }


def upcoming(*, artist_id: str | None = None, page: int = 1, size: int = 20,
             cfg: dict | None = None) -> dict:
    """Грядущие релизы из Control Room (пресейвы до даты выхода).

    Без `artist_id` — по всему аккаунту. Владелец 12.09.2026 отметил этот
    раздел как важный: здесь видно то, чего ещё нет в магазинах.
    """
    _load_credentials(cfg)  # ранний NotConfigured, если учётки нет
    params: dict = {"page": page, "size": size}
    if artist_id:
        params["artist"] = artist_id
    r = _get("/control-room/upcoming", params, _load_credentials(cfg))
    try:
        data = r.json()
    except Exception:  # noqa: BLE001
        data = {"upcoming": [], "total": 0}
    return data


def in_deployment(*, artist_id: str | None = None, page: int = 1, size: int = 20,
                  cfg: dict | None = None) -> dict:
    """Релизы «в развёртывании» — уже отданы в магазины, ещё не везде живут."""
    params: dict = {"page": page, "size": size}
    if artist_id:
        params["artist"] = artist_id
    r = _get("/control-room/in-deployment", params, _load_credentials(cfg))
    try:
        return r.json()
    except Exception:  # noqa: BLE001
        return {}


def whoami(*, cfg: dict | None = None) -> dict:
    """Кто мы для кабинета — заодно самый дешёвый пинг живости сессии."""
    r = _get("/me", None, _load_credentials(cfg))
    try:
        return r.json()
    except Exception:  # noqa: BLE001
        return {}


# ── async-обёртки для FastAPI-роутов ─────────────────────────────────────────
#
# Логика одна и живёт в синхронном ядре; здесь только увод блокирующего
# httpx в поток, чтобы не держать событийный цикл.

async def a_ids_for(url: str, *, cfg: dict | None = None, fresh: bool = False) -> dict:
    import asyncio
    return await asyncio.to_thread(ids_for, url, cfg=cfg, fresh=fresh)


async def a_upcoming(*, artist_id: str | None = None, page: int = 1, size: int = 20,
                     cfg: dict | None = None) -> dict:
    import asyncio
    return await asyncio.to_thread(upcoming, artist_id=artist_id, page=page,
                                   size=size, cfg=cfg)
