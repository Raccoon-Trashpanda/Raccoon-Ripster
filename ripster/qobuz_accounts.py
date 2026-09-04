"""Кто есть кто среди учёток Qobuz: подписка активна, lossless, hi-res, срок.

Продолжение того, что 04.09.2026 закрыло Deezer (`deezer_accounts.py`). Там
гостю не отдали релиз, потому что перебор учёток тратил попытки на бесплатную и
две отвергнутые, а две рабочие стояли последними. У Qobuz раскладка ровно та же:
у владельца несколько токенов, и платная подписка есть не у всех — про это прямо
написано в `runner.py` («у владельца три токена Qobuz, и платная подписка есть
не у всех»). Не хватало только измерения: пул не знал про учётки НИЧЕГО.

Почему не переиспользован `routes/auth.py`. Там та же проба, но она живёт внутри
приложения: пишет `_cfg`, зовёт `_save_config`, то есть имеет побочные эффекты и
требует, чтобы маршруты были установлены. Сторож здоровья работает отдельным
процессом, без приложения — ему нужна чистая функция. Эндпоинт и разбор ответа
намеренно те же самые, чтобы два места не разошлись в трактовке.

🔒 Токен в кэш НЕ пишется — только короткий хеш. Файл лежит рядом с конфигом и
попадает в бэкапы.
"""
from __future__ import annotations

import hashlib
import json
import time

_LOGIN = "https://www.qobuz.com/api.json/0.2/user/login"
_DEFAULT_APP_ID = "312369995"      # тот же, что в engines/qobuz.py
_TTL = 6 * 3600.0
_MEM: dict[str, tuple[float, dict]] = {}


def _key(secret: str) -> str:
    return hashlib.sha256((secret or "").strip().encode()).hexdigest()[:16]


def _cache_path():
    import os
    from pathlib import Path
    base = Path(os.environ.get("RIPSTER_BASE_DIR") or Path(__file__).resolve().parent.parent)
    p = base / "dist" / "qobuz_accounts.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _cache_load() -> dict:
    try:
        d = json.loads(_cache_path().read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _cache_save(k: str, info: dict) -> None:
    try:
        d = _cache_load(); d[k] = {**info, "cached_at": int(time.time())}
        _cache_path().write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass


def account_secret(acct: dict) -> str:
    """Строка, по которой учётка опознаётся: токен, а при входе по паролю —
    почта. Именно она попадает в реестр снятых и в ключ кэша."""
    return ((acct.get("qobuz-auth-token") or "").strip()
            or (acct.get("qobuz-email") or "").strip())


def _read_user(user: dict) -> dict:
    """Разбор блока `user` из ответа Qobuz.

    Повторяет `routes/auth.py::_qobuz_user_block` в той части, что важна для
    маршрутизации. Ключевое — `eligible`: `credential.parameters` пуст у
    бесплатных и просроченных аккаунтов, и streamrip на таком падает с
    IneligibleError. Токен при этом ВАЛИДЕН, поэтому «жив» и «может качать» —
    разные вопросы, и путать их нельзя.
    """
    creds = (user or {}).get("credential") or {}
    params = creds.get("parameters") or {}     # JSON null у бесплатных
    sub = (user or {}).get("subscription") or {}
    end = sub.get("end_date") or params.get("end_date") or ""
    expired = False
    if end:
        try:
            from datetime import date as _date
            y, m, d = (int(x) for x in str(end)[:10].split("-"))
            expired = (_date(y, m, d) - _date.today()).days < 0
        except Exception:
            pass
    return {
        # Чей это аккаунт — по версии Qobuz. Два разных токена могут вести в
        # одну учётку, и узнать это можно только спросив сервис
        # (см. ripster/dedup_accounts.py).
        "account_id": str((user or {}).get("id") or ""),
        "country": (user or {}).get("country_code") or (user or {}).get("country") or "",
        "plan": str(sub.get("offer") or creds.get("label")
                    or creds.get("description") or params.get("short_label") or "").strip(),
        "eligible": bool(params),
        "lossless": bool(params.get("lossless_streaming")),
        "hires": bool(params.get("hires_streaming")),
        "expires": str(end or ""),
        "expired": expired,
    }


async def account_info(acct: dict, app_id: str = "", fresh: bool = False) -> dict:
    """Что известно про учётку Qobuz.

    Возвращает {alive, eligible, lossless, hires, …} либо {"alive": False,
    "reason": …}. `unreachable=True` значит «спросить не удалось» — по такому
    ответу нельзя ни снимать учётку, ни двигать её в конец очереди: виноват
    канал, а не токен (урок Deezer от 04.09.2026).
    """
    secret = account_secret(acct)
    if not secret:
        return {"alive": False, "reason": "нет ни токена, ни почты"}
    k = _key(secret)
    hit = _MEM.get(k)
    if hit and not fresh and time.time() - hit[0] < _TTL:
        return hit[1]

    app_id = (app_id or "").strip() or _DEFAULT_APP_ID
    token = (acct.get("qobuz-auth-token") or "").strip()
    try:
        import httpx
        # Свой клиент на каждый вызов — то же правило, что спасло Deezer:
        # общий процесс-клиент склеивает состояние между учётками.
        async with httpx.AsyncClient(timeout=20) as c:
            if token:
                params = {"user_auth_token": token, "app_id": app_id}
                uid = (acct.get("qobuz-user-id") or "").strip()
                if uid:
                    params["user_id"] = uid
                r = await c.post(_LOGIN, params=params)
            else:
                import hashlib as _h
                pwd = (acct.get("qobuz-password") or "")
                r = await c.get(_LOGIN, params={
                    "email": (acct.get("qobuz-email") or "").strip(),
                    "password": _h.md5(pwd.encode()).hexdigest(),
                    "app_id": app_id}, headers={"X-App-Id": app_id})
    except Exception as e:
        return {"alive": False, "unreachable": True,
                "reason": f"запрос не прошёл: {type(e).__name__}"}

    if r.status_code == 401:
        bad = {"alive": False, "reason": "Qobuz отверг учётные данные (401)"}
        _MEM[k] = (time.time(), bad)
        _cache_save(k, bad)
        return bad
    if r.status_code == 400:
        # Неверный app_id — это про НАСТРОЙКУ, а не про учётку. Снимать её за
        # это нельзя: сменится app_id — та же учётка снова заработает.
        return {"alive": False, "unreachable": True,
                "reason": "Qobuz: 400 (неверный app_id — настройка, не учётка)"}
    if r.status_code != 200:
        return {"alive": False, "unreachable": True,
                "reason": f"Qobuz HTTP {r.status_code}"}

    try:
        data = r.json()
    except Exception:
        return {"alive": False, "unreachable": True, "reason": "Qobuz ответил не-JSON"}

    info = {"alive": True, **_read_user(data.get("user") or {})}
    if not info["eligible"]:
        # Токен рабочий, но качать нечем: подписки нет или она истекла.
        info["reason"] = ("подписка истекла " + info["expires"]) if info["expired"] \
            else "нет активной подписки Qobuz (скачивание невозможно)"
    _MEM[k] = (time.time(), info)
    _cache_save(k, dict(info))
    return info


def known(secret: str, max_age: float = 24 * 3600.0) -> dict | None:
    """Что УЖЕ измерено — без единого сетевого запроса (см. deezer_accounts.known)."""
    k = _key(secret)
    hit = _MEM.get(k)
    if hit and time.time() - hit[0] < _TTL:
        return hit[1]
    rec = _cache_load().get(k)
    if not isinstance(rec, dict):
        return None
    if time.time() - float(rec.get("cached_at") or 0) > max_age:
        return None
    return rec


def configured_accounts(config: dict) -> list[dict]:
    """Учётки Qobuz из конфига. Определение ОДНО, живёт в пуле — второй список
    неизбежно разъехался бы с первым."""
    from ripster import qobuz_pool
    return qobuz_pool._configured_accounts(config)


async def survey(config: dict, fresh: bool = False) -> list[dict]:
    """Обойти все учётки Qobuz. Секрет наружу не отдаём — только метку и хеш."""
    app_id = (config.get("qobuz-app-id") or "").strip()
    out = []
    for i, a in enumerate(configured_accounts(config)):
        info = await account_info(a, app_id=app_id, fresh=fresh)
        out.append({"idx": i, "label": a.get("label") or f"account{i}",
                    "primary": i == 0, "id": _key(account_secret(a)), **info})
    return out
