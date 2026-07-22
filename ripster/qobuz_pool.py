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


def _configured_accounts(config: dict) -> list[dict]:
    """Primary account (slot 0) + any extras from `qobuz-accounts`."""
    accounts: list[dict] = []
    primary = _account_from_dict(config, "primary")
    if primary:
        accounts.append(primary)
    for a in (config.get("qobuz-accounts") or []):
        acct = _account_from_dict(a, f"account{len(accounts)+1}")
        if acct:
            accounts.append(acct)
    return accounts


def pool_enabled(config: dict) -> bool:
    return len(_configured_accounts(config)) >= 2


class QobuzPool:
    def __init__(self, accounts: list[dict], base_dir: Path):
        self.accounts = accounts
        self.base_dir = base_dir
        self._busy = [False] * len(accounts)
        self._lock = threading.Lock()

    def acquire(self) -> tuple[int, dict, Path | None] | None:
        """Return (slot, account_dict, cfg_dir_override) for a free account,
        or None if every configured account is currently busy."""
        with self._lock:
            for i, busy in enumerate(self._busy):
                if not busy:
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
                    {"slot": i, "label": a["label"], "primary": i == 0, "busy": self._busy[i]}
                    for i, a in enumerate(self.accounts)
                ],
            }


_pool_instance: QobuzPool | None = None
_pool_accounts_fingerprint: tuple = ()


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
