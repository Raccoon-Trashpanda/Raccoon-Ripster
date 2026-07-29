"""
Admin Console — owner-only system diagnostics endpoint.

This is the SECOND console: not for download events (those live in
console.html / appendLog), but for internal system state — module health,
service credentials, queue, storage, tokens, process info.

Endpoints (all owner-only; guests get 403 via the existing auth middleware):

  GET  /api/admin/diagnostics       — full structured snapshot
  POST /api/admin/probe-all         — probe every configured service in parallel
  GET  /api/admin/system-log        — recent server-side stdout (tail=N, service, level)
  GET  /api/admin/modules           — loaded engines / route modules / package versions

Install: admin.install(app, ctx)
"""
from __future__ import annotations

import asyncio
import os
import platform
import re
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request

router = APIRouter()

_cfg: Any = None
_ctx: Any = None
_started_at: float = time.time()


# ── Owner gate ───────────────────────────────────────────────────────────────

def _require_owner(request: Request) -> None:
    """Reject any non-owner. Guest sessions are deny-by-default at the auth
    middleware, but this is a defence in depth: even if the allowlist ever
    grows to include /api/admin/ accidentally, the route still rejects."""
    try:
        from ripster.guest_manager import get_manager
        gm  = get_manager()
        sid = gm.get_session_id_from_request(request)
        if sid and gm.get_session(sid):
            raise HTTPException(403, "Admin console is owner-only")
    except HTTPException:
        raise
    except Exception:
        pass

    if _ctx is not None and getattr(_ctx, "owner_auth_fn", None):
        cookie = request.cookies.get("ripster-session", "")
        if not _ctx.owner_auth_fn(cookie):
            # If auth is disabled entirely, treat the local request as owner.
            try:
                from ripster.auth import is_enabled
                if is_enabled():
                    raise HTTPException(403, "Admin console requires owner login")
            except HTTPException:
                raise
            except Exception:
                pass


# ── Helpers ──────────────────────────────────────────────────────────────────

def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe_int(x: Any, default: int = 0) -> int:
    try:
        return int(x)
    except (TypeError, ValueError):
        return default


def _fmt_bytes(n: int) -> str:
    """Human-readable byte size."""
    if n < 1024:
        return f"{n} B"
    units = ["KB", "MB", "GB", "TB"]
    v = float(n)
    for u in units:
        v /= 1024
        if v < 1024:
            return f"{v:.1f} {u}"
    return f"{v:.1f} PB"


def _process_info() -> dict:
    """Process metrics — uptime, pid, memory if psutil available."""
    info = {
        "pid":          os.getpid(),
        "python":       sys.version.split()[0],
        "platform":     platform.platform(),
        "uptime_s":     int(time.time() - _started_at),
        "started_at":   datetime.fromtimestamp(_started_at, timezone.utc).isoformat(timespec="seconds"),
        "memory_mb":    None,
        "cwd":          os.getcwd(),
    }
    try:
        import psutil
        p = psutil.Process()
        info["memory_mb"]   = round(p.memory_info().rss / (1024 * 1024), 1)
        info["threads"]     = p.num_threads()
        info["open_files"]  = len(p.open_files()) if hasattr(p, "open_files") else None
    except ImportError:
        info["psutil"] = "not installed (pip install psutil for memory metrics)"
    except Exception as e:
        info["psutil_error"] = str(e)
    return info


def _package_versions() -> dict:
    """Pinned versions of critical deps so misconfig is visible.

    Falls back to importlib.metadata when the module itself has no
    ``__version__`` (yt_dlp, mutagen, protobuf-from-google.protobuf).
    """
    try:
        from importlib.metadata import version as _pkg_version, PackageNotFoundError
    except ImportError:
        _pkg_version, PackageNotFoundError = None, Exception

    # (import_name, distribution_name)
    pkgs = [
        ("fastapi",    "fastapi"),
        ("uvicorn",    "uvicorn"),
        ("httpx",      "httpx"),
        ("streamrip",  "streamrip"),
        ("yt_dlp",     "yt-dlp"),
        ("mutagen",    "mutagen"),
        ("PIL",        "Pillow"),
        ("pywidevine", "pywidevine"),
        ("google.protobuf", "protobuf"),
        ("yaml",       "PyYAML"),
        ("Crypto",     "pycryptodome"),
        ("m3u8",       "m3u8"),
        ("construct",  "construct"),
        ("grpc",       "grpcio"),
    ]
    versions = {}
    for import_name, dist_name in pkgs:
        try:
            __import__(import_name)
        except Exception:
            versions[dist_name] = "missing"
            continue
        v = "?"
        if _pkg_version:
            try:
                v = _pkg_version(dist_name)
            except PackageNotFoundError:
                v = "?"
            except Exception:
                v = "?"
        if v == "?":
            try:
                mod = sys.modules.get(import_name) or __import__(import_name)
                v = getattr(mod, "__version__", "?")
            except Exception:
                pass
        versions[dist_name] = v
    return versions


def _storage_info() -> dict:
    """Free space + downloads dir size."""
    info = {}
    try:
        save_path = _cfg.get("save-path", "downloads") if _cfg else "downloads"
        p = Path(save_path)
        if not p.is_absolute():
            base_dir = _ctx.base_dir if _ctx and _ctx.base_dir else Path.cwd()
            p = base_dir / p

        if p.exists():
            total, used, free = shutil.disk_usage(p)
            info["download_dir"] = str(p)
            info["disk_total"]   = _fmt_bytes(total)
            info["disk_free"]    = _fmt_bytes(free)
            info["disk_used"]    = _fmt_bytes(used)
            info["disk_free_pct"] = round(free / total * 100, 1) if total else 0
        else:
            info["download_dir"] = str(p) + " (does not exist)"
    except Exception as e:
        info["error"] = str(e)
    return info


def _persistence_files() -> list:
    """Mtime / size of each persistence file — fast staleness check."""
    if not _ctx or not _ctx.base_dir:
        return []
    base = _ctx.base_dir
    paths = [
        ("config.yaml",         base / "config.yaml"),
        ("history.json",        base / "history.json"),
        ("queue_pending.json",  base / "queue_pending.json"),
        ("watchlist.json",      base / "watchlist.json"),
        ("guest_links.json",    base / "guest_links.json"),
        ("guest_sessions.json", base / "guest_sessions.json"),
        ("ripster_stats.db",    base / "ripster_stats.db"),
        ("spotify_token.json",  base / "spotify_token.json"),
        ("cookies.txt",         base / "cookies.txt"),
    ]
    out = []
    for name, p in paths:
        try:
            if p.exists():
                stat = p.stat()
                out.append({
                    "name":    name,
                    "exists":  True,
                    "size":    _fmt_bytes(stat.st_size),
                    "mtime":   datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(timespec="seconds"),
                    "age_min": round((time.time() - stat.st_mtime) / 60, 1),
                })
            else:
                out.append({"name": name, "exists": False})
        except Exception as e:
            out.append({"name": name, "exists": False, "error": str(e)})
    return out


def _queue_info() -> dict:
    """Live queue state."""
    if not _ctx:
        return {}
    queue = _ctx.queue or []
    qm    = _ctx.queue_manager
    by_status = {}
    for t in queue:
        s = (t.get("status") or "queued").lower()
        by_status[s] = by_status.get(s, 0) + 1
    return {
        "total":      len(queue),
        "by_status":  by_status,
        "state":      qm.state.value if qm else "unknown",
        "running":    bool(qm and qm.is_running),
        "paused":     bool(qm and qm.is_paused),
        "active":     len(qm.active_tasks) if qm else 0,
        "runners":    len(qm.runners) if qm else 0,
    }


def _guest_info() -> dict:
    """Guest links + sessions."""
    info = {}
    try:
        from ripster.guest_manager import get_manager
        gm = get_manager()
        try:
            gm.cleanup_expired()
        except Exception:
            pass
        all_links = gm.all_links() if hasattr(gm, "all_links") else []
        info["links_total"]  = len(all_links)
        info["links_active"] = sum(1 for l in all_links if l.get("active"))
        info["sessions"]     = len(getattr(gm, "_sessions", {}))
    except Exception as e:
        info["error"] = str(e)
    return info


def _engine_info() -> dict:
    """Which engines are registered, and their key config flags."""
    try:
        from ripster.engines import REGISTRY
    except Exception as e:
        return {"error": str(e)}

    names = sorted(REGISTRY.keys())
    info  = {"registered": names, "count": len(names), "details": {}}

    if _cfg:
        info["details"]["default"] = _cfg.get("engine", "zhaarey")
        info["details"]["apple_engine"]    = _cfg.get("engine", "zhaarey")
        info["details"]["spotify_engine"]  = _cfg.get("spotify-engine", "zotify")
        info["details"]["beatport_engine"] = "orpheus_beatport"
        info["details"]["amd_dir"]         = _cfg.get("amd-dir", "")
        info["details"]["amd_instance"]    = _cfg.get("amd-instance-url", "")
        info["details"]["zhaarey_wrapper"] = _cfg.get("gamdl-use-wrapper", False)
        info["details"]["zhaarey_wrapper_url"] = _cfg.get("gamdl-wrapper-account-url", "")
        info["details"]["use_go_run"]      = _cfg.get("use-go-run", False)
    return info


def _wrapper_pool_info() -> dict:
    """Apple local-wrapper POOL — how many wrapper containers are busy right now.
    Cheap: reads in-memory slot state via wrapper_pool.live_status(), never
    touches docker and never creates the pool (no side effects on a poll)."""
    info: dict = {}
    try:
        from ripster import wrapper_pool as wp
        cfg = _cfg or {}
        info["enabled"] = bool(wp.pool_enabled(cfg))
        live = wp.live_status()          # None until first Apple task → no side effects
        if live is not None:
            info["live"] = True
            info.update(live)            # size, active_slots, busy, free, slots
            info["cap"] = live["size"]
        else:
            info["live"] = False
            info["busy"] = 0
            info["free"] = 0
            info["active_slots"] = 0
            info["slots"] = []
            # Derive cap from config WITHOUT building the pool / reaper thread.
            try:
                sz = int(cfg.get("apple-pool-size", wp.DEFAULT_POOL_SIZE) or wp.DEFAULT_POOL_SIZE)
            except Exception:
                sz = wp.DEFAULT_POOL_SIZE
            info["cap"] = max(1, min(5, sz)) if info["enabled"] else 1
    except Exception as e:
        info["error"] = str(e)
    return info


def _tunnel_info() -> dict:
    """Serveo + ngrok tunnel state."""
    info = {}
    if _cfg:
        info["remote_enabled"] = bool(_cfg.get("remote-enabled", False))
        info["public_url"]     = _cfg.get("public-url", "")
    try:
        from ripster.routes import guest as g
        info["serveo_running"]    = bool(g._tunnel_proc and g._tunnel_proc.poll() is None)
        info["serveo_connecting"] = g._tunnel_connecting
        info["serveo_url"]        = g._tunnel_url
        info["serveo_log_tail"]   = list(g._tunnel_log)[-5:] if g._tunnel_log else []
    except Exception as e:
        info["serveo_error"] = str(e)
    try:
        from ripster import ngrok_service as ng
        info["ngrok_url"]     = getattr(ng, "_ngrok_url", "") or ""
        info["ngrok_running"] = bool(getattr(ng, "_ngrok_proc", None))
    except Exception:
        pass
    return info


def _tokens_summary() -> dict:
    """Which tokens are present (no values exposed). Detects format issues."""
    if not _cfg:
        return {}
    keys = {
        "apple":      ("media-user-token", "authorization-token"),
        "qobuz":      ("qobuz-user-id", "qobuz-auth-token", "qobuz-app-id"),
        "tidal":      ("tidal-token", "tidal-refresh", "tidal-user-id", "tidal-country"),
        "deezer":     ("deezer-arl",),
        "spotify":    ("spotify-client-id", "spotify-client-secret", "spotify-sp-dc"),
        "soundcloud": ("soundcloud-oauth-token",),
        "beatport":   ("beatport-username", "beatport-password"),
        "yandex":     ("yandex-token",),
    }
    out = {}
    for svc, fields in keys.items():
        present = {}
        for k in fields:
            v = str(_cfg.get(k, "") or "").strip()
            present[k] = {"set": bool(v), "length": len(v)}
        out[svc] = present
    return out


# ── Service probe (parallel) ─────────────────────────────────────────────────

async def _probe_one(svc: str) -> dict:
    """Wrap test_auth so a single service failure does not kill the batch."""
    try:
        from ripster.routes.auth import test_auth
        result = await test_auth(svc)
        # Spotify: the Web API probe can 429 (rate-limit) while native downloads
        # still work via the librespot blob — don't flag a false red.
        if svc == "spotify" and not result.get("ok") and "429" in str(result.get("error", "")):
            try:
                from ripster.engines.orpheus_spotify import is_authenticated as _osp_auth
                if _osp_auth():
                    result = {"ok": True,
                              "note": "Web API rate-limited (429) — нативная загрузка работает"}
            except Exception:
                pass
        return {"service": svc, **result, "checked_at": _utc_iso()}
    except HTTPException as e:
        return {"service": svc, "ok": False, "error": f"HTTP {e.status_code}: {e.detail}",
                "checked_at": _utc_iso()}
    except Exception as e:
        return {"service": svc, "ok": False, "error": f"{type(e).__name__}: {e}",
                "checked_at": _utc_iso()}


# ── Routes ───────────────────────────────────────────────────────────────────

def install(app, ctx) -> None:
    global _cfg, _ctx
    _cfg = ctx.config
    _ctx = ctx
    app.include_router(router)


@router.get("/api/admin/diagnostics")
async def admin_diagnostics(request: Request):
    """Full system snapshot. Cheap — no external API calls. Polled by UI."""
    _require_owner(request)
    return {
        "ts":          _utc_iso(),
        "process":     _process_info(),
        "packages":    _package_versions(),
        "storage":     _storage_info(),
        "files":       _persistence_files(),
        "queue":       _queue_info(),
        "pool":        _wrapper_pool_info(),
        "guest":       _guest_info(),
        "engines":     _engine_info(),
        "tunnel":      _tunnel_info(),
        "tokens":      _tokens_summary(),
        "auth": {
            "enabled":         _is_auth_enabled(),
            "remote_enabled":  bool(_cfg and _cfg.get("remote-enabled", False)),
        },
    }


# ── Dependency management (owner-only) ───────────────────────────────────────
# Packages that are PINNED for a reason — updating them breaks the build/runtime.
# Never touched by "update all"; an individual update requires explicit force.
_PINNED_PKGS = {
    # Hard-pinned — version-locked, break the build/runtime on change.
    "streamrip",      # ==2.0.5 — 2.1.0 has Tidal-client regressions (see VERSIONS.md)
    "protobuf",       # ==6.33.4 — runtime>=gencode for orpheus(6.33)+AMD(6.31)+pywidevine
    "grpcio-tools",   # paired with protobuf / AMD gencode
    "grpcio",
    "construct",      # ==2.8.8 — pinned for WVD/pywidevine
    # DRM / downloader core — version-sensitive (decrypt, tag-parse, stream-parse).
    "pywidevine", "mutagen", "deemix", "deezer-py", "yt-dlp",
    "pycryptodome", "pycryptodomex", "m3u8", "aiodns", "pycares",
    # HTTP stack — http2/GOAWAY tuning + connection behaviour relied on in code.
    "httpx", "httpcore", "h2", "hpack", "aiohttp",
    # Service engines — CLI/API behaviour relied on (a bump can break downloads).
    "gamdl", "tidalapi", "mpegdash", "telethon",
    # Web framework — API-breaking bumps would take the whole app down.
    "fastapi", "starlette", "pydantic", "pydantic-core", "pydantic_core", "uvicorn", "anyio",
    # DB + heavy/native — risky/huge updates (numpy ABI, torch, GUI toolkits).
    "sqlalchemy", "numpy", "torch", "torchvision", "matplotlib",
    "pyqt6", "pyqt6-qt6", "pyqt6-sip", "pyside6", "pyside6_addons", "pyside6_essentials",
    "shiboken6", "psycopg2-binary", "psycopg2",
    # Build / browser-automation — keep stable (launcher build + selenium stack).
    "pyinstaller", "pyinstaller-hooks-contrib", "selenium", "webdriver-manager", "seleniumbase",
}
_PKG_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _pip_outdated() -> list[dict]:
    import json as _json
    import subprocess
    out = subprocess.run([sys.executable, "-m", "pip", "list", "--outdated", "--format=json"],
                         capture_output=True, text=True, timeout=90,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    data = _json.loads(out.stdout or "[]")
    return [{"name": p.get("name", ""), "version": p.get("version", ""),
             "latest": p.get("latest_version", ""),
             "pinned": p.get("name", "").lower() in _PINNED_PKGS} for p in data]


@router.get("/api/admin/deps")
async def deps_list(request: Request):
    """Что можно обновить БЕЗ риска. Остальное сюда не попадает вовсе.

    Раньше закреплённые пакеты показывались в общем списке — с пометкой, но с
    рабочей кнопкой «всё равно обновить». А обновление именно этих пакетов и
    ломает сборку целиком: у них взаимоисключающие требования (pymp4 держит
    construct==2.8.8, keydive хочет >=2.10.70; OrpheusDL тянет protobuf 3.15.8,
    что ниже нужного AMD и pywidevine). Один клик — и не работает ничего, причём
    выясняется это не сразу.

    Поэтому список теперь только из безопасного, а про остальные честно
    сообщается: они внутри есть, но обновление у них убрано. Осознанное
    обновление никуда не делось — оно делается руками через pip, где видно, что
    именно ты меняешь.
    """
    _require_owner(request)
    try:
        pkgs = await asyncio.to_thread(_pip_outdated)
        safe   = [p for p in pkgs if not p["pinned"]]
        locked = [p for p in pkgs if p["pinned"]]
        return {"packages": safe,
                "locked_count": len(locked),
                "locked_names": sorted(p["name"] for p in locked),
                "locked_note": (
                    "Внутри есть и другие зависимости, но их обновление отсюда "
                    "убрано: у них взаимоисключающие требования к версиям, и "
                    "апгрейд ломает загрузки целиком — decrypt, теги, разбор "
                    "потоков. Обновлять их можно только осознанно и вручную."),
                "pinned": sorted(_PINNED_PKGS)}
    except Exception as e:
        raise HTTPException(500, f"pip list failed: {e}")


@router.post("/api/admin/deps/update")
async def deps_update(body: dict, request: Request):
    """Update one package (`package`) or all non-pinned (`package`:"all").
    Pinned packages need `force:true`. Runs `pip install -U` in the app's env."""
    _require_owner(request)
    import subprocess
    pkg   = (body.get("package") or "").strip()
    force = bool(body.get("force"))
    if not pkg:
        raise HTTPException(400, "package required")

    if pkg == "all":
        try:
            targets = [p["name"] for p in await asyncio.to_thread(_pip_outdated)
                       if p["name"].lower() not in _PINNED_PKGS]
        except Exception as e:
            raise HTTPException(500, f"pip list failed: {e}")
        if not targets:
            return {"ok": True, "updated": [], "msg": "Нечего обновлять (или всё закреплено)."}
    else:
        if not _PKG_RE.match(pkg):
            raise HTTPException(400, "bad package name")
        if pkg.lower() in _PINNED_PKGS:
            # Раньше это обходилось флагом force прямо из интерфейса. Обход убран:
            # ровно эти пакеты и ломают сборку целиком, а кнопка рядом со списком
            # выглядит как обычное обновление. Кому действительно нужно — делает
            # это руками через pip, видя, что меняет.
            return {"ok": False, "pinned": True,
                    "msg": f"{pkg} обновлять отсюда нельзя: у него жёстко "
                           f"связанная версия, апгрейд ломает загрузки. "
                           f"Только вручную через pip, осознанно."}
        targets = [pkg]

    def _do() -> dict:
        results = []
        for name in targets:
            if not _PKG_RE.match(name):
                results.append({"name": name, "ok": False, "out": "bad name"})
                continue
            r = subprocess.run([sys.executable, "-m", "pip", "install", "-U", name],
                               capture_output=True, text=True, timeout=600,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            results.append({"name": name, "ok": r.returncode == 0,
                            "out": (r.stdout + r.stderr)[-400:]})
        return {"ok": all(x["ok"] for x in results), "updated": results}

    res = await asyncio.to_thread(_do)
    res["restart_required"] = True
    return res


def _is_auth_enabled() -> bool:
    try:
        from ripster.auth import is_enabled
        return is_enabled()
    except Exception:
        return False


def _bot_overview() -> dict:
    """Telegram-bot view: registered users + their downloads (tagged meta.tg_user)."""
    import json as _json

    base = _ctx.base_dir if _ctx and _ctx.base_dir else Path.cwd()
    users_file = base / "tgbot" / "users.json"
    try:
        raw = _json.loads(users_file.read_text(encoding="utf-8"))
    except Exception:
        raw = {}

    # Gather every download that carries a tg_user tag (live queue + history).
    by_user: dict[str, list] = {}
    seen: set = set()

    def _collect(task: dict):
        meta = task.get("meta") or {}
        tg = meta.get("tg_user")
        if tg is None:
            return
        tid = task.get("id")
        if tid in seen:
            return
        seen.add(tid)
        by_user.setdefault(str(tg), []).append({
            "id":       tid,
            "title":    meta.get("title") or meta.get("album") or task.get("url", "")[:60],
            "artist":   meta.get("artist") or "",
            "service":  task.get("service") or meta.get("service") or "",
            "quality":  task.get("quality") or "",
            "status":   task.get("status") or "",
            "progress": task.get("progress") or 0,
            "added":    task.get("added") or "",
        })

    for tsk in (_ctx.queue or []):
        _collect(tsk)
    for tsk in (_ctx.download_history or []):
        _collect(tsk)

    users_out = []
    for uid, info in raw.items():
        dls = by_user.get(str(uid), [])
        active = sum(1 for d in dls if d["status"] in ("queued", "running"))
        done = sum(1 for d in dls if d["status"] == "done")
        failed = sum(1 for d in dls if d["status"] in ("error", "cancelled"))
        users_out.append({
            "id":        uid,
            "name":      info.get("name", ""),
            "username":  info.get("username", ""),
            "status":    info.get("status", ""),
            "lang":      info.get("lang", ""),
            "owner":     bool(info.get("owner")),
            "ts":        info.get("ts", 0),
            "left_ts":   info.get("left_ts", 0),
            "left_reason": info.get("left_reason", ""),
            "quota":     info.get("quota") or {},
            "counts":    {"total": len(dls), "active": active, "done": done, "failed": failed},
            "downloads": sorted(dls, key=lambda d: d["status"] != "running")[:25],
        })
    # owners first, then by recent activity
    users_out.sort(key=lambda u: (not u["owner"], -u["counts"]["active"], -u["ts"]))

    status_counts: dict = {}
    for u in users_out:
        status_counts[u["status"]] = status_counts.get(u["status"], 0) + 1

    return {
        "ts":            _utc_iso(),
        "bot_online":    _bot_process_online(),
        "total_users":   len(users_out),
        "status_counts": status_counts,
        "users":         users_out,
    }


def _bot_process_online() -> bool:
    try:
        import psutil
        for p in psutil.process_iter(["cmdline"]):
            cl = p.info.get("cmdline") or []
            norm = [str(c).replace("\\", "/") for c in cl]
            if any(c.endswith("bot.py") for c in norm) and not any(c.endswith("app.py") for c in norm):
                return True
    except Exception:
        pass
    return False


@router.get("/api/admin/bot-overview")
async def admin_bot_overview(request: Request):
    """Telegram-bot dashboard: users, their access status, and per-user downloads."""
    _require_owner(request)
    return _bot_overview()


@router.post("/api/admin/bot-user")
async def admin_bot_user(body: dict, request: Request):
    """Owner-side moderation of Telegram-bot users from the web admin.
    Writes tgbot/users.json; the running bot picks the change up via its
    file-mtime sync. action ∈ approve | ban | decline | unban."""
    import json as _json
    _require_owner(request)
    uid = str(body.get("uid") or "").strip()
    action = (body.get("action") or "").strip().lower()
    if not uid or action not in ("approve", "ban", "decline", "unban", "preadd"):
        raise HTTPException(400, "uid and valid action required")

    base = _ctx.base_dir if _ctx and _ctx.base_dir else Path.cwd()
    users_file = base / "tgbot" / "users.json"
    try:
        data = _json.loads(users_file.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}

    # Pre-add by numeric id (active now) or @username (claimed on first /start).
    if action == "preadd":
        ident = uid.lstrip("@")
        if uid.lstrip("-").isdigit():
            key = uid
            data[key] = {**(data.get(key) or {}), "status": "approved",
                         "pre": True, "ts": time.time()}
        else:
            key = "@" + ident.lower()
            data[key] = {"status": "approved", "username": ident.lower(),
                         "pre": True, "ts": time.time()}
        try:
            tmp = users_file.with_suffix(".tmp")
            tmp.write_text(_json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, users_file)
        except Exception as e:
            raise HTTPException(500, f"write failed: {e}")
        return {"ok": True, "uid": key, "status": "approved", "preadded": True}

    u = data.get(uid) or {}
    if u.get("owner"):
        raise HTTPException(400, "cannot modify the owner")
    new_status = {"approve": "approved", "ban": "banned",
                  "decline": "declined", "unban": "approved"}[action]
    u["status"] = new_status
    u["ts_decided"] = time.time()
    # Stamp/clear the "left/blocked" mark so the bot panel can show banned users
    # in the inactive column; re-approving / unbanning brings them back active.
    if action == "ban":
        u["left_ts"] = time.time()
        u["left_reason"] = "banned_by_owner"
    elif action in ("approve", "unban"):
        u.pop("left_ts", None)
        u.pop("left_reason", None)
    data[uid] = u
    try:
        tmp = users_file.with_suffix(".tmp")
        tmp.write_text(_json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, users_file)
    except Exception as e:
        raise HTTPException(500, f"write failed: {e}")
    return {"ok": True, "uid": uid, "status": new_status}


# ── per-user detail / audit timeline / web→bot outbox ────────────────────────
def _tgbot_dir() -> Path:
    base = _ctx.base_dir if _ctx and _ctx.base_dir else Path.cwd()
    return base / "tgbot"


def _orpheus_config_dir() -> Path:
    base = _ctx.base_dir if _ctx and _ctx.base_dir else Path.cwd()
    return base / "orpheus" / "config"


def _read_audit(chat_id: int | None, limit: int = 400) -> list:
    """Read the bot's append-only audit log (newest first), optionally filtered
    to one user. The log is never pruned — full who/what/when history."""
    f = _tgbot_dir() / "audit.jsonl"
    out: list = []
    try:
        import json as _json
        lines = f.read_text(encoding="utf-8").splitlines()
        for ln in reversed(lines):
            ln = ln.strip()
            if not ln:
                continue
            try:
                rec = _json.loads(ln)
            except Exception:
                continue
            if chat_id is not None and int(rec.get("chat_id", 0)) != int(chat_id):
                continue
            out.append(rec)
            if len(out) >= limit:
                break
    except Exception:
        pass
    return out


def _append_outbox(cmd: dict) -> None:
    import json as _json
    p = _tgbot_dir() / "outbox.json"
    try:
        cur = _json.loads(p.read_text(encoding="utf-8")) if p.exists() else []
        if not isinstance(cur, list):
            cur = []
    except Exception:
        cur = []
    cur.append(cmd)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(_json.dumps(cur, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)


@router.get("/api/admin/bot-user-detail")
async def admin_bot_user_detail(request: Request, uid: str):
    """Full per-user view for the admin window: profile, ALL their downloads
    (live queue + history, uncapped), and the persistent audit timeline."""
    _require_owner(request)
    import json as _json
    try:
        users = _json.loads((_tgbot_dir() / "users.json").read_text(encoding="utf-8"))
    except Exception:
        users = {}
    info = users.get(uid) or users.get(str(uid)) or {}

    dls: list = []
    seen: set = set()

    def _collect(task: dict):
        meta = task.get("meta") or {}
        if str(meta.get("tg_user")) != str(uid):
            return
        tid = task.get("id")
        if tid in seen:
            return
        seen.add(tid)
        dls.append({
            "id": tid,
            "title": meta.get("title") or meta.get("album") or task.get("url", "")[:70],
            "artist": meta.get("artist") or "",
            "service": task.get("service") or meta.get("service") or "",
            "quality": task.get("quality") or "",
            "status": task.get("status") or "",
            "progress": task.get("progress") or 0,
            "added": task.get("added") or "",
            "url": task.get("url") or "",
        })

    for tsk in (_ctx.queue or []):
        _collect(tsk)
    for tsk in (_ctx.download_history or []):
        _collect(tsk)

    return {
        "uid": uid,
        "info": {"name": info.get("name", ""), "username": info.get("username", ""),
                 "status": info.get("status", ""), "lang": info.get("lang", ""),
                 "owner": bool(info.get("owner")), "ts": info.get("ts", 0)},
        "downloads": dls,
        "quota": info.get("quota") or {},
        "audit": _read_audit(int(uid) if str(uid).lstrip("-").isdigit() else None, 400),
    }


@router.post("/api/admin/bot-broadcast")
async def admin_bot_broadcast(body: dict, request: Request):
    """Queue a text message to one or many bot users (delivered by the bot via
    the outbox bridge). body: {uids:[...], text:"..."}."""
    _require_owner(request)
    text = (body.get("text") or "").strip()
    uids = [str(x) for x in (body.get("uids") or []) if str(x).lstrip("-").isdigit()]
    if not text or not uids:
        raise HTTPException(400, "text and uids required")
    _append_outbox({"type": "message", "chat_ids": uids, "text": text})
    return {"ok": True, "queued": len(uids)}


@router.post("/api/admin/bot-enqueue")
async def admin_bot_enqueue(body: dict, request: Request):
    """Create a download FOR a user (admin gift): the bot queues it and delivers
    to that user's chat. body: {uid, url, quality?}."""
    _require_owner(request)
    uid = str(body.get("uid") or "").strip()
    url = (body.get("url") or "").strip()
    quality = (body.get("quality") or "").strip()
    if not uid.lstrip("-").isdigit() or not url.startswith("http"):
        raise HTTPException(400, "valid uid and url required")
    _append_outbox({"type": "download", "uid": int(uid), "url": url,
                    "quality": quality or None})
    return {"ok": True}


@router.get("/api/admin/bot-cache")
async def admin_bot_cache_get(request: Request):
    """Whether the owner's own bot downloads are mirrored to the cache channel
    (the bot reads tgbot/config.json fresh, so this is the live state)."""
    _require_owner(request)
    import json as _json
    try:
        c = _json.loads((_tgbot_dir() / "config.json").read_text(encoding="utf-8"))
    except Exception:
        c = {}
    return {"enabled": bool(c.get("cache_owner_enabled", False)),
            "channel": bool(c.get("cache_channel_id"))}


@router.post("/api/admin/bot-cache")
async def admin_bot_cache_set(body: dict, request: Request):
    """Toggle 'send my downloads to the cache channel'. Shared live with the
    bot's /cache command (both read/write tgbot/config.json)."""
    _require_owner(request)
    import json as _json
    p = _tgbot_dir() / "config.json"
    try:
        c = _json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        c = {}
    c["cache_owner_enabled"] = bool(body.get("enabled"))
    tmp = p.with_suffix(".tmp")
    tmp.write_text(_json.dumps(c, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)
    return {"ok": True, "enabled": c["cache_owner_enabled"]}


# ── Bot configuration tab (de-hardwire token/owner/channel — task #23) ───────
# Keys the owner may view/edit from the web "Бот" tab. Secrets are never sent
# back to the client and a blank value never wipes a stored secret.
_BOT_CFG_KEYS = [
    "bot_token", "owner_id", "backend_url", "config_yaml", "local_bot_api",
    "api_id", "api_hash", "max_upload_mb", "default_quality",
    "cache_channel_id", "cache_channel_link",
]
_BOT_CFG_SECRET = {"bot_token", "api_hash"}
_BOT_CFG_INT    = {"owner_id", "api_id", "max_upload_mb", "cache_channel_id"}
# Changing any of these needs a bot restart to take effect (read once at startup).
_BOT_CFG_RESTART = {"bot_token", "owner_id", "api_id", "api_hash",
                    "local_bot_api", "backend_url", "config_yaml"}


@router.get("/api/admin/bot-config")
async def admin_bot_config_get(request: Request):
    """Read tgbot/config.json for the Бот settings tab. Secrets (bot_token,
    api_hash) are redacted — only a boolean `<key>_set` is returned."""
    _require_owner(request)
    import json as _json
    try:
        c = _json.loads((_tgbot_dir() / "config.json").read_text(encoding="utf-8"))
    except Exception:
        c = {}
    out: dict = {}
    for k in _BOT_CFG_KEYS:
        if k in _BOT_CFG_SECRET:
            out[k] = ""
            out[k + "_set"] = bool(c.get(k))
        else:
            out[k] = c.get(k)
    return out


@router.post("/api/admin/bot-config")
async def admin_bot_config_set(body: dict, request: Request):
    """Write whitelisted bot keys to tgbot/config.json. A blank secret is
    ignored (won't erase a stored token). Returns which keys changed and whether
    a bot restart is required."""
    _require_owner(request)
    import json as _json
    p = _tgbot_dir() / "config.json"
    try:
        c = _json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        c = {}
    if not isinstance(c, dict):
        c = {}
    changed: list[str] = []
    for k in _BOT_CFG_KEYS:
        if k not in body:
            continue
        v = body[k]
        sv = "" if v is None else str(v).strip()
        if k in _BOT_CFG_SECRET and not sv:
            continue  # never wipe a stored secret with a blank field
        if k in _BOT_CFG_INT and sv:
            try:
                v = int(sv)
            except ValueError:
                raise HTTPException(400, f"{k} must be an integer")
        elif k == "local_bot_api":
            v = sv or None      # empty → use Telegram cloud API
        else:
            v = sv if isinstance(v, str) else v
        if c.get(k) != v:
            c[k] = v
            changed.append(k)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(_json.dumps(c, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)
    return {"ok": True, "changed": changed,
            "restart_required": any(k in _BOT_CFG_RESTART for k in changed)}


@router.post("/api/admin/spotify-token")
async def admin_spotify_token(body: dict, request: Request):
    """Refresh the Spotify web tokens for native OGG downloads. Accepts either the
    raw DevTools headers blob (`blob`) or explicit `bearer`/`client_token`. Writes
    orpheus/config/spotify-token.txt + spotify-client-token.txt. The Bearer lives
    ~1h (re-paste when native 401s); client-token lasts weeks."""
    _require_owner(request)
    import re as _re
    blob = (body.get("blob") or "").strip()
    bearer = (body.get("bearer") or "").strip()
    ctoken = (body.get("client_token") or body.get("client-token") or "").strip()
    if blob:
        m = (_re.search(r'Bearer\s+([A-Za-z0-9_\-]{30,})', blob, _re.I)
             or _re.search(r'\b(BQ[A-Za-z0-9_\-]{50,})\b', blob))
        if m and not bearer:
            bearer = m.group(1)
        m = (_re.search(r'client-token["\']?\s*:?\s*([A-Za-z0-9+/=_\-]{50,})', blob, _re.I)
             or _re.search(r'\b(AA[A-Za-z0-9+/=]{60,})', blob))
        if m and not ctoken:
            ctoken = m.group(1)
    # also strip a leading "Bearer " if the user pasted it into the bearer field
    bearer = _re.sub(r'^\s*Bearer\s+', '', bearer, flags=_re.I).strip()
    cfg_dir = _orpheus_config_dir()
    updated = []
    try:
        cfg_dir.mkdir(parents=True, exist_ok=True)
        if bearer:
            (cfg_dir / "spotify-token.txt").write_text(bearer, encoding="utf-8")
            updated.append("bearer")
        if ctoken:
            (cfg_dir / "spotify-client-token.txt").write_text(ctoken, encoding="utf-8")
            updated.append("client_token")
    except Exception as e:
        raise HTTPException(500, f"write failed: {e}")
    if not updated:
        raise HTTPException(400, "no Bearer/client-token found in input")
    return {"ok": True, "updated": updated}


def _spotify_push_secret() -> str:
    """Shared secret for the browser-extension token push. Auto-created in config
    on first use. The extension sends it so the (cookie-less, cross-origin) push
    endpoint can authenticate without the owner session."""
    import secrets as _secrets
    s = (_cfg.get("spotify-push-secret") or "").strip() if _cfg else ""
    if not s:
        s = _secrets.token_urlsafe(24)
        try:
            if _cfg is not None:
                _cfg["spotify-push-secret"] = s
                if _ctx and getattr(_ctx, "save_config", None):
                    _ctx.save_config(_cfg)
        except Exception:
            pass
    return s


@router.get("/api/admin/spotify-push-secret")
async def admin_spotify_push_secret(request: Request):
    """Owner-only: reveal the push secret to paste into the browser extension."""
    _require_owner(request)
    return {"secret": _spotify_push_secret()}


@router.options("/api/spotify-token-push")
async def spotify_token_push_preflight():
    from fastapi import Response
    return Response(status_code=204, headers={
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
    })


_sp_push_log: list = []   # recent token pushes from the browser extension


@router.get("/api/admin/spotify-token-status")
async def spotify_token_status(request: Request):
    """Owner-only: token freshness + recent extension-push log for the Spotify tab."""
    _require_owner(request)
    import time as _t
    d = _orpheus_config_dir()
    def _age(name):
        p = d / name
        try:
            return round((_t.time() - p.stat().st_mtime) / 60, 1) if p.exists() else None
        except Exception:
            return None
    tok_age = _age("spotify-token.txt")
    return {
        "bearer_age_min": tok_age,
        "client_token_age_min": _age("spotify-client-token.txt"),
        "fresh": (tok_age is not None and tok_age < 55),     # Bearer ~1h
        "log": list(reversed(_sp_push_log[-25:])),           # newest first
    }


def _jwt_exp(token: str):
    """Return the `exp` (unix seconds) from a JWT access token, or None if the
    token isn't a JWT / has no exp claim. Tidal access tokens are ES256 JWTs."""
    import base64 as _b64, json as _json
    try:
        parts = (token or "").split(".")
        if len(parts) < 2:
            return None
        payload = parts[1] + "=" * (-len(parts[1]) % 4)          # pad to b64
        exp = _json.loads(_b64.urlsafe_b64decode(payload)).get("exp")
        return int(exp) if exp else None
    except Exception:
        return None


@router.get("/api/admin/token-expiry")
async def token_expiry(request: Request):
    """Owner-only: per-service token expiry (days left) for the settings tabs.
    Computed from the real token, not the configured `*-token-expiry` field
    (which may be padded to bypass streamrip's pre-emptive refresh). Currently
    Tidal (JWT `exp`); other services slot in here as their token shape allows."""
    _require_owner(request)
    import time as _t
    cfg = _cfg or {}
    out: dict = {}

    def _from_jwt(svc: str, token: str):
        exp = _jwt_exp(token)
        if not exp:
            return
        days = (exp - _t.time()) / 86400
        out[svc] = {
            "expires_at": exp,
            "days_left":  round(days, 1),
            "hours_left": round(days * 24, 1),
            "valid":      days > 0,
        }

    # Tidal — prefer the self-refreshing device-flow (TV) session. When it exists,
    # OrpheusDL mints fresh access tokens from its long-lived refresh_token on its
    # own, so the manually pasted JWT's expiry is irrelevant — reporting it as
    # "expired" was the false "🔴 токен истёк" shown while the account was clearly
    # logged in. Only fall back to the manual JWT when there's NO device-flow session.
    try:
        from ripster.engines.tidal import _read_tv_session
        if _read_tv_session():
            out["tidal"] = {"session": "device-flow", "valid": True,
                            "days_left": 9999, "hours_left": 9999 * 24, "expires_at": 0}
    except Exception:
        pass

    if "tidal" not in out:
        # No device-flow session → fall back to the manually pasted JWT.
        tidal_tok = str(cfg.get("tidal-token") or "").strip()
        if not tidal_tok:
            try:
                import yaml as _yaml
                p = Path(__file__).resolve().parents[2] / "tokens" / "tidal.yaml"
                if p.exists():
                    tidal_tok = str((_yaml.safe_load(p.read_text("utf-8")) or {}).get("tidal-token") or "").strip()
            except Exception:
                pass
        _from_jwt("tidal", tidal_tok)

    return out


@router.post("/api/spotify-token-push")
async def spotify_token_push(body: dict):
    """Cookie-less token ingest for the browser extension (zero-touch refresh).
    Auth via the shared `secret`, NOT the owner session, so it works from the
    open.spotify.com origin. Writes the Spotify web token files."""
    from fastapi import Response
    import json as _json, time as _t
    cors = {"Access-Control-Allow-Origin": "*"}
    def _rec(status, detail=""):
        _sp_push_log.append({"ts": _t.time(),
                             "time": _t.strftime("%H:%M:%S", _t.localtime()),
                             "status": status, "detail": detail})
        del _sp_push_log[:-50]
    if (body.get("secret") or "") != _spotify_push_secret():
        _rec("✗ bad secret")
        return Response(content=_json.dumps({"ok": False, "error": "bad secret"}),
                        status_code=403, media_type="application/json", headers=cors)
    import re as _re
    bearer = _re.sub(r'^\s*Bearer\s+', '', (body.get("bearer") or "").strip(), flags=_re.I).strip()
    ctoken = (body.get("client_token") or "").strip()
    cfg_dir = _orpheus_config_dir()
    updated = []
    try:
        cfg_dir.mkdir(parents=True, exist_ok=True)
        if bearer:
            (cfg_dir / "spotify-token.txt").write_text(bearer, encoding="utf-8"); updated.append("bearer")
        if ctoken:
            (cfg_dir / "spotify-client-token.txt").write_text(ctoken, encoding="utf-8"); updated.append("client_token")
    except Exception as e:
        _rec("✗ ошибка записи", str(e)[:60])
        return Response(content=_json.dumps({"ok": False, "error": str(e)}),
                        status_code=500, media_type="application/json", headers=cors)
    _rec("✓ обновлён" if updated else "— пусто", ", ".join(updated))
    return Response(content=_json.dumps({"ok": True, "updated": updated}),
                    status_code=200, media_type="application/json", headers=cors)


@router.post("/api/admin/bot-quota")
async def admin_bot_quota(body: dict, request: Request):
    """Set/clear a bot user's download quota (the bot reads users.json fresh, so
    it applies live). body: {uid, quota:{until?,count?,services?,allowed?} | null}.
    Accumulated usage (used / used_services) is preserved across edits."""
    _require_owner(request)
    import json as _json
    uid = str(body.get("uid") or "").strip()
    if not uid.lstrip("-").isdigit():
        raise HTTPException(400, "valid uid required")
    quota = body.get("quota")
    users_file = _tgbot_dir() / "users.json"
    try:
        data = _json.loads(users_file.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    u = data.get(uid) or {}
    if u.get("owner"):
        raise HTTPException(400, "owner has no quota")
    if not quota:
        u.pop("quota", None)
    else:
        old = u.get("quota") or {}
        q: dict = {}
        if quota.get("until"):
            q["until"] = float(quota["until"])
        if quota.get("count"):
            q["count"] = int(quota["count"])
        svcs = quota.get("services") or {}
        if isinstance(svcs, dict):
            q["services"] = {k: int(v) for k, v in svcs.items() if v}
        allowed = quota.get("allowed") or []
        if isinstance(allowed, list) and allowed:
            q["allowed"] = list(allowed)
        q["used"] = old.get("used", 0)
        q["used_services"] = old.get("used_services", {})
        u["quota"] = q
    data[uid] = u
    tmp = users_file.with_suffix(".tmp")
    tmp.write_text(_json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, users_file)
    return {"ok": True, "quota": u.get("quota")}


def _bot_pids() -> list[int]:
    """PIDs of running tgbot/bot.py processes (app.py is excluded)."""
    pids: list[int] = []
    try:
        import psutil
        for p in psutil.process_iter(["pid", "cmdline"]):
            cl = p.info.get("cmdline") or []
            norm = [str(c).replace("\\", "/") for c in cl]
            if any(c.endswith("bot.py") for c in norm) and not any(c.endswith("app.py") for c in norm):
                pids.append(p.info["pid"])
    except Exception:
        pass
    return pids


def _bot_python() -> str:
    """Interpreter that has aiogram for the bot. Owner's machine keeps it under
    C:\\Python314 (config `bot-python` overrides); fall back to this server's
    interpreter only if that path is gone."""
    cand = ((_cfg.get("bot-python") if _cfg else "") or r"C:\Python314\python.exe").strip()
    try:
        if cand and Path(cand).exists():
            return cand
    except Exception:
        pass
    return sys.executable


@router.post("/api/admin/bot-control")
async def admin_bot_control(body: dict, request: Request):
    """Start / stop / restart the Telegram bot (tgbot/bot.py) from the web.
    The bot is a SEPARATE process from app.py, so the owner can manage it in the
    🦝 Бот tab instead of needing an SSH/terminal. Only ONE bot may poll Telegram
    at a time, so `start` is a no-op when already online and `restart` stops the
    old process before spawning a fresh one."""
    _require_owner(request)
    action = (body.get("action") or "").strip().lower()
    if action not in ("start", "stop", "restart"):
        raise HTTPException(400, "action ∈ start|stop|restart")

    base = _ctx.base_dir if _ctx and _ctx.base_dir else Path.cwd()
    bot_dir = base / "tgbot"
    bot_py  = bot_dir / "bot.py"

    def _kill_pids(pids: list[int]) -> int:
        """Terminate (then hard-kill) the given PIDs. Returns how many were hit."""
        try:
            import psutil
        except Exception:
            return 0
        procs = []
        for pid in pids:
            try:
                procs.append(psutil.Process(pid))
            except Exception:
                pass
        for p in procs:
            try:
                p.terminate()
            except Exception:
                pass
        _gone, alive = psutil.wait_procs(procs, timeout=4)
        for p in alive:
            try:
                p.kill()
            except Exception:
                pass
        return len(procs)

    def _start() -> int | None:
        """Spawn a detached bot.py; return its PID (or None on failure)."""
        if not bot_py.exists():
            return None
        import subprocess
        flags = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP) \
                if os.name == "nt" else 0
        try:
            return subprocess.Popen([_bot_python(), "bot.py"],
                                    cwd=str(bot_dir), creationflags=flags).pid
        except Exception:
            return None

    # `start` on an already-running bot is a no-op — a 2nd poller would trigger
    # Telegram's "Conflict: terminated by other getUpdates" and break both.
    if action == "start" and _bot_process_online():
        return {"ok": True, "action": action, "online": True, "pids": _bot_pids(),
                "note": "already online"}

    if action in ("stop", "restart"):
        await asyncio.to_thread(_kill_pids, _bot_pids())

    new_pid: int | None = None
    if action in ("start", "restart"):
        if action == "restart":
            await asyncio.sleep(1.2)   # let the old Telegram long-poll session free up
        new_pid = await asyncio.to_thread(_start)
        if not new_pid:
            raise HTTPException(500, f"failed to start bot ({bot_py})")
        # Guarantee a SINGLE instance: reap any other bot.py (stray manual start
        # or a stop() that raced an empty psutil snapshot). Never touch new_pid.
        await asyncio.sleep(0.8)
        for _ in range(2):
            others = [pid for pid in _bot_pids() if pid != new_pid]
            if not others:
                break
            await asyncio.to_thread(_kill_pids, others)
            await asyncio.sleep(0.6)

    await asyncio.sleep(1.2)            # let the new process register / old one die
    return {"ok": True, "action": action, "online": _bot_process_online(), "pids": _bot_pids()}


@router.post("/api/admin/probe-all")
async def admin_probe_all(request: Request):
    """Probe every service in parallel — slow (network), so explicit POST."""
    _require_owner(request)
    services = ["apple", "qobuz", "tidal", "deezer", "spotify", "soundcloud", "beatport", "yandex", "amazon"]
    results = await asyncio.gather(*[_probe_one(s) for s in services])
    summary = {
        "ok_count":   sum(1 for r in results if r.get("ok")),
        "fail_count": sum(1 for r in results if not r.get("ok")),
        "ts":         _utc_iso(),
    }
    return {"summary": summary, "services": results}


@router.get("/api/admin/probe/{service}")
async def admin_probe_one(service: str, request: Request):
    """Probe a single service — used for retry buttons in the UI."""
    _require_owner(request)
    return await _probe_one(service.lower())


@router.get("/api/admin/system-log")
async def admin_system_log(
    request: Request,
    tail: int = 300,
    service: str = "",
    level: str = "",
):
    """Recent server-side stdout lines from the in-process ring buffer.
    The same lines the Console view shows — but here we also include lines
    that have no `task_id` (system events, startup, errors). Owner sees ALL,
    not just download events."""
    _require_owner(request)
    # Persistent buffer (3-tuple: ts, service, text) lives on the top-level
    # `app.py` (run as __main__ when started via `python app.py`). Doing
    # `import app` would re-execute the script and bind to a *different* module
    # with its own empty deque — the symptom we hit on the first revision.
    # Look it up through sys.modules instead.
    _LOG_HISTORY = None
    for mod_name in ("__main__", "app"):
        mod = sys.modules.get(mod_name)
        if mod and hasattr(mod, "_LOG_HISTORY"):
            _LOG_HISTORY = getattr(mod, "_LOG_HISTORY")
            break
    if _LOG_HISTORY is None:
        return {"ok": False, "error": "_LOG_HISTORY not available", "lines": []}

    items = list(_LOG_HISTORY)   # each: (ts, svc, text)
    if service:
        svc = service.lower()
        items = [x for x in items if x[1] == svc]
    if level:
        lvl = level.lower()
        import re as _re_lvl
        # 4xx/5xx codes appear in logs like "→ 401:", "→ 403 for", "HTTP 502"
        _re_http_err = _re_lvl.compile(r"(?:HTTP\s*|→\s*|->\s*|status[:=]?\s*)[45]\d\d\b")
        def _lvl_of(text: str) -> str:
            tl = text.lower()
            if ("error" in tl or "✗" in text or "traceback" in tl
                    or "exception" in tl or "crashed" in tl
                    or "fatal" in tl or _re_http_err.search(text)):
                return "error"
            if ("warn" in tl or "⚠" in text or "fail" in tl
                    or "expired" in tl or "invalid" in tl
                    or "rejected" in tl or "timeout" in tl):
                return "warn"
            return "info"
        order = {"error": 3, "warn": 2, "info": 1}
        need  = order.get(lvl, 1)
        items = [x for x in items if order.get(_lvl_of(x[2]), 1) >= need]

    items = items[-max(1, min(tail, 4000)):]
    return {
        "ok":    True,
        "count": len(items),
        "lines": [
            {"ts": ts, "service": svc, "text": text}
            for ts, svc, text in items
        ],
    }


@router.get("/api/admin/modules")
async def admin_modules(request: Request):
    """List of all loaded route modules and their endpoints. Useful for
    spotting accidentally-disabled features."""
    _require_owner(request)
    from fastapi.routing import APIRoute
    routes = []
    for r in request.app.router.routes:
        if isinstance(r, APIRoute):
            routes.append({
                "path":    r.path,
                "methods": sorted(r.methods - {"HEAD", "OPTIONS"}),
                "name":    r.name,
            })
    routes.sort(key=lambda x: x["path"])
    return {"count": len(routes), "routes": routes}


@router.post("/api/admin/clear-caches")
async def admin_clear_caches(request: Request):
    """Drop in-process caches that can mask issues (token caches, etc).
    Persistent state (config, tokens.yaml) is never touched."""
    _require_owner(request)
    cleared = []
    try:
        from ripster.routes import spotify as sp
        if hasattr(sp, "_sp_releases_cache"):
            sp._sp_releases_cache.clear()
            cleared.append("spotify.releases_cache")
    except Exception:
        pass
    try:
        from ripster.routes import soundcloud as sc
        if hasattr(sc, "_sc_client_id"):
            sc._sc_client_id = ""
            cleared.append("soundcloud.client_id")
    except Exception:
        pass
    return {"ok": True, "cleared": cleared}
