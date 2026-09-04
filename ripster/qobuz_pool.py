"""
Qobuz multi-account load-balancing pool.

Even simpler than Deezer's (ripster/deezer_pool.py): streamrip's `rip` CLI
takes an explicit ``--config-path`` flag (see ripster/engines/qobuz.py's
build_cmd), so each pool slot just gets its own isolated config.toml
directory passed directly on the command line — no subprocess env override
needed at all.

Slot 0 always uses the primary qobuz-* config keys and streamrip's own
DEFAULT config dir (unaffected — no override needed, so a single-account
setup behaves exactly as before this pool existed). Slots 1+ come from the
``qobuz-accounts`` config list and get an isolated config dir under
``dist/qobuz_pool/acct{i}/``.
"""
from __future__ import annotations

import threading

from ripster import account_fallback as _afb
from pathlib import Path


def _account_from_dict(a: dict, label_fallback: str) -> dict | None:
    """An account is either token-mode (user_id+auth_token) or
    email-mode (email+password) — same two shapes qobuz.py's _write_config
    already accepts, just packaged per-slot here."""
    user_id    = (a.get("qobuz-user-id") or a.get("user_id") or "").strip()
    auth_token = (a.get("qobuz-auth-token") or a.get("auth_token") or "").strip()
    email      = (a.get("qobuz-email") or a.get("email") or "").strip()
    password   = (a.get("qobuz-password") or a.get("password") or "").strip()
    if not ((user_id and auth_token) or email):
        return None
    return {
        "qobuz-user-id": user_id, "qobuz-auth-token": auth_token,
        "qobuz-email": email, "qobuz-password": password,
        "label": a.get("label") or label_fallback,
    }


def health_rank(acct: dict) -> int:
    """Насколько учётка пригодна: 0 — лучшая, 3 — непригодная.

    Читаем только измеренное (`qobuz_accounts` кэширует ответ Qobuz), в сеть на
    этом пути не ходим: `acquire()` зовут во время загрузки.

    Зачем. 04.09.2026 замер по живым учёткам владельца: из восьми ТРИ отвечают
    401 и стоят сразу за основной, ещё у одной подписка истекла 15.08. Лимит
    попыток на задачу — 4 (`account_fallback.MAX_ACCOUNT_ATTEMPTS`), то есть
    отказ на слоте 0 съедал бы весь бюджет на мёртвых, ни разу не дойдя до
    четырёх рабочих. Ровно тот же дефект, что в тот же день закрыт у Deezer.

    Отдельная строка про «жив, но не eligible»: у Qobuz токен может быть
    ВАЛИДНЫМ при погасшей подписке — `credential.parameters` пуст, и streamrip
    падает с IneligibleError. Качать таким нельзя, поэтому ранг тот же, что у
    мёртвого; но снимать его нельзя тоже — подписку продлевают.
    """
    try:
        from ripster import qobuz_accounts as _qa
        from ripster import retired_credentials as _retired
        secret = _qa.account_secret(acct)
        if _retired.is_retired("qobuz_account", secret):
            return 3
        info = _qa.known(secret)
        if not info:
            return 2                      # не спрашивали — не судим
        if not info.get("alive"):
            return 2 if info.get("unreachable") else 3
        if not info.get("eligible"):
            return 3                      # подписки нет: streamrip не скачает
        return 0 if (info.get("hires") or info.get("lossless")) else 1
    except Exception:                     # noqa: BLE001
        return 2


def health_note(acct: dict) -> dict:
    """Почему слот пропускается — словами, для панели настроек.

    Без этого владелец видел в списке восемь учёток и не знал, что четырьмя из
    них скачать нельзя: перебор молча их обходит. Секрет наружу не отдаём.
    """
    rank = health_rank(acct)
    try:
        from ripster import qobuz_accounts as _qa
        info = _qa.known(_qa.account_secret(acct)) or {}
    except Exception:               # noqa: BLE001
        info = {}
    state = {0: "ok", 1: "lossy", 2: "unknown", 3: "unusable"}[rank]
    if rank == 3:
        why = info.get("reason") or "снята автоматикой"
    elif rank == 2:
        why = info.get("reason") or "ещё не проверялась"
    else:
        why = info.get("plan") or ""
    return {"health": state, "health_why": why, "usable": rank < 3}


def _configured_accounts(config: dict) -> list[dict]:
    """Primary account (slot 0) + any extras from `qobuz-accounts`."""
    accounts: list[dict] = []
    sources: list[dict] = []
    primary = _account_from_dict(config, "primary")
    if primary:
        accounts.append(_afb.stamp(primary, _afb.primary_src(config, "qobuz"), 0))
        sources.append(_afb.primary_src(config, "qobuz"))
    for a in (config.get("qobuz-accounts") or []):
        acct = _account_from_dict(a, f"account{len(accounts)+1}")
        if acct:
            accounts.append(_afb.stamp(acct, a, len(accounts)))
            sources.append(a)
    # Приоритет по здоровью — только там, где владелец не задал свой. Смотрим
    # ИСХОДНУЮ запись: `_afb.stamp` уже подставил номер слота вместо
    # отсутствующего priority, и «задал 0» от «подставили 0» уже не отличить.
    for i, (acc, src) in enumerate(zip(accounts, sources)):
        if (src or {}).get("priority") is None:
            acc["priority"] = float(health_rank(acc) * 100 + i)
    return accounts


def better_account_untried(tried: list, config: dict) -> bool:
    """Осталась ли непробованная учётка с бо́льшими правами (см.
    `account_fallback._better_account_untried`)."""
    accounts = _configured_accounts(config)
    if not accounts:
        return False
    done = set(tried or [])
    worst = max((health_rank(accounts[i]) for i in done if 0 <= i < len(accounts)),
                default=None)
    if worst is None:
        return False
    return any(health_rank(a) < worst for i, a in enumerate(accounts) if i not in done)


def pool_enabled(config: dict) -> bool:
    return len(_configured_accounts(config)) >= 2


class QobuzPool:
    def __init__(self, accounts: list[dict], base_dir: Path):
        self.accounts = accounts
        self.base_dir = base_dir
        self._busy = [False] * len(accounts)
        self._lock = threading.Lock()

    def acquire(self, exclude=()) -> tuple[int, dict, Path | None] | None:
        """Return (slot, account_dict, cfg_dir_override) for a free account,
        or None if every configured account is currently busy.

        `exclude` — слоты, которые уже пробовали в ЭТОЙ задаче и получили отказ
        по правам/региону. Без него повтор снова получал первую свободную учётку,
        то есть ту же самую, и перебор не двигался с места."""
        ex = set(exclude or ())
        with self._lock:
            order = _afb.order_indices(self.accounts)
            # Заведомо непригодные (401 или подписка погасла) пропускаем, пока
            # есть годные: каждая потраченная на них попытка — попытка, не
            # доставшаяся рабочей учётке. Если годных не осталось вовсе, берём
            # как есть — пусть отказ придёт от Qobuz, а не от нашей догадки.
            usable = [i for i in order if health_rank(self.accounts[i]) < 3]
            for i in (usable or order):
                if not self._busy[i] and i not in ex:
                    self._busy[i] = True
                    cfg_dir = None if i == 0 else (self.base_dir / f"acct{i}")
                    return i, self.accounts[i], cfg_dir

    def release(self, slot: int) -> None:
        with self._lock:
            if 0 <= slot < len(self._busy):
                self._busy[slot] = False

    def status(self) -> dict:
        with self._lock:
            return {
                "pool_enabled": True,
                "accounts": [
                    {"slot": i, "label": a["label"], "primary": i == 0, "busy": self._busy[i],
                     "enabled": a.get("enabled", True), "priority": a.get("priority", i),
                     "order": _afb.order_indices(self.accounts).index(i),
                     **health_note(a)}
                    for i, a in enumerate(self.accounts)
                ],
            }


_pool_instance: QobuzPool | None = None
_pool_accounts_fingerprint: tuple = ()


def _warm_health(accounts: list[dict], config: dict) -> None:
    """Опросить учётки фоном, чтобы порядку слотов было на что опираться.

    `health_rank` смотрит только на измеренное; при первом запуске измеренного
    нет, и перебор снова идёт «как в конфиге». Свой поток и свой цикл событий:
    `get_pool` зовут и из синхронного кода, и изнутри работающего цикла. Ошибки
    глушим намеренно — это подсказка для сортировки, а не операция, ради которой
    стоит ронять загрузку.
    """
    import threading

    def run() -> None:
        import asyncio
        from ripster import qobuz_accounts as _qa
        app_id = (config.get("qobuz-app-id") or "").strip()
        todo = [a for a in accounts if _qa.known(_qa.account_secret(a)) is None]
        if not todo:
            return
        loop = asyncio.new_event_loop()
        try:
            for a in todo:
                try:
                    loop.run_until_complete(_qa.account_info(a, app_id=app_id))
                except Exception:       # noqa: BLE001
                    pass
        finally:
            loop.close()

    threading.Thread(target=run, name="qobuz-health-warm", daemon=True).start()


def get_pool(config: dict) -> QobuzPool | None:
    global _pool_instance, _pool_accounts_fingerprint
    if not pool_enabled(config):
        return None
    accounts = _configured_accounts(config)
    fp = tuple((a["qobuz-user-id"], a["qobuz-auth-token"], a["qobuz-email"]) for a in accounts)
    if _pool_instance is None or fp != _pool_accounts_fingerprint:
        from pathlib import Path as _P
        base = _P(__file__).resolve().parent.parent / "dist" / "qobuz_pool"
        _pool_instance = QobuzPool(accounts, base)
        _pool_accounts_fingerprint = fp
        _warm_health(accounts, config)
    return _pool_instance


def live_status(config: dict) -> dict:
    p = get_pool(config)
    if p is None:
        accounts = _configured_accounts(config)
        return {"pool_enabled": False, "accounts": [
            {"slot": i, "label": a["label"], "primary": i == 0, "busy": False,
             "enabled": a.get("enabled", True), "priority": a.get("priority", i),
             "order": _afb.order_indices(accounts).index(i),
             **health_note(a)}
            for i, a in enumerate(accounts)
        ]}
    return p.status()
