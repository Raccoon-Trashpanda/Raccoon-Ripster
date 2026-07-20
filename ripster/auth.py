"""
Application-level single-password auth.

Stateless design:
  - Password stored as PBKDF2-HMAC-SHA256 hash in ``config["app-password-hash"]``
    (format: ``"pbkdf2$<iters>$<salt_hex>$<hash_hex>"``). Plaintext is never persisted.
  - Sessions are HMAC-signed cookies: ``"<issued_unix>.<hmac_hex>"``.
    The signing key lives in ``config["session-secret"]`` and is auto-created
    on first save. 30-day lifetime.
  - If no password is set, auth is disabled entirely — the whole middleware
    becomes a no-op. This keeps the local-only use case frictionless.

Defences layered on top of the basic flow:
  - **Brute-force protection on /api/login**: per-IP sliding-window limit of
    ``MAX_ATTEMPTS`` failures within ``LOCKOUT_WINDOW`` seconds → 429 with
    ``Retry-After``. Counters live in memory, not persisted — a restart
    resets them (acceptable: it just means the attacker must wait for the
    server to come back up, not less effective than DB-backed lockout).
  - **CSRF protection on mutating endpoints**: POST/PUT/DELETE requests
    verify the ``Origin`` header (same-origin or empty/missing, which
    covers curl/native clients that don't set Origin). Combined with
    ``SameSite=Lax`` cookies this blocks the standard CSRF attack.
  - **Secure cookie auto-detection**: when the incoming request came via
    HTTPS (``X-Forwarded-Proto: https`` from a reverse proxy, or direct
    HTTPS) we flag the session cookie ``Secure``. Never downgrade.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets as _secrets
import sys
import time
from collections import defaultdict, deque
from typing import Callable

from fastapi import HTTPException, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse


SESSION_MAX_AGE = 30 * 24 * 3600   # 30 days
_PBKDF2_ITERS   = 200_000          # ~100ms on modern hardware

# Brute-force tuning: after 5 failures within 5 minutes from the same IP,
# lock that IP out for the remaining window. Successful login resets the
# counter. This is not an absolute defence (an attacker with a botnet of
# fresh IPs bypasses it) — pair with a decent password minimum length.
MAX_ATTEMPTS    = 5
LOCKOUT_WINDOW  = 300              # seconds

_LOGIN_PATH     = "/login"
_LOGIN_API      = "/api/login"
_LOGOUT_API     = "/api/logout"
_PASSWORD_API   = "/api/set-password"
_STATUS_API     = "/api/auth-status"

# Paths that never require auth. Extendable via add_public_path().
_PUBLIC_PATHS: set[str] = {_LOGIN_PATH, _LOGIN_API, _LOGOUT_API, _STATUS_API}

# Guest routes that bypass the owner-auth requirement but are not public.
# Routes starting with these prefixes are allowed for valid guest sessions.
_GUEST_PUBLIC_PREFIXES: tuple[str, ...] = ("/guest/",)

# ── Guest sandbox: deny-by-default allowlist ──────────────────────────────────
# A valid guest session may reach ONLY the paths below — everything else is
# 403. This is what keeps guests (and anyone reaching the public tunnel with a
# guest link) out of install / config-write / admin / wrapper / Spotify-OAuth /
# stats / watchlist / releases / restart, etc.
# NOTE: `/api/config` and `/api/engine` are intentionally NOT here — they are
# hard-blocked for guests by ``security.GUEST_BLOCKED_PATHS`` (enforced in
# app.py ``_guest_guard``). Guests receive engine/quality/storefront via the WS
# ``init`` payload instead, so REST config/engine stay owner-only. Keep the two
# layers consistent: do not re-add them here.
_GUEST_GET_EXACT = frozenset({
    "/", "/ws", "/api/session-info", "/api/qualities", "/api/search",
    "/api/info", "/api/meta", "/api/release/expand",
    "/api/download-file", "/api/cover/best", "/api/queue",
    "/api/soundcloud/search",
})
_GUEST_GET_PREFIX  = ("/api/artist/", "/api/album/", "/api/soundcloud/playlist/", "/api/lyrics")
_GUEST_POST_EXACT  = frozenset({
    "/api/queue/add", "/api/zip-request",
})
_GUEST_POST_PREFIX = ("/api/queue/retry/",)
# Prefixes a guest may use with any HTTP method (browse + own files + media).
_GUEST_ANY_PREFIX  = ("/api/guest/", "/api/bbc/", "/api/stream/", "/api/proxy",
                      "/api/sc_key", "/api/sc_license", "/api/sc_m3u8",
                      "/api/sc_fps_cert", "/api/sc_fps_license", "/api/sc_fps_log")


def _guest_allowed(path: str, method: str) -> bool:
    """True if a guest session may reach (path, method). Deny-by-default."""
    if any(path.startswith(p) for p in _GUEST_ANY_PREFIX):
        return True
    m = (method or "GET").upper()
    if m in ("GET", "HEAD"):
        return path in _GUEST_GET_EXACT or any(path.startswith(p) for p in _GUEST_GET_PREFIX)
    if m == "POST":
        return path in _GUEST_POST_EXACT or any(path.startswith(p) for p in _GUEST_POST_PREFIX)
    return False

# Optional hook: set by the guest system so the auth middleware can check
# guest sessions without a circular import.
# Signature: (request) -> bool
_guest_session_fn = None


def add_public_path(path: str) -> None:
    """Make a path exempt from auth. Call before the first request."""
    _PUBLIC_PATHS.add(path)


def set_guest_checker(fn) -> None:
    """Register a callable that returns True if the request carries a valid guest session."""
    global _guest_session_fn
    _guest_session_fn = fn

# Mutating HTTP methods — we enforce CSRF origin-check on these.
_CSRF_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
# Paths exempt from the Origin-based CSRF guard (secret-authenticated, meant to
# be called cross-origin — e.g. the browser-extension Spotify token push).
_CSRF_EXEMPT_PATHS = {"/api/spotify-token-push", "/api/telemetry/ingest"}

# Module-level state set by ``install()``. These are small enough that holding
# references here rather than threading them through every helper is cleaner.
_config: dict = {}
_save_config: Callable[[dict], None] = lambda cfg: None

# Per-IP login attempt log: IP → deque[timestamp]. Bounded by the lockout
# window; old entries get evicted lazily. Thread-safety is not needed because
# FastAPI serves requests in a single asyncio loop.
_login_attempts: dict[str, deque] = defaultdict(deque)


# ── Crypto helpers ──────────────────────────────────────────────────────────

def _hash_password(plain: str) -> str:
    salt = _secrets.token_bytes(16)
    h = hashlib.pbkdf2_hmac("sha256", plain.encode("utf-8"), salt, _PBKDF2_ITERS)
    return f"pbkdf2${_PBKDF2_ITERS}${salt.hex()}${h.hex()}"


def _verify_password(plain: str, stored: str) -> bool:
    try:
        scheme, iters_s, salt_hex, hash_hex = stored.split("$")
        if scheme != "pbkdf2":
            return False
        iters = int(iters_s)
        salt  = bytes.fromhex(salt_hex)
        want  = bytes.fromhex(hash_hex)
        got   = hashlib.pbkdf2_hmac("sha256", plain.encode("utf-8"), salt, iters)
        return hmac.compare_digest(got, want)
    except (ValueError, TypeError):
        return False


def _ensure_session_secret() -> str:
    s = _config.get("session-secret", "") or ""
    if len(s) < 32:
        s = _secrets.token_hex(32)
        _config["session-secret"] = s
        _save_config(_config)
    return s


def _sign_session(issued_at: int) -> str:
    secret = _ensure_session_secret().encode()
    mac = hmac.new(secret, str(issued_at).encode(), hashlib.sha256).hexdigest()
    return f"{issued_at}.{mac}"


def verify_session_cookie(cookie: str) -> bool:
    """Return True iff the cookie is a valid, non-expired session."""
    if not cookie or "." not in cookie:
        return False
    try:
        issued_s, mac_provided = cookie.split(".", 1)
        issued = int(issued_s)
    except ValueError:
        return False
    if time.time() - issued > SESSION_MAX_AGE:
        return False
    secret = _ensure_session_secret().encode()
    mac_expected = hmac.new(secret, issued_s.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(mac_provided, mac_expected)


def is_enabled() -> bool:
    """Whether the app currently requires a password."""
    return bool((_config.get("app-password-hash") or "").strip())


# ── Rate limiting for /api/login ────────────────────────────────────────────

def _client_ip(request: Request) -> str:
    """Resolve the login rate-limit key. Deliberately does NOT trust
    X-Forwarded-For: this app is reached either directly or through a tunnel
    (cloudflared/serveo) whose client always connects from localhost — so
    "trust XFF when the peer is localhost" (a normal reverse-proxy rule) is
    exactly backwards here: it's the ONE case where the peer is NOT the real
    caller, and a remote attacker going through the tunnel can set XFF to
    anything they want on every request, giving each login attempt a "fresh"
    IP and defeating the attempt counter below entirely (verified — this was
    the actual behavior before this fix). Using the raw peer means every
    tunnel-borne login attempt shares ONE bucket (peer stays 127.0.0.1),
    which is a stricter but SAFE degrade: it still won't over-limit direct
    local use (peer is the real LAN/loopback IP there), and it turns the
    per-IP limit into a de-facto global limit for the one scenario (remote
    access) where that's exactly what's needed."""
    return request.client.host if request.client else "unknown"


def _rate_limit_check(ip: str) -> int | None:
    """Return seconds-until-unlock if locked out, else None (proceed)."""
    now = time.time()
    attempts = _login_attempts[ip]
    # Drop stale entries
    while attempts and now - attempts[0] > LOCKOUT_WINDOW:
        attempts.popleft()
    if len(attempts) >= MAX_ATTEMPTS:
        oldest = attempts[0]
        return max(1, int(LOCKOUT_WINDOW - (now - oldest)))
    return None


def _record_failed_login(ip: str) -> None:
    _login_attempts[ip].append(time.time())


def _clear_login_attempts(ip: str) -> None:
    _login_attempts.pop(ip, None)


# ── CSRF / Secure cookie helpers ────────────────────────────────────────────

def _request_is_https(request: Request) -> bool:
    """Detect HTTPS through direct scheme or X-Forwarded-Proto header."""
    if request.url.scheme == "https":
        return True
    fwd_proto = request.headers.get("x-forwarded-proto", "").lower()
    return fwd_proto == "https"


def _csrf_check(request: Request) -> bool:
    """Return True if the request is allowed to mutate state.

    Origin header must either be absent (non-browser client like curl or a
    native app) or match the Host header. This blocks the classic CSRF attack
    where example.com POSTs to our API with the user's cookie attached.
    """
    if request.method.upper() not in _CSRF_METHODS:
        return True
    origin = request.headers.get("origin", "")
    if not origin:
        # No Origin → normally a non-browser client (curl/native app), allowed.
        # BUT when the box is exposed to the network (remote-enabled), an absent
        # Origin is also how a non-browser attacker would replay a stolen cookie,
        # so require a same-origin Origin on mutating requests in that mode. The
        # real browser UI always sends Origin on POST, so this doesn't break it.
        if _config.get("remote-enabled", False):
            return False
        return True
    host = request.headers.get("host", "")
    # Normalise origin to just "scheme://host[:port]" and compare bytewise.
    try:
        origin_norm = origin.rstrip("/").lower()
        # Any scheme matching our host counts as same-origin. We accept both
        # http:// and https:// since users run locally without TLS.
        return origin_norm.endswith("://" + host.lower())
    except Exception:
        return False


# ── FastAPI integration ─────────────────────────────────────────────────────

_LOGIN_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8"><title>Ripster — вход</title>
<style>
body{margin:0;background:#0a0a0d;color:#e8e8ec;font-family:-apple-system,BlinkMacSystemFont,system-ui,sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh}
.card{background:#15151a;border:1px solid #222;border-radius:14px;padding:32px 34px;width:320px;box-shadow:0 20px 60px rgba(0,0,0,.5)}
h1{margin:0 0 4px;font-size:22px;font-weight:800;letter-spacing:-.3px}
.sub{color:#7a7a85;font-size:12px;margin-bottom:20px}
input{width:100%;box-sizing:border-box;padding:11px 13px;background:#0a0a0d;border:1px solid #2a2a30;border-radius:9px;color:#e8e8ec;font-size:14px;outline:none;margin-bottom:10px}
input:focus{border-color:#fc3c44}
button{width:100%;padding:11px;background:#fc3c44;color:#fff;border:none;border-radius:9px;font-size:14px;font-weight:700;cursor:pointer}
button:hover{filter:brightness(1.1)}
.err{color:#fc3c44;font-size:12px;margin-bottom:10px;min-height:16px}
</style></head><body>
<div class="card">
  <h1>🎵 Ripster</h1>
  <div class="sub">Введи пароль приложения</div>
  <div class="err" id="err"></div>
  <input id="pw" type="password" placeholder="Пароль" autofocus>
  <button onclick="go()">Войти</button>
</div>
<script>
document.getElementById('pw').onkeydown = e => { if(e.key==='Enter') go(); };
async function go(){
  const pw = document.getElementById('pw').value;
  const err = document.getElementById('err');
  err.textContent = '';
  try {
    const r = await fetch('/api/login', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({password: pw})});
    if(r.ok){ location.href = '/'; }
    else { const d = await r.json().catch(()=>({})); err.textContent = d.detail || 'Неверный пароль'; }
  } catch(e){ err.textContent = 'Ошибка сети: '+e.message; }
}
</script></body></html>"""


def install(app, config: dict, save_config: Callable[[dict], None]) -> None:
    """Wire the auth layer into a FastAPI app. Call this exactly once at
    startup, before any other endpoints are registered that rely on auth."""
    global _config, _save_config
    _config = config
    _save_config = save_config

    @app.middleware("http")
    async def _auth_middleware(request: Request, call_next):
        # CSRF check applies to all mutating requests regardless of whether
        # auth is enabled — it's a defence-in-depth measure.
        # EXEMPT: the browser-extension token push is intentionally cross-origin
        # (it fires from the chrome-extension:// context) and authenticates with
        # its own shared secret, so the Origin-based CSRF guard doesn't apply.
        if request.url.path not in _CSRF_EXEMPT_PATHS and not _csrf_check(request):
            return JSONResponse(
                {"error": "forbidden", "detail": "Cross-site request blocked"},
                status_code=403,
            )

        if not is_enabled():
            # No owner password set. A purely-local box is fine as-is; but if
            # remote access is enabled, do NOT grant blanket owner rights over
            # the tunnel — fall through so only guest links + public paths
            # work (verify_session_cookie below never matches without a
            # password, so the tunnel can never reach owner endpoints).
            if not _config.get("remote-enabled", False):
                return await call_next(request)

        path = request.url.path
        # NOTE: the Apple auth routes (/apple/, /api/apple/) are intentionally NOT
        # public. They mutate owner state (set-token overwrites media-user-token,
        # POST /api/apple/cookies writes cookies.txt to disk), so exposing them
        # unauthenticated let anyone reaching the remote tunnel poison the token or
        # write the cookie file. The app UI calls them with the session cookie, and
        # the "🍎 Get Apple Token" bookmarklet opens set-token as a top-level GET
        # navigation, which carries the SameSite=Lax session cookie for a
        # logged-in owner — so gating them owner-only keeps that flow working.
        if (path in _PUBLIC_PATHS
                or path.startswith("/static/")
                or path.startswith("/spotify/")   # OAuth redirect target (external)
                or path.startswith("/guest/")):   # guest landing pages
            return await call_next(request)

        if verify_session_cookie(request.cookies.get("ripster-session", "")):
            request.state.is_owner = True
            return await call_next(request)

        # Check guest session — guests are deny-by-default: only an explicit
        # allowlist of paths is reachable, everything else returns 403.
        if _guest_session_fn is not None and _guest_session_fn(request):
            request.state.is_owner = False
            if not _guest_allowed(path, request.method):
                return JSONResponse(
                    {"error": "forbidden",
                     "detail": "Guest access is not allowed for this endpoint"},
                    status_code=403,
                )
            return await call_next(request)

        if path.startswith("/api/") or path == "/ws":
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return RedirectResponse(url=_LOGIN_PATH, status_code=303)

    @app.get(_LOGIN_PATH, response_class=HTMLResponse)
    async def login_page():
        return _LOGIN_HTML

    @app.get(_STATUS_API)
    async def auth_status(request: Request):
        return {
            "enabled":   is_enabled(),
            "logged_in": verify_session_cookie(request.cookies.get("ripster-session", "")),
        }

    @app.post(_LOGIN_API)
    async def api_login(body: dict, request: Request):
        if not is_enabled():
            return {"ok": True, "note": "auth disabled"}
        ip = _client_ip(request)
        locked = _rate_limit_check(ip)
        if locked is not None:
            return JSONResponse(
                {"detail": f"Слишком много попыток. Подожди {locked} секунд."},
                status_code=429,
                headers={"Retry-After": str(locked)},
            )
        pw     = (body or {}).get("password", "")
        stored = _config.get("app-password-hash", "")
        if not isinstance(pw, str) or not _verify_password(pw, stored):
            _record_failed_login(ip)
            raise HTTPException(401, "Неверный пароль")
        _clear_login_attempts(ip)
        resp = JSONResponse({"ok": True})
        resp.set_cookie(
            "ripster-session", _sign_session(int(time.time())),
            max_age=SESSION_MAX_AGE,
            httponly=True,
            samesite="lax",
            secure=_request_is_https(request),
        )
        return resp

    @app.post(_LOGOUT_API)
    async def api_logout():
        resp = JSONResponse({"ok": True})
        resp.delete_cookie("ripster-session")
        return resp

    @app.post(_PASSWORD_API)
    async def api_set_password(body: dict):
        new_pw = (body or {}).get("password", "")
        old_pw = (body or {}).get("current", "")
        if not isinstance(new_pw, str) or not isinstance(old_pw, str):
            raise HTTPException(400, "Invalid payload")
        if is_enabled():
            if not old_pw:
                raise HTTPException(401, "Введи текущий пароль")
            if not _verify_password(old_pw, _config.get("app-password-hash", "")):
                raise HTTPException(401, "Текущий пароль неверен")
        if new_pw == "":
            _config["app-password-hash"] = ""
        else:
            if len(new_pw) < 4:
                raise HTTPException(400, "Пароль слишком короткий (минимум 4 символа)")
            _config["app-password-hash"] = _hash_password(new_pw)
        _save_config(_config)
        return {"ok": True, "auth_enabled": is_enabled()}


def ws_allowed(ws: WebSocket) -> bool:
    """Check whether a WebSocket handshake should proceed."""
    if not is_enabled():
        return True
    if verify_session_cookie(ws.cookies.get("ripster-session", "")):
        return True
    # Allow guest sessions too
    if _guest_session_fn is not None:
        from starlette.requests import Request as _Req
        # WebSocket exposes cookies/headers the same way as Request
        try:
            return _guest_session_fn(ws)
        except Exception:
            pass
    return False
