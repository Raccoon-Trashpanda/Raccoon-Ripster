"""Когда отказ — повод взять СЛЕДУЮЩУЮ учётку того же сервиса, а когда не повод.

У владельца по нескольку учёток на сервис (пять Apple, три Qobuz), и смысл в них
появляется только если релиз, недоступный одной, молча уезжает на другую. Раньше
пулы были ТОЛЬКО балансировщиками: `acquire()` выдавал любую свободную учётку, а
после отказа задача просто падала — вторая учётка не получала ни одной попытки.

Главное здесь — таблица, а не код. Перебирать учётки можно ровно на двух классах
отказа, и НЕЛЬЗЯ на третьем:

    entitlement   у ЭТОЙ учётки нет прав на релиз   → следующая учётка
    region        витрина учётки не отдаёт релиз    → учётка в другой стране
    сеть/враппер  авария на нашей стороне           → НЕ перебирать

Третья строка — не осторожность, а защита: враппер лёг, а мы прогоняем через него
все пять учёток подряд, получаем пять одинаковых отказов и жжём слоты устройств
на аварии, которая к учёткам отношения не имеет. Ровно та же логика, по которой
`availability.record_outcome` отказывается записывать сетевую аварию как вердикт
о доступности.

Токены причин приходят из `runner._classify_partial_reason`.
"""
from __future__ import annotations

# Отказы, после которых имеет смысл СЛЕДУЮЩАЯ учётка.
#
# `session` добавлен 21.08.2026 по жалобе владельца: «упирается в
# недействительность primary токена и не качает по второму, по третьему — он даже
# не пытается». И правда не пытался. Мёртвый ARL/токен классификатор помечал как
# `session`, а этого токена в таблице не было — значит перебор не начинался
# вообще, и четыре заведённые учётки лежали мёртвым грузом.
#
# Возражение против добавления было такое: если ляжет сам сервис, все учётки
# отдадут `session`, и мы сожжём пул на общей аварии. Возражение верное, но оно
# про ЦЕНУ, а не про правильность: цена — до четырёх лишних попыток
# (MAX_ACCOUNT_ATTEMPTS), после чего задача всё равно падает. Плата за обратное
# — постоянно неработающий фоллбэк ради редкого случая. Меняем.
RETRY_WITH_NEXT_ACCOUNT = frozenset({"entitlement", "region", "session"})

# Предел попыток на задачу. Без него перебор превращается в `ripster-runaway-runs`:
# прогон, который не может закончиться успехом, но продолжает молотить. Считаем
# ПОПЫТКИ, а не учётки: пул может быть больше, но дальше четвёртой смены смысла
# нет — если четыре учётки сказали «нет», это свойство релиза, а не учёток.
MAX_ACCOUNT_ATTEMPTS = 4

# Сервисы с пулом учёток: имя движка → модуль пула. Apple здесь НЕТ намеренно —
# у него свой, более умный перебор по СТРАНАМ слотов (runner.py, ветка
# `pick_slot_for`), и подменять его общим «следующая свободная» значит потерять
# подбор витрины.
_POOL_MODULES = {
    "deezer":     "ripster.deezer_pool",
    "qobuz":      "ripster.qobuz_pool",
    "soundcloud": "ripster.soundcloud_pool",
    "yandex":     "ripster.yandex_pool",
}


def pool_module(engine_name: str):
    """Модуль пула для движка или None."""
    path = _POOL_MODULES.get(engine_name or "")
    if not path:
        return None
    try:
        import importlib
        return importlib.import_module(path)
    except Exception:
        return None


def pool_size(config: dict, engine_name: str) -> int:
    """Сколько учёток настроено. 0 — если пула у сервиса нет."""
    mod = pool_module(engine_name)
    if mod is None:
        return 0
    try:
        return len(mod._configured_accounts(config) or [])
    except Exception:
        return 0


def tried_slots(task: dict, engine_name: str) -> list:
    return list((task.get("_accts_tried") or {}).get(engine_name) or [])


def mark_tried(task: dict, engine_name: str, slot) -> None:
    """Запомнить, что этой учёткой уже пробовали — иначе перебор зациклится на
    первой свободной."""
    if slot is None:
        return
    d = task.setdefault("_accts_tried", {})
    lst = d.setdefault(engine_name, [])
    if slot not in lst:
        lst.append(slot)


def _better_account_untried(task: dict, engine_name: str, config: dict) -> bool:
    """Есть ли ещё не пробованная учётка с БОЛЬШИМИ правами, чем у пробованных.

    Знание про качество учёток есть только у пула сервиса, поэтому спрашиваем
    его — по желанию: пул без `better_account_untried` просто отвечает «нет», и
    поведение остаётся прежним.
    """
    mod = pool_module(engine_name)
    fn = getattr(mod, "better_account_untried", None)
    if fn is None:
        return False
    try:
        return bool(fn(tried_slots(task, engine_name), config))
    except Exception:       # noqa: BLE001
        return False


def should_try_next(task: dict, engine_name: str, reason: str, config: dict) -> bool:
    """Брать ли следующую учётку после отказа с причиной `reason`.

    Пустой `tried` — это НЕ «ещё ни одной не пробовали, значит можно». Это
    «пула в этой задаче не было вовсе»: при одной настроенной учётке
    `get_pool()` возвращает None, `mark_tried` не вызывается — и наивное
    `len(tried) < pool_size` разрешало бесконечный повтор ТОЙ ЖЕ учётки.
    Ловится только тестом на пул из одного элемента, поэтому условие явное.
    """
    if reason not in RETRY_WITH_NEXT_ACCOUNT:
        # `no-flac` — единственная причина, про которую нельзя решить по одному
        # слову. Deezer на отказ ПО ПРАВАМ учётки отвечает тем же «can't stream
        # the track at the desired bitrate», что и на честное отсутствие FLAC у
        # релиза: 04.09.2026 бесплатная учётка отвечала так на ЛЮБОЙ битрейт,
        # включая 128, а две Family рядом качали тот же трек в FLAC. Задача при
        # этом останавливалась и советовала «выбери MP3 320» — совет в никуда.
        # Спрашиваем пул: есть ли непробованная учётка, которая умеет БОЛЬШЕ
        # текущей. Нет — значит у релиза действительно нет FLAC, и перебор был
        # бы пустой тратой попыток.
        if reason != "no-flac" or not _better_account_untried(task, engine_name, config):
            return False
    tried = tried_slots(task, engine_name)
    if not tried:
        return False
    if len(tried) >= MAX_ACCOUNT_ATTEMPTS:
        return False
    return len(tried) < pool_size(config, engine_name)


# ── Порядок учёток: чей ход первый ──────────────────────────────────────────
# Раньше порядок был жёстко индексом: primary — слот 0, дальше как в конфиге.
# Владелец не мог ни поменять последовательность, ни выключить сдохшую учётку —
# и каждая задача начиналась с той, про которую уже известно, что она не ответит.
#
# Порядок задаётся ДВУМЯ полями на учётке, и оба необязательны:
#   priority  меньше — раньше. По умолчанию — собственный индекс, то есть
#             поведение без настройки в точности прежнее.
#   enabled   False — учётку не берём вовсе.
#
# Индексы НЕ переставляем. Слот — это адрес: у Deezer это каталог
# `dist/deezer_pool/acct{i}`, у Apple — слот устройства. Переставить список
# значит увести чужой конфиг под чужой учёткой. Поэтому сортируется список
# ПРЕДПОЧТЕНИЯ, а сами слоты остаются на своих местах.


def order_indices(accounts: list) -> list:
    """Индексы учёток в порядке предпочтения.

    Если выключены ВСЕ — выключение игнорируется целиком. Это не мягкость: пул,
    вернувший пустоту, для вызывающего неотличим от «все заняты», и тот молча
    уходит на primary-ключ в обход пула. То есть строгое поведение не запретило
    бы ничего, а лишь спрятало бы происходящее от глаз. Лучше взять всех и
    сказать об этом в лог.
    """
    if not accounts:
        return []
    idx = [i for i, a in enumerate(accounts) if (a or {}).get("enabled", True) is not False]
    if not idx:
        print("[accounts] выключены все учётки — беру как есть", flush=True)
        idx = list(range(len(accounts)))
    idx.sort(key=lambda i: (_prio(accounts[i], i), i))
    return idx


def _prio(acct: dict, fallback: int) -> float:
    try:
        v = (acct or {}).get("priority", None)
        return float(fallback if v is None else v)
    except (TypeError, ValueError):
        return float(fallback)


def primary_src(config: dict, service: str) -> dict:
    """Откуда брать `priority`/`enabled` ОСНОВНОЙ учётки.

    У дополнительных учёток есть свой словарь в `<svc>-accounts`, и поля лежат
    прямо в нём. У основной такого словаря нет — она размазана по плоским ключам
    конфига (`deezer-arl`, `qobuz-email`…). Соблазн передать в `stamp` весь
    config велик и НЕВЕРЕН: тогда основная учётка Deezer прочитала бы общий ключ
    `priority`, общий с Qobuz и со всем остальным, — одно поле на все сервисы
    сразу. Поэтому у каждого сервиса свои два ключа, и имена собираются здесь,
    а не по месту.
    """
    svc = (service or "").strip()
    return {"enabled":  config.get(f"{svc}-primary-enabled", True),
            "priority": config.get(f"{svc}-primary-priority", None)}


def stamp(acct: dict, src: dict, index: int) -> dict:
    """Проставить учётке `priority`/`enabled` из её источника в конфиге.

    Вызывается пулами в `_configured_accounts`. Отдельная функция, а не четыре
    копии по пулам: правило «чем меньше priority, тем раньше» должно иметь одно
    место, иначе через месяц у Qobuz оно будет наоборот.
    """
    acct["enabled"] = (src or {}).get("enabled", True) is not False
    acct["priority"] = _prio(src or {}, index)
    return acct
