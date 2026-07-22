"""
Yandex Music multi-account load-balancing pool.

Same shape as soundcloud_pool.py — ymd takes the token as a plain `--token`
CLI argument (ripster/engines/yandex.py), no shared file, so a pool slot is
just "which token to substitute into this task's config view".
"""
from __future__ import annotations

import threading


def _configured_accounts(config: dict) -> list[dict]:
    """Primary account (slot 0) + any extras from `yandex-accounts`."""
    accounts: list[dict] = []
    primary = (config.get("yandex-token") or "").strip()
    if primary:
        accounts.append({"token": primary, "label": "primary"})
    for a in (config.get("yandex-accounts") or []):
        tok = (a.get("token") or "").strip()
        if tok:
            accounts.append({"token": tok, "label": a.get("label") or f"account{len(accounts)+1}"})
    return accounts


def pool_enabled(config: dict) -> bool:
    return len(_configured_accounts(config)) >= 2


class YandexPool:
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


_pool_instance: YandexPool | None = None
_pool_fingerprint: tuple = ()


def get_pool(config: dict) -> YandexPool | None:
    global _pool_instance, _pool_fingerprint
    if not pool_enabled(config):
        return None
    accounts = _configured_accounts(config)
    fp = tuple(a["token"] for a in accounts)
    if _pool_instance is None or fp != _pool_fingerprint:
        _pool_instance = YandexPool(accounts)
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
