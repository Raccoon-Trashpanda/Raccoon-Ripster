"""Автохил основного Apple-враппера (slot 0): откат на последнюю РАБОЧУЮ учётку.

Проблема (14.10.2026): владелец внедряет новую учётку в
`wrapper-apple-id`/`wrapper-password` и перезапускает вход. Apple её не
принимает («Check the account information you entered and try again» →
`[!] login failed`), а прежняя РАБОЧАЯ пара уже затёрта в конфиге — загрузки
Apple умирают у ВСЕХ гостей до ручного вмешательства.

Решение: пока враппер подтверждённо вошёл (записан `adi.pb` — Apple принял
логин), запоминаем текущую пару как «последнюю рабочую» в
`tokens/apple_last_good.yaml` (секрет: папка `tokens/` гитигнорится, гостям
наружу не отдаётся). Сессия каждой учётки и так лежит в своей папке
`rootfs_id/<login>/` (см. `amd._rootfs_data`), поэтому откат — это вернуть
id/пароль в конфиг и поднять враппер штатно: он переиспользует СОХРАНЁННУЮ
сессию той учётки, без 2FA.

Модуль чистый: читает/пишет файл снимка и мутирует cfg. Docker и перезапуск
враппера драйвит `ripster/amd.py`, который сюда и зовёт.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import yaml

_FILENAME = "apple_last_good.yaml"


def _path(base_dir: Any) -> Path:
    return Path(base_dir) / "tokens" / _FILENAME


def _slot0(cfg: Any) -> tuple[str, str]:
    """(apple_id, password) основного слота, обрезанные. Пустые — слот не задан."""
    return (str(cfg.get("wrapper-apple-id") or "").strip(),
            str(cfg.get("wrapper-password") or "").strip())


def _same(aid: str, pwd: str, snap: dict) -> bool:
    return (aid.lower() == str(snap.get("id") or "").lower()
            and pwd == str(snap.get("password") or ""))


def last_good(base_dir: Any) -> "dict | None":
    """Снимок последней рабочей пары, либо None. Битый/пустой файл — как None."""
    p = _path(base_dir)
    try:
        if not p.exists():
            return None
        d = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        return None
    if isinstance(d, dict) and d.get("id") and d.get("password"):
        return d
    return None


def remember_good(cfg: Any, base_dir: Any) -> bool:
    """Снять текущую slot-0 пару как «последнюю рабочую».

    Звать ТОЛЬКО когда враппер подтверждённо вошёл: неудачный логин `adi.pb` не
    пишет (контейнер сносится), поэтому вызывающий гейтит это на `_has_saved_session()`.
    Пишем только при изменении — лишних записей секрета на диск не делаем.
    """
    aid, pwd = _slot0(cfg)
    if not (aid and pwd):
        return False
    prev = last_good(base_dir)
    if prev and _same(aid, pwd, prev):
        return False
    p = _path(base_dir)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(
            yaml.safe_dump({"id": aid, "password": pwd,
                            "at": time.strftime("%Y-%m-%dT%H:%M:%S")},
                           allow_unicode=True, sort_keys=False),
            encoding="utf-8")
        tmp.replace(p)
        return True
    except Exception:  # noqa: BLE001
        return False


def should_rollback(cfg: Any, base_dir: Any) -> bool:
    """Есть рабочий снимок И текущая slot-0 пара ОТ НЕГО отличается — значит в
    слоте 0 стоит другая (только что упавшая) учётка, на которую есть куда
    откатиться. Та же пара в снимке и в конфиге → откатываться некуда."""
    snap = last_good(base_dir)
    if not snap:
        return False
    aid, pwd = _slot0(cfg)
    return not _same(aid, pwd, snap)


def rollback(cfg: Any, base_dir: Any, save_config_fn: Any) -> dict:
    """Вернуть slot-0 к последней рабочей паре и сохранить конфиг.

    {"ok": True, "restored": <маска id>} — откатились;
    {"ok": False, "reason": ...} — нечего (нет снимка / та же пара) или не сохранили.
    Враппер НЕ трогает: перезапуск — на вызывающем (amd)."""
    snap = last_good(base_dir)
    if not snap:
        return {"ok": False, "reason": "no_snapshot"}
    aid, pwd = _slot0(cfg)
    if _same(aid, pwd, snap):
        return {"ok": False, "reason": "already_good"}
    cfg["wrapper-apple-id"] = snap["id"]
    cfg["wrapper-password"] = snap["password"]
    if save_config_fn is not None:
        try:
            save_config_fn(cfg)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "reason": f"save_failed: {str(e)[:120]}"}
    return {"ok": True, "restored": mask_id(snap["id"]),
            "replaced": mask_id(aid), "at": snap.get("at", "")}


def mask_id(value: Any) -> str:
    """Apple ID наружу только маской: три буквы логина + домен."""
    s = str(value or "").strip()
    if not s:
        return "—"
    if "@" in s:
        local, _, dom = s.partition("@")
        return f"{local[:3]}…@{dom}"
    return f"{s[:3]}…"
