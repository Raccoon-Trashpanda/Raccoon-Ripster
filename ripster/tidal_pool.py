"""Пул учёток Tidal.

У Deezer, Qobuz, SoundCloud и Яндекса пулы появились раньше, а Tidal оставался
одиночным — и это было не ленью, а препятствием: качает его OrpheusDL, который
держит сессию в ``orpheus/config/loginstorage.bin`` и **не принимает путь к
конфигу** (в его argparse есть только ``-o``, ``-lr``, ``-cv``, ``-cr``,
``-sd``). У streamrip для Qobuz есть ``--config-path``, здесь такого флага нет.

Изоляция поэтому делается рабочим каталогом. OrpheusDL берёт свою папку данных
как относительный путь ``config`` от текущего каталога (``orpheus/core.py``:
``self.data_folder_base = 'config'``), а код мы и так подкладываем через
``sys.path`` — значит слот это отдельная папка, где лежит свой ``config/`` и
ссылки на общие ``modules``/``extensions``/``orpheus``/``utils``.

Проверено 12.09.2026: запуск из ``dist/tidal_pool/acct1`` с junction-ссылками
доходит до разбора аргументов OrpheusDL без единой ошибки импорта.

Слот 0 — основная учётка (ключи ``tidal-*`` в конфиге) и ОБЫЧНЫЙ каталог
``orpheus/``: установка с одной учёткой должна вести себя ровно так же, как до
появления пула, вплоть до того же файла сессии.

Каталог настроек слота (``<слот>/config``) движок теперь знает явно: качество и
папка сохранения пишутся в НЕГО, а не в общий ``orpheus/config/settings.json``,
который одновременно перечитывает соседний прогон (Spotify). Сессия у слота
своим файлом в этом же каталоге; основной аккаунт работает общей.
"""
from __future__ import annotations

import os
import subprocess
import threading
from pathlib import Path

from ripster import account_fallback as _afb

#: Папки, которые слот берёт у общей установки OrpheusDL. Копировать их нельзя:
#: это сотни мегабайт и, что хуже, две расходящиеся копии модулей.
_SHARED_DIRS = ("modules", "extensions", "orpheus", "utils")


def _base_dir() -> Path:
    """Корень установки — от ЭТОГО файла, а не от `sys.argv[0]`.

    Первая версия брала каталог запускающего скрипта, и это ломалось ровно там,
    где пул и проверяют: прогон из отдельного скрипта создал слот рядом со
    скриптом, ссылки на общие папки указали в пустоту, и OrpheusDL упал на
    `makedirs('extensions')` — junction был, но вёл в никуда. Путь к своему
    модулю известен всегда и не зависит от того, чем запущен процесс.
    """
    return Path(__file__).resolve().parent.parent


def orpheus_dir() -> Path:
    return _base_dir() / "orpheus"


def slot_dir(slot: int) -> Path:
    """Рабочий каталог слота. Ноль — сама установка OrpheusDL."""
    if slot <= 0:
        return orpheus_dir()
    return _base_dir() / "dist" / "tidal_pool" / f"acct{slot}"


def slot_config_dir(slot: int) -> Path:
    """Каталог настроек слота. Движок пишет качество/папку именно сюда — иначе
    параллельный прогон соседнего сервиса перебивал бы настройки Tidal (тот же
    разбор, что покоридорил Beatport с JioSaavn)."""
    return slot_dir(slot) / "config"


def slot_session(slot: int) -> Path:
    """Файл сессии слота. Для основного аккаунта (слот 0) это общий
    ``orpheus/config/loginstorage.bin`` — ровно тот, куда пишет вход из
    Settings, поэтому никаких ручных действий переезд коридора не требует."""
    return slot_config_dir(slot) / "loginstorage.bin"


def _link_shared(dst: Path, src: Path) -> None:
    """Связать папку слота с общей. Junction на Windows, симлинк на Unix.

    Junction, а не копия: копия разъехалась бы с общей установкой при первом же
    обновлении модуля, и расследование «у одного аккаунта работает, у другого
    нет» стоило бы дороже всей затеи.
    """
    if dst.exists():
        return
    try:
        if os.name == "nt":
            subprocess.run(["cmd", "/c", "mklink", "/J", str(dst), str(src)],
                           check=True, capture_output=True)
        else:
            dst.symlink_to(src, target_is_directory=True)
    except Exception as e:  # noqa: BLE001
        print(f"[tidal-pool] не смог связать {dst.name}: {e}", flush=True)


def ensure_slot(slot: int) -> Path:
    """Создать каталог слота, если его ещё нет. Возвращает путь."""
    d = slot_dir(slot)
    if slot <= 0:
        return d
    (d / "config").mkdir(parents=True, exist_ok=True)
    for name in _SHARED_DIRS:
        _link_shared(d / name, orpheus_dir() / name)
    # Настройки заводит движок: `seed_slot` копирует общий файл только если он
    # читается как JSON, пишет атомарно, и с тех пор `modules.tidal` догоняет
    # общий конфиг сам. Раньше здесь лежал слепой `write_bytes(read_bytes(...))`
    # без права на обновление — скопированная при создании осколка, копия годами
    # показывала слоту устаревшие клиентские ключи, даже когда владелец менял их
    # в общем конфиге.
    from ripster.engines.tidal import seed_slot

    seed_slot(slot_config_dir(slot), src=orpheus_dir() / "config" / "settings.json")
    return d


#: Клиенты, которыми OrpheusDL держит сессию Tidal. Их идентификаторы лежат в
#: его же настройках, и подставлять свои нельзя: сессия, выписанная чужим
#: клиентом, не даст тех прав, за которыми слот и заводится.
_CLIENT_KEYS = {
    "TV":             ("tv_atmos_token", "tv_atmos_secret"),
    "MOBILE_ATMOS":   ("mobile_atmos_hires_token", None),
    "MOBILE_DEFAULT": ("mobile_hires_token", None),
}


def _orpheus_clients() -> dict[str, tuple[str, str]]:
    """(client_id, client_secret) по имени сессии — из настроек OrpheusDL."""
    import json

    try:
        st = json.loads((orpheus_dir() / "config" / "settings.json").read_text(encoding="utf-8"))
        mod = st.get("modules", {}).get("tidal", {})
    except Exception:  # noqa: BLE001
        mod = {}
    out: dict[str, tuple[str, str]] = {}
    for name, (id_key, sec_key) in _CLIENT_KEYS.items():
        cid = (mod.get(id_key) or "").strip()
        if cid:
            out[name] = (cid, (mod.get(sec_key) or "").strip() if sec_key else "")
    return out


def write_session(slot: int, refresh: str, country: str = "") -> dict:
    """Выписать слоту собственную сессию OrpheusDL из refresh-токена.

    Зачем так, а не «войти»: вход у OrpheusDL интерактивный (TV-логин требует
    открыть ссылку и подтвердить, mobile-логин спрашивает пароль через
    ``input()``), а мы запускаем его без терминала. Зато формат его хранилища
    прост и известен — три клиента, у каждого access/refresh/expires/user_id/
    country_code, — и refresh-токен Tidal принимает от ЛЮБОГО из этих клиентов.

    Возвращает отчёт: по каким клиентам сессия получена, а по каким нет и
    почему. Пустой словарь вместо исключения не отдаём — молчаливый провал
    здесь означал бы слот, который «есть», но ничего не качает.
    """
    import datetime as _dt
    import pickle

    import httpx

    d = ensure_slot(slot)
    clients = _orpheus_clients()
    if not clients:
        return {"ok": False, "why": "в настройках OrpheusDL нет идентификаторов клиентов Tidal"}

    sessions: dict[str, dict] = {}
    errors: dict[str, str] = {}
    for name, (cid, csec) in clients.items():
        data = {"refresh_token": refresh, "client_id": cid, "grant_type": "refresh_token"}
        if csec:
            data["client_secret"] = csec
        try:
            r = httpx.post("https://auth.tidal.com/v1/oauth2/token", data=data, timeout=30)
        except Exception as e:  # noqa: BLE001
            errors[name] = f"сеть: {type(e).__name__}"
            continue
        if r.status_code != 200:
            # Тело ответа Tidal называет причину точнее нашей догадки.
            errors[name] = f"HTTP {r.status_code}: {r.text[:120]}"
            continue
        j = r.json()
        user = j.get("user") or {}
        sessions[name] = {
            "access_token": j["access_token"],
            "refresh_token": j.get("refresh_token") or refresh,
            "expires": _dt.datetime.now() + _dt.timedelta(seconds=int(j.get("expires_in", 3600))),
            "user_id": int(user.get("userId") or j.get("user_id") or 0),
            "country_code": (user.get("countryCode") or country or "").upper(),
        }

    if not sessions:
        return {"ok": False, "why": "ни один клиент не принял токен", "errors": errors}

    # За основу берём хранилище основной установки: в нём уже есть разделы
    # других модулей (beatport, spotify), и затирать их нулём значило бы
    # разлогинить слот во всём остальном.
    blob: dict = {"advancedmode": False, "modules": {}}
    src = orpheus_dir() / "config" / "loginstorage.bin"
    if src.is_file():
        try:
            from ripster.safe_pickle import safe_loads

            blob = safe_loads(src.read_bytes())
        except Exception:  # noqa: BLE001
            pass
    mod = blob.setdefault("modules", {}).setdefault("tidal", {})
    mod.setdefault("selected", "default")
    sess = mod.setdefault("sessions", {}).setdefault("default", {})
    sess["clear_session"] = False
    sess["custom_data"] = {"sessions": sessions}

    (d / "config").mkdir(parents=True, exist_ok=True)
    (d / "config" / "loginstorage.bin").write_bytes(pickle.dumps(blob))
    return {"ok": True, "clients": sorted(sessions), "errors": errors,
            "country": next(iter(sessions.values())).get("country_code", "")}


def _account_from_dict(a: dict, label_fallback: str) -> dict | None:
    """Учётка — это либо refresh-токен, либо логин с паролем.

    Оба пути у OrpheusDL есть (TV-логин по коду даёт refresh, mobile-логин
    принимает логин/пароль), поэтому оба и принимаем: владелец просил «вход как
    по логину паролю, так и по токенам».
    """
    refresh = (a.get("tidal-refresh") or a.get("refresh") or "").strip()
    email = (a.get("tidal-email") or a.get("email") or "").strip()
    password = (a.get("tidal-password") or a.get("password") or "").strip()
    if not (refresh or (email and password)):
        return None
    return {
        "tidal-refresh": refresh,
        "tidal-email": email,
        "tidal-password": password,
        "tidal-country": (a.get("tidal-country") or a.get("country") or "").strip().upper(),
        "label": a.get("label") or label_fallback,
    }


def health_rank(acct: dict) -> int:
    """Насколько учётка пригодна: 0 — лучшая, 3 — непригодная.

    Читаем ТОЛЬКО измеренное (`tidal_accounts` кэширует ответ Tidal на сутки);
    в сеть отсюда не ходим — `acquire()` зовут во время загрузки.

    «Не спрашивали» и «мертва» — разные состояния, и разводить их обязательно:
    спутав их, мы отправили бы живую, но ещё не измеренную учётку в конец
    очереди. Поэтому неизвестность — это 2, а не 3.
    """
    try:
        from ripster import retired_credentials as _retired
        from ripster import tidal_accounts as _ta

        secret = _ta.account_secret(acct)
        if secret and _retired.is_retired("tidal_account", secret):
            return 3
        info = _ta.known(secret) if secret else None
        if not info:
            return 2                       # не спрашивали — не судим
        if info.get("alive") is None:
            return 2                       # сеть не ответила: не свойство учётки
        if not info.get("alive"):
            return 3
        if info.get("hires") or info.get("lossless"):
            return 0
        return 1                           # жива, но lossless не отдаст
    except Exception:  # noqa: BLE001
        return 2


def health_note(acct: dict) -> dict:
    """Почему слот стоит там, где стоит — словами, для панели настроек.

    Без этой строки в списке видно восемь учёток и непонятно, какими из них
    вообще можно качать: перебор обходит их молча.
    """
    rank = health_rank(acct)
    try:
        from ripster import tidal_accounts as _ta

        info = _ta.known(_ta.account_secret(acct)) or {}
    except Exception:  # noqa: BLE001
        info = {}
    state = {0: "ok", 1: "lossy", 2: "unknown", 3: "unusable"}[rank]
    if rank == 3:
        why = info.get("reason") or "снята автоматикой"
    elif rank == 2:
        why = info.get("reason") or "ещё не проверялась"
    else:
        why = " · ".join(x for x in (info.get("plan"), info.get("quality"),
                                     info.get("country")) if x)
    return {"health": state, "health_why": why, "usable": rank < 3}


def configured_accounts(config: dict) -> list[dict]:
    """Основная учётка (слот 0) плюс всё из ``tidal-accounts``."""
    accounts: list[dict] = []
    sources: list[dict] = []
    primary = _account_from_dict(config, "primary")
    if primary:
        accounts.append(_afb.stamp(primary, _afb.primary_src(config, "tidal"), 0))
        sources.append(_afb.primary_src(config, "tidal"))
    for a in (config.get("tidal-accounts") or []):
        acct = _account_from_dict(a, f"account{len(accounts) + 1}")
        if acct:
            accounts.append(_afb.stamp(acct, a, len(accounts)))
            sources.append(a)
    for i, (acc, src) in enumerate(zip(accounts, sources)):
        if (src or {}).get("priority") is None:
            # Порядок: сперва пригодность, потом СТРАНА, и лишь затем позиция в
            # списке. Страна здесь не вкус, а календарь: новая неделя релизов
            # начинается в Новой Зеландии, и пятничный альбом появляется в её
            # витрине на сутки раньше, чем в европейской. Правило владельца
            # («Новая Зеландия первая») и та же логика, что у автоскачки
            # вишлиста по новозеландской пятнице.
            acc["priority"] = float(health_rank(acc) * 1000 + country_rank(acc) * 10 + i)
    return accounts


#: Чем МЕНЬШЕ число, тем раньше витрина получает релиз. Список задаётся здесь,
#: а не в конфиге, потому что это факт про часовые пояса, а не настройка вкуса;
#: `tidal-country-order` в конфиге его перекрывает, если владелец решит иначе.
_COUNTRY_ORDER = ("NZ", "AU", "JP", "GB", "DE", "US")


def country_rank(acct: dict, config: dict | None = None) -> int:
    """Насколько рано витрина этой страны отдаёт новинки."""
    order = None
    if config:
        raw = config.get("tidal-country-order")
        if isinstance(raw, (list, tuple)) and raw:
            order = tuple(str(x).strip().upper() for x in raw if str(x).strip())
        elif isinstance(raw, str) and raw.strip():
            order = tuple(x.strip().upper() for x in raw.split(",") if x.strip())
    order = order or _COUNTRY_ORDER
    cc = (acct.get("tidal-country") or "").strip().upper()
    if not cc:
        # Страну можно ещё и ИЗМЕРИТЬ: она приходит в ответе Tidal вместе с
        # подпиской. Это точнее метки в конфиге, которую легко забыть проставить.
        try:
            from ripster import tidal_accounts as _ta

            cc = ((_ta.known(_ta.account_secret(acct)) or {}).get("country") or "").upper()
        except Exception:  # noqa: BLE001
            cc = ""
    if not cc:
        return len(order)          # неизвестна — в середину, не в конец
    return order.index(cc) if cc in order else len(order) + 1


_pool_instance: "TidalPool | None" = None
_pool_fingerprint: tuple = ()


def get_pool(config: dict) -> "TidalPool | None":
    """Единственный экземпляр; пересобирается, только когда изменился список
    учёток — иначе занятость слотов терялась бы между задачами.

    ``None``, когда учётка одна: пул из одного слота ничего не решает, а
    рабочий каталог остаётся тем же, что был до его появления.
    """
    global _pool_instance, _pool_fingerprint
    accounts = configured_accounts(config)
    if len(accounts) < 2:
        return None
    # enabled/priority обязаны входить в отпечаток: иначе выключатель и
    # перетаскивание порядка не пересобирали закэшированный пул (18.09.2026).
    fp = tuple(((a.get("tidal-refresh") or a.get("tidal-email") or "")[-12:],
                a.get("enabled", True), a.get("priority")) for a in accounts)
    if _pool_instance is None or fp != _pool_fingerprint:
        _pool_instance = TidalPool(config)
        _pool_fingerprint = fp
    return _pool_instance


def live_status(config: dict) -> dict:
    p = get_pool(config)
    return p.status() if p else {"pool_enabled": False, "accounts": []}


class TidalPool:
    """Раздаёт слоты по одному: две задачи не должны писать в одну сессию."""

    def __init__(self, config: dict) -> None:
        self.accounts = configured_accounts(config)
        self._busy = [False] * len(self.accounts)
        self._lock = threading.Lock()

    def __len__(self) -> int:
        return len(self.accounts)

    def acquire(self, exclude=()) -> tuple[int, dict, Path] | None:
        """(слот, учётка, рабочий каталог) свободной учётки или None."""
        ex = set(exclude or ())
        with self._lock:
            order = _afb.order_indices(self.accounts)
            for i in order:
                if i in ex or self._busy[i]:
                    continue
                self._busy[i] = True
                return i, self.accounts[i], ensure_slot(i)
            # Все отвергнутые заняты или исключены — пробуем хотя бы отвергнутые,
            # иначе задача встанет там, где могла бы просто попробовать ещё раз.
            for i in order:
                if self._busy[i]:
                    continue
                self._busy[i] = True
                return i, self.accounts[i], ensure_slot(i)
        return None

    def release(self, slot: int) -> None:
        with self._lock:
            if 0 <= slot < len(self._busy):
                self._busy[slot] = False

    def status(self) -> dict:
        with self._lock:
            order = _afb.order_indices(self.accounts)
            return {
                "pool_enabled": True,
                "accounts": [
                    {
                        "slot": i,
                        "label": a["label"],
                        "primary": i == 0,
                        "busy": self._busy[i],
                        "mode": "token" if a.get("tidal-refresh") else "password",
                        "country": a.get("tidal-country") or "",
                        # order_pos, а не order.index(i): order_indices НЕ включает
                        # выключенные учётки, и .index падал ValueError'ом, роняя
                        # ВЕСЬ список (500) — тот же баг, что 18.09 выбил пул Qobuz.
                        # Здесь он спал только потому, что выключенных Tidal не было.
                        "order": _afb.order_pos(self.accounts, i),
                        "enabled": a.get("enabled", True),
                        "priority": a.get("priority", i),
                    }
                    for i, a in enumerate(self.accounts)
                ],
            }
