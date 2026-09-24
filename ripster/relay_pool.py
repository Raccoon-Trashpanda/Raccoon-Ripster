# -*- coding: utf-8 -*-
"""Пул key-серверов Wrapper Lite, который обслуживает своё реле.

Реле (``ripster/routes/relay.py``) само ничего у Apple не просит: оно берёт
ключ у враппера. Этот модуль — единственная точка, где реле решает, У КОГО.

Инстанс = один контейнер wrapper-lite на одну учётку. Это не формальность:
device-info враппер выводит из хэша логина (`lite_main.cpp`), а сессию держит в
СВОЁМ base-dir, поэтому два аккаунта в одном контейнере — это один и тот же
device-info на двоих, то есть ровно то, за что Apple блокирует пачками
(разбор в docs/WRAPPER_LITE_AUDIT_2026-09-24.md §2). Порт наружу у контейнера —
только 127.0.0.1; публичный адрес существует ровно у реле и ни у чего больше.

Чего здесь сознательно нет:

* никаких чужих учётных данных и никакой страницы «залогинься и получишь
  квоту», как у wm.wol.moe. Пул пополняется ТОЛЬКО хозяйским конфигом;
* молчаливого «попробуем у следующего» на ошибке Apple: повтор запроса к
  ключу на другую учётку — это вторая лицензия Apple на тот же трек. Повторяем
  только транспортный сбой (контейнер не ответил), и только на другом инстансе.
"""
from __future__ import annotations

import asyncio
import hashlib
import threading
import time

from . import apple_plan, lite, pacing

#: Сервис в счётчиках ``ripster/pacing.py``. Штраф за 403/429, удвоение паузы и
#: прощение после 2xx получаем даром — тем же кодом, что бережёт учётки на
#: дороге закачки.
PACE_SERVICE = "relay-upstream"

#: Сколько жить ответу /status без перепроверки. Вопрос дешёвый, но на каждый
#: запрос клиента лезть к врапперу нельзя: сами себе устроим «слишком много».
_HEALTH_TTL = 60.0

#: Транспортный сбой выводит инстанс из выбора на этот срок, не дожидаясь
#: следующего хозяйского health-прохода.
_DOWN_TTL = 30.0

#: Что реле умеет спросить у враппера. Пути — те же, что у wm.wol.moe /
#: wrapper-lite, чтобы любой lite-клиент говорил с нами без переделки.
ENDPOINTS = ("status", "m3u8", "key", "lyrics", "webplayback", "license")

_lock = threading.RLock()
_clients: dict[str, lite.LiteClient] = {}
_health: dict[str, dict] = {}          # url → {"ts", "reachable", "regions", "error"}
_down_until: dict[str, float] = {}     # url → ts (транспортный сбой)
_inflight: dict[str, int] = {}         # url → сколько запросов в работе


class RelayUpstreamError(RuntimeError):
    """Враппер ответил ошибкой. `code` — поле конверта (наружу), `http_status` —
    HTTP-код upstream (только для штрафов), `retry_after` — сколько ждать."""

    def __init__(self, msg: str, code: int = -1, http_status: int = 0):
        super().__init__(msg)
        self.code = code
        self.http_status = int(http_status or 0)
        self.retry_after = 0.0


class RelayNoInstance(RuntimeError):
    """Спросить не у кого: пул пуст, все инстансы остыют или занятые.

    `reason` — машинный ключ (empty / cooling / busy / down), чтобы реле отдало
    честный 429/503 с Retry-After, а не «500 неизвестно почему»."""

    def __init__(self, reason: str, retry_after: float = 0.0, detail: str = ""):
        super().__init__(detail or reason)
        self.reason = reason
        self.retry_after = float(retry_after or 0.0)


# ── конфигурация пула ────────────────────────────────────────────────────────

def instances(config: dict | None = None) -> list[dict]:
    """Список инстансов из хозяйского конфига.

    `relay-instances`: [{url, label, account, streams}] — `url` обязателен,
    `account` (apple-id учётки) желателен: по нему считается потолок
    одновременных потоков тарифа. Ключа нет или он пустой список — реле работает
    «из коробки» на том контейнере, что уже поднят (замер 24.09: один
    ripster-wrapper-lite на 127.0.0.1:12340).

    Но непустой список, в котором не осталось ни одного годного инстанса, — это
    НЕ «настройки нет»: хозяин задал пул, и подменять ему молча другой учёткой
    нельзя. Такой пул считается пустым, и клиент получает честный `empty`.

    Поле `enabled: false` выключает инстанс, не удаляя запись: хозяйский
    «вырвать и забыть» лишает нас причины, по которой пул стал меньше.
    """
    cfg = config or {}
    raw = cfg.get("relay-instances")
    out: list[dict] = []
    if isinstance(raw, list) and raw:
        for item in raw:
            if not isinstance(item, dict):
                continue
            if str(item.get("enabled", True)).lower() in ("0", "false", "no", "off"):
                continue
            url = _norm_url(item.get("url") or item.get("base_url"))
            if not url:
                continue
            out.append({
                "url": url,
                "label": str(item.get("label") or "")[:40] or url.rsplit(":", 1)[-1],
                "account": str(item.get("account") or "")[:80],
                "streams": _pos_int(item.get("streams")),
            })
        return out
    return [{"url": _norm_url(lite.lite_url(cfg)),
             "label": "lite", "account": "", "streams": 0}]


def _norm_url(raw) -> str:
    s = str(raw or "").strip().rstrip("/")
    if not s:
        return ""
    if "://" not in s:
        s = "http://" + s
    return s


def _pos_int(v) -> int:
    try:
        n = int(v or 0)
    except (TypeError, ValueError):
        return 0
    return n if n > 0 else 0


def cap_of(inst: dict, config: dict | None = None) -> int:
    """Потолок одновременных запросов к этому инстансу.

    Приоритет: явно названное в конфиге → тариф учётки (individual/student 1,
    family 6, «неизвестен» 1 — right на шесть надо ЗНАТЬ, а не предположить) →
    единица. Общего `relay-max-concurrency` здесь нет: он ограничен сверху
    тем же тарифом и только размывает ответ на вопрос «почему именно столько».
    """
    if inst.get("streams"):
        return int(inst["streams"])
    acct = str(inst.get("account") or "")
    if acct:
        try:
            return max(1, int(apple_plan.cap_for(acct) or 1))
        except Exception:                                      # noqa: BLE001
            return 1
    return 1


def ident_of(inst: dict) -> str:
    """Несекретное имя инстанса для счётчиков: хэш URL'а.

    В `dist/pacing.json` не должен попадать ни адрес с портом, ни, тем более,
    что-то похожее на учётку — файл лежит в `dist/` и показывается владельцу."""
    url = str(inst.get("url") or "")
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:12] if url else ""


# ── health ───────────────────────────────────────────────────────────────────

def _client(url: str) -> lite.LiteClient:
    """Клиент на инстанс, с ОБЩИМ шифрованным кэшем ключей.

    Кэш один на пул намеренно: попадание в него — это НОЛЬ обращений к Apple,
    а обращения к Apple — единственный расход, за который платят учётки.
    Тот же файл, что читает путь Apple в PC (ripster/lite.py), поэтому реле и
    закачка не просят один и тот же ключ дважды."""
    with _lock:
        cl = _clients.get(url)
        if cl is None:
            cache = lite.LiteKeyCache()
            cl = _clients[url] = lite.LiteClient(url, cache=cache)
        return cl


def probe(inst: dict, force: bool = False,
          now: float | None = None) -> dict:
    """Один дешёвый GET /status к инстансу, запомненный на _HEALTH_TTL.

    Возвращает {"reachable", "logged_in", "regions", "error"} — НИКАКИХ
    учётных данных, только витрины и факт ответа.
    """
    url = str(inst.get("url") or "")
    now = time.time() if now is None else now
    if not force:
        rec = _health.get(url)
        if rec and now - rec["ts"] < _HEALTH_TTL:
            return rec
    regions: list[str] = []
    reachable, err = False, ""
    try:
        data = _client(url).status()
        reachable = True
        regions = [str(r) for r in (data.get("regions") or []) if str(r)]
    except lite.LiteError as e:
        err = str(e)[:160]
        # HTTP был, а конверт мы не прочли — враппер жив, но залогинен ли,
        # неизвестно: «неизвестно» показываем отдельно от «не отвечает»
        reachable = bool(getattr(e, "http_status", 0))
    except Exception as e:                               # noqa: BLE001
        err = f"{type(e).__name__}: {e}"[:160]
    rec = {"ts": now, "reachable": reachable, "logged_in": bool(regions),
           "regions": regions, "error": err}
    with _lock:
        _health[url] = rec
    return rec


def probe_all(config: dict | None = None, force: bool = False) -> list[dict]:
    """health по всему пулу с сырыми url/label — только для хозяйской
    диагностики (`/relay/status` наружу идёт через snapshot, где адресов нет)."""
    return [dict(probe(inst, force=force), label=inst["label"],
                 url=inst["url"]) for inst in instances(config)]


# ── выбор инстанса ───────────────────────────────────────────────────────────

def _load(url: str) -> int:
    with _lock:
        return int(_inflight.get(url) or 0)


def _hold(url: str) -> None:
    with _lock:
        _inflight[url] = _inflight.get(url, 0) + 1


def _free(url: str) -> None:
    with _lock:
        n = _inflight.get(url, 0) - 1
        if n > 0:
            _inflight[url] = n
        else:
            _inflight.pop(url, None)


def candidates(config: dict | None = None, now: float | None = None,
               exclude=()) -> list[dict]:
    """Инстансы, готовые принять запрос прямо сейчас, по возрастанию загрузки.

    Каждому считаем `cooling` (штраф/потолок из пейсинга) и `busy` (все места
    заняты). Порядок — «кому проще»: при равной загрузке раньше встанет тот, к
    кому реже ходили (стабильность сортировки по ident даёт ротацию)."""
    now = time.time() if now is None else now
    skip = set(exclude or ())
    out = []
    for inst in instances(config):
        url = inst["url"]
        if url in skip:
            continue
        wait = pacing.wait_seconds(PACE_SERVICE, ident_of(inst), config, now=now)
        down = max(0.0, _down_until.get(url, 0.0) - now)
        cap = cap_of(inst, config)
        load = _load(url)
        health = _health.get(url) or {}
        out.append({**inst, "cap": cap, "load": load, "wait": wait, "down": down,
                    "regions": [str(r) for r in (health.get("regions") or [])],
                    "busy": load >= cap,
                    "ready": wait <= 0 and down <= 0 and load < cap})
    out.sort(key=lambda i: (not i["ready"], i["wait"], i["down"], i["load"],
                            i["url"]))
    return out


def pick(config: dict | None = None, exclude=()) -> dict:
    """Один инстанс под запрос или RelayNoInstance с честной причиной."""
    now = time.time()
    cands = candidates(config, now=now, exclude=exclude)
    if not cands:
        raise RelayNoInstance("empty",
                              detail="свободных key-серверов нет (все уже пробованы)"
                              if exclude else "relay-instances пуст")
    live = [c for c in cands if c["ready"]]
    if live:
        return live[0]
    if all(c["busy"] for c in cands):
        raise RelayNoInstance("busy", retry_after=0.5,
                              detail="все инстансы заняты в пределах тарифа")
    waits = [c["wait"] for c in cands if c["wait"] > 0]
    if waits:
        raise RelayNoInstance("cooling", retry_after=max(1.0, min(waits)),
                              detail="учётки остыают после 429/403")
    raise RelayNoInstance("down", retry_after=min(_DOWN_TTL, max(
        [c["down"] for c in cands if c["down"] > 0] or [_DOWN_TTL])),
        detail="key-серверы не отвечают")


# ── обход пула (single-flight) ───────────────────────────────────────────────

def _plan_call(endpoint: str, inst: dict, params: dict) -> dict:
    """Синхронный звонок врапперу. Выполняется в threadpool: lite держит
    соединение до 120 с, вешать на этом событийный цикл нельзя."""
    cl = _client(inst["url"])
    if endpoint == "key":
        # кэш + единый на процесс «замок ключей»: одна закачка одного трека
        # не должна выжимать лицензию Apple дважды
        return cl.key(str(params.get("adamId") or ""), str(params.get("uri") or ""))
    if endpoint == "m3u8":
        return {"adamId": str(params.get("adamId") or ""),
                "m3u8": cl.m3u8_url(str(params.get("adamId") or ""))}
    if endpoint == "webplayback":
        return cl.webplayback(str(params.get("adamId") or ""))
    if endpoint == "lyrics":
        return cl.get_data("/lyrics", {
            "adamId": params.get("adamId") or "",
            "language": params.get("language") or "en",
            "syllable": params.get("syllable") or "1"}, timeout=30.0)
    if endpoint == "license":
        return cl.post_data("/license", {
            "adamId": params.get("adamId"), "challenge": params.get("challenge"),
            "uri": params.get("uri"), "drm-type": params.get("drm-type") or "wv"},
            timeout=60.0)
    if endpoint == "status":
        return cl.status()
    raise RelayUpstreamError(f"неизвестный эндпоинт {endpoint}", code=400)


async def call(endpoint: str, config: dict | None = None,
               params: dict | None = None, attempts: int = 2) -> dict:
    """Спросить `endpoint` у пула → `data` из конверта враппера дословно.

    Транспортный сбой (контейнер молчит) пробуем ещё раз на другом инстансе —
    его вина не в запросе. Ошибку из конверта (Apple/враппер ответили) НЕ
    повторяем: второй лицензией на тот же трек мы чужой ответ не исправим, а
    расход учётки удвоим.
    """
    params = params or {}
    cfg = config or {}
    tried: list[str] = []
    last: Exception | None = None
    for _ in range(max(1, min(int(attempts or 1), 4))):
        try:
            inst = pick(cfg, exclude=tried)
        except RelayNoInstance as e:
            # пустой список инстансов vs «все заняты/остывают» — разные ответы
            # клиенту; если хоть одна попытка была, помним её причину
            if last is None:
                raise
            raise RelayNoInstance(e.reason, retry_after=e.retry_after,
                                  detail=str(last)[:200]) from e
        url = inst["url"]
        _hold(url)
        try:
            data = await asyncio.to_thread(_plan_call, endpoint, inst, params)
        except lite.LiteError as e:
            status = int(getattr(e, "http_status", 0) or 0)
            _free(url)
            if status:
                pacing.outcome(PACE_SERVICE, ident_of(inst), status)
                raise RelayUpstreamError(str(e)[:200], code=int(e.code or -1),
                                         http_status=status) from e
            _note_down(url)                      # контейнер не ответил
            last, tried = e, tried + [url]
            continue
        except Exception as e:                   # noqa: BLE001
            _free(url)
            _note_down(url)
            last, tried = e, tried + [url]
            continue
        _free(url)
        pacing.outcome(PACE_SERVICE, ident_of(inst), 200)
        if not isinstance(data, dict):
            raise RelayUpstreamError("key-сервер вернул не-объект", code=500)
        return data
    if last is not None:
        raise RelayUpstreamError(str(last)[:200] or "key-сервер недоступен",
                                 code=502,
                                 http_status=int(getattr(last, "http_status", 0) or 0))
    raise RelayNoInstance("down", detail="key-серверы не отвечают")


def _note_down(url: str) -> None:
    with _lock:
        _down_until[url] = time.time() + _DOWN_TTL


# ── снимок для хозяйской админки и /relay/status ─────────────────────────────

def snapshot(config: dict | None = None, force: bool = False) -> dict:
    """Снимок пула: {"ready", "regions", "instances", "reason"} — для
    хозяйской админки и для `/relay/status`.

    `reason` — машинный ключ пустого/больного пула ("", empty, down, cooling,
    busy), чтобы интерфейс говорил причину, а не рисовал красную точку.
    """
    cands = candidates(config)
    for c in cands:
        c["health"] = probe(c, force=force)
    regions: list[str] = []
    for c in cands:
        for r in (c["health"].get("regions") or []):
            if r not in regions:
                regions.append(r)
    ready = any(c["health"]["reachable"] and c["ready"] for c in cands)
    rows = []
    for c in cands:
        rows.append({
            "label": c["label"],
            "ready": bool(c["ready"] and c["health"]["reachable"]),
            "reachable": c["health"]["reachable"],
            "logged_in": c["health"]["logged_in"],
            "regions": c["health"]["regions"],
            "load": c["load"], "cap": c["cap"],
            "cooling": round(c["wait"], 1), "down": round(c["down"], 1),
            "error": c["health"]["error"] if not c["health"]["reachable"] else "",
        })
    reason = ""
    if not cands:
        reason = "empty"
    elif not any(c["health"]["reachable"] for c in cands):
        reason = "down"
    elif not ready:
        reason = "cooling" if any(c["wait"] > 0 for c in cands) else "busy"
    return {"ready": ready, "regions": regions, "instances": rows, "reason": reason}


def reset() -> None:
    """Только для тестов: забыть клиентов, health и загрузку."""
    with _lock:
        for cl in _clients.values():
            cl.close()
        _clients.clear()
        _health.clear()
        _down_until.clear()
        _inflight.clear()
