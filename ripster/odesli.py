"""
Odesli (song.link) — опознание релиза по ссылке ЛЮБОГО сервиса.

Зачем он нам вообще нужен. Наш собственный резолвер
(`routes/discovery._resolve_release_id`) знает шесть хостов: apple, deezer,
qobuz, tidal, spotify, beatport. Ссылка с YouTube Music, Amazon, Яндекса,
Napster или Pandora для него — `url:`-фолбэк, то есть идентификатора нет, и
матрица доступности честно отвечает «не спрашивал» про все сервисы разом.
Odesli знает эти площадки и отдаёт по ним штрихкод.

ЧТО СЛУЧИЛОСЬ С ИХ API (замер 06.09.2026)

Публичный API закрыт. Все четыре формы вызова отвечают одинаково::

    GET https://api.song.link/v1-alpha.1/links?url=...&userCountry=US   401
    GET https://api.song.link/v1-alpha.1/links?url=...                  401
    GET https://api.odesli.co/v1-alpha.1/links?url=...                  401
    GET https://api.song.link/v1-alpha.1/links?id=..&platform=..        401
    → {"statusCode":401,"code":"PUBLIC_API_ACCESS_DEPRECATED"}

Namespace `v1-alpha.1` выведён из обращения 31.07.2026; ключ выдают по запросу
в переписке. Пока ключа нет, остаётся публичная страница song.link — она
отвечает 200 и несёт те же данные во встроенном `__NEXT_DATA__`.

ПОЭТОМУ ЗДЕСЬ РОВНО ОДНА ФУНКЦИЯ И ЖЁСТКИЕ ГРАНИЦЫ

  • Это ПОСЛЕДНИЙ ход, а не первый. Зовётся, только когда наши собственные
    резолверы идентификатора не дали. Свои источники точнее: они отвечают про
    наши учётки и наши регионы, а Odesli — про мир вообще.
  • Отсюда берём ТОЛЬКО идентификаторы и чужие ссылки. Ни одно «есть на
    платформе X» отсюда не становится нашим `available`: доступность у нас
    означает «эта учётка может скачать сейчас», и знать этого Odesli не может.
    Проверяем сами — тем же `availability._probe_one`.
  • Разбор страницы хрупок по определению. Любая неудача — это `None`, а не
    выдумка: пустой результат честен, придуманный штрихкод отравит кэш
    матрицы, и разгребать это будет некому.
  • Кэш на диске и обязателен. Дёргать чужую страницу на каждый чих нельзя ни
    по совести, ни по здравому смыслу — ответ для ссылки не меняется.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Optional

_PAGE = "https://song.link/"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
_NEXT = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)

# Ответ по ссылке не меняется, но и вечно держать его незачем: релиз может
# доехать до новых площадок. Неделя — компромисс, за который ничего не портится.
_TTL = 7 * 24 * 3600

_base_dir: Path = Path(".")
_cache: dict = {}
_loaded = False
_enabled = True


def configure(base_dir: Path, enabled: bool = True) -> None:
    """`enabled=False` выключает поход наружу целиком (для офлайна и тестов)."""
    global _base_dir, _enabled
    _base_dir, _enabled = Path(base_dir), bool(enabled)


def _cache_path() -> Path:
    return _base_dir / "odesli_cache.json"


def _load() -> None:
    global _cache, _loaded
    if _loaded:
        return
    try:
        _cache = json.loads(_cache_path().read_text(encoding="utf-8"))
    except Exception:                                          # noqa: BLE001
        _cache = {}
    _loaded = True


def _save() -> None:
    try:
        _cache_path().write_text(json.dumps(_cache, ensure_ascii=False),
                                 encoding="utf-8")
    except Exception:                                          # noqa: BLE001
        pass


async def identify(url: str) -> Optional[dict]:
    """Что это за релиз по ссылке любого сервиса.

    Возвращает ``{upc, isrc, type, title, artist, provider, links{...}}`` либо
    ``None``. ``None`` значит «не удалось узнать» — и это единственная честная
    форма незнания: подставлять сюда пустой штрихкод нельзя, его примут за
    ответ.
    """
    u = (url or "").strip()
    if not u or not _enabled:
        return None
    _load()
    hit = _cache.get(u)
    if hit and time.time() - float(hit.get("ts") or 0) < _TTL:
        return hit.get("data")

    data = None
    try:
        from ripster import http_client as _HTTP
        async with _HTTP.ashared() as c:
            r = await c.get(_PAGE + u, headers={"User-Agent": _UA},
                            follow_redirects=True)
        if r.status_code == 200:
            data = _parse(r.text)
    except Exception as e:                                     # noqa: BLE001
        print(f"[odesli] {type(e).__name__}: {e}", flush=True)
        return None

    # Отрицательный ответ кэшируем тоже: иначе неизвестная ссылка будет ходить
    # наружу при каждом обращении к карточке.
    _cache[u] = {"ts": time.time(), "data": data}
    _save()
    return data


def _parse(html: str) -> Optional[dict]:
    m = _NEXT.search(html)
    if not m:
        return None
    try:
        pd = json.loads(m.group(1))["props"]["pageProps"]["pageData"]
    except Exception:                                          # noqa: BLE001
        return None
    ed = pd.get("entityData") or {}
    links: dict = {}
    for sec in (pd.get("sections") or []):
        for it in (sec.get("links") or []):
            name = it.get("platform") or it.get("id") or ""
            href = it.get("url") or ""
            if name and href:
                links[name] = href
    upc = "".join(ch for ch in str(ed.get("upc") or "") if ch.isdigit())
    isrc = str(ed.get("isrc") or "").strip().upper()
    if not (upc or isrc or links):
        return None                    # страница есть, толку нет — это не ответ
    return {
        "upc": upc,
        "isrc": isrc,
        "type": ed.get("type") or "",
        "title": ed.get("title") or "",
        "artist": ed.get("artistName") or "",
        "provider": ed.get("provider") or "",
        "links": links,
    }
