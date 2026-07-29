"""
Матрица доступности релиза по сервисам.

«Релиз вышел» — бесполезное утверждение. Мировая дата релиза это анонс, а витрины
наполняются каждая сама, в своей зоне и в своём темпе, да ещё аккаунты владельца
живут в разных странах (Tidal новозеландский, Apple американский, Deezer
европейский). Значение имеет только одно: **в каком сервисе я могу скачать это
прямо сейчас**.

Поэтому храним не «есть/нет», а состояние ПО КАЖДОМУ сервису вместе с причиной —
она различает три совершенно разные ситуации:

  not_in_catalog_yet  витрина ещё не наполнилась     → перепроверить позже
  region_locked       в этом сторефронте не будет    → перепроверять бессмысленно
  no_token            наша недоработка, не витрины   → чинится настройками

Сверяем только точными идентификаторами — по названию нельзя: одноимённые синглы,
делюксы и ремастеры дают ложные совпадения. Штрихкод (UPC) для Apple и Deezer,
ISRC первого трека для Qobuz и Tidal.

Кэш живёт на диске, иначе он не переживает перезапуск и каждое утро всё
опрашивается заново.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

# Сервисы, у которых есть точный поиск по идентификатору. Spotify держим
# отдельно: он каталог, а не источник файлов.
SERVICES = ("apple", "deezer", "qobuz", "tidal")

REASON_NOT_YET  = "not_in_catalog_yet"
REASON_REGION   = "region_locked"
REASON_NO_TOKEN = "no_token"
# Спросить было НЕЧЕМ. У Qobuz и Tidal нет поиска по штрихкоду — им нужен ISRC, и
# без него ответ «ещё не появился» был бы враньём: мы туда вообще не ходили.
# Отдельная причина, потому что лечится она не ожиданием, а добычей ISRC.
REASON_NO_ID    = "no_identifier"

# Найденное больше не перепроверяем: релиз из витрины не исчезает.
_TTL_MISS   = 30 * 60          # ещё не подъехало — заглядываем каждые полчаса
_TTL_REGION = 7 * 24 * 3600    # региональный отказ — раз в неделю, не чаще
_TTL_TOKEN  = 10 * 60          # наша проблема: почини токен — увидим сразу

_cfg: dict = {}
_base_dir: Path = Path(".")
_cache: dict = {}
_loaded = False


def configure(cfg: dict, base_dir: Path) -> None:
    global _cfg, _base_dir
    _cfg, _base_dir = cfg, Path(base_dir)


def _cache_path() -> Path:
    return _base_dir / "availability_cache.json"


def _load() -> None:
    global _cache, _loaded
    if _loaded:
        return
    try:
        _cache = json.loads(_cache_path().read_text(encoding="utf-8")) or {}
    except Exception:
        _cache = {}
    _loaded = True


def _save() -> None:
    try:
        _cache_path().write_text(json.dumps(_cache, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _key(upc: str, isrc: str, title: str, artist: str) -> str:
    if upc:
        return "upc:" + "".join(ch for ch in upc if ch.isdigit())
    if isrc:
        return "isrc:" + isrc.upper()
    return "name:" + (title or "").lower().strip() + "|" + (artist or "").lower().strip()


def _fresh(rec: dict) -> bool:
    """Нужно ли доверять записи или пора перепроверять."""
    if rec.get("available"):
        return True                     # найденное не пропадает
    age = time.time() - float(rec.get("checked_ts") or 0)
    reason = rec.get("reason") or REASON_NOT_YET
    ttl = {REASON_REGION: _TTL_REGION, REASON_NO_TOKEN: _TTL_TOKEN}.get(reason, _TTL_MISS)
    return age < ttl


def _has_credentials(service: str) -> bool:
    """Есть ли чем спрашивать этот сервис. Без этого «не найдено» — враньё."""
    c = _cfg or {}
    if service == "apple":
        tok = str(c.get("authorization-token") or "").strip()
        return bool(tok) and tok != "your-authorization-token"
    if service == "deezer":
        return True                     # публичный поиск по штрихкоду, токен не нужен
    if service == "qobuz":
        return bool(str(c.get("qobuz-auth-token") or "").strip())
    if service == "tidal":
        return bool(str(c.get("tidal-token") or "").strip()) or True  # есть путь через OrpheusDL
    return False


async def _probe_one(service: str, upc: str, isrc: str) -> dict:
    """Спросить один сервис. Возвращает запись матрицы."""
    now = time.time()
    if not _has_credentials(service):
        return {"available": False, "reason": REASON_NO_TOKEN, "checked_ts": now}
    try:
        from ripster.routes import discovery as _disc
        hit = None
        if service in ("apple", "deezer") and upc:
            hit = await _disc._find_by_upc(upc, service,
                                           str((_cfg or {}).get("storefront", "us")))
        if hit is None and isrc and service in ("qobuz", "tidal"):
            # Список ISRC (несколько первых треков) — одного мало: его может не
            # быть в каталоге при том, что релиз там есть.
            for one in (isrc.split(",") if isinstance(isrc, str) else list(isrc)):
                one = one.strip()
                if not one:
                    continue
                hit = await _disc._find_by_isrc(one, service)
                if hit:
                    break
        if hit is None and service in ("qobuz", "tidal") and not isrc:
            return {"available": False, "reason": REASON_NO_ID, "checked_ts": now}
        if hit:
            return {"available": True, "url": hit.get("url", ""),
                    "title": hit.get("title", ""), "artist": hit.get("artist", ""),
                    "cover": hit.get("cover", ""), "matched_by": hit.get("matched_by", ""),
                    "checked_ts": now}
    except Exception as e:                                     # noqa: BLE001
        return {"available": False, "reason": REASON_NOT_YET,
                "error": str(e)[:120], "checked_ts": now}
    return {"available": False, "reason": REASON_NOT_YET, "checked_ts": now}


async def _derive_isrc(service: str, hit: dict) -> str:
    """Достать ISRC первого трека из найденного релиза.

    Он и есть ключ к Qobuz и Tidal: штрихкода у них нет, а ISRC опознаёт запись
    однозначно — без него пришлось бы сверять по названию, а это ложные
    совпадения на делюксах, ремастерах и одноимённых синглах.
    """
    try:
        from ripster.routes import discovery as _disc
        url = str(hit.get("url") or "")
        aid = ""
        if service == "deezer":
            import re
            m = re.search(r"/album/(\d+)", url)
            aid = m.group(1) if m else ""
        if not aid:
            return ""
        vals = await _disc._seed_isrcs({"id": aid, "service": service}) or []
        return ",".join(vals)
    except Exception:
        return ""


async def matrix(upc: str = "", isrc: str = "", title: str = "", artist: str = "",
                 services: Optional[tuple] = None, force: bool = False) -> dict:
    """Где этот релиз можно взять прямо сейчас.

    Кэш: найденное не перепроверяется никогда, ненайденное — по своему TTL,
    региональный отказ — раз в неделю (в этом сторефронте оно не появится).
    """
    _load()
    svcs = tuple(services or SERVICES)
    k = _key(upc, isrc, title, artist)
    rec = _cache.get(k) or {"services": {}}
    out = dict(rec.get("services") or {})

    # По штрихкоду умеют только Apple и Deezer, поэтому идём ими первыми: если
    # релиз там нашёлся, из него добываем ISRC первого трека — и тогда Qobuz с
    # Tidal можно спросить по-настоящему, а не отписаться «нечем спросить».
    ordered = [s for s in ("deezer", "apple") if s in svcs] + \
              [s for s in svcs if s not in ("deezer", "apple")]

    for svc in ordered:
        cur = out.get(svc)
        if cur and not force and _fresh(cur):
            if svc in ("deezer", "apple") and cur.get("available") and not isrc:
                isrc = isrc or (rec.get("isrc") or "")
            continue
        out[svc] = await _probe_one(svc, upc, isrc)
        if (svc in ("deezer", "apple") and out[svc].get("available") and not isrc):
            isrc = await _derive_isrc(svc, out[svc])

    _cache[k] = {"services": out, "upc": upc, "isrc": isrc,
                 "title": title, "artist": artist, "ts": time.time()}
    # ISRC достали по ходу — сохраняем, иначе следующая проверка снова пойдёт
    # его добывать.
    _save()
    return {"key": k, "upc": upc, "isrc": isrc, "services": out,
            "available_in": [s for s, v in out.items() if v.get("available")]}


def pick_source(matrix_services: dict, preference: Optional[list] = None) -> str:
    """Откуда качать: пересечение предпочтений владельца с тем, что реально есть.

    Порядок по умолчанию — от лучшего качества к худшему. Ждать «свой» сервис,
    когда релиз уже доступен в другом, значит потерять сутки.
    """
    pref = preference or list((_cfg or {}).get("availability-preference")
                              or ["apple", "qobuz", "deezer", "tidal"])
    for svc in pref:
        if (matrix_services.get(svc) or {}).get("available"):
            return svc
    return ""


def summary_ru(matrix_services: dict) -> str:
    """Строка для владельца: не «релиз доступен», а где именно."""
    ready = [s for s, v in matrix_services.items() if v.get("available")]
    waiting = [s for s, v in matrix_services.items()
               if not v.get("available") and v.get("reason") == REASON_NOT_YET]
    blocked = [s for s, v in matrix_services.items()
               if v.get("reason") == REASON_REGION]
    no_tok = [s for s, v in matrix_services.items()
              if v.get("reason") == REASON_NO_TOKEN]
    no_id = [s for s, v in matrix_services.items()
             if v.get("reason") == REASON_NO_ID]
    parts = []
    if ready:
        parts.append("✅ " + ", ".join(ready) + " — можно скачать")
    if waiting:
        parts.append("⏳ " + ", ".join(waiting) + " — ещё не появился")
    if blocked:
        parts.append("🚫 " + ", ".join(blocked) + " — нет в регионе аккаунта")
    if no_tok:
        parts.append("🔑 " + ", ".join(no_tok) + " — нет токена")
    if no_id:
        parts.append("❔ " + ", ".join(no_id) + " — не спрашивал, нет ISRC")
    return " · ".join(parts) or "нигде не найден"
