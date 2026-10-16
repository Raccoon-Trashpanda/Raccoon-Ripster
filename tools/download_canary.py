# -*- coding: utf-8 -*-
r"""Download canary — проверка, что файл реально СКАЧИВАЕТСЯ, а не только что сервис отвечает.

Причина появления (24.09.2026, утро): Apple с ~00:00 отвечала Invalid CKC на
КАЖДЫЙ релиз, и телефоны, и гости весь день получали «Ни одна своя Apple-
учётка не дала ключ». Никто не заметил за 12 часов, потому что дневной
sweep (ripster_healthcheck.check_engine_probe) проверяет только «службы
отвечают» — а «отвечают» и «отдают файл» это РАЗНЫЕ вопросы.

Канарейка раз в 3 часа (и по требованию) качает ОДИН короткий заведомо
исправный трек каждого настроенного сервиса ЧЕРЕЗ СОБСТВЕННЫЙ ПУТЬ ЗАДАЧ
ПРИЛОЖЕНИЯ (ripster.runner.run_task — тот же код, что проходит гость),
проверяет файл (существует, > 100 КБ, длительность ffprobe в пределах ±2 с,
кодек соответствует запрошенному качеству) и сносит за собой в свою temp-
папку — библиотека владельца не загрязняется никогда.

Состояние — C:\dev\LOG\download_canary.json (series on streak).
Оповещение — РОВНО ОДНО сообщение владельцу на смену состояния (2 подряд
падения, либо первое, если ошибка похожа на «учётка мертва»), и одно
«восстановлено» на возврат. Никакого спама.

Запуск:
  в приложении — фоновый цикл app.py вызывает run_loop(config)
  по требованию — .venv\\Scripts\\python.exe tools\\download_canary.py --once
  состояние    — ... --status
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# Console of owner is cp1251: silence unencodable chars, keep Russian as is.
for _s in (sys.stdout, sys.stderr):
    try: _s.reconfigure(errors="replace")
    except Exception: pass

ROOT       = Path(__file__).resolve().parent.parent
# CLI «python tools\download_canary.py» стартует с sys.path[0]=tools/ — без
# корня репозитория `import ripster` в автономном режиме невозможен.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

#: Корень установки Orpheus-движки вычисляют как
#: `Path(sys.argv[0]).resolve().parent` (orpheus_spotify, orpheus_beatport,
#: soundcloud, tidal, zotify, sc_widevine). Боевое приложение — `python app.py`
#: из корня, поэтому там это корень репозитория. Канарейка живёт в tools/ и,
#: оставь она argv как есть, отдала бы движкам `ROOT/tools`: `is_installed()`
#: искала бы `tools/orpheus/orpheus.py`, не нашла и выдала «OrpheusDL не
#: установлен» — Spotify и Beatport легли бы в отчёт мёртвыми, каких их в
#: продукте нет. Хуже того, то же относится к canary-циклу внутри приложения,
#: если его запускают скриптом из tools/. Значение ниже — каноническое и для
#: приложения, и для автономного прогона. Урок tidal_pool (24.09): путь от
#: запускающего скрипта ломается ровно там, где им проверяют.
sys.argv[0] = str(ROOT / "app.py")
TRACKS     = ROOT / "tools" / "download_canary_tracks.json"
STATE_FILE = Path(r"C:\dev\LOG\download_canary.json")
BOT_CFG    = ROOT / "tgbot" / "config.json"

INTERVAL_S  = 3 * 3600.0        # период canary-прохода
BOOT_GRACE  = 180.0             # не стартовать в разгар запуска приложения
TICK        = 300.0             # цикл просыпается и перепланирует
TASK_TIMEOUT = 600.0            # один трек дольше 10 минут — уже падение
MIN_BYTES    = 100_000          # «скачалось» меньше 100 КБ — это не файл
DUR_TOL_S    = 2.0              # длительность: ±2 секунды от ожидаемой

# Подписи «учётка мертва»: здесь даже ПЕРВОЕ падение — повод написать,
# тянуть до второго нельзя (весь инцидент 24.09 и был «вторым падением»,
# наступавшим 12 часов).
_DEAD_SIGNS = re.compile(
    r"invalid ckc|no subscription|not available for this account"
    r"|territory restricted|device limit|arl.*expir|premium required"
    r"|ключ.*ни одна|не отда.*ключ|ни одна.*не дала ключ",
    re.I)

# тишина сервиса «настройкен, но молчит весь день» для healthcheck'а — секунды
STALE_S = 3 * INTERVAL_S


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(raw) -> "datetime | None":
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except Exception:
        return None


# ── Tracks ────────────────────────────────────────────────────────────────────

def load_tracks() -> dict:
    try:
        data = json.loads(TRACKS.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return {k: v for k, v in data.items()
            if k != "_comment" and isinstance(v, dict)}


# ── Which services are configured (same question /api/services/status asks) ──

def configured_services(cfg: dict) -> "list[str]":
    def has(key: str) -> bool:
        return bool(str(cfg.get(key) or "").strip())
    out = []
    if True:
        out.append("apple")           # как в /api/services/status: всегда
    if has("qobuz-auth-token"):    out.append("qobuz")
    if has("deezer-arl"):          out.append("deezer")
    if has("tidal-token"):         out.append("tidal")
    sp = False
    try:
        from ripster.engines.orpheus_spotify import is_authenticated as _spa
        sp = bool(_spa())
    except Exception:
        pass
    if sp or has("spotify-sp-dc") or has("spotify-client-id"):
        out.append("spotify")
    if has("beatport-username"):       out.append("beatport")
    if has("soundcloud-oauth-token"):  out.append("soundcloud")
    if has("yandex-token"):            out.append("yandex")
    return out


# ── ffprobe ───────────────────────────────────────────────────────────────────

def _ffprobe_path(cfg: dict) -> str:
    """ffprobe ищем рядом с настроенным ffmpeg — так же, как раннер
    (см. orpheus_jiosaavn.py): PATH между оболочками различается."""
    ff = str((cfg or {}).get("gamdl-ffmpeg-path") or "").strip()
    if ff:
        p = Path(ff)
        cand = p.with_name("ffprobe" + (".exe" if p.suffix.lower() == ".exe" else ""))
        if cand.exists():
            return str(cand)
    return shutil.which("ffprobe") or "ffprobe"


def probe_media(path: str, cfg: dict) -> dict:
    """-show_streams codec_name/codec_type + format duration одним прогоном;
    {} при аварии.

    Разделители в `-show_entries` НЕ перепутать: секции делятся ДВОЕТОЧИЕМ,
    поля внутри секции — ЗАПЯТОЙ. На «stream=codec_name:codec_type:format=…»
    ffprobe отвечал «No match for section 'codec_type'» с пустым stdout и
    ненулевым кодом: парсер молча выдавал {"codec": "", "duration": 0.0}, и
    canary валил ЗДОРОВЫЕ файлы формулировкой «длительность 0.0 с ≠ 169.0».
    Так 24.09 выглядели qobuz/deezer/yandex в первом же боевом прогоне — то
    есть ни одна витрина не могла пройти проверку в принципе.
    """
    cmd = [_ffprobe_path(cfg), "-v", "error",
           "-show_entries", "stream=codec_name,codec_type:format=duration",
           "-of", "json", path]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60,
                           creationflags=0x08000000 if os.name == "nt" else 0)
        # Пустой stdout при ненулевом коде — это не «файл без длительности»,
        # это сломанный ffprobe/аргументы: врать про 0.0 с нельзя.
        if r.returncode != 0 or not (r.stdout or "").strip():
            return {}
        j = json.loads(r.stdout)
        codecs = [s.get("codec_name", "") for s in j.get("streams", [])
                  if s.get("codec_type") == "audio"]
        return {"codec": codecs[0] if codecs else "",
                "duration": float(j.get("format", {}).get("duration") or 0)}
    except Exception:
        return {}


# ── Verification of one downloaded canary file ───────────────────────────────

_AUDIO_EXT = {".flac", ".m4a", ".mp3", ".ogg", ".opus", ".wav", ".alac", ".aiff"}

#: Строки журнала, которые раннер только широковещает (см. `_collect_log`).
_BC_SINK: "list[dict]" = []


def _collect_log(msg) -> None:
    """Единственное место, где canary слушает консоль раннера.

    `_log()` раннера только широковещает, в `task["log"]` не пишет (кроме
    ветки перебора Apple-учёток), поэтому у standalone-прогона без этого стока
    упавшая задача остаётся без текста. Наружу строки не идут: метки учёток
    маскируются в самом раннере, а canary печатает только вердикт.
    """
    if isinstance(msg, dict) and msg.get("type") == "log":
        _BC_SINK.append(msg)


def _bc_tail(task_id: str, started_at: int) -> str:
    """Последние строки журнала ЭТОЙ задачи — то, что человек увидел бы в
    консоли приложения. Уровень `error`/`warn` важнее прочего: падение обычно
    подписано им, а мусором вокруг него служит прогресс."""
    rows = [m for m in _BC_SINK[int(started_at):]
            if str(m.get("task_id") or "") in (str(task_id), "")]
    if not rows:
        return ""
    prio = [m for m in rows if str(m.get("level") or "") in ("error", "warn")]
    picked = (prio or rows)[-4:]
    return " | ".join(str(m.get("text") or "").split("] ", 1)[-1] for m in picked)[:400]


def verify_downloads(tmp_dir: Path, track: dict, cfg: dict) -> dict:
    """Требования: файл есть, > 100 КБ, длительность в ±2 с, кодек свой."""
    files = [p for p in tmp_dir.rglob("*")
             if p.suffix.lower() in _AUDIO_EXT and p.is_file()]
    if not files:
        return {"ok": False, "error": "ни одного аудиофайла в canary-папке"}
    biggest = max(files, key=lambda p: p.stat().st_size)
    size = biggest.stat().st_size
    if size <= MIN_BYTES:
        return {"ok": False, "error": f"файл {size} Б ≤ {MIN_BYTES} Б — не загрузка"}
    info = probe_media(str(biggest), cfg)
    if not info:
        return {"ok": False, "error": f"ffprobe не ответил для {biggest.name}"}
    exp_dur = float(track.get("duration_s") or 0)
    if exp_dur and abs(info["duration"] - exp_dur) > DUR_TOL_S:
        return {"ok": False, "error": f"длительность {info['duration']:.1f} с ≠ "
                                      f"{exp_dur:.1f} с (допуск ±{DUR_TOL_S} с)"}
    codecs = {str(c).lower() for c in (track.get("codecs") or [])}
    if codecs and info["codec"].lower() not in codecs:
        return {"ok": False, "error": f"кодек {info['codec']!r} не среди "
                                      f"{sorted(codecs)}"}
    return {"ok": True, "file": biggest.name, "size": size,
            "duration": round(info["duration"], 2), "codec": info["codec"]}


# ── The download itself: app's OWN task path ─────────────────────────────────

async def download_one(service: str, track: dict, cfg: dict) -> dict:
    """Один трек через runner.run_task — тот же код, что проходит гость.

    _canary_save_path указывает в temp-папку canary (раннер подхватывает его
    в run_task), поэтому в библиотеку владельца не попадает ничего; по
    завершении папка сносится целиком.
    """
    from ripster import runner, service_layer
    from ripster.routes.queue import _make_task

    url = track.get("url") or ""
    if not url:
        return {"ok": False, "error": "в download_canary_tracks.json нет url"}
    quality = service_layer.default_quality(service)

    engine = service_layer.engine_for_svc(service)
    if service == "apple":
        # Та же маршрутизация, что у гостя в /api/queue/add: движок+враппер
        # по факту доступности качества (apple_router — единственный источник
        # истины, публичный враппер оттуда же — manual-only).
        try:
            from ripster.apple_router import route_apple
            routed = await asyncio.to_thread(route_apple, quality, cfg, url)
            engine, quality = routed["engine"], routed["quality"]
        except Exception:
            pass

    tmp = Path(tempfile.gettempdir()) / "ripster_canary" / service
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)

    t = _make_task(url, quality, engine, service, source="download_canary")
    t["_canary_save_path"] = str(tmp)
    started = time.monotonic()
    bc_mark = len(_BC_SINK)

    async def _one_round():
        left = TASK_TIMEOUT - (time.monotonic() - started)
        if left <= 0:
            raise asyncio.TimeoutError()
        await asyncio.wait_for(runner.run_task(t), left)

    try:
        await _one_round()
        # Авто-повтор и перебор учёток возвращают run_task со статусом
        # «queued»: в приложении задачу подхватывает очередь, здесь её
        # никто не подхватит — канарейка прогоняет сама, иначе временная
        # авария засчиталась бы падением (так tidal упал «queued» 24.09,
        # хотя на третьем слоте ключ был).
        while t.get("status") == "queued" and int(t.get("_retry_count") or 0) <= 6:
            await _one_round()
        err = str(t.get("error") or "")
        if t.get("status") != "done":
            if not err:
                tail = [str(x) for x in (t.get("log") or []) if str(x).strip()]
                err = " | ".join(tail[-3:])          # без этого — голый статус вместо диагноза
            if not err:
                # `task["log"]` пустой — норма: `_log()` раннера только
                # широковещает. Без этого прохода canary сдавалась словами
                # «задача завершилась статусом error» (SoundCloud 24.09), и
                # владельцу было нечего читать.
                err = _bc_tail(t.get("id", ""), bc_mark)
            return {"ok": False,
                    "error": err or f"задача завершилась статусом {t.get('status')}"}
        v = verify_downloads(tmp, track, cfg)
        v["ms"] = int((time.monotonic() - started) * 1000)
        v["quality"] = quality
        if not v.get("ok"):
            v["error"] = f"файл не прошёл проверку: {v.get('error')} " \
                         f"(трек {service}, качество {quality})"
        return v
    except asyncio.TimeoutError:
        return {"ok": False, "error": f"таймаут {int(TASK_TIMEOUT)} с"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"[:400]}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ── State + alerts ────────────────────────────────────────────────────────────

def load_state() -> dict:
    try:
        st = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(st, dict):
            st.setdefault("services", {})
            return st
    except Exception:
        pass
    return {"version": 1, "services": {}}


def save_state(st: dict) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        print(f"[canary] состояние не сохранено: {e}", flush=True)


def _fmt_hm(secs: float) -> str:
    secs = int(secs)
    if secs < 90:      return f"{secs} с"
    if secs < 5400:    return f"{secs // 60} мин"
    return f"{secs / 3600:.1f} ч"


def _dead_sign(err: str) -> bool:
    return bool(_DEAD_SIGNS.search(err or ""))


# ТИХОЕ понижение качества: скачалось «успешно», но приехал не тот кодек
# (24.09.2026 14:01Z beatport/hifi: «кодек 'aac' не среди ['flac']»). Это не
# дождаться второго падения подряд: в отличие от сети, битый тариф/токен
# повторяется молча, а зелёная папка «FLAC» с AAC внутри — порча библиотеки.
_QUALITY_SIGNS = re.compile(r"кодек\s+'.+' не среди|не прошед\w+ проверку: кодек",
                            re.I)


async def record_results(st: dict, results: dict, *, notify: bool = True) -> None:
    """Обновляет серию и шлёт ровно по одному сообщению на смену состояния.

    down: 2 падения подряд, либо первое с подписью «учётка мертва».
    up:   успех после отправленного down. Больше — молчим.
    """
    for svc, r in results.items():
        s = st["services"].setdefault(svc, {})
        was_down = s.get("alerted") == "down"
        now_iso = _now_iso()
        if r.get("ok"):
            s["ok"] = True
            s["streak_fail"] = 0
            s["last_ok"] = now_iso
            s["last_ms"] = r.get("ms")
            if was_down:
                downtime = ""
                since = _parse_iso(s.get("since"))
                if since:
                    downtime = _fmt_hm(
                        (datetime.now(timezone.utc) - since).total_seconds())
                await _notify(
                    f"🐤 <b>Canary: {html.escape(svc.upper())} — восстановлен</b>"
                    + (f"\nпростой {html.escape(downtime)}" if downtime else "")
                    + f"\n{html.escape(str(r.get('file') or ''))} — "
                      f"{r.get('duration', '?')} с, кодек {html.escape(str(r.get('codec') or '?'))}",
                    notify)
                s["alerted"] = None
                s["since"] = None
            continue
        s["ok"] = False
        s["streak_fail"] = int(s.get("streak_fail") or 0) + 1
        s["last_fail"] = now_iso
        s["last_error"] = str(r.get("error") or "")[:600]
        if not s.get("since"):
            s["since"] = now_iso
        if not was_down and (s["streak_fail"] >= 2 or _dead_sign(s["last_error"])
                             or _QUALITY_SIGNS.search(s["last_error"])):
            dead = "\n⚠ похоже на «учётка мертва» — писать сразу, не дожидаясь второго" \
                if _dead_sign(s["last_error"]) else ""
            if _QUALITY_SIGNS.search(s["last_error"]):
                dead += ("\n⚠ тихое понижение качества: скачалось «успешно», "
                         "но приехал не заказанный кодек")
            await _notify(
                f"🐤 <b>Canary: {html.escape(svc.upper())} — скачивание мертво</b>\n"
                f"подряд падений: {s['streak_fail']}\n"
                f"с: {html.escape(str(s.get('since')))}\n"
                f"ошибка: {html.escape(s['last_error'][:300])}{dead}",
                notify)
            s["alerted"] = "down"


async def _notify(text: str, enabled: bool = True) -> None:
    """Одно сообщение владельцу через локальный Bot API (127.0.0.1:8081).

    Только владелец: chat_id = owner_id из tgbot/config.json. Токен не
    печатается никогда. Локального API нет в конфиге — берём cfg, как везде.
    """
    if not enabled:
        return
    try:
        cfg = json.loads(BOT_CFG.read_text(encoding="utf-8-sig"))
        token, owner = cfg["bot_token"], cfg["owner_id"]
        api = (cfg.get("local_bot_api") or "https://api.telegram.org").rstrip("/")
        fields = {"chat_id": owner, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": "true"}
        data = urllib.parse.urlencode(fields).encode()
        def _post():
            req = urllib.request.Request(f"{api}/bot{token}/sendMessage", data=data)
            with urllib.request.urlopen(req, timeout=25) as r:
                return r.status
        await asyncio.to_thread(_post)
    except Exception as e:
        print(f"[canary] владелец-уведомление не отправлено: {str(e)[:120]}", flush=True)


# ── The pass ──────────────────────────────────────────────────────────────────

async def canary_pass(cfg: dict, services: "list[str] | None" = None,
                      *, notify: bool = True) -> dict:
    """Один проход: каждый настроенный сервис — по одному короткому треку.

    Собирает результат в состояние и решает оповещение. Возвращает dict
    {service: result} — healthcheck и CLI печатают его, не перекачивая.
    """
    tracks = load_tracks()
    if services is None:
        services = [s for s in configured_services(cfg) if s in tracks]
    st = load_state()
    results: dict = {}
    for svc in services:
        track = tracks.get(svc) or {}
        try:
            if svc not in configured_services(cfg):
                results[svc] = {"ok": False,
                                "error": "сервис не настроен (нет учётных данных)"}
                continue
            results[svc] = await download_one(svc, track, cfg)
        except Exception as e:
            results[svc] = {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]}
        p = Path(tempfile.gettempdir()) / "ripster_canary" / svc
        shutil.rmtree(p, ignore_errors=True)
    st["last_run"] = _now_iso()
    st["last_pass"] = {s: {"ok": bool(r.get("ok")),
                           "error": str(r.get("error") or "")[:300]}
                       for s, r in results.items()}
    await record_results(st, results, notify=notify)
    save_state(st)
    try:
        shutil.rmtree(Path(tempfile.gettempdir()) / "ripster_canary",
                      ignore_errors=True)
    except Exception:
        pass
    return results


def next_delay(state: dict, *, now: "datetime | None" = None) -> float:
    """Сколько секунд спать до следующего прохода — СЧИТАЯ ОТ ПОСЛЕДНЕГО.

    Урок 09.08 (watchlist): рестарт не должен обнулять часы. Нет записи —
    запускаем скоро (после бут-грейса); запись была 2.5 ч назад — спим 0.5 ч.
    """
    now = now or datetime.now(timezone.utc)
    last = _parse_iso(state.get("last_run"))
    if last is None:
        return BOOT_GRACE
    since = max(0.0, (now - last).total_seconds())
    return max(0.0, INTERVAL_S - since)


async def run_loop(cfg: dict, base_dir: "Path | None" = None) -> None:
    """Фоновый цикл приложения: каждые 3 часа, часы — из state-файла.

    Цикл просыпается тиками и перепланирует по last_run из
    C:\\dev\\LOG\\download_canary.json, поэтому рестарт посреди интервала
    стоит только пропуска спящего тика, а не отсчёта с нуля.
    """
    booted = time.monotonic()
    while True:
        delay = next_delay(load_state())
        left_grace = BOOT_GRACE - (time.monotonic() - booted)
        if delay > 0:
            await asyncio.sleep(min(max(delay, left_grace), TICK))
            continue
        if left_grace > 0:
            await asyncio.sleep(min(TICK, left_grace))
            continue
        try:
            await canary_pass(cfg)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[canary] проход упал: {e}", flush=True)
        await asyncio.sleep(TICK)


# ── Standalone context (CLI without a running app) ───────────────────────────

def _install_standalone() -> dict:
    """Минимальный AppContext: тот же runner.run_task вне запущенного приложения.

    history/broadcast — пустышки: автономная проверка не пишет в боевую
    историю и не кричит в WebSocket; движки работают subprocess-ами как
    обычно. save-path раннером перебивается _base_save_path задачи.
    """
    from ripster import runner, service_layer
    from ripster.app_context import AppContext
    from ripster.config_service import load_config
    from ripster.queue_manager import QueueManager
    from ripster.service_layer import detect_service

    # Движки регистрируются импортом модуля — как в app.py: без этого
    # get_engine('qobuz') знает только [].
    import pkgutil
    import ripster.engines as _engines_pkg
    for _mi in pkgutil.iter_modules(_engines_pkg.__path__):
        if _mi.name in {"base", "registry", "__init__", "streamrip_utils", "errors"}:
            continue
        try:
            __import__(f"ripster.engines.{_mi.name}")
        except Exception:
            pass

    cfg = load_config(ROOT / "config.yaml", ROOT / "tokens")
    service_layer.install(cfg)

    async def _noop_bc(msg):
        # Поток журнала раннера — ЕДИНСТВЕННОЕ место, где живёт диагноз
        # (см. `_collect_log`). Раньше здесь был `pass`, и canary сдавалась
        # словами «задача завершилась статусом error».
        _collect_log(msg)

    ctx = AppContext(
        config=cfg, save_config=lambda c: None,
        queue=[], queue_manager=QueueManager(),
        download_history=[], save_history=lambda h: None,
        broadcast=_noop_bc, detect_service=detect_service,
        base_dir=ROOT, is_windows=(os.name == "nt"),
        queue_snapshot=lambda: [], validate_url=lambda u: True,
        enrich_meta=None, default_quality=service_layer.default_quality,
        engine_for_svc=service_layer.engine_for_svc,
        process_queue=None)
    runner.install(ctx)
    return cfg


# ── CLI ───────────────────────────────────────────────────────────────────────

def main(argv: "list[str]") -> int:
    once  = "--once" in argv
    status = "--status" in argv
    no_notify = "--no-notify" in argv
    only = None
    for a in argv:
        if a.startswith("--services="):
            only = [s for s in a.split("=", 1)[1].split(",") if s]
    if status or not once:
        st = load_state()
        if not st.get("services"):
            print("canary ещё ни разу не запускалась (нет состояния)")
            return 0
        print(f"последний проход: {st.get('last_run')}")
        rc = 0
        for svc, s in sorted(st["services"].items()):
            flag = "✓" if s.get("ok") else "✗"
            extra = ""
            if not s.get("ok"):
                extra = f"  (подряд {s.get('streak_fail')}, с {s.get('since')})"
            # Для здорового сервиса `last_error` — память о ПАДЕНИИ, а не о
            # текущем состоянии: после восстановления он остаётся в состоянии,
            # и строка «✓ deezer … файл не прошёл проверку» читается как
            # противоречие. Зелёному показываем, когда он прошёл и за сколько.
            detail = (str(s.get("last_error") or "") if not s.get("ok")
                      else f"ок с {s.get('last_ok') or '?'}, {s.get('last_ms')} мс")
            print(f"  {flag} {svc:<10} {detail[:120]}{extra}")
            rc = rc or (0 if s.get("ok") else 1)
        return rc
    if once:
        cfg = _install_standalone()
        res = asyncio.run(canary_pass(cfg, only, notify=not no_notify))
        bad = 0
        for svc, r in sorted(res.items()):
            if r.get("ok"):
                print(f"  ✓ {svc:<10} {r.get('file')} — {r.get('duration')} с, "
                      f"{r.get('codec')}, {r.get('size')} Б, {r.get('ms')} мс")
            else:
                bad += 1
                print(f"  ✗ {svc:<10} {str(r.get('error'))[:200]}")
        return 1 if bad else 0
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
