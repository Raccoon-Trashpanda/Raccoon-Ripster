"""Guest routes — admin link management + session-info + guest landing page.

Public (no auth needed):
  GET  /guest/{token}          validate token, issue cookie, redirect to /
  GET  /api/session-info       returns {mode: "owner"|"guest"|"none", ...}

Owner-only:
  GET  /api/admin/links
  POST /api/admin/links/create
  POST /api/admin/links/revoke
  POST /api/admin/links/token-mode
  GET  /api/admin/links/{token}/activity

Guest-only:
  POST /api/guest/tokens        guest stores their own credentials
  GET  /api/guest/config        redacted, guest-safe subset of config
"""
from __future__ import annotations

import asyncio
import re as _re
import shutil as _shutil
import time   # module-level: _tunnel_watchdog uses time.time() (was NameError → watchdog never respawned a dead tunnel)
import subprocess as _subprocess
import threading as _threading
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from ripster.guest_manager import get_manager, COOKIE_NAME, LINK_TTL_S

router = APIRouter()

_config:        dict = {}
_broadcast           = None
_owner_auth_fn       = None   # verify_session_cookie from ripster.auth
_save_config         = None   # save_config(cfg) callable

# ── Serveo tunnel state ────────────────────────────────────────────────────────
_tunnel_proc:       Optional[asyncio.subprocess.Process] = None
_tunnel_url:        str  = ""
_tunnel_log:        list = []   # last 20 lines of SSH output
_tunnel_connecting: bool = False
_tunnel_fhs:        list = []   # open file handles for the live proc (closed on respawn)
_stray_cleaned:     bool = False  # orphan-tunnel WMI scan runs once per process (it's slow)
_RE_TUNNEL_URL = _re.compile(r'Forwarding .* from (https?://\S+)', _re.I)   # serveo
_RE_CF_URL     = _re.compile(r'(https://[-a-z0-9]+\.trycloudflare\.com)', _re.I)  # cloudflared
_RE_ANSI       = _re.compile(r'\x1b\[[0-9;]*[mGKH]')


def _extract_tunnel_url(line: str) -> str:
    """Pull the public URL out of a tunnel-provider log line (cloudflared first,
    then serveo). Returns '' if the line has no URL."""
    m = _RE_CF_URL.search(line)
    if m:
        return m.group(1).rstrip("/")
    m = _RE_TUNNEL_URL.search(line)
    if m:
        return m.group(1).rstrip("/")
    return ""


def _tlog(msg: str) -> None:
    """Append a tunnel event to project-root tunnel.log so the tunnel can be
    diagnosed regardless of how the server was launched (console / detached)."""
    try:
        import time as _t
        from pathlib import Path as _P
        f = _P(__file__).resolve().parents[2] / "tunnel.log"
        with open(f, "a", encoding="utf-8") as fh:
            fh.write(f"{_t.strftime('%H:%M:%S')} {msg}\n")
    except Exception:
        pass


def _tunnel_broadcast(loop, payload: dict) -> None:
    """Fire a broadcast from the reader thread onto the main event loop."""
    if _broadcast and loop is not None:
        try:
            asyncio.run_coroutine_threadsafe(_broadcast(payload), loop)
        except Exception:
            pass


def _tail_tunnel_output(proc, path, loop) -> None:
    """Tail ssh's output FILE in a daemon thread until the serveo URL appears
    or the process dies. File redirection (not a pipe) avoids the immediate
    empty-EOF seen when ssh.exe inherits a pipe under the launcher-spawned
    server. Runs until ssh exits, so status flips correctly on drop."""
    global _tunnel_url, _tunnel_connecting
    import time as _t
    pos = 0
    try:
        while True:
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    f.seek(pos)
                    chunk = f.read()
                    pos = f.tell()
            except Exception:
                chunk = ""
            for raw in chunk.splitlines():
                line = _RE_ANSI.sub('', raw).strip()
                if line:
                    _tunnel_log.append(line)
                    if len(_tunnel_log) > 20:
                        _tunnel_log.pop(0)
                u = _extract_tunnel_url(line)
                if u and not _tunnel_url:
                    _tunnel_url = u
                    _tunnel_connecting = False
                    _config["public-url"] = _tunnel_url
                    _config["remote-enabled"] = True
                    if _save_config:
                        _save_config(_config)
                    _tlog(f"URL captured: {_tunnel_url}")
                    _tunnel_broadcast(loop, {"type": "tunnel_status", "running": True,
                                             "connecting": False, "url": _tunnel_url})
            dead = proc.poll() is not None
            if dead:
                if not _tunnel_url:
                    _tlog(f"ssh died before URL rc={proc.poll()}; last={_tunnel_log[-4:]}")
                break
            _t.sleep(2.0 if _tunnel_url else 0.5)
    except Exception as e:
        _tlog(f"tail EXCEPTION: {type(e).__name__}: {e}")
    finally:
        _tunnel_connecting = False
        _tlog(f"tail ended; url={_tunnel_url or '-'} rc={proc.poll()}")
        _tunnel_broadcast(loop, {"type": "tunnel_status", "running": False,
                                 "connecting": False, "url": ""})


def _kill_stray_tunnels() -> None:
    """Kill EVERY leftover tunnel process belonging to this app — ssh→serveo.net
    and cloudflared→:7799 — including orphans from a previous or crashed server
    instance that ``_tunnel_proc`` no longer tracks.

    Why this matters: a fresh server process starts with ``_tunnel_proc = None``,
    so ``_cleanup_prev_tunnel`` alone can't see tunnels spawned by an earlier
    instance. Over many restarts they pile up (we once found 12), and because they
    all reconnect to serveo.net with the same SSH key requesting the same
    subdomain, serveo refuses the connection → "tunnel won't come up". Matching on
    the command line (``serveo.net`` / port ``7799``) makes this safe: it never
    touches the Python server itself or unrelated ssh sessions (e.g. to a router)."""
    import sys as _sys, subprocess as _sp
    try:
        if _sys.platform == "win32":
            # WMI -Filter narrows to the two process names IN the query (fast),
            # instead of pulling EVERY process then filtering in PowerShell (slow).
            ps = (
                "Get-CimInstance Win32_Process -Filter "
                "\"Name='ssh.exe' OR Name='cloudflared.exe'\" | Where-Object { "
                "($_.Name -eq 'ssh.exe' -and $_.CommandLine -like '*serveo.net*') -or "
                "($_.Name -eq 'cloudflared.exe' -and $_.CommandLine -like '*7799*') } | "
                "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
            )
            _sp.run(["powershell", "-NoProfile", "-Command", ps],
                    timeout=15, capture_output=True,
                    creationflags=0x08000000)  # CREATE_NO_WINDOW
        else:
            _sp.run("pkill -f 'ssh.*serveo\\.net'; pkill -f 'cloudflared.*7799'",
                    shell=True, timeout=10)
    except Exception as e:
        _tlog(f"stray-tunnel cleanup failed: {e}")


def _cleanup_prev_tunnel() -> None:
    """Kill any lingering ssh proc and close its leaked file handles. Without this,
    rapid respawns pile up un-closed `open("w")` handles to _serveo_out.log; on
    Windows the next open() then hits a sharing violation and ssh dies instantly
    rc=255 with no output (the bug seen 2026-05-30)."""
    global _tunnel_proc, _tunnel_fhs, _stray_cleaned
    if _tunnel_proc and _tunnel_proc.poll() is None:
        try: _tunnel_proc.terminate()
        except Exception: pass
    _tunnel_proc = None
    for fh in _tunnel_fhs:
        try: fh.close()
        except Exception: pass
    _tunnel_fhs = []
    # Reap orphans from prior/crashed instances ONLY ONCE per process — the WMI
    # scan is slow (~1-3s) and made every tunnel on/off sluggish. Orphans can only
    # come from an EARLIER instance; within this process our tracked _tunnel_proc
    # is terminated above, so subsequent toggles need no expensive scan.
    if not _stray_cleaned:
        _kill_stray_tunnels()
        _stray_cleaned = True


def stop_for_restart() -> None:
    """Погасить туннель ПЕРЕД тем, как процесс уйдёт на перезапуск.

    Все пути перезапуска уходят через `os._exit(0)`, а он не выполняет ни
    обработчиков выключения FastAPI, ни `atexit`. Поэтому ssh оставался сиротой,
    serveo продолжал держать закреплённый поддомен ещё десятки секунд, свежий
    экземпляр получал «remote port forwarding failed», и гость по внешней ссылке
    видел 502. 15.08.2026 таких окон набралось восемь за полтора часа — по одному
    на каждый перезапуск. Закрытая по-хорошему сессия освобождает привязку
    заметно раньше.

    Живёт здесь, а не в вызывающих: реализаций перезапуска в проекте ДВЕ
    (`app.py::_spawn_restart` и `routes/setup.py::restart_app`), и правка,
    положенная в одну из них, ровно наполовину не работает — именно так и вышло
    с первой попыткой. Один общий вызов вместо двух копий.
    """
    try:
        _cleanup_prev_tunnel()
        print("[tunnel] остановлен перед перезапуском", flush=True)
    except Exception as e:                                         # noqa: BLE001
        print(f"[tunnel] не остановился перед перезапуском: {e}", flush=True)


_SSH_VERIFIED: Optional[str] = None   # cached working ssh.exe path


def _resolve_ssh() -> str:
    """Return a path to an ssh client that ACTUALLY RUNS in this process context.

    `shutil.which("ssh")` can't be trusted here: the launcher (PyQt QProcess) may
    inherit a PATH (e.g. mutated by an Android-emulator install) where `ssh` resolves
    to a binary that dies rc=255 with no output. So we probe candidates with `ssh -V`
    (prints version to stderr, exits 0) and pick the first that genuinely works.
    Native `C:\\Windows\\System32\\OpenSSH\\ssh.exe` is preferred — statically linked,
    no MSYS DLL dependency, verified working."""
    global _SSH_VERIFIED
    if _SSH_VERIFIED:
        return _SSH_VERIFIED
    import os as _os
    candidates = [
        _os.path.join(_os.environ.get("SystemRoot", r"C:\Windows"),
                      "System32", "OpenSSH", "ssh.exe"),
        r"C:\Windows\System32\OpenSSH\ssh.exe",
        r"C:\Program Files\OpenSSH\ssh.exe",
        r"C:\Program Files\Git\usr\bin\ssh.exe",
        _shutil.which("ssh") or "ssh",
    ]
    seen = set()
    for c in candidates:
        if not c or c in seen:
            continue
        seen.add(c)
        if c != "ssh" and not _os.path.isfile(c):
            continue
        try:
            v = _subprocess.run([c, "-V"], capture_output=True, text=True, timeout=8,
                                creationflags=getattr(_subprocess, "CREATE_NO_WINDOW", 0))
            ver = ((v.stderr or "") + (v.stdout or "")).strip()
            if v.returncode == 0 and "OpenSSH" in ver:
                _tlog(f"ssh resolved OK: {c}  ({ver})")
                _SSH_VERIFIED = c
                return c
            _tlog(f"ssh candidate rejected: {c}  rc={v.returncode} ver={ver!r}")
        except Exception as e:
            _tlog(f"ssh candidate errored: {c}  {type(e).__name__}: {e}")
    # Nothing verified — fall back to PATH lookup and let the spawn surface the error.
    fallback = _shutil.which("ssh") or "ssh"
    _tlog(f"ssh resolve: NO working candidate, falling back to {fallback}")
    return fallback


_CF_VERIFIED: Optional[str] = None   # cached working cloudflared.exe path


def _cloudflared_target():
    from pathlib import Path as _Pth
    return _Pth(__file__).resolve().parents[2] / "tools" / "cloudflared.exe"


def _resolve_cloudflared() -> str:
    """Return a runnable cloudflared binary path, or '' if none is available."""
    global _CF_VERIFIED
    if _CF_VERIFIED:
        return _CF_VERIFIED
    import os as _os
    candidates = [
        (_config.get("cloudflared-path") or "").strip(),
        str(_cloudflared_target()),
        _shutil.which("cloudflared") or "",
        r"C:\Program Files (x86)\cloudflared\cloudflared.exe",
        r"C:\Program Files\cloudflared\cloudflared.exe",
    ]
    seen = set()
    for c in candidates:
        if not c or c in seen:
            continue
        seen.add(c)
        if not _os.path.isfile(c):
            continue
        try:
            v = _subprocess.run([c, "--version"], capture_output=True, text=True, timeout=10,
                                creationflags=getattr(_subprocess, "CREATE_NO_WINDOW", 0))
            if v.returncode == 0 and "cloudflared" in ((v.stdout or "") + (v.stderr or "")).lower():
                _CF_VERIFIED = c
                _tlog(f"cloudflared resolved OK: {c}  ({(v.stdout or v.stderr).strip()[:60]})")
                return c
            _tlog(f"cloudflared candidate rejected: {c} rc={v.returncode}")
        except Exception as e:
            _tlog(f"cloudflared candidate errored: {c} {type(e).__name__}: {e}")
    return ""


_CF_DL_URL = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe"


async def _ensure_cloudflared() -> str:
    """Resolve cloudflared, downloading the official Windows binary on first use."""
    cf = _resolve_cloudflared()
    if cf:
        return cf
    target = _cloudflared_target()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        _tlog(f"cloudflared not found — downloading to {target}")
        import httpx as _httpx
        async with _httpx.AsyncClient(timeout=_httpx.Timeout(connect=10, read=120, write=30, pool=10),
                                      follow_redirects=True) as c:
            async with c.stream("GET", _CF_DL_URL) as r:
                if r.status_code != 200:
                    _tlog(f"cloudflared download HTTP {r.status_code}")
                    return ""
                tmp = target.with_suffix(".exe.part")
                with open(tmp, "wb") as fh:
                    async for chunk in r.aiter_bytes(1 << 16):
                        fh.write(chunk)
                tmp.replace(target)
        _tlog("cloudflared downloaded ok")
        global _CF_VERIFIED
        _CF_VERIFIED = None
        return _resolve_cloudflared()
    except Exception as e:
        _tlog(f"cloudflared download failed: {type(e).__name__}: {e}")
        return ""


def _spawn_tunnel() -> None:
    """Launch the public tunnel via Popen, redirecting output to a file that a daemon
    thread tails (pipe-free — robust under the launcher server). Provider is chosen by
    config `tunnel-provider` (default cloudflared — fast, no serveo 10MB-drops; falls
    back to serveo/ssh when cloudflared isn't available)."""
    global _tunnel_proc, _tunnel_url, _tunnel_log, _tunnel_connecting, _tunnel_fhs
    from pathlib import Path as _Path
    _cleanup_prev_tunnel()
    proj = _Path(__file__).resolve().parents[2]

    provider = (_config.get("tunnel-provider") or "cloudflared").lower()
    cf = _resolve_cloudflared() if provider != "serveo" else ""
    if cf:
        # TryCloudflare quick tunnel: free, anonymous, no large-file drops.
        # --protocol http2: default is QUIC (UDP 7844); many home routers/ISPs
        # throttle or block UDP → the tunnel registers but passes no traffic
        # ("failed to accept QUIC stream: no recent network activity") and the
        # public URL is dead. HTTP/2 runs over TCP 443 and works everywhere.
        cmd = [cf, "tunnel", "--no-autoupdate", "--protocol", "http2",
               "--url", "http://127.0.0.1:7799"]
        _tlog(f"tunnel provider: cloudflared ({cf})")
    else:
        ssh_exe = _resolve_ssh()
        # Optional STABLE subdomain: serveo's default *.serveousercontent.com URL
        # embeds the user's IP + a per-connection hash, so it changes on every
        # restart AND whenever the IP changes — breaking saved guest links. A
        # custom subdomain `<name>.serveo.net` (config `tunnel-subdomain`) is
        # IP-independent and stable across restarts (first-come on serveo). If
        # serveo refuses it, ssh exits (ExitOnForwardFailure) → clear the key.
        sub = (_config.get("tunnel-subdomain") or "").strip().lower()
        remote = f"{sub}:80:localhost:7799" if sub else "80:localhost:7799"
        # A custom subdomain requires a serveo-REGISTERED SSH key. Use the
        # configured key, else the generated ~/.ssh/serveo_ripster if present.
        import os as _os
        key = (_config.get("tunnel-ssh-key") or "").strip()
        if not key and sub:        # only when a custom subdomain is requested
            _dk = _os.path.expanduser("~/.ssh/serveo_ripster")
            if _os.path.isfile(_dk):
                key = _dk
        cmd = [ssh_exe,
               "-o", "StrictHostKeyChecking=no",      # serveo is ephemeral — don't verify
               "-o", "UserKnownHostsFile=NUL",        # no profile/known_hosts dependency
               # NOTE: do NOT set NumberOfPasswordPrompts=0 — serveo now authenticates
               # via keyboard-interactive (auto-succeeds); setting prompts to 0 disables
               # that method entirely → "Permission denied (publickey,keyboard-interactive)".
               "-o", "ServerAliveInterval=30",
               "-o", "ServerAliveCountMax=3",
               "-o", "ExitOnForwardFailure=yes"]
        if key:
            cmd += ["-i", key]
        cmd += ["-R", remote, "serveo.net"]
        _tlog(f"tunnel provider: serveo (subdomain={sub or 'random'}, key={'yes' if key else 'no'})")
    _tunnel_url        = ""
    _tunnel_log        = []
    _tunnel_connecting = True
    log_path = proj / "_serveo_out.log"
    out_fh = open(log_path, "w", encoding="utf-8", errors="replace")
    # stdin must be a REAL (empty) file handle, NOT subprocess.DEVNULL. On Windows
    # DEVNULL maps to NUL, and the native OpenSSH client (C:\Windows\System32\OpenSSH)
    # detects the null device and DISABLES keyboard-interactive auth entirely →
    # "No more authentication methods" → "Permission denied (publickey,keyboard-interactive)".
    # serveo now admits anonymous tunnels via keyboard-interactive (0 prompts), so the
    # method must stay enabled. An empty file gives immediate EOF without blocking.
    stdin_path = proj / "_serveo_stdin.tmp"
    try:
        stdin_path.write_text("")
    except Exception:
        pass
    stdin_fh = open(stdin_path, "r")
    _tunnel_fhs = [out_fh, stdin_fh]
    _tunnel_proc = _subprocess.Popen(
        cmd, stdin=stdin_fh, stdout=out_fh, stderr=_subprocess.STDOUT,
        creationflags=getattr(_subprocess, "CREATE_NO_WINDOW", 0))
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    _threading.Thread(target=_tail_tunnel_output,
                      args=(_tunnel_proc, str(log_path), loop), daemon=True).start()


def install(app, ctx) -> None:
    global _config, _broadcast, _owner_auth_fn, _save_config
    _config        = ctx.config
    _broadcast     = ctx.broadcast
    _owner_auth_fn = ctx.owner_auth_fn
    _save_config   = ctx.save_config
    app.include_router(router)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _is_owner(request: Request) -> bool:
    # A valid guest session is NEVER the owner — check this before anything else
    try:
        gm  = get_manager()
        sid = gm.get_session_id_from_request(request)
        if sid and gm.get_session(sid):
            return False
    except Exception:
        pass

    if _owner_auth_fn is None:
        return True

    # When auth is disabled, anyone without a guest session is treated as owner
    try:
        from ripster.auth import is_enabled
        if not is_enabled():
            return True
    except Exception:
        pass

    return _owner_auth_fn(request.cookies.get("ripster-session", ""))


def _require_owner(request: Request):
    if not _is_owner(request):
        raise HTTPException(403, "Owner access required")


def _require_guest_session(request: Request) -> str:
    """Returns the session_id or raises 401."""
    gm  = get_manager()
    sid = gm.get_session_id_from_request(request)
    if not sid or not gm.get_session(sid):
        raise HTTPException(401, "Valid guest session required")
    return sid


_OFFLINE_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Ripster — недоступно</title>
<style>
body{margin:0;background:#0a0a0d;color:#e8e8ec;font-family:system-ui,sans-serif;
     display:flex;align-items:center;justify-content:center;min-height:100vh}
.card{background:#15151a;border:1px solid #222;border-radius:14px;padding:32px 34px;
      width:340px;text-align:center}
h1{margin:0 0 8px;font-size:20px;font-weight:800}
p{color:#7a7a85;font-size:13px;line-height:1.6}
</style></head><body>
<div class="card">
  <div style="font-size:40px;margin-bottom:12px">🔒</div>
  <h1>Внешний доступ выключен</h1>
  <p>Владелец не включил удалённый доступ.<br>
     Попроси его запустить сервер для гостей.</p>
</div></body></html>"""

_EXPIRED_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Ripster — ссылка истекла</title>
<style>
body{margin:0;background:#0a0a0d;color:#e8e8ec;font-family:system-ui,sans-serif;
     display:flex;align-items:center;justify-content:center;min-height:100vh}
.card{background:#15151a;border:1px solid #222;border-radius:14px;padding:32px 34px;
      width:340px;text-align:center}
h1{margin:0 0 8px;font-size:20px;font-weight:800}
p{color:#7a7a85;font-size:13px;line-height:1.6}
</style></head><body>
<div class="card">
  <div style="font-size:40px;margin-bottom:12px">🔗</div>
  <h1>Ссылка истекла</h1>
  <p>Время действия этой ссылки закончилось.<br>
     Запроси новую у владельца.</p>
</div></body></html>"""


# ── Public: guest landing ─────────────────────────────────────────────────────

@router.get("/guest/{token}", response_class=HTMLResponse)
async def guest_landing(token: str, request: Request):
    if not _config.get("remote-enabled", False):
        # Allow localhost access even when remote is disabled (owner testing)
        host = request.headers.get("host", "")
        if not (host.startswith("127.0.0.1") or host.startswith("localhost")):
            return HTMLResponse(_OFFLINE_HTML, status_code=503)

    gm   = get_manager()
    link = gm.validate_token(token)
    if not link:
        return HTMLResponse(_EXPIRED_HTML, status_code=410)

    # Reuse existing session for this token if cookie already set
    existing_sid = request.cookies.get(COOKIE_NAME, "")
    if existing_sid and gm.get_session(existing_sid):
        # Already authenticated — just redirect
        return RedirectResponse("/", status_code=303)

    # Create a new session
    sid = gm.create_session(token)
    if not sid:
        return HTMLResponse(_EXPIRED_HTML, status_code=410)

    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(
        COOKIE_NAME,
        sid,
        max_age=LINK_TTL_S,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
    )
    return resp


# ── Public: session-info ──────────────────────────────────────────────────────

@router.get("/api/session-info")
async def session_info(request: Request):
    gm = get_manager()
    if _is_owner(request):
        return {"mode": "owner"}

    sid  = gm.get_session_id_from_request(request)
    link = gm.get_session(sid) if sid else None
    if link:
        q = link.get("quota", {})
        return {
            "mode":        "guest",
            "label":       link.get("label", "Guest"),
            "token_mode":  link.get("token_mode", "owner"),
            "expires_at":  link.get("expires_at", ""),
            "quota": {
                "type":  q.get("type", "unlimited"),
                "limit": q.get("limit", 0),
                "used":  q.get("used", 0),
            },
        }
    return {"mode": "none"}


# ── Owner: admin panel ────────────────────────────────────────────────────────

@router.get("/api/admin/links")
async def admin_list_links(request: Request):
    _require_owner(request)
    gm = get_manager()
    gm.cleanup_expired()
    return gm.all_links()


@router.post("/api/admin/links/create")
async def admin_create_link(body: dict, request: Request):
    _require_owner(request)
    label      = (body.get("label") or "Guest").strip()
    quota_obj  = body.get("quota") or {}
    quota_type = quota_obj.get("type", "unlimited") if isinstance(quota_obj, dict) else "unlimited"
    quota_limit = int(quota_obj.get("limit", 0)) if isinstance(quota_obj, dict) else 0
    token_mode = body.get("token_mode", "owner")
    if quota_type not in ("unlimited", "count", "time"):
        raise HTTPException(400, "quota type must be unlimited|count|time")
    if token_mode not in ("owner", "guest"):
        raise HTTPException(400, "token_mode must be owner|guest")
    try:
        gm   = get_manager()
        link = gm.create_link(label, quota_type, quota_limit, token_mode)
    except ValueError as e:
        raise HTTPException(400, str(e))
    pub = _config.get("public-url", "").rstrip("/")
    base = pub or str(request.base_url).rstrip("/")
    return {
        "ok":   True,
        "link": link,
        "url":  f"{base}/guest/{link['token']}",
    }


@router.post("/api/admin/links/revoke")
async def admin_revoke_link(body: dict, request: Request):
    _require_owner(request)
    token = (body.get("token") or "").strip()
    if not token:
        raise HTTPException(400, "token required")
    ok = get_manager().revoke_link(token)
    if _broadcast and ok:
        await _broadcast({"type": "guest_link_revoked", "token": token})
    return {"ok": ok}


@router.post("/api/admin/links/token-mode")
async def admin_set_token_mode(body: dict, request: Request):
    _require_owner(request)
    token = (body.get("token") or "").strip()
    mode  = (body.get("token_mode") or body.get("mode") or "").strip()
    ok = get_manager().set_token_mode(token, mode)
    return {"ok": ok}


@router.get("/api/admin/links/{token}/activity")
async def admin_link_activity(token: str, request: Request):
    _require_owner(request)
    gm   = get_manager()
    link = gm._links.get(token)
    if not link:
        raise HTTPException(404, "Link not found")
    # Count live sessions for this link
    session_count = sum(
        1 for sid, tok in gm._sessions.items()
        if tok == token and gm.get_session(sid)
    )
    return {
        "activity":      link.get("activity", []),
        "session_count": session_count,
        "label":         link.get("label", ""),
        "created_at":    link.get("created_at", ""),
        "expires_at":    link.get("expires_at", ""),
        "quota":         link.get("quota", {}),
    }


# ── Owner: serveo tunnel ─────────────────────────────────────────────────────

async def auto_start_tunnel() -> None:
    """Called from lifespan startup when remote-enabled is true."""
    global _tunnel_proc, _tunnel_url, _tunnel_log, _tunnel_connecting
    _tlog(f"auto_start_tunnel called; remote-enabled={_config.get('remote-enabled', False)} "
          f"ssh={_shutil.which('ssh')}")
    if not _config.get("remote-enabled", False):
        _tlog("skip: remote-enabled is false")
        return
    if _tunnel_proc and _tunnel_proc.poll() is None:
        _tlog("skip: tunnel already running")
        return
    try:
        if (_config.get("tunnel-provider") or "cloudflared").lower() != "serveo":
            await _ensure_cloudflared()
        _spawn_tunnel()
        _tlog("spawned tunnel ok")
        print("[tunnel] auto-start: connecting…", flush=True)
    except Exception as e:
        _tunnel_connecting = False
        _tlog(f"auto-start EXCEPTION: {type(e).__name__}: {e}")
        print(f"[tunnel] auto-start failed: {e}", flush=True)
    # Keep the tunnel alive: a flaky uplink / serveo drop kills the ssh process
    # after a few minutes, and nothing respawned it → the guest link died. The
    # watchdog respawns it automatically.
    global _watchdog_started
    if not _watchdog_started:
        _watchdog_started = True
        asyncio.create_task(_tunnel_watchdog())


_watchdog_started = False


async def _tunnel_watchdog() -> None:
    """Respawn the tunnel whenever its ssh process has died, so the guest link
    self-heals across network blips instead of staying down until a restart.

    Backoff: serveo may still hold the (stable) subdomain binding from the dead
    ssh for ~60-90s, during which a respawn instantly fails (ExitOnForwardFailure).
    Hammering it would rate-limit our key → 'serveo won't start'. So we respawn at
    most once per 60s and give the dead binding time to release."""
    # 🔴 09.08.2026. Пауза была ПОСТОЯННОЙ — 60 секунд, сколько бы попыток
    # подряд ни провалилось. За сутки это дало 702 строки «link down →
    # reconnecting» в консоли (больше, чем все остальные сообщения вместе) и,
    # что важнее, столько же попыток входа на serveo. О вреде частых попыток
    # предупреждает комментарий выше — и он же, судя по всему, описывает нашу
    # текущую беду: закреплённый поддомен сервер отвергает, а СЛУЧАЙНЫЙ держит.
    # Похоже на ограничение по ключу, которое мы сами себе и устроили.
    #
    # Теперь пауза растёт: минута, две, пять, пятнадцать, полчаса. Успешный
    # подъём сбрасывает её обратно. И в консоль пишем не каждую попытку, а
    # первую и потом изредка: повторяющаяся строка не несёт новой информации,
    # зато топит собой всё остальное.
    _BACKOFF = (60, 120, 300, 900, 1800)
    last_respawn = 0.0
    fails = 0
    was_alive = None
    while True:
        await asyncio.sleep(20)
        try:
            if not _config.get("remote-enabled", False) or _tunnel_connecting:
                continue
            alive = bool(_tunnel_proc and _tunnel_proc.poll() is None)
            if alive:
                if fails:
                    _tlog(f"watchdog: туннель поднялся после {fails} неудач")
                    print(f"[tunnel] link restored (failed attempts: {fails})",
                          flush=True)
                fails = 0
                was_alive = True
                continue
            wait = _BACKOFF[min(fails, len(_BACKOFF) - 1)]
            if (time.time() - last_respawn) < wait:
                continue
            last_respawn = time.time()
            fails += 1
            _tlog(f"watchdog: tunnel down → respawning (попытка {fails}, "
                  f"следующая через {_BACKOFF[min(fails, len(_BACKOFF)-1)]} с)")
            # В консоль — только первая неудача и далее каждая пятая.
            if fails == 1 or fails % 5 == 0:
                print(f"[tunnel] no link, reconnecting (attempt {fails}, "
                      f"will slow down)", flush=True)
            was_alive = False
            _spawn_tunnel()
        except Exception as e:
            _tlog(f"watchdog error: {type(e).__name__}: {e}")


@router.post("/api/tunnel/start")
async def tunnel_start(body: dict, request: Request):
    global _tunnel_proc, _tunnel_url, _tunnel_log, _tunnel_connecting
    _require_owner(request)

    if _tunnel_proc and _tunnel_proc.poll() is None:
        return {"ok": False, "error_key": "err.tunnel_already_running", "error": "Туннель уже запущен", "url": _tunnel_url}

    try:
        if (_config.get("tunnel-provider") or "cloudflared").lower() != "serveo":
            await _ensure_cloudflared()
        _spawn_tunnel()
        return {"ok": True, "connecting": True}
    except Exception as e:
        _tunnel_connecting = False
        return {"ok": False, "error": str(e)}


@router.post("/api/tunnel/stop")
async def tunnel_stop(request: Request):
    global _tunnel_proc, _tunnel_url, _tunnel_connecting
    _require_owner(request)
    proc = _tunnel_proc
    if proc:
        try:
            proc.terminate()
            await asyncio.get_event_loop().run_in_executor(None, lambda: proc.wait(3))
        except Exception:
            try: proc.kill()
            except Exception: pass
    _cleanup_prev_tunnel()          # closes leaked out_fh/stdin_fh handles too
    _tunnel_url        = ""
    _tunnel_connecting = False
    return {"ok": True}


@router.get("/api/tunnel/status")
async def tunnel_status_ep(request: Request):
    _require_owner(request)
    running = bool(_tunnel_proc and _tunnel_proc.poll() is None)
    return {
        "running":    running,
        "connecting": _tunnel_connecting,
        "url":        _tunnel_url,
        "log":        list(_tunnel_log),
    }


# ── Owner: remote access control ─────────────────────────────────────────────

@router.get("/api/remote/status")
async def remote_status(request: Request):
    _require_owner(request)
    gm = get_manager()
    gm.cleanup_expired()
    return {
        "enabled":         _config.get("remote-enabled", False),
        "public_url":      _config.get("public-url", ""),
        "active_links":    len(gm.active_links()),
    }


@router.post("/api/remote/start")
async def remote_start(body: dict, request: Request):
    _require_owner(request)
    pub = (body.get("public_url") or "").strip()
    _config["remote-enabled"] = True
    if pub:
        _config["public-url"] = pub
    if _save_config:
        _save_config(_config)
    return {"ok": True, "public_url": _config.get("public-url", "")}


@router.post("/api/remote/stop")
async def remote_stop(request: Request):
    _require_owner(request)
    _config["remote-enabled"] = False
    if _save_config:
        _save_config(_config)
    gm = get_manager()
    revoked = gm.revoke_all_links()
    if _broadcast:
        await _broadcast({"type": "remote_stopped"})
    return {"ok": True, "revoked": revoked}


# ── Guest: token management ───────────────────────────────────────────────────

@router.post("/api/guest/tokens")
async def guest_update_tokens(body: dict, request: Request):
    sid = _require_guest_session(request)
    ok  = get_manager().update_guest_tokens(sid, body or {})
    return {"ok": ok}


@router.get("/api/guest/history")
async def guest_history(request: Request):
    """Returns this guest's own download activity log."""
    sid  = _require_guest_session(request)
    link = get_manager().get_session(sid)
    if not link:
        return {"activity": []}
    acts = list(reversed(link.get("activity", [])))
    return {"activity": acts[:200]}


@router.get("/api/guest/config")
async def guest_config(request: Request):
    """Minimal, non-secret config subset for the guest UI."""
    _require_guest_session(request)
    return {
        "engine":     _config.get("engine", "zhaarey"),
        "quality":    _config.get("quality", "alac"),
        "storefront": _config.get("storefront", "gb"),
    }
