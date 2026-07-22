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
from pathlib import Path


def _configured_accounts(config: dict) -> list[dict]:
    """Primary account (slot 0) + any extras from `deezer-accounts`."""
    primary_arl = (config.get("deezer-arl") or "").strip()
    accounts: list[dict] = []
    if primary_arl:
        accounts.append({"arl": primary_arl, "label": config.get("deezer-arl-label", "primary")})
    for a in (config.get("deezer-accounts") or []):
        arl = (a.get("arl") or "").strip()
        if arl:
            accounts.append({"arl": arl, "label": a.get("label") or f"account{len(accounts)+1}"})
    return accounts


def pool_enabled(config: dict) -> bool:
    return len(_configured_accounts(config)) >= 2


class DeezerPool:
    def __init__(self, accounts: list[dict], base_dir: Path):
        self.accounts = accounts
        self.base_dir = base_dir
        self._busy = [False] * len(accounts)
        self._lock = threading.Lock()

    def acquire(self) -> tuple[int, str, Path | None] | None:
        """Return (slot, arl, cfg_dir_override) for a free account, or None if
        every configured account is currently busy (caller falls back to
        waiting in the normal queue lane, same as before the pool existed)."""
        with self._lock:
            for i, busy in enumerate(self._busy):
                if not busy:
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
                    {"slot": i, "label": a["label"], "primary": i == 0, "busy": self._busy[i]}
                    for i, a in enumerate(self.accounts)
                ],
            }


_pool_instance: DeezerPool | None = None
_pool_accounts_fingerprint: tuple = ()


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
    return _pool_instance


def live_status(config: dict) -> dict:
    p = get_pool(config)
    if p is None:
        accounts = _configured_accounts(config)
        return {"pool_enabled": False, "accounts": [
            {"slot": i, "label": a["label"], "primary": i == 0, "busy": False}
            for i, a in enumerate(accounts)
        ]}
    return p.status()
