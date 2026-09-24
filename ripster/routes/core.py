"""
Core routes — config, engine, cookies, info, bearer token, meta.

  GET  /               — serve frontend SPA
  GET  /api/config
  POST /api/config
  GET  /api/qualities
  GET  /api/engine
  POST /api/engine
  POST /api/upload-cookies
  GET  /api/check-cookies
  GET  /api/info
  GET  /api/fetch-bearer
  GET  /api/meta

Install: core.install(app, ctx)
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from ripster.i18n_msg import imsg

router = APIRouter()

_cfg: dict          = {}
_save_cfg           = None
_broadcast          = None
_get_engine         = None
_get_qualities      = None
_auto_fetch_bearer  = None
_fetch_meta         = None
_load_html          = None
_app_info: dict     = {}
_base_dir: Path     = Path(".")


def install(app, ctx) -> None:
    global _cfg, _save_cfg, _broadcast
    global _get_engine, _get_qualities
    global _auto_fetch_bearer, _fetch_meta
    global _load_html, _app_info
    _cfg               = ctx.config
    _save_cfg          = ctx.save_config
    _broadcast         = ctx.broadcast
    _get_engine        = ctx.get_engine
    _get_qualities     = ctx.get_qualities
    _auto_fetch_bearer = ctx.auto_fetch_bearer
    _fetch_meta        = ctx.fetch_meta
    _load_html         = ctx.load_html
    _app_info          = ctx.app_info
    app.include_router(router)


# ── Secret redaction ──────────────────────────────────────────────────────────

_SECRET_KEYS = {
    "media-user-token", "authorization-token", "bearer",
    "deezer-arl",
    "qobuz-password", "qobuz-auth-token", "qobuz-secrets", "qobuz-secret",
    "tidal-token", "tidal-refresh",
    "spotify-client-secret", "spotify-sp-dc",
    "spotify-radar-client-secret", "spotify-radar-sp-dc",   # dedicated radar account
    "soundcloud-oauth-token",
    "beatport-password",
    "wrapper-password", "wrapper-apple-id",
    "amd-wm-api-key",              # ключ wm.wol.moe (Bearer) — секрет, гостям маскируется
    "tl1001-password",
    "yandex-token",
    "amazon-token",
    "ripster-repo-token",           # PAT for self-update from a private repo
    "spotify-push-secret",          # 32-char push secret (Spotify extension) — real secret
    "qobuz-token",                  # dead/typo key but redact for completeness (never non-empty)
    "telemetry-token",              # sole gate of the public CSRF-exempt /api/telemetry/ingest
    "spotify-proxy",                # proxy URL may embed user:pass credentials
    "spotify-totp-secret",          # web-token TOTP secret (owner-injected on rotation)
    "app-password-hash", "session-secret",
}


# Имена полей ВНУТРИ записей пулов аккаунтов. Плоская проверка по _SECRET_KEYS
# видит только верхний уровень, поэтому пароли из wrapper-accounts/deezer-accounts
# уходили в открытом виде и в /api/config (то есть гостю в init-пакете), и в
# отчёт с логами. Нашлось 28.07.2026 при проверке архива отчёта на утечки.
_NESTED_SECRET_FIELDS = ("password", "passwd", "token", "secret", "arl",
                         "cookie", "api_key", "apikey", "key")


def _looks_secret(key: str) -> bool:
    k = str(key).lower().replace("-", "_")
    return any(f in k for f in _NESTED_SECRET_FIELDS)


def _redact_deep(value):
    """Замаскировать всё, что похоже на секрет, на любой глубине."""
    if isinstance(value, dict):
        return {k: (f"••••••••  ({len(str(v))} chars)" if (v and _looks_secret(k) and not isinstance(v, (dict, list)))
                    else _redact_deep(v))
                for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_deep(v) for v in value]
    return value


def _redact_config(cfg: dict) -> dict:
    out = {}
    for k, v in cfg.items():
        if k in _SECRET_KEYS and v:
            out[k] = f"••••••••  ({len(str(v))} chars)"
        elif isinstance(v, (dict, list)):
            out[k] = _redact_deep(v)
        else:
            out[k] = v
    return out


# ── Settings export/import ──────────────────────────────────────────────────
# Export is a hard-exclude, not a redaction: the whole point is a file safe
# to hand to someone else or re-import elsewhere, so leaked keys must be
# ABSENT, not replaced with a "••••" placeholder that still hints at length.
# On top of _SECRET_KEYS (single credential fields), the multi-account pool
# lists (deezer-accounts, wrapper-accounts, ...) each embed real credentials
# per entry (arl/password/token) that _SECRET_KEYS' flat key check can't see
# into — excluded wholesale. A few account-identity fields (email/user-id/
# username) are excluded too: not technically "tokens" but PII the owner
# said not to include, and useless without the paired credential anyway.
_EXPORT_ACCOUNT_LIST_KEYS = {
    "deezer-accounts", "qobuz-accounts", "soundcloud-accounts",
    "yandex-accounts", "wrapper-accounts",
}
_EXPORT_IDENTITY_KEYS = {
    "qobuz-user-id", "qobuz-email", "qobuz-app-id",
    "tidal-user-id", "beatport-username",
    "spotify-client-id", "spotify-radar-client-id",
}
_EXPORT_EXCLUDE_KEYS = _SECRET_KEYS | _EXPORT_ACCOUNT_LIST_KEYS | _EXPORT_IDENTITY_KEYS


def _export_config(cfg: dict) -> dict:
    return {k: v for k, v in cfg.items() if k not in _EXPORT_EXCLUDE_KEYS}


from ripster.security import config_key_allowed as _config_key_allowed


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
async def root():
    # no-store: the whole UI (HTML+CSS+JS) is one inline file — without this,
    # browsers (esp. iOS Safari) serve a stale page after an update and the
    # user sees old layout/behaviour despite a reload.
    return HTMLResponse(
        _load_html() if _load_html else "",
        headers={"Cache-Control": "no-store, must-revalidate"},
    )


@router.get("/api/config")
async def get_config():
    return _redact_config(_cfg)


@router.get("/api/config/export")
async def export_config():
    """Downloadable settings backup — every preference/path/toggle EXCEPT
    credentials (tokens, passwords, ARLs, multi-account pool lists, account
    identity fields). Re-importable via POST /api/config, which already
    whitelist-filters what it accepts — no separate import endpoint needed.

    Сюда же кладётся реестр отзывов владельца («это не мой артист»): это тоже
    настройка — то, что человек уже решил о своих подписках. Теряется при
    переносе — и однофамильцы возвращаются в ленту молча.
    """
    from fastapi.responses import JSONResponse
    import time as _time
    payload = {
        "_ripster_export": True,
        "_exported_at": int(_time.time()),
        "_app_version": _app_info.get("version", ""),
        "settings": _export_config(_cfg),
    }
    try:
        from ripster import owner_feedback as _fb
        payload["owner_feedback"] = _fb.export()
    except Exception as e:                                   # noqa: BLE001
        print(f"[config] отзыв владельца не вывезен: {e}", flush=True)
    return JSONResponse(
        payload,
        headers={"Content-Disposition": "attachment; filename=ripster-settings.json"},
    )


@router.post("/api/config")
async def post_config(body: dict):
    if not isinstance(body, dict):
        raise HTTPException(400, "Expected JSON object")
    # Реестр отзывов — не настройка витрины, а отдельный durable-файл: его
    # сливает в себя `owner_feedback.apply_export`, и в `_cfg` ему делать
    # нечего (иначе whitelist снёс бы его как «blocked»).
    fb = body.pop("owner_feedback", None)
    fb_added = 0
    if isinstance(fb, dict):
        try:
            from ripster import owner_feedback as _fb
            fb_added = _fb.apply_export(fb)
            _fb.invalidate()
        except Exception as e:                               # noqa: BLE001
            print(f"[config] отзыв владельца не ввезён: {e}", flush=True)
    safe    = {k: v for k, v in body.items() if _config_key_allowed(k)}
    blocked = [k for k in body if k not in safe]
    for k in list(safe):
        if k in _SECRET_KEYS and isinstance(safe[k], str) and safe[k].startswith("••"):
            del safe[k]
    if blocked:
        print(f"[config] blocked non-whitelisted keys: {blocked}", flush=True)
    _cfg.update(safe)
    if "apple-public-mode" in safe:
        # Перевыбор режима владельцем — явное действие, оно снимает автопаузу
        # 429 (router живёт молчанием, пауза — не приговор выбору человека).
        try:
            from ripster import apple_router as _ar
            _ar.clear_public_pause()
        except Exception:                                    # noqa: BLE001
            pass
    if _save_cfg:
        _save_cfg(_cfg)
    return {"ok": True, "blocked": blocked, "feedback_added": fb_added}


@router.post("/api/config/reload")
async def post_config_reload(request: Request):
    """Перечитать config.yaml и tokens/*.yaml в память приложения.

    Только с этой же машины: маршрут ничего не принимает и ничего не отдаёт
    наружу, но перечитывание конфига — это смена учёток и путей, и делать её
    по запросу извне нельзя.

    Зачем маршрут вообще: приложение держит конфиг в памяти и сохраняет ЦЕЛИКОМ,
    а сторож здоровья правит файл из другого процесса — снимает мёртвую учётку,
    назначает основной ту, что отдаёт lossless. Без перечитывания первая же
    запись из памяти вернула бы старое, и починка выглядела бы сделанной,
    молча откатываясь (поймано 12.09.2026 на автозамене учётки Tidal).
    """
    # loopback ОДИН не доказывает «свой»: за туннелем внешний клиент тоже приходит
    # как 127.0.0.1 (uvicorn без proxy_headers) — тот же обход, что закрыли в
    # pairing 18.09.2026. Доверяем НЕПОДДЕЛЫВАЕМОЙ owner-cookie; голый loopback —
    # только когда туннеля нет (remote-enabled off). Свой сторож здоровья
    # (credential_health._notify_app_config_changed) теперь шлёт эту cookie, так
    # что автоперечитывание конфига не ломается.
    from ripster.auth import verify_session_cookie as _vsc
    host = (request.client.host if request.client else "") or ""
    _local = host in ("127.0.0.1", "::1", "localhost")
    _owner = _vsc(request.cookies.get("ripster-session", "")) or (_local and not _cfg.get("remote-enabled", False))
    if not _owner:
        # Лог, чтобы «тихий откат» (сторож не смог перечитать → починка молча
        # вернулась) был ВИДЕН, а не выглядел сделанным. См. docstring выше.
        print(f"[config] reload отклонён: не владелец (host={host}, remote={_cfg.get('remote-enabled', False)})", flush=True)
        raise HTTPException(403, imsg("err.loopback_only", "только с этой машины"))
    # Пути считаем от этого файла: корень установки известен всегда, а
    # `sys.argv[0]` зависит от того, чем запущен процесс — на этом уже
    # обожглись в пуле Tidal в тот же день.
    base = Path(__file__).resolve().parents[2]
    try:
        n = _cfg.reload(base / "config.yaml", base / "tokens")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, imsg("err.config_reload", "не удалось перечитать конфиг: {e}", e=str(e)))
    print(f"[config] перечитан с диска: {n} ключей", flush=True)
    return {"ok": True, "keys": n}


# ── Yandex Music OAuth (device flow — automated token capture) ─────────────────
# Public Yandex Music app credentials (the same ones the official client + every
# community tool use). Device flow: user enters a short code on ya.ru/device, we
# poll Yandex server-side and save the access_token to config automatically — no
# manual copy of a long token from a redirect URL.
_YM_CLIENT_ID     = "23cabbbdc6cd418abb4b39c32c41195d"
_YM_CLIENT_SECRET = "53bc75238f0c4d08a118e51fe9203300"


@router.post("/api/yandex/auth/start")
async def yandex_auth_start():
    import httpx as _httpx
    import uuid as _uuid
    dev_id = "ripster-" + _uuid.uuid4().hex[:12]
    try:
        async with _httpx.AsyncClient(timeout=15) as c:
            r = await c.post("https://oauth.yandex.ru/device/code",
                             data={"client_id": _YM_CLIENT_ID,
                                   "device_id": dev_id, "device_name": "Ripster"})
        j = r.json()
    except Exception as e:
        return {"ok": False, "error_key": "err.ya_down", "error_args": {"e": str(e)}, "error": f"Yandex недоступен: {e}"}
    if j.get("error") or not j.get("device_code"):
        return {"ok": False, "error": j.get("error_description") or j.get("error") or "device/code failed"}
    return {"ok": True, "user_code": j.get("user_code"),
            "verification_url": j.get("verification_url") or "https://ya.ru/device",
            "device_code": j["device_code"], "interval": j.get("interval", 5),
            "expires_in": j.get("expires_in", 300)}


@router.post("/api/yandex/auth/poll")
async def yandex_auth_poll(body: dict):
    import httpx as _httpx
    code = (body.get("device_code") or "").strip()
    if not code:
        return {"ok": False, "error": "device_code required"}
    try:
        async with _httpx.AsyncClient(timeout=15) as c:
            r = await c.post("https://oauth.yandex.ru/token",
                             data={"grant_type": "device_code", "code": code,
                                   "client_id": _YM_CLIENT_ID, "client_secret": _YM_CLIENT_SECRET})
        j = r.json()
    except Exception as e:
        return {"ok": False, "error_key": "err.ya_down", "error_args": {"e": str(e)}, "error": f"Yandex недоступен: {e}"}
    if j.get("access_token"):
        _cfg["yandex-token"] = j["access_token"]
        if _save_cfg:
            _save_cfg(_cfg)
        return {"ok": True, "saved": True, "preview": j["access_token"][:6] + "…"}
    err = j.get("error")
    if err in ("authorization_pending", "slow_down"):
        return {"ok": True, "pending": True}
    return {"ok": False, "error": j.get("error_description") or err or "token exchange failed"}


# ── Tidal TV device-flow login (link.tidal.com) ───────────────────────────────
# Tidal has NO password API — only the TV device-code flow. Mirror the Yandex
# flow: backend fetches a device+user code, the UI shows the code + link, then
# polls until the user authorises. On success we write a fresh TV session into
# OrpheusDL's loginstorage.bin (the same plain-dict the tidal engine reads and
# self-refreshes), so downloads + metadata pick it up with no browser-token paste.
_TIDAL_AUTH_BASE = "https://auth.tidal.com/v1/"


def _save_tidal_session(session_type, access_token, refresh_token, expires, user_id, country_code) -> bool:
    """Persist a Tidal session (TV / MOBILE_ATMOS / MOBILE_DEFAULT) into OrpheusDL's
    loginstorage.bin. Updates the existing pickle in place (plain-dict format),
    building the minimal skeleton if the file is absent. Returns True on success."""
    import pickle, os as _os
    from ripster.engines.tidal import _session_path
    from ripster.safe_pickle import safe_loads as _pickle_loads
    sess = {"access_token": access_token, "refresh_token": refresh_token,
            "expires": expires, "user_id": user_id, "country_code": country_code}
    try:
        p = _session_path()
        try:
            blob = _pickle_loads(p.read_bytes())
        except Exception:
            blob = {"advancedmode": False, "modules": {}}
        default = (blob.setdefault("modules", {}).setdefault("tidal", {})
                       .setdefault("sessions", {}).setdefault("default", {}))
        sessions = default.setdefault("custom_data", {}).setdefault("sessions", {})
        sessions[session_type] = sess
        tmp = p.with_suffix(".bin.tmp")
        tmp.write_bytes(pickle.dumps(blob))
        _os.replace(tmp, p)
        # Drop the engine's in-process token cache so the new session is used now.
        try:
            from ripster.engines import tidal as _te
            _te._AT_CACHE.update({"token": "", "exp": 0.0})
        except Exception:
            pass
        return True
    except Exception as e:
        print(f"[tidal-session] save failed ({session_type}): {e}", flush=True)
        return False


async def _derive_tidal_mobile_atmos(refresh_token, user_id, country):
    """Derive the MOBILE_ATMOS session from an existing refresh_token (Tidal refresh
    tokens work with ANY client id). This is what actually delivers AC-4 Atmos —
    so a single TV login also unlocks Atmos, no separate sign-in. Best-effort."""
    import httpx as _httpx
    from datetime import datetime, timedelta
    from ripster.engines.tidal import _mobile_atmos_client
    macid = _mobile_atmos_client()
    if not (refresh_token and macid):
        return False
    try:
        async with _httpx.AsyncClient(timeout=15) as c:
            r = await c.post(_TIDAL_AUTH_BASE + "oauth2/token",
                             data={"client_id": macid, "refresh_token": refresh_token,
                                   "grant_type": "refresh_token", "scope": "r_usr w_usr"})
        if r.status_code != 200:
            print(f"[tidal-atmos] derive failed: HTTP {r.status_code} {r.text[:120]}", flush=True)
            return False
        j = r.json()
        at = j.get("access_token")
        if not at:
            return False
        rt = j.get("refresh_token") or refresh_token
        exp = datetime.now() + timedelta(seconds=int(j.get("expires_in", 3600)))
        u = j.get("user", {}) or {}
        uid = int(u.get("userId") or user_id or 0)
        cc = u.get("countryCode") or country or ""
        return _save_tidal_session("MOBILE_ATMOS", at, rt, exp, uid, cc)
    except Exception as e:
        print(f"[tidal-atmos] derive error: {e}", flush=True)
        return False


@router.post("/api/tidal/auth/start")
async def tidal_auth_start():
    import httpx as _httpx
    from ripster.engines.tidal import _tv_client
    cid, _csec = _tv_client()
    if not cid:
        return {"ok": False, "error_key": "err.tidal_no_tv_client_id", "error": "Нет TV client_id (orpheus settings.json → modules.tidal.tv_atmos_token)"}
    try:
        async with _httpx.AsyncClient(timeout=15) as c:
            r = await c.post(_TIDAL_AUTH_BASE + "oauth2/device_authorization",
                             data={"client_id": cid, "scope": "r_usr w_usr"})
        j = r.json()
    except Exception as e:
        return {"ok": False, "error_key": "err.tidal_down", "error_args": {"e": str(e)}, "error": f"Tidal недоступен: {e}"}
    if r.status_code != 200 or not j.get("deviceCode"):
        return {"ok": False, "error": j.get("error_description") or j.get("error") or "device_authorization failed"}
    user_code = j.get("userCode") or ""
    return {"ok": True, "user_code": user_code,
            "verification_url": "https://link.tidal.com/" + user_code,
            "device_code": j["deviceCode"], "interval": j.get("interval", 2),
            "expires_in": j.get("expiresIn", 300)}


@router.post("/api/tidal/auth/poll")
async def tidal_auth_poll(body: dict):
    import httpx as _httpx
    from datetime import datetime, timedelta
    from ripster.engines.tidal import _tv_client
    code = (body.get("device_code") or "").strip()
    if not code:
        return {"ok": False, "error": "device_code required"}
    # target: "primary" (по умолчанию — прежнее поведение, основная сессия
    # перезаписывается) либо "pool" — вошедшая учётка добавляется в
    # `tidal-accounts`, основная не трогается. Второе нужно владельцу, который
    # сидит с телефона вдали от ПК и не может вклеивать refresh-токены руками.
    target = (body.get("target") or "primary").strip().lower()
    if target not in ("primary", "pool"):
        return {"ok": False, "error_key": "err.tidal_bad_target",
                "error": "target — 'primary' или 'pool'"}
    cid, csec = _tv_client()
    try:
        async with _httpx.AsyncClient(timeout=15) as c:
            r = await c.post(_TIDAL_AUTH_BASE + "oauth2/token",
                             data={"client_id": cid, "client_secret": csec,
                                   "device_code": code,
                                   "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                                   "scope": "r_usr w_usr"})
        j = r.json()
    except Exception as e:
        return {"ok": False, "error_key": "err.tidal_down", "error_args": {"e": str(e)}, "error": f"Tidal недоступен: {e}"}
    if r.status_code == 200 and j.get("access_token"):
        at = j["access_token"]
        rt = j.get("refresh_token", "")
        exp = datetime.now() + timedelta(seconds=int(j.get("expires_in", 3600)))
        # Resolve user_id + country from the session endpoint, exactly as OrpheusDL.
        user_id, country = 0, ""
        try:
            async with _httpx.AsyncClient(timeout=15) as c:
                sr = await c.get("https://api.tidal.com/v1/sessions",
                                 headers={"Authorization": f"Bearer {at}", "X-Tidal-Token": cid})
            if sr.status_code == 200:
                sj = sr.json()
                user_id = int(sj.get("userId") or 0)
                country = sj.get("countryCode") or ""
        except Exception:
            pass
        if target == "pool":
            # Основная сессия не трогается: вход во вторую учётку не имеет права
            # разлогинить первую (ни TV-сессию, ни производную Atmos-сессию).
            from ripster import tidal_accounts as _ta
            from ripster.routes.setup import tidal_pool_append
            res = tidal_pool_append(refresh=rt, country=country,
                                    label=(body.get("label") or "").strip(),
                                    user_id=user_id)
            if not res.get("ok"):
                return {"ok": False, "error": res.get("msg"), "error_key": res.get("msg_key")}
            # Та же проба, что у панели пула и у ростера: страна, тариф, живость.
            # Ответ кэшируется по этому refresh, поэтому новая строка пула
            # загорается сразу, без второго запроса.
            # Токен наружу не отдаётся никогда — ни access, ни refresh.
            info = await _ta.account_info({"refresh": rt}, fresh=True)
            return {"ok": True, "saved": True, "target": "pool",
                    "slot": res.get("slot"), "label": res.get("label") or "",
                    "country": info.get("country") or country,
                    "plan": info.get("plan") or "",
                    "quality": info.get("quality") or "",
                    "alive": info.get("alive")}
        if not _save_tidal_session("TV", at, rt, exp, user_id, country):
            return {"ok": False, "error_key": "err.session_write_failed", "error": "Авторизация прошла, но не удалось записать сессию"}
        # Also derive the MOBILE_ATMOS session from the same refresh_token so the
        # one TV login unlocks real AC-4 Atmos too (best-effort, never blocks login).
        atmos_ok = await _derive_tidal_mobile_atmos(rt, user_id, country)
        return {"ok": True, "saved": True, "country": country,
                "atmos": bool(atmos_ok), "preview": at[:6] + "…"}
    err = j.get("error")
    if err in ("authorization_pending", "slow_down") or (r.status_code == 400 and not err):
        # `slow_down` наружу отдельным флагом (RFC 8628 3.5): тот, кто опрашивает
        # ссылку, обязан по этой ответке увеличить паузу. Молча поллить чаще —
        # второй способ получить блокировку учётки «за злоупотребление».
        return {"ok": True, "pending": True, "slow_down": err == "slow_down"}
    return {"ok": False, "error": j.get("error_description") or err or "token exchange failed"}


# ── Spotify out-of-box login via librespot PKCE OAuth ──────────────────────────
# Spotify OGG needs a durable librespot blob (reusable_credentials.json) — the
# keeper mints Bearers from it and orpheus streams audio with it. A fresh GitHub
# clone has none. This runs librespot's browser OAuth flow (no desktop app, no
# extension) which saves exactly that blob, so Spotify works out of the box.
# The helper runs as a subprocess (librespot's global protobuf flag + a blocking
# 127.0.0.1:5588 callback server must stay out of the app process).
_SP_OAUTH: dict = {"proc": None, "slot": 0}


def _return_url() -> str:
    """Ripster's own address, for a login helper to bounce the browser back to.
    Honours RIPSTER_PORT so a non-default port still returns to the right app."""
    import os as _os
    port = (_os.environ.get("RIPSTER_PORT") or "7799").strip() or "7799"
    return f"http://127.0.0.1:{port}/?spotify_login=ok"


def _sp_oauth_paths(slot: int = 0):
    """Пути входа для слота. Слот 0 — основной (orpheus/config), работает ровно
    как до мультиаккаунта. Слот i>0 — коридор учётки: blob пишется в
    ``corridor_blob(i)``, основные секреты владельца не затрагиваются."""
    from ripster import spotify_pool as _sp
    base = _sp._base_dir()
    if slot and slot > 0:
        cfg = _sp.corridor_config(slot)
        blob = _sp.corridor_blob(slot)
    else:
        cfg = _sp.main_config_dir()
        blob = _sp.live_blob()
    cache = cfg / ".librespot_cache"
    return {
        "base": base, "cache": cache, "config_dir": cfg, "slot": slot,
        "blob": blob,
        "bak":  cache / "reusable_credentials.json.bak",
        "url":  cache / ".sp_oauth_url.txt",
        "done": cache / ".sp_oauth_done.txt",
        "err":  cache / ".sp_oauth_err.txt",
        "helper": base / "tools" / "spotify_oauth_login.py",
    }


def _sp_blob_login(blob) -> str:
    """Логин (username) из blob'а librespot — офлайн, без сети. Одно место
    чтения blob'ов: `spotify_pool.read_login`."""
    from ripster import spotify_pool as _sp
    return _sp.read_login(blob)


def _sp_existing_logins(exclude_slot: int = -1) -> dict:
    """{логин: слот} для всех уже подключённых учёток (основная + коридоры с
    blob'ом). Нужен, чтобы не пустить вторую копию той же учётки в пул."""
    from ripster import spotify_pool as _sp
    out: dict = {}
    if exclude_slot != 0:
        pl = _sp_blob_login(_sp.live_blob())
        if pl:
            out[pl.lower()] = 0
    accounts = _cfg.get("spotify-accounts") or []
    for k, a in enumerate(accounts):
        slot = k + 1
        if slot == exclude_slot:
            continue
        al = _sp_blob_login(_sp.corridor_blob(slot)) or (a.get("login") or "").strip()
        if al:
            out[al.lower()] = slot
    return out


@router.post("/api/spotify/auth/start")
async def spotify_auth_start(body: dict = None):
    import os as _os, sys as _sys, subprocess, asyncio as _aio
    slot = 0
    try:
        slot = int((body or {}).get("slot") or 0)
    except (TypeError, ValueError):
        slot = 0
    P = _sp_oauth_paths(slot)
    if not P["helper"].exists():
        return {"ok": False, "error_key": "err.sp_oauth_script_missing", "error": "tools/spotify_oauth_login.py отсутствует"}
    # Уточка в коридор: каталог создаётся до старта, чтобы хелперу было куда
    # писать. Слот 0 коридора не требует (основной конфиг уже есть).
    if slot > 0:
        try:
            from ripster import spotify_pool as _sp
            _sp.ensure_corridor(slot)
        except Exception as e:
            return {"ok": False, "error_key": "err.login_start_failed",
                    "error_args": {"e": str(e)}, "error": f"не удалось подготовить слот: {e}"}
    # Kill any prior helper so the 127.0.0.1:5588 callback port is free.
    prev = _SP_OAUTH.get("proc")
    if prev and prev.poll() is None:
        try:
            prev.kill()
        except Exception:
            pass
    P["cache"].mkdir(parents=True, exist_ok=True)
    # Back up an existing blob so a failed re-login can be restored (status does it).
    # COPY (not move): the live blob must keep working during the browser OAuth so an
    # ABANDONED re-login (browser closed without finishing) never logs the account
    # out. is_authenticated()/_heal_blob() also restore from .bak as a safety net.
    if P["blob"].exists():
        try:
            import shutil as _sh
            _sh.copy2(P["blob"], P["bak"])
        except Exception:
            pass
    for f in ("url", "done", "err"):
        try:
            P[f].unlink()
        except OSError:
            pass
    env = dict(_os.environ)
    env.setdefault("PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION", "python")
    # Send the browser back into Ripster after Spotify's redirect instead of
    # leaving it on the helper's dead-end page — the in-window login option has
    # no popup to close, and `?spotify_login=ok` is what makes the UI re-check
    # auth immediately (it used to look logged-out until the app was restarted).
    env["RIPSTER_RETURN_URL"] = _return_url()
    # Слот i>0: направляем хелпер в коридор через ORPHEUS_CONFIG_DIR. Слот 0
    # остаётся без этой переменной — хелпер пишет в orpheus/config как раньше.
    if slot > 0:
        env["ORPHEUS_CONFIG_DIR"] = str(P["config_dir"])
    flags = subprocess.CREATE_NO_WINDOW if _os.name == "nt" else 0
    try:
        proc = subprocess.Popen([_sys.executable, str(P["helper"])],
                                cwd=str(P["base"]), env=env, creationflags=flags)
    except Exception as e:
        return {"ok": False, "error_key": "err.login_start_failed", "error_args": {"e": str(e)}, "error": f"не удалось запустить вход: {e}"}
    _SP_OAUTH["proc"] = proc
    _SP_OAUTH["slot"] = slot
    # Wait for librespot to emit the auth URL (it writes it before blocking).
    for _ in range(50):   # ~25 s
        if P["url"].exists():
            try:
                url = P["url"].read_text(encoding="utf-8").strip()
            except Exception:
                url = ""
            if url:
                return {"ok": True, "auth_url": url, "slot": slot}
        if proc.poll() is not None:
            break
        await _aio.sleep(0.5)
    msg = ""
    if P["err"].exists():
        try:
            msg = P["err"].read_text(encoding="utf-8").strip()
        except Exception:
            pass
    return {"ok": False, "error_key": "err.sp_no_auth_url", "error": msg or "нет auth URL — проверь, свободен ли порт 5588 и установлен ли librespot"}


@router.post("/api/spotify/auth/status")
async def spotify_auth_status(body: dict = None):
    slot = 0
    try:
        slot = int((body or {}).get("slot") or 0)
    except (TypeError, ValueError):
        slot = _SP_OAUTH.get("slot", 0)
    P = _sp_oauth_paths(slot)
    # Success: a fresh blob plus the done marker.
    if P["blob"].exists() and P["done"].exists():
        try:
            P["bak"].unlink()
        except OSError:
            pass
        # Drop the engine's cached Bearer so the new session is used immediately.
        try:
            from orpheus.modules.spotify import spotify_embed_api as _se  # noqa
        except Exception:
            pass
        # Уточка в дополнительный слот: проверяем, что это НЕ второй экземпляр
        # уже подключённой учётки, и запоминаем логин для панели/повторов.
        if slot > 0:
            return _sp_finalize_slot_login(slot, P)
        return {"ok": True, "done": True, "slot": 0}
    proc = _SP_OAUTH.get("proc")
    failed = P["err"].exists() or (proc is not None and proc.poll() not in (None, 0))
    if failed:
        # Restore the backed-up blob if the new login didn't produce one.
        if P["bak"].exists() and not P["blob"].exists():
            try:
                P["bak"].replace(P["blob"])
            except Exception:
                pass
        msg = ""
        if P["err"].exists():
            try:
                msg = P["err"].read_text(encoding="utf-8").strip()
            except Exception:
                pass
        return {"ok": False, "error_key": "err.login_failed", "error": msg or "вход не удался"}
    return {"ok": True, "pending": True}


def _sp_finalize_slot_login(slot: int, P: dict) -> dict:
    """После успешного входа в слот i>0: отбраковать дубль той же учётки и
    записать логин в запись конфига. Основной blob (слот 0) здесь не трогается."""
    from ripster import spotify_pool as _sp
    login = _sp_blob_login(P["blob"])
    known = _sp_existing_logins(exclude_slot=slot)
    if login and login.lower() in known:
        # Тот же аккаунт уже подключён — убираем свежий blob, чтобы не держать
        # две записи на одну учётку, и объясняем человеку почему вход отменён.
        try:
            P["blob"].unlink()
        except OSError:
            pass
        try:
            P["done"].unlink()
        except OSError:
            pass
        return {"ok": False, "duplicate": True, "slot": slot,
                "error_key": "err.sp_account_exists",
                "error_args": {"login": login, "slot": known[login.lower()]},
                "error": f"Эта учётка ({login}) уже подключена"}
    accounts = list(_cfg.get("spotify-accounts") or [])
    idx = slot - 1
    if 0 <= idx < len(accounts) and login:
        accounts[idx] = {**accounts[idx], "login": login}
        _cfg["spotify-accounts"] = accounts
        if _save_cfg:
            try:
                _save_cfg(_cfg)
            except Exception:
                pass
    return {"ok": True, "done": True, "slot": slot, "login": login}



@router.get("/api/qualities")
async def get_qualities_ep(service: str = ""):
    if service and service != "apple":
        try:
            return _get_engine(service).qualities()
        except (KeyError, Exception):
            pass
        # A SERVICE key whose engine has a different name (jiosaavn →
        # orpheus_jiosaavn, beatport → orpheus_beatport): without this the URL
        # bar fell through to the Apple codec list for those services.
        try:
            from ripster.service_layer import engine_for_svc as _efs
            _en = _efs(service)
            if _en and _en != service:
                return _get_engine(_en).qualities()
        except (KeyError, Exception):
            pass
    eng = _cfg.get("engine", "zhaarey")
    try:
        return _get_engine(eng).qualities()
    except (KeyError, Exception):
        return _get_qualities()


@router.get("/api/engine")
async def api_engine_get():
    return {"engine": _cfg.get("engine", "zhaarey"), "qualities": _get_qualities()}


@router.post("/api/engine")
async def api_engine_set(body: dict):
    eng = body.get("engine", "zhaarey")
    if eng not in ("zhaarey", "gamdl", "amd"):
        raise HTTPException(400, "Unknown engine")
    _cfg["engine"] = eng
    if _save_cfg:
        _save_cfg(_cfg)
    qs = _get_qualities()
    if _broadcast:
        await _broadcast({"type": "engine_changed", "engine": eng, "qualities": qs})
    return {"ok": True, "engine": eng, "qualities": qs}


@router.post("/api/upload-cookies")
async def api_upload_cookies(body: dict):
    text = (body.get("content") or "").strip()
    # SECURITY: the destination is NEVER taken from the request body — that would
    # be an arbitrary file-write primitive (→ RCE). Always write to the configured
    # cookies path (or the default beside the app).
    path = (_cfg.get("gamdl-cookies-path") or "").strip() or str(Path(".") / "cookies.txt")
    if not text:
        raise HTTPException(400, "Empty content")
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(text, encoding="utf-8")
        _cfg["gamdl-cookies-path"] = path
        if _save_cfg:
            _save_cfg(_cfg)
        return {"ok": True, "path": path}
    except Exception as e:
        raise HTTPException(500, str(e))


@router.get("/api/check-cookies")
async def api_check_cookies():
    path = _cfg.get("gamdl-cookies-path") or str(Path(".") / "cookies.txt")
    p = Path(path)
    if not p.exists():
        return {"valid": False, "exists": False, "path": path, "msg": "File not found"}
    try:
        lines = [ln for ln in p.read_text(errors="replace").splitlines()
                 if ln.strip() and not ln.startswith("#")]
        has_apple = any("apple.com" in ln for ln in lines)
        has_mut   = any("media-user-token" in ln for ln in lines)
        return {
            "valid":     has_apple,
            "exists":    True,
            "path":      path,
            "size":      p.stat().st_size,
            "lines":     len(lines),
            "has_apple": has_apple,
            "has_mut":   has_mut,
            "account":   "Apple Music ✓" if has_apple else "Not detected",
            "msg":       "Valid" if has_apple else "No apple.com cookies — export from music.apple.com",
        }
    except Exception as e:
        return {"valid": False, "exists": True, "path": path, "msg": str(e)}


@router.get("/api/info")
async def get_info():
    return _app_info


@router.get("/api/fetch-bearer")
async def get_bearer():
    if not _auto_fetch_bearer:
        raise HTTPException(503, "Bearer fetch not available")
    token = await _auto_fetch_bearer()
    if not token:
        raise HTTPException(503, "Could not extract Bearer token from Apple Music JS. Try manually.")
    _cfg["authorization-token"] = token
    if _save_cfg:
        _save_cfg(_cfg)
    if _broadcast:
        await _broadcast({"type": "bearer_updated"})
    return {"ok": True, "length": len(token)}


@router.get("/api/meta")
async def get_meta(url: str):
    if not _fetch_meta:
        raise HTTPException(503, "Meta fetch not available")
    try:
        meta = await _fetch_meta(url)
    except (ValueError, RuntimeError) as e:
        # fetch_meta raises with a HUMAN reason (unsupported URL — e.g. an Apple
        # radio station ra.*, expired bearer, 404 not-found). Surface it as a clean
        # 422 so the card shows WHY instead of a generic 500 Internal Server Error.
        raise HTTPException(422, str(e))
    if not meta:
        if not _cfg.get("authorization-token", ""):
            raise HTTPException(401, "Bearer token missing. Click 'Auto-fetch' in the Tokens tab.")
        raise HTTPException(404, "Could not fetch metadata. Check URL and tokens.")
    return meta
