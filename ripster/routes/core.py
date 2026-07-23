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

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse

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
    "soundcloud-oauth-token",
    "beatport-password",
    "wrapper-password", "wrapper-apple-id",
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


def _redact_config(cfg: dict) -> dict:
    out = {}
    for k, v in cfg.items():
        if k in _SECRET_KEYS and v:
            out[k] = f"••••••••  ({len(str(v))} chars)"
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
    "spotify-client-id",
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
    whitelist-filters what it accepts — no separate import endpoint needed."""
    from fastapi.responses import JSONResponse
    import time as _time
    payload = {
        "_ripster_export": True,
        "_exported_at": int(_time.time()),
        "_app_version": _app_info.get("version", ""),
        "settings": _export_config(_cfg),
    }
    return JSONResponse(
        payload,
        headers={"Content-Disposition": "attachment; filename=ripster-settings.json"},
    )


@router.post("/api/config")
async def post_config(body: dict):
    if not isinstance(body, dict):
        raise HTTPException(400, "Expected JSON object")
    safe    = {k: v for k, v in body.items() if _config_key_allowed(k)}
    blocked = [k for k in body if k not in safe]
    for k in list(safe):
        if k in _SECRET_KEYS and isinstance(safe[k], str) and safe[k].startswith("••"):
            del safe[k]
    if blocked:
        print(f"[config] blocked non-whitelisted keys: {blocked}", flush=True)
    _cfg.update(safe)
    if _save_cfg:
        _save_cfg(_cfg)
    return {"ok": True, "blocked": blocked}


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
        return {"ok": False, "error": f"Yandex недоступен: {e}"}
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
        return {"ok": False, "error": f"Yandex недоступен: {e}"}
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
        return {"ok": False, "error": "Нет TV client_id (orpheus settings.json → modules.tidal.tv_atmos_token)"}
    try:
        async with _httpx.AsyncClient(timeout=15) as c:
            r = await c.post(_TIDAL_AUTH_BASE + "oauth2/device_authorization",
                             data={"client_id": cid, "scope": "r_usr w_usr"})
        j = r.json()
    except Exception as e:
        return {"ok": False, "error": f"Tidal недоступен: {e}"}
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
        return {"ok": False, "error": f"Tidal недоступен: {e}"}
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
        if not _save_tidal_session("TV", at, rt, exp, user_id, country):
            return {"ok": False, "error": "Авторизация прошла, но не удалось записать сессию"}
        # Also derive the MOBILE_ATMOS session from the same refresh_token so the
        # one TV login unlocks real AC-4 Atmos too (best-effort, never blocks login).
        atmos_ok = await _derive_tidal_mobile_atmos(rt, user_id, country)
        return {"ok": True, "saved": True, "country": country,
                "atmos": bool(atmos_ok), "preview": at[:6] + "…"}
    err = j.get("error")
    if err in ("authorization_pending", "slow_down") or (r.status_code == 400 and not err):
        return {"ok": True, "pending": True}
    return {"ok": False, "error": j.get("error_description") or err or "token exchange failed"}


# ── Spotify out-of-box login via librespot PKCE OAuth ──────────────────────────
# Spotify OGG needs a durable librespot blob (reusable_credentials.json) — the
# keeper mints Bearers from it and orpheus streams audio with it. A fresh GitHub
# clone has none. This runs librespot's browser OAuth flow (no desktop app, no
# extension) which saves exactly that blob, so Spotify works out of the box.
# The helper runs as a subprocess (librespot's global protobuf flag + a blocking
# 127.0.0.1:5588 callback server must stay out of the app process).
_SP_OAUTH: dict = {"proc": None}


def _sp_oauth_paths():
    base = Path(__file__).resolve().parents[2]
    cache = base / "orpheus" / "config" / ".librespot_cache"
    return {
        "base": base, "cache": cache,
        "blob": cache / "reusable_credentials.json",
        "bak":  cache / "reusable_credentials.json.bak",
        "url":  cache / ".sp_oauth_url.txt",
        "done": cache / ".sp_oauth_done.txt",
        "err":  cache / ".sp_oauth_err.txt",
        "helper": base / "tools" / "spotify_oauth_login.py",
    }


@router.post("/api/spotify/auth/start")
async def spotify_auth_start(body: dict = None):
    import os as _os, sys as _sys, subprocess, asyncio as _aio
    P = _sp_oauth_paths()
    if not P["helper"].exists():
        return {"ok": False, "error": "tools/spotify_oauth_login.py отсутствует"}
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
    flags = subprocess.CREATE_NO_WINDOW if _os.name == "nt" else 0
    try:
        proc = subprocess.Popen([_sys.executable, str(P["helper"])],
                                cwd=str(P["base"]), env=env, creationflags=flags)
    except Exception as e:
        return {"ok": False, "error": f"не удалось запустить вход: {e}"}
    _SP_OAUTH["proc"] = proc
    # Wait for librespot to emit the auth URL (it writes it before blocking).
    for _ in range(50):   # ~25 s
        if P["url"].exists():
            try:
                url = P["url"].read_text(encoding="utf-8").strip()
            except Exception:
                url = ""
            if url:
                return {"ok": True, "auth_url": url}
        if proc.poll() is not None:
            break
        await _aio.sleep(0.5)
    msg = ""
    if P["err"].exists():
        try:
            msg = P["err"].read_text(encoding="utf-8").strip()
        except Exception:
            pass
    return {"ok": False, "error": msg or "нет auth URL — проверь, свободен ли порт 5588 и установлен ли librespot"}


@router.post("/api/spotify/auth/status")
async def spotify_auth_status():
    P = _sp_oauth_paths()
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
        return {"ok": True, "done": True}
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
        return {"ok": False, "error": msg or "вход не удался"}
    return {"ok": True, "pending": True}


@router.get("/api/qualities")
async def get_qualities_ep(service: str = ""):
    if service and service != "apple":
        try:
            return _get_engine(service).qualities()
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
