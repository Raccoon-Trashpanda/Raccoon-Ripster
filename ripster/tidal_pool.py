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
    import sys

    return Path(sys.argv[0]).resolve().parent if sys.argv else Path(".").resolve()


def orpheus_dir() -> Path:
    return _base_dir() / "orpheus"


def slot_dir(slot: int) -> Path:
    """Рабочий каталог слота. Ноль — сама установка OrpheusDL."""
    if slot <= 0:
        return orpheus_dir()
    return _base_dir() / "dist" / "tidal_pool" / f"acct{slot}"


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
    # Настройки копируем ОДИН раз: дальше у слота своя жизнь (качество, папка
    # сохранения правятся движком под каждую задачу), а сессия у него своя по
    # определению — ради неё всё и затевалось.
    src, dst = orpheus_dir() / "config" / "settings.json", d / "config" / "settings.json"
    if src.is_file() and not dst.is_file():
        dst.write_bytes(src.read_bytes())
    return d


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

    Читаем только измеренное; в сеть здесь не ходим — `acquire()` зовут во
    время загрузки. Пока измерений нет, все учётки равны (ранг 2 — «не
    спрашивали»), и это честнее, чем выдумывать порядок.
    """
    try:
        from ripster import retired_credentials as _retired

        ident = acct.get("tidal-refresh") or acct.get("tidal-email") or ""
        if ident and _retired.is_retired("tidal_account", ident):
            return 3
    except Exception:  # noqa: BLE001
        pass
    return 2


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
            acc["priority"] = float(health_rank(acc) * 100 + i)
    return accounts


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
                        "order": order.index(i),
                    }
                    for i, a in enumerate(self.accounts)
                ],
            }
