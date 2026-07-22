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


def _configured_accounts(config: dict) -> list[dict]:
    """Primary account (slot 0) + any extras from `soundcloud-accounts`."""
    accounts: list[dict] = []
    primary = (config.get("soundcloud-oauth-token") or "").strip()
    if primary:
        accounts.append({"token": primary, "label": "primary"})
    for a in (config.get("soundcloud-accounts") or []):
        tok = (a.get("token") or "").strip()
        if tok:
            accounts.append({"token": tok, "label": a.get("label") or f"account{len(accounts)+1}"})
    return accounts


def pool_enabled(config: dict) -> bool:
    return len(_configured_accounts(config)) >= 2


class SoundcloudPool:
    def __init__(self, accounts: list[dict]):
        self.accounts = accounts
        self._busy = [False] * len(accounts)
        self._lock = threading.Lock()

    def acquire(self) -> tuple[int, str] | None:
        with self._lock:
            for i, busy in enumerate(self._busy):
                if not busy:
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
                    {"slot": i, "label": a["label"], "primary": i == 0, "busy": self._busy[i]}
                    for i, a in enumerate(self.accounts)
                ],
            }


_pool_instance: SoundcloudPool | None = None
_pool_fingerprint: tuple = ()


def get_pool(config: dict) -> SoundcloudPool | None:
    global _pool_instance, _pool_fingerprint
    if not pool_enabled(config):
        return None
    accounts = _configured_accounts(config)
    fp = tuple(a["token"] for a in accounts)
    if _pool_instance is None or fp != _pool_fingerprint:
        _pool_instance = SoundcloudPool(accounts)
        _pool_fingerprint = fp
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
