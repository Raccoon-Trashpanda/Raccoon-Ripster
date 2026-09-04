"""Кто есть кто среди учёток SoundCloud: жив ли токен и есть ли Go+.

Третий в ряду после `deezer_accounts` и `qobuz_accounts` (04.09.2026). Повод тот
же, и владелец назвал его прямо: «внедрил третий токен для SoundCloud, но надо
проверять на наличие вообще подписки — по мотивам нашей борьбы с Deezer».

У SoundCloud вопрос стоит даже острее, чем у остальных. Токен без Go+ ВАЛИДЕН:
`/me` отвечает 200, ошибок нет — просто HQ-поток (AAC 256) такой учётке не
дают, и трек приезжает в 128 kbps либо не приезжает вовсе. То есть «жив» и
«может качать в качестве» здесь особенно разные вещи, и по одному коду ответа
их не различить.

Тариф читаем ровно так же, как проба в настройках (`routes/auth.py::
_probe_soundcloud`): Go+ лежит в `consumer_subscription.product`, а НЕ в
`subscription`. Трактовку намеренно не переизобретаю — два места, расходящиеся
в том, что считать подпиской, хуже одного неудобного импорта.

🔒 Токен в кэш не пишется — только короткий хеш.
"""
from __future__ import annotations

import hashlib
import json
import time

_ME = "https://api-v2.soundcloud.com/me"
_TTL = 6 * 3600.0
_MEM: dict[str, tuple[float, dict]] = {}

# Идентификаторы тарифов, означающие «платного ничего нет».
_FREE_IDS = {"", "free", "free-tier", "trial", "free-v01"}
_LABEL = {
    "consumer-high-tier": "Go+",
    "consumer-mid-tier": "Go",
    "soundcloud-go-plus": "Go+",
    "soundcloud-go": "Go",
}


def _key(token: str) -> str:
    return hashlib.sha256((token or "").strip().encode()).hexdigest()[:16]


def _cache_path():
    import os
    from pathlib import Path
    base = Path(os.environ.get("RIPSTER_BASE_DIR") or Path(__file__).resolve().parent.parent)
    p = base / "dist" / "soundcloud_accounts.json"
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


def _auth(token: str) -> str:
    """Токен владелец копирует и с префиксом, и без — принимаем оба вида."""
    t = (token or "").strip()
    return t if t.lower().startswith("oauth ") else f"OAuth {t}"


def _read_plan(u: dict) -> dict:
    prod = ((u.get("consumer_subscription") or {}).get("product")
            or (u.get("subscription") or {}).get("product") or {})
    pid = str(prod.get("id") or "")
    label = _LABEL.get(pid, prod.get("name") or "")
    paid = (bool(pid) and pid.lower() not in _FREE_IDS) or bool(u.get("go_plus"))
    return {
        "plan": label or (pid if pid else ("Go+" if paid else "Free")),
        "go_plus": paid,
        # Чей это аккаунт — по версии SoundCloud. У владельца 04.09.2026 два
        # разных токена вели в один аккаунт; поймать это можно только так.
        "account_id": str(u.get("id") or ""),
        "login": u.get("username") or "",
        "country": u.get("country_code") or u.get("country") or "",
    }


async def account_info(token: str, fresh: bool = False) -> dict:
    """Что известно про учётку SoundCloud.

    `{alive, go_plus, plan, country, …}` либо `{alive: False, reason}`.
    `unreachable=True` значит «спросить не удалось» — по такому ответу учётку не
    снимают и в конец очереди не двигают: виноват канал, а не токен.
    """
    token = (token or "").strip()
    if not token:
        return {"alive": False, "reason": "токен не задан"}
    k = _key(token)
    hit = _MEM.get(k)
    if hit and not fresh and time.time() - hit[0] < _TTL:
        return hit[1]

    try:
        import httpx
        # Свой клиент на вызов — правило, которое 04.09.2026 спасло Deezer:
        # общий процесс-клиент склеивает состояние между учётками.
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(_ME, headers={"Authorization": _auth(token),
                                          "User-Agent": "Mozilla/5.0"})
    except Exception as e:
        return {"alive": False, "unreachable": True,
                "reason": f"запрос не прошёл: {type(e).__name__}"}

    if r.status_code == 401:
        bad = {"alive": False, "reason": "SoundCloud отверг токен (401)"}
        _MEM[k] = (time.time(), bad)
        _cache_save(k, bad)
        return bad
    if r.status_code == 429:
        return {"alive": False, "unreachable": True,
                "reason": "SoundCloud: лимит запросов (429)"}
    if r.status_code != 200:
        return {"alive": False, "unreachable": True,
                "reason": f"SoundCloud HTTP {r.status_code}"}
    try:
        u = r.json()
    except Exception:
        return {"alive": False, "unreachable": True, "reason": "SoundCloud ответил не-JSON"}

    info = {"alive": True, **_read_plan(u)}
    if not info["go_plus"]:
        # Токен рабочий, но HQ (AAC 256) такой учётке не отдадут.
        info["reason"] = "нет подписки Go+ — HQ-поток недоступен, только 128 kbps"
    _MEM[k] = (time.time(), info)
    _cache_save(k, dict(info))
    return info


def known(token: str, max_age: float = 24 * 3600.0) -> dict | None:
    """Что УЖЕ измерено — без сетевых запросов (см. `deezer_accounts.known`)."""
    k = _key(token)
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
    """Учётки SoundCloud из конфига. Определение одно и живёт в пуле."""
    from ripster import soundcloud_pool
    return soundcloud_pool._configured_accounts(config)


async def survey(config: dict, fresh: bool = False) -> list[dict]:
    """Обойти все токены. Наружу — метка и хеш, но не сам токен."""
    out = []
    for i, a in enumerate(configured_accounts(config)):
        info = await account_info(a["token"], fresh=fresh)
        out.append({"idx": i, "label": a.get("label") or f"account{i}",
                    "primary": i == 0, "id": _key(a["token"]), **info})
    return out
