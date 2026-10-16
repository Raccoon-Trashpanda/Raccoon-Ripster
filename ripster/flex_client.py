"""Клиент «Премиум Ripster» для ПК — то же, что FlexClient/ServerDirectory в мобилке.

ПК-версия человека (не владельца) ходит за ссылками на звук и за поисковыми учётками на сервер
владельца по ключу поддержки. Серверная часть — ripster/routes/flex.py; здесь только сторона клиента:
активация по ключу, каталог адресов сервера, getUrl, report, search-lease, статус.

Секреты: токен устройства лежит в `flex_client.json` рядом с приложением (не в config.yaml и не в tokens/),
в журнал и в ответы наружу не попадает; ключ поддержки нигде не сохраняется — только токен.
Сетевые вызовы — async (httpx), чтобы работать из FastAPI-обработчиков и движков.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path

import httpx

BAKED = "https://raccoon-ripster.serveousercontent.com"
DISCOVERY_URL = "https://raw.githubusercontent.com/Raccoon-Trashpanda/Raccoon-Ripster-Mobile/main/report-endpoint.json"
SUPPORTED_V = 1
_DISCOVERY_TTL_S = 6 * 3600
_MAX_URL = 200
_ORIGIN = re.compile(r"^https?://[A-Za-z0-9][A-Za-z0-9.-]*(:[0-9]{2,5})?$")

_lock = threading.RLock()
_path: Path | None = None
_state: dict = {}


class FlexClientError(Exception):
    """code: invalid_key | unreachable | no_token | busy | flex_disabled | upstream | ..."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(code)
        self.code = code
        self.detail = detail


def init(base_dir: Path) -> None:
    global _path, _state
    with _lock:
        _path = Path(base_dir) / "flex_client.json"
        try:
            _state = json.loads(_path.read_text("utf-8")) if _path.exists() else {}
        except Exception:
            _state = {}
        if not isinstance(_state, dict):
            _state = {}


def _save() -> None:
    if _path is None:
        return
    tmp = _path.with_suffix(".tmp")
    tmp.write_text(json.dumps(_state, ensure_ascii=False), "utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, _path)


def is_active() -> bool:
    with _lock:
        return bool(_state.get("token"))


def _token() -> str:
    with _lock:
        return str(_state.get("token") or "")


def revoke() -> None:
    """Отвязать этот ПК (токен забываем; на сервере устройство отзывает владелец)."""
    with _lock:
        for k in ("token", "device_id", "last_good"):
            _state.pop(k, None)
        _save()


# ── адреса сервера ───────────────────────────────────────────────────────────

def sane_manual(raw: str | None) -> str:
    """Ручной адрес → базовый URL или ''. Без схемы — https; без логина, пробелов и путей."""
    u = (raw or "").strip().rstrip("/")
    if not u or len(u) > _MAX_URL:
        return ""
    if u.lower().startswith("http://"):
        u = "http://" + u[7:]
    elif u.lower().startswith("https://"):
        u = "https://" + u[8:]
    else:
        u = "https://" + u
    if "@" in u or " " in u or ".." in u:
        return ""
    return u if _ORIGIN.match(u) else ""


def parse_servers(text: str | None) -> list[str]:
    """Тело discovery-файла → адреса (`servers`, затем `ingest`). Мусор → пусто."""
    try:
        root = json.loads(text or "")
    except Exception:
        return []
    if not isinstance(root, dict) or root.get("v") != SUPPORTED_V:
        return []
    items = list(root.get("servers") or []) + [root.get("ingest")]
    out: list[str] = []
    for it in items:
        u = str(it or "").strip().rstrip("/")
        if (u.startswith("https://") and len(u) <= _MAX_URL and "@" not in u and " " not in u
                and ".." not in u and not u.startswith("https://.") and _ORIGIN.match(u)):
            out.append(u)
    return out


def candidates(manual: str = "") -> list[str]:
    """Что пробовать и в каком порядке: ручной, последний рабочий, из discovery, вшитый."""
    with _lock:
        order = [sane_manual(manual), str(_state.get("last_good") or ""),
                 *[str(x) for x in (_state.get("discovered") or [])], BAKED]
    seen: set[str] = set()
    out: list[str] = []
    for x in order:
        u = x.strip().rstrip("/")
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


async def refresh_discovery(force: bool = False) -> None:
    with _lock:
        fresh = time.time() - float(_state.get("discovered_at") or 0) < _DISCOVERY_TTL_S
        if fresh and not force and _state.get("discovered"):
            return
    try:
        async with httpx.AsyncClient(timeout=6) as c:
            r = await c.get(DISCOVERY_URL)
        got = parse_servers(r.text) if r.status_code == 200 else []
    except Exception:
        got = []
    if got:
        with _lock:
            _state["discovered"] = got
            _state["discovered_at"] = time.time()
            _save()


# ── вызовы сервера ───────────────────────────────────────────────────────────

def _auth() -> dict:
    t = _token()
    if not t:
        raise FlexClientError("no_token")
    return {"Authorization": f"Bearer {t}"}


def _err(resp: httpx.Response) -> FlexClientError:
    try:
        code = str(resp.json().get("error") or "")
    except Exception:
        code = ""
    return FlexClientError(code or "upstream", f"HTTP {resp.status_code}")


async def activate(key: str, name: str = "", manual: str = "") -> None:
    """Обменять ключ поддержки на токен устройства. Ключ не сохраняется."""
    await refresh_discovery()
    payload = {"key": re.sub(r"\s+", "", key or "").upper()[:40], "name": (name or "")[:60]}
    tried: list[str] = []
    async with httpx.AsyncClient(timeout=20) as c:
        for base in candidates(manual):
            try:
                r = await c.post(f"{base}/api/flex/activate", json=payload)
            except Exception as e:  # нет сети/DNS/TLS — пробуем следующий адрес
                tried.append(f"{base.split('//')[-1]}: {type(e).__name__}")
                continue
            try:
                body = r.json()
            except Exception:
                body = None
            if r.status_code == 200 and isinstance(body, dict) and body.get("token") and body.get("device_id"):
                with _lock:
                    _state.update(token=str(body["token"]), device_id=str(body["device_id"]), last_good=base)
                    _save()
                return
            if isinstance(body, dict) and body.get("error"):
                raise _err(r)           # ответил наш сервер — слово окончательное
            tried.append(f"{base.split('//')[-1]}: HTTP {r.status_code}")
    raise FlexClientError("unreachable", "; ".join(tried)[:300])


async def _call(method: str, path: str, *, json_body: dict | None = None, params: dict | None = None,
                timeout: float = 30) -> dict:
    headers = _auth()
    last: Exception | None = None
    async with httpx.AsyncClient(timeout=timeout) as c:
        for base in candidates():
            try:
                r = await c.request(method, f"{base}{path}", headers=headers, json=json_body, params=params)
            except Exception as e:
                last = e
                continue
            if r.status_code == 200:
                with _lock:
                    if _state.get("last_good") != base:
                        _state["last_good"] = base
                        _save()
                return r.json()
            try:
                is_ours = bool(r.json().get("error"))
            except Exception:
                is_ours = False
            if is_ours:
                raise _err(r)
            last = RuntimeError(f"HTTP {r.status_code}")
    raise FlexClientError("unreachable", type(last).__name__ if last else "")


async def status() -> dict:
    return await _call("GET", "/api/flex/status", timeout=15)


async def search_lease(service: str) -> dict:
    """Поисковая учётка сервиса (qobuz|tidal) для поиска на этом ПК."""
    r = await _call("GET", "/api/flex/search-lease", params={"service": service}, timeout=15)
    return r.get("credential") or {}


async def get_url(*, service: str, track_id: str, title: str = "", artists: list | None = None,
                  album: str = "", release_date: str = "", duration_ms: int = 0, quality: str = "lossless",
                  isrc: str = "", prefer: str = "", dash: bool = True) -> dict:
    body = {
        "service": service, "trackId": track_id, "title": title[:200], "artists": list(artists or [])[:5],
        "album": album[:200], "releaseDate": release_date, "durationMs": int(duration_ms or 0),
        "quality": quality, "isrc": isrc,
        # Что умеет ПК-клиент: оба способа, расшифровку Deezer/Яндекса и сборку DASH из сегментов.
        "modes": ["proxy", "direct"], "decryptors": ["deezer_bf", "yandex_aes_ctr"],
    }
    if prefer:
        body["prefer"] = prefer
    if dash:
        body["dash"] = True
    return await _call("POST", "/api/flex/getUrl", json_body=body, timeout=45)


async def report(report_id: str | None, ok: bool, code: str = "") -> None:
    """Исход прямой загрузки — серверу для автовыбора способа. Тихо: сбой отчёта ничему не мешает."""
    if not report_id:
        return
    try:
        await _call("POST", "/api/flex/report", json_body={"reportId": report_id, "ok": ok, "code": code}, timeout=10)
    except Exception:
        pass


def resolve_url(path: str) -> str:
    """Относительный путь proxy-потока → полный URL на последнем рабочем адресе."""
    if path.startswith("http"):
        return path
    with _lock:
        base = str(_state.get("last_good") or BAKED)
    return base.rstrip("/") + path
