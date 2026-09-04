"""Реестр учёток, снятых автоматикой. Чтобы снятое не возвращалось.

Зачем понадобился. 03.09.2026 сторож здоровья честно отработал: ARL
`...947f15` три прохода подряд получил от Deezer «отвергнут», ушёл строкой в
DEAD_ACCOUNTS.txt, и `credential_health.disable_deezer_arl` вычистил его из
config.yaml. Назавтра он снова лежал в конфиге и снова копил streak.

Никто его не возвращал руками. Возвращает архитектура: сторож живёт в ОТДЕЛЬНОМ
процессе (tools/ripster_healthcheck.py), а работающее приложение держит конфиг в
памяти и при любом сохранении пишет `config.yaml` ЦЕЛИКОМ из своей копии
(config_service.save_config). Копия в памяти про внешнюю правку не знает — и
первое же сохранение настроек воскрешает мёртвую учётку. То есть авто-снятие
было не сломано, а обратимо чужой рукой; результата это не меняло.

Поэтому решение — не «править файл аккуратнее», а вынести факт снятия туда, где
его видят ОБА процесса, и сверяться с реестром на загрузке и на сохранении.
Файл-реестр отдельный: config.yaml переписывается целиком, и хранить в нём
защиту от переписывания config.yaml было бы кольцом.

🔒 Секрет в реестр не пишется — только sha256-хвост (сопоставить можно, достать
нельзя). Ровно то же правило, что в deezer_accounts: секреты утекают через
кэши и бэкапы.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

_KIND_DEEZER = "deezer_arl"
_KIND_QOBUZ = "qobuz_account"
_KIND_SC = "soundcloud_token"


def _base_dir() -> Path:
    return Path(os.environ.get("RIPSTER_BASE_DIR")
                or Path(__file__).resolve().parent.parent)


def _path() -> Path:
    p = _base_dir() / "dist" / "retired_credentials.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _digest(secret: str) -> str:
    return hashlib.sha256((secret or "").strip().encode()).hexdigest()[:16]


def _load() -> dict:
    try:
        d = json.loads(_path().read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def retire(kind: str, secret: str, reason: str = "") -> None:
    """Запомнить, что учётка снята с маршрутизации."""
    secret = (secret or "").strip()
    if not secret:
        return
    d = _load()
    d[f"{kind}:{_digest(secret)}"] = {
        "kind": kind,
        "tail": secret[-6:],          # чтобы владелец узнал строку, но не восстановил
        "reason": reason,
        "at": time.strftime("%Y-%m-%d %H:%M"),
    }
    try:
        _path().write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass


def unretire(kind: str, secret: str) -> bool:
    """Вернуть учётку в строй. Нужно, если владелец продлил ту же подписку и
    осознанно вставляет ТУ ЖЕ строку обратно — автоматика не должна спорить с
    человеком молча."""
    d = _load()
    if d.pop(f"{kind}:{_digest(secret)}", None) is None:
        return False
    try:
        _path().write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        return False
    return True


def is_retired(kind: str, secret: str) -> bool:
    if not (secret or "").strip():
        return False
    return f"{kind}:{_digest(secret)}" in _load()


def listing() -> list[dict]:
    """Всё снятое — для отчётов и для панели настроек."""
    return sorted(_load().values(), key=lambda r: r.get("at", ""))


def strip_from_config(cfg: dict) -> list[str]:
    """Убрать снятые Deezer ARL из словаря конфига НА МЕСТЕ.

    Зовётся и на загрузке, и перед сохранением: первое не даёт снятой учётке
    попасть в маршрутизацию после рестарта, второе — вычищает её из файла
    навсегда, тем самым освобождая поле в настройках, о чём и просил владелец.

    Возвращает человекочитаемые строки о том, что убрано (пусто = ничего).
    """
    if not isinstance(cfg, dict):
        return []
    notes: list[str] = []

    main = (cfg.get("deezer-arl") or "").strip()
    if main and is_retired(_KIND_DEEZER, main):
        cfg["deezer-arl"] = ""
        notes.append(f"Deezer ARL ...{main[-6:]} (основной) снят автоматикой — поле освобождено")

    pool = cfg.get("deezer-accounts")
    if isinstance(pool, list):
        kept = []
        for a in pool:
            arl = (a.get("arl") or "").strip() if isinstance(a, dict) else ""
            if arl and is_retired(_KIND_DEEZER, arl):
                notes.append(f"Deezer ARL ...{arl[-6:]} снят автоматикой — запись удалена")
                continue
            kept.append(a)
        if len(kept) != len(pool):
            cfg["deezer-accounts"] = kept

    notes += _strip_qobuz(cfg)
    notes += _strip_soundcloud(cfg)
    return notes


def _qobuz_secret(a: dict) -> str:
    """Чем опознаётся учётка Qobuz — токен, а при входе по паролю почта.
    Совпадает с `qobuz_accounts.account_secret`; продублировано намеренно,
    чтобы реестр не тянул за собой сетевой модуль."""
    if not isinstance(a, dict):
        return ""
    return ((a.get("qobuz-auth-token") or a.get("auth_token") or "").strip()
            or (a.get("qobuz-email") or a.get("email") or "").strip())


def _strip_qobuz(cfg: dict) -> list[str]:
    notes: list[str] = []
    main = _qobuz_secret(cfg)
    if main and is_retired(_KIND_QOBUZ, main):
        for k in ("qobuz-auth-token", "qobuz-user-id", "qobuz-email", "qobuz-password"):
            if k in cfg:
                cfg[k] = ""
        notes.append(f"Qobuz ...{main[-6:]} (основная) снята автоматикой — поля освобождены")

    pool = cfg.get("qobuz-accounts")
    if isinstance(pool, list):
        kept = []
        for a in pool:
            s = _qobuz_secret(a)
            if s and is_retired(_KIND_QOBUZ, s):
                notes.append(f"Qobuz ...{s[-6:]} снята автоматикой — запись удалена")
                continue
            kept.append(a)
        if len(kept) != len(pool):
            cfg["qobuz-accounts"] = kept
    return notes


def _strip_soundcloud(cfg: dict) -> list[str]:
    """То же для SoundCloud: снятый токен не должен возвращаться сохранением."""
    notes: list[str] = []
    main = (cfg.get("soundcloud-oauth-token") or "").strip()
    if main and is_retired(_KIND_SC, main):
        cfg["soundcloud-oauth-token"] = ""
        notes.append(f"SoundCloud ...{main[-6:]} (основной) снят автоматикой — поле освобождено")

    pool = cfg.get("soundcloud-accounts")
    if isinstance(pool, list):
        kept = []
        for a in pool:
            tok = (a.get("token") or "").strip() if isinstance(a, dict) else ""
            if tok and is_retired(_KIND_SC, tok):
                notes.append(f"SoundCloud ...{tok[-6:]} снят автоматикой — запись удалена")
                continue
            kept.append(a)
        if len(kept) != len(pool):
            cfg["soundcloud-accounts"] = kept
    return notes
