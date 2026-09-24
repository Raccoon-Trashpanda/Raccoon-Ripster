"""Пейсинг запросов к сервисам: счётчики, мягкие суточные потолки и штраф.

Владелец 24.09.2026 (выжимка чата @apple_music_alac): учётки летят в бан пачками
— Apple отвечает 429 на тысячи скачиваний в сутки, а Qobuz режет 403 тех, кто
часто дергает ОФИЦИАЛЬНЫЙ API плейлистов (`api.json/0.2/playlist/get`; именно
его звали веером при разборе чужих ссылок). Молотить после отказа с той же
частотой — значит конвертировать «замедлились» в «заблокированы».

Здесь три разнородные вещи, и все три обязаны быть честными:

  * СЧЁТЧИКИ — сколько запросов сделали за час и за сутки. Хранятся на диске:
    перезапуск приложения не должен обнулять сутки, иначе потолок обещает
    одно, а считает другое.
  * ПОТОЛКИ — мягкие. Мягкий значит «задержись», а не «умри»: часовая переполнена
    — подождать до переката часа; суточная — запрос НЕ отправляется, вызывающий
    получает пустой список там, где и так умеет жить без него.
  * ШТРАФ — 429/403 удваивают паузу (30 с, 60, 120 … до 6 часов) и владельцу
    сообщается ОДИН раз на ступень, а не на каждую строку журнала: молчание
    здесь хуже, чем крик на каждый ответ сервера.

Чего этот модуль НЕ измеряет, и важно это говорить: расшифровочные запросы
к ключам идут внутри враппер-контейнеров (Go/AMDL), наружу из них Python не
видит ни одного вызова. Поэтому под «apple» здесь считаются OUR запросы к
amp-api/catalog с токеном конкретной учётки — поиск витрины, раскрытие
плейлиста, метаданные альбома. Потолок ключей внутри контейнера — это тариф
учётки (`ripster.apple_plan`), а не счётчик здесь.
"""

import asyncio
import json
import os
import time
from pathlib import Path

#: Мягкие потолки по умолчанию. `hour` — про частоту (то, за что режут 429),
#: `day` — про суточный объём (то, за что банят). Числа взяты не с потолка:
#: штатный день владельца — это сотни раскрытий ссылок и десятки проверок
#: витрин, до тысячи в час далеко, а ниже — начинёт тормозить нормальную
#: работу. Правятся в настройках (`*-per-hour` / `*-per-day` ниже).
DEFAULTS = {
    "apple":          {"hour": 1500, "day": 15000,
                       "cfg_hour": "apple-requests-per-hour",
                       "cfg_day":  "apple-requests-per-day"},
    "qobuz_playlist": {"hour": 120,  "day": 1200,
                       "cfg_hour": "qobuz-playlist-per-hour",
                       "cfg_day":  "qobuz-playlist-per-day"},
}

#: Ступени штрафа: 30 с, 60, 120 … и не дольше полудня. Первая ступень —
#: «замедлились», пятая — «сервер нас помнит», и там уже надо остановиться.
PENALTY_BASE_S = 30.0
PENALTY_MAX_S = 6 * 3600.0

#: Ответы, после которых просить ещё раз в том же темпе нельзя.
#: 401 — это не про частоту (протух токен), 403 — про нас: Qobuz им и режет.
PENALTY_STATUSES = (403, 429)

HOUR = 3600.0
DAY = 24 * HOUR


def _path() -> Path:
    base = Path(os.environ.get("RIPSTER_BASE_DIR")
                or Path(__file__).resolve().parent.parent)
    p = base / "dist" / "pacing.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _load() -> dict:
    try:
        d = json.loads(_path().read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:                                       # noqa: BLE001
        return {}


def _save(d: dict) -> None:
    try:
        p = _path()
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(p)
    except Exception as e:                                  # noqa: BLE001
        print(f"[pacing] счётчики не записаны: {e}", flush=True)


def caps(service: str, config: dict | None = None) -> dict:
    """Потолки сервиса: конфиг владельца поверх дефолтов. Ноль или пустое
    значение = потолок снят (владелец имеет право сказать «не мешай»).

    Неизвестный сервис — это «потолка нет», а не «упасть»: `caps` зовётся из
    `wait_seconds` на каждом запросе, и падение нового сервиса обрубило бы
    дорогу, а не только лимит."""
    d = DEFAULTS.get(service) or {"hour": 0, "day": 0, "cfg_hour": "", "cfg_day": ""}
    cfg = config or {}
    out = {}
    for key, cfgkey in (("hour", "cfg_hour"), ("day", "cfg_day")):
        try:
            v = int((cfg.get(d[cfgkey], d[key]) if d[cfgkey] else d[key]) or 0)
        except (TypeError, ValueError):
            v = int(d[key])
        out[key] = max(0, v)
    return out


def _bucket(now: float, seconds: float) -> int:
    return int(now // seconds)


def _entry(service: str, ident: str) -> dict:
    return _load().get(f"{service}|{ident}") or {}


def counts(service: str, ident: str = "", now: float | None = None) -> dict:
    """Сколько запросов сделано в текущем часе и сегодняшних сутках.

    Бакет сверяется по метке, а не по «сколько времени прошло»: иначе после
    полудня вчерашние сутки выглядели бы как «сегодня 5000 запросов»."""
    now = time.time() if now is None else now
    e = _entry(service, ident)
    hour = int(e.get("nh", 0)) if int(e.get("h", -1)) == _bucket(now, HOUR) else 0
    day = int(e.get("nd", 0)) if int(e.get("d", -1)) == _bucket(now, DAY) else 0
    return {"hour": hour, "day": day,
            "penalty_until": float(e.get("until") or 0),
            "strikes": int(e.get("strikes") or 0),
            "refused": int(e.get("refused") or 0)}


def note(service: str, ident: str = "", now: float | None = None) -> None:
    """Записать ОТПРАВЛЕННЫЙ запрос (не «попытаемся, если пустит потолок»)."""
    now = time.time() if now is None else now
    k = f"{service}|{ident}"
    d = _load()
    e = dict(d.get(k) or {})
    hb, db = _bucket(now, HOUR), _bucket(now, DAY)
    e["nh"] = (int(e.get("nh", 0)) + 1) if int(e.get("h", -1)) == hb else 1
    e["h"] = hb
    e["nd"] = (int(e.get("nd", 0)) + 1) if int(e.get("d", -1)) == db else 1
    e["d"] = db
    e["at"] = now
    d[k] = e
    _save(d)


def wait_seconds(service: str, ident: str = "", config: dict | None = None,
                 now: float | None = None) -> float:
    """Сколько подождать ПЕРЕД запросом. 0 — просить можно.

    Сперва штраф (429/403 удваивают паузу), потом потолок: часовая переполнена
    — до переката часа, но не дольше штрафа-потолка, иначе «подождать»
    превращается в «задача зависла».
    """
    now = time.time() if now is None else now
    e = _entry(service, ident)
    until = float(e.get("until") or 0)
    wait = max(0.0, until - now)
    cap = caps(service, config)
    c = counts(service, ident, now=now)
    if cap["hour"] and c["hour"] >= cap["hour"]:
        left = (_bucket(now, HOUR) + 1) * HOUR - now
        wait = max(wait, min(left, HOUR))
    if cap["day"] and c["day"] >= cap["day"]:
        # Суточный объём выбран: ждать сутки бессмысленно, это `blocked()`.
        wait = max(wait, 1.0)
    return wait


def blocked(service: str, ident: str = "", config: dict | None = None,
            now: float | None = None) -> bool:
    """Суточный потолок выбран — запрос отправлять нельзя.

    Возвращать True вместо «поспать до завтра» можно только там, где вызывающий
    умеет жить без ответа; все три вызывающих умеют (пустой список = «не
    раскрылось», а не «сломалось»)."""
    now = time.time() if now is None else now
    cap = caps(service, config)
    if not cap["day"]:
        return False
    c = counts(service, ident, now=now)
    if c["day"] < cap["day"]:
        return False
    k = f"{service}|{ident}"
    d = _load()
    e = dict(d.get(k) or {})
    e["refused"] = int(e.get("refused", 0)) + 1
    d[k] = e
    _save(d)
    if int(e["refused"]) == 1 or int(e["refused"]) % 50 == 0:
        print(f"[pacing] {service}/{_mask(ident)}: суточный потолок "
              f"{cap['day']} выбран — дальше не просим (отказов {e['refused']})",
              flush=True)
    return True


def penalty(service: str, ident: str = "", status: int = 0,
            config: dict | None = None, now: float | None = None) -> float:
    """429/403: удвоить паузу и сказать владельцу ОДИН раз на ступень.

    Без штрафа «попросили ещё раз» и «попросили тысячу раз» выглядят для
    сервиса одинаково, а для нас вторым — бан аккаунта.
    """
    if status not in PENALTY_STATUSES:
        return 0.0
    now = time.time() if now is None else now
    k = f"{service}|{ident}"
    d = _load()
    e = dict(d.get(k) or {})
    strikes = int(e.get("strikes") or 0) + 1
    pause = min(PENALTY_BASE_S * (2 ** (strikes - 1)), PENALTY_MAX_S)
    e.update({"strikes": strikes, "until": now + pause, "last_status": int(status)})
    d[k] = e
    _save(d)
    print(f"[pacing] {_mask(ident)} · {service}: HTTP {status} → пауза "
          f"{_fmt(pause)} (ступень {strikes})", flush=True)
    return pause


def pardon(service: str, ident: str = "", now: float | None = None) -> None:
    """Успешный ответ снимает накопленный штраф: без него одна ночная авария
    Apple держала бы учётку на минимальной скорости до конца суток."""
    now = time.time() if now is None else now
    k = f"{service}|{ident}"
    d = _load()
    e = d.get(k)
    if e and (e.get("strikes") or e.get("until")):
        e["strikes"] = 0
        e["until"] = 0
        d[k] = e
        _save(d)


#: Сколько вызывающий имеет право спать в `allow`. Потолок обязан быть:
#: задача, заснувшая на перекат суточного бакета, выглядит для владельца
#: зависшей, а зависшая задача потом повторяет запрос — ровно то, против чего
#: этот модуль и заведён.
MAX_WAIT_S = 300.0

#: Ровный шаг между запросами одного рода: веер из 300 ссылок за минуту — это
#: и без всякого потолка то, за что режут.
MIN_GAP_S = 0.25

#: Но спим мы на шаге только в БУРЕ. Одиночный запрос пользователя («открой
#: эту ссылку») притормаживать нечем: 429 ловят за сотни запросов подряд, а не
#: за один. Порог — сколько запросов за час уже должно случиться, прежде чем
#: «держать интервал» станет полезным; ниже этого шага мы только плодим задержки
#: в UI и в тестах.
BURST_MIN_PER_HOUR = 20


async def allow(service: str, ident: str = "", config: dict | None = None,
                now: float | None = None) -> bool:
    """Можно ли просить (True — можно, возможно, после паузы).

    False значит «суточный потолок выбран»: вызывающий обязан вернуться так,
    как он возвращается на пустой ответ, — все три вызывающих так умеют.
    Ждать дольше `MAX_WAIT_S` смысла нет: задача выглядит зависшей."""
    now = time.time() if now is None else now
    wait = wait_seconds(service, ident, config, now=now)
    if wait > MAX_WAIT_S:
        # Не «подождём и всё равно попросим», а честный отказ: смысл ожидания
        # в том, чтобы не молотить, а не в том, чтобы замедлить отказ.
        return False
    if wait:
        await asyncio.sleep(wait)
    if blocked(service, ident, config, now=now):
        return False
    if counts(service, ident, now=now)["hour"] >= BURST_MIN_PER_HOUR:
        await asyncio.sleep(MIN_GAP_S)
    return True


def outcome(service: str, ident: str = "", status: int = 0) -> float:
    """Один состоявшийся запрос: счётчик, а по коду ответа — штраф или прощение."""
    note(service, ident)
    if status in PENALTY_STATUSES:
        return penalty(service, ident, status)
    if 200 <= status < 400:
        pardon(service, ident)
    return 0.0


def snapshot(config: dict | None = None, now: float | None = None) -> list[dict]:
    """Счётчики для отчёта владельцу: сервис, учётка, час/сутки, штраф, отказы."""

    now = time.time() if now is None else now
    out = []
    for key in _load():
        service, _, ident = key.partition("|")
        if service not in DEFAULTS:
            continue
        c = counts(service, ident, now=now)
        cap = caps(service, config)
        out.append({"service": service, "account": _mask(ident),
                    "hour": c["hour"], "hour_cap": cap["hour"],
                    "day": c["day"], "day_cap": cap["day"],
                    "strikes": c["strikes"], "refused": c["refused"],
                    "penalty_left": max(0.0, c["penalty_until"] - now)})
    out.sort(key=lambda r: (-r["day"], r["service"], r["account"]))
    return out


def ident_for(secret: str) -> str:
    """Несекретное имя учётки для счётчика: короткий хеш вместо токена.

    Счётчики лежат на диске в `dist/` и показываются владельцу в отчёте;
    media-user-token туда попасть не мог ни при каких обстоятельствах."""
    import hashlib
    s = str(secret or "").strip()
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:12] if s else ""


def _mask(ident: str) -> str:
    """Учётка в журнале и отчёте — хвост, а не адрес почты."""
    s = str(ident or "")
    if not s:
        return "без учётки"
    return f"...{s[-8:]}" if len(s) > 8 else s


def _fmt(seconds: float) -> str:
    s = int(seconds)
    if s < 90:
        return f"{s} с"
    if s < 5400:
        return f"{round(s / 60)} мин"
    return f"{round(s / 3600, 1)} ч"
