# -*- coding: utf-8 -*-
"""Своё реле ключей: wm.wol.moe-совместимый API наружу, наши учётки внутрь.

Владелец 24.09.2026: «организовать свои ключи». Наружу из этого файла торчит
только `/relay/…` (публичный префикс, ключевая авторизация здесь же, в роуте).
Хозяйская админка — `/api/relay/admin/…`: она под обычным сессионным замком,
публичным префиксом НЕ является и через гостевые ссылки недоступна.

Совместимость держим слово в слово с wrapper-lite: конверт
`{"code": 0, "msg": …, "data": …}`, те же имена ручек и параметров
(`/m3u8?adamId=`, `/key?adamId=&uri=`, …), чтобы AMDL/AppleMusicDecrypt и
любой lite-клиент говорил с нами без правки конфига — только адрес другой.

Правила, которые проверяют тесты (см. tests/test_relay_routes.py):

* без ключа — 401, отозванного ключа — 401 (одинаковый ответ: «нет такого»
  и «отозван» наружу звучат неразличимо);
* поверх квоты — 429 с честным Retry-After;
* ключ никогда не попадает в логи/ответы — только `hint` (первые 8 символов
  хэша-refs);
* учётных данных Apple реле не отдаёт НИКОГДА: наружи только то, что отдал
  key-сервер по этому же запросу (контент-ключи и шаблоны);
* /relay/status — только готовность и витрины, без адресов инстансов;
* POST /relay/license по умолчанию закрыт (самая дорогая ручка), открывается
  хозяйским `relay-license-allow`.

Чего здесь сознательно нет: приёма чужих учётных данных Apple. Маршрута вида
«залогинься к нам и получишь квоту», как у wm.wol.moe, не будет ни при каких
настройках — пул пополняется только хозяйским `relay-instances`.
"""
from __future__ import annotations

import base64
import binascii
import time
from collections import deque

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ripster import auth as _auth
from ripster import relay_pool, relay_store

router = APIRouter()

_cfg: dict = {}
_unauth_window: dict[str, deque] = {}      # peer ip → deque[ts] за последнюю минуту


def install(app, ctx) -> None:
    global _cfg
    _cfg = ctx.config
    # Секунду авторизации реле несёт само (Bearer rk_…), поэтому сессионный
    # замок хозяина ему не фон. Публичность — ровно префикс /relay/: админка
    # живёт в /api/relay/ и остаётся за сессией.
    _auth.add_public_prefix("/relay/")
    app.include_router(router)


# ── конверт и журнальная строка ──────────────────────────────────────────────

def _env(code: int, msg: str = "ok", data: dict | None = None,
         status: int | None = None) -> JSONResponse:
    return JSONResponse({"code": int(code), "msg": msg, "data": data or {}},
                        status_code=status if status is not None
                        else (200 if int(code) == 0 else int(code) or 500))


def _log(hint: str, endpoint: str, status: int, extra: str = "") -> None:
    """Одна строка на запрос. ПЛАЙНТЕКСТ КЛЮЧА здесь не появляется в принципе:
    только ref-подсказка, ручка, HTTP-код. Адресов upstream — тоже."""
    line = f"[relay] {hint or '-'} {endpoint} -> {status}"
    if extra:
        line += f" {extra}"
    print(line, flush=True)


# ── допуск ключа ─────────────────────────────────────────────────────────────

def _key_from_request(request: Request) -> str:
    """`Authorization: Bearer rk_…` или basic-форма `https://<key>@host`
    (браузер шлёт `Basic base64("<key>:")`) — обе, как их понимает wm.wol.moe.
    Ключ в query-параметре НЕ принимаем: URL'ы оседают в логах прокси."""
    header = (request.headers.get("authorization") or "").strip()
    low = header.lower()
    if low.startswith("bearer "):
        return header[7:].strip()
    if low.startswith("basic "):
        try:
            decoded = base64.b64decode(header[6:].strip()).decode("utf-8", "replace")
        except (binascii.Error, ValueError):
            return ""
        user = decoded.split(":", 1)[0]
        return user.strip()
    return ""


def _unauth_allows(request: Request) -> bool:
    """Перебор ключа не должен быть бесплатным: сырые (без ключа или с неверным)
    обращения — не чаще `relay-unauth-per-minute` с одного пира. За туннелем пир
    всегда 127.0.0.1, то есть потолок де-факто глобальный — и это правильно:
    XFF атакующий подделывает на каждом запросе (разбор в auth._client_ip)."""
    try:
        limit = int(_cfg.get("relay-unauth-per-minute", 30) or 0)
    except (TypeError, ValueError):
        limit = 30
    if limit <= 0:
        return True
    ip = _auth._client_ip(request)
    now = time.time()
    win = _unauth_window.setdefault(ip, deque(maxlen=max(64, limit * 2)))
    while win and now - win[0] >= 60.0:
        win.popleft()
    if len(win) >= limit:
        return False
    win.append(now)
    return True


def _gate(request: Request, endpoint: str):
    """Возвращает (запись ключа, None) либо (None, JSONResponse-отказ)."""
    if not bool(_cfg.get("relay-enabled", False)):
        return None, _env(503, "реле выключено", status=503)
    plain = _key_from_request(request)
    if not plain:
        if not _unauth_allows(request):
            _log("", endpoint, 429, "unauth-flood")
            return None, _env(429, "слишком много запросов без ключа",
                              status=429)
        _log("", endpoint, 401)
        return None, _env(401, "нужен ключ: Authorization: Bearer <ключ>",
                          status=401)
    rec = relay_store.resolve(plain)
    if rec is None:
        if not _unauth_allows(request):
            _log("", endpoint, 429, "unauth-flood")
            return None, _env(429, "слишком много запросов с неверным ключом",
                              status=429)
        _log("", endpoint, 401, "unknown-or-revoked")
        return None, _env(401, "ключа нет или он отозван", status=401)
    verdict = relay_store.check(rec, _cfg)
    if not verdict.get("ok"):
        relay_store.note_hit(rec["ref"], endpoint, ok=False)
        wait = float(verdict.get("retry_after") or 1.0)
        _log(rec["ref"][:8], endpoint, 429, f"quota={verdict.get('reason')}")
        resp = _env(429, f"квота ключа: {verdict.get('reason')}", status=429)
        resp.headers["Retry-After"] = str(max(1, int(wait + 0.999)))
        return None, resp
    return rec, None


async def _serve(endpoint: str, request: Request, params: dict,
                 attempts: int = 2) -> JSONResponse:
    rec, denial = _gate(request, endpoint)
    if denial is not None:
        return denial
    hint = str(rec.get("ref") or "")[:8]
    relay_store.acquire(rec)
    relay_store.note_hit(rec["ref"], endpoint, ok=True)
    try:
        data = await relay_pool.call(endpoint, _cfg, params, attempts=attempts)
    except relay_pool.RelayNoInstance as e:
        _log(hint, endpoint, 503 if e.reason in ("empty", "down") else 429,
             f"pool={e.reason}")
        status = 503 if e.reason in ("empty", "down") else 429
        resp = _env(status, "нет свободного key-сервера", status=status)
        if e.retry_after > 0:
            resp.headers["Retry-After"] = str(max(1, int(e.retry_after + 0.999)))
        return resp
    except relay_pool.RelayUpstreamError as e:
        status = e.http_status or 502
        if status < 400 or status > 599:
            status = 502
        _log(hint, endpoint, status, f"upstream-code={e.code}")
        return _env(e.code if e.code not in (0, None) else status,
                    "key-сервер вернул ошибку", status=status)
    except Exception:                                           # noqa: BLE000
        _log(hint, endpoint, 500)
        return _env(500, "внутренняя ошибка реле", status=500)
    finally:
        relay_store.release(rec)
    _log(hint, endpoint, 200)
    return _env(0, "ok", data)


# ── публичные ручки (wm.wol.moe-совместимые) ─────────────────────────────────

@router.get("/relay/m3u8")
async def relay_m3u8(request: Request, adamId: str = ""):
    if not adamId.strip():
        return _env(400, "нужен adamId", status=400)
    return await _serve("m3u8", request, {"adamId": adamId.strip()})


@router.get("/relay/key")
async def relay_key(request: Request, adamId: str = "", uri: str = ""):
    if not adamId.strip() or not uri.strip():
        return _env(400, "нужны adamId и uri", status=400)
    return await _serve("key", request, {"adamId": adamId.strip(),
                                         "uri": uri.strip()})


@router.get("/relay/lyrics")
async def relay_lyrics(request: Request, adamId: str = "", language: str = "en",
                       syllable: str = "1"):
    if not adamId.strip():
        return _env(400, "нужен adamId", status=400)
    return await _serve("lyrics", request, {
        "adamId": adamId.strip(), "language": (language or "en")[:8],
        "syllable": "1" if str(syllable) in ("1", "true", "yes") else "0"})


@router.get("/relay/webplayback")
async def relay_webplayback(request: Request, adamId: str = ""):
    if not adamId.strip():
        return _env(400, "нужен adamId", status=400)
    return await _serve("webplayback", request, {"adamId": adamId.strip()})


@router.post("/relay/license")
async def relay_license(request: Request):
    if not bool(_cfg.get("relay-license-allow", False)):
        # Сначала врата, потом ключ: иначе «403, потому что закрыто» и «401,
        # потому что ключа нет» различаются, и аноним картографирует настройки.
        return _env(403, "license отключён хозяином", status=403)
    try:
        body = await request.json()
    except Exception:                                           # noqa: BLE001
        body = {}
    if not isinstance(body, dict):
        body = {}
    adam_id = str(body.get("adamId") or "").strip()
    if not adam_id:
        return _env(400, "нужен adamId", status=400)
    return await _serve("license", request, {
        "adamId": adam_id,
        "challenge": str(body.get("challenge") or ""),
        "uri": str(body.get("uri") or ""),
        "drm-type": str(body.get("drmType") or body.get("drm-type") or "wv")[:16]})


@router.get("/relay/status")
async def relay_status(request: Request):
    """Наружу — только «живо ли, какие витрины, сколько инстансов».
    Ни адресов, ни логинов, ни ошибок upstream (они бывают с текстом враппера).

    Ключ не требуется (клиент должен уметь посмотреть сервер ДО настройки), но
    без ключа ответ под общий неаутентифицированный лимитер — статус-чекер,
    молотящий каждые 2 секунды, не должен стоить нам перебора чужих ключей.
    """
    if not bool(_cfg.get("relay-enabled", False)):
        return _env(503, "реле выключено", status=503)
    if not _key_from_request(request) and not _unauth_allows(request):
        return _env(429, "слишком часто", status=429)
    snap = await _status_snapshot()
    rows = snap.get("instances") or []
    return _env(0, "ok", {
        "ready": bool(snap.get("ready")),
        "regions": list(snap.get("regions") or []),
        "instances": len(rows),
        "readyInstances": sum(1 for r in rows if r.get("ready")),
    })


async def _status_snapshot() -> dict:
    import asyncio
    try:
        return await asyncio.to_thread(relay_pool.snapshot, _cfg)
    except Exception:                                           # noqa: BLE001
        return {"ready": False, "regions": [], "instances": [], "reason": "down"}


# ── хозяйская админка (под обычным сессионным замком) ────────────────────────

def _is_owner(request: Request) -> bool:
    return bool(getattr(request.state, "is_owner", False)) or \
        _auth.is_owner_request(request)


@router.get("/api/relay/admin/keys")
async def admin_keys(request: Request):
    if not _is_owner(request):
        return _env(401, "нужна хозяйская сессия", status=401)
    return JSONResponse({"code": 0, "data": {
        "enabled": bool(_cfg.get("relay-enabled", False)),
        "keys": relay_store.list_keys(_cfg),
        "usage": relay_store.usage_summary(7),
        "defaults": relay_store.defaults(_cfg),
    }})


@router.get("/api/relay/admin/pool")
async def admin_pool(request: Request):
    if not _is_owner(request):
        return _env(401, "нужна хозяйская сессия", status=401)
    import asyncio
    snap = await asyncio.to_thread(relay_pool.snapshot, _cfg, True)
    return JSONResponse({"code": 0, "data": snap})


@router.post("/api/relay/admin/issue")
async def admin_issue(request: Request):
    """Выпустить ключ из админки (бот выдаёт одобренным пользователям, здесь —
    хозяйские устройства). Плаинтекст уходит ОДИН РАЗ и больше нигде не
    появляется: в базе лежит только HMAC-ref."""
    if not _is_owner(request):
        return _env(401, "нужна хозяйская сессия", status=401)
    body = await _json_body(request)
    cap = _int_cfg("relay-max-keys", 50)
    if cap > 0 and relay_store.count_active() >= cap:
        return JSONResponse({"code": 429, "msg": f"потолок активных ключей {cap}"},
                            status_code=429)
    q = body.get("quota") if isinstance(body.get("quota"), dict) else body
    plain, ref = relay_store.issue(
        label=str(body.get("label") or "")[:80],
        owner=str(body.get("owner") or "owner")[:64],
        quota={"qps": q.get("qps"), "concurrency": q.get("concurrency"),
               "per_hour": q.get("per_hour"), "per_day": q.get("per_day")})
    _log(ref[:8], "issue", 200)
    return JSONResponse({"code": 0, "data": {"key": plain, "ref": ref,
                                             "shown_once": True}})


@router.post("/api/relay/admin/revoke")
async def admin_revoke(request: Request):
    if not _is_owner(request):
        return _env(401, "нужна хозяйская сессия", status=401)
    body = await _json_body(request)
    out = relay_store.revoke(str(body.get("ref") or body.get("label") or ""))
    return JSONResponse({"code": 0 if out.get("ok") else 404,
                         "msg": "" if out.get("ok") else "не нашлось",
                         "data": out},
                        status_code=200 if out.get("ok") else 404)


@router.post("/api/relay/admin/quota")
async def admin_quota(request: Request):
    if not _is_owner(request):
        return _env(401, "нужна хозяйская сессия", status=401)
    body = await _json_body(request)
    ref = str(body.get("ref") or "")
    out = relay_store.set_quota(ref, **{k: body.get(k) for k in
                                        ("qps", "concurrency", "per_hour",
                                         "per_day", "label") if k in body})
    return JSONResponse({"code": 0 if out.get("ok") else 404,
                         "data": out},
                        status_code=200 if out.get("ok") else 404)


async def _json_body(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:                                           # noqa: BLE001
        return {}
    return body if isinstance(body, dict) else {}


def _int_cfg(key: str, default: int) -> int:
    try:
        return int(_cfg.get(key, default) or 0)
    except (TypeError, ValueError):
        return default
