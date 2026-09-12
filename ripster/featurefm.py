"""feature.fm: ISRC и UPC из кабинета, через собственную сессию владельца.

Почему так, а не через их API. Партнёрский API существует
(`developers.feature.fm`, ключ `x-api-key`), но выдаётся по заявке партнёрам, и
главное — ISRC/UPC в его ответах НЕТ вовсе: там они только на ВХОД, для
сканирования. Проверено 12.09.2026. Значит единственный честный путь к своим же
данным — повторить запрос, который делает их собственный кабинет.

Запрос не угадывается, а переносится: DevTools → Copy as cURL (bash) →
`tools/curl_import.py featurefm`. Сохранённое лежит в
`tokens/featurefm_request.json`.

ЧЕСТНОЕ ОГРАНИЧЕНИЕ, о котором предупредил автор способа: всё держится на
куке сессии. Когда она протухнет, запросы начнут возвращать вход вместо
данных — и это не «сервис сломался». Поэтому ниже отказ по протухшей сессии
называется своим именем, а не прячется за «ничего не найдено»: перепутать эти
два состояния значит однажды решить, что релиза нет в каталоге.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

_BASE = Path(__file__).resolve().parent.parent
_REQ = _BASE / "tokens" / "featurefm_request.json"
_CACHE = _BASE / "dist" / "featurefm_cache.json"

#: Сколько держим ответ. Идентификаторы релиза не меняются, но кабинет может
#: дополнить карточку, поэтому неделя, а не вечность.
_TTL = 7 * 24 * 3600.0


class NotConfigured(RuntimeError):
    """Запрос ещё не перенесён из браузера."""


class SessionExpired(RuntimeError):
    """Кука сессии протухла — данные недоступны, но каталог тут ни при чём."""


def configured() -> bool:
    return _REQ.is_file()


def _load_request() -> dict:
    if not _REQ.is_file():
        raise NotConfigured(
            "запрос feature.fm не перенесён: DevTools → Copy as cURL (bash) → "
            "python tools/curl_import.py featurefm --file curl.txt")
    return json.loads(_REQ.read_text(encoding="utf-8"))


def _cache_read(key: str) -> dict | None:
    try:
        d = json.loads(_CACHE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    ent = d.get(key)
    if not ent or (time.time() - float(ent.get("ts", 0))) > _TTL:
        return None
    return ent.get("data")


def _cache_write(key: str, data: dict) -> None:
    try:
        _CACHE.parent.mkdir(parents=True, exist_ok=True)
        d = {}
        if _CACHE.is_file():
            d = json.loads(_CACHE.read_text(encoding="utf-8"))
        d[key] = {"ts": time.time(), "data": data}
        _CACHE.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def _looks_like_login(resp_text: str, status: int) -> bool:
    """Ответ — это страница входа, а не данные?

    Сервисы на куках редко отвечают честным 401: чаще приезжает 200 с формой
    входа или редирект. Принять такое за «ничего не найдено» — самый дорогой
    способ ошибиться.
    """
    if status in (401, 403):
        return True
    low = resp_text[:2000].lower()
    return ("login" in low and "password" in low) or "signin" in low


async def lookup(code: str, *, fresh: bool = False) -> dict:
    """Спросить кабинет про ISRC или UPC. Возвращает разобранный ответ.

    Ключ подставляется в сохранённый запрос: и в адрес, и в тело — кабинет у
    разных экранов шлёт его по-разному, а угадывать, где именно, значит
    молча получать пустой ответ.
    """
    import httpx

    code = (code or "").strip()
    if not code:
        return {}
    if not fresh:
        hit = _cache_read(code)
        if hit is not None:
            return hit

    req = _load_request()
    url = req["url"].replace("__CODE__", code)
    body = (req.get("body") or "").replace("__CODE__", code)
    headers = dict(req.get("headers") or {})
    cookies = dict(req.get("cookies") or {})

    async with httpx.AsyncClient(timeout=30, follow_redirects=False) as c:
        r = await c.request(req.get("method", "GET"), url, headers=headers,
                            cookies=cookies, content=body or None)
    text = r.text
    if _looks_like_login(text, r.status_code):
        raise SessionExpired(
            "feature.fm вернул страницу входа — кука сессии протухла; "
            "перенеси свежий запрос (tools/curl_import.py featurefm)")

    try:
        data = r.json()
    except Exception:  # noqa: BLE001
        data = {"_raw": text[:4000]}
    out = {"code": code, "status": r.status_code, "data": data}
    _cache_write(code, out)
    return out


def extract_ids(payload: dict) -> dict:
    """Достать ISRC/UPC из ответа, где бы они ни лежали.

    Форма ответа кабинета заранее не известна и может смениться без
    предупреждения, поэтому ищем по ИМЕНАМ полей на любой глубине, а не по
    жёсткому пути. Нашлось несколько — возвращаем все: выбор одного из них уже
    решение вызывающего.
    """
    found: dict[str, set] = {"isrc": set(), "upc": set()}

    def walk(node) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                lk = str(k).lower()
                if isinstance(v, (str, int)) and str(v).strip():
                    if lk in ("isrc", "isrccode", "isrc_code"):
                        found["isrc"].add(str(v).strip().upper())
                    elif lk in ("upc", "upccode", "upc_code", "ean", "barcode"):
                        found["upc"].add(str(v).strip())
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(payload)
    return {"isrc": sorted(found["isrc"]), "upc": sorted(found["upc"])}
