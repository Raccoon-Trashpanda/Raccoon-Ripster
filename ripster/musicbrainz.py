"""MusicBrainz: канонический артист + дизамбигуация по жанру + ссылки на сервисы.

Зачем (13.09.2026, владелец: «левые артисты по одному имени»). Резолв артиста
по ИМЕНИ путает одноимённых: для «Bop» брался популярный (Kidz Bop / поп-группы),
а нужен был нишевый Ambient Drum & Bass. Отбор по имени осторожен, но
без канонической идентичности «какой именно Bop» не угадать.

MusicBrainz это решает: поиск возвращает КАЖДОГО «Bop» со своим `disambiguation`
(«Ambient Drum & Bass artist») и тегами жанра — по ним видно, кто нужен. А в
`url-rels` у артиста лежат прямые ссылки на Deezer/Tidal/Spotify/… — то есть
можно взять ИМЕННО его id в витрине, а не угадывать поиском по имени.

ОГРАНИЧЕНИЯ, вокруг которых построено:
  • MusicBrainz жёстко лимитит: не чаще 1 запроса в секунду с одного IP, иначе
    503. Поэтому здесь — сериализация вызовов с паузой и ОБЯЗАТЕЛЬНЫЙ дисковый
    кэш: ответ по имени+жанру меняется редко.
  • Требует честный User-Agent с контактом — без него банят.
  • Не у каждого артиста есть ссылки на сервисы в relations; если их нет, вызов
    честно вернёт пусто, и решение остаётся за прежним путём (name-search).
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Optional

_MB = "https://musicbrainz.org/ws/2"
#: MusicBrainz требует контактный UA. Без «настоящего» вида — 403/бан.
_UA = "Ripster/1.0 ( https://github.com/Raccoon-Trashpanda )"

#: Их лимит — 1 req/сек. Держим паузу с запасом и сериализуем обращения.
_MIN_INTERVAL = 1.2
_lock = threading.Lock()
_last_call = 0.0

_base_dir: Path = Path(".")
_cache: dict = {}
_loaded = False
#: id найдены навсегда, промахи — на 2 недели (артиста могли ещё не завести).
_TTL_MISS = 14 * 24 * 3600.0

#: Хосты сервисов в url-rels → наш ключ + как достать id из ссылки.
_SVC_HOST = {
    "deezer.com": "deezer", "tidal.com": "tidal", "listen.tidal.com": "tidal",
    "open.spotify.com": "spotify", "music.apple.com": "apple",
    "soundcloud.com": "soundcloud", "music.yandex": "yandex",
    "qobuz.com": "qobuz",
}

#: Витрины, чьи id мы вообще умеем доставать из ссылок (Apple — страница
#: подписки, её id лежит в вишлисте, поэтому и в разборе он участвует).
_SVC_ORDER = ("apple", "spotify", "deezer", "tidal", "qobuz")


def configure(base_dir: Path) -> None:
    global _base_dir, _loaded
    _base_dir = base_dir
    _loaded = False


def _cache_path() -> Path:
    p = _base_dir / "dist" / "musicbrainz_xref.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _load() -> None:
    global _cache, _loaded
    if _loaded:
        return
    try:
        _cache = json.loads(_cache_path().read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        _cache = {}
    _loaded = True


def _save() -> None:
    try:
        _cache_path().write_text(json.dumps(_cache, ensure_ascii=False, indent=1),
                                 encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def _throttled_get(path: str, params: dict) -> Optional[dict]:
    """GET к MB с сериализацией и паузой 1.2с. None — сеть/лимит/не-JSON."""
    import httpx
    global _last_call
    with _lock:
        wait = _MIN_INTERVAL - (time.time() - _last_call)
        if wait > 0:
            time.sleep(wait)
        try:
            r = httpx.get(f"{_MB}/{path}", params={**params, "fmt": "json"},
                          headers={"User-Agent": _UA}, timeout=20)
        except Exception:  # noqa: BLE001
            return None
        finally:
            _last_call = time.time()
    if r.status_code != 200:
        return None
    try:
        return r.json()
    except Exception:  # noqa: BLE001
        return None


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def _genre_score(cand: dict, genre_hint: str) -> int:
    """Насколько кандидат подходит под жанр-подсказку: совпало слов в тегах +
    в disambiguation. 0 — не подтверждён (не значит «чужой»)."""
    if not genre_hint:
        return 0
    hint = {w for w in re.split(r"[^a-z0-9]+", genre_hint.lower()) if len(w) >= 3}
    if not hint:
        return 0
    hay = " ".join([t.get("name", "") for t in (cand.get("tags") or [])]
                   + [cand.get("disambiguation", "")]).lower()
    return sum(1 for w in hint if w in hay)


def search_artist(name: str, genre_hint: str = "") -> Optional[dict]:
    """Канонический артист по имени, разведённый по жанру.

    Возвращает {mbid, name, disambiguation, tags, score, genre_match} лучшего
    кандидата или None. При наличии `genre_hint` предпочитает того, чьи теги/
    disambiguation совпали с жанром (так нишевый Bop-DnB бьёт популярный Kidz
    Bop); без подсказки — по score MusicBrainz.
    """
    name = (name or "").strip()
    if not name:
        return None
    _load()
    key = f"a::{_norm(name)}::{_norm(genre_hint)}"
    ent = _cache.get(key)
    if ent is not None:
        if ent.get("miss"):
            if time.time() - float(ent.get("ts", 0)) <= _TTL_MISS:
                return None
        else:
            return ent

    data = _throttled_get("artist", {"query": name, "limit": 8})
    if data is None:
        return None      # сеть/лимит (503) — НЕ кэшируем как «нет такого»
    cands = data.get("artists") or []
    exact = [a for a in cands if _norm(a.get("name", "")) == _norm(name)] or cands
    if not exact:
        _cache[key] = {"miss": True, "ts": time.time()}
        _save()
        return None

    def rank(a: dict) -> tuple:
        return (_genre_score(a, genre_hint), int(a.get("score", 0)))

    best = max(exact, key=rank)
    out = {
        "mbid": best.get("id"),
        "name": best.get("name", ""),
        "disambiguation": best.get("disambiguation", ""),
        "tags": [t.get("name") for t in (best.get("tags") or [])][:6],
        "score": int(best.get("score", 0)),
        "genre_match": _genre_score(best, genre_hint),
    }
    _cache[key] = out
    _save()
    return out


def artist_service_ids(mbid: str) -> dict:
    """{service: {'url':…, 'id':…, 'ids':[…]}} из url-rels артиста.

    `ids` — ВСЕ id этой витрины, которые у артиста есть, а не первый: Robert
    Hood висит на двух deezer-id (182613 и 284523761) одновременно, и, оставив
    в карте один, мы сами лишим себя доказательства, что это ОДИН человек, а
    не два однофамильца. `id` оставлен первым — прежний контракт вызывающих.

    Пусто — ссылок нет (это «не знаю», а не «артиста там нет»).
    """
    mbid = (mbid or "").strip()
    if not mbid:
        return {}
    _load()
    # Ключ со сменой формата: старые записи держат по одному id на витрину, и
    # читать их новым кодом — значит молча вернуть прежнюю слепоту.
    key = f"rels2::{mbid}"
    ent = _cache.get(key)
    if ent is not None and not ent.get("miss"):
        return ent.get("svc") or {}

    data = _throttled_get(f"artist/{mbid}", {"inc": "url-rels"})
    if data is None:
        return {}      # сеть/лимит — не кэшируем как «нет»
    svc: dict = {}
    for rel in data.get("relations", []):
        url = ((rel.get("url") or {}).get("resource") or "").strip()
        low = url.lower()
        for host, name in _SVC_HOST.items():
            if host not in low:
                continue
            m = re.search(r"/(?:artist|user)/([A-Za-z0-9._-]+)", url)
            sid = (m.group(1) if m else "").rstrip(".")
            rec = svc.setdefault(name, {"url": url, "id": sid, "ids": []})
            if sid and sid not in rec["ids"]:
                rec["ids"].append(sid)
            break
    _cache[key] = {"svc": svc, "ts": time.time()}
    _save()
    return svc


def artist_variants(name: str) -> list:
    """ВСЕ канонические артисты с этим именем, а не лучший.

    Резолв «имя → один артист» и есть причина жалобы: у «Sasha» в MB живут UK
    диджей и немецкая поп-певица, у «Marina» — японская певица и LP, и выбор
    любого одного по популярности приписывает подписке чужого человека. Здесь
    ничего не выбирается: наружу идут все варианты со своими ссылками, а решают
    по ним признаки (`artist_identity`).
    """
    name = (name or "").strip()
    if not name:
        return []
    _load()
    key = f"variants::{_norm(name)}"
    ent = _cache.get(key)
    if ent is not None:
        if ent.get("miss"):
            if time.time() - float(ent.get("ts", 0)) <= _TTL_MISS:
                return []
        else:
            return ent.get("items") or []
    data = _throttled_get("artist", {"query": f'artist:"{name}"', "limit": 15})
    if data is None:
        return []                      # сеть/лимит — не кэшируем как «нет»
    items = [{"mbid": a.get("id"),
              "name": a.get("name", ""),
              "disambiguation": a.get("disambiguation", ""),
              "tags": [t.get("name") for t in (a.get("tags") or [])][:6],
              "score": int(a.get("score", 0)),
              "svc": artist_service_ids(a.get("id") or "")}
             for a in (data.get("artists") or [])
             if _norm(a.get("name", "")) == _norm(name)]
    if not items:
        _cache[key] = {"miss": True, "ts": time.time()}
    else:
        _cache[key] = {"items": items, "ts": time.time()}
    _save()
    return items


def person_index(name: str) -> dict:
    """{витрина: {id: [mbid, …]}} — чем MB считает каждого носителя имени.

    Отдаётся СПИСКОМ mbid намеренно: один и тот же deezer-id у MB висит сразу
    под двумя артистами (так склеено «Sasha»), и это не доказательство
    тождества, а доказательство НЕОДНОЗНАЧНОСТИ. Вызывающий по этому признаку
    обязан уйти в `needs_owner`, а не слить двух людей в одного.
    """
    out: dict = {}
    for v in artist_variants(name):
        mbid = str(v.get("mbid") or "")
        if not mbid:
            continue
        for svc, rec in (v.get("svc") or {}).items():
            ids = (rec or {}).get("ids") or [((rec or {}).get("id"))]
            bucket = out.setdefault(svc, {})
            for sid in ids:
                sid = str(sid or "")
                if sid and mbid not in bucket.setdefault(sid, []):
                    bucket[sid].append(mbid)
    return out


def resolve_service_id(name: str, service: str, genre_hint: str = "") -> Optional[dict]:
    """id артиста в `service`, опознанный через MusicBrainz по имени+жанру.

    Это ТОЧНЫЙ путь: MB разводит одноимённых по жанру и отдаёт прямую ссылку на
    витрину. Нет MB-записи / нет ссылки на этот сервис → None.
    Отсюда уходит только КАНДИДАТ: `artist_identity` сверяет его работы с
    работами подписки, и одного имени для принятия мало.
    """
    art = search_artist(name, genre_hint)
    if not art or not art.get("mbid"):
        return None
    svc = artist_service_ids(art["mbid"])
    hit = svc.get(service)
    if not hit or not hit.get("id"):
        return None
    return {"id": hit["id"], "name": art["name"], "url": hit["url"],
            "via": "musicbrainz", "mbid": art["mbid"],
            "disambiguation": art.get("disambiguation", "")}
