"""Кто есть кто среди учёток Deezer: страна, тариф, жива ли, когда истекает.

Продолжение того, что 09.08.2026 сработало для Apple (`apple_accounts.py`).
Там незнание страны СЛОТА обошлось в ночь разбирательств: маршрутизатор не мог
выбрать сессию, которой релиз доступен, и уходил к чужому публичному wrapper'у.
У Deezer та же дыра, и она прямо описана в коде панели настроек: «страну мы
знаем только у ОСНОВНОЙ учётки: пул хранит ARL, но не страну».

Откуда берём. Открытый `api.deezer.com` про владельца ARL не знает ничего —
нужен приватный `gw-light.php`, метод `deezer.getUserData`. Он же отдаёт тариф
и срок, так что одним запросом закрываются сразу три вопроса: жива ли учётка,
в какой она стране и до какого числа.

🔒 ARL в кэш НЕ пишется. Файл лежит рядом с конфигом и попадает в бэкапы; ключ
кэша — короткий хеш, по нему учётку не восстановить. Секреты в кэшах — это то,
как они утекают.
"""
from __future__ import annotations

import hashlib
import json
import time

from ripster import http_client as _HTTP

_GW = "https://www.deezer.com/ajax/gw-light.php"
_BASE = {"api_version": "1.0", "input": "3", "api_token": ""}
_TTL = 6 * 3600.0
_MEM: dict[str, tuple[float, dict]] = {}


def _key(arl: str) -> str:
    return hashlib.sha256((arl or "").encode()).hexdigest()[:16]


def _cache_path():
    from pathlib import Path
    import os
    base = Path(os.environ.get("RIPSTER_BASE_DIR") or Path(__file__).resolve().parent.parent)
    p = base / "dist" / "deezer_accounts.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _cache_load() -> dict:
    try:
        return json.loads(_cache_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


def _cache_save(k: str, info: dict) -> None:
    try:
        d = _cache_load(); d[k] = {**info, "cached_at": int(time.time())}
        _cache_path().write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass


async def arl_info(arl: str, fresh: bool = False) -> dict:
    """Что известно об учётке по её ARL.

    Возвращает {alive, country, plan, expires, name} либо {"alive": False,
    "reason": …}. Различать «мёртвая» и «не спросили» обязательно: пустой ответ
    из-за сети — не то же самое, что отвергнутый ARL, и лечится по-разному.
    """
    if not (arl or "").strip():
        return {"alive": False, "reason": "ARL не задан"}
    k = _key(arl)
    hit = _MEM.get(k)
    if hit and not fresh and time.time() - hit[0] < _TTL:
        return hit[1]

    try:
        # ОТДЕЛЬНЫЙ клиент на каждый ARL, а не общий процесс-клиент.
        #
        # Общий клиент держит свою банку кук: первый же запрос кладёт в неё
        # `arl=<первый токен>`, и все последующие проверки отвечают про ПЕРВУЮ
        # учётку. 04.09.2026 из-за этого шесть разных ARL показывали одно и то
        # же — «жив, Deezer Free», — хотя на деле два из них Deezer Family с
        # lossless, а три вообще отвергнуты (гостевая сессия). Проверка учёток
        # врала всем: и healthcheck'у, и пулу, и владельцу. В самом http_client
        # об этом прямо написано: «не использовать там, где нужны куки уровня
        # клиента».
        import httpx as _httpx
        async with _httpx.AsyncClient(
            follow_redirects=True,
            timeout=25,
            headers={"User-Agent": "Mozilla/5.0"},
        ) as c:
            r = await c.post(_GW, params={**_BASE, "method": "deezer.getUserData"},
                             cookies={"arl": arl}, json={})
        if r.status_code != 200:
            return {"alive": False, "unreachable": True,
                    "reason": f"gw-light HTTP {r.status_code}"}
        res = (r.json() or {}).get("results") or {}
    except Exception as e:
        # Сеть/таймаут — это НЕ «ARL протух». Возвращаем причину как есть.
        # `unreachable` отличает «мы не смогли спросить» от «Deezer отказал»:
        # по первому нельзя ни снимать учётку с маршрутизации, ни двигать её
        # в конец очереди — виноват канал, а не токен.
        return {"alive": False, "unreachable": True,
                "reason": f"запрос не прошёл: {type(e).__name__}"}

    user = res.get("USER") or {}
    uid = user.get("USER_ID")
    if not uid or str(uid) == "0":
        # Отказ — тоже ответ, и его надо помнить. Раньше кэшировался только
        # успех: каждый выбор слота заново ходил в сеть за одним и тем же
        # «нет», а порядок перебора не мог узнать, что учётка мертва.
        # Сетевые сбои выше по коду возвращаются БЕЗ записи в кэш — они не
        # приговор учётке.
        bad = {"alive": False, "reason": "ARL отвергнут (гостевая сессия)"}
        _MEM[k] = (time.time(), bad)
        _cache_save(k, bad)
        return bad

    opts = user.get("OPTIONS") or {}
    info = {
        "alive": True,
        "country": str(user.get("COUNTRY") or opts.get("license_country") or "").lower(),
        "name": user.get("BLOG_NAME") or user.get("FIRSTNAME") or "",
        # Тариф: в gw-light он лежит в нескольких местах, берём первое непустое.
        "plan": str((res.get("OFFER_NAME") or opts.get("offer_name") or "")).strip(),
        "expires": str(opts.get("expiration_timestamp") or "") or "",
        "lossless": bool(opts.get("web_lossless") or opts.get("mobile_lossless")),
    }
    if info["expires"]:
        try:
            info["expires_date"] = time.strftime("%Y-%m-%d",
                                                 time.localtime(int(info["expires"])))
        except Exception:
            info["expires_date"] = ""
    _MEM[k] = (time.time(), info)
    _cache_save(k, {kk: vv for kk, vv in info.items() if kk != "name"})
    return info


def known(arl: str, max_age: float = 24 * 3600.0) -> dict | None:
    """Что УЖЕ измерено про учётку — ни одного сетевого запроса.

    Нужно там, где спрашивают на горячем пути (выбор слота в пуле): ждать
    ответа Deezer ради порядка перебора нельзя, а решать вслепую — то самое,
    из-за чего 04.09.2026 гостю не отдали релиз.

    Читаем память процесса, а при промахе — файл рядом с конфигом. Файл писали
    и другие процессы (healthcheck ходит своим прогоном), поэтому после
    рестарта приложения знание не теряется. `None` значит «не спрашивали» и
    честно отличается от «спрашивали, отвергнут».
    """
    k = _key(arl)
    hit = _MEM.get(k)
    if hit and time.time() - hit[0] < _TTL:
        return hit[1]
    rec = _cache_load().get(k)
    if not isinstance(rec, dict):
        return None
    if time.time() - float(rec.get("cached_at") or 0) > max_age:
        return None                      # устарело — судить по нему нельзя
    return rec


def configured_arls(config: dict) -> list[dict]:
    """Все ARL из конфига: основной плюс пул. Порядок = приоритет."""
    out = []
    main = (config.get("deezer-arl") or "").strip()
    if main:
        out.append({"arl": main, "label": "основной", "primary": True})
    for a in config.get("deezer-accounts") or []:
        if isinstance(a, dict) and (a.get("arl") or "").strip():
            out.append({"arl": a["arl"].strip(),
                        "label": a.get("label") or "без метки", "primary": False})
    return out


async def survey(config: dict, fresh: bool = False) -> list[dict]:
    """Обойти все ARL и сказать про каждый: жив, страна, тариф, срок.

    ARL наружу не отдаём — только метку и хеш, чтобы строку можно было
    сопоставить с записью в конфиге, но не утащить ключ.
    """
    out = []
    for i, a in enumerate(configured_arls(config)):
        info = await arl_info(a["arl"], fresh=fresh)
        out.append({"idx": i, "label": a["label"], "primary": a["primary"],
                    "id": _key(a["arl"]), **info})
    return out


async def pick_arl(config: dict, country: str = "", need_lossless: bool = False) -> str | None:
    """ARL, которым стоит качать: живой, нужной страны, с нужным качеством.

    Пустой ответ значит «подходящей учётки нет» — вызывающий волен взять
    основную и получить честный отказ, но выбирать вслепую он больше не обязан.
    """
    want = (country or "").lower()
    for a in configured_arls(config):
        info = await arl_info(a["arl"])
        if not info.get("alive"):
            continue
        if want and info.get("country") != want:
            continue
        if need_lossless and not info.get("lossless"):
            continue
        return a["arl"]
    return None
