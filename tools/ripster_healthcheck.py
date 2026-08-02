# -*- coding: utf-8 -*-
"""Ripster autonomous health-check + self-heal.

Runs a full check of every Ripster subsystem, AUTO-FIXES the safe ones (dead Apple
wrapper, stale media-user-token), then:
  * appends a structured entry to HANDOFF_DAILY_OPS.md
  * sends an owner-only report to the Telegram bot

Designed to run headless (Windows Task Scheduler at 08:00 / 20:00) AND to be the
mechanical tool the `ripster-daily-ops` skill drives when Claude wakes. No 3rd-party
deps (urllib + subprocess only). Never raises — every check is best-effort and the
worst case is a "could not check" line in the report.

Exit code: 0 = all healthy, 1 = issues found (some may be auto-fixed).

Usage:  python tools/ripster_healthcheck.py [--no-bot] [--no-fix] [--no-log]
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config.yaml"
BOT_CFG = ROOT / "tgbot" / "config.json"
HANDOFF = ROOT / "HANDOFF_DAILY_OPS.md"
BASE = "http://127.0.0.1:7799"
WRAPPER_IMAGE = "ripster-wrapper:premium"
CNW = 0x08000000  # CREATE_NO_WINDOW

NO_BOT = "--no-bot" in sys.argv
NO_FIX = "--no-fix" in sys.argv
# Dry/verification runs shouldn't pollute HANDOFF with entries that look exactly
# like a real scheduled check (that happened on 2026-07-25).
NO_LOG = "--no-log" in sys.argv

_report: list[str] = []
_issues = 0
_fixes: list[str] = []


def _cfg_get(key: str) -> str:
    """Cheap YAML scalar read (avoids a pyyaml dep)."""
    try:
        import re
        txt = CONFIG.read_text(encoding="utf-8")
        m = re.search(rf"^{re.escape(key)}:\s*(.+)$", txt, re.M)
        return m.group(1).strip().strip("'\"") if m else ""
    except Exception:
        return ""


def _cookie() -> str:
    sec = _cfg_get("session-secret")
    ts = str(int(time.time()))
    mac = hmac.new(sec.encode(), ts.encode(), hashlib.sha256).hexdigest()
    return f"{ts}.{mac}"


def _api(path: str, method: str = "GET", timeout: float = 15):
    try:
        req = urllib.request.Request(
            BASE + path, method=method,
            headers={"Origin": BASE, "Cookie": f"ripster-session={_cookie()}"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "ignore")
            try:
                return r.status, json.loads(body)
            except Exception:
                return r.status, body
    except Exception as e:
        return 0, str(e)


def _docker(*args, timeout=30):
    try:
        r = subprocess.run(["docker", *args], capture_output=True, text=True,
                           timeout=timeout, creationflags=CNW)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except Exception as e:
        return -1, str(e)


def ok(msg): _report.append(f"✅ {msg}")
def warn(msg):
    global _issues; _issues += 1; _report.append(f"⚠️ {msg}")
def bad(msg):
    global _issues; _issues += 1; _report.append(f"❌ {msg}")
def fixed(msg):
    _fixes.append(msg); _report.append(f"🔧 {msg}")


# ── checks ────────────────────────────────────────────────────────────────────
def check_app():
    if _app_alive():
        ok("App (7799) отвечает"); return True
    bad("App (7799) НЕ отвечает — сервер лежит")
    if NO_FIX:
        return False
    return _heal_app_down()


def _app_alive(timeout: float = 8) -> bool:
    try:
        req = urllib.request.Request(BASE + "/", headers={"User-Agent": "healthcheck"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def _ps_proc_count(cmdline_regex: str) -> int:
    """Count python processes whose command line matches regex (via PowerShell CIM)."""
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "@(Get-CimInstance Win32_Process -Filter \"Name='python.exe' OR Name='pythonw.exe'\" "
             f"| Where-Object {{ $_.CommandLine -match '{cmdline_regex}' }}).Count"],
            capture_output=True, text=True, timeout=30, creationflags=CNW)
        return int((r.stdout or "0").strip() or 0)
    except Exception:
        return 0


_DETACHED = 0x00000008  # DETACHED_PROCESS


def _heal_app_down() -> bool:
    """Post-reboot recovery: Docker Desktop doesn't autostart and the autostarted
    RipsterLauncher.exe can hang without spawning the backend (seen 2026-07-18).
    Fix: bring Docker up, kill hung launchers, start app.py directly via venv."""
    # 1. Docker engine (wrapper + tg-bot-api live there; restart-policy revives them)
    if _docker("ps", timeout=15)[0] != 0:
        dd = Path(r"C:\Program Files\Docker\Docker\Docker Desktop.exe")
        if dd.exists():
            try:
                subprocess.Popen([str(dd)], creationflags=CNW | _DETACHED)
                for _ in range(24):  # up to ~2 мин на движок
                    time.sleep(5)
                    if _docker("ps", timeout=10)[0] == 0:
                        fixed("Docker Desktop запущен (лежал после ребута)"); break
            except Exception as e:
                warn(f"Не смог запустить Docker Desktop: {str(e)[:60]}")
    # 2. Backend already starting? Then only wait, no duplicate spawn.
    #    ВАЖНО: app.py спавнит дочерний python-воркер, который наследует сокет 7799 —
    #    убивать «лишние» app.py по netstat-владельцу нельзя, роняет весь сервис.
    how = ""
    if _ps_proc_count(r"app\.py") == 0:
        try:
            subprocess.run(["taskkill", "/F", "/IM", "RipsterLauncher.exe"],
                           capture_output=True, timeout=15, creationflags=CNW)
        except Exception:
            pass
        time.sleep(2)
        exe = ROOT / "RipsterLauncher.exe"
        py = ROOT / ".venv" / "Scripts" / "python.exe"
        try:
            if exe.exists():
                # Проверенный путь (2026-07-19): чистый перезапуск лаунчера; спавн
                # бэкенда у него медленный (~2-4 мин), но стек остаётся штатным.
                subprocess.Popen([str(exe)], cwd=str(ROOT),
                                 creationflags=CNW | _DETACHED)
                how = "перезапуском RipsterLauncher.exe"
            elif py.exists():
                logf = open(ROOT / "logs" / "app_heal.log", "ab")
                subprocess.Popen([str(py), "app.py"], cwd=str(ROOT),
                                 stdout=logf, stderr=subprocess.STDOUT,
                                 creationflags=CNW | _DETACHED)
                how = "прямым запуском .venv app.py"
            else:
                warn("Нет ни RipsterLauncher.exe, ни .venv python — не могу поднять app сам")
                return False
        except Exception as e:
            warn(f"Не смог запустить app: {str(e)[:60]}"); return False
    # 3. Wait for 7799 (лаунчер спавнит бэкенд медленно — ждём до 4 мин)
    for _ in range(80):
        time.sleep(3)
        if _app_alive(timeout=4):
            fixed(f"App поднят {how or 'ожиданием уже стартующего процесса'}")
            return True
    warn("App не поднялся за 4 мин — нужен ручной разбор (логи logs/)")
    return False


def check_apple_wrapper():
    # container running?
    rc, out = _docker("ps", "--filter", "name=amd-wrapper", "--format", "{{.Names}}")
    running = "amd-wrapper" in out
    # decrypt port open?
    port_ok = False
    try:
        with socket.create_connection(("127.0.0.1", 10020), timeout=2):
            port_ok = True
    except Exception:
        port_ok = False
    if running and port_ok:
        ok("Apple wrapper (amd-wrapper) работает, порт 10020 открыт")
    else:
        warn(f"Apple wrapper не готов (running={running}, port10020={port_ok})")
        if not NO_FIX:
            _heal_wrapper()
    # account API 30020 + token
    st, data = _api_raw_30020()
    if isinstance(data, dict) and len(data.get("music_token") or "") > 50:
        ok(f"Apple токен-API (30020) отдаёт media-user-token ({len(data['music_token'])} симв.)")
        if not NO_FIX:
            s, r = _api("/api/apple/sync-from-wrapper", "POST")
            if isinstance(r, dict) and r.get("mut_synced"):
                fixed("Apple media-user-token пересинхронизирован из wrapper")
        return
    # 30020 silent while 10020 listens = the wrapper is up on a DEAD session
    # (app-side `amd.start_wrapper()` reuses dist/docker/rootfs/data with no -L,
    # so an expired session loops "[.] playback error" forever and every decrypt
    # would fail "Invalid CKC" even though the TCP port is open). Seen 2026-07-25.
    warn("Apple токен-API (30020) не отдаёт токен — video/aac-lc могут не работать")
    if NO_FIX:
        return
    _heal_wrapper()
    st, data = _api_raw_30020()
    if isinstance(data, dict) and len(data.get("music_token") or "") > 50:
        fixed(f"Apple токен-API (30020) восстановлен ({len(data['music_token'])} симв.)")
        s, r = _api("/api/apple/sync-from-wrapper", "POST")
        if isinstance(r, dict) and r.get("mut_synced"):
            fixed("Apple media-user-token пересинхронизирован из wrapper")
    else:
        bad("Apple токен-API (30020) молчит и после перезапуска wrapper — "
            "ручной разбор (skill ripster-apple-wrapper)")


def check_gamdl_cookies():
    """Отличить ДВА состояния cookies.txt, которые до 01.08.2026 выглядели одинаково.

    gamdl падает с «unexpected finish»/«No active Apple Music subscription» и по
    обоим поводам совет был один: «экспортируй cookies.txt заново». Но причины
    две, и лечатся они по-разному:
      * сессия ПРОТУХЛА (401/403) → повторный экспорт из браузера действительно чинит;
      * сессия ЖИВА, а подписка на этом аккаунте КОНЧИЛАСЬ (200, active=False) →
        повторный экспорт тех же куки не изменит НИЧЕГО, и владелец потратит
        время впустую.
    01.08.2026: куки жили с 06.05, mut-refresh истёк 09.06 — по всем внешним
    признакам «протухли», а на деле сессия отвечала 200 и storefront=gb; мёртвой
    была подписка. Второй аккаунт (враппер, ca) при этом active=True.

    Проверка read-only: dev_token берём у локального враппера (30020),
    media-user-token — из самого cookies.txt. Ни одного запроса на скачивание,
    ни одной записи."""
    p = Path(_cfg_get("gamdl-cookies-path") or "cookies.txt")
    if not p.is_absolute():
        p = ROOT / p
    if not p.exists():
        return  # gamdl просто не настроен — это не поломка
    mut = ""
    try:
        for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            f = line.split("\t")
            if len(f) >= 7 and f[5].strip() == "media-user-token":
                mut = f[6].strip()
    except Exception:
        return
    if not mut:
        warn(f"gamdl: в {p.name} нет media-user-token — экспортируй заново "
             f"из залогиненного music.apple.com")
        return
    dev, dev_src = _apple_dev_token()
    if not dev:
        # Без ЗАВЕДОМО живого dev_token любой ответ 401 неотличим от «протухли
        # куки» — а это ровно тот ложный совет, ради которого проверка писалась.
        # Молчим честно вместо того, чтобы гадать.
        warn("gamdl: не удалось получить действующий Apple dev_token "
             "(враппер отдал протухший, со страницы Apple добыть не вышло) — "
             "состояние cookies.txt НЕ проверено, выводов не делаем")
        return
    # gamdl в wrapper-режиме ходит на 30020 САМ и подстраховаться скрейпом не
    # умеет: если токен враппера протух, этот путь отдаст 401 на любом качестве.
    # _cfg_get отдаёт СТРОКУ: "false" тоже истинна, сравниваем по значению.
    if (dev_src != "с враппера 30020"
            and _cfg_get("gamdl-use-wrapper").lower() in ("true", "yes", "1", "on")):
        warn("gamdl wrapper-режим: dev_token на 30020 протух (враппер выдаёт его "
             "один раз при старте, живёт он 5 минут) — качества, идущие через "
             "--wrapper-account-url, получат 401. Лечится только перезапуском "
             "враппера, а он стоит слота устройства: трогать, только если "
             "gamdl реально нужен")
    try:
        req = urllib.request.Request(
            "https://amp-api.music.apple.com/v1/me/account?meta=subscription",
            headers={"Authorization": f"Bearer {dev}", "Media-User-Token": mut,
                     "Origin": "https://music.apple.com", "User-Agent": "Ripster"})
        with urllib.request.urlopen(req, timeout=12) as r:
            body = json.loads(r.read().decode("utf-8", "ignore"))
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            # dev_token заведомо жив (проверен на публичном каталоге), значит
            # отказ действительно про media-user-token из cookies.txt.
            warn(f"gamdl: сессия в {p.name} протухла ({e.code}, dev_token "
                 f"проверен и жив — {dev_src}) — экспортируй cookies.txt заново "
                 f"из браузера с активной подпиской")
        return
    except Exception:
        return  # сеть — не наша поломка, молчим
    sub = (body.get("meta") or {}).get("subscription") or {}
    sf = sub.get("storefront") or "?"
    if sub.get("active"):
        ok(f"gamdl: cookies.txt жив, подписка активна (storefront {sf})")
    else:
        warn(f"gamdl: сессия в {p.name} ЖИВА, но подписки на этом аккаунте нет "
             f"(storefront {sf}) — повторный экспорт ТЕХ ЖЕ куки не поможет. "
             f"Нужны куки аккаунта С подпиской либо продлить текущий. "
             f"Загрузки через wrapper (zhaarey/AMD) это не затрагивает")


def _api_raw_30020():
    try:
        with urllib.request.urlopen("http://127.0.0.1:30020", timeout=6) as r:
            return r.status, json.loads(r.read().decode("utf-8", "ignore"))
    except Exception as e:
        return 0, str(e)


def _dev_token_works(tok):
    """Единственный честный признак живого dev_token — публичный каталог отвечает."""
    if not tok:
        return False
    try:
        req = urllib.request.Request(
            "https://amp-api.music.apple.com/v1/catalog/us/artists/909253",
            headers={"Authorization": f"Bearer {tok}", "Origin": "https://music.apple.com"})
        with urllib.request.urlopen(req, timeout=12) as r:
            return r.status == 200
    except Exception:
        return False


_dev_token_cache = None


def _apple_dev_token():
    """Действующий Apple developer token + откуда он взят.

    02.08.2026: враппер на 30020 генерирует dev_token ОДИН раз при старте, срок
    жизни у него 300 секунд, а отдаёт он его потом сутками. Через пять минут
    после запуска контейнера любой запрос с этим токеном получает 401 — и
    проверка cookies.txt, опиравшаяся на 30020, снова начала печатать «сессия
    протухла, экспортируй куки заново». Ровно тот ложный совет, который
    01.08.2026 уже опровергли живьём (сессия отвечала 200, мертва была подписка).

    Поэтому: токен с враппера сначала ПРОВЕРЯЕМ на публичном каталоге и, если он
    протух, берём свежий со страницы music.apple.com (тот живёт ~2 месяца).
    Возвращает ("", "") если действующего токена нет — тогда вызывающий обязан
    промолчать, а не гадать о причине 401."""
    global _dev_token_cache
    if _dev_token_cache is not None:
        return _dev_token_cache
    st, data = _api_raw_30020()
    tok = (data.get("dev_token") or "").strip() if isinstance(data, dict) else ""
    if _dev_token_works(tok):
        _dev_token_cache = (tok, "с враппера 30020")
        return _dev_token_cache
    _dev_token_cache = (_scrape_dev_token(), "со страницы music.apple.com")
    if not _dev_token_cache[0]:
        _dev_token_cache = ("", "")
    return _dev_token_cache


def _scrape_dev_token():
    """Достать web-player dev_token из JS-бандла music.apple.com.

    В бандле лежит несколько JWT; годится не всякий (у одного из трёх iss своя,
    и каталог отвечает ему 401), поэтому берём первый, который РЕАЛЬНО ответил."""
    ua = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    try:
        req = urllib.request.Request("https://music.apple.com/us/browse", headers=ua)
        with urllib.request.urlopen(req, timeout=20) as r:
            html = r.read().decode("utf-8", "ignore")
        m = re.search(r"/assets/index~[^\"']*\.js", html)
        if not m:
            return ""
        req = urllib.request.Request("https://music.apple.com" + m.group(0), headers=ua)
        with urllib.request.urlopen(req, timeout=25) as r:
            body = r.read().decode("utf-8", "ignore")
    except Exception:
        return ""
    for tok in sorted(set(re.findall(
            r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}", body))):
        if _dev_token_works(tok):
            return tok
    return ""


_healed_wrapper = False


def _heal_wrapper():
    """Restart a SINGLE premium wrapper — the proven single-account recovery."""
    global _healed_wrapper
    if _healed_wrapper:  # idempotent: at most one login per sweep (leases are scarce)
        return
    _healed_wrapper = True
    aid, pw = _cfg_get("wrapper-apple-id"), _cfg_get("wrapper-password")
    if not (aid and pw):
        warn("Wrapper credentials не заданы — не могу поднять wrapper"); return
    # Mount the primary account's OWN persistent device identity. The stock image's
    # baked-in adi.pb is shared by every container ever run from it, and a stuck
    # lease on it makes any account fail "device limit" instantly (see skill #0).
    ident = ROOT / "dist" / "docker" / "rootfs_working" / "data"
    ident.mkdir(parents=True, exist_ok=True)
    _docker("rm", "-f", "amd-wrapper")
    rc, out = _docker(
        "run", "-d", "--name", "amd-wrapper", "--restart", "unless-stopped",
        "-v", f"{ident}:/app/rootfs/data",
        "-p", "127.0.0.1:10020:10020", "-p", "127.0.0.1:20020:20020",
        "-p", "127.0.0.1:30020:30020",
        "-e", f"args=-H 0.0.0.0 -L {aid}:{pw}", WRAPPER_IMAGE, timeout=60)
    if rc != 0:
        bad(f"Не смог поднять amd-wrapper: {out[:120]}"); return
    # wait for login (up to ~40s)
    for _ in range(20):
        time.sleep(2)
        _, logs = _docker("logs", "amd-wrapper", "--tail", "8")
        if "account info cached successfully" in logs:
            fixed("Apple wrapper перезапущен и залогинен (account cached)"); return
        if "device limit" in logs or "login failed" in logs:
            bad("Wrapper: device-limit/login-failed — нужен ручной разбор (см. skill ripster-apple-wrapper)")
            return
    warn("Wrapper поднят, но не подтвердил логин за 40с — проверь docker logs amd-wrapper")


def check_tokens():
    checks = {
        "qobuz-auth-token": "Qobuz", "deezer-arl": "Deezer",
        "tidal-token": "Tidal", "spotify-sp-dc": "Spotify",
        "soundcloud-oauth-token": "SoundCloud", "yandex-token": "Yandex",
    }
    missing = [name for key, name in checks.items() if not _cfg_get(key)]
    if missing:
        warn(f"Токены отсутствуют: {', '.join(missing)} — вставь через бот /set")
    else:
        ok("Токены всех сервисов на месте")


def check_engine_probe():
    """Реально ли работают сервисы — по настоящим пробам, а не по наличию токена.

    Раньше тут опрашивался `/api/services/status`, а он, вопреки названию,
    сообщает лишь ЗАПОЛНЕНО ЛИ ПОЛЕ с токеном — так и написано в его же
    докстроке. Поэтому 28.07.2026 Qobuz часами отвечал 401 «Invalid
    username/email and password», в консоли это было видно, а проверка бодро
    писала «все зелёные». Классическая поломка, которая выглядит как успех.
    `/api/admin/probe-all` ходит в каждый сервис по-настоящему.
    """
    # Пробы ходят в девять внешних сервисов — 15 секунд им мало.
    st, data = _api("/api/admin/probe-all", method="POST", timeout=180)
    services = (data or {}).get("services") if isinstance(data, dict) else None
    if st != 200 or not isinstance(services, list):
        # Запасной путь — старый эндпоинт, но честно говорим, что он проверяет.
        st2, d2 = _api("/api/services/status")
        if st2 == 200 and isinstance(d2, dict):
            missing = [k for k, v in d2.items() if v is False]
            if missing:
                warn(f"Токен не заполнен: {', '.join(missing)}")
            else:
                warn("Пробы недоступны — проверил только НАЛИЧИЕ токенов, не работу")
        else:
            warn("Не смог опросить сервисы")
        return

    down = []
    for s in services:
        if not isinstance(s, dict) or s.get("ok"):
            continue
        name = str(s.get("service") or "?")
        err = str(s.get("error") or "")
        # Сторонний Amazon-враппер лежит сутками — это не наша поломка, и
        # ронять из-за него общий вердикт нельзя (иначе проверка станет
        # постоянно красной, и на неё перестанут смотреть).
        if name == "amazon" and ("amz.dezalty.com" in err or "503" in err):
            continue
        down.append(f"{name}: {err[:70]}")
    if down:
        warn("Сервисы НЕ РАБОТАЮТ (живая проба): " + " · ".join(down))
    else:
        ok(f"Сервисы отвечают по-настоящему ({len(services)} проверено)")


def check_tunnel():
    url = _cfg_get("public-url")
    if not url or _cfg_get("remote-enabled") not in ("true", "True", "1"):
        ok("Туннель выключен (remote-enabled off) — пропускаю"); return
    try:
        req = urllib.request.Request(url, method="GET", headers={"User-Agent": "hc"})
        with urllib.request.urlopen(req, timeout=12) as r:
            ok(f"Туннель отвечает ({url})"); return
    except urllib.error.HTTPError as he:
        # Any HTTP status (401/403/405/…) means the tunnel is UP and routing —
        # only a connection error means it's actually down.
        if he.code < 500:
            ok(f"Туннель отвечает ({url}, HTTP {he.code})"); return
        warn(f"Туннель вернул {he.code} (5xx) — сервер за туннелем нездоров")
    except Exception as e:
        warn(f"Туннель ({url}) не отвечает: {str(e)[:60]} — переподключится сам (watchdog)")


def check_queue():
    st, data = _api("/api/queue")
    if st != 200 or not isinstance(data, list):
        warn("Не смог прочитать очередь"); return
    now = time.time()
    stuck = [t for t in data
             if t.get("status") == "running"
             and now - float(t.get("_start_time") or now) > 1800]
    if stuck:
        warn(f"Очередь: {len(stuck)} задач висят >30мин (running) — возможен затык")
    else:
        ok(f"Очередь здорова ({len(data)} задач)")


def check_bot():
    rc, out = _docker("ps", "--filter", "name=tg-bot-api", "--format", "{{.Names}}")
    api_up = "tg-bot-api" in out
    if api_up:
        ok("TG Bot API контейнер (tg-bot-api) работает")
    else:
        warn("TG Bot API контейнер не найден — доставки в бот могут не идти (docker start tg-bot-api)")
        if not NO_FIX:
            _docker("start", "tg-bot-api")
    # bot.py process (реальная проверка по командной строке)
    if _ps_proc_count(r"bot\.py") > 0:
        ok("Бот (bot.py) запущен")
    else:
        warn("Бот (bot.py) не запущен")
        if not NO_FIX:
            try:
                subprocess.Popen([r"C:\Python314\python.exe", "bot.py"],
                                 cwd=str(ROOT / "tgbot"),
                                 creationflags=CNW | _DETACHED)
                time.sleep(4)
                if _ps_proc_count(r"bot\.py") > 0:
                    fixed("Бот (bot.py) перезапущен")
            except Exception as e:
                warn(f"Не смог запустить bot.py: {str(e)[:60]}")


def check_watchlist():
    """Watchlist entries that the background checker cannot actually poll.

    Found 2026-07-24: every entry had an empty artist_id, and the Apple branch
    only iterates entries that HAVE one — so the watchlist had never checked
    anything since it was built, silently. `POST /api/watchlist/repair` resolves
    the ids and drops duplicates; it is idempotent, so running it every sweep is
    free once the list is clean.
    """
    st, items = _api("/api/watchlist")
    if st != 200 or not isinstance(items, dict):
        warn("Не смог прочитать вишлист"); return
    items = items.get("items") or []
    if not items:
        ok("Вишлист пуст — нечего проверять"); return

    # Подписки на ЛЕЙБЛ сюда не относятся: у них artist_id не бывает вовсе —
    # ключом служит название, и опрашиваются они своим путём. Без этого условия
    # каждая подписка на лейбл считалась сломанной записью (29.07.2026: «This
    # Never Happened»). Ложная краснота вредна ровно так же, как ложная зелень:
    # и та и другая приучают не смотреть на проверку.
    # artist_id нужен записи ЛЮБОГО сервиса, кроме SoundCloud: наблюдение всегда
    # идёт по каталогу Apple, сервис записи говорит лишь куда качать. 31.07.2026
    # фильтр `service == "apple"` пропустил мёртвую deezer-подписку — она
    # показывалась мягким «ни разу не проверялась» вместо «не проверяется вообще».
    broken = [x for x in items
              if x.get("kind") != "label"
              and x.get("service", "apple") != "soundcloud"
              and not x.get("artist_id")]
    seen, dups = set(), 0
    for x in items:
        sig = ((x.get("name") or "").strip().lower(), x.get("service", ""))
        if sig in seen:
            dups += 1
        seen.add(sig)

    if not broken and not dups:
        never = [x for x in items if not x.get("last_check")]
        if never:
            warn(f"Вишлист: {len(never)} из {len(items)} ни разу не проверялись "
                 f"(авто-проверка раз в 6ч — если так и останется, смотри логи)")
        else:
            ok(f"Вишлист здоров ({len(items)} записей, все опрашиваются)")
        return

    warn(f"Вишлист: {len(broken)} записей без artist_id, {dups} дублей — "
         f"такие записи НЕ проверяются вообще")
    if NO_FIX:
        return
    st, res = _api("/api/watchlist/repair", method="POST", timeout=90)
    if st == 200 and isinstance(res, dict) and res.get("ok"):
        msg = (f"Вишлист починен: artist_id восстановлен у {res.get('fixed', 0)}, "
               f"удалено дублей {res.get('dropped', 0)}")
        # Склейка соавторов («A, B, C») в имени записи — с 02.08.2026 репейр
        # разбирает её на отдельных артистов вместо вечно мёртвой записи.
        if res.get("split"):
            msg += f", склеек соавторов разобрано {res['split']}"
        fixed(msg)
    else:
        warn(f"Авто-починка вишлиста не удалась: {str(res)[:80]}")


def check_external_apis():
    """Canaries for third-party endpoints we depend on but do not control.

    Apple retired `itunes.apple.com/rss/artistnewreleases/` at some point and it
    now 400s for every id — the watchlist kept "working" while finding nothing,
    for months, because nobody was watching the dependency itself. Cheap probes
    turn that class of silent rot into a visible warning.
    """
    probes = [
        ("iTunes lookup (вишлист/релизы)",
         "https://itunes.apple.com/lookup?id=634763116&entity=album&limit=1",
         lambda b: '"resultCount"' in b and '"resultCount":0' not in b),
        ("iTunes search (резолв артистов)",
         "https://itunes.apple.com/search?term=lane+8&entity=musicArtist&limit=1",
         lambda b: '"artistId"' in b),
    ]
    for label, url, is_good in probes:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "healthcheck"})
            with urllib.request.urlopen(req, timeout=12) as r:
                body = r.read().decode("utf-8", "ignore")
            if r.status == 200 and is_good(body):
                ok(f"Внешний API жив: {label}")
            else:
                warn(f"Внешний API отвечает НЕ тем: {label} (HTTP {r.status}) — "
                     f"возможно, Apple сменил/убрал эндпоинт")
        except Exception as e:
            warn(f"Внешний API недоступен: {label} — {str(e)[:60]}")


def check_botapi_responsive():
    """The Bot API container can be *running* yet wedged — deliveries hang while
    `docker ps` still shows it up, so the plain container check passes. Probe the
    API itself and restart on a hang (see project_botapi_container_hang)."""
    cfg = ROOT / "tgbot" / "config.json"
    try:
        token = json.loads(cfg.read_text(encoding="utf-8")).get("bot_token", "")
    except Exception:
        return  # no bot configured on this box — nothing to check
    if not token:
        return
    try:
        req = urllib.request.Request(f"http://127.0.0.1:8081/bot{token}/getMe")
        with urllib.request.urlopen(req, timeout=12) as r:
            if r.status == 200 and '"ok":true' in r.read().decode("utf-8", "ignore"):
                ok("TG Bot API отвечает (getMe)"); return
        warn("TG Bot API ответил неожиданно на getMe")
    except Exception as e:
        warn(f"TG Bot API не отвечает ({str(e)[:50]}) — контейнер завис")
        if NO_FIX:
            return
        _docker("restart", "tg-bot-api", timeout=90)
        time.sleep(8)
        try:
            req = urllib.request.Request(f"http://127.0.0.1:8081/bot{token}/getMe")
            with urllib.request.urlopen(req, timeout=12) as r:
                if r.status == 200:
                    fixed("tg-bot-api перезапущен (висел, docker ps врал что живой)")
                    return
        except Exception:
            pass
        warn("Перезапуск tg-bot-api не помог — нужен ручной разбор")


def check_disk():
    try:
        import shutil
        sp = _cfg_get("save-path") or str(ROOT)
        drive = os.path.splitdrive(sp)[0] or "C:"
        total, used, free = shutil.disk_usage(drive + "\\")
        gb = free / (1024 ** 3)
        if gb < 5:
            warn(f"Мало места на {drive}: {gb:.1f} GB свободно (<5 GB)")
        else:
            ok(f"Диск {drive}: {gb:.0f} GB свободно")
    except Exception as e:
        warn(f"Не смог проверить диск: {str(e)[:60]}")


# ── Heuristic error study: aggregate & classify recent failures ──────────────
# The "power reserve" for learning from errors — scan the last 24h of the console
# log, group failures by service + a coarse cause bucket, and surface the top
# offenders so the owner (and future me) can spot patterns instead of one-off noise.
_ERR_BUCKETS = [
    # Third-party wrapper amz.dezalty.com goes down for days at a time (503 /
    # Heroku error page). Our probe reports that honestly, but it fires on every
    # services-status refresh, so a single outage buried 214 of 233 "other"
    # errors on 2026-07-25 and made the bucket look alarming. Classify it (and
    # keep it FIRST — the message says "connection"/"503", which the generic
    # `network` bucket would otherwise swallow). Not our bug, just noise.
    ("amazon-thirdparty", ("amz.dezalty.com", "[amazon] probe failed")),
    # A task whose engine can't speak its URL's service. The Apple Go tool answers
    # "Failed to get album response" and exits 0, so the failure hid inside the
    # generic `other` bucket (2026-07-27: a Spotify album URL reached
    # apple-music-downloader.exe). runner._sanity_route now re-routes it, but keep
    # the bucket — if this ever reappears, it names itself instead of hiding.
    # 2026-07-28: the gate turned out to no-op for the very case that prompted it —
    # engine_for_svc() returns the APPLE default for any unmapped service, so in the
    # DEFAULT spotify-engine=convert a raw Spotify URL went right back to zhaarey.
    # Only this box's orpheus_spotify setting routed out of it. _sanity_route now
    # sets task["_route_error"] when no engine can speak the URL → named ERROR row.
    # Настоящее несовпадение движка и ссылки называет себя само — это строка,
    # которую ставит runner._sanity_route.
    ("engine-url-mismatch", ("не умеет", "задача не запущена")),
    # А «failed to get album response» — это НЕ про движок. 28.07.2026 все 8
    # таких строк оказались одним альбомом, которого просто нет в запрошенной
    # витрине (279606055 издан только в tr/ru, ссылка была на us → 404 → Apple
    # отвечает «Failed to get album response» и выходит с кодом 0). Пока они
    # лежали в бакете про движок, диагностика уходила не туда — искали ошибку
    # маршрутизации там, где её нет.
    ("apple-album-unavailable", ("failed to get album response",
                                 "error getting album response")),
    ("apple-hires-wrapper", ("wm.wol.moe", "wrapper-manager", "decrypt stream", "деш",
                              "wrappermanagerexception", "no healthy and ready instances",
                              "caught internally by rip_song")),
    # Каждый Invalid-CKC инцидент печатает ДВЕ строки: диагноз движка («Invalid
    # CKC») и следом совет раннера, где написано «DRM/CKC» — вторая не совпадала
    # ни с одним ключом и падала в `other`. 29.07.2026 те же 4 инцидента лежали в
    # двух бакетах сразу: apple-ckc×4 + 4 безымянных. Ловим обе формы.
    ("apple-ckc",           ("invalid ckc", "decryptfragment", "drm/ckc")),
    # Совет раннера про регион («прав в регионе аккаунта нет / перелогин НЕ
    # поможет») — это ВТОРАЯ строка того же инцидента, что и apple-ckc, и она не
    # совпадала ни с одним ключом: 31.07.2026 четыре таких строки составили
    # большую часть `other` и прятали за собой настоящую находку. Диагноз в них
    # верный — им не хватало только имени.
    ("apple-region",        ("resource not found", "40400", "territory",
                             "прав в регионе аккаунта нет")),
    # Протухшие cookies.txt: gamdl рапортует «нет подписки», хотя подписка есть.
    # Постоянное состояние, чинится только руками владельца (см. gamdl.py).
    ("gamdl-cookies",       ("no active apple music subscription",
                             "протухли cookies")),
    ("apple-tags",          ("failed to write tags", "parseuint")),
    # Лирика Spotify отдаёт 400 на треках, у которых её просто нет. Ключ
    # "spotify" в бакете авторизации проглатывал это и рисовал spotify-auth×3 при
    # полностью здоровой авторизации (29.07.2026: 2 строки лирики + 1 попытка
    # blob'а 1/4, которая сама же и пересоздалась). Держать ПЕРЕД spotify-auth.
    ("spotify-lyrics",      ("color-lyrics", "error fetching spotify lyrics")),
    ("spotify-auth",        ("orpheus_not_authed", "gettrack", "401", "spotify", "attribute 'download_type'")),
    ("qobuz-sub",           ("нет активной подписки", "ineligible")),
    # Beatport отвечает 403 «You do not have permission to perform this action» на
    # запросе ПОТОКА (логин при этом проходит, подписка активна) — это права на
    # конкретный релиз, а не регион и не поломка. 29.07.2026 дало 21 безымянную
    # строку в `other` от одного релиза, качавшегося по кругу.
    ("beatport-entitlement", ("do not have permission to perform this action",
                              "не хватает прав на скачивание")),
    ("beatport-region",     ("region locked", "territory restricted")),
    ("network",             ("getaddrinfo", "deadline exceeded", "connection", "timeout", "429")),
    # Хвосты уже посчитанных провалов: «Exit code 0» печатается ПОСЛЕ причины
    # (apple-album-unavailable), «=== Track N failed ===» — после 403 Beatport.
    # Сами по себе они ничего не диагностируют, но раздували `other` вдвое.
    # Держать ПОСЛЕДНИМИ: любой хвост, у которого есть своя причина, до сюда не
    # дойдёт. Смысл `other` — «такого я ещё не видел», и он должен быть честным.
    ("noise-tail",          ("exit code 0", "=== track ")),
]


def check_errors_24h():
    log = ROOT / "logs" / "console.log"
    if not log.exists():
        return
    try:
        import re as _re
        cutoff = datetime.now().timestamp() - 24 * 3600
        buckets: dict = {}
        total = 0
        for ln in log.read_text(encoding="utf-8", errors="ignore").splitlines()[-8000:]:
            if " ERROR " not in ln:
                continue
            m = _re.match(r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)", ln)
            if m:
                try:
                    if datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").timestamp() < cutoff:
                        continue
                except Exception:
                    pass
            low = ln.lower()
            if any(x in low for x in ("=======", "traceback", 'file "')):
                continue
            total += 1
            tag = next((name for name, keys in _ERR_BUCKETS if any(k in low for k in keys)), "other")
            buckets[tag] = buckets.get(tag, 0) + 1
        if not buckets:
            ok("Ошибок за сутки: 0 — чисто")
            return
        top = sorted(buckets.items(), key=lambda x: -x[1])
        summary = ", ".join(f"{k}×{v}" for k, v in top[:6])
        # Единственный бакет, который требует РУК владельца: cookies.txt заново
        # не экспортируешь автоматически. Отдельной строкой, иначе он тонет в
        # информационной сводке и месяцами никем не читается.
        if buckets.get("gamdl-cookies"):
            # Не советуем «экспортируй заново» вслепую: с 01.08.2026 точный
            # диагноз (протухшая сессия vs кончившаяся подписка) даёт
            # check_gamdl_cookies выше — там же и правильное действие.
            warn(f"gamdl: {buckets['gamdl-cookies']} падений за сутки из-за cookies.txt — "
                 f"смотри строку «gamdl:» выше, там точная причина и что делать. "
                 f"Загрузки через wrapper (zhaarey/AMD) это не затрагивает")
        # A high error count of a FIXABLE class is worth flagging; noise (region/sub)
        # is expected, so it's informational.
        _report.append(f"📈 Ошибки за 24ч ({total}): {summary}")
    except Exception as e:
        _report.append(f"📈 Ошибки за 24ч: не смог посчитать ({str(e)[:50]})")


# ── report + log ──────────────────────────────────────────────────────────────
def send_bot(text: str):
    if NO_BOT:
        return
    try:
        cfg = json.loads(BOT_CFG.read_text(encoding="utf-8"))
        token, owner = cfg["bot_token"], cfg["owner_id"]
        api = (cfg.get("local_bot_api") or "https://api.telegram.org").rstrip("/")
        import urllib.parse
        # Telegram hard limit 4096; chunk.
        for i in range(0, len(text), 3800):
            chunk = text[i:i + 3800]
            data = urllib.parse.urlencode({
                "chat_id": owner, "text": chunk, "parse_mode": "HTML",
                "disable_web_page_preview": "true"}).encode()
            urllib.request.urlopen(
                urllib.request.Request(f"{api}/bot{token}/sendMessage", data=data), timeout=25)
            time.sleep(0.4)
    except Exception as e:
        print(f"[healthcheck] bot send failed: {str(e)[:100]}")


def append_handoff(summary: str):
    if NO_LOG:
        return
    try:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        entry = f"\n## {stamp} — авто-проверка\n{summary}\n"
        if not HANDOFF.exists():
            HANDOFF.write_text("# HANDOFF — ежедневная авто-проверка Ripster\n\n"
                               "Автоген `tools/ripster_healthcheck.py` (Task Scheduler 08:00/20:00).\n",
                               encoding="utf-8")
        with HANDOFF.open("a", encoding="utf-8") as f:
            f.write(entry)
    except Exception as e:
        print(f"[healthcheck] handoff append failed: {str(e)[:100]}")


def main():
    app_ok = check_app()
    if app_ok:
        check_apple_wrapper()
        check_gamdl_cookies()
        check_tokens()
        check_engine_probe()
        check_tunnel()
        check_queue()
        check_watchlist()
        check_bot()
        check_botapi_responsive()
        check_external_apis()
        check_disk()
        check_errors_24h()
    status = "🟢 ВСЁ ЗДОРОВО" if _issues == 0 else f"🟠 НАЙДЕНО ПРОБЛЕМ: {_issues}"
    if _fixes:
        status += f" · автофиксов: {len(_fixes)}"
    when = datetime.now().strftime("%d.%m %H:%M")
    body = (f"🦝 <b>Авто-проверка Ripster · {when}</b>\n{status}\n\n"
            + "\n".join(_report))
    if _fixes:
        body += "\n\n<b>Исправлено автоматически:</b>\n" + "\n".join(f"• {x}" for x in _fixes)
    if _issues:
        body += ("\n\n<i>Оставшееся требует внимания — если критично, я разберу на "
                 "следующем пробуждении или напиши мне.</i>")
    send_bot(body)
    # plain-text handoff (strip simple tags)
    plain = body.replace("<b>", "").replace("</b>", "").replace("<i>", "").replace("</i>", "")
    append_handoff(plain)
    # The ASCII squash exists so a cp866 console can't kill the run with
    # UnicodeEncodeError — but it also turned every logged report into rows of "?",
    # unreadable exactly when it matters (reading logs/boot_recovery.log after an
    # outage). Squash only when the stream genuinely can't carry the text.
    try:
        print(plain)
    except UnicodeEncodeError:
        print(plain.encode("ascii", "replace").decode("ascii"))
    sys.exit(0 if _issues == 0 else 1)


if __name__ == "__main__":
    main()
