"""Измерение учёток Яндекс.Музыки.

До 12.09.2026 у Яндекса не было ничего: пул раздавал токены по порядку записи,
и протухший уходил в загрузку первым просто потому, что стоял выше. Это
последний сервис без измерений — у Apple, Deezer, Qobuz, SoundCloud и Tidal они
уже есть.

Спрашиваем `account/status`: он отвечает и о самом токене (жив ли), и о
подписке (`plus.hasPlus`), и о регионе. Без Plus сервис отдаёт только lossy —
это не повод снимать учётку, но повод сказать вслух, иначе «скачалось не то
качество» выглядит как ошибка загрузчика.

ВАЖНО про геозависимость: `account/status` отвечает 403 вне России. Такой ответ
означает «мы не смогли спросить», а НЕ «токен мёртв» — тот же урок, что уже
стоил нам сервиса в мобильном приложении, где живой Яндекс не показывался в
поиске из-за пробы, зависящей от страны запроса.
"""
from __future__ import annotations

import hashlib
import json
import time

_MEM: dict[str, tuple[float, dict]] = {}


def _key(secret: str) -> str:
    return hashlib.sha256((secret or "").strip().encode()).hexdigest()[:16]


def _cache_path():
    import os
    from pathlib import Path

    base = Path(os.environ.get("RIPSTER_BASE_DIR") or Path(__file__).resolve().parent.parent)
    p = base / "dist" / "yandex_accounts.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _cache_load() -> dict:
    try:
        d = json.loads(_cache_path().read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _cache_save(k: str, info: dict) -> None:
    try:
        d = _cache_load()
        d[k] = {**info, "cached_at": int(time.time())}
        _cache_path().write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def account_secret(acct: dict) -> str:
    return ((acct.get("yandex-token") or acct.get("token") or "").strip())


def known(secret: str, max_age: float = 24 * 3600.0) -> dict | None:
    """Последнее измерение, если свежее `max_age`; иначе None («не спрашивали»)."""
    k = _key(secret)
    hit = _MEM.get(k)
    if hit and (time.time() - hit[0]) < max_age:
        return hit[1]
    ent = _cache_load().get(k)
    if ent and (time.time() - float(ent.get("cached_at", 0))) < max_age:
        info = {x: y for x, y in ent.items() if x != "cached_at"}
        _MEM[k] = (time.time(), info)
        return info
    return None


async def token_info(token: str, fresh: bool = False) -> dict:
    """Измерить токен. Ключи: alive, reason, plus, region, login, unreachable."""
    import httpx

    secret = (token or "").strip()
    if not secret:
        return {"alive": False, "reason": "пустой токен"}
    if not fresh:
        cached = known(secret)
        if cached is not None:
            return cached

    info: dict
    try:
        async with httpx.AsyncClient(timeout=25) as c:
            r = await c.get(
                "https://api.music.yandex.net/account/status",
                headers={"Authorization": f"OAuth {secret}",
                         "X-Yandex-Music-Client": "YandexMusicAndroid/24023621"},
            )
        if r.status_code in (401, 403) and r.status_code == 401:
            info = {"alive": False, "reason": "токен отвергнут (401)"}
        elif r.status_code == 403:
            # Геоблок: про токен мы не узнали ничего.
            info = {"alive": None, "unreachable": True,
                    "reason": "регион не обслуживается (403) — не смогли спросить"}
        elif r.status_code != 200:
            info = {"alive": None, "unreachable": True,
                    "reason": f"сервис ответил HTTP {r.status_code}"}
        else:
            acc = (r.json().get("result") or {})
            plus = bool((acc.get("plus") or {}).get("hasPlus"))
            a = acc.get("account") or {}
            info = {
                "alive": True,
                "plus": plus,
                "lossless": plus,          # FLAC отдаётся только с Plus
                "login": a.get("login") or "",
                "region": str(a.get("region") or ""),
                "reason": "" if plus else "нет Plus — только lossy",
            }
    except Exception as e:  # noqa: BLE001
        info = {"alive": None, "unreachable": True, "reason": f"сеть: {type(e).__name__}"}

    _cache_save(_key(secret), info)
    _MEM[_key(secret)] = (time.time(), info)
    return info


def configured_tokens(config: dict) -> list[dict]:
    """Основной токен плюс все из `yandex-accounts`."""
    out: list[dict] = []
    primary = (config.get("yandex-token") or "").strip()
    if primary:
        out.append({"token": primary, "label": "primary", "primary": True})
    for i, a in enumerate(config.get("yandex-accounts") or []):
        tok = (a.get("token") or a.get("yandex-token") or "").strip()
        if tok:
            out.append({"token": tok, "label": a.get("label") or f"account{i + 1}",
                        "primary": False})
    return out


async def survey(config: dict, fresh: bool = False) -> list[dict]:
    rows = []
    for i, e in enumerate(configured_tokens(config)):
        info = await token_info(e["token"], fresh=fresh)
        rows.append({"slot": i, "label": e["label"], **info})
    return rows
