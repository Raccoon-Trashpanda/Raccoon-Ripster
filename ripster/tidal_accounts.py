"""Измерение учёток Tidal: что они РЕАЛЬНО отдают.

Пул без измерений раздаёт слоты по порядку записи, то есть вслепую: мёртвая
учётка получит задачу раньше живой просто потому, что стоит выше. У Deezer и
Qobuz эта дыра уже закрыта (см. `deezer_accounts.py`, `qobuz_accounts.py`),
здесь — то же самое для Tidal.

Спрашиваем сам Tidal, а не догадываемся по метке в конфиге:

* `/v1/sessions` — жив ли токен и в какой он стране (страна учётки решает,
  какие релизы вообще видны: новозеландская пятница наступает первой);
* `/v1/users/{id}/subscription` — тип подписки и `highestSoundQuality`.

Последнее важнее всего: 12.09.2026 разбор жалобы «Tidal отдаёт 403 на
загрузке» начался с вывода «подписка не даёт lossless» — и вывод оказался
НЕВЕРНЫМ, потому что проверяли на треке, у которого в каталоге стоит
`audioQuality: LOW`. Права учётки видны только у самой учётки, у трека — его
собственное качество. Поэтому здесь спрашиваем именно подписку.
"""
from __future__ import annotations

import hashlib
import json
import time

_MEM: dict[str, tuple[float, dict]] = {}

#: Клиент, которым обмениваем refresh на access. Тот же, что у движка: чужой
#: клиент выпишет сессию с другими правами, и измерение будет не про эту учётку.
_CID = "cgiF7TQuB97BUIu3"
_CSEC = "1nqpgx8uvBdZigrx4hUPDV2hOwgYAAAG5DYXOr6uNf8="


def _key(secret: str) -> str:
    return hashlib.sha256((secret or "").strip().encode()).hexdigest()[:16]


def _cache_path():
    import os
    from pathlib import Path

    base = Path(os.environ.get("RIPSTER_BASE_DIR") or Path(__file__).resolve().parent.parent)
    p = base / "dist" / "tidal_accounts.json"
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
    """Чем учётка опознаётся: refresh-токен, а при входе по паролю — почта."""
    # Форм записи ДВЕ (как у Qobuz): в config.yaml — поля верхнего уровня
    # (`tidal-refresh`, `tidal-email`), а запись ПУЛА (`tidal-accounts[i]`) —
    # короткие ключи (`refresh`, `email`). Читались только конфиг-имена, из-за
    # чего КАЖДАЯ пуловая учётка получала ПУСТОЙ идентификатор: здоровье, маска
    # и счётчик неудач всех слотов складывались в одну кучу, а статус не
    # показывался. Проверено на живом конфиге — все 3 слота давали пусто.
    # 18.09.2026.
    return ((acct.get("tidal-refresh") or acct.get("refresh") or "").strip()
            or (acct.get("tidal-email") or acct.get("email") or "").strip())


def engine_session_secret() -> str:
    """Refresh-токен ТОЙ сессии, которой движок будет качать прямо сейчас.

    Это НЕ `tidal-refresh` из config.yaml: OrpheusDL берёт сессию из своего
    хранилища (`orpheus/config/loginstorage.bin`, у слота пула — своего), и эти
    два хранилища ничем не синхронизированы. 23.09.2026 ростер показывал
    «✅ активна, alive: true» по конфигу, а загрузки в 19:37 падали с
    TidalAuthError именно потому, что сессия движка была мертва. Пусто — если
    сессии нет вовсе (тогда качать нечем).
    """
    try:
        from ripster.engines.tidal import session_refresh_token
        return session_refresh_token()
    except Exception:  # noqa: BLE001
        return ""


def known(secret: str, max_age: float = 24 * 3600.0) -> dict | None:
    """Последнее измерение, если оно не старше `max_age`. Иначе None.

    None означает «не спрашивали», а не «мертва»: путать эти два состояния —
    ровно та ошибка, из-за которой живые учётки уходили в конец очереди.
    """
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


async def account_info(acct: dict, fresh: bool = False) -> dict:
    """Измерить учётку. Возвращает факт, а не обещание.

    Ключи: alive, reason, country, plan, quality, valid_until, lossless, hires.
    Сетевая авария помечается `unreachable` и НЕ считается приговором учётке —
    то же правило, что у Deezer и Qobuz: сеть не является свойством аккаунта.
    """
    import httpx

    secret = account_secret(acct)
    if not secret:
        return {"alive": False, "reason": "нет ни токена, ни почты"}
    if not fresh:
        cached = known(secret)
        if cached is not None:
            return cached

    refresh = (acct.get("tidal-refresh") or "").strip()
    if not refresh:
        # Вход по паролю измерить этим путём нельзя: токена ещё нет, а логиниться
        # ради проверки — значит жечь попытки входа. Честно говорим «не знаем».
        info = {"alive": None, "reason": "вход по паролю — не измеряли"}
        _cache_save(_key(secret), info)
        return info

    info: dict = {}
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(
                "https://auth.tidal.com/v1/oauth2/token",
                data={"refresh_token": refresh, "client_id": _CID,
                      "client_secret": _CSEC, "grant_type": "refresh_token"},
            )
            if r.status_code != 200:
                info = {"alive": False,
                        "reason": f"токен не обновляется (HTTP {r.status_code})"}
            else:
                tok = r.json()["access_token"]
                h = {"Authorization": f"Bearer {tok}"}
                s = (await c.get("https://api.tidal.com/v1/sessions", headers=h)).json()
                uid, cc = s.get("userId"), (s.get("countryCode") or "").upper()
                sub = await c.get(
                    f"https://api.tidal.com/v1/users/{uid}/subscription",
                    params={"countryCode": cc}, headers=h,
                )
                if sub.status_code != 200:
                    info = {"alive": True, "country": cc, "user_id": uid,
                            "reason": f"подписка не отвечает (HTTP {sub.status_code})"}
                else:
                    j = sub.json()
                    q = (j.get("highestSoundQuality") or "").upper()
                    info = {
                        "alive": True,
                        "user_id": uid,
                        "country": cc,
                        "plan": (j.get("subscription") or {}).get("type") or "",
                        "quality": q,
                        "valid_until": j.get("validUntil") or "",
                        "lossless": q in ("LOSSLESS", "HI_RES", "HI_RES_LOSSLESS"),
                        "hires": q in ("HI_RES", "HI_RES_LOSSLESS"),
                        "reason": "",
                    }
    except Exception as e:  # noqa: BLE001
        # Сеть, а не учётка: `alive: None` и пометка — чтобы ранжирование
        # оставило её на месте, а не выкинуло в конец.
        info = {"alive": None, "unreachable": True, "reason": f"сеть: {type(e).__name__}"}

    _cache_save(_key(secret), info)
    _MEM[_key(secret)] = (time.time(), info)
    return info


async def survey(config: dict, fresh: bool = False) -> list[dict]:
    """Измерить все заведённые учётки — для панели настроек."""
    from ripster.tidal_pool import configured_accounts

    out = []
    for i, a in enumerate(configured_accounts(config)):
        info = await account_info(a, fresh=fresh)
        out.append({"slot": i, "label": a.get("label") or f"account{i}", **info})
    return out
