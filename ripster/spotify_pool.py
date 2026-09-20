"""
Spotify multi-account pool — КОРИДОРЫ (изоляция на учётку), как у Deezer.

Каждая учётка получает свой «коридор»: отдельный каталог с собственным
``config/`` внутри. Движок запускается с ``cwd`` = коридор и переменной
``ORPHEUS_CONFIG_DIR`` = ``<коридор>/config``, поэтому у учётки СВОИ
``settings.json``, ``loginstorage.bin`` и ``.librespot_cache/reusable_credentials.json``.
Учётки не мешают друг другу и могут качать ПАРАЛЛЕЛЬНО — ни подмены живого
blob'а, ни глобального лока больше нет.

Почему понадобился патч вендоренного OrpheusDL. ``orpheus/orpheus/core.py``
берёт ``data_folder_base = 'config'`` (относительно CWD) — это уже следовало бы
за коридором. А вот модуль Spotify резолвил blob ОТНОСИТЕЛЬНО ФАЙЛА МОДУЛЯ
(``spotify_api._get_spotify_credentials_dir`` и ``spotify_embed_api``), и blob
всегда падал в ``orpheus/config`` мимо CWD. Поэтому в оба места добавлен
оверрайд ``ORPHEUS_CONFIG_DIR`` (см. комментарии «ПАТЧ RIPSTER» там же) — без
него мультиаккаунт молча ходил бы под ОДНОЙ учёткой.

Слот 0 — «основной»: это живой blob в ``orpheus/config``, установка с одним
аккаунтом работает ровно как раньше (коридор ей не нужен).

Здоровье слотов читаем только из уже измеренного кэша — сети модуль не касается.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from ripster import account_fallback as _afb


def _base_dir() -> Path:
    return Path(__file__).resolve().parent.parent


def main_config_dir() -> Path:
    """Конфиг OrpheusDL по умолчанию — он же коридор слота 0."""
    return _base_dir() / "orpheus" / "config"


def live_blob() -> Path:
    return main_config_dir() / ".librespot_cache" / "reusable_credentials.json"


def corridor_dir(i: int) -> Path:
    """Каталог коридора учётки (CWD движка)."""
    return _base_dir() / "dist" / "spotify_corridors" / f"acct{i}"


def corridor_config(i: int) -> Path:
    return corridor_dir(i) / "config"


def corridor_blob(i: int) -> Path:
    return corridor_config(i) / ".librespot_cache" / "reusable_credentials.json"


def ensure_corridor(i: int) -> Path:
    """Создать коридор и положить в него общие настройки, если их там нет.

    ``settings.json`` копируем из основного конфига: качество и прочие
    предпочтения одни на все учётки, и заводить их руками в каждом коридоре —
    лишняя работа и источник расхождений. Секреты НЕ копируем: blob у коридора
    свой, он и определяет учётку.
    """
    cfg = corridor_config(i)
    (cfg / ".librespot_cache").mkdir(parents=True, exist_ok=True)
    src = main_config_dir() / "settings.json"
    dst = cfg / "settings.json"
    try:
        if src.is_file() and (not dst.is_file()
                              or src.stat().st_mtime > dst.stat().st_mtime):
            shutil.copy2(src, dst)
    except Exception:                                    # noqa: BLE001
        pass          # без settings.json OrpheusDL создаст свой — не фатально
    return cfg


def health_rank(label: str) -> int:
    """0 — здоров/неизвестно, больше — хуже. Только измеренный кэш, без сети."""
    try:
        from ripster import spotify_accounts as _sa          # появится позже
    except Exception:                                        # noqa: BLE001
        return 0
    try:
        info = _sa.known(label) or {}
    except Exception:                                        # noqa: BLE001
        return 0
    if info.get("alive") is False:
        return 2
    if info.get("alive") is True:
        return 0
    return 1


def _configured_accounts(config: dict) -> list[dict]:
    """Слот 0 (основной конфиг) + слоты из ``spotify-accounts`` (коридоры).

    Запись в конфиге: ``{"label": "...", "enabled": true, "priority": 0}``.
    Сам blob в конфиге НЕ хранится (секрет и бинарь) — он лежит файлом в
    коридоре. Слот считается настроенным, только если его blob реально есть на
    диске: запись без файла — это обещание, а не учётка.
    """
    accounts: list[dict] = []
    sources: list[dict] = []

    if live_blob().exists():
        accounts.append(_afb.stamp(
            {"slot": 0, "label": (config.get("spotify-account-label") or "primary"),
             "cwd": _base_dir() / "orpheus", "config_dir": main_config_dir()},
            config, 0))
        sources.append(config)

    for a in (config.get("spotify-accounts") or []):
        i = len(accounts)
        if not corridor_blob(i).exists():
            continue
        accounts.append(_afb.stamp(
            {"slot": i, "label": a.get("label") or f"account{i}",
             "cwd": corridor_dir(i), "config_dir": corridor_config(i)},
            a, i))
        sources.append(a)

    # Приоритет по здоровью — только там, где владелец не задал свой (тот же
    # комментарий, что в deezer_pool: проштампованный priority от заданного не
    # отличить, поэтому спрашиваем ИСХОДНУЮ запись конфига).
    for i, (acc, src) in enumerate(zip(accounts, sources)):
        if (src or {}).get("priority") is None:
            acc["priority"] = float(health_rank(acc["label"]) * 100 + i)
    return accounts


def pool_enabled(config: dict) -> bool:
    return len(_configured_accounts(config)) >= 2


def pick(config: dict, exclude=()) -> dict | None:
    """Лучший слот (меньше priority — раньше), кроме `exclude`.

    `exclude` — слоты, уже отказавшие в ЭТОЙ задаче (права/регион/429), см.
    ripster/account_fallback.py.
    """
    ex = set(exclude or ())
    cands = [a for a in _configured_accounts(config)
             if a.get("enabled", True) and a["slot"] not in ex]
    if not cands:
        return None
    cands.sort(key=lambda a: (a.get("priority", a["slot"]), a["slot"]))
    return cands[0]


def acquire(config: dict, exclude=()) -> dict | None:
    """Выбрать коридор под прогон. Лока НЕТ — коридоры изолированы.

    Возвращает запись слота с готовыми `cwd` и `config_dir`; раннер передаёт их
    подпроцессу (cwd + env ORPHEUS_CONFIG_DIR). Освобождать нечего — release()
    оставлен только для симметрии вызова.
    """
    acct = pick(config, exclude)
    if acct is None:
        return None
    if acct["slot"] != 0:
        try:
            ensure_corridor(acct["slot"])
        except Exception:                                    # noqa: BLE001
            return None
    return acct


def release() -> None:
    """Ничего не держим (коридоры изолированы) — оставлено для симметрии."""
    return None
