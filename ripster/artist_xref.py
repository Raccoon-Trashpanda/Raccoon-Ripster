"""Один артист — разные id в разных каталогах.

Радар устроен по сервисам: каждый источник читает подписки СВОЕГО аккаунта и
отдаёт релизы оттуда же. Из-за этого артист виден только тому источнику, в
котором на него подписаны, и витрина, где релиз появляется РАНЬШЕ всех, до ленты
не доходит вовсе.

Живой разбор (29.07.2026, Sultan + Shepard — «Centuries»): релиз лежал в Tidal NZ
за сутки до мировой даты, `streamReady=True`, LOSSLESS; в вишлисте артист записан
со службой apple, у Apple релиза ещё не было — радар честно показывал пустоту, а
Tidal-источник молчал, потому что в служебном Tidal-аккаунте 0 подписок. Ровно то
преимущество, ради которого новозеландский аккаунт и заводился, не использовалось.

Этот модуль убирает связку «где следим» ↔ «где артист записан»: по имени артиста
находит его id в любом каталоге и кэширует находку на диск. Дальше любой источник
радара может спросить свою витрину про артистов, за которыми следят в других
сервисах.

Сверка ТОЛЬКО по точному совпадению нормализованного имени. Поиск охотно отдаёт
соседей — на «Sultan + Shepard» приходят «Sultan & Ned Shepard» и «Sultan +
Shepard feat. Nadia Ali», и любой из них, принятый за артиста, наполнил бы ленту
чужими релизами. Лучше не найти, чем найти не того: не найденное чинится ISRC/UPC
на следующем шаге, а подмена молча портит и ленту, и авто-скачивание.
"""
from __future__ import annotations

import json
import re
import time
import unicodedata
from pathlib import Path
from typing import Optional

import httpx

# Каталоги, в которых умеем искать артиста по имени. Apple сюда не входит
# сознательно: его id уже лежит в записи вишлиста (`artist_id`), а публичный
# iTunes-поиск отдаёт совсем другие идентификаторы.
SERVICES = ("tidal", "qobuz", "deezer")

_TIDAL_API = "https://api.tidal.com/v1"
_QOBUZ_API = "https://www.qobuz.com/api.json/0.2"

# Найденное не протухает: id артиста в каталоге не меняется. А вот «не нашли»
# перепроверяем — артиста могли просто ещё не завести в этой витрине.
_NEG_TTL = 14 * 24 * 3600

_cfg: dict = {}
_base_dir: Path = Path(".")
_cache: dict = {}
_loaded = False


def configure(cfg: dict, base_dir: Path) -> None:
    global _cfg, _base_dir
    _cfg, _base_dir = cfg, Path(base_dir)


def _cache_path() -> Path:
    return _base_dir / "artist_xref.json"


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
        _cache_path().write_text(json.dumps(_cache, ensure_ascii=False),
                                 encoding="utf-8")
    except Exception as e:                                          # noqa: BLE001
        print(f"[xref] cache save failed: {e}", flush=True)


def norm(name: str) -> str:
    """Имя артиста в виде, пригодном для сравнения между каталогами.

    Диакритика снимается («Ørjan Nilsen» и «Orjan Nilsen» — один человек), а
    соединители приводятся к одному слову: «Sultan + Shepard», «Sultan &
    Shepard» и «Sultan and Shepard» пишутся в витринах вперемешку. При этом
    «Sultan & Ned Shepard» остаётся ОТДЕЛЬНЫМ именем — лишнее слово никуда не
    девается, и это тот случай, ради которого сверка вообще существует.
    """
    s = unicodedata.normalize("NFKD", str(name or ""))
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.replace("Ø", "O").replace("ø", "o").replace("Đ", "D").replace("đ", "d")
    s = s.replace("ß", "ss").replace("Æ", "AE").replace("æ", "ae")
    s = s.lower()
    s = re.sub(r"[&+]", " and ", s)
    s = re.sub(r"\bfeat\.?\b|\bfeaturing\b|\bvs\.?\b|\bpres\.?\b", " ", s)
    s = re.sub(r"[^\w\s]", " ", s, flags=re.UNICODE)
    return re.sub(r"\s+", " ", s).strip()


def _key(service: str, name: str) -> str:
    return f"{service}|{norm(name)}"


def _cached(service: str, name: str) -> Optional[dict]:
    _load()
    rec = _cache.get(_key(service, name))
    if not rec:
        return None
    if rec.get("miss"):
        if (time.time() - float(rec.get("ts") or 0)) < _NEG_TTL:
            return rec
        return None
    return rec


def _remember(service: str, name: str, rec: Optional[dict]) -> None:
    _load()
    _cache[_key(service, name)] = dict(rec or {"miss": True}, ts=time.time())
    _save()


def _pick_exact(name: str, candidates: list, id_key: str = "id",
                name_key: str = "name") -> Optional[dict]:
    """Из выдачи поиска взять ТОГО САМОГО артиста — или никого.

    ЖЁСТКИЕ МЕРЫ (03.09.2026). Раньше брался первый по имени, и при коллизии
    (BOP, Solomon Grey — таких артистов несколько в каждом каталоге) в радар
    подмешивались релизы ЧУЖОГО. Чужой релиз в ленте вводит в заблуждение
    сильнее, чем отсутствие сшивки: пропущенный кросс-сервисный релиз просто
    невидим. Поэтому при неоднозначности: берём заметно популярнейшего
    (обычно именно его и имеют в виду), а если по популярности не различить —
    НЕ сшиваем вовсе.
    """
    want = norm(name)
    hits = [a for a in (candidates or [])
            if norm(a.get(name_key, "")) == want and a.get(id_key)]
    if not hits:
        return None
    if len(hits) == 1:
        a = hits[0]
        return {"id": str(a.get(id_key)), "name": a.get(name_key, "")}

    def _pop(a: dict) -> int:
        for k in ("nb_fan", "popularity", "nb_album", "albumsCount", "albums_count"):
            v = a.get(k)
            if isinstance(v, (int, float)) and v > 0:
                return int(v)
        return 0

    ranked = sorted(hits, key=_pop, reverse=True)
    top, second = _pop(ranked[0]), _pop(ranked[1])
    # Различаем только если у лидера есть популярность И он заметно впереди
    # (в 3+ раза или второй вовсе без метрики). Иначе — воздерживаемся.
    if top > 0 and (second == 0 or top >= second * 3):
        a = ranked[0]
        return {"id": str(a.get(id_key)), "name": a.get(name_key, "")}
    print(f"[xref] «{name}»: {len(hits)} одноимённых артиста, по популярности не "
          f"различить — сшивку пропускаю (лучше пропуск, чем чужой релиз)", flush=True)
    return None


# ── Поиск по каталогам ────────────────────────────────────────────────────────

async def _search_tidal(c: httpx.AsyncClient, name: str) -> Optional[dict]:
    token = str((_cfg or {}).get("tidal-token") or "").strip()
    if not token:
        return None
    cc = str((_cfg or {}).get("tidal-country") or "US").strip().upper() or "US"
    r = await c.get(f"{_TIDAL_API}/search",
                    params={"query": name, "limit": 10, "countryCode": cc,
                            "types": "ARTISTS"},
                    headers={"Authorization": f"Bearer {token}"})
    if r.status_code != 200:
        return None
    hit = _pick_exact(name, (r.json().get("artists") or {}).get("items") or [])
    if hit:
        hit["url"] = f"https://listen.tidal.com/artist/{hit['id']}"
    return hit


async def _search_qobuz(c: httpx.AsyncClient, name: str) -> Optional[dict]:
    token = str((_cfg or {}).get("qobuz-auth-token") or "").strip()
    if not token:
        return None
    app_id = str((_cfg or {}).get("qobuz-app-id") or "").strip() or "312369995"
    r = await c.get(f"{_QOBUZ_API}/artist/search",
                    params={"query": name, "limit": 10, "app_id": app_id},
                    headers={"X-User-Auth-Token": token, "X-App-Id": app_id})
    if r.status_code != 200:
        return None
    hit = _pick_exact(name, (r.json().get("artists") or {}).get("items") or [])
    if hit:
        hit["url"] = f"https://open.qobuz.com/artist/{hit['id']}"
    return hit


async def _search_deezer(c: httpx.AsyncClient, name: str) -> Optional[dict]:
    r = await c.get("https://api.deezer.com/search/artist",
                    params={"q": name, "limit": 10})
    if r.status_code != 200:
        return None
    hit = _pick_exact(name, (r.json().get("data") or []))
    if hit:
        hit["url"] = f"https://www.deezer.com/artist/{hit['id']}"
    return hit


_SEARCH = {"tidal": _search_tidal, "qobuz": _search_qobuz, "deezer": _search_deezer}


def has_credentials(service: str) -> bool:
    """Есть ли чем спрашивать каталог. Без этого «не нашли» — не факт, а враньё."""
    c = _cfg or {}
    if service == "tidal":
        return bool(str(c.get("tidal-token") or "").strip())
    if service == "qobuz":
        return bool(str(c.get("qobuz-auth-token") or "").strip())
    if service == "deezer":
        return True                      # публичный поиск, токен не нужен
    return False


async def resolve(name: str, service: str,
                  client: Optional[httpx.AsyncClient] = None) -> Optional[dict]:
    """id артиста в каталоге `service`, или None.

    Кэш на диске: найденное навсегда, ненайденное — на две недели (артиста могли
    ещё не завести в этой витрине).
    """
    if not name or service not in _SEARCH or not has_credentials(service):
        return None

    rec = _cached(service, name)
    if rec is not None:
        return None if rec.get("miss") else rec

    own = client is None
    c = client or httpx.AsyncClient(timeout=httpx.Timeout(connect=10, read=20,
                                                          write=10, pool=5))
    try:
        hit = await _SEARCH[service](c, name)
    except Exception as e:                                          # noqa: BLE001
        print(f"[xref] {service} «{name}»: {e}", flush=True)
        return None                      # сетевой сбой не кэшируем как «нет такого»
    finally:
        if own:
            await c.aclose()

    _remember(service, name, hit)
    return hit


async def resolve_many(names: list, service: str,
                       client: Optional[httpx.AsyncClient] = None,
                       concurrency: int = 4) -> dict:
    """Резолв пачки имён. Возвращает {имя: {"id", "name", "url"}} только по найденным."""
    import asyncio

    if not names or not has_credentials(service):
        return {}

    own = client is None
    c = client or httpx.AsyncClient(timeout=httpx.Timeout(connect=10, read=20,
                                                          write=10, pool=5))
    sem = asyncio.Semaphore(concurrency)
    out: dict = {}

    async def _one(nm: str) -> None:
        async with sem:
            hit = await resolve(nm, service, c)
        if hit:
            out[nm] = hit

    try:
        await asyncio.gather(*(_one(n) for n in names), return_exceptions=True)
    finally:
        if own:
            await c.aclose()
    return out
