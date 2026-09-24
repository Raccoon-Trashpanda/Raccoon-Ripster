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
import ssl
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
CANARY_STATE = Path(r"C:\dev\LOG\download_canary.json")
HANDOFF = ROOT / "HANDOFF_DAILY_OPS.md"
BASE = "http://127.0.0.1:7799"
WRAPPER_IMAGE = "ripster-wrapper:premium"
CNW = 0x08000000  # CREATE_NO_WINDOW

NO_BOT = "--no-bot" in sys.argv
NO_FIX = "--no-fix" in sys.argv
if NO_FIX:
    # credential_health читает это сам: streak, снятие учёток и автозамена
    # основной под --no-fix не происходят.
    os.environ["RIPSTER_HEALTH_DRY_RUN"] = "1"
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
        txt = CONFIG.read_text(encoding="utf-8-sig")
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


def _esc(msg) -> str:
    """Report lines carry raw error text (e.g. "<urlopen error _ssl.c:1063…>"), and the
    owner report is sent parse_mode=HTML — an unescaped "<" makes Telegram reject the
    WHOLE message with 400 and the owner silently gets nothing (2026-08-07). Only main()
    emits real markup, so every check-produced line is escaped at the source."""
    return (str(msg).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _strip_html(s: str) -> str:
    """Markup out, entities back to real characters — for the log and the plain-text resend."""
    for tag in ("<b>", "</b>", "<i>", "</i>"):
        s = s.replace(tag, "")
    return s.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


_STATE = ROOT / "tools" / ".healthcheck_state.json"


def _state_load() -> dict:
    """Small cross-run memory. Never fatal: a corrupt/missing file just means
    'no history', which degrades to the old single-run behaviour."""
    try:
        return json.loads(_STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _state_save(st: dict) -> None:
    # A --no-log run is a verification re-run: it must not inflate streak
    # counters, or re-checking a fix would itself look like a repeat failure.
    if NO_LOG:
        return
    try:
        _STATE.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass


def _streak(key: str, failing: bool) -> int:
    """Consecutive sweeps this check has been failing (0 when healthy).

    Added 08.08.2026: the tunnel playbook says "escalate if it's still like this
    on the next check", but nothing counted — the streak lived only in
    HANDOFF_DAILY_OPS.md and a human had to read it back. Now the check knows."""
    st = _state_load()
    n = (int(st.get(key, 0)) + 1) if failing else 0
    # 08.08.2026: _state_save already refuses to PERSIST on a --no-log run, but
    # the inflated number was still being RETURNED, so a verification re-run
    # printed "4-я проверка подряд" while the state file honestly held 3 — the
    # half-fix looked exactly like the bug it was meant to prevent. A run that
    # isn't counted must not be counted in the text either: report the last real
    # streak instead.
    if NO_LOG:
        return int(st.get(key, 0)) if failing else 0
    st[key] = n
    _state_save(st)
    return n


def ok(msg): _report.append(f"✅ {_esc(msg)}")
def warn(msg):
    global _issues; _issues += 1; _report.append(f"⚠️ {_esc(msg)}")
def bad(msg):
    global _issues; _issues += 1; _report.append(f"❌ {_esc(msg)}")
def fixed(msg):
    _fixes.append(_esc(msg)); _report.append(f"🔧 {_esc(msg)}")


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


def _port_owner() -> tuple[int, float]:
    """(pid, unix-время старта) процесса, который СЛУШАЕТ порт приложения;
    (0, 0.0) — если не слушает никто.

    Опознание по сокету, а не по командной строке. 20.09.2026: бэкенд поднимает
    ЭЛЕВИРОВАННЫЙ лаунчер, и у его дочернего python.exe наш непривилегированный
    CIM-запрос видит CommandLine == null (и ExecutablePath тоже). Поэтому
    `-match 'app\\.py'` перестал совпадать НИ С ЧЕМ — молча, без единой жалобы:
    подсказка «Код новее запущенного app.py» в последний раз сработала для старта
    16.09 15:51 и с тех пор не могла появиться в принципе, а `_heal_app_down`
    считал, что бэкенда нет вообще — то есть на ЖИВОМ старте пошёл бы убивать
    лаунчер и спавнить второй, ровно то, от чего предостерегает комментарий в
    шаге 2. Владелец порта виден независимо от прав: PID и CreationDate отдаются
    и для элевированного процесса.

    Время считаем вычитанием эпохи, а не `Get-Date -UFormat %s`: тот отдаёт
    строку в текущей локали, и `[double]::Parse` на запятичном разделителе врёт.
    """
    try:
        port = int(BASE.rsplit(":", 1)[1])
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"$c = Get-NetTCPConnection -LocalPort {port} -State Listen "
             "-ErrorAction SilentlyContinue | Select-Object -First 1; "
             "if ($c) { $p = Get-CimInstance Win32_Process -Filter "
             "\"ProcessId=$($c.OwningProcess)\"; if ($p) { \"$($p.ProcessId) \" + "
             "[int][Math]::Floor((((Get-Date $p.CreationDate).ToUniversalTime() "
             "- [datetime]'1970-01-01').TotalSeconds)) } }"],
            capture_output=True, text=True, timeout=30, creationflags=CNW)
        pid, _, ts = (r.stdout or "").strip().partition(" ")
        return int(pid or 0), float(ts or 0)
    except Exception:
        return 0, 0.0


def _app_backend_running() -> bool:
    """Есть ли вообще процесс бэкенда — по командной строке ИЛИ по владельцу
    порта. Второе обязательно: под элевированным лаунчером первое слепо
    (см. `_port_owner`), а «бэкенда нет» запускает спавн второго."""
    return _ps_proc_count(r"app\.py") > 0 or _port_owner()[0] > 0


def _app_started_ts() -> float:
    """Когда стартовал ЖИВОЙ app.py (unix-время), 0.0 — если определить не вышло.

    Нужно, чтобы отличать поломки, случившиеся под ТЕКУЩИМ кодом, от тех, что
    произошли до перезапуска и уже исправлены. Без этого суточное окно любой
    проверки держит предупреждение ещё сутки после починки — и следующее
    пробуждение заново выводит диагноз по симптому, которого больше нет.
    """
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "$p = Get-CimInstance Win32_Process -Filter \"Name='python.exe' OR "
             "Name='pythonw.exe'\" | Where-Object { $_.CommandLine -match 'app\\.py' } "
             "| Sort-Object CreationDate | Select-Object -First 1; "
             "if ($p) { [int][double]::Parse((Get-Date $p.CreationDate -UFormat %s)) }"],
            capture_output=True, text=True, timeout=30, creationflags=CNW)
        ts = float((r.stdout or "0").strip() or 0)
    except Exception:
        ts = 0.0
    # Командной строки не видно (элевированный лаунчер) — спрашиваем сокет.
    return ts or _port_owner()[1]


_DETACHED = 0x00000008  # DETACHED_PROCESS
DOCKER_DESKTOP_EXE = Path(r"C:\Program Files\Docker\Docker\Docker Desktop.exe")

# Containers Ripster needs up: Apple decrypt wrapper + local Bot API.
REQUIRED_CONTAINERS = ("amd-wrapper", "tg-bot-api")
BOT_PY = r"C:\Python314\python.exe"


def docker_engine_up(timeout: float = 15) -> bool:
    """Отвечает ли демон: `docker ps` — самый дешёвый честный тест."""
    return _docker("ps", timeout=timeout)[0] == 0


def ensure_docker_engine(notify=fixed, wait_iters: int = 24, delay: float = 5) -> bool:
    """Поднять Docker Desktop, если демон лежит, и дождаться готовности движка.

    После ребута движок не стартует сам, а на нём и wrapper, и tg-bot-api.
    Вызывается и из `_heal_app_down`, и из лончера (Ripster.exe) при старте —
    вторая реализация той же логики означала бы два разных поведения таймаута."""
    if docker_engine_up():
        return True
    if not DOCKER_DESKTOP_EXE.exists():
        warn(f"Нет Docker Desktop по пути {DOCKER_DESKTOP_EXE} — контейнеры не поднимутся")
        return False
    try:
        subprocess.Popen([str(DOCKER_DESKTOP_EXE)], creationflags=CNW | _DETACHED)
    except Exception as e:
        warn(f"Не смог запустить Docker Desktop: {str(e)[:60]}")
        return False
    for _ in range(wait_iters):  # up to ~2 мин на движок
        time.sleep(delay)
        if docker_engine_up(timeout=10):
            notify("Docker Desktop запущен (лежал после ребута)")
            return True
    warn("Docker-движок не ответил за ~2 мин — контейнеры недоступны")
    return False


def running_container_names() -> set:
    rc, out = _docker("ps", "--format", "{{.Names}}")
    return set(out.split()) if rc == 0 else set()


def ensure_container(name: str, notify=fixed) -> bool:
    """Поднять контейнер по имени: `--restart unless-stopped` оживает сам, но
    только если движок был готов раньше него — после ребута порядок обратный."""
    if name in running_container_names():
        return True
    rc, out = _docker("start", name)
    if rc == 0 and name in running_container_names():
        notify(f"Контейнер {name} поднят")
        return True
    return False


def bot_process_count() -> int:
    return _ps_proc_count(r"bot\.py")


def start_bot_process(notify=fixed):
    """Единственный способ поднять tgbot/bot.py — тот же интерпретатор и cwd,
    что у `check_bot`. Лончер берёт его отсюда, чтобы интерпретатор не
    разъезжался с тем, что сторож считает «живым ботом»; handle возвращается,
    чтобы лончер мог следить за процессом, а не только считать его по имени."""
    try:
        proc = subprocess.Popen([BOT_PY, "bot.py"], cwd=str(ROOT / "tgbot"),
                                creationflags=CNW | _DETACHED)
    except Exception as e:
        warn(f"Не смог запустить bot.py: {str(e)[:60]}")
        return None
    time.sleep(4)
    if bot_process_count() > 0:
        notify("Бот (bot.py) перезапущен")
        return proc
    return None


def _heal_app_down() -> bool:
    """Post-reboot recovery: Docker Desktop doesn't autostart and the autostarted
    RipsterLauncher.exe can hang without spawning the backend (seen 2026-07-18).
    Fix: bring Docker up, kill hung launchers, start app.py directly via venv."""
    # 1. Docker engine (wrapper + tg-bot-api live there; restart-policy revives them)
    ensure_docker_engine()
    # 2. Backend already starting? Then only wait, no duplicate spawn.
    #    ВАЖНО: app.py спавнит дочерний python-воркер, который наследует сокет 7799 —
    #    убивать «лишние» app.py по netstat-владельцу нельзя, роняет весь сервис.
    how = ""
    if not _app_backend_running():
        try:
            subprocess.run(["taskkill", "/F", "/IM", "RipsterLauncher.exe"],
                           capture_output=True, timeout=15, creationflags=CNW)
        except Exception:
            pass
        time.sleep(2)
        old = ROOT / "RipsterLauncher.exe"
        py = ROOT / ".venv" / "Scripts" / "python.exe"
        try:
            # Порядок 24.09.2026: раньше первым шёл RipsterLauncher.exe, но это
            # stale-сборка 30.05 (PyQt-эра), которая висит ДО спавна бэкенда —
            # ровно тот hangs, из-за которого хозяин поднимал стек руками
            # (24.09: два её трупа живы с 02:34:16, бэкенда нет). Боевой лончер —
            # Ripster.exe (frozen launcher_exe.py), его и зовём первым.
            exe = ROOT / "Ripster.exe"
            if exe.exists():
                subprocess.Popen([str(exe)], cwd=str(ROOT),
                                 creationflags=CNW | _DETACHED)
                how = "перезапуском Ripster.exe"
            elif py.exists():
                logf = open(ROOT / "logs" / "app_heal.log", "ab")
                subprocess.Popen([str(py), "app.py"], cwd=str(ROOT),
                                 stdout=logf, stderr=subprocess.STDOUT,
                                 creationflags=CNW | _DETACHED)
                how = "прямым запуском .venv app.py"
            elif old.exists():
                # Проверенный путь (2026-07-19): чистый перезапуск лаунчера; спавн
                # бэкенда у него медленный (~2-4 мин), но стек остаётся штатным.
                # С 24.09 — последний шанс: именно этот exe висает без бэкенда.
                subprocess.Popen([str(old)], cwd=str(ROOT),
                                 creationflags=CNW | _DETACHED)
                how = "перезапуском RipsterLauncher.exe"
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
    # 4. Лаунчер завис, так и не запустив app.py (19.09.2026 после ребута: exe жил
    #    7 мин, в launcher.log ни строки). Запасной путь — прямой .venv app.py;
    #    лаунчер при следующем открытии сам подцепится к живому 7799.
    py = ROOT / ".venv" / "Scripts" / "python.exe"
    if how.startswith("перезапуском") and py.exists() and not _app_backend_running():
        try:
            for img in ("Ripster.exe", "RipsterLauncher.exe"):
                subprocess.run(["taskkill", "/F", "/IM", img],
                               capture_output=True, timeout=15, creationflags=CNW)
            logf = open(ROOT / "logs" / "app_heal.log", "ab")
            subprocess.Popen([str(py), "app.py"], cwd=str(ROOT),
                             stdout=logf, stderr=subprocess.STDOUT,
                             creationflags=CNW | _DETACHED)
            for _ in range(60):
                time.sleep(3)
                if _app_alive(timeout=4):
                    fixed("App поднят прямым запуском .venv app.py (лаунчер завис без бэкенда)")
                    return True
        except Exception as e:
            warn(f"Запасной запуск app.py не удался: {str(e)[:60]}")
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


def check_apple_bearer():
    """Рабочий ли developer-bearer лежит в config.yaml (`authorization-token`).

    ЗАЧЕМ ОТДЕЛЬНО. 03.09.2026 Apple отозвал АНОНИМНЫЙ публичный MusicKit
    dev-токен (iss M62YD85FTQ — тот, что exe скрейпит из JS music.apple.com и
    что харвестит браузерное расширение). `ampapi.GetToken()` всё равно
    возвращал этот мёртвый токен без ошибки, фолбэк на config.yaml не
    срабатывал → КАЖДАЯ Apple-загрузка (album + song, гости и владелец) падала
    с замаскированным «error getting album response». Ни одна проверка не
    смотрела, работает ли токен ИЗ config.yaml — а именно его читает Go-exe.

    Само-лечится: `app._apple_bearer_keeper` (цикл 30 мин) + `main.go` теперь
    берёт config-токен ПЕРВЫМ. Плюс `sync_mut_from_wrapper` синкает `dev_token`
    из враппера (:30020, user-scoped, iss UDK28SN10P — переживает ротацию
    публичного) в `authorization-token`. Здесь — верификация и подстраховка на
    случай, если кипер по какой-то причине не крутится."""
    tok = _cfg_get("authorization-token")
    if not tok or tok == "your-authorization-token":
        # config-токен не задан вовсе — exe пойдёт по скрейпу, это отдельный путь
        ok("Apple dev-bearer в config.yaml не задан — exe использует web-скрейп")
        return
    if _dev_token_works(tok):
        ok("Apple dev-bearer (config.yaml) рабочий — catalog API отвечает 200")
        return
    n = _streak("apple_bearer_dead", True)
    warn(f"Apple dev-bearer в config.yaml отклонён Apple (401 на catalog) — "
         f"Go-загрузчик даст 401 на КАЖДУЮ Apple-загрузку.{(' %d-я проверка подряд.' % n) if n >= 2 else ''}")
    if NO_FIX:
        return
    # Тот же путь, что делает app._apple_bearer_keeper: тянет свежий dev_token
    # из враппера в config через sync_mut_from_wrapper (side effect).
    _api("/api/apple/sync-from-wrapper", "POST")
    time.sleep(1)
    tok2 = _cfg_get("authorization-token")
    if tok2 and tok2 != tok and _dev_token_works(tok2):
        _streak("apple_bearer_dead", False)
        fixed("Apple dev-bearer пересинхронизирован из враппера — catalog API снова 200")
    else:
        bad("Apple dev-bearer мёртв И враппер не дал рабочей замены "
            "(враппер лежит / не залогинен) — ручной разбор, skill ripster-apple-wrapper")


def check_apple_pool_slots():
    """Мёртвые слоты мульти-аккаунт пула (rip-wrapper-N): архивировать после
    нескольких подряд неудачных проходов, освободить порт. См. ripster/credential_health.py —
    заведено 29.08.2026 из-за rip-wrapper-3, месяцами зациклённого на 'account disabled'."""
    try:
        sys.path.insert(0, str(ROOT))
        from ripster import credential_health as ch
    except Exception as e:
        warn(f"credential_health недоступен: {e}")
        return
    lines = ch.check_all_apple_slots()
    if not lines:
        ok("Слоты пула Apple живы (или ещё не набрали порог)")
        return
    for line in lines:
        if line.startswith("💀"):
            fixed(line)
        else:
            warn(line)


def check_tidal_accounts():
    """Мёртвые и безлицензионные учётки Tidal (основная + пул).

    Отличие от Deezer: Tidal умеет отвечать «жива, но lossless не отдам» —
    такую учётку снимать нельзя (ею можно качать AAC), но молчать о ней тоже
    нельзя. Ровно так 12.09.2026 выяснилось, что основная учётка этой машины —
    INTRO с истёкшим сроком, и все загрузки Tidal шли через неё.
    """
    try:
        sys.path.insert(0, str(ROOT))
        from ripster import credential_health as ch
    except Exception as e:
        warn(f"credential_health недоступен: {e}")
        return
    # Автозамена основной учётки — это ПОЧИНКА, а не проверка: под --no-fix
    # сторож обязан только рассказать, что нашёл.
    lines = ch.check_all_tidal_accounts(promote=not NO_FIX)
    if not lines:
        ok("Учётки Tidal живы и отдают lossless")
        return
    for line in lines:
        if line.startswith("💀"):
            fixed(line)
        else:
            warn(line)


def check_yandex_tokens():
    """Мёртвые токены Яндекса (основной + пул). 403 = «не смогли спросить»
    (сервис геозависим), и в порог снятия такое не засчитывается."""
    try:
        sys.path.insert(0, str(ROOT))
        from ripster import credential_health as ch
    except Exception as e:
        warn(f"credential_health недоступен: {e}")
        return
    lines = ch.check_all_yandex_tokens()
    if not lines:
        ok("Токены Яндекса живы, Plus на месте")
        return
    for line in lines:
        if line.startswith("💀"):
            fixed(line)
        else:
            warn(line)


def check_deezer_arls():
    """Мёртвые Deezer ARL (основной + пул): та же логика, что check_apple_pool_slots,
    но для Deezer. Проверка — приватный gw-light.php (ripster/deezer_accounts.py),
    архивация/снятие с маршрутизации — ripster/credential_health.py."""
    try:
        sys.path.insert(0, str(ROOT))
        from ripster import credential_health as ch
    except Exception as e:
        warn(f"credential_health недоступен: {e}")
        return
    lines = ch.check_all_deezer_arls()
    if not lines:
        ok("Deezer ARL живы (или ещё не набрали порог)")
        return
    for line in lines:
        if line.startswith("💀"):
            fixed(line)
        else:
            warn(line)


def check_qobuz_accounts():
    """Мёртвые и погасшие учётки Qobuz — то же, что для Deezer ARL.

    04.09.2026, замер по живым учёткам владельца: из восьми три отвечают 401 и
    стоят сразу за основной, у четвёртой подписка истекла 15.08. Лимит попыток
    на задачу — 4, то есть отказ на первой учётке съедал весь бюджет на мёртвых
    и до четырёх рабочих не доходил.

    Снимаются только отвергнутые (401). Истёкшая подписка при валидном токене —
    не повод удалять: её продлевают.
    """
    try:
        sys.path.insert(0, str(ROOT))
        from ripster import credential_health as ch
    except Exception as e:
        warn(f"credential_health недоступен: {e}")
        return
    lines = ch.check_all_qobuz_accounts()
    if not lines:
        ok("Учётки Qobuz живы (или ещё не набрали порог)")
        return
    for line in lines:
        if line.startswith("💀"):
            fixed(line)
        else:
            warn(line)


def check_soundcloud_tokens():
    """Токены SoundCloud: жив ли и есть ли Go+.

    Отдельная проверка нужна потому, что токен БЕЗ подписки полностью валиден —
    `/me` отвечает 200, а HQ-поток не отдаётся. Плюс ловим разные токены одной
    учётки: у владельца 04.09.2026 два из трёх были аккаунтом `goku`.
    """
    try:
        sys.path.insert(0, str(ROOT))
        from ripster import credential_health as ch
    except Exception as e:
        warn(f"credential_health недоступен: {e}")
        return
    lines = ch.check_all_soundcloud_tokens()
    if not lines:
        ok("Токены SoundCloud живы, с Go+")
        return
    for line in lines:
        if line.startswith("💀"):
            fixed(line)
        else:
            warn(line)


def check_account_duplicates():
    """Разные ключи, ведущие в один аккаунт: находим и убираем лишние.

    04.09.2026: в пуле SoundCloud три токена, все живые с Go+ — но два из них
    один аккаунт `goku`. Пул давал две независимые учётки вместо трёх, а при
    отказе «следующая учётка» оказывалась той же самой, и вторая попытка
    гарантированно повторяла первую.

    Удаление здесь автоматическое, но узкое: основанием служит только ответ
    сервиса о том, чей это аккаунт, обе записи должны быть живы, основная не
    трогается никогда, а перед удалением всё измеряется заново (см.
    ripster/dedup_accounts.py). Снятое лежит в реестре и в DEAD_ACCOUNTS.txt —
    вернуть можно.
    """
    try:
        sys.path.insert(0, str(ROOT))
        from ripster import credential_health as ch
        from ripster import dedup_accounts as dd
    except Exception as e:
        warn(f"dedup_accounts недоступен: {e}")
        return
    cfg = ch._load_raw_config()
    if not cfg:
        return
    import asyncio
    for service in ("deezer", "qobuz", "soundcloud"):
        try:
            lines = asyncio.run(dd.apply(cfg, service, confirm=not NO_FIX))
        except Exception as e:
            warn(f"{service}: разбор дублей не прошёл ({type(e).__name__})")
            continue
        for line in lines:
            if line.startswith("💀"):
                fixed(line)
            elif line.startswith("⚠️") or line.startswith("✗"):
                warn(line)
    ok("Дубли учёток проверены")


def check_gamdl_cookies():
    """Отличить ДВА состояния cookies.txt, которые до 01.08.2026 выглядели одинаково.

    gamdl падает с «No active Apple Music subscription», а причины две и лечатся
    по-разному: протухшую СЕССИЮ чинит повторный экспорт, кончившуюся ПОДПИСКУ —
    нет. Разбор переехал в `ripster/apple_cookies.py`: 04.09.2026 выяснилось, что
    правду знал только этот отчёт, а сообщение самого движка всё равно советовало
    «экспортируй куки заново». Теперь источник один.
    """
    try:
        sys.path.insert(0, str(ROOT))
        from ripster import apple_cookies as ac
    except Exception as e:
        warn(f"apple_cookies недоступен: {e}")
        return
    p = Path(_cfg_get("gamdl-cookies-path") or "cookies.txt")
    if not p.is_absolute():
        p = ROOT / p
    if not p.exists():
        return  # gamdl просто не настроен — это не поломка

    # gamdl в wrapper-режиме ходит на 30020 САМ и подстраховаться скрейпом не
    # умеет: протух токен враппера — этот путь отдаст 401 на любом качестве.
    # _cfg_get отдаёт СТРОКУ: "false" тоже истинна, сравниваем по значению.
    dev, dev_src = ac.dev_token()
    if (dev and dev_src != "с враппера 30020"
            and _cfg_get("gamdl-use-wrapper").lower() in ("true", "yes", "1", "on")):
        warn("gamdl wrapper-режим: dev_token на 30020 протух (враппер выдаёт его "
             "один раз при старте, живёт он 5 минут) — качества, идущие через "
             "--wrapper-account-url, получат 401. Лечится только перезапуском "
             "враппера, а он стоит слота устройства: трогать, только если "
             "gamdl реально нужен")

    v = ac.verdict(p)
    st = v.get("state")
    if st == "ok":
        ok(f"gamdl: cookies.txt жив, {v['reason']}")
    elif st == "no_subscription":
        warn(f"gamdl: {v['reason']} — повторный экспорт ТЕХ ЖЕ куки не поможет. "
             f"Нужны куки аккаунта С подпиской либо продлить текущий. Загрузки "
             f"через wrapper (zhaarey/AMD) это не затрагивает")
    elif st == "expired":
        warn(f"gamdl: {v['reason']} — экспортируй cookies.txt заново из браузера "
             f"с активной подпиской")
    elif st == "no_token":
        warn(f"gamdl: {v['reason']} — экспортируй заново из залогиненного "
             f"music.apple.com")
    else:
        # Без ЗАВЕДОМО живого dev_token любой 401 неотличим от «протухли куки» —
        # ровно тот ложный совет, ради которого проверка писалась. Молчим честно.
        warn(f"gamdl: {v.get('reason', 'состояние не проверено')} — выводов не делаем")

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
    # БЕЗ `--restart` — той же причиной, по которой его убрал `ripster/amd.py`:
    # пересозданный движком после ребута контейнер поднимается без нашей правки
    # `-p`, и если когда-нибудь вернётся старая форма с голым портом, дыра
    # откроется сама, без единой нашей проверки. Что ПРОВЕРЕНО сегодня на
    # engine 29.6.2: `docker restart` HostIp НЕ теряет (петля остаётся); путь
    # полного ребута машины не проверялся — он и не нужен, раз враппер поднимаем
    # сами: и `ensure_container`, и `check_wrapper_exposure` ниже.
    rc, out = _docker(
        "run", "-d", "--name", "amd-wrapper",
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


# ── Экспозиция портов враппера: только петля, и firewall как гарант ──────────
# 21.09.2026 `docker port amd-wrapper` показал 0.0.0.0 на 10020/20020/30020, а
# 30020 БЕЗ АВТОРИЗАЦИИ отдаёт `dev_token`, `storefront_id` и media-user-token
# активной учётки: любой в локальной сети получал живую сессию Apple владельца.
# Мерили со СВОЕЙ сетевой стеки (VM docker-desktop, `nc` до 192.168.1.98): с
# включённым правилом все три порта закрыты; с выключенным — ОТКРЫТЫ, и GET на
# 30020 возвращает токены. Контроль (свой слушатель на 14020: открыт → правило
# Block → закрыт → правило сняли → снова открыт) доказал, что закрывает именно
# правило, а не маршрутизация.
#
# ПОЧЕМУ ДВА ЗАМКА, А НЕ ОДИН. Замерено здесь же, на Docker Desktop 4.83 /
# engine 29.6.2: `-p 127.0.0.1:PORT:PORT` работает — и `docker port`, и `netstat`
# дают петлю, и `docker restart` её сохраняет. Прежнее заключение «докер всё
# равно публикует на 0.0.0.0 независимо от хоста» было снято с контейнера,
# который пересоздало приложение своей старой командой: петлю не ломает докер,
# её ПЕРЕЗАПИСЫВАЕТ тот, кто запускает враппер без хоста. Поэтому `-p` — замок
# основной, а firewall — второй, на случай такого перезаписывания (и он же
# накрывает слоты пула, до которых `amd.py` не касается вовсе).
WRAPPER_GUARD_DISPLAY_NAME = "Ripster: Apple wrapper only local"
# Имя правила: им и ищем, DisplayName человек может и подправить.
WRAPPER_GUARD_RULE_NAME = "Ripster Apple wrapper only local"
# Слоты пула (`wrapper_pool`) сидят на 10020+i / 20020+i, i — номер учётки;
# учёток со временем больше, поэтому диапазон, а не три точечных порта.
WRAPPER_GUARD_PORT_SPECS = ("10020-10049", "20020-20049", "30020")
WRAPPER_CONTAINER_PREFIXES = ("amd-wrapper", "rip-wrapper-")
# Порты основного враппера — то, что обязано быть закрыто безусловно.
WRAPPER_CORE_PORTS = (10020, 20020, 30020)


def _expand_port_specs(specs) -> set:
    """("10020-10049", "30020") -> {10020..10049, 30020}.

    Firewall отдаёт LocalPort то списком портов, то диапазонами — сравнивать
    строки наивным `==` нельзя, покрытие проверяем по множеству."""
    out = set()
    for s in specs or ():
        s = str(s).strip()
        if not s:
            continue
        if "-" in s:
            a, _, b = s.partition("-")
            try:
                out.update(range(int(a), int(b) + 1))
            except ValueError:
                pass
        elif s.isdigit():
            out.add(int(s))
    return out


def _is_loopback(host: str) -> bool:
    """Пустой хост — это НЕ петля: docker трактует "" как «все интерфейсы».

    Именно на этом и погорели первый раз: HostIp "" в `docker inspect` выглядит
    как «что-то дефолтное», а по факту это 0.0.0.0."""
    h = (host or "").strip().strip("[]")
    return h == "localhost" or h.startswith("127.") or h == "::1"


def _parse_docker_port(text: str) -> dict:
    """`docker port NAME` -> {порт_контейнера: (порт_хоста, интерфейс_хоста)}.

    На один контейнерный порт приходится по строке на стек (v4 и v6); держим
    ХУДШИЙ интерфейс: если хоть один из них не петля — порт смотрит наружу."""
    out: dict = {}
    for line in (text or "").splitlines():
        m = re.match(r"^\s*(\d+)/\w+\s+->\s+(.+?):(\d+)\s*$", line)
        if not m:
            continue
        cport, host, hport = int(m.group(1)), m.group(2).strip(), int(m.group(3))
        worst = host if _is_loopback(host) else (host or "0.0.0.0")
        cur = out.get(cport)
        if cur is None or (not _is_loopback(cur[1]) and _is_loopback(worst)):
            out[cport] = (hport, worst)
    return out


def _wrapper_containers() -> list:
    rc, out = _docker("ps", "--format", "{{.Names}}")
    if rc != 0:
        return []
    return [n for n in out.split() if n.startswith(WRAPPER_CONTAINER_PREFIXES)]


def _dec_port(name: str) -> dict:
    rc, out = _docker("port", name)
    return _parse_docker_port(out) if rc == 0 else {}


def _run_spec_from_inspect(js: dict) -> dict:
    """Что передать `docker run`, чтобы поднять тот же контейнер заново.

    Пересоздание — единственный способ переписать публикацию портов, и оно
    ОБЯЗАНО сохранить всё остальное один-в-один: mounts — это персональная
    identity-папка учётки (`rootfs_id/<login>`), спутать её с `rootfs_working`
    значит угнать аккаунт в «device limit» (см. skill ripster-apple-wrapper).
    `--restart` сознательно НЕ восстанавливаем: см. комментарий в `_heal_wrapper`.
    """
    hc = js.get("HostConfig") or {}
    cfg = js.get("Config") or {}
    return {
        "image": cfg.get("Image") or "",
        "binds": list(hc.get("Binds") or []),
        "env": [e for e in (cfg.get("Env") or []) if not e.upper().startswith("PATH=")],
        "entrypoint": list(cfg.get("Entrypoint") or []),
        "cmd": list(cfg.get("Cmd") or []),
    }


def _republish_cmd(name: str, spec: dict, bindings: dict) -> list:
    """Команда перепубликации: тот же контейнер, но хост-интерфейс — петля."""
    cmd = ["docker", "run", "-d", "--name", name]
    # Entrypoint проставляем явно, когда он был: иначе пересозданный контейнер
    # возьмёт entrypoint образа, а не то, чем его реально запускали.
    if spec.get("entrypoint"):
        cmd += ["--entrypoint", spec["entrypoint"][0]]
    for b in spec.get("binds") or []:
        cmd += ["-v", b]
    for e in spec.get("env") or []:
        cmd += ["-e", e]
    for cport in sorted(bindings):
        cmd += ["-p", f"127.0.0.1:{bindings[cport][0]}:{cport}"]
    if spec.get("image"):
        cmd.append(spec["image"])
    # При явном `--entrypoint` всё, что было в Entrypoint после программы,
    # становится аргументами команды — иначе они теряются.
    cmd += spec.get("entrypoint", [])[1:] + (spec.get("cmd") or [])
    return cmd


def _heal_publish(name: str, bindings: dict) -> bool:
    """Пересоздать контейнер на петле и ПЕРЕМЕРИТЬ, что получилось.

    Без повторного замера эта функция однажды отрапортовала «перепубликован
    на 127.0.0.1» на машине, где порт в тот же момент снова смотрел на 0.0.0.0:
    `docker run` прошёл успешно, враппер ответил с петли — а команду пересоздания
    выиграло у нас приложение, которое увидело, что враппер умер, и подняло его
    своей старой командой (процесс запущен ДО правки `amd.py`, код на диске уже
    новый). Зелёный отчёт при открытой дыре — худший из возможных исходов
    самопочинки, поэтому верим только `docker port` после всех событий.
    """
    rc, out = _docker("inspect", name, "--format", "{{json .}}")
    if rc != 0:
        bad(f"Не смог прочитать конфиг контейнера {name} — порты правлю вручную")
        return False
    try:
        spec = _run_spec_from_inspect(json.loads(out.strip()))
    except Exception:
        bad(f"Не распознал конфиг контейнера {name} — порты правлю вручную")
        return False
    cmd = _republish_cmd(name, spec, bindings)
    _docker("rm", "-f", name, timeout=60)
    rc, out = _docker(*cmd[1:], timeout=120)
    if rc != 0:
        # Команду не эхом: в env лежит `-L логин:пароль`.
        bad(f"Перепубликация {name} не удалась: {out[:140]} — подними враппер "
            f"вручную (образ {spec.get('image') or '?'}"
            f"{', тома: ' + '; '.join(spec.get('binds') or []) if spec.get('binds') else ''})")
        return False
    # Ждём не «порт 10020 отвечает», а что ИМЕННО ЭТОТ контейнер поднялся: у
    # слотов пула свои порты (10021, 10022…), а на 10020 отвечает основной
    # враппер — проверка не по своему контейнеру дала бы «ожил» там, где слот
    # лежит мёртвый.
    after: dict = {}
    for _ in range(20):
        time.sleep(2)
        rc2, st = _docker("inspect", "-f", "{{.State.Running}}", name)
        after = {cp: hp for cp, hp in _dec_port(name).items() if not _is_loopback(hp[1])}
        if st.strip() == "true" and not after:
            fixed(f"{name}: порты перепубликованы на 127.0.0.1 "
                  f"(было {sorted({h for _p, h in bindings.values()})})")
            return True
        if after:
            break
    bad(f"{name}: пересоздал на 127.0.0.1, а {sorted(after) or '?'} СНОВА опубликованы "
        f"наружу ({sorted({h for _p, h in after.values()}) or '?'}) — враппер поднимает "
        "процесс, запущенный до правки `ripster/amd.py`: код на диске верный, нужен "
        "перезапуск app.py (сам не перезапускаю). До него порты держит только "
        "правило firewall.")
    return False


def _lan_ipv4() -> str:
    """Адрес карты, у которой есть маршрут по умолчанию (не петли и не APIPA)."""
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-NetIPConfiguration | Where-Object { $_.IPv4DefaultGateway } | "
             "ForEach-Object { $_.IPv4Address.IPAddress }"],
            capture_output=True, text=True, timeout=30, creationflags=CNW)
        for ip in (r.stdout or "").split():
            ip = ip.strip()
            if ip and not ip.startswith("127.") and not ip.startswith("169.254."):
                return ip
    except Exception:
        pass
    return ""


def _offhost_reachable(ip: str, port: int):
    """Достигается ли порт С ДРУГОЙ сетевой стеки. None — спросить нечем.

    Мерить с самого хоста бессмысленно: пакет на собственный LAN-адрес уходит
    внутренним маршрутом и firewall его не видит вовсе — на этом и обжигались
    («из той же машины всё закрыто» оказалось неверным выводом). Поэтому
    спрашиваем VM Docker Desktop: отдельное ядро Linux со своим `nc`.
    """
    if not ip:
        return None
    try:
        r = subprocess.run(
            ["wsl", "-d", "docker-desktop", "-e", "sh", "-c",
             f"nc -z -w2 {ip} {port}"],
            capture_output=True, text=True, timeout=25, creationflags=CNW)
    except Exception:
        return None
    out = ((r.stdout or "") + (r.stderr or "")).lower()
    if r.returncode == 0:
        return True
    if "does not exist" in out or "could not be started" in out or "not found" in out:
        return None            # ни дистрибутива, ни nc нет — это «не знаю», а не «закрыто»
    return False


def _firewall_guard_state() -> dict:
    """{known, exists, enabled, action, ports} — что реально заведено в firewall."""
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"$r = @(Get-NetFirewallRule -DisplayName '{WRAPPER_GUARD_DISPLAY_NAME}' "
             "-ErrorAction SilentlyContinue); if (-not $r) { 'MISSING' } else { "
             "$p = ($r | Get-NetFirewallPortFilter -ErrorAction SilentlyContinue).LocalPort "
             "-join ','; "
             "$e = if (@($r | Where-Object { $_.Enabled -eq 'True' }).Count) {'on'} else {'off'}; "
             "$a = if (@($r | Where-Object { $_.Action -eq 'Block' -and $_.Direction -eq "
             "'Inbound' }).Count) {'block'} else {'wrong'}; \"$e|$a|$p\" }"],
            capture_output=True, text=True, timeout=40, creationflags=CNW)
    except Exception:
        return {"known": False, "exists": False, "enabled": False, "ports": set()}
    txt = (r.stdout or "").strip()
    if not txt or "MISSING" in txt:
        return {"known": r.returncode == 0, "exists": False,
                "enabled": False, "ports": set()}
    if "|" not in txt:
        return {"known": False, "exists": False, "enabled": False, "ports": set()}
    enabled, action, ports = (txt.split("|") + ["", "", ""])[:3]
    return {"known": True, "exists": True, "enabled": enabled == "on",
            "action": action, "ports": _expand_port_specs(ports.split(","))}


def _ensure_firewall_guard() -> tuple:
    """Завести (пере)создать Block-правило. (ok, чем_закончилось).

    Старое правило с тем же именем сначала снимается: иначе их накапливается
    несколько, и «починили» превращается в «завели ещё одно поверх»."""
    ps = ("Remove-NetFirewallRule -DisplayName "
          f"'{WRAPPER_GUARD_DISPLAY_NAME}' -ErrorAction SilentlyContinue | Out-Null; "
          f"New-NetFirewallRule -Name '{WRAPPER_GUARD_RULE_NAME}' "
          f"-DisplayName '{WRAPPER_GUARD_DISPLAY_NAME}' -Group 'Ripster' "
          "-Direction Inbound -Action Block -Protocol TCP "
          f"-LocalPort {','.join(WRAPPER_GUARD_PORT_SPECS)} -Profile Any | Out-Null")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, text=True, timeout=90, creationflags=CNW)
    except Exception as e:
        return False, f"PowerShell не ответил: {str(e)[:60]}"
    if r.returncode != 0:
        err = ((r.stdout or "") + (r.stderr or "")).strip()
        low = err.lower()
        if "access" in low or "denied" in low or "отказ" in low or "прав" in low:
            return False, ("нужны права администратора; выполни в повышенной "
                           "консоли: " + ps)
        return False, err[:160]
    return True, "правило заведено заново"


def check_wrapper_exposure():
    """Порты Apple-враппера доступны ТОЛЬКО с петли — и это подкреплено правилом.

    Три независимых измерения, потому что каждое ломается само по себе и молча:
      1. как контейнер ОПУБЛИКОВАН (`docker port`: хост-интерфейс);
      2. есть ли правило firewall, которое реально закрывает порты;
      3. достижим ли порт с другой сетевой стеки (если спросить чем).
    Расхождение чинит сам: машина, которая уже лежит с дырой, обязана прийти в
    норму без единого действия владельца.

    Почему firewall нужен, если верный `-p 127.0.0.1:` тоже работает (замерено
    21.09.2026: nginx с `-p 127.0.0.1:18080:80` даёт петлю и в `docker port`, и
    в PortBindings, и в netstat; `docker restart` HostIp не теряет). Затем, что
    дыра пришла НЕ из докера, а из процесса приложения, запущенного ДО правки
    `ripster/amd.py`: он пересоздаёт контейнер своей старой командой с голым
    портом, и делает это через несколько секунд после нашего `docker rm -f`.
    Правило firewall держит порты закрытыми и в тот момент, и при любом чужом
    автоподъёме — это второй замок, а не единственный.
    """
    need = _expand_port_specs(WRAPPER_GUARD_PORT_SPECS)

    # ── firewall чиним ПЕРВЫМ: это единственный работающий замок, и лезть в
    #    живые контейнеры без приспущенного занавеса — значит на несколько
    #    секунд открыть токены наружу там, где они были закрыты.
    guard = _firewall_guard_state()
    problem = ""
    if not guard["known"]:
        problem = "не смог прочитать firewall"
    elif not guard["exists"]:
        problem = "правило не заведено"
    elif not guard["enabled"]:
        problem = "правило выключено"
    elif guard.get("action") != "block":
        problem = "правило не Block/Inbound"
    elif not need.issubset(guard["ports"]):
        miss = sorted(need - guard["ports"])
        problem = (f"из-под правила выпадают {miss[0]}…{miss[-1]} "
                   f"({len(miss)} портов, всего накрыто {len(guard['ports'])})")
    if problem:
        warn(f"Firewall-защита портов враппера не работает: {problem}")
        if not NO_FIX and guard["known"]:
            ok_, what = _ensure_firewall_guard()
            after = _firewall_guard_state()
            if ok_ and after["exists"] and after["enabled"] and need.issubset(after["ports"]):
                fixed(f"Правило «{WRAPPER_GUARD_DISPLAY_NAME}»: {what}")
            elif ok_:
                bad(f"Правило пересоздал, но проверить не смог ({what}) — "
                    f"глянь Get-NetFirewallRule -DisplayName '{WRAPPER_GUARD_DISPLAY_NAME}'")
            else:
                bad(f"Не смог завести правило firewall ({what}) — "
                    "порты враппера открыты всей сети, нужен админ")
    else:
        ok(f"Firewall: порты враппера закрыты извне правилом "
           f"«{WRAPPER_GUARD_DISPLAY_NAME}»")

    # ── публикация контейнеров
    exposed: dict = {}
    seen = 0
    for name in _wrapper_containers():
        seen += 1
        bad_ports = {cp: hp for cp, hp in _dec_port(name).items()
                     if not _is_loopback(hp[1])}
        if bad_ports:
            exposed[name] = bad_ports
    if exposed:
        warn("Порты враппера опубликованы наружу: "
             + "; ".join(f"{n} → {sorted(p)}" for n, p in exposed.items()))
        if not NO_FIX:
            for name, ports in exposed.items():
                _heal_publish(name, ports)
    elif seen:
        ok(f"Порты {seen} враппер-контейнеров(а) опубликованы на петлю")

    # ── живая проверка извне (ничего не чинит, только верит факту)
    ip = _lan_ipv4()
    if ip:
        verdicts = {p: _offhost_reachable(ip, p) for p in WRAPPER_CORE_PORTS}
        reachable = [p for p, v in verdicts.items() if v is True]
        unknown = [p for p, v in verdicts.items() if v is None]
        if reachable:
            bad(f"С внешней стеки ({ip}, VM docker-desktop) ДОСТУПНЫ порты "
                f"{reachable} — сессия Apple утекает в сеть прямо сейчас")
        elif unknown:
            warn(f"Проверить досягаемость портов враппера извне нечем "
                 f"(в VM docker-desktop нет nc/дистрибутива) — полагаюсь на "
                 "docker port + firewall")
        else:
            ok(f"Проверено извне ({ip}): порты 10020/20020/30020 недоступны")


def check_public_wrapper():
    """Публичный wrapper-manager — четыре разных состояния вместо одной точки.

    Повод. Проверка здоровья считала «здорово» ЛЮБОМУ HTTP-коду ниже 500, а
    wm.wol.moe перешёл на HTTP и закрылся API-ключом: в ответ — 401. То есть
    мы рапортовали порядок ровно тогда, когда путь был мёртв, и никакой
    автохил тут бессилен: чужой сервис нам не подчиняется.

    Поэтому отчёт называет причину своими словами и различает четыре случая:
    работает / жив, но нас не пускает / молчит / не настроен. Строка в
    настройках (static/views/settings.html) читает ТОТ ЖЕ провод, так что
    расходиться им нечем.
    """
    st, js = _api("/api/amd/wrapper-status")
    if not isinstance(js, dict):
        bad(f"Наш сервер не ответил на /api/amd/wrapper-status: HTTP {st} "
            f"{_esc(str(js)[:80])}")
        return
    state = str(js.get("state") or "")
    if not state:
        # Старый процесс на новом диске: кода четырёх состояний в нём нет, и
        # врать про «работает/не работает» этот отчёт не вправе.
        warn("Приложение отдаёт /api/amd/wrapper-status БЕЗ поля `state` — код на "
             "диске новый, процесс старый. Нужен перезапуск app.py (сам не "
             "перезапускаю); до него статус публичного враппера непроверяем")
        return
    reason = str(js.get("reason") or "")
    detail = str(js.get("detail") or "")[:120]
    latch = ""
    if js.get("down"):
        latch = f"; снят с маршрутов, следующая проба через {js.get('next_probe_in')} с"
    chosen = str(_cfg_get("apple-wrapper") or "").strip().lower() == "public"

    if state == "working":
        ok(f"Публичный wrapper: готов, клиентов в пуле {js.get('client_count')}, "
           f"регионы {','.join(map(str, js.get('regions') or [])) or '—'}{latch}")
    elif state == "not_configured":
        warn("Публичный wrapper не настроен: пуст `amd-instance-url`. "
             "На «Публичный» режим автоматически не переключаем — "
             "настройка на месте, если он вообще нужен")
    elif state == "unreachable":
        line = f"Публичный wrapper не отвечает ({reason})"
        (bad if chosen else warn)(line + (f": {detail}" if detail else "") + latch)
    elif state == "refusing":
        # Все ветки «refusing» — это ОНИ, а не мы. Ключа у нас нет и раздобыть
        # его автохилом нельзя; выдумывать тут «починили» означало бы писать
        # в отчёт то, чего не было.
        why = {
            "api_key": "сервер требует API-ключ, которого у нас нет",
            "pool_empty": "в чужом пуле нет готовых аккаунтов",
            "no_status": "не узнаём их /status — upstream снова сменил протокол",
        }.get(reason, f"HTTP-отказ ({reason or '?'})")
        line = f"Публичный wrapper жив, но нас не обслуживает: {why}"
        (bad if chosen else warn)(line + (f". Ответ: {detail}" if detail else "") + latch)
    else:
        warn(f"Публичный wrapper: незнакомое состояние `{state}` от нашего же API "
             "— проверка больше не вслепую, но и вывода не даёт")


def check_tokens():
    """Есть ли у сервиса ДОЛГОЖИВУЩИЙ ключ — а не лежит ли в конфиге кэш.

    ПОЧЕМУ КОРТЕЖ, А НЕ ОДИН КЛЮЧ. 12.09.2026 сводка сказала «Токены
    отсутствуют: Tidal — вставь через бот /set», хотя вставлять было нечего:
    `tidal-token` это access-токен на 4 часа (в тот раз выписан 20:01:02,
    годен до 00:01:02), он пуст всегда, кроме как сразу после обновления.
    Долгоживёт `tidal-refresh` (без exp) — он и лежал на месте, и через
    несколько секунд ТОТ ЖЕ пробег сам выписал по нему свежий access
    (`check_engine_probe` дёргает живое приложение, оно рефрешит сессию).
    То есть проверка ругалась на кэш и просила владельца руками починить то,
    что чинится само. Правило: сторож смотрит на то, ЧЕГО НЕЛЬЗЯ ПОЛУЧИТЬ
    ЗАНОВО. Сервис считается заряженным, если есть ХОТЬ ОДИН ключ из кортежа.
    """
    checks = {
        ("qobuz-auth-token",): "Qobuz",
        ("deezer-arl",): "Deezer",
        # refresh — долгоживущий, tidal-accounts — пул; access-токен НЕ считаем
        ("tidal-refresh", "tidal-accounts", "tidal-token"): "Tidal",
        ("spotify-sp-dc",): "Spotify",
        ("soundcloud-oauth-token",): "SoundCloud",
        ("yandex-token",): "Yandex",
    }
    missing = [name for keys, name in checks.items()
               if not any(_cfg_get(k) for k in keys)]
    if missing:
        warn(f"Токены отсутствуют: {', '.join(missing)} — вставь через бот /set")
    else:
        ok("Токены всех сервисов на месте")


def check_token_files():
    """Файл в tokens/, который приложение молча НЕ применяет.

    `config_service._load_token_files` требует `key: value` и правильно
    отказывается есть что-то другое — но говорит об этом строкой в stderr. Её
    никто не читает: `tokens/soundcloud.yaml` пролежал так с 01.06 по 14.08.2026
    (в него вписали логин и пароль двумя строками), и всё это время он выглядел
    как настроенный токен, а был пустым местом.

    Не чиним автоматически СПЕЦИАЛЬНО: угадывать, какой ключ имелся в виду для
    руками вписанного секрета, — это записать чужую строку не в то поле. Наше
    дело — чтобы владелец про это узнал.

    ВАЖНО про формулировку. Первая версия писала «Файлы токенов игнорируются —
    приложение их НЕ применяет», и это прочли как «SoundCloud не работает», хотя
    рабочий токен лежал в config.yaml, был жив (Go+) и качал с расшифровкой DRM.
    Тревога была формально верной и по смыслу ложной. Поэтому теперь мы СНАЧАЛА
    смотрим, настроен ли сервис в config.yaml, и различаем два разных случая:
    лишний файл при рабочем сервисе — это уборка, а не авария.
    """
    import datetime as _dt
    import re as _re2
    tdir = ROOT / "tokens"
    if not tdir.is_dir():
        ok("Папки tokens/ нет — всё из config.yaml")
        return
    # Без pyyaml (у этой проверялки его сознательно нет): смотрим ВЕРХНЕУРОВНЕВЫЕ
    # строки. У отображения каждая начинается с `ключ:`; строки с отступом и
    # элементы списка — продолжения значения и в счёт не идут.
    _KEY = _re2.compile(r"^[\w.\-]+\s*:")
    # Какой ключ config.yaml делает сервис рабочим БЕЗ файла в tokens/.
    _CFG_KEY = {
        "soundcloud": "soundcloud-oauth-token", "qobuz": "qobuz-auth-token",
        "tidal": "tidal-token", "deezer": "deezer-arl",
        "apple": "media-user-token", "yandex": "yandex-token",
        "tl1001": "tl1001-email",
    }
    bad, cosmetic = [], []
    for tf in sorted(tdir.glob("*.yaml")):
        try:
            lines = tf.read_text(encoding="utf-8-sig").splitlines()
        except Exception as e:
            bad.append(f"{tf.name} (не читается: {str(e)[:40]})")
            continue
        top = [ln for ln in lines
               if ln.strip() and not ln.lstrip().startswith(("#", "-"))
               and not ln[:1].isspace()]
        if top and not any(_KEY.match(ln) for ln in top):
            age = ""
            try:
                d = _dt.date.fromtimestamp(tf.stat().st_mtime)
                age = f", с {d.strftime('%d.%m.%Y')}"
            except Exception:
                pass
            # Сервис уже настроен в config.yaml? Тогда файл — мусор, а не поломка.
            cfg_key = _CFG_KEY.get(tf.stem)
            if cfg_key and str(_cfg_get(cfg_key) or "").strip():
                cosmetic.append(f"{tf.name}{age}")
            else:
                bad.append(f"{tf.name} (не key: value{age})")
    if bad:
        warn(f"Сервис настроен ТОЛЬКО файлом, а файл не читается: {'; '.join(bad)}"
             f" — нужен формат 'ключ: значение', иначе сервис без токена")
    elif cosmetic:
        # Не warn: ничего не сломано, чинить нечего, а красная строка про рабочий
        # сервис приучает не читать отчёт.
        ok(f"Лишние файлы в tokens/ (сервис работает из config.yaml, файл не "
           f"читается и не нужен): {'; '.join(cosmetic)}")
    else:
        ok(f"Файлы токенов читаются ({len(list(tdir.glob('*.yaml')))} шт.)")


def check_spotify_bearer():
    """Свежесть web-player Bearer'а Spotify — а не «лежит ли файл».

    ЗАЧЕМ ОТДЕЛЬНАЯ ПРОВЕРКА. 10.08.2026 Bearer не обновлялся 6 часов (радар и
    OGG-загрузки мертвы), а сводка была зелёной: `check_tokens` смотрит поля
    конфига, `check_engine_probe` — свои пробы, и ни одна не знает про файл
    `orpheus/config/spotify-token.txt`, который живёт ~60 минут. Нашлось только
    ручным разбором бакета ошибок. Причина была в интерпретаторе: app.py
    перезапустили под `C:\\Python314`, где нет `librespot`, и минт молча падал.
    Кипер теперь сам выбирает интерпретатор, но проверка ГОДНОСТИ ПО ВОЗРАСТУ
    нужна независимо от причины — она поймает и следующую, ещё неизвестную.
    """
    tok = ROOT / "orpheus" / "config" / "spotify-token.txt"
    blob = ROOT / "orpheus" / "config" / ".librespot_cache" / "reusable_credentials.json"
    if not blob.exists():
        return                      # не спарен — кипер намеренно спит, это не поломка
    age = (time.time() - tok.stat().st_mtime) / 60 if tok.exists() else 1e9
    if age < 55:
        ok(f"Spotify Bearer свежий ({int(age)} мин из ~60) — радар и OGG живы")
        return
    stale = "отсутствует" if age > 1e8 else f"протух ({int(age)} мин, живёт ~60)"
    if NO_FIX:
        warn(f"Spotify Bearer {stale} — радар и OGG-загрузки не работают")
        return
    try:
        sys.path.insert(0, str(ROOT))
        import asyncio as _a
        from ripster import spotify_token_keeper as _k
        got = _a.run(_k.mint_now(ROOT))
    except Exception as e:
        got = False
        _last = f"{type(e).__name__}: {e}"
    else:
        _last = ""
    if got and tok.exists() and (time.time() - tok.stat().st_mtime) < 300:
        fixed(f"Spotify Bearer перевыпущен (был {stale})")
    else:
        bad(f"Spotify Bearer {stale}, перевыпустить НЕ удалось{(' — ' + _last) if _last else ''}. "
            f"Радар/OGG стоят. Проверь `librespot` хотя бы в одном интерпретаторе "
            f"(.venv) и блоб: tools/spotify_pair.py")


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

    # Отказы, при которых ЗАГРУЗКА продолжает работать: у неё свой путь, а
    # упала лишь метадата-часть. Валить их в «НЕ РАБОТАЮТ» — враньё прибора:
    # 23.08.2026 проба писала «apple: сервис не работает», пока ночью Apple
    # выкачал 29/29 треков без единой ошибки. Веб-API Apple отказывает адресу
    # (401 даже на запрос БЕЗ токенов), и сама проба это честно объясняет —
    # объяснение просто не доживало до отчёта.
    _DEGRADED_KEYS = {"pr.ap_api_blocked"}

    down, degraded = [], []
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
        # 70 символов обрезали объяснение ровно на полуслове — владелец читал
        # «контрольный запрос БЕЗ токенов » и терял вторую половину фразы,
        # ту самую, которая меняет вердикт. Хвост дороже краткости.
        line = f"{name}: {err[:200]}"
        if str(s.get("error_key") or "") in _DEGRADED_KEYS:
            degraded.append(line)
        else:
            down.append(line)
    if down:
        warn("Сервисы НЕ РАБОТАЮТ (живая проба): " + " · ".join(down))
    if degraded:
        warn("Метаданные недоступны, ЗАГРУЗКА работает: " + " · ".join(degraded))
    if not down and not degraded:
        ok(f"Сервисы отвечают по-настоящему ({len(services)} проверено)")


def check_download_canary():
    """Отвечать и ОТДАВАТЬ ФАЙЛ — разные вопросы; probe-all отвечает на первый.

    24.09.2026 Apple 12 часов отвечала Invalid CKC на каждый релиз: все пробы
    «зелёные», скачать нельзя ничего. Канарейка (tools/download_canary.py)
    качает по треку каждого настроенного сервиса каждые 3 часа; здесь она
    ТОЛЬКО читается — ничего не перекачивается ради отчёта.
    """
    try:
        st = json.loads(CANARY_STATE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        warn("Canary: состояния нет — фоновый цикл в приложении не запущен?")
        return
    except Exception as e:
        warn(f"Canary: состояние не читается ({str(e)[:80]})")
        return
    svc = st.get("services") or {}
    if not svc:
        warn("Canary: пусто — ни одного прохода не было"); return
    last = str(st.get("last_run") or "")
    stale = ""
    try:
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
        age_h = (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0
        if age_h > 8:
            stale = f" · ПОСЛЕДНИЙ ПРОХОД {age_h:.0f} ч назад (цикл мог умереть)"
    except Exception:
        stale = " · метка времени не читается"
    dead = {s: v for s, v in sorted(svc.items()) if not v.get("ok")}
    if dead:
        parts = [f"{s}: {v.get('streak_fail', '?')} пад. подряд с {str(v.get('since'))[:16]}"
                 f" — {str(v.get('last_error') or '')[:90]}" for s, v in dead.items()]
        bad("Canary — скачивание мертво: " + " · ".join(parts) + stale)
    else:
        ok(f"Canary: {len(svc)} сервис(ов) реально скачали свой трек"
           f" (последний проход {last[:16]}{stale})")


def _trust_ctx():
    """Контекст проверки TLS на СВЕЖЕМ наборе корней (Mozilla/certifi).

    В хранилище Windows на этой машине лежит ПРОСРОЧЕННАЯ кросс-подписанная
    копия ISRG Root X2 (истекла 15.09.2025), и OpenSSL строит цепочку через
    неё вместо валидной (через ISRG Root X1, живой до 2032) — нормальный
    сертификат Let's Encrypt проверяется как "certificate has expired", хотя
    любой браузер (CryptoAPI) его принимает. У certifi этой копии нет.
    None = certifi недоступен, проверяем штатным хранилищем.
    """
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return None


def _serveo_sshd_note() -> str:
    """Tell serveo's OWN ssh daemon being sick apart from our network blocking 22.

    08.08.2026: serveo.net:22 completed the TCP handshake and then sent no SSH
    banner at all for 10s, while a control host answered in ~2s — so outbound 22
    is open from here and it is their sshd that is degraded. Silence-after-accept
    is the cheapest positive proof it is their side, not ours."""
    try:
        with socket.create_connection(("serveo.net", 22), timeout=10) as s:
            s.settimeout(10)
            try:
                banner = s.recv(48)
            except Exception:
                banner = b""
    except Exception:
        return "; serveo.net:22 не принимает вовсе (похоже на нашу сеть)"
    if not banner.startswith(b"SSH-"):
        return "; serveo.net:22 принимает соединение, но НЕ шлёт SSH-баннер — болен их sshd"
    return ""


def _tunnel_probe(url: str) -> int:
    """HTTP-код туннеля, 0 = соединение не состоялось."""
    try:
        req = urllib.request.Request(url, method="GET", headers={"User-Agent": "hc"})
        with urllib.request.urlopen(req, timeout=12, context=_trust_ctx()) as r:
            return r.status
    except urllib.error.HTTPError as he:
        return he.code
    except Exception:
        return 0


def _tunnel_restart_window() -> str:
    """Была ли за последние 5 минут пересборка туннеля после старта приложения.

    15.08.2026. Каждый запуск app.py зовёт auto_start_tunnel(), а прежняя
    ssh-сессия ещё удерживает поддомен, поэтому новая падает с 'remote port
    forwarding failed for listen port 80'; сторож поднимает связь секунд через
    20. Всё это время край serveo отвечает 502 — апстрима у него нет. Это окно
    перезапуска, а не авария, и путать их нельзя."""
    log = ROOT / "tunnel.log"
    try:
        tail = log.read_text(encoding="utf-8", errors="replace").splitlines()[-40:]
    except Exception:
        return ""
    now = datetime.now()
    for line in reversed(tail):
        if "listen port 80" not in line and "tunnel down" not in line:
            continue
        # 14.09.2026. «tunnel down → respawning (попытка 5…)» — это сторож,
        # который уже час долбится в лежащий serveo, а не пересборка после
        # старта приложения. Сводка назвала часовую аварию «окном перезапуска»
        # только потому, что пятая попытка пришлась на те же 5 минут.
        m = re.search(r"попытка (\d+)", line)
        if m and int(m.group(1)) >= 2:
            return ""
        try:
            t = datetime.strptime(line[:8], "%H:%M:%S").replace(
                year=now.year, month=now.month, day=now.day)
        except Exception:
            continue
        if 0 <= (now - t).total_seconds() <= 300:
            return (f" В {t:%H:%M:%S} приложение перезапускалось и туннель "
                    f"пересобирался — это окно перезапуска, а не авария.")
    return ""


def check_tunnel():
    url = _cfg_get("public-url")
    if not url or _cfg_get("remote-enabled") not in ("true", "True", "1"):
        ok("Туннель выключен (remote-enabled off) — пропускаю"); return
    try:
        req = urllib.request.Request(url, method="GET", headers={"User-Agent": "hc"})
        with urllib.request.urlopen(req, timeout=12, context=_trust_ctx()) as r:
            _streak("tunnel_serveo_side", False)
            ok(f"Туннель отвечает ({url})"); return
    except urllib.error.HTTPError as he:
        # Any HTTP status (401/403/405/…) means the tunnel is UP and routing —
        # only a connection error means it's actually down.
        if he.code < 500:
            _streak("tunnel_serveo_side", False)
            ok(f"Туннель отвечает ({url}, HTTP {he.code})"); return
        # 🔴 15.08.2026. Здесь стояло «сервер за туннелем нездоров» — вердикт,
        # который проверка не имела права выносить: в том же проходе app (7799)
        # ответил 200. 502 отдаёт КРАЙ serveo, когда у него нет апстрима, а нет
        # его ровно те ~20 секунд, пока пересобирается ssh после перезапуска
        # приложения. Мы сообщали владельцу поломку сервера там, где был
        # короткий провал связи — и не сообщали, если бы сервер лёг по-настоящему.
        # Теперь: перепроверяем после паузы, отделяем НАШУ сторону от их края и
        # считаем серию, как это давно делает ветка «TCP есть, HTTP молчит».
        local_ok = _app_alive()
        note = _tunnel_restart_window()
        time.sleep(20)
        again = _tunnel_probe(url)
        if again and again < 500:
            _streak("tunnel_serveo_side", False)
            ok(f"Туннель отвечает ({url}, HTTP {again}); {he.code} был кратким "
               f"провалом — на перепроверке через 20 с всё поднялось.{note}")
            return
        n = _streak("tunnel_serveo_side", True)
        tail = (f" Это {n}-я проверка подряд — само не чинится."
                if n >= 2 else "")
        if local_ok:
            warn(f"Туннель отдаёт {he.code}, хотя app (7799) отвечает — лежит не "
                 f"наш сервер, а связь с ним: у края serveo нет апстрима "
                 f"(ssh-сессия не держится).{note or _serveo_sshd_note()}{tail}")
        else:
            bad(f"Туннель отдаёт {he.code} И app (7799) не отвечает — лежит сам "
                f"сервер, туннелю нечего отдавать.{tail}")
    except Exception as e:
        # A TLS trust failure is NOT the tunnel being down: serveo's wildcard cert
        # is theirs to renew, and reconnecting the ssh session does nothing for it
        # (2026-08-03: cert expired at 23:59 GMT, tunnel kept routing HTTP 200).
        # Re-probe without verification to tell the two apart honestly.
        if "CERTIFICATE_VERIFY" in str(e):
            try:
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                req = urllib.request.Request(url, headers={"User-Agent": "hc"})
                with urllib.request.urlopen(req, timeout=12, context=ctx) as r:
                    code = r.status
            except urllib.error.HTTPError as he:
                code = he.code
            except Exception:
                code = 0
            if code and code < 500:
                # Routing works — this is a cert problem, not a serveo-side outage.
                _streak("tunnel_serveo_side", False)
                # "…[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed:
                #  certificate has expired (_ssl.c:1081)>" → "certificate has expired"
                reason = str(e).split("certificate verify failed:")[-1]
                reason = reason.split("(_ssl.c")[0].strip(" >)") or "невалиден"
                warn(f"Туннель РАБОТАЕТ (HTTP {code}), но его TLS-сертификат не проходит "
                     f"проверку: {reason} — это сертификат serveo, watchdog "
                     f"НЕ поможет. Гости упрутся в предупреждение браузера, пока serveo "
                     f"его не перевыпустит")
                return
        # «Не отвечает» — слишком общо, чтобы решить, надо ли вмешиваться. Отделяем
        # НАШУ сторону от serveo: если TCP на 443 принимается, а HTTP молчит, то
        # маршрутизация лежит у serveo, и ни watchdog, ни перезапуск ssh не помогут
        # (07.08.2026: TCP 80/443 и serveo.net:22 принимали, HTTP/HTTPS —
        # таймаут; ssh-сессии поднимались и отваливались каждые несколько минут).
        host = url.split("://", 1)[-1].split("/", 1)[0]
        tcp_ok = False
        try:
            with socket.create_connection((host, 443), timeout=8):
                tcp_ok = True
        except Exception:
            pass
        if tcp_ok:
            n = _streak("tunnel_serveo_side", True)
            tail = (f"Это {n}-я проверка подряд — молчаливого самовосстановления "
                    f"уже не ждём, писать в serveo" if n >= 2 else
                    "Если так же и на следующей проверке — писать им")
            warn(f"Туннель ({url}): serveo принимает TCP, но HTTP не отвечает "
                 f"({str(e)[:50]}){_serveo_sshd_note()} — маршрутизация лежит на "
                 f"СТОРОНЕ serveo, перезапуск ssh не поможет. Гости не зайдут, "
                 f"пока serveo не починится. {tail}")
        else:
            _streak("tunnel_serveo_side", True)
            warn(f"Туннель ({url}) недоступен даже по TCP: {str(e)[:50]} — "
                 f"сеть или serveo целиком; переподключится сам (watchdog)")


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
    if "tg-bot-api" in running_container_names():
        ok("TG Bot API контейнер (tg-bot-api) работает")
    else:
        warn("TG Bot API контейнер не найден — доставки в бот могут не идти (docker start tg-bot-api)")
        if not NO_FIX:
            if not ensure_container("tg-bot-api", notify=lambda m: None):
                warn("Не смог поднять tg-bot-api")
    # bot.py process (реальная проверка по командной строке)
    if bot_process_count() > 0:
        ok("Бот (bot.py) запущен")
    else:
        warn("Бот (bot.py) не запущен")
        if not NO_FIX:
            start_bot_process()


def check_pool_identities():
    """ИНВАРИАНТ, а не каталог прошлых аварий: у КАЖДОЙ учётки пула обязан быть
    непустой идентификатор.

    18.09.2026 это нарушалось молча и месяцами. `account_secret()` у Qobuz и
    Tidal читал только КОНФИГ-имена (`qobuz-auth-token`, `tidal-refresh`), а
    записи ПУЛА лежат с короткими ключами (`auth_token`, `refresh`) — и каждая
    пуловая учётка получала ПУСТУЮ строку как идентификатор: здоровье, маска и
    счётчик неудач всех слотов складывались в одну кучу, статус слота не
    показывался, снятие по такой учётке не срабатывало.

    Ни одна из существующих проверок этого не видела — все они описывают
    ИЗВЕСТНЫЕ поломки, а значит по определению не находят новый класс. Эта
    описывает СВОЙСТВО, которое обязано выполняться всегда, поэтому поймает и
    следующий сервис, у которого появится пул.
    """
    try:
        import yaml
        cfg = yaml.safe_load(CONFIG.read_text(encoding="utf-8-sig")) or {}
    except Exception as e:                                    # noqa: BLE001
        warn(f"Идентичность пулов: не смог прочитать конфиг ({str(e)[:50]})")
        return
    import sys as _s
    if str(ROOT) not in _s.path:
        _s.path.insert(0, str(ROOT))
    probes = [("Qobuz",  "qobuz-accounts",  "ripster.qobuz_accounts"),
              ("Tidal",  "tidal-accounts",  "ripster.tidal_accounts"),
              ("Yandex", "yandex-accounts", "ripster.yandex_accounts")]
    bad, total = [], 0
    for name, key, mod in probes:
        pool = cfg.get(key)
        if not isinstance(pool, list) or not pool:
            continue
        try:
            m = __import__(mod, fromlist=["account_secret"])
            fn = getattr(m, "account_secret", None)
        except Exception:                                     # noqa: BLE001
            continue
        if fn is None:
            continue
        for i, a in enumerate(pool):
            total += 1
            try:
                s = (fn(a) or "").strip()
            except Exception:                                 # noqa: BLE001
                s = ""
            if not s:
                bad.append(f"{name}[{i}] {((a or {}).get('label') or '?')}")
    if not total:
        return
    if bad:
        warn(f"Идентичность пулов НАРУШЕНА: {len(bad)} из {total} учёток без "
             f"идентификатора ({', '.join(bad[:6])}) — здоровье, маска и снятие "
             f"по ним работают вслепую. Чинить в account_secret() модуля: он "
             f"обязан читать И конфиг-имена, И короткие ключи записи пула.")
    else:
        ok(f"Идентичность пулов: все {total} учёток пула опознаются")


def check_bot_delivery():
    """Механический бэкстоп на инцидент 18.09.2026 («гость завис на 95%»): трекер
    бота застрял в 'running'/'queued', но задачи НЕТ ни в очереди app, ни в
    манифесте → это потерянный призрак, который блокирует порядковую выдачу чата.

    Живьём это чинит `_delivery_watchdog` в боте (~3 мин). Проверка — страховка:
    если призраки ВСЁ РАВНО висят здесь, значит бот на старом коде (сторож не
    вооружён) или сам сторож сломан. Только диагноз, без автофикса: рестарт
    ЖИВОГО бота отсюда оборвал бы активные загрузки — это делает человек.
    Ложных тревог избегаем: если очередь app не прочиталась — проверку
    пропускаем (иначе всё выглядело бы призраком)."""
    trk = ROOT / "tgbot" / "trackers.json"
    try:
        saved = json.loads(trk.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return                      # нет бота на этой машине — не наша забота
    except Exception as e:
        warn(f"Бот-доставка: не смог прочитать trackers.json ({str(e)[:50]})")
        return
    live = [t for t in (saved or []) if t.get("status") in ("queued", "running")]
    if not live:
        ok(f"Бот-доставка здорова (трекеров: {len(saved or [])}, зависших нет)")
        return
    st, data = _api("/api/queue")
    if not (st == 200 and isinstance(data, list)):
        warn("Бот-доставка: не смог прочитать очередь app — проверка пропущена")
        return
    q_ids = {t.get("id") for t in data}
    try:
        man = json.loads((ROOT / "downloads_manifest.json").read_text(encoding="utf-8"))
    except Exception:
        man = {}
    ghosts = [t for t in live
              if t.get("task_id") not in q_ids and t.get("task_id") not in man]
    if ghosts:
        who = ", ".join(f"{(t.get('req_name') or '?')}:{str(t.get('task_id'))[:8]}"
                        for t in ghosts[:5])
        warn(f"Бот-доставка ЗАВИСЛА: {len(ghosts)} трекер(ов) в running/queued, "
             f"но их нет ни в очереди app, ни в манифесте — очередь чата "
             f"заблокирована ({who}). Сторож бота должен был добить; проверь, что "
             f"бот на свежем коде, иначе перезапусти bot.py.")
    else:
        ok(f"Бот-доставка здорова ({len(live)} активных, все живы в app/манифесте)")


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
    # Ключ обязан совпадать с тем, по которому дедуплицирует /api/watchlist/repair,
    # а там в подпись входит `kind`: подписка на ЛЕЙБЛ «Balance Music» и подписка
    # на АРТИСТА «Balance Music» — разные вещи, опрашиваются разными путями, и
    # склеивать их нельзя. 23.08.2026 без `kind` проверка считала эту законную
    # пару дублем, звала репейр, тот справедливо не удалял ничего — и жалоба
    # «1 дублей» висела бы вечно, при том что обе записи опрашивались в тот же день.
    seen, dups = set(), 0
    for x in items:
        sig = (x.get("kind", "artist"), (x.get("name") or "").strip().lower(),
               x.get("service", ""))
        if sig in seen:
            dups += 1
        seen.add(sig)

    if not broken and not dups:
        # СНАЧАЛА возраст последнего прохода, и только потом «ни разу не
        # проверялись». 09.08.2026 порядок был обратный, и проверка сказала
        # «6 из 152 ни разу» — мягкая, ожидаемая фраза про шесть вчерашних
        # записей, — тогда как правда была куда хуже: весь список не
        # опрашивался 25 часов, а те шестеро просто попали под общий простой.
        # Диагноз показывал симптом самой малой части и прятал причину.
        newest = None
        for x in items:
            raw = x.get("last_check")
            if not raw:
                continue
            try:
                t = datetime.fromisoformat(str(raw))
            except ValueError:
                continue
            if newest is None or t > newest:
                newest = t
        age_h = ((datetime.now() - newest).total_seconds() / 3600.0
                 if newest else None)
        # Интервал 6ч; поднимаем тревогу вдвое позже, чтобы обычная задержка
        # прохода не мигала краснотой.
        if age_h is None:
            warn(f"Вишлист ({len(items)} записей): не проверялся НИ РАЗУ — "
                 f"фоновой проход не отработал ни одного раза")
            return
        if age_h > 12:
            warn(f"Вишлист: последний проход {age_h:.0f} ч назад при интервале 6ч — "
                 f"опрос стоит, релизы проходят мимо. Частая причина: app.py "
                 f"перезапускался чаще, чем раз в 6ч, и таймер обнулялся "
                 f"(лечится initial_check_delay — нужен рестарт app.py)")
            return
        never = [x for x in items if not x.get("last_check")]
        if never:
            warn(f"Вишлист: {len(never)} из {len(items)} ни разу не проверялись "
                 f"(последний проход {age_h:.1f} ч назад — новые записи "
                 f"подхватятся следующим)")
        else:
            ok(f"Вишлист здоров ({len(items)} записей, все опрашиваются, "
               f"последний проход {age_h:.1f} ч назад)")
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


def check_bbc_radar_urls():
    """ИНВАРИАНТ: каждая ссылка BBC-райдара обязана разбираться регуляркой загрузчика.

    ЗАЧЕМ. 19.09.2026 радар выдавал эпизоды ссылками вида /programmes/<pid>
    (их строит routes/radar.py), а runner._RE_BBC_PID узнавал только
    /sounds/play/<pid> — в результате КАЖДАЯ загрузка с радара падала с
    «could not parse the id from the link» (console.bbc_bad_pid), хотя сами
    эпизоды существовали и качались бы. Регулярку расширили обоими форматами,
    но договорённость «формат ссылки радара ↔ разбор загрузчика» не записана
    нигде, и следующая правка формата (свой домен, новый префикс, пустой pid)
    разойдётся с ней снова. Проверка сравнивает ЖИВЫЕ ссылки с той же
    регуляркой, что стоит в runner.py, — импортом, не копией: копия протухает
    в тот день, когда правят оригинал (этот класс лжи уже проходили на
    мёртвых детекторах отсечек).

    Авто-починки нет и быть не может: лечится только правкой кода (radar.py
    или runner.py), а это решает человек."""
    try:
        sys.path.insert(0, str(ROOT))
        from ripster.runner import _RE_BBC_PID as pid_re
    except Exception:
        try:  # runner тянет за собой половину пакета; запасной провод — тот же regex в metadata
            from ripster.metadata.bbc import _RE_PID as pid_re
        except Exception as e:
            warn(f"BBC-радар: не смог импортировать регулярку загрузчика ({str(e)[:60]}) "
                 f"— инвариант не проверен")
            return
    st, data = _api("/api/releases/bbc", timeout=60)
    if st != 200 or not isinstance(data, dict) or not isinstance(data.get("releases"), list):
        warn(f"BBC-радар: /api/releases/bbc не ответил (HTTP {st}, {str(data)[:60]}) — "
             f"ссылки нечем проверить")
        return
    urls = [str(r.get("url") or "") for r in data["releases"] if isinstance(r, dict)]
    if not urls:
        # Пустая лента = проверка прошла бы вхолостую. За 90 дней по 11 передачам
        # она обязана быть непустой; пустая — это сдохший источник, а не «всё хорошо».
        warn("BBC-радар: лента релизов ПУСТА — нечего проверять (11 передач за 90 дней "
             "не могут дать ноль эпизодов; источник мёртв?)")
        return
    unparsed = [u for u in urls if not pid_re.search(u)]
    if unparsed:
        warn(f"BBC-радар: {len(unparsed)} из {len(urls)} ссылок НЕ разбираются регуляркой "
             f"загрузчика (runner._RE_BBC_PID), например {unparsed[0] or '(пустая)'} — "
             f"такие загрузки упадут с «could not parse the id from the link». "
             f"Синхронизировать формат в routes/radar.py и разбор в runner.py")
    else:
        ok(f"BBC-радар: все {len(urls)} ссылок разбираются регуляркой загрузчика")


def check_namesake_guard():
    """Однофамильцы: «скрыто N, возвращено M» и сколько раз правило промахнулось.

    ЗАЧЕМ. Дверь ленты (`artist_identity.feed_filter`) судит при ЧТЕНИИ, поэтому
    «мы вылечили алгоритм» и «витрина чиста» — разные утверждения: карточка,
    пропущенная ВЧЕРашним правилом, лежит в ленте до первого перечитывания, а
    карточка, спрятанная ПРЕЖНИМ правилом по ошибке, после правки сама не
    вернётся — отметка «скрыто» переживает правку. Ровно на это владелец и
    жаловался 24.09.2026 (Qobuz «639 Hz Inner Worth»): артиста лечили шесть раз,
    а класс однофамильцев ходил по-прежнему.

    Проверяем три вещи, и третью — особенно честно:
      • суточный прогон склада вообще состоялся и не отстал от правил;
      • слово владельца («это не мой артист») переживает перезапуск — реестр на
        месте и не пустой, если человек уже жаловался;
      • МЕТРИКА УТЕЧКИ: сколько карточек радар ПОКАЗАЛ, а владелец отверг. Пока
        она растёт, рапортовать о «скрыто N» нечестно — это и есть сигнал, что
        лечат артиста, а не алгоритм.

    Авто-починки нет: промах правила чинится только правкой правила.
    """
    st, data = _api("/api/identity", timeout=90)
    if st != 200 or not isinstance(data, dict):
        warn(f"Однофамильцы: /api/identity не ответил (HTTP {st}, {str(data)[:60]}) — "
             f"состояние двери неизвестно")
        return
    a = data.get("audit") or {}
    fb = data.get("feedback") or {}
    hidden = int(a.get("hidden") or 0)
    returned = int(a.get("returned") or 0)
    kept = int(a.get("kept") or 0)
    cards = int(a.get("cards") or 0)
    # Число берём из `leaks` (оно полное), а не из `leak_items` — там только
    # первые десять карточек для показа, и по ним «утечек 10» было бы наглым
    # занижением при сотке.
    leaks = int(a.get("leaks") or 0)
    if not a.get("ts"):
        warn("Однофамильцы: самоаудит склада НИ РАЗУ не проходил — карточки, "
             "которые прежнее правило спрятало по ошибке, до сих пор спрятаны, "
             "а новые однофамильцы до сих пор в ленте. "
             "POST /api/identity/audit (или дождаться ночного прогона)")
    elif a.get("stale_model"):
        warn("Однофамильцы: правила менялись ПОСЛЕ последнего прогона — отчёт "
             f"«скрыто {hidden}, возвращено {returned}» описывает прошлые правила, "
             "а не нынешние. Нужен прогон заново (POST /api/identity/audit)")
    else:
        ok(f"Однофамильцы: скрыто {hidden}, возвращено {returned}, "
           f"показано {kept} из {cards}; слово хозяина: «не мой» "
           f"{int(fb.get('negative') or 0)}, «это мой» {int(fb.get('positive') or 0)}")
    if leaks:
        names = sorted({str(x.get("artist") or "").strip()
                        for x in (a.get("leak_items") or [])} - {""})[:5]
        warn(f"Однофамильцы: метрика утечки = {leaks} — радар ПОКАЗАЛ этих артистов "
             f"и владелец отверг их словом «не мой» "
             f"({', '.join(names)[:90]}). Значит правило по-прежнему пропускает "
             f"целый класс — лечить его, а не артиста")
    if int(a.get("leaks_delta") or 0) > 0:
        warn(f"Однофамильцы: за последние сутки утечка ВЫРОСЛА на "
             f"{a['leaks_delta']} — новый промах алгоритма, а не новый каприз владельца")


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
        token = json.loads(cfg.read_text(encoding="utf-8-sig")).get("bot_token", "")
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
    # ДЕРЖАТЬ ПЕРЕД `apple-album-unavailable`: у обоих одна и та же первая
    # строка, а вывод из них противоположный. «Нет в этом магазине» — состояние
    # каталога, делать нечего; 429 — Apple нас придержал, само пройдёт, и чинить
    # надо ЧАСТОТУ запросов. 05.09.2026 отчёт сказал владельцу
    # «apple-album-unavailable×45», то есть «45 релизов недоступны», тогда как
    # 39 из 45 строк были одним 429-всплеском на ОДНОМ альбоме (13 задач за 26
    # секунд, 15:01:34–15:02:00), а «недоступных» релизов не было вовсе. Имя
    # бакета уводило от причины ровно так же, как когда-то `engine-url-mismatch`
    # уводил от витрины. Сетевой хвост (`dial tcp`) тоже отделяем: он тоже
    # временный. Обе формы теперь ещё и повторяются раннером
    # (`_apple_catalog_transient`), так что бакет должен звать их своими именами.
    ("apple-rate-limited",  ("429 too many requests",
                             "apple music api → http 429")),
    ("apple-catalog-network", ("error getting album response: get \"https://amp-api",)),
    # А «failed to get album response» — это НЕ про движок. 28.07.2026 все 8
    # таких строк оказались одним альбомом, которого просто нет в запрошенной
    # витрине (279606055 издан только в tr/ru, ссылка была на us → 404 → Apple
    # отвечает «Failed to get album response» и выходит с кодом 0). Пока они
    # лежали в бакете про движок, диагностика уходила не туда — искали ошибку
    # маршрутизации там, где её нет.
    ("apple-album-unavailable", ("failed to get album response",
                                 "error getting album response")),
    # «У трека нет текста» — НОРМА, а не поломка: печатается на КАЖДЫЙ трек, и
    # 10.08.2026 семнадцать таких строк составили большинство бакета `other`,
    # пряча в нём настоящую находку (мёртвый минт Spotify). Отдельное имя нужно
    # ещё и потому, что раньше причину было не отличить: и 401 без подписки, и
    # 404 не в том магазине, и честное «текста нет» давали одну строку «failed to
    # get lyrics». Теперь сообщение называет статус и магазин, а права видно по
    # ключу `media-user-token rejected` — вот ЕГО в этом бакете быть не должно,
    # он попадёт в `other` и потребует разбора.
    ("lyrics-none",         ("lyrics not found", "no lyrics resource there",
                             "no lyrics in response", "lyrics entry present but empty")),
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
                             "прав в регионе аккаунта нет",
                             # Третья форма того же диагноза (24.08.2026, 2 строки
                             # в `other`): движок говорит про трек, а не про права.
                             "недоступен в этом регионе")),
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
    # Трек недоступен учётке (лицензия/регион) — НЕ отказ авторизации. Цепочка из
    # одного такого трека печатает ЧЕТЫРЕ строки: SpotifyTrackUnavailableError,
    # «Track/Episode is unavailable», попытку добрать его как эпизод (404
    # Extended Metadata) и падение `'NoneType' has no attribute 'download_type'`.
    # Все четыре содержали "spotify"/"401"/"download_type" и падали в spotify-auth:
    # 16.08.2026 бакет показал spotify-auth×137 при полностью живой авторизации
    # (Bearer свежий, sp-keeper минтит каждые 45 мин) — из них 120+ строк были
    # этой недоступностью, и настоящий отказ входа в такой куче не разглядеть.
    # Держать ПЕРЕД spotify-auth — как spotify-lyrics выше и по той же причине.
    ("spotify-unavailable", ("cannot get alternative track", "is unavailable on spotify",
                             "spotifytrackunavailableerror", "attribute 'download_type'",
                             "extended metadata request failed",
                             # Наш СОБСТВЕННЫЙ вердикт того же инцидента, 20.09.2026:
                             # строка буквально говорит «Вход исправен… перелогин не
                             # поможет» — и всё равно попадала в spotify-auth, потому
                             # что содержит слово «spotify». Диагноз в ней верный,
                             # не хватало только имени.
                             "недоступно для этой учётной записи")),
    # ДЕРЖАТЬ ПЕРЕД spotify-auth. Бакет авторизации ловит голую подстроку
    # "spotify", а у Spotify хост САМ содержит это слово — поэтому
    # «HTTPSConnectionPool(host='apresolve.spotify.com'): Max retries exceeded»
    # (отказ СЕТИ, авторизация в нём не участвует) приходил как spotify-auth.
    # 12.09.2026 сводка показала владельцу spotify-auth×40 первым бакетом при
    # полностью живой авторизации: 26 строк были этим коннектом, ещё 12 —
    # сторожем, сдавшимся из-за него же, и лишь 2 имели отношение к Spotify.
    # Настоящая причина (DNS лежал 06:45–15:26) стояла ОТДЕЛЬНО в network×37 и
    # выглядела второстепенной. Имя бакета уводило от причины — ровно тот же
    # дефект, что уже чинили для spotify-lyrics и spotify-unavailable.
    # Ключи нарочно транспортные: 401/OAuth-формы сюда не подходят.
    ("network",             ("httpsconnectionpool", "max retries exceeded",
                             "getaddrinfo")),
    # Кипер сдался после N попыток — ЭТО настоящий сигнал «нужно вмешательство»,
    # и он обязан называться собой, а не «авторизацией»: 12.09 он сработал из-за
    # обрыва сети, а не из-за токена, и починился сам, когда сеть вернулась.
    ("spotify-keeper-giveup", ("попытки подряд не помогли",)),
    # ДЕРЖАТЬ ПЕРЕД spotify-auth, и по той же причине, что spotify-lyrics /
    # spotify-unavailable / network выше: бакет авторизации ловит голую подстроку
    # "spotify", а эта строка её содержит в префиксе «[spotify]». 20.09.2026
    # сводка вывела spotify-auth×10 ПЕРВЫМ бакетом при свежем Bearer (41 мин из
    # 60) — и ни одна из десяти строк не была отказом авторизации: пять — вот
    # этот 502/503 от САМОГО Spotify на /me/following, четыре — недоступность
    # релиза учётке, одна — обрыв дочитывания после 200. Отказ на стороне
    # Spotify ничего не требует от владельца и проходит сам; называть его
    # «авторизацией» значит звать чинить живой токен.
    ("spotify-following-5xx", ("/me/following returned 5", "following fetch error")),
    ("spotify-auth",        ("orpheus_not_authed", "gettrack", "401", "spotify")),
    ("qobuz-sub",           ("нет активной подписки", "ineligible")),
    # Beatport отвечает 403 «You do not have permission to perform this action» на
    # запросе ПОТОКА (логин при этом проходит, подписка активна) — это права на
    # конкретный релиз, а не регион и не поломка. 29.07.2026 дало 21 безымянную
    # строку в `other` от одного релиза, качавшегося по кругу.
    # 09.08.2026: строка САМОЙ отсечки («Прогон прерван: … отказал в правах на 10
    # треках подряд») сюда не попадала — формулировка другая, — и все 7 строк
    # `other` за сутки оказались этим же Beatport'ом. `other` обязан значить
    # «такого я ещё не видел», иначе новый сбой в нём не заметен.
    ("beatport-entitlement", ("do not have permission to perform this action",
                              "не хватает прав на скачивание",
                              "отказал в правах")),
    ("beatport-region",     ("region locked", "territory restricted")),
    # Отказ ВХОДА Beatport — не права на релиз (beatport-entitlement выше) и не
    # регион. 19.09.2026 шесть таких строк лежали в `other`, то есть в бакете
    # «такого я ещё не видел», хотя это был известный и в тот же день починенный
    # дефект: refresh РОТИРУЕТ refresh_token, новый не писался обратно в
    # loginstorage.bin, OrpheusDL приходил с погашенным → Beatport отзывал всю
    # цепочку. Формулировка «неверный логин/пароль» при этом ВРЁТ — учётка жива,
    # чинить надо запись сессии. Имя бакета обязано вести к этому, а не к паролю.
    ("beatport-auth",       ("beatport_not_authed",
                             "authentication credentials were not provided")),
    # Deezer отдаёт трек, но не в запрошенном качестве (нет FLAC/320 у источника).
    # Это состояние источника/ARL, а не поломка: сообщение уже само советует, что
    # делать. 03.08.2026 давало 2 безымянные строки (сама ошибка + её копия из
    # [history] recorded error) — ключ ловит обе формы.
    ("deezer-quality",      ("недоступен в выбранном качестве", "нет flac/320")),
    # Apple music-video: первая попытка варианта приходит нулевой («Failed to dl
    # aac-lc: Unavailable» → 0 B), ключа нет, и runv3.go:487 всё равно зовёт
    # mp4decrypt --key с пустым значением → «invalid argument for --key».
    # 03.08.2026 обе такие строки САМИ ВОССТАНОВИЛИСЬ: следом шёл
    # audio-stereo-256 и «Decrypted.» Загрузка не падает — это шум пробы
    # варианта, а не сбой. Значение ключа в лог не попадает (уже редактится).
    ("apple-mv-key",        ("invalid argument for --key",)),
    # Сторож тишины: 300 секунд без вывода → процесс убит. Это НАСТОЯЩИЙ отказ,
    # и до 24.08.2026 у него не было имени — обе строки падали в `other`. Свой
    # бакет нужен, чтобы отличать «завис и убит» от «отказано в правах»: 24.08
    # это была нога лестницы витрин (локальная 'ca' не дала прав, повтор по 'ca'
    # завис), и по безымянной строке этого было не понять. Держать ПЕРЕД
    # `network`: там ключ "timeout", но наша строка русская — совпадения нет, а
    # порядок страхует от будущей английской формулировки.
    ("stall-timeout",       ("процесс завис", "нет вывода")),
    # Tidal на гео-блоке отвечает «Album [id] not found. This might be
    # region-locked.» — и повторяет попытку ЧЕТЫРЕ раза на детерминированном
    # отказе, так что один релиз даёт 5 строк. Ключ `region locked` бакета
    # beatport-region не совпадает: у Tidal дефис (`region-locked`).
    ("tidal-region",        ("this might be region-locked",
                             "недоступно в регионе твоего аккаунта")),
    ("network",             ("getaddrinfo", "deadline exceeded", "connection", "timeout", "429")),
    # Хвосты уже посчитанных провалов: «Exit code 0» печатается ПОСЛЕ причины
    # (apple-album-unavailable), «=== Track N failed ===» — после 403 Beatport.
    # Сами по себе они ничего не диагностируют, но раздували `other` вдвое.
    # Держать ПОСЛЕДНИМИ: любой хвост, у которого есть своя причина, до сюда не
    # дойдёт. Смысл `other` — «такого я ещё не видел», и он должен быть честным.
    # Мёртвый ARL Deezer печатает ТРИ разные строки одного инцидента: приглашение
    # deemix («Paste here your arl:»), его же «Aborted!» под stdin=DEVNULL и наш
    # разбор. 04.09.2026 все семь строк лежали в `other` — то есть в бакете «такого
    # я ещё не видел», хотя видели много раз и точно знаем, что делать.
    ("deezer-arl",          ("paste here your arl", "aborted!",
                             "arl не задан или протух", "неверный arl")),
    # Отказ Apple ПО ПРАВАМ, а не по региону и не по токену: ключ не выдала ни
    # одна учётка. Диагноз верный и действие понятное (нужна учётка с правами),
    # ему не хватало только имени.
    ("apple-no-entitlement", ("ключ на этот релиз не выдала",)),
    # Сеть подсистемы уведомлений (accounts-watch → Telegram). К загрузкам
    # отношения не имеет, но и «невиданным» не является.
    ("watch-notify",        ("notification not sent",)),
    # Страховочная сетка раннера: задача покинула run_task, не проставив финальный
    # статус. Это ВНУТРЕННИЙ дефект, а не отказ сервиса, и он должен быть виден
    # отдельной строкой, а не тонуть среди чужих ошибок.
    ("runner-stuck",        ("safety net: task",)),
    # SoundCloud не смог достать id трека из ссылки. Отказ настоящий, но НЕ
    # фатальный: 05.09.2026 три повтора дали то же, а затем сработала отдача с
    # диска («на диске 1 файл(ов) — отдаю их») и задача закрылась успехом.
    # Своё имя нужно, чтобы отличать его от невиданного: в `other` он выглядел
    # как четыре безымянные строки подряд.
    ("soundcloud-resolve",  ("could not extract track id",)),
    # BBC: из ссылки не разобрался pid. 19.09.2026 две попытки одной передачи
    # (m00315gq, Essential Mix) дали четыре безымянные строки в `other`, причём в
    # лог ушёл СЫРОЙ ключ «console.bbc_bad_pid» — тогда живой app.py не знал ни
    # расширенной регулярки (/programmes/ рядом с /sounds/play/), ни строки
    # перевода. Обе правки уже на месте и проверены проводом 20.09 (та же ссылка
    # → pid → HLS). Бакет нужен, чтобы возврат симптома назвал себя сам, а не
    # выглядел новинкой: ловим и человеческую форму, и сырой ключ.
    ("bbc-bad-pid",         ("console.bbc_bad_pid",
                             "не удалось разобрать идентификатор из ссылки",
                             "could not parse the id from the link")),
    # Заголовок трейсбека сам по себе не диагностирует НИЧЕГО — настоящая
    # причина печатается отдельной ERROR-строкой в конце (05.09.2026 обе
    # оказались `tidal-region`) и попадает в свой бакет. Держать в хвосте: если
    # у нового сбоя причины не окажется, она всё равно придёт своей строкой и
    # честно ляжет в `other`.
    # Тело HTML-страницы чужой ошибки. 20.09.2026 Spotify ответил на
    # /me/following страницей 503, её раскидало по восьми строкам лога, и две из
    # них («<title>503 Server Error</title>», «<h1>Error: Server Error</h1>»)
    # оказались помечены ERROR и попали в `other` — при том что сам инцидент уже
    # посчитан строкой выше (spotify-following-5xx). Диагностируют они ровно
    # ничего: это разметка, а не причина.
    ("noise-tail",          ("exit code 0", "=== track ",
                             "traceback (most recent call last)",
                             "server error</title>", "error: server error")),
]


def check_retry_storms():
    """Задача, которая повторялась и КАЖДЫЙ раз сохранила ноль файлов.

    Отдельный класс от «много ошибок»: повтор осмыслен, когда второй проход
    добирает недостающее, и бессмыслен, когда отказ детерминированный (нет прав
    у аккаунта, релиза нет в магазине, сервер лежит). Отличить их должен
    `_RE_NO_RETRY` / фаст-фейл в runner.py — но именно эти детекторы дважды
    оказывались МЁРТВЫМИ, потому что были заякорены на строку, которая до
    проверки не доходит: 08.08 отсечка враппера ждала фразу, которую tenacity
    обрезает (24 мин, 830 ERROR, 0 файлов), 09.08 отсечка по правам Beatport
    ловила `do not have permission` и `отказал в правах`, а в `msg` приходило
    «не хватает прав» (4 прогона × 10 отказов).

    Детектор, не сработавший ни разу, неотличим от «проблемы не бывает», поэтому
    здесь проверяется СИМПТОМ, а не конкретная формулировка: сколько раз за сутки
    раннер объявлял авто-повтор, уже имея ноль сохранённых треков. Это ловит
    следующий мёртвый детектор, каким бы ни был его текст.
    """
    log = ROOT / "logs" / "console.log"
    if not log.exists():
        return
    try:
        import re as _re
        cutoff = datetime.now().timestamp() - 24 * 3600
        lines = log.read_text(encoding="utf-8", errors="ignore").splitlines()[-8000:]
        fresh = []
        for ln in lines:
            m = _re.match(r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)", ln)
            if m:
                try:
                    if datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").timestamp() < cutoff:
                        continue
                except Exception:
                    pass
            fresh.append(ln)
        # «Прогон прерван» = движок сам вынес вердикт «дальше бессмысленно».
        # Если следом идёт авто-повтор — внешний слой этот вердикт выбросил.
        # Инциденты ДО старта текущего app.py случились под другим кодом. Если
        # отсечку с тех пор починили и приложение перезапустили, симптом уже не
        # воспроизводится — но суточное окно держало бы предупреждение ещё сутки,
        # и следующее пробуждение выводило бы диагноз по несуществующей проблеме
        # (ровно так и вышло 10.08: все 5 инцидентов были от 09.08 17:10–17:45,
        # правка легла в 22:48, приложение перезапущено в 23:49).
        app_ts = _app_started_ts()

        def _ts(ln):
            m = _re.match(r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)", ln)
            if not m:
                return 0.0
            try:
                return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").timestamp()
            except Exception:
                return 0.0

        # ВТОРОЙ счётчик, не зависящий от формулировки движка. Первый (ниже)
        # заякорен на «Прогон прерван», то есть ловит только случай «движок вынес
        # вердикт, а раннер его выбросил». Но докстрока этой проверки обещает
        # ловить СИМПТОМ — «раннер объявил повтор, уже имея ноль файлов», — а он
        # бывает и без всякого вердикта: 15.08.2026 gamdl упал детерминированным
        # `KeyError: AUDIO-SESSION-KEY-IDS` (экспериментальный кодек без wrapper'а),
        # никакого «прогон прерван» не печатал, раннер увидел обычную частичную
        # загрузку и пошёл на повтор — 0 файлов, повторы по кругу, а сводка
        # написала «Повторов вхолостую нет». Проверка, которая обещает симптом, а
        # проверяет формулировку, — ровно тот мёртвый детектор, ради которого её и
        # заводили. Якорь по РУССКОЙ строке намеренно: консоль одноязычная
        # (`console.partial_retry` в ripster/i18n.py), английской формы в логе нет.
        _zero_all = [ln for ln in fresh if _re.search(r"Частично:\s*0\s+скачано", ln)]
        zero_retry = sum(
            1 for ln in _zero_all
            if not app_ts or not _ts(ln) or _ts(ln) >= app_ts
        )
        zero_old = len(_zero_all) - zero_retry

        # ТРЕТЬЯ форма, мимо обоих счётчиков выше: обычный повтор после ошибки
        # (`console.error_retry`: «⚠ Ошибка: … — повтор n/m через Nс…»). Вердикта
        # «прогон прерван» нет, «Частично» нет — а повтор вхолостую есть.
        # 13.09.2026: Spotify «недоступно для этой учётной записи» прошло 3/3 по
        # 15/45/120 с, и сводка в 20:01 написала «Повторов вхолостую нет». Симптом:
        # повтор n≥2 с ТЕМ ЖЕ текстом ошибки, что и попытка n-1, — детерминированный
        # отказ, который отсечка не узнала.
        # Тот же текст на ДРУГОЙ учётке — не холостой повтор, а перебор пула.
        # 18.09.2026: Deezer «нет FLAC/320» прошёл 3/3 по слотам 0→2→3 (US→US→BR)
        # и был засчитан как «вхолостую», хотя каждый заход спрашивал другой
        # аккаунт/регион — ровно то, ради чего повтор и нужен.
        _prev_err: dict = {}
        _prev_slot: dict = {}
        cur_slot = None
        same_err = same_err_old = 0
        for ln in fresh:
            ms = _re.search(r"\[deezer-pool\] ARL слота (\d+)", ln)
            if ms:
                cur_slot = ms.group(1)
                continue
            m = _re.search(r"⚠ Ошибка: (.+) — повтор (\d+)/\d+", ln)
            if not m:
                continue
            txt, n = m.group(1), int(m.group(2))
            _slot, cur_slot = cur_slot, None
            rotated = (n >= 2 and _slot is not None
                       and _prev_slot.get(n - 1) is not None
                       and _prev_slot.get(n - 1) != _slot)
            _prev_slot[n] = _slot
            if n >= 2 and _prev_err.get(n - 1) == txt and not rotated:
                if not app_ts or not _ts(ln) or _ts(ln) >= app_ts:
                    same_err += 1
                else:
                    same_err_old += 1
            _prev_err[n] = txt

        storms = live = 0
        for i, ln in enumerate(fresh):
            if "Прогон прерван" not in ln:
                continue
            # Только РЕАЛЬНЫЙ повтор («повтор 2/3 через 45с», «⟳ Авто-повтор 2/3»),
            # не совет в тексте ошибки («повтори с другим треком, чтобы понять
            # масштаб») — иначе проверка сама себе завышает счёт.
            nxt = " ".join(fresh[i + 1:i + 3])
            if not _re.search(r"Авто-повтор|повтор\s+\d+\s*/\s*\d+", nxt):
                continue
            storms += 1
            m = _re.match(r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)", ln)
            when = 0.0
            if m:
                try:
                    when = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").timestamp()
                except Exception:
                    pass
            # Без надёжной отметки времени считаем инцидент живым — умалчивать
            # опаснее, чем лишний раз предупредить.
            if not app_ts or not when or when >= app_ts:
                live += 1
        if live >= 3:
            warn(f"Повторы вхолостую: {live} раз за сутки движок объявил «прогон прерван» "
                 f"(ноль сохранённых), а раннер всё равно пошёл на авто-повтор. Значит, отсечка "
                 f"не узнала отказ. С 10.08 раннер читает сам вердикт движка (`abort_reason`) "
                 f"помимо `_RE_NO_RETRY` — если счёт живой, вердикт до раннера не доходит: "
                 f"grep «Прогон прерван» в logs/console.log")
        elif zero_retry >= 3:
            warn(f"Повторы вхолостую: {zero_retry} раз за сутки раннер пошёл на повтор, "
                 f"имея НОЛЬ скачанных треков. Вердикта «прогон прерван» при этом не было — "
                 f"значит, движок упал так, что отсечка его не распознала (15.08 это был "
                 f"детерминированный KeyError gamdl на экспериментальном кодеке). Повтор "
                 f"осмыслен, только если второй проход что-то добирает: grep «Частично: 0 "
                 f"скачано» в logs/console.log и посмотри причину выше по логу")
        elif same_err >= 2:
            warn(f"Повторы вхолостую: {same_err} раз за сутки авто-повтор вернул ТУ ЖЕ ошибку "
                 f"слово в слово — отказ детерминированный, а `_RE_NO_RETRY` его не узнал. "
                 f"grep «— повтор» в logs/console.log и заякорь текст ошибки в ripster/runner.py")
        elif live or zero_retry or same_err:
            _z = f", из них без вердикта движка: {zero_retry}" if zero_retry else ""
            ok(f"Повторы вхолостую: {live + zero_retry + same_err} — единичные, отсечка в целом держит{_z}")
        elif storms or zero_old or same_err_old:
            ok(f"Повторы вхолостую: {storms + zero_old + same_err_old} за сутки, но все ДО перезапуска "
               f"app.py — под текущим кодом ни одного, окно дочистится само")
        else:
            ok("Повторов вхолостую нет — отсечка безнадёжных прогонов работает")
    except Exception as e:
        _report.append(f"↻ Повторы вхолостую: не смог посчитать ({str(e)[:50]})")


def check_pacing():
    """Счётчики запросов к сервисам — владелец просит их видеть, когда «само
    замедлилось» (24.09.2026, выжимка чата @apple_music_alac: учётки летят в бан
    пачками за тысячи запросов в сутки, Apple отвечает 429, Qobuz режет 403 на
    официальном эндпоинте плейлистов).

    Здесь ровно то, ради чего пейсинг заведён: сколько ушло за час/сутки, есть ли
    штраф после 429/403 и сколько запросов суточный потолок уже ОТКАЗАЛ. Отказ —
    это не ошибка, а штатная защита: вызывающий получает пустой список там, где
    умеет без него жить. Предупреждение — только если потолок начал резать
    по-настоящему или штраф уехал на высокую ступень."""
    try:
        sys.path.insert(0, str(ROOT))
        from ripster import pacing
    except Exception as e:
        _report.append(f"↻ Пейсинг: модуль не импортирован ({str(e)[:50]})")
        return
    try:
        # pyyaml в этот скрипт намеренно не тащат (см. `_cfg_get`), поэтому
        # потолки читаются теми же точечными скалярами, что и остальной конфиг.
        cfg = {k: _cfg_get(k) for k in
               ("apple-requests-per-hour", "apple-requests-per-day",
                "qobuz-playlist-per-hour", "qobuz-playlist-per-day")}
        cfg = {k: v for k, v in cfg.items() if v}
        rows = pacing.snapshot(cfg)
    except Exception as e:
        _report.append(f"↻ Пейсинг: счётчики не прочитаны ({str(e)[:50]})")
        return
    if not rows:
        ok("Пейсинг: запросов к Apple/Qobuz-API с этого прогона ещё не считали")
        return
    worst_day = max(rows, key=lambda r: (r["day"] / (r["day_cap"] or 1e9)))
    refused = sum(int(r["refused"] or 0) for r in rows)
    hot     = [r for r in rows if r["day_cap"] and r["day"] >= r["day_cap"]]
    struck  = [r for r in rows if r["strikes"]]
    for r in rows[:6]:
        _report.append(
            f"↻ Пейсинг {r['service']} · {r['account']}: {r['hour']}/{r['hour_cap'] or '∞'} за час, "
            f"{r['day']}/{r['day_cap'] or '∞'} за сутки"
            + (f", штраф {int(r['penalty_left'])} с (ступень {r['strikes']})" if r["penalty_left"] > 0 else "")
            + (f", отказов {r['refused']}" if r["refused"] else ""))
    if len(rows) > 6:
        # Отчёт живёт в Telegram-сообщении с лимитом в 4096 знаков: показываем
        # шесть самых загруженных, остальное — числом, а не молчанием.
        _report.append(f"↻ Пейсинг: ещё {len(rows) - 6} счётчиков (полный список — "
                       f"dist/pacing.json или /api/admin/diagnostics)")
    if hot:
        warn(f"Пейсинг: суточный потолок выбран у {len(hot)} сервис(а/ов) — запросы "
             f"НЕ отправляются до переката суток (всего отказов {refused}). Это "
             f"защита от бана, а не поломка; снять можно в настройках "
             f"(`apple-requests-per-day`, `qobuz-playlist-per-day`)")
    elif any(r["strikes"] >= 3 for r in rows):
        warn(f"Пейсинг: 429/403 повторяются (ступень {max(r['strikes'] for r in struck)} у "
             f"{struck[0]['service']}) — сервер нас притормаживает всерьёз. Пул учёток "
             f"и суточный объём стоит уменьшить, а не потолок поднимать")
    elif struck:
        ok(f"Пейсинг: штрафы были у {len(struck)}, но уже отработаны; "
           f"максимум {worst_day['day']}/{worst_day['day_cap'] or '∞'} суток по {worst_day['service']}")
    else:
        ok(f"Пейсинг: в штатном режиме — максимум {worst_day['day']}/"
           f"{worst_day['day_cap'] or '∞'} суток по {worst_day['service']} · {worst_day['account']}")


def check_code_newer_than_app():
    """Фикс лежит на диске, а живой app.py его ещё не видел.

    15.09.2026: сводка написала «Повторы вхолостую: 8» по Deezer «ни один трек не
    скачался» — хотя разворот коротких ссылок `link.deezer.com/s/…` уже лежал в
    ripster/engines/deezer.py с 14.09 20:04. app.py стартовал 14.09 03:15, так что
    обе ссылки гостя в 23:22 прогнали старым кодом по 3 повтора. Диагноз «отсечка
    не узнала отказ» вёл бы чинить уже починенное. Это не поломка, а подсказка:
    ℹ️ без счёта проблем. Правки моложе 15 мин не считаем — это ещё идёт работа.
    """
    try:
        app_ts = _app_started_ts()
        if not app_ts:
            return
        fresh_edge = datetime.now().timestamp() - 15 * 60
        newer = []
        for p in [ROOT / "app.py", *(ROOT / "ripster").rglob("*.py")]:
            try:
                mt = p.stat().st_mtime
            except OSError:
                continue
            if app_ts < mt < fresh_edge:
                newer.append((mt, p.relative_to(ROOT).as_posix()))
        if not newer:
            return
        newer.sort(reverse=True)
        names = ", ".join(n for _, n in newer[:4]) + (f" и ещё {len(newer) - 4}" if len(newer) > 4 else "")
        since = datetime.fromtimestamp(app_ts).strftime("%d.%m %H:%M")
        _report.append(_esc(
            f"ℹ️ Код новее запущенного app.py (старт {since}): {names} — правки не действуют "
            f"до перезапуска; повторяющиеся ошибки ниже могут быть уже починены"))
    except Exception as e:
        _report.append(f"↻ Свежесть кода: не смог проверить ({_esc(str(e)[:50])})")


def check_errors_24h():
    log = ROOT / "logs" / "console.log"
    if not log.exists():
        return
    try:
        import re as _re
        cutoff = datetime.now().timestamp() - 24 * 3600
        buckets: dict = {}
        total = 0
        amz_ext = 0
        _sp_tail_pending = False   # впереди идущий ERROR = отказ трека, ждём его хвост
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
            # УСПЕХ, записанный на уровне ERROR. streamrip/SoundCloud печатает
            # итог прогона («Summary: 1 Success, 0 Failed») в stderr, и наш
            # захват метит stderr как ERROR — 24.08.2026 такая строка лежала в
            # `other` и означала ровно противоположное тому, что бакет считает.
            # Настоящий провал внутри такого прогона печатает СВОЮ строку и
            # считается по ней, так что сводку можно не считать вовсе.
            if "summary:" in low and "success" in low:
                continue
            # Шапка альбома OrpheusDL, напечатанная в stderr. Соседние строки того
            # же блока (Year / Duration / Number of tracks / Service) идут в
            # stdout, а «Artist: Markus Homm (53403)» — в stderr, и наш захват
            # метит её ERROR. 19.09.2026 четыре такие строки составили четверть
            # бакета `other` при ПОЛНОСТЬЮ успешной загрузке Beatport. Отсекаем по
            # форме (имя + числовой id в скобках на конце), а не по подстроке
            # «artist:»: иначе настоящая ошибка со словом «artist» молча
            # превратилась бы в шум — а это хуже, чем лишняя строка в `other`.
            if _re.search(r"\bartists?: .+\(\d+\)\s*$", low):
                continue
            # Сторонний Amazon-враппер (amz.dezalty.com) лежит сутками — каждый
            # прогон probe-all пишет одну и ту же ERROR-строку, и за сутки их
            # набирается 50+. Это НЕ наша поломка (check_engine_probe её уже
            # игнорирует), но в сводке ошибок она забивала топ и создавала
            # впечатление шумного прохода. Считаем отдельно, одной строкой.
            if "[amazon] probe failed" in low and "amz.dezalty.com" in low:
                amz_ext += 1
                continue
            # ОДИН отказ трека = одна ошибка, а не две. На недоступный этой
            # учётке трек OrpheusDL печатает сначала «Episode download also
            # failed …: Extended Metadata request failed: Status code 404»
            # (запасная попытка взять то же как эпизод), а строкой ниже — своё же
            # падение `'NoneType' object has no attribute 'download_type'`.
            # Замер по всем четырём логам (13.09–23.09): 510 и 509 строк, и в 508
            # случаях NoneType идёт СРАЗУ за episode-404; обратного порядка нет
            # ни разу. Связывает эти строки только соседство — id в хвосте
            # отсутствует, — поэтому хвостом считаем строго строку, следующую за
            # уже посчитанным episode-404. Осиротевший NoneType остаётся в
            # бакете: это уже необъяснённый провал, а не эхо.
            # Зачем это здесь, а не «починим потом»: без этого разделения
            # «spotify-unavailable ×96 за сутки» (docs/BACKLOG.md:26) прятал за
            # удвоением ровно 26 настоящих отказов, и любая правка движка
            # выглядела бы в сводке как ничего не изменившая.
            if "attribute 'download_type'" in low and _sp_tail_pending:
                _sp_tail_pending = False
                buckets["noise-tail"] = buckets.get("noise-tail", 0) + 1
                continue
            _sp_tail_pending = "extended metadata request failed" in low
            total += 1
            tag = next((name for name, keys in _ERR_BUCKETS if any(k in low for k in keys)), "other")
            buckets[tag] = buckets.get(tag, 0) + 1
        if amz_ext:
            _report.append(f"ℹ️ Amazon: сторонний {amz_ext}× за сутки «amz.dezalty.com недоступен» "
                           f"— внешний сервис лежит, не наша поломка (загрузки Amazon стоят, "
                           f"остальное не затронуто)")
        if not buckets:
            ok("Ошибок за сутки: 0 — чисто" + (f" (не считая {amz_ext}× внешнего Amazon)" if amz_ext else ""))
            return
        # Заголовок не должен считать НОРМУ поломками. Оба этих бакета заведены
        # именно как «не ошибка»: `lyrics-none` — у трека нет текста (печатается
        # на каждый трек), `noise-tail` — хвосты уже посчитанных провалов. Пока
        # они входили в total, счётчик за 04.09.2026 показывал 162 «ошибки», из
        # которых 83 были нормой — почти половина. Такой заголовок либо пугает
        # зря, либо приучает не смотреть на него вовсе. Показываем отдельно, но
        # НЕ прячем: цифры остаются на виду.
        BENIGN = ("lyrics-none", "noise-tail")
        benign = {k: v for k, v in buckets.items() if k in BENIGN}
        real = {k: v for k, v in buckets.items() if k not in BENIGN}
        real_total = sum(real.values())
        top = sorted(real.items(), key=lambda x: -x[1])
        summary = ", ".join(f"{k}×{v}" for k, v in top[:6]) or "нет"
        if benign:
            summary += " · норма: " + ", ".join(
                f"{k}×{v}" for k, v in sorted(benign.items(), key=lambda x: -x[1]))
        total = real_total
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
        cfg = json.loads(BOT_CFG.read_text(encoding="utf-8-sig"))
        token, owner = cfg["bot_token"], cfg["owner_id"]
        api = (cfg.get("local_bot_api") or "https://api.telegram.org").rstrip("/")
        import urllib.parse

        def _post(chunk: str, html: bool):
            fields = {"chat_id": owner, "text": chunk, "disable_web_page_preview": "true"}
            if html:
                fields["parse_mode"] = "HTML"
            data = urllib.parse.urlencode(fields).encode()
            urllib.request.urlopen(
                urllib.request.Request(f"{api}/bot{token}/sendMessage", data=data), timeout=25)

        # Telegram hard limit 4096; chunk.
        for i in range(0, len(text), 3800):
            chunk = text[i:i + 3800]
            try:
                _post(chunk, html=True)
            except urllib.error.HTTPError as he:
                # A markup complaint must never cost the owner the whole report:
                # resend the chunk as plain text rather than losing it.
                if he.code != 400:
                    raise
                print("[healthcheck] HTML rejected (400) — resending chunk as plain text")
                _post(_strip_html(chunk), html=False)
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


def check_config_bom():
    """A UTF-8 BOM in a JSON/YAML config silently disables whatever reads it.

    2026-08-03: a PowerShell write (`>`/`Set-Content` default to UTF-8-with-BOM on
    this box) put an EF BB BF on tgbot/config.json. `json.loads` then raised on the
    very first character, and because every reader wraps that call in a bare
    `except`, the damage was invisible: THIS checker could not message the owner,
    check_botapi_responsive() decided "no bot configured" and skipped itself, and
    telemetry reported "config недоступен". Readers now use utf-8-sig, but code we
    don't control (engines, anything new) still reads plain utf-8 — so strip it.
    """
    targets = [BOT_CFG, ROOT / "tgbot" / "users.json", CONFIG]
    hit = []
    for p in targets:
        try:
            if not p.is_file():
                continue
            raw = p.read_bytes()
            if not raw.startswith(b"\xef\xbb\xbf"):
                continue
            hit.append(p.name)
            if NO_FIX:
                continue
            p.write_bytes(raw[3:])
        except Exception as e:
            warn(f"BOM-проверка {p.name} не удалась: {str(e)[:60]}")
    if not hit:
        ok("Конфиги без BOM (читаются всеми)")
        return
    if NO_FIX:
        warn(f"BOM в конфигах: {', '.join(hit)} — ломает читателей на plain utf-8")
        return
    still = [p.name for p in targets
             if p.is_file() and p.read_bytes().startswith(b"\xef\xbb\xbf")]
    if still:
        warn(f"BOM снять не удалось: {', '.join(still)} — нужен ручной разбор")
    else:
        fixed(f"Снят UTF-8 BOM с конфигов: {', '.join(hit)} "
              f"(из-за него молча отваливались отчёт в бот и часть проверок)")


def main():
    # Before anything else: a BOM here would break the owner report itself.
    check_config_bom()
    app_ok = check_app()
    if app_ok:
        check_apple_wrapper()
        check_apple_bearer()
        check_apple_pool_slots()
        # Обязан идти ПОСЛЕ всех, кто контейнеры поднимает: `ensure_container`
        # делает `docker start`/`docker run`, и пока процесс приложения старый
        # (запущен до правки `ripster/amd.py`), он пересоздаёт враппер своей
        # командой с ГОЛЫМ портом — то есть любой автоподъём обязан попадать под
        # проверку публикации.
        check_wrapper_exposure()
        check_public_wrapper()
        check_deezer_arls()
        check_qobuz_accounts()
        check_soundcloud_tokens()
        # Tidal и Яндекс заведены 12.09.2026 по просьбе владельца «автохил по
        # ВСЕМ сервисам»: до этого их учётки никто не мерил, и протухшая уходила
        # в загрузку первой просто потому, что стояла выше в списке.
        check_tidal_accounts()
        check_yandex_tokens()
        check_account_duplicates()
        check_pool_identities()
        check_gamdl_cookies()
        check_tokens()
        check_token_files()
        check_spotify_bearer()
        check_engine_probe()
        check_download_canary()
        check_tunnel()
        check_queue()
        check_watchlist()
        check_bbc_radar_urls()
        check_namesake_guard()
        check_bot()
        check_bot_delivery()
        check_botapi_responsive()
        check_external_apis()
        check_disk()
        check_code_newer_than_app()
        check_retry_storms()
        check_pacing()
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
    plain = _strip_html(body)
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
