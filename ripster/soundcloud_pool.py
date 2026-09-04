"""
SoundCloud multi-account load-balancing pool.

Simpler than Deezer/Qobuz's pools: the `lucida` runner takes the OAuth token
as a plain `--oauth-token=` CLI argument (ripster/engines/soundcloud.py), not
a file read from a fixed shared path — so there's no config-dir isolation to
build at all. A pool slot is just "which token to substitute into this
task's config view", nothing more.
"""
from __future__ import annotations

import threading

from ripster import account_fallback as _afb


def health_rank(token: str) -> int:
    """Насколько учётка пригодна: 0 — лучшая, 3 — непригодная.

    Читаем только измеренное (`soundcloud_accounts` кэширует ответ `/me`), в
    сеть на этом пути не ходим: `acquire()` зовут во время загрузки.

    Отличие SoundCloud от Deezer и Qobuz: учётка БЕЗ Go+ всё равно качает,
    просто в 128 kbps вместо AAC 256. Это хуже, но это не отказ — поэтому ранг
    1, а не 3, и такая учётка из перебора не выбрасывается. Ранг 3 остаётся за
    тем, что действительно не работает: отвергнутый токен.
    """
    try:
        from ripster import soundcloud_accounts as _sa
        from ripster import retired_credentials as _retired
        if _retired.is_retired("soundcloud_token", token):
            return 3
        info = _sa.known(token)
        if not info:
            return 2                      # не спрашивали — не судим
        if not info.get("alive"):
            return 2 if info.get("unreachable") else 3
        return 0 if info.get("go_plus") else 1
    except Exception:                     # noqa: BLE001
        return 2


def health_note(token: str) -> dict:
    """Что со слотом — словами, для панели настроек. Секрет наружу не отдаём.

    `account` — логин учётки: у владельца 04.09.2026 два РАЗНЫХ токена
    принадлежали одному аккаунту (`goku`), то есть пул из трёх записей давал
    две независимые учётки. По одному списку токенов этого не видно, а на
    параллельность и перебор влияет напрямую.
    """
    rank = health_rank(token)
    try:
        from ripster import soundcloud_accounts as _sa
        info = _sa.known(token) or {}
    except Exception:                     # noqa: BLE001
        info = {}
    state = {0: "ok", 1: "lossy", 2: "unknown", 3: "unusable"}[rank]
    why = info.get("reason") or info.get("plan") or ("ещё не проверялся" if rank == 2 else "")
    return {"health": state, "health_why": why, "usable": rank < 3,
            "account": info.get("login") or "", "plan": info.get("plan") or ""}


def better_account_untried(tried: list, config: dict) -> bool:
    """Осталась ли непробованная учётка с бо́льшими правами (см.
    `account_fallback._better_account_untried`). Для SoundCloud это «Go+ против
    бесплатной»: отказ по качеству на бесплатной осмысленно повторить на Go+."""
    accounts = _configured_accounts(config)
    if not accounts:
        return False
    done = set(tried or [])
    worst = max((health_rank(accounts[i]["token"]) for i in done if 0 <= i < len(accounts)),
                default=None)
    if worst is None:
        return False
    return any(health_rank(a["token"]) < worst
               for i, a in enumerate(accounts) if i not in done)


def _configured_accounts(config: dict) -> list[dict]:
    """Primary account (slot 0) + any extras from `soundcloud-accounts`."""
    accounts: list[dict] = []
    sources: list[dict] = []
    primary = (config.get("soundcloud-oauth-token") or "").strip()
    if primary:
        accounts.append(_afb.stamp({"token": primary, "label": "primary"}, _afb.primary_src(config, "soundcloud"), 0))
        sources.append(_afb.primary_src(config, "soundcloud"))
    for a in (config.get("soundcloud-accounts") or []):
        tok = (a.get("token") or "").strip()
        if tok:
            accounts.append(_afb.stamp(
                {"token": tok, "label": a.get("label") or f"account{len(accounts)+1}"},
                a, len(accounts)))
            sources.append(a)
    # Приоритет по здоровью — только там, где владелец не задал свой. Смотрим
    # ИСХОДНУЮ запись: `_afb.stamp` уже подставил номер слота вместо
    # отсутствующего priority, и «задал 0» от «подставили 0» не отличить.
    for i, (acc, src) in enumerate(zip(accounts, sources)):
        if (src or {}).get("priority") is None:
            acc["priority"] = float(health_rank(acc["token"]) * 100 + i)
    return accounts


def pool_enabled(config: dict) -> bool:
    return len(_configured_accounts(config)) >= 2


class SoundcloudPool:
    def __init__(self, accounts: list[dict]):
        self.accounts = accounts
        self._busy = [False] * len(accounts)
        self._lock = threading.Lock()

    def acquire(self, exclude=()) -> tuple[int, str] | None:
        """`exclude` — слоты, уже отказавшие в этой задаче по правам/региону
        (ripster/account_fallback.py); без него перебор топчется на первом."""
        ex = set(exclude or ())
        with self._lock:
            order = _afb.order_indices(self.accounts)
            # Отвергнутые токены пропускаем, пока есть годные: попытка на
            # мёртвом — это попытка, не доставшаяся рабочему. Если годных нет
            # вовсе, берём как есть — пусть откажет SoundCloud, а не наша
            # догадка. Бесплатные учётки здесь НЕ пропускаются: 128 kbps хуже
            # AAC 256, но это рабочая загрузка.
            usable = [i for i in order if health_rank(self.accounts[i]["token"]) < 3]
            for i in (usable or order):
                if not self._busy[i] and i not in ex:
                    self._busy[i] = True
                    return i, self.accounts[i]["token"]

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
                     **health_note(a["token"])}
                    for i, a in enumerate(self.accounts)
                ],
            }


_pool_instance: SoundcloudPool | None = None
_pool_fingerprint: tuple = ()


def _warm_health(tokens: tuple) -> None:
    """Опросить учётки фоном, чтобы порядку слотов было на что опираться.

    `health_rank` смотрит только на измеренное; при первом запуске измеренного
    нет. Свой поток и свой цикл событий: `get_pool` зовут и из синхронного
    кода, и изнутри работающего цикла. Ошибки глушим намеренно — это подсказка
    для сортировки, а не операция, ради которой стоит ронять загрузку.
    """
    import threading

    def run() -> None:
        import asyncio
        from ripster import soundcloud_accounts as _sa
        todo = [t for t in tokens if _sa.known(t) is None]
        if not todo:
            return
        loop = asyncio.new_event_loop()
        try:
            for t in todo:
                try:
                    loop.run_until_complete(_sa.account_info(t))
                except Exception:       # noqa: BLE001
                    pass
        finally:
            loop.close()

    threading.Thread(target=run, name="soundcloud-health-warm", daemon=True).start()


def get_pool(config: dict) -> SoundcloudPool | None:
    global _pool_instance, _pool_fingerprint
    if not pool_enabled(config):
        return None
    accounts = _configured_accounts(config)
    fp = tuple(a["token"] for a in accounts)
    if _pool_instance is None or fp != _pool_fingerprint:
        _pool_instance = SoundcloudPool(accounts)
        _pool_fingerprint = fp
        _warm_health(fp)
    return _pool_instance


def live_status(config: dict) -> dict:
    p = get_pool(config)
    if p is None:
        accounts = _configured_accounts(config)
        return {"pool_enabled": False, "accounts": [
            {"slot": i, "label": a["label"], "primary": i == 0, "busy": False,
             "enabled": a.get("enabled", True), "priority": a.get("priority", i),
             "order": _afb.order_indices(accounts).index(i),
             **health_note(a["token"])}
            for i, a in enumerate(accounts)
        ]}
    return p.status()
