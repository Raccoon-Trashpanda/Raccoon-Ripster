"""Сколько ОДНОВРЕМЕННЫХ потоков расшифровки выдержит эта учётка Apple.

Владелец 24.09.2026 просил внедрить всё полезное из чата @apple_music_alac.
Пункт про потоки (#343764, #348845): личная подписка Apple Music терпит ОДИН
поток расшифровки, семейная — до шести. На личной второй поток Apple встречает
диалогом «More than one device is trying to play music» и `end lease code 3084`.

Почему это вообще наша проблема, а не враппера. Один слот пула = одна учётка =
один контейнер, и `acquire()` второго слота той же учётке не отдаёт. Но есть
обходной путь: задача с `_force_slot` (маршрутизатор подобрал витрину) берёт
порт слота НАПРЯМУЮ, не помечая слот занятым, — и две закреплённые за одним
слотом задачи садятся двумя потоками на одну учётку. На личной подписке это
ровно тот 3084.

Здесь живут: разбор тарифа из `/v1/me/account?meta=subscription`, потолок
потоков на учётку и реакция на 3084 (сузить до одного потока и дать лизингу
остыть, а не хоронить учётку — в отличие от `device_limit`/3062, где пауза
входа на 12 часов оправдана: там помогает только время, а не порядок наших
действий).

Неизвестный тариф = один поток. Это не осторожность ради осторожности: у нас
нет права угадывать семейный план, потому что цена ошибки — диалог Apple и
потерянный лизинг, а цена правильной догадки всего лишь скорость.

Ключ хранилища — `wrapper_pool.account_identity` (id+хеш пароля либо сам токен):
тот же ключ, что у паузы входа, чтобы одна учётка не получила два разных
счёта при перестановке слотов местами (09.08.2026 именно так два аккаунта
делили одну device-identity).
"""

import json
import os
import time
from pathlib import Path

#: Сколько потоков даёт этот тариф. Ключи — нормализованные (lower) значения
#: поля `plan` из ответа Apple.
PLAN_STREAMS = {
    "individual": 1,
    "student":    1,
    "family":     6,
}

#: Потолок, который не поднимет ни одна настройка: больше шести Apple не
#: разрешает ни на одном тарифе, а седьмой поток — это гарантированный 3084.
MAX_STREAMS = 6

#: Тариф, которого нет в списке (новый план, другой регион, пустой ответ).
UNKNOWN_STREAMS = 1

#: 3084 — лизинг удержан чужой сессией. Apple освобождает его сам через
#: несколько минут простоя; брать его обратно раньше — значит словить тот же
#: диалог второй раз и удвоить время ожидания.
LEASE_COOLDOWN_S = 15 * 60

#: Тариф меняется реже, чем раз в месяц (продление подписки), но спрашивать
#: его вечно нельзя: устаревшее «family» после перехода на «individual» было бы
#: тем самым обещанием, которого код не даёт.
PLAN_TTL_S = 30 * 24 * 3600


def plan_known(account_id: str, now: float | None = None) -> bool:
    """Есть ли свежее измерение тарифа (не «неизвестен» и не протухшее)."""
    now = time.time() if now is None else now
    ent = _load().get(_key(account_id)) or {}
    return bool(ent.get("plan")) and (now - float(ent.get("at") or 0)) <= PLAN_TTL_S


#: Повторять запрос о тарифе чаще нельзя: учётка Apple живёт на лизингах
#: устройств, и «спросим ещё раз» на каждой задаче — это лишние ходы к amp-api
#: без всякой на то надобности. Раз в час более чем достаточно: тариф не
#: меняется в середине закачки.
MEASURE_MIN_INTERVAL_S = 3600.0


def measure_container(account_id: str, container: str) -> str:
    """Измерить тариф по токенам контейнера слота ('' — измерить не удалось).

    Вызывается с дороги закачки (`wrapper_pool.up_slot`), когда слот только что
    признали годным, — поэтому обязан быть дешёвым: локальный файл проверяется
    до всякой сети, и запрос уходит максимум раз в час на учётку."""
    k = _key(account_id)
    if not k:
        return ""
    now = time.time()
    if plan_known(k, now=now):
        return plan_of(k)
    ent = _load().get(k) or {}
    if now - float(ent.get("tried_at") or 0) < MEASURE_MIN_INTERVAL_S:
        return plan_of(k)
    d = _load()
    ent = dict(d.get(k) or {})
    ent["tried_at"] = now
    d[k] = ent
    _save(d)
    try:
        from . import apple_accounts as aa
        from .credential_health import subscription_meta
        raw = aa._account_json(container)
    except Exception:                                       # noqa: BLE001
        return plan_of(k)
    if not raw:
        return plan_of(k)
    return record_plan(k, subscription_meta(raw), now=now)


def _path() -> Path:
    base = Path(os.environ.get("RIPSTER_BASE_DIR")
                or Path(__file__).resolve().parent.parent)
    p = base / "dist" / "docker" / "apple_plan.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _load() -> dict:
    """Состояние с посимвольным перечитыванием только когда файл менялся:
    `health()` пула спрашивает потолок для каждого слота, а слотов шесть."""
    p = _path()
    try:
        mtime = p.stat().st_mtime
    except Exception:                                       # noqa: BLE001
        return {}
    hit = _MEM.get(str(p))
    if hit and hit[0] == mtime:
        return dict(hit[1])
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        d = d if isinstance(d, dict) else {}
    except Exception:                                       # noqa: BLE001
        d = {}
    _MEM[str(p)] = (mtime, d)
    return dict(d)


#: путь -> (mtime, разобранный файл). Кэш на время жизни процесса: файл пишет
#: только этот процесс, а `tmp.replace(p)` меняет mtime на каждой записи.
_MEM: dict[str, tuple[float, dict]] = {}


_MEM: dict[str, tuple[float, dict]] = {}


def _save(d: dict) -> None:
    try:
        p = _path()
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(p)
        _MEM[str(p)] = (p.stat().st_mtime, dict(d))
    except Exception as e:                                  # noqa: BLE001
        _MEM.pop(str(_path()), None)
        print(f"[plan] состояние тарифа не записано: {e}", flush=True)


def _key(account_id: str) -> str:
    return str(account_id or "").strip().lower()


# ── разбор тарифа ────────────────────────────────────────────────────────────

#: Поля, где Apple успевал прятать название тарифа. `plan` — основное у
#: `meta.subscription`; остальные встречаются в старых сборках amp-api и в
#: ответах региональных витрин.
_PLAN_FIELDS = ("plan", "type", "productType", "product_type")


def parse_plan(sub: dict) -> str:
    """Название тарифа по ответу `meta.subscription` ('' — не назван).

    Возвращает нормализованное (lower) значение ИМЕННО того слова, которое
    прислал Apple, — распознавание не подгоняется под удобный ответ. Если
    завтра Apple назовёт тариф `DUO`, здесь появится `duo`, а `cap_for`
    отнесёт его к неизвестным (1 поток) и попросит добавить строку в
    `PLAN_STREAMS`, а не угадать."""
    if not isinstance(sub, dict):
        return ""
    if isinstance(sub, dict):
        meta = sub.get("meta")
        if isinstance(meta, dict) and isinstance(meta.get("subscription"), dict):
            sub = meta["subscription"]
    for f in _PLAN_FIELDS:
        v = sub.get(f)
        if isinstance(v, str) and v.strip():
            return v.strip().lower()
    # Семейный план бывает помечен и без названия тарифа — хозяин он или член.
    if sub.get("isFamilyHost") or sub.get("isFamilyMember") or sub.get("familySharing"):
        return "family"
    return ""


def streams_for_plan(plan: str) -> int:
    """Сколько потоков разрешает этот тариф. Неизвестный — один, а не «сколько
    попросили»."""
    return PLAN_STREAMS.get(str(plan or "").strip().lower(), UNKNOWN_STREAMS)


# ── учёт ─────────────────────────────────────────────────────────────────────

def record_plan(account_id: str, sub: dict, now: float | None = None, *,
                force: bool = False) -> str:
    """Запомнить тариф по ответу `/v1/me/account?meta=subscription`.

    `sub` — словарь `meta.subscription` ИЛИ весь ответ amp-api целиком (в нём
    `meta.subscription` и лежит): и то, и другое достается вызывающим из разных
    мест, а тихое «плоский ответ = тарифа нет» означало бы учётку на одном
    потоке вместо шести.

    Возвращает записанный тариф или '' — если названия в ответе нет. Пустой
    ответ НЕ затирает уже известный тариф: «не услышали» и «тарифа нет» —
    разные события, и второе здесь бывает только как явное имя плана.

    `force=True` — явный пересчёт по требованию: снимает сужение после 3084,
    не дожидаясь истечения `PLAN_TTL_S`.
    """
    k = _key(account_id)
    if not k:
        return ""
    plan = parse_plan(sub)
    now = time.time() if now is None else now
    d = _load()
    ent = dict(d.get(k) or {})
    if not plan:
        # Имени нет — но факт опроса запоминаем, иначе запись никогда не
        # станет «свежей» и каждый проход health будет лезть в сеть.
        ent.setdefault("at", now)
        ent["plan_seen"] = False
        d[k] = ent
        _save(d)
        return ""
    if not force and ent.get("plan") == plan and (now - float(ent.get("at") or 0)) < PLAN_TTL_S:
        return plan
    ent.update({"plan": plan, "streams": streams_for_plan(plan), "at": now,
                "plan_seen": True, "narrowed_at": 0})
    d[k] = ent
    _save(d)
    print(f"[plan] {_mask_id(k)}: тариф {plan} → до {ent['streams']} потоков",
          flush=True)
    return plan


def plan_of(account_id: str) -> str:
    return str((_load().get(_key(account_id)) or {}).get("plan") or "")


def cap_for(account_id: str, now: float | None = None) -> int:
    """Потолок одновременных потоков расшифровки для этой учётки.

    3084 сужает учётку до одного потока НАДДОЛГО: повторить отказ — значит
    снова держать лизинг, а право на шесть потоков даёт не поле `plan`, а сам
    Apple. Снимает сужение только СВЕЖЕЕ измерение тарифа (`record_plan` после
    `PLAN_TTL_S` или `force=True`): новый факт от Apple перевешивает старый
    симптом."""
    now = time.time() if now is None else now
    ent = _load().get(_key(account_id)) or {}
    if float(ent.get("single_until") or 0) > now:
        return 1
    if not ent.get("plan") or (now - float(ent.get("at") or 0)) > PLAN_TTL_S:
        return UNKNOWN_STREAMS
    if float(ent.get("narrowed_at") or 0) > float(ent.get("at") or 0):
        return 1
    try:
        streams = int(ent.get("streams") or 0)
    except (TypeError, ValueError):
        streams = 0
    return max(1, min(streams or streams_for_plan(ent.get("plan")), MAX_STREAMS))


def note_lease_conflict(account_id: str, now: float | None = None) -> bool:
    """Apple сказал «More than one device is trying to play music» (3084).

    Сузим учётку до одного потока и ставим её в остывание лизинга. Возвращает
    True на ПЕРВОМ сигнале — владельцу сообщают один раз, а не на каждую
    строку журнала: журнал враппера говорит это на каждом треке."""
    k = _key(account_id)
    if not k:
        return False
    now = time.time() if now is None else now
    d = _load()
    ent = dict(d.get(k) or {})
    fresh = float(ent.get("single_until") or 0) <= now
    ent["single_until"] = now + LEASE_COOLDOWN_S
    ent["streams"] = 1
    ent["narrowed_at"] = now
    ent["hits"] = int(ent.get("hits") or 0) + 1
    d[k] = ent
    _save(d)
    if fresh:
        print(f"[plan] {_mask_id(k)}: Apple отказал вторым потоком (3084) — "
              f"учётка переведена в один поток, лизинг остывает до "
              f"{time.strftime('%d.%m %H:%M', time.localtime(ent['single_until']))}",
              flush=True)
    return fresh


def lease_cooling(account_id: str, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    return float((_load().get(_key(account_id)) or {}).get("single_until") or 0) > now


def snapshot(account_id: str, now: float | None = None) -> dict:
    """Что известно об этой учётке — для настроек и отчёта сторожа.

    Ключи: `plan` ('' — тариф не измерен), `streams`, `cooling_until`, `hits`,
    `measured_at`. Ни почт, ни паролей, ни токенов здесь нет и быть не может."""
    now = time.time() if now is None else now
    ent = _load().get(_key(account_id)) or {}
    cooling = float(ent.get("single_until") or 0)
    return {
        "plan": str(ent.get("plan") or ""),
        "streams": cap_for(account_id, now=now),
        "known": bool(ent.get("plan")) and (now - float(ent.get("at") or 0)) <= PLAN_TTL_S,
        "cooling_until": cooling if cooling > now else 0,
        "hits": int(ent.get("hits") or 0),
        "measured_at": float(ent.get("at") or 0),
    }


def _mask_id(ident: str) -> str:
    """Идентичность учётки в журнал — хвост и короткий хеш, как везде."""
    s = str(ident or "")
    if "@" in s:
        try:
            from .credential_health import _ident
            return _ident(s)
        except Exception:                                   # noqa: BLE001
            return f"...{s.split('@')[0][-4:]}@…"
    return f"...{s[-8:]}" if len(s) > 8 else "???"
