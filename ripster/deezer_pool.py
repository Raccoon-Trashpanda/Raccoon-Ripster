"""
Deezer multi-account load-balancing pool.

Unlike Apple's wrapper_pool.py, there's no Docker container per account here —
deemix is a plain CLI that reads its ARL token from a config-directory file
(``%APPDATA%\\deemix\\.arl`` on Windows). To run two ARLs concurrently without
one download's subprocess clobbering the other's ARL file mid-run, each pool
slot gets its OWN deemix config directory (via an APPDATA/XDG_CONFIG_HOME
override passed as subprocess env — see ``ripster/runner.py``'s deezer
dispatch block and ``ripster/engines/deezer.py``'s ``_deemix_config_dir()``).

Slot 0 always uses the primary ``deezer-arl`` config key and deemix's own
DEFAULT config dir (unaffected — no override needed, so a single-account
setup behaves exactly as before this pool existed). Slots 1+ come from the
``deezer-accounts`` config list and get an isolated config dir under
``dist/deezer_pool/acct{i}/``.
"""
from __future__ import annotations

import threading

from ripster import account_fallback as _afb
from pathlib import Path


def health_rank(arl: str) -> int:
    """Насколько учётка пригодна: 0 — лучшая, 3 — отвергнутая.

    Читаем ТОЛЬКО то, что уже измерено (`deezer_accounts` кэширует ответ
    Deezer), сети здесь не касаемся: `acquire()` зовут на горячем пути.

    Зачем: 04.09.2026 гостю не отдали релиз, потому что перебор учёток потратил
    все попытки на слоты 0–2 (бесплатная и две отвергнутые) и не добрался до
    двух Deezer Family с lossless, стоявших последними. Порядок «как в конфиге»
    имеет смысл, только пока про учётки ничего не известно; когда известно —
    первым должен идти тот, кто действительно может скачать.
    """
    try:
        from ripster import deezer_accounts as _da
        from ripster import retired_credentials as _retired
        if _retired.is_retired("deezer_arl", arl):
            return 3        # уже снята сторожем — в маршрутизацию не возвращаем
        info = _da.known(arl)
        if not info:
            return 2                       # не спрашивали — не судим
        if not info.get("alive"):
            if info.get("unreachable"):
                return 2                   # не достучались — про учётку неизвестно ничего
            return 3                       # отвергнут: тратить на него попытку незачем
        return 0 if info.get("lossless") else 1
    except Exception:                       # noqa: BLE001
        return 2


def health_note(arl: str) -> dict:
    """Почему слот пропускается — словами, для панели настроек. Секрет наружу
    не отдаём: только состояние и причина."""
    rank = health_rank(arl)
    try:
        from ripster import deezer_accounts as _da
        info = _da.known(arl) or {}
    except Exception:               # noqa: BLE001
        info = {}
    state = {0: "ok", 1: "lossy", 2: "unknown", 3: "unusable"}[rank]
    if rank == 3:
        why = info.get("reason") or "снят автоматикой"
    elif rank == 2:
        why = info.get("reason") or "ещё не проверялся"
    else:
        why = info.get("plan") or ""
    return {"health": state, "health_why": why, "usable": rank < 3}


def better_account_untried(tried: list, config: dict) -> bool:
    """Осталась ли учётка, которая умеет больше уже пробованных.

    Deezer отвечает «can't stream the track at the desired bitrate» и когда у
    релиза правда нет FLAC, и когда прав нет у КОНКРЕТНОЙ учётки. Различить
    можно только по составу пула: если пробовали бесплатную, а рядом лежит
    непробованная lossless — отказ был про учётку, и перебор осмыслен.

    Спрашивает `ripster.account_fallback` (см. `_better_account_untried`).
    """
    accounts = _configured_accounts(config)
    if not accounts:
        return False
    done = set(tried or [])
    worst_tried = max((health_rank(accounts[i]["arl"])
                       for i in done if 0 <= i < len(accounts)), default=None)
    if worst_tried is None:
        return False
    return any(health_rank(a["arl"]) < worst_tried
               for i, a in enumerate(accounts) if i not in done)


def _configured_accounts(config: dict) -> list[dict]:
    """Primary account (slot 0) + any extras from `deezer-accounts`."""
    primary_arl = (config.get("deezer-arl") or "").strip()
    accounts: list[dict] = []
    sources: list[dict] = []
    if primary_arl:
        accounts.append(_afb.stamp(
            {"arl": primary_arl, "label": config.get("deezer-arl-label", "primary")},
            config, 0))
        sources.append(config)
    for a in (config.get("deezer-accounts") or []):
        arl = (a.get("arl") or "").strip()
        if arl:
            accounts.append(_afb.stamp(
                {"arl": arl, "label": a.get("label") or f"account{len(accounts)+1}"},
                a, len(accounts)))
            sources.append(a)
    # Приоритет по здоровью — но ТОЛЬКО там, где владелец не задал свой.
    # Спрашиваем ИСХОДНУЮ запись конфига, а не проштампованную: `_afb.stamp`
    # уже подставил номер слота вместо отсутствующего priority, и по результату
    # «владелец задал 0» от «мы подставили 0» не отличить.
    for i, (acc, src) in enumerate(zip(accounts, sources)):
        if (src or {}).get("priority") is None:
            acc["priority"] = float(health_rank(acc["arl"]) * 100 + i)
    return accounts


def pool_enabled(config: dict) -> bool:
    return len(_configured_accounts(config)) >= 2


class DeezerPool:
    def __init__(self, accounts: list[dict], base_dir: Path):
        self.accounts = accounts
        self.base_dir = base_dir
        self._busy = [False] * len(accounts)
        self._lock = threading.Lock()

    def acquire(self, exclude=()) -> tuple[int, str, Path | None] | None:
        """Return (slot, arl, cfg_dir_override) for a free account, or None if
        every configured account is currently busy (caller falls back to
        waiting in the normal queue lane, same as before the pool existed).

        `exclude` — слоты, уже отказавшие в этой задаче по правам/региону
        (ripster/account_fallback.py)."""
        ex = set(exclude or ())
        with self._lock:
            order = _afb.order_indices(self.accounts)
            # Заведомо отвергнутые пропускаем ПОЛНОСТЬЮ, пока есть живые: каждая
            # попытка на мёртвой учётке — это попытка, не доставшаяся рабочей
            # (предел попыток на задачу конечен). Если живых не осталось вовсе,
            # берём как есть: пусть отказ придёт от Deezer, а не от нашей
            # догадки.
            usable = [i for i in order if health_rank(self.accounts[i]["arl"]) < 3]
            for i in (usable or order):
                if not self._busy[i] and i not in ex:
                    self._busy[i] = True
                    arl = self.accounts[i]["arl"]
                    # Slot 0 = primary = deemix's own default config dir (no
                    # override — single-account installs are byte-for-byte
                    # unchanged from before this pool existed).
                    cfg_dir = None if i == 0 else (self.base_dir / f"acct{i}")
                    return i, arl, cfg_dir

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
                     **health_note(a["arl"])}
                    for i, a in enumerate(self.accounts)
                ],
            }


_pool_instance: DeezerPool | None = None
_pool_accounts_fingerprint: tuple = ()


def _warm_health(arls: tuple) -> None:
    """Опросить учётки фоном, чтобы порядок слотов было чем обосновать.

    `health_rank` смотрит только на измеренное; при первом запуске (или после
    смены списка) измеренного нет, и перебор снова идёт «как в конфиге». Один
    фоновый проход это чинит.

    Свой поток и свой цикл событий — get_pool зовут и из синхронного кода, и
    изнутри работающего цикла; ошибки глушим намеренно: это подсказка для
    сортировки, а не операция, ради которой стоит ронять загрузку.
    """
    import threading

    def run() -> None:
        import asyncio
        from ripster import deezer_accounts as _da
        unknown = [a for a in arls if _da.known(a) is None]
        if not unknown:
            return
        loop = asyncio.new_event_loop()
        try:
            for a in unknown:
                try:
                    loop.run_until_complete(_da.arl_info(a))
                except Exception:       # noqa: BLE001
                    pass
        finally:
            loop.close()

    threading.Thread(target=run, name="deezer-health-warm", daemon=True).start()


def get_pool(config: dict) -> DeezerPool | None:
    """Singleton, rebuilt only when the configured account list actually
    changes (so acquire()'d busy-state survives across calls within a run)."""
    global _pool_instance, _pool_accounts_fingerprint
    if not pool_enabled(config):
        return None
    accounts = _configured_accounts(config)
    fp = tuple(a["arl"] for a in accounts)
    if _pool_instance is None or fp != _pool_accounts_fingerprint:
        from pathlib import Path as _P
        base = _P(__file__).resolve().parent.parent / "dist" / "deezer_pool"
        _pool_instance = DeezerPool(accounts, base)
        _pool_accounts_fingerprint = fp
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
             **health_note(a["arl"])}
            for i, a in enumerate(accounts)
        ]}
    return p.status()
