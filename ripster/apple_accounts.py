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


def ensure_slot_up(container: str, port: int = 0, timeout: float = 60.0) -> bool:
    """Поднять слот и дождаться, пока он реально начнёт расшифровывать.

    Мало запустить контейнер: порт расшифровки открывается не сразу, а
    загрузчик, ткнувшийся раньше времени, получает «connection refused» — и это
    выглядит как «нет прав в регионе», хотя права ни при чём. Поэтому ждём
    именно порт, а не факт запуска.
    """
    import socket, time as _t
    if not container_running(container):
        try:
            subprocess.run(["docker", "start", container], capture_output=True,
                           text=True, timeout=30, creationflags=_CNW)
        except Exception:
            return False
    if not port:
        return container_running(container)
    deadline = _t.time() + timeout
    while _t.time() < deadline:
        s = socket.socket(); s.settimeout(1.5)
        try:
            s.connect(("127.0.0.1", port))
            return True
        except Exception:
            _t.sleep(2)
        finally:
            try: s.close()
            except Exception: pass
    return False


def slot_port(slot: int) -> int:
    """Порт расшифровки слота. Слот 0 — общий 10020, дальше 10020+N."""
    return 10020 + int(slot)


def all_slots(max_slots: int = 8, include_stopped: bool = False) -> list[dict]:
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
        if running:
            cc = container_storefront(name)
            if cc:
                _memo_save(name, cc)
        elif include_stopped:
            # Выключенный слот спросить нельзя, но страну мы уже знали раньше.
            # Без этого остановленная сессия выпадала из выбора совсем, и задача
            # уходила к чужому публичному wrapper'у при живом своём аккаунте.
            cc = memo.get(name, "")
        if cc:
            out.append({"slot": i, "container": name, "country": cc,
                        "running": running, "port": slot_port(i)})
    return out


def slot_for_country(cc: str) -> dict | None:
    """Живая сессия в нужной стране, если такая есть."""
    cc = (cc or "").lower()
    return next((s for s in all_slots() if s["country"] == cc), None)


def pick_slot_for(countries, exclude=()) -> dict | None:
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
    skip = {c.lower() for c in (exclude or ())}
    # include_stopped: выключенный слот — это НАША живая учётка, просто
    # погашенная сборщиком простоя. Пропускать её значит уходить к чужому
    # публичному wrapper'у при свободном своём аккаунте (09.08.2026).
    slots = [s for s in all_slots(include_stopped=True) if s["country"] not in skip]
    for c in want:                                  # порядок витрин важнее
        for s in slots:
            if s["country"] == c:
                return s
    return None
