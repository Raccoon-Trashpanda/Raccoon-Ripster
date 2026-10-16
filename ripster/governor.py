"""Регулятор нагрузки: сколько загрузок можно вести одновременно ПРЯМО СЕЙЧАС.

Владелец 26.09: «бот и Ripster должны САМИ регулировать потоки качающих, чтобы не
было постоянной максималки и ничего не ломалось; переносимо на арендуемый сервер».

Что делает:
* фоновый поток раз в POLL_S секунд меряет CPU (сглаженно), свободную RAM и диск
  папки загрузок;
* выводит уровень normal / busy / overload / critical и потолок одновременных
  загрузок для очереди (runner.process_queue берёт его перед стартом новой задачи);
* уже идущие загрузки НИКОГДА не обрываются — потолок влияет только на старт новых;
* состояние отдаётся в /api/governor — его читает бот (авто-техработы при
  затяжной критике) и панель.

Профиль «тихо, но быстро» (владелец 26.09: «комп максимально тихий, но быстрый в
отдаче; не как в ту ночь, когда гудел»):
* регулятор сам узнаёт железо (ядра, RAM) и из него выводит потолок и пороги RAM —
  на мощном ПК потолок выше, на арендованном сервере подстроится сам;
* пороги CPU нарочно низкие (средний CPU держится около половины — вентиляторы не
  уходят в вой), ночью (governor-quiet-hours, по умолчанию 0-8) ещё ниже;
* сервер и всё, что он запускает (ffmpeg, расшифровка), идут с пониженным
  приоритетом — Windows отдаёт им CPU только когда он не нужен владельцу.
Любой порог можно задать явно ключом config.yaml (governor-*); не задан — «авто».
"""
from __future__ import annotations

import threading
import time

POLL_S = 3.0
EMA_ALPHA = 0.25          # сглаживание CPU: один всплеск ffmpeg не роняет потолок

def hardware() -> dict:
    """Что за железо: логические/физические ядра и RAM (ГБ)."""
    try:
        import psutil
        logical = psutil.cpu_count(logical=True) or 4
        physical = psutil.cpu_count(logical=False) or max(1, logical // 2)
        ram = psutil.virtual_memory().total / 1e9
    except Exception:
        logical, physical, ram = 4, 2, 8.0
    return {"cpu_logical": int(logical), "cpu_physical": int(physical), "ram_total_gb": round(ram, 1)}


def auto_defaults(hw: dict | None = None) -> dict:
    """Пороги из железа. Потолок — по числу физических ядер (3..8): загрузка в
    основном ждёт сеть, а тишину держат пороги CPU ниже — как только средний CPU
    растёт, потолок сам падает. RAM-пороги — доля от общей памяти."""
    hw = hw or hardware()
    cap = max(3, min(8, hw["cpu_physical"]))
    ram = hw["ram_total_gb"]
    return {
        "governor-enabled": True,
        "governor-low-priority": True,
        "governor-quiet-hours": "0-8",
        "governor-max-active": cap,
        "governor-busy-active": max(2, cap // 2),
        "governor-overload-active": 1,
        "governor-cpu-busy": 55,          # % CPU (сглаженный) — тихий профиль
        "governor-cpu-overload": 70,
        "governor-cpu-critical": 90,
        "governor-ram-busy-gb": round(max(2.0, ram * 0.15), 1),
        "governor-ram-overload-gb": round(max(1.5, ram * 0.08), 1),
        "governor-ram-critical-gb": round(max(1.0, ram * 0.04), 1),
    }


DEFAULTS = auto_defaults()
QUIET_CPU_SHIFT = 10      # ночью пороги CPU ниже на столько процентов

_LEVELS = ("normal", "busy", "overload", "critical")

_lock = threading.Lock()
_state = {"level": "normal", "cap": DEFAULTS["governor-max-active"], "cpu": 0.0,
          "ram_free_gb": 0.0, "disk_free_gb": 0.0, "reasons": [], "since": time.time(),
          "ts": 0.0, "enabled": True}
#: Хвост переходов между уровнями: когда, откуда куда, какой CPU и КТО грузил. Без него на вопрос
#: «почему в 10:33 было 100 %» ответить было нечем: регулятор решал, но следов не оставлял.
_history: list[dict] = []
_HISTORY_MAX = 60
_cfg_get = None           # функция чтения config (ставит start())
_disk_path = None
_thread: threading.Thread | None = None


def _c(key: str):
    v = None
    if _cfg_get:
        try:
            v = _cfg_get(key)
        except Exception:
            v = None
    return DEFAULTS[key] if v in (None, "", "auto") else v


def in_quiet_hours(spec: str, hour: int) -> bool:
    """«0-8» — с 0:00 до 8:00; «23-7» — через полночь; пусто/off — никогда."""
    try:
        a, b = (int(x) for x in str(spec).split("-"))
    except Exception:
        return False
    return (a <= hour < b) if a <= b else (hour >= a or hour < b)


def classify(cpu: float, ram_free_gb: float, cfg=None, quiet: bool = False) -> tuple[str, int, list[str]]:
    """(уровень, потолок, причины) — чистая функция, её и тестируем.
    quiet — ночные часы: пороги CPU ниже на QUIET_CPU_SHIFT, потолок вдвое меньше."""
    g0 = (lambda k: (cfg or {}).get(k, DEFAULTS[k])) if cfg is not None else _c
    shift = QUIET_CPU_SHIFT if quiet else 0

    def g(k):
        v = g0(k)
        return float(v) - shift if k.startswith("governor-cpu-") else v
    reasons = []
    level = 0
    checks = (
        (cpu >= float(g("governor-cpu-critical")), 3, f"CPU {cpu:.0f}%"),
        (ram_free_gb <= float(g("governor-ram-critical-gb")), 3, f"RAM {ram_free_gb:.1f} ГБ"),
        (cpu >= float(g("governor-cpu-overload")), 2, f"CPU {cpu:.0f}%"),
        (ram_free_gb <= float(g("governor-ram-overload-gb")), 2, f"RAM {ram_free_gb:.1f} ГБ"),
        (cpu >= float(g("governor-cpu-busy")), 1, f"CPU {cpu:.0f}%"),
        (ram_free_gb <= float(g("governor-ram-busy-gb")), 1, f"RAM {ram_free_gb:.1f} ГБ"),
    )
    for hit, lv, why in checks:
        if hit and lv >= level:
            if lv > level:
                reasons = []
            level = lv
            if why not in reasons:
                reasons.append(why)
    caps = [int(g("governor-max-active")), int(g("governor-busy-active")),
            int(g("governor-overload-active")), 0]
    if quiet:
        caps = [max(1, caps[0] // 2), max(1, caps[1] // 2), caps[2], 0]
    return _LEVELS[level], max(0, caps[level]), reasons


def top_consumers(n: int = 4, window_s: float = 1.2) -> list[dict]:
    """Кто грузит процессор ПРЯМО СЕЙЧАС: дельта CPU за [window_s] секунд (не накопленное время —
    урок 22.09). Доля — от ВСЕЙ машины. Имена без командной строки: в логе она не нужна и бывает
    длинной/с секретами."""
    try:
        import psutil
        ncpu = psutil.cpu_count(logical=True) or 1
        t0 = {}
        for p in psutil.process_iter(["pid", "name"]):
            try:
                c = p.cpu_times()
                t0[p.pid] = (c.user + c.system, p.info.get("name") or "?")
            except Exception:
                continue
        time.sleep(window_s)
        agg: dict[str, float] = {}
        for p in psutil.process_iter(["pid", "name"]):
            try:
                if p.pid not in t0:
                    continue
                c = p.cpu_times()
                d = (c.user + c.system) - t0[p.pid][0]
                if d > 0:
                    nm = t0[p.pid][1]
                    agg[nm] = agg.get(nm, 0.0) + d / window_s / ncpu * 100.0
            except Exception:
                continue
        top = sorted(agg.items(), key=lambda kv: kv[1], reverse=True)[:n]
        return [{"name": k, "pct": round(v, 1)} for k, v in top if v >= 1.0]
    except Exception:
        return []


def _note_transition(old: str, new: str, cpu: float, reasons: list[str]) -> None:
    """Запомнить смену уровня; при ухудшении до busy и выше — ещё и кто грузит. Вызывается из потока
    замеров (он и так спит между замерами, лишние 1–2 с ему не мешают)."""
    worse = _LEVELS.index(new) > _LEVELS.index(old)
    top = top_consumers() if worse and new != "normal" else []
    rec = {"ts": time.time(), "from": old, "to": new, "cpu": round(cpu, 1),
           "reasons": list(reasons), "top": top}
    with _lock:
        _history.append(rec)
        del _history[:-_HISTORY_MAX]
        if top:
            _state["top"] = top
            _state["top_ts"] = rec["ts"]
    who = ", ".join(f"{t['name']} {t['pct']:.0f}%" for t in top) or "—"
    print(f"[governor] {old} → {new}: CPU {cpu:.0f}% ({'; '.join(reasons) or '—'}); грузят: {who}", flush=True)


def history(limit: int = 30) -> list[dict]:
    with _lock:
        return list(_history[-limit:])


def is_calm() -> bool:
    """Тихо ли сейчас для НЕсрочной тяжёлой работы (канарейка, сканеры): уровень normal или
    регулятор ещё не мерил/выключен. Идущие загрузки гостей эта проверка не касается."""
    with _lock:
        if not _state["ts"] or not _state["enabled"]:
            return True
        return _state["level"] == "normal"


def bg_discovery_paused() -> bool:
    """Притормозить ли фоновую РАЗВЕДКУ (spotify-краул, сверка лейблов, радар) —
    то, что не «отдача релизов» и может подождать.

    True, когда: регулятор под нагрузкой (level != normal) ИЛИ тихие часы.
    Ночью владелец хочет ТИШИНЫ, а не среднего CPU: даже при низкой загрузке
    непрерывные пассы сканеров крутят кулер (жалоба 15.10). Выключен/не мерил —
    не паузим. Канарейки выживания загрузок и срочных задач НЕ касается: для них
    отдельный `is_calm()`, этот гейт применяют ТОЛЬКО к разведке."""
    with _lock:
        if not _state["ts"] or not _state["enabled"]:
            return False
        return bool(_state.get("quiet")) or _state["level"] != "normal"


def _sample_loop() -> None:
    import psutil
    ema = None
    psutil.cpu_percent(interval=None)                   # первый вызов — точка отсчёта
    while True:
        time.sleep(POLL_S)
        try:
            cpu = psutil.cpu_percent(interval=None)
            ema = cpu if ema is None else (EMA_ALPHA * cpu + (1 - EMA_ALPHA) * ema)
            ram = psutil.virtual_memory().available / 1e9
            try:
                disk = psutil.disk_usage(_disk_path or ".").free / 1e9
            except Exception:
                disk = 0.0
            enabled = str(_c("governor-enabled")).lower() not in ("false", "0", "no")
            quiet = in_quiet_hours(_c("governor-quiet-hours"), time.localtime().tm_hour)
            level, cap, reasons = classify(ema, ram, quiet=quiet)
            if not enabled:
                level, cap, reasons = "normal", int(_c("governor-max-active")), []
            prev_level = _state["level"] if _state["ts"] else None
            if prev_level is not None and level != prev_level:
                _note_transition(prev_level, level, ema, reasons)
            with _lock:
                if level != _state["level"]:
                    _state["since"] = time.time()
                _state.update(level=level, cap=cap, cpu=round(ema, 1), ram_free_gb=round(ram, 1),
                              disk_free_gb=round(disk, 1), reasons=reasons, ts=time.time(),
                              enabled=enabled, quiet=quiet)
        except Exception:
            pass


def start(cfg_get, disk_path=None) -> None:
    """Запустить замеры (идемпотентно). cfg_get(key) — чтение живого config."""
    global _cfg_get, _disk_path, _thread
    _cfg_get, _disk_path = cfg_get, disk_path
    if _thread and _thread.is_alive():
        return
    _state["hardware"] = hardware()
    if str(_c("governor-low-priority")).lower() not in ("false", "0", "no"):
        lower_priority()
    _thread = threading.Thread(target=_sample_loop, name="governor", daemon=True)
    _thread.start()


def lower_priority() -> None:
    """Сервер и его будущие дочерние процессы — ниже обычного приоритета. В Windows
    дочерний процесс наследует класс BELOW_NORMAL, так что ffmpeg/расшифровка тоже
    уступают CPU интерактивной работе владельца."""
    try:
        import psutil
        p = psutil.Process()
        if hasattr(psutil, "BELOW_NORMAL_PRIORITY_CLASS"):
            p.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
        else:
            p.nice(5)
        _state["priority"] = "below_normal"
    except Exception as e:
        _state["priority"] = f"unchanged ({e})"


def state() -> dict:
    with _lock:
        s = dict(_state)
    s["for_s"] = int(time.time() - s["since"])
    return s


def cap() -> int | None:
    """Потолок одновременных загрузок; None — регулятор ещё не мерил (не мешать)."""
    with _lock:
        if not _state["ts"] or not _state["enabled"]:
            return None
        return int(_state["cap"])
