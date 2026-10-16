"""Живая проверка «что реально работает» для отчёта о неполадке (ПК).

Владелец 11.10.2026: тестера нельзя просить по каждому пункту «есть ли сервис в поиске, ищет ли, качает ли»:
отчёт должен отвечать сам и так, чтобы проблему было видно за секунды. Поэтому в архиве отчёта первым лежит
`_00_health_report.txt`: сверху итог `PROBLEMS`, ниже — по каждому сервису результат ПРОБНОГО поиска и карточки
альбома, состояние Премиума, найденные программы, свободное место, доступность сервисов из сети человека
(гео-блоки и обрывы видны сразу), очередь и итог проверки установщика.

Только факты и причины. Значений секретов здесь нет; общая вычистка в diagnostics.build_bundle идёт поверх всего текста.
Модуль никогда не бросает: «не смогли спросить» — это содержимое строки, а не сбой отчёта.
"""
from __future__ import annotations

import asyncio
import os
import platform
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

QUERY = "daft punk"
SEARCH_TIMEOUT_S = 25
CONCURRENCY = 3                 # параллельных проверок сервисов: пачка из десяти разом давала ложные таймауты
TOTAL_TIMEOUT_S = 75

# Сервисы с поиском в приложении и тип проверки карточки альбома (None — карточки нет/не проверяем).
_SERVICES = ("deezer", "qobuz", "tidal", "yandex", "soundcloud", "spotify", "apple", "bbc", "jiosaavn", "beatport")

# (метка, адрес) — достаточно, чтобы увидеть блок/обрыв именно из сети тестера
_HOSTS = [
    ("deezer", "https://api.deezer.com/infos"),
    ("qobuz", "https://www.qobuz.com/api.json/0.2/status"),
    ("tidal", "https://api.tidal.com/v1/ping"),
    ("tidal-auth", "https://auth.tidal.com/v1/oauth2/token"),
    ("spotify", "https://open.spotify.com/embed/track/5aTCSOha8FYily7Il95kxF"),
    ("soundcloud", "https://api-v2.soundcloud.com/"),
    ("yandex", "https://api.music.yandex.net/"),
    ("apple-itunes", "https://itunes.apple.com/search?term=daft+punk&limit=1"),
    ("bbc", "https://www.bbc.co.uk/sounds"),
    ("jiosaavn", "https://www.jiosaavn.com/"),
    ("github", "https://api.github.com/"),
    ("owner-server", ""),          # адрес берётся из flex_client.BAKED
]


def _why(e: BaseException) -> str:
    return f"{type(e).__name__}: {str(e).replace(chr(10), ' ')[:100]}".rstrip(": ")


# ── сервисы ──────────────────────────────────────────────────────────────────

def _own_has(cfg: dict) -> dict:
    from ripster.source_mode import OWN_KEYS
    has = {s: bool(str(cfg.get(k) or "").strip()) for s, k in OWN_KEYS.items()}
    has["bbc"] = has["jiosaavn"] = has["apple"] = True      # публичные: учётка не нужна
    try:
        from ripster.engines.orpheus_spotify import is_authenticated
        has["spotify"] = bool(is_authenticated())
    except Exception:
        has["spotify"] = bool(str(cfg.get("spotify-sp-dc") or "").strip())
    return has


async def _probe_service(svc: str, cfg: dict, mode: str, configured: bool) -> tuple[str, bool]:
    """-> (строка, проблема?)"""
    if not configured:
        return f"{svc:<10} NOT configured — search not attempted", False
    try:
        from ripster.routes import discovery as D
    except Exception as e:
        return f"{svc:<10} cannot load search module: {_why(e)}", True
    if svc in ("soundcloud", "bbc"):
        return await _probe_own_route(svc)
    parts, bad = [], False
    first_album = ""
    for kind in ("track", "album"):
        t0 = time.time()
        try:
            r = await asyncio.wait_for(D._dispatch_search(QUERY, svc, kind, 5, ""), SEARCH_TIMEOUT_S)
        except asyncio.TimeoutError:
            parts.append(f"search {kind}: FAIL timeout {SEARCH_TIMEOUT_S}s")
            bad = True
            continue
        except Exception as e:
            parts.append(f"search {kind}: FAIL {_why(e)}")
            bad = True
            continue
        ms = int((time.time() - t0) * 1000)
        err = (r or {}).get("error") if isinstance(r, dict) else None
        items = (r or {}).get("results") or [] if isinstance(r, dict) else []
        if err:
            parts.append(f"search {kind}: FAIL {str(err)[:100]}")
            bad = True
        elif not items:
            parts.append(f"search {kind}: EMPTY ({ms}ms)")
            bad = True
        else:
            parts.append(f"search {kind}: OK {len(items)} ({ms}ms)")
            if kind == "album" and not first_album:
                first_album = str(items[0].get("id") or "")
    # карточка альбома: то, что открывается по клику в результатах
    if first_album and svc in getattr(D, "_ENGINE_SERVICES", {}):
        try:
            d = await asyncio.wait_for(D.api_album_detail(svc, first_album), SEARCH_TIMEOUT_S)
            if isinstance(d, dict) and d.get("error"):
                parts.append(f"album card: FAIL {str(d['error'])[:90]}")
                bad = True
            else:
                n = len((d or {}).get("tracks") or [])
                parts.append(f"album card: OK {n} tracks" if n else "album card: EMPTY (0 tracks)")
                bad = bad or not n
        except Exception as e:
            parts.append(f"album card: FAIL {_why(e)}")
            bad = True
    return f"{svc:<10} " + " | ".join(parts), bad


async def _probe_own_route(svc: str) -> tuple[str, bool]:
    """SoundCloud и BBC ищутся своими маршрутами (вкладки), а не общим поиском."""
    t0 = time.time()
    try:
        if svc == "soundcloud":
            from ripster.routes import soundcloud as M
            r = await asyncio.wait_for(M.sc_search(q=QUERY, kind="all", limit=10), SEARCH_TIMEOUT_S)
        else:
            from ripster.routes import bbc as M
            r = await asyncio.wait_for(M.search_bbc(q=QUERY), SEARCH_TIMEOUT_S)
    except asyncio.TimeoutError:
        return f"{svc:<10} search: FAIL timeout {SEARCH_TIMEOUT_S}s", True
    except Exception as e:
        return f"{svc:<10} search: FAIL {_why(e)}", True
    ms = int((time.time() - t0) * 1000)
    if isinstance(r, dict) and r.get("error"):
        return f"{svc:<10} search: FAIL {str(r['error'])[:100]}", True
    n = 0
    if isinstance(r, dict):
        for v in r.values():
            if isinstance(v, list):
                n += len(v)
    elif isinstance(r, list):
        n = len(r)
    if not n:
        return f"{svc:<10} search: EMPTY ({ms}ms)", True
    return f"{svc:<10} search: OK {n} ({ms}ms)", False


async def _services(cfg: dict) -> tuple[list[str], list[str]]:
    from ripster import source_mode
    mode = source_mode.effective_mode(str(cfg.get(source_mode.CONFIG_KEY) or ""), {}) or "unset"
    own = _own_has(cfg)
    eff = source_mode.apply_mode(source_mode.effective_mode(str(cfg.get(source_mode.CONFIG_KEY) or ""), own), own,
                                 source_mode.flex_active())
    sem = asyncio.Semaphore(CONCURRENCY)

    async def guarded(s):
        async with sem:
            return await _probe_service(s, cfg, mode, bool(eff.get(s)))

    rows = await asyncio.gather(*(guarded(s) for s in _SERVICES))
    lines, problems = [], []
    for s, (line, bad) in zip(_SERVICES, rows):
        if s in ("bbc", "jiosaavn", "apple"):
            shown = "public"                      # учётка не нужна
        else:
            shown = "own-keys" if own.get(s) else ("premium" if eff.get(s) else "-")
        lines.append(f"{line}   [source: {shown}]")
        if bad:
            problems.append(f"{s}: {line.split(' | ')[0].split(None, 1)[1][:110]}" if " | " in line or "FAIL" in line else f"{s}: {line[:110]}")
    return lines, problems


# ── Премиум ──────────────────────────────────────────────────────────────────

async def _premium(cfg: dict) -> tuple[list[str], list[str]]:
    from ripster import flex_client, source_mode, premium_search
    out, problems = [], []
    mode = str(cfg.get(source_mode.CONFIG_KEY) or "").strip().lower() or "(not chosen)"
    out.append(f"source mode: {mode}")
    active = flex_client.is_active()
    out.append(f"device token: {'present' if active else 'NOT activated'}")
    if mode in ("premium", "both") and not active:
        problems.append("premium: mode is premium/both but no key is activated")
    if active:
        try:
            st = await asyncio.wait_for(flex_client.status(), 12)
            out.append(f"server status: reachable, supportActive={st.get('support_active')}, until={st.get('support_until')}")
            if not st.get("support_active"):
                problems.append("premium: server says support is NOT active (key expired or revoked)")
        except Exception as e:
            out.append(f"server status: FAIL {_why(e)}")
            problems.append(f"premium: server unreachable ({_why(e)[:60]})")
        for svc in ("qobuz", "tidal"):
            try:
                cred = await asyncio.wait_for(premium_search.lease(svc), 12)
                out.append(f"search lease {svc}: {'ok' if cred else 'NONE (no search account on the server)'}")
                if not cred:
                    problems.append(f"premium: no search lease for {svc}")
            except Exception as e:
                out.append(f"search lease {svc}: FAIL {_why(e)}")
    return out, problems


# ── программы, система, сеть ─────────────────────────────────────────────────

async def _tools(base: Path) -> list[str]:
    try:
        from ripster import setup as S
        S._base_dir = S._base_dir if S._base_dir != Path(".") else base
        t = await asyncio.wait_for(S.check_tools(), 40)
    except Exception as e:
        return [f"check_tools failed: {_why(e)}"]
    out = []
    for k, v in t.items():
        if isinstance(v, dict):
            found = v.get("found")
            ver = str(v.get("version") or "")[:50].replace("\n", " ")
            if ver.upper().startswith("ERROR"):
                ver = "(tool prints usage only)"
            out.append(f"{k:<12} {'FOUND' if found else 'missing'}{('  ' + ver) if ver else ''}")
    return out


def _system(base: Path, cfg: dict) -> list[str]:
    out = [f"OS: {platform.platform()}", f"python: {sys.version.split()[0]}",
           f"time: {datetime.now():%Y-%m-%d %H:%M:%S} tz={time.tzname}"]
    try:
        import psutil
        vm = psutil.virtual_memory()
        out.append(f"RAM: {vm.available // 1048576} MB free of {vm.total // 1048576} MB; CPU cores {psutil.cpu_count(logical=False)}/{psutil.cpu_count()}")
    except Exception:
        out.append("psutil missing (load governor will not work)")
    save = str(cfg.get("save-path") or "downloads")
    for label, p in (("save-path", save), ("app dir", str(base))):
        try:
            u = shutil.disk_usage(p if os.path.exists(p) else str(base))
            warn = "  <-- LOW (<5 GB: downloads pause)" if u.free < 5 * 1024 ** 3 else ""
            out.append(f"disk {label}: {u.free // 1048576} MB free{warn}")
        except Exception as e:
            out.append(f"disk {label}: {_why(e)}")
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        if os.environ.get(k):
            out.append(f"env {k}: set")
    out.append(f"config proxy: {'set' if str(cfg.get('proxy') or '').strip() else 'none'}")
    return out


async def _network() -> tuple[list[str], list[str]]:
    import httpx
    from ripster import flex_client

    sem = asyncio.Semaphore(4)

    async def one(label: str, url: str):
        u = url or flex_client.BAKED + "/api/ping"
        last = ""
        async with sem:
            for attempt in (1, 2):                      # один повтор: на загруженной машине первый коннект бывает медленным
                t0 = time.time()
                try:
                    async with httpx.AsyncClient(timeout=12, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"}) as c:
                        r = await c.get(u)
                    return f"{label:<13} HTTP {r.status_code} ({int((time.time() - t0) * 1000)}ms)", r.status_code >= 500
                except Exception as e:
                    last = _why(e)[:80]
        return f"{label:<13} UNREACHABLE {last}", True

    rows = await asyncio.gather(*(one(l, u) for l, u in _HOSTS))
    problems = [f"network: {r[0].split()[0]} unreachable from this PC" for r in rows if r[1] and "UNREACHABLE" in r[0]]
    return [r[0] for r in rows], problems


def _queue(snapshot) -> list[str]:
    try:
        q = list(snapshot()) if callable(snapshot) else []
    except Exception as e:
        return [f"queue unavailable: {_why(e)}"]
    if not q:
        return ["queue empty"]
    out, counts = [], {}
    for t in q:
        counts[t.get("status")] = counts.get(t.get("status"), 0) + 1
    out.append("counts: " + ", ".join(f"{k}={v}" for k, v in counts.items()))
    for t in [t for t in q if t.get("status") == "error"][:10]:
        log = (t.get("log") or [])[-1:] or [""]
        out.append(f"  [{t.get('service')}/{t.get('engine')}] {str(t.get('title') or t.get('url'))[:60]} -> {str(log[0])[:120]}")
    return out


def _install_check(base: Path) -> list[str]:
    p = base / "logs" / "install_check.txt"
    try:
        lines = [l for l in p.read_text(encoding="utf-8", errors="replace").splitlines() if not l.startswith("OK ")]
        return lines or ["all checks passed at install time"]
    except Exception:
        return ["no install check on record (installed before 3.9.5, or not by the installer)"]


# ── сборка ───────────────────────────────────────────────────────────────────

async def collect(cfg: dict, base_dir: Path, queue_snapshot=None, version: str = "") -> str:
    """Один текст отчёта. Не бросает; общий потолок TOTAL_TIMEOUT_S."""
    sections: dict[str, list[str]] = {}
    problems: list[str] = []

    async def run():
        nonlocal problems
        svc, premium, net, tools = await asyncio.gather(
            _services(cfg), _premium(cfg), _network(), _tools(base_dir), return_exceptions=True)
        for name, res in (("services", svc), ("premium", premium), ("network", net)):
            if isinstance(res, BaseException):
                sections[name] = [f"probe crashed: {_why(res)}"]
                problems.append(f"{name}: probe crashed")
            else:
                sections[name] = res[0]
                problems += res[1]
        sections["tools"] = tools if not isinstance(tools, BaseException) else [f"failed: {_why(tools)}"]

    try:
        await asyncio.wait_for(run(), TOTAL_TIMEOUT_S)
    except asyncio.TimeoutError:
        problems.append(f"health probe did not finish in {TOTAL_TIMEOUT_S}s (some sections missing)")
    # Сеть и поиск не должны противоречить друг другу: если поиск сервиса только что отработал, «недоступен из сети»
    # про его адрес — ложная тревога (медленный момент на загруженной машине), и в итог она не идёт.
    worked = {ln.split()[0] for ln in sections.get("services", []) if ("search track: OK" in ln or "search: OK" in ln)}
    problems = [p for p in problems if not (p.startswith("network: ") and p.split()[1].split("-")[0] in worked)]
    # Нет программ, без которых отдельные функции мертвы, — тоже проблема, а не деталь.
    for line in sections.get("tools", []):
        if line.startswith(("ffmpeg", "ffprobe")) and "missing" in line:
            problems.append(f"tools: {line.split()[0]} is missing")
    out = [f"=== RIPSTER HEALTH REPORT · {version} · {datetime.now():%Y-%m-%d %H:%M:%S} ===", ""]
    out.append(f"PROBLEMS ({len(problems)}):" if problems else "PROBLEMS: none detected by the live check")
    out += [f"  ! {p}" for p in problems]
    blocks = [("SERVICES (live search \"%s\" + album card)" % QUERY, sections.get("services")),
              ("PREMIUM", sections.get("premium")), ("NETWORK (reachability from this PC)", sections.get("network")),
              ("TOOLS", sections.get("tools")), ("SYSTEM", _system(base_dir, cfg)),
              ("QUEUE", _queue(queue_snapshot)), ("INSTALL CHECK (not OK lines)", _install_check(base_dir))]
    for title, lines in blocks:
        out += ["", f"-- {title} --"] + (lines or ["not collected"])
    return "\n".join(out) + "\n"
