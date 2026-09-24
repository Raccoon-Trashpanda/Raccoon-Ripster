"""Кто есть кто среди Apple-учёток: страна каждой сессии и жива ли она.

Зачем понадобилось (09.08.2026). У владельца пять Apple-аккаунтов и поднятые
под них контейнеры, но приложение знало страну ровно одного — основного, потому
что только он публикует наружу порт 30020 с данными об учётке. Остальные для
маршрутизатора были безымянными «слотами расшифровки».

Из-за этого случился разбор 08.08: альбом Apparat «A Hum Of Maybe» не отдался
канадскому аккаунту (в CA его нет), и задача ушла в публичный wrapper-manager,
который в тот момент лежал, — 19 минут в пустоту. А рядом простаивали ДВА
британских слота, и в GB этот альбом есть.

Как узнаём страну, не пересоздавая контейнеры. Порт 30020 внутри контейнера
работает всегда, наружу он просто не выведен. Пересоздать слот ради публикации
порта нельзя дёшево: каждый повторный логин жжёт слот устройства у Apple (см.
скилл ripster-apple-wrapper). Поэтому спрашиваем изнутри — `docker exec` и
`/dev/tcp` в bash: ни curl, ни wget в образе нет, а bash есть.

Результат кэшируется: страна аккаунта не меняется, а `docker exec` не бесплатен.
"""
from __future__ import annotations

import json
import re
import subprocess
import time

# Числовые магазины Apple → коды стран. Список не полный намеренно: только то,
# что реально встречается у наших учёток и в ссылках. Неизвестный номер честно
# возвращаем как есть, а не подставляем «us».
STOREFRONT_IDS = {
    "143441": "us", "143444": "gb", "143455": "ca", "143462": "jp",
    "143480": "ru", "143443": "de", "143442": "fr", "143450": "au",
    "143461": "nz", "143503": "tr", "143467": "in", "143503-2": "tr",
    "143446": "nl", "143454": "br", "143456": "se", "143478": "pl",
    "143495": "it", "143465": "kr", "143470": "cn", "143464": "sg",
}

_CACHE: dict[str, tuple[float, str]] = {}   # контейнер → (когда, код страны)
_TTL = 3600.0

_CNW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _account_json(container: str, timeout: float = 8.0) -> dict:
    """Спросить у контейнера его учётку через порт 30020 ИЗНУТРИ."""
    probe = (r'exec 3<>/dev/tcp/127.0.0.1/30020 && '
             r'printf "GET / HTTP/1.0\r\nHost: x\r\n\r\n" >&3 && timeout 5 cat <&3')
    try:
        out = subprocess.run(
            ["docker", "exec", container, "bash", "-c", probe],
            capture_output=True, text=True, timeout=timeout, creationflags=_CNW,
        ).stdout
    except Exception:
        return {}
    m = re.search(r"\{.*\}", out, re.S)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except Exception:
        return {}


def container_storefront(container: str, fresh: bool = False) -> str:
    """Код страны учётки контейнера ('gb', 'ca', …) или '' если не выяснили.

    Пустая строка значит именно «не выяснили» — контейнер мёртв, не отвечает
    или магазин незнакомый. Считать её за «нет доступа» нельзя: на этой подмене
    смыслов уже один раз построили неверный диагноз.
    """
    now = time.time()
    hit = _CACHE.get(container)
    if hit and not fresh and now - hit[0] < _TTL:
        return hit[1]
    sid = str((_account_json(container) or {}).get("storefront_id") or "")
    cc = STOREFRONT_IDS.get(sid.split("-")[0], "")
    if cc:
        _CACHE[container] = (now, cc)
    return cc


def forget_container(container: str) -> None:
    """Забыть всё, что знали об этом контейнере. Обязателен после пересоздания:
    country кэшируется на час, а свежий контейнер с `response type 6` внутри —
    это НЕ та сессия, о которой часом раньше честно ответил тот же номер."""
    _CACHE.pop(container, None)


def release_container(container: str) -> None:
    """Контейнер УДАЛЁН (слот снят вместе с учёткой): освободить порт и все
    следы о нём.

    Отличается от `forget_container` тем, что вычищается запись страны на диске
    и карта аккаунт↔контейнер. Пока контейнер лишь пересоздаётся, memo нужна:
    без неё остановленный слот выпадает из маршрутизации. Когда контейнера
    больше нет — memo врёт: порт 10020+N свободен, а `slot_countries.json`
    по-прежнему «помнит» слот, и следующий `all_slots()` считает его
    существующим. Именно так `rip-wrapper-3`, три недели как мёртвый, продолжал
    занимать слот и порт."""
    forget_container(container)
    try:
        memo = _memo_load()
        if container in memo:
            del memo[container]
            _memo_path().write_text(json.dumps(memo, ensure_ascii=False, indent=1),
                                    encoding="utf-8")
    except Exception:
        pass
    account_map_forget(container=container)


def _memo_path():
    from pathlib import Path
    import os
    base = Path(os.environ.get("RIPSTER_BASE_DIR") or Path(__file__).resolve().parent.parent)
    p = base / "dist" / "docker" / "slot_countries.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _memo_load() -> dict:
    try:
        return json.loads(_memo_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


def _memo_save(container: str, cc: str) -> None:
    """Запомнить страну слота НА ДИСКЕ.

    Зачем не только в памяти: страну мы узнаём, спрашивая работающий контейнер.
    Сборщик простаивающих слотов гасит их через пять минут, и после этого
    выключенная сессия для нас как бы не существует — 09.08.2026 именно поэтому
    британские слоты не выбирались, а альбом уходил к чужому публичному
    wrapper'у. Страна аккаунта не меняется, так что помнить её между запусками
    безопасно и достаточно.
    """
    try:
        d = _memo_load(); d[container] = cc
        _memo_path().write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass


def container_running(container: str) -> bool:
    try:
        out = subprocess.run(["docker", "inspect", "-f", "{{.State.Running}}", container],
                             capture_output=True, text=True, timeout=10,
                             creationflags=_CNW).stdout.strip().lower()
        return out == "true"
    except Exception:
        return False


# Что враппер пишет в свой журнал, когда сесть некуда. Классы берутся из
# скилла ripster-apple-wrapper: device-limit (lease 3062 / response type 6) и
# отказ логина (response type 4) лечатся ПО-РАЗНОМУ — первое продлением
# ожидания и своим identity, второе только временем, — и оба неотличимы снаружи
# от «нет прав в регионе», пока не заглянешь в контейнер.
_BLOCK_PATTERNS = (
    # «Your account is disabled. This Apple Account has been disabled for
    # security reasons» — 24.09.2026 три учётки владельца получили этот диалог и
    # числились «просто остановленными»: паттерна «account IS disabled» в списке
    # не было, был только «account HAS BEEN disabled» из rip-wrapper-3.
    ("account_disabled", ("account is disabled", "account has been disabled",
                          "disabled for security reasons", "apple account has been disabled")),
    ("device_limit", ("device limit", "concurrent playing devices",
                      "lease code 3062", "response type 6")),
    ("login_failed", ("login failed", "response type 4")),
    ("no_session", ("playback error",)),
)

#: Причины из `_BLOCK_PATTERNS`, которые значат «учётка мертва НАСОВСЕМ в рамках
#: этого симптома». `no_session` — нет: это состояние контейнера, а не аккаунта.
HARD_BLOCK_REASONS = ("account_disabled", "device_limit", "login_failed")


def container_block_reason(container: str, tail: int = 60) -> str:
    """ПОЧЕМУ контейнер не даёт ключ — по его же журналу. '' — не выяснили;
    выдумывать причину вместо этого нельзя (см. ripster-honest-diagnostics)."""
    try:
        r = subprocess.run(["docker", "logs", f"--tail={tail}", container],
                           capture_output=True, text=True, timeout=15,
                           creationflags=_CNW)
        # враппер пишет и в stdout, и в stderr — какой поток доедет до клиента,
        # заранее не угадать, поэтому читаем оба
        logs = ((r.stdout or "") + "\n" + (r.stderr or "")).lower()
    except Exception:
        return ""
    for reason, needles in _BLOCK_PATTERNS:
        if any(n in logs for n in needles):
            return reason
    return ""


def slot_health(container: str, port: int = 0) -> dict:
    """Три разных состояния, которые раньше сваливались в одно «слот не
    поднялся»: контейнер жив, порт открыт, УЧЁТКА ЗАКЕШИРОВАНА. Последние два
    расходятся ровно в том случае, который 22.09.2026 выдал «нет прав в
    регионе» на альбом, доступный в четырёх витринах: контейнер работал, порт
    10021 отвечал, а внутри — `response type 6`, то есть ключ дать было
    некому."""
    running = container_running(container)
    session = container_storefront(container) if running else ""
    return {"container": container, "running": running,
            "country": session or _memo_load().get(container, ""),
            "session": bool(session),
            "port_open": bool(running and port and _port_open(port)),
            "reason": "" if session else (container_block_reason(container)
                                          if running else "stopped")}


def _port_open(port: int, timeout: float = 1.2) -> bool:
    import socket
    s = socket.socket(); s.settimeout(timeout)
    try:
        s.connect(("127.0.0.1", int(port)))
        return True
    except Exception:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


def _bindings(container: str, field: str) -> dict:
    """Портовые привязки контейнера: HostConfig.PortBindings (как созданный)
    или NetworkSettings.Ports (факт). Формы одинаковые."""
    out = ""
    try:
        out = subprocess.run(
            ["docker", "inspect", "-f", "{{json ." + field + "}}", container],
            capture_output=True, text=True, timeout=10,
            creationflags=_CNW).stdout.strip()
        return json.loads(out) if out and out != "null" else {}
    except Exception:
        return {}


def _loopback_only(spec: dict) -> bool:
    """Каждая опубликованная строка смотрит на петлю? Пустая публикация — «нет»:
    молча принять пустоту означало бы довериться недоступному проверке состоянию.
    """
    if not spec:
        return False
    for rows in spec.values():
        for row in (rows or []):
            if (row or {}).get("HostIp") not in ("127.0.0.1", "::1"):
                return False
    return True


def container_exists(container: str) -> bool:
    """Контейнер СУЩЕСТВУЕТ (хотя бы остановлен). Отличает «нет такого» от
    «есть, но выключен» — `container_running` этих состояний не разделяет, а
    лечатся они противоположно: второе — стартом, первое — созданием."""
    out = ""
    try:
        r = subprocess.run(["docker", "inspect", "-f", "{{.Id}}", container],
                           capture_output=True, text=True, timeout=10,
                           creationflags=_CNW)
        out = (r.stdout or "").strip()
        return bool(out) and r.returncode == 0
    except Exception:
        return False


def slot_of_container(container: str) -> int:
    """`amd-wrapper` → 0, `rip-wrapper-N` → N, иначе -1."""
    if container == "amd-wrapper":
        return 0
    m = re.match(r"rip-wrapper-(\d+)$", container or "")
    return int(m.group(1)) if m else -1


def container_for_slot(slot: int) -> str:
    """Контейнер слота: 0 → `amd-wrapper`, дальше `rip-wrapper-N`. Одно место,
    потому что `_name` в пуле и обход сторожа обязаны называть слот одинаково —
    иначе сторож снимает чужой контейнер."""
    return "amd-wrapper" if int(slot) == 0 else f"rip-wrapper-{int(slot)}"


def _inspect(container: str, fmt: str) -> str:
    try:
        r = subprocess.run(["docker", "inspect", "-f", fmt, container],
                           capture_output=True, text=True, timeout=10,
                           creationflags=_CNW)
        return (r.stdout or "").strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def container_stop_info(container: str) -> dict:
    """Почему контейнер НЕ работает: штатный простой или симптом смерти.

    Различать обязано. Сборщик гасит простаивающие слоты через пять минут
    (см. `wrapper_pool.IDLE_COOLDOWN`), и «остановлен» — норма для 90% слотов в
    любой момент суток. Считать такие остановки неудачами — значит снять весь
    пул за одну ночь простоя. Мёртвый же слот выдаёт себя журналом: device
    limit, login failed, «account is disabled», или нечистым кодом выхода.

    Возвращает {"exists", "running", "exit_code", "block_reason", "idle"};
    `idle` — то есть «остановлен без симптомов», проверкой НЕ является.
    """
    state = _inspect(container, "{{.State.Status}}|{{.State.ExitCode}}")
    if not state:
        return {"exists": False, "running": False, "exit_code": None,
                "block_reason": "", "idle": False}
    status, _, code = state.partition("|")
    try:
        exit_code = int(code)
    except ValueError:
        exit_code = None
    running = status == "running"
    reason = container_block_reason(container) if not running else ""
    return {"exists": True, "running": running, "exit_code": exit_code,
            "block_reason": reason,
            "idle": (not running and not reason and exit_code in (0, None))}


def container_env_id(container: str) -> str:
    """Apple ID, под которым контейнер СОЗДАВАЛСЯ (`-L id:пароль` в env).

    Это единственный честный ответ на вопрос «чей вообще этот контейнер»: номер
    слота — порядок в списке, а список правят, и после удаления середины
    `rip-wrapper-2` остаётся сессией аккаунта, которого в списке уже нет. Имя
    здесь ни при чём — по имени судят о владении только те, кто ни разу не
    доставал env."""
    raw = _inspect(container, "{{json .Config.Env}}")
    try:
        env = json.loads(raw) if raw else []
    except Exception:
        return ""
    for item in (env or []):
        s = str(item)
        if not s.startswith("args="):
            continue
        m = re.search(r"-L\s+(\S+?):", s)
        if m:
            return m.group(1).strip()
    return ""


# ── Карта «аккаунт ↔ слот/контейнер» ────────────────────────────────────────
# До 24.09.2026 её не было вовсе: настройки показывали шесть учётки из конфига,
# `all_slots()` — три контейнера, и никто не умел сказать, что к чему относится.
# Из этого вырос основной упрёк владельца: мёртвая учётка `device_limit_2026-08-01`
# жила в списке с августа и ни разу не была снята, потому что сторож смотрел на
# контейнеры, а контейнера у неё давно не было.
#
# Ключ — sha256-хеш опознания учётки (`retired_credentials._digest`), НЕ сам
# Apple ID: файл лежит в dist/, а dist уезжает в бэкапы и кэши.

def _map_path():
    from pathlib import Path
    import os
    base = Path(os.environ.get("RIPSTER_BASE_DIR") or Path(__file__).resolve().parent.parent)
    p = base / "dist" / "docker" / "apple_account_map.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def account_map() -> dict:
    """{"accounts": {digest: {slot, container, kind, label, at}}, "containers": {имя: digest}}."""
    try:
        d = json.loads(_map_path().read_text(encoding="utf-8"))
    except Exception:
        return {"accounts": {}, "containers": {}}
    if not isinstance(d, dict):
        return {"accounts": {}, "containers": {}}
    d.setdefault("accounts", {})
    d.setdefault("containers", {})
    return d


def account_map_put(digest: str, *, slot: int, container: str, kind: str,
                    label: str) -> None:
    """Записать, где живёт учётка. Вызывается КАЖДЫМ проходом сторожа — карта
    обязана переживать удаление контейнеров и правку списка, но не переживать
    перестановку учёток местами."""
    if not digest:
        return
    d = account_map()
    d["accounts"][digest] = {"slot": int(slot), "container": container,
                             "kind": kind, "label": label,
                             "at": int(time.time())}
    if container:
        d["containers"][container] = digest
    try:
        p = _map_path()
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(p)
    except Exception:
        pass


def account_map_forget(*, digest: str = "", container: str = "") -> None:
    """Вычеркнуть учётку (снята) или контейнер (удалён осиротевшим)."""
    d = account_map()
    changed = False
    if digest:
        if d["accounts"].pop(digest, None) is not None:
            changed = True
        for name in [c for c, dg in d["containers"].items() if dg == digest]:
            del d["containers"][name]
            changed = True
    if container and d["containers"].pop(container, None) is not None:
        changed = True
    if not changed:
        return
    try:
        _map_path().write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass


def ensure_slot_up(container: str, port: int = 0, timeout: float = 60.0,
                   config: dict | None = None) -> bool:
    """Поднять слот и дождаться, пока он реально начнёт расшифровывать.

    Мало запустить контейнер: порт расшифровки открывается не сразу, а
    загрузчик, ткнувшийся раньше времени, получает «connection refused» — и это
    выглядит как «нет прав в регионе», хотя права ни при чём. Поэтому ждём
    именно порт, а не факт запуска.

    22.09.2026: `docker start` у остановленного контейнера обнуляет HostIp
    (Docker Desktop 4.83) — тот же механизм, из-за которого пул пересоздаёт
    слоты вместо оживления, а amd.py убрал `--restart`. Этот путь — ТРЕТИЙ
    оживлятор слотов (после пула и amd) и проверки не имел: оживлённый здесь
    слот публиковал 30020 с токенами Apple на 0.0.0.0. Поэтому поднимаем только
    заведомо-петлевые привязки, проверяем факт ПОСЛЕ старта, и при сорванной
    привязке снимаем контейнер, а не оставляем наружу.

    🔴 Снятие — не конец истории. 22.09 контейнеры `rip-wrapper-1/2` исчезли
    именно так: `rm -f` сработал, а пересоздавать никто не вызвался, и страна
    учёток GB выпала из перебора вместе с контейнером. Поэтому теперь слот,
    которого НЕТ (или который мы только что сняли), пересоздаётся через
    `wrapper_pool.ensure_slot` — единственный рецепт создания с петлевой
    привязкой. Без `config` пересоздать нечем (нет учёток), и ответ честно
    False.
    """
    import socket, time as _t
    slot = slot_of_container(container)

    def _recreate() -> bool:
        if config is None or slot < 0:
            return False
        try:
            from ripster import wrapper_pool
            return bool(wrapper_pool.ensure_slot(config, slot))
        except Exception as e:
            print(f"[apple] слот {container} не пересоздан: {e}", flush=True)
            return False

    if not container_running(container):
        # Без `config` пересоздавать нечем (нет ни списка учёток, ни пула), и
        # тогда путь ровно прежний: оживление существующего контейнера.
        exists = container_exists(container) if config is not None else True
        if not exists:
            if not _recreate():
                return False
        elif not _loopback_only(_bindings(container, "HostConfig.PortBindings")):
            # Поднимать нельзя, но и молча бросать нельзя: у слота есть учётка
            # и страна, просто рецепт создания — у пула.
            if not _recreate():
                return False
        else:
            try:
                subprocess.run(["docker", "start", container], capture_output=True,
                               text=True, timeout=30, creationflags=_CNW)
            except Exception:
                return False
    if not _loopback_only(_bindings(container, "NetworkSettings.Ports")):
        subprocess.run(["docker", "rm", "-f", container], capture_output=True,
                       timeout=30, creationflags=_CNW)
        # привязка сорвалась ПОСЛЕ старта — пересоздаём сразу, иначе слот
        # исчезнет до следующей задачи, а задача сегодня уже никуда не пойдёт
        return _recreate()
    if not port:
        return container_running(container) and wait_session(container)
    deadline = _t.time() + timeout
    while _t.time() < deadline:
        s = socket.socket(); s.settimeout(1.5)
        try:
            s.connect(("127.0.0.1", port))
            # Порт открыт — ещё НЕ значит, что есть кому дать ключ. Go внутри
            # контейнера поднимает порт раньше, чем успеет сесть: при
            # `response type 6` (device limit) он держит TCP открытым сутками, и
            # каждый трек через такой порт — Invalid CKC. 22.09.2026 на этом
            # «живом» порту альбом, доступный в четырёх витринах, умер с
            # вердиктом «нет прав в регионе».
            return wait_session(container)
        except Exception:
            _t.sleep(2)
        finally:
            try: s.close()
            except Exception: pass
    return False


def wait_session(container: str, timeout: float = 20.0) -> bool:
    """Дождаться, что слот НАЗЫВАЕТ свою учётку (порт 30020 внутри отвечает).
    Только что созданный контейнер логинится несколько секунд, поэтому ждём, но
    недолго и без молотого `docker exec`: пробы идут с паузой, а удача
    кэшируется в `container_storefront`."""
    deadline = time.time() + timeout
    while True:
        if container_storefront(container):
            return True
        if time.time() >= deadline:
            return False
        time.sleep(2.5)


def slot_port(slot: int) -> int:
    """Порт расшифровки слота. Слот 0 — общий 10020, дальше 10020+N."""
    return 10020 + int(slot)


def any_session_alive(config: dict | None = None, max_slots: int = 8) -> bool:
    """Есть ли ХОТЬ ОДНА своя учётка, способная дать ключ.

    Спрашивать об этом надо ВСЕ слоты, а не только слот 0: `local_wrapper_session_alive()`
    смотрит на 30020 основного контейнера, тогда как расшифровку в этот момент делал
    другой слот. 22.09.2026 эта пара фактов и родила враньё в отчёте — «сессия ЖИВА,
    значит у контента нет прав в регионе» — на задаче, где жива была основная
    витрина, а мёртв именно тот слот, что отдавал ключ.
    """
    return any(s.get("session") for s in all_slots(max_slots, True, config))


# ── Предпочтения по слотам: чей ход раньше и кого не трогать ────────────────
# У Apple слот — это НЕ «следующая свободная учётка», как в пулах Deezer/Qobuz.
# Слот привязан к стране, а страна решает, есть ли релиз вообще. Поэтому
# приоритет здесь работает ВНУТРИ страны, а не вместо неё: сначала выбирается
# витрина, в которой релиз существует, и только среди своих аккаунтов в этой
# витрине приоритет решает, кого спросить первым.
#
# Сделать наоборот — поставить приоритет выше страны — значит сломать
# маршрутизацию: аккаунт с приоритетом 0 в US будет вечно получать японские
# релизы, которых в US нет, и упираться в Invalid CKC. Разбор 08.08 (Apparat)
# был ровно про это, только с другой стороны.
#
# Индекс записи И ЕСТЬ номер слота: primary = 0 (контейнер `amd-wrapper`),
# дальше `wrapper-accounts[i-1]` → `rip-wrapper-i`. Переставлять записи нельзя —
# уведёт чужой контейнер под чужую учётку.

def slot_prefs(config: dict | None) -> dict:
    """slot -> {'priority': float, 'enabled': bool, 'label': str}.

    Ключей может не быть вовсе — тогда приоритет равен номеру слота, то есть
    поведение в точности прежнее. Это не вежливость: у владельца шесть учёток,
    и молчаливая перестановка после обновления была бы неотличима от поломки.
    """
    out: dict[int, dict] = {}
    if not config:
        return out
    def _f(v, dflt):
        try:
            return float(dflt if v is None else v)
        except (TypeError, ValueError):
            return float(dflt)
    aid = config.get("wrapper-apple-id")
    if aid:
        out[0] = {"priority": _f(config.get("wrapper-primary-priority"), 0),
                  "enabled":  config.get("wrapper-primary-enabled", True) is not False,
                  "label":    str(config.get("wrapper-apple-id") or "primary")}
    for i, extra in enumerate(config.get("wrapper-accounts") or [], start=1):
        if not isinstance(extra, dict):
            continue
        out[i] = {"priority": _f(extra.get("priority"), i),
                  "enabled":  extra.get("enabled", True) is not False,
                  "label":    str(extra.get("label") or extra.get("id") or f"slot{i}")}
    return out


def all_slots(max_slots: int = 8, include_stopped: bool = False,
              config: dict | None = None) -> list[dict]:
    """Все живые Apple-сессии: контейнер, слот, страна.

    Слот 0 — основной `amd-wrapper`, дальше `rip-wrapper-N`. Молчащие слоты в
    список не попадают: сессия, не отвечающая на 30020, качать всё равно не
    сможет.
    """
    memo = _memo_load()
    out: list[dict] = []
    for i in range(max_slots):
        name = "amd-wrapper" if i == 0 else f"rip-wrapper-{i}"
        running = container_running(name)
        cc = ""
        session = False
        if running:
            cc = container_storefront(name)
            session = bool(cc)
            if cc:
                _memo_save(name, cc)
            else:
                # Проба молчит СЕЙЧАС (контейнер только что создан и ещё не
                # прогрет, docker exec не успел), а страну мы уже знали раньше.
                # Раньше такой слот выпадал из `all_slots` целиком — то есть
                # свежеподнятый аккаунт был невидим для перебора ровно до
                # следующего прогрева. Это не «нет страны», это «не спросили».
                cc = memo.get(name, "")
        elif include_stopped:
            # Выключенный слот спросить нельзя, но страну мы уже знали раньше.
            # Без этого остановленная сессия выпадала из выбора совсем, и задача
            # уходила к чужому публичному wrapper'у при живом своём аккаунте.
            cc = memo.get(name, "")
        if cc:
            out.append({"slot": i, "container": name, "country": cc,
                        "running": running, "port": slot_port(i),
                        # `session` — учётка реально закеширована И может дать
                        # ключ. `running` без `session` (device limit) и есть
                        # тот самый слот, который 22.09 выдавал себя за живой:
                        # контейнер запущен, порт открыт, ключа нет.
                        "session": session,
                        "reason": ("" if session else
                                   (container_block_reason(name) if running
                                    else "stopped"))})
    # Предпочтения навешиваются ПОСЛЕ обхода: обход про то, что физически есть,
    # конфиг — про то, чего мы от этого хотим. Смешивать нельзя, иначе слот,
    # выключенный в настройках, исчезнет и из диагностики тоже.
    prefs = slot_prefs(config)
    for sl in out:
        pr = prefs.get(sl["slot"]) or {}
        sl["priority"] = pr.get("priority", float(sl["slot"]))
        sl["enabled"]  = pr.get("enabled", True)
        if pr.get("label"):
            sl["label"] = pr["label"]
    return out


def slot_for_country(cc: str, config: dict | None = None) -> dict | None:
    """Живая сессия в нужной стране, если такая есть."""
    cc = (cc or "").lower()
    got = [s for s in all_slots(config=config)
           if s["country"] == cc and s.get("enabled", True)]
    got.sort(key=lambda s: (float(s.get("priority", s["slot"])), s["slot"]))
    return got[0] if got else None


def pick_slot_for(countries, exclude=(), config: dict | None = None) -> dict | None:
    """Первая наша сессия, чья страна есть среди *countries* и не в *exclude*.

    Ровно то, чего не хватало маршрутизатору: релиз доступен в списке витрин —
    спрашиваем, нет ли у нас аккаунта в одной из них, и только если нет, идём к
    чужому публичному wrapper'у.

    `exclude` обязателен по существу, а не для удобства. «Релиз числится в
    витрине» и «наш аккаунт в этой витрине получит ключ» — разные утверждения:
    09.08.2026 каталог показывал альбом в CA, а канадский аккаунт отдал только
    3 трека из 11 и упёрся в Invalid CKC. Уже отказавшую страну надо исключать,
    иначе выбор будет бесконечно возвращать её же.
    """
    want = [c.lower() for c in (countries or [])]
    skip = {c.lower() for c in (exclude or ())}    # include_stopped: выключенный слот — это НАША живая учётка, просто
    # погашенная сборщиком простоя. Пропускать её значит уходить к чужому
    # публичному wrapper'у при свободном своём аккаунте (09.08.2026).
    slots = [s for s in all_slots(include_stopped=True, config=config)
             if s["country"] not in skip]
    # Выключенную владельцем учётку не берём вовсе. Если выключены ВСЕ — берём
    # как есть: пустой ответ здесь неотличим от «своих аккаунтов нет», и задача
    # молча уедет к чужому публичному wrapper'у. Про такое надо сказать вслух.
    on = [s for s in slots if s.get("enabled", True)]
    if slots and not on:
        print("[apple] все слоты выключены в настройках — беру как есть", flush=True)
        on = slots
    # Внутри одной витрины — по приоритету, затем по номеру слота. Порядок
    # витрин остаётся главным: страна решает, есть ли релиз, приоритет — лишь
    # кого из СВОИХ в этой стране спросить первым.
    on.sort(key=lambda s: (float(s.get("priority", s["slot"])), s["slot"]))
    for c in want:                                  # порядок витрин важнее
        for s in on:
            if s["country"] == c:
                return s
    return None


# ── Лестница своих аккаунтов: порядок перебора и память об успехах ────────────
#
# `pick_slot_for` отвечает на вопрос «давай ОДИН слот под список витрин». Для
# отказа по CKC этого мало: надо перебрать ВСЕ свои учётки, потому что «нет
# прав в регионе» — это утверждение про ОДНУ сессию, а не про аккаунты владельца.
# 22.09.2026 задача на альбом из `/ru/` умерла с текстом «нет прав», хотя рядом
# стояли GB-учётки: перебор не начался, потому что каталог по названию не
# подтвердил релиз (iTunes search не находит DJ-миксы), а old-логика требовала
# подтверждения. Пустой ответ каталога — это «не выяснили», а не «нигде нет»
# (см. докстринг `errors.apple_album_by_storefront`), и решать по нему нельзя.
#
# Поэтому порядок такой:
#   1) страна, где ЭТОТ релиз уже успешно качнулся раньше (память побед);
#   2) страны, где каталог релиз подтвердил;
#   3) остальные свои страны — каталог молчит ≠ прав нет;
#   внутри группы — по памяти побед, затем приоритет владельца, затем номер слота.
# Отказавшие страны и слоты исключаются вызывающим (уже попробовано).

def _wins_path():
    from pathlib import Path
    import os
    base = Path(os.environ.get("RIPSTER_BASE_DIR") or Path(__file__).resolve().parent.parent)
    p = base / "dist" / "docker" / "apple_slot_wins.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _wins_load() -> dict:
    try:
        d = json.loads(_wins_path().read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def release_id(url_or_id: str) -> str:
    """Числовой Apple-номер релиза из ссылки (для памяти побед).

    `?i=` проверяется ПЕРВЫМ: в ссылке вида `/us/album/x/1?i=2` номер альбома —
    это контекст, а качается трек 2. Память, keyed не на тот номер, возвращала
    бы удачную учётку не для того релиза.
    """
    m = (re.search(r"[?&]i=(\d+)", url_or_id or "")
         or re.search(r"/(?:album|song|playlist|music-video)/[^/]+/(\d+)", url_or_id or "")
         or re.search(r"/(\d+)(?:\?|$)", url_or_id or ""))
    return m.group(1) if m else ""


def remember_win(country: str, url_or_id: str = "", slot: int = -1) -> bool:
    """Записать, КАКОЙ своей учёткой релиз в итоге качнулся.

    Память двухуровневая: по номеру релиза (тот же альбом повторно идёт сразу
    туда, где сработало) и по стране (следующий ПОХОЖИЙ релиз начинает с
    недавно удачной витрины, а не с той же канадской, что отказывает третьей
    неделю). Ничего секретного не пишем: только страна, номер релиза и номер
    слота — ни учётных данных, ни токенов.
    """
    cc = (country or "").lower()
    if not cc:
        return False
    d = _wins_load()
    now = time.time()
    rel = d.setdefault("release", {})
    rid = release_id(url_or_id)
    if rid:
        rel[rid] = {"country": cc, "slot": slot, "ts": now}
    ctr = d.setdefault("country", {})
    row = ctr.get(cc) or {"wins": 0, "ts": 0}
    ctr[cc] = {"wins": int(row.get("wins") or 0) + 1, "ts": now}
    try:
        _wins_path().write_text(json.dumps(d, ensure_ascii=False, indent=1),
                                encoding="utf-8")
        return True
    except Exception:
        return False


def winning_country(url_or_id: str = "") -> str:
    """Страна, которой этот релиз уже давался (пусто — не помним)."""
    rid = release_id(url_or_id)
    if not rid:
        return ""
    return str(((_wins_load().get("release") or {}).get(rid) or {}).get("country") or "")


def manageable_slots(config: dict | None = None) -> list[dict]:
    """Свои учётки, которые МОЖНО поднять: контейнер жив либо пул умеет его
    создать. Молчание здесь опаснее ошибки — 22.09 перебор молча не увидел ни
    одной GB-учётки, потому что контейнеров не существовало."""
    slots = all_slots(include_stopped=True, config=config)
    try:
        from ripster import wrapper_pool
    except Exception:
        return [s for s in slots if s.get("running")]
    out = []
    for s in slots:
        if s.get("running") or wrapper_pool.managed_slot(config or {}, s["slot"]):
            out.append(s)
    return out


def ladder_order(config: dict | None = None, url_or_id: str = "",
                 exclude=(), avail: dict | None = None) -> list[dict]:
    """Наши Apple-сессии в том порядке, в котором их перебирать после
    «Invalid CKC при живой сессии».

    exclude — страны ИЛИ номера слотов, которые уже отказали этой задаче.
    avail — {страна: (id, имя)} из каталога; пустой dict означает «не выяснено»,
    и он НИКОГО не отсеивает (иначе именно так и терялись GB-учётки).
    """
    skip_cc = {str(x).lower() for x in exclude or ()
               if isinstance(x, str) and not str(x).isdigit()}
    skip_slot = {int(x) for x in exclude or ()
                 if isinstance(x, int) or (isinstance(x, str) and x.isdigit())}
    known = {str(c).lower() for c in (avail or {})}
    star = winning_country(url_or_id)
    wins = _wins_load().get("country") or {}
    out: list[dict] = []
    for s in manageable_slots(config):
        cc = (s.get("country") or "").lower()
        if not cc or cc in skip_cc or s["slot"] in skip_slot:
            continue
        if s.get("enabled") is False:      # выключенную владельцем учётку не зовём
            continue
        tier = 0 if (star and cc == star) else (1 if cc in known else 2)
        out.append({**s, "tier": tier,
                    "_win": float((wins.get(cc) or {}).get("ts") or 0),
                    "_prio": float(s.get("priority", s["slot"]))})
    out.sort(key=lambda s: (s["tier"], -s["_win"], s["_prio"], s["slot"]))
    return out
