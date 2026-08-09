"""Radar sources beyond Spotify: BBC shows, SoundCloud channels, Apple artists.

The releases view already merges several services into one feed and already has
per-source toggles — what was missing were the sources themselves. Each endpoint
here returns the SAME item shape the Spotify radar produces, so the frontend can
drop them straight into the same list:

    {id, title, artist, artist_id, type, group, date, year,
     tracks, cover, url, service}

What each source watches, and why that is the right thing to watch:

  bbc         the shows in bbc.BRANDS (Essential Mix, Pete Tong, …). A show is a
              standing thing you follow; a new episode is the release.
  soundcloud  the channels already on the watchlist — following a channel and
              wanting its uploads in the feed are the same intent, so there is no
              second list to maintain.
  apple       the Apple artists on the watchlist, via the compilation-aware
              lookup in watchlist.py, so label compilations show up here too
              (see ripster/compilations.py for why that needs saying).

Every source is best-effort and independent: one failing service returns an empty
list with an `error`, and the rest of the feed still renders. Results are cached
briefly because these are third-party APIs and the view refetches on every filter
change; the cache is persisted and served stale-while-revalidate, so a restart
does not make the user wait for a cold refetch.
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta

from fastapi import APIRouter, Query

from ripster import compilations as _comps

router = APIRouter()
_s: dict = {}

_CACHE_TTL = 900          # 15 min — a new episode/upload is not a per-second event
_cache: dict = {}         # key -> (ts, payload)
_refreshing: set = set()  # keys with a background refresh already in flight


def install(app, ctx) -> None:
    _s.update({
        "config":    ctx.config,
        "watchlist": ctx.watchlist,
        "cache_file": ctx.base_dir / "radar_cache.json",
        "favs_file":  ctx.base_dir / "rel_favorites.json",
        "store_file": ctx.base_dir / "radar_store.json",
    })
    _load_cache()
    app.include_router(router)


# ── Избранные релизы: живут на сервере, а не в браузере ─────────────────────
# Звезда на карточке писалась в localStorage — то есть в конкретный браузерный
# профиль. У Ripster таких профилей минимум два: окно программы (WebView2) и
# запасной ярлык в обычном браузере, и списки у них разные. Плюс любая чистка
# данных сайта стирала избранное молча. Со стороны владельца это выглядело как
# «жму в избранное не первый раз, Ripster не помнит» (01.08.2026).
#
# Храним на сервере: одно избранное на всю программу, переживает перезапуск,
# смену оболочки и переустановку браузера.
_FAV_CAP = 1000


def _favs_load() -> list:
    f = _s.get("favs_file")
    if not f or not f.exists():
        return []
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
        return d if isinstance(d, list) else (d.get("items") or [])
    except Exception as e:
        print(f"[radar] favorites load error: {e}", flush=True)
        return []


def _favs_save(items: list) -> None:
    f = _s.get("favs_file")
    if not f:
        return
    try:
        f.write_text(json.dumps(items[:_FAV_CAP], ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        print(f"[radar] favorites save error: {e}", flush=True)


def _fav_uid(rel: dict) -> str:
    """Тот же ключ, что считает интерфейс, иначе списки разъедутся."""
    return (str(rel.get("service") or "") + "|"
            + str(rel.get("id") or rel.get("url")
                  or f"{rel.get('title') or ''}~{rel.get('artist') or ''}"))


# ── Долговременный склад релизов не-Spotify источников ──────────────────────
# У Spotify есть склад по артистам и снимки уже отданных лент, поэтому находка
# там переживает и перезапуск, и сбой сети. У BBC/SoundCloud/Apple не было
# ничего, кроме 15-минутного кэша: источник отдаёт только своё недавнее окно, и
# всё, что из него выпало, исчезало НАВСЕГДА — вернуть было неоткуда.
#
# Для владельца это выглядело как «карточка была и пропала»: релиз, найденный
# 24 июля, к августу выпадал из окна источника и не возвращался никаким
# обновлением (01.08.2026, разбор пропавшей карточки PROFF).
#
# Склад чинит именно это: всё once-увиденное складывается на диск и подмешивается
# к свежей выдаче. Источник замолчал — записи остаются; вернулся — обновляются.
_STORE_WINDOW_DAYS = 400          # столько храним; дальше запись не нужна никому
_STORE_CAP = 4000                 # на источник — чтобы файл не пух бесконечно


def _rel_uid(r: dict) -> str:
    return (str(r.get("service") or "") + "|"
            + str(r.get("id") or r.get("url")
                  or f"{r.get('artist') or ''}~{r.get('title') or ''}"))


def _durable_load() -> dict:
    f = _s.get("store_file")
    if not f or not f.exists():
        return {}
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception as e:
        print(f"[radar] store load error: {e}", flush=True)
        return {}


def _durable_save(store: dict) -> None:
    f = _s.get("store_file")
    if not f:
        return
    try:
        f.write_text(json.dumps(store, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        print(f"[radar] store save error: {e}", flush=True)


def _durable_merge(source: str, fresh: list, days: int) -> list:
    """Слить свежую выдачу источника со складом и отдать окно в `days`.

    Свежие записи ОБНОВЛЯЮТ складские (у релиза могла уточниться дата или
    обложка), но никогда их не удаляют: отсутствие в текущем ответе означает
    лишь «источник больше не показывает», а не «релиза не было».
    """
    store = _durable_load()
    bucket = store.get(source) or {}
    for r in (fresh or []):
        if r.get("date"):
            bucket[_rel_uid(r)] = r

    keep = _cutoff(_STORE_WINDOW_DAYS)
    bucket = {k: v for k, v in bucket.items() if (v.get("date") or "") >= keep}
    if len(bucket) > _STORE_CAP:
        newest = sorted(bucket.items(), key=lambda kv: kv[1].get("date", ""), reverse=True)
        bucket = dict(newest[:_STORE_CAP])

    store[source] = bucket
    _durable_save(store)

    cut = _cutoff(days)
    out = [r for r in bucket.values() if (r.get("date") or "") >= cut]
    out.sort(key=lambda x: x.get("date", ""), reverse=True)
    restored = len(out) - sum(1 for r in (fresh or []) if (r.get("date") or "") >= cut)
    if restored > 0:
        print(f"[radar] {source}: source gave {len(fresh or [])}, added from store "
              f"{restored} (window {days}d)", flush=True)
    return out


@router.get("/api/rel-favs")
async def rel_favs_get():
    return {"ok": True, "items": _favs_load()}


@router.post("/api/rel-favs")
async def rel_favs_post(body: dict):
    """Добавить/убрать релиз, либо влить список целиком.

    `merge` нужен ровно один раз на человека: старое избранное лежит в
    localStorage, и при первом запуске новой версии интерфейс переливает его
    сюда. Слияние идёт по ключу и ничего не затирает.
    """
    items = _favs_load()
    have = {_fav_uid(r) for r in items}

    if body.get("merge"):
        added = 0
        for rel in (body.get("items") or []):
            u = _fav_uid(rel)
            if u not in have:
                have.add(u)
                items.append(rel)
                added += 1
        _favs_save(items)
        return {"ok": True, "items": items, "added": added}

    rel = body.get("item") or {}
    uid = body.get("uid") or _fav_uid(rel)
    if body.get("remove"):
        items = [r for r in items if _fav_uid(r) != uid]
    elif uid not in have and rel:
        items.insert(0, rel)
    _favs_save(items)
    return {"ok": True, "items": items}


# ── Cache: survives a restart, and never makes the user wait ─────────────────
# A cold source costs ~2.5s (BBC polls 11 shows, Apple two lookups per artist).
# In-memory only, that price was paid again after every restart, and a stale
# entry made the user wait for a refetch before seeing anything. So: persist it,
# and serve what we have immediately while refreshing behind the request.

def _load_cache() -> None:
    f = _s.get("cache_file")
    try:
        if f and f.exists():
            raw = json.loads(f.read_text(encoding="utf-8")) or {}
            for k, v in raw.items():
                if isinstance(v, list) and len(v) == 2:
                    _cache[k] = (float(v[0]), v[1])
            print(f"[radar] cache loaded: {len(_cache)} sources", flush=True)
    except Exception as e:
        print(f"[radar] cache load failed: {e}", flush=True)


def _save_cache() -> None:
    f = _s.get("cache_file")
    if not f:
        return
    try:
        f.write_text(json.dumps({k: [ts, p] for k, (ts, p) in _cache.items()},
                                ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        print(f"[radar] cache save failed: {e}", flush=True)


def _cutoff(days: int) -> str:
    return (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")


def _serve_or_refresh(key: str, builder):
    """Fresh → return it. Stale → return the stale copy NOW and refresh behind
    the request. Nothing cached → the caller has to wait; there is nothing to
    show yet. Returns the payload, or None when there is no cached copy."""
    hit = _cache.get(key)
    if not hit:
        return None
    ts, payload = hit
    if (time.time() - ts) < _CACHE_TTL:
        return payload
    if key not in _refreshing:
        _refreshing.add(key)

        async def _bg():
            try:
                await builder()
            except Exception as e:
                print(f"[radar] background refresh {key}: {e}", flush=True)
            finally:
                _refreshing.discard(key)

        asyncio.create_task(_bg())
    return {**payload, "stale": True}


def _store(key: str, payload: dict) -> dict:
    _cache[key] = (time.time(), payload)
    _save_cache()
    return payload


def _watch_entries(service: str) -> list:
    return [e for e in (_s.get("watchlist") or [])
            if (e.get("service") or "apple") == service]


# ── BBC ───────────────────────────────────────────────────────────────────────

@router.get("/api/releases/bbc")
async def releases_bbc(days: int = Query(90, ge=1, le=365),
                       force: int = Query(0)):
    """New episodes of the BBC shows we know about."""
    key = f"bbc|{days}"
    if not force:
        hit = _serve_or_refresh(key, lambda: releases_bbc(days=days, force=1))
        if hit is not None:
            return hit

    from ripster.routes.bbc import BRANDS, get_episodes

    cutoff = _cutoff(days)
    sem = asyncio.Semaphore(4)          # be polite to the BBC API

    async def _one(brand: dict) -> list:
        async with sem:
            try:
                data = await get_episodes(brand_id=brand["id"], offset=0, limit=12)
            except Exception as e:
                print(f"[radar] bbc {brand['label']}: {e}", flush=True)
                return []
        out = []
        for ep in data.get("items", []):
            date = (ep.get("date") or "")[:10]
            if not date or date < cutoff:
                continue
            out.append({
                "id":        ep.get("pid", ""),
                "title":     ep.get("subtitle") or ep.get("title", ""),
                "artist":    brand["label"],
                "artist_id": brand["id"],
                "type":      "mix",
                "group":     "mix",
                "date":      date,
                "year":      date[:4],
                "tracks":    None,
                "cover":     ep.get("image", ""),
                "url":       f"https://www.bbc.co.uk/programmes/{ep.get('pid','')}",
                "service":   "bbc",
                "duration":  ep.get("duration") or 0,
            })
        return out

    results = await asyncio.gather(*(_one(b) for b in BRANDS),
                                   return_exceptions=True)
    releases = [r for res in results if isinstance(res, list) for r in res]
    # Склад: то, что источник уже не показывает, всё равно остаётся.
    releases = _durable_merge("bbc", releases, days)
    return _store(key, {"ok": True, "releases": releases,
                        "sources": len(BRANDS)})


# ── SoundCloud ────────────────────────────────────────────────────────────────

@router.get("/api/releases/soundcloud")
async def releases_soundcloud(days: int = Query(90, ge=1, le=365),
                              force: int = Query(0)):
    """Recent uploads from the SoundCloud channels on the watchlist."""
    key = f"sc|{days}"
    if not force:
        hit = _serve_or_refresh(key, lambda: releases_soundcloud(days=days, force=1))
        if hit is not None:
            return hit

    from ripster.routes.soundcloud import sc_user_tracks
    from ripster.routes.watchlist import _sc_permalink

    entries = _watch_entries("soundcloud")
    if not entries:
        # Not an error: nothing followed yet. The UI shows a hint rather than
        # an empty feed with no explanation.
        return _store(key, {"ok": True, "releases": [], "sources": 0,
                            "hint": "no_channels"})

    cutoff = _cutoff(days)
    sem = asyncio.Semaphore(3)

    async def _one(entry: dict) -> list:
        permalink = _sc_permalink(entry)
        if not permalink:
            return []
        async with sem:
            try:
                r = await sc_user_tracks(permalink=permalink, limit=15)
            except Exception as e:
                print(f"[radar] sc {permalink}: {e}", flush=True)
                return []
        if not r.get("ok"):
            return []
        out = []
        for tr in r.get("results", []):
            date = (tr.get("date") or "")[:10]
            if not date or date < cutoff:
                continue
            out.append({
                "id":        str(tr.get("id", "")),
                "title":     tr.get("title", ""),
                "artist":    tr.get("artist") or entry.get("name", permalink),
                "artist_id": permalink,
                "type":      "mix",
                "group":     "mix",
                "date":      date,
                "year":      date[:4],
                "tracks":    None,
                "cover":     tr.get("artwork_sm") or tr.get("artwork", ""),
                "url":       tr.get("url", ""),
                "service":   "soundcloud",
                "duration":  tr.get("duration") or 0,
            })
        return out

    results = await asyncio.gather(*(_one(e) for e in entries),
                                   return_exceptions=True)
    releases = [r for res in results if isinstance(res, list) for r in res]
    # Склад: то, что источник уже не показывает, всё равно остаётся.
    releases = _durable_merge("soundcloud", releases, days)
    return _store(key, {"ok": True, "releases": releases,
                        "sources": len(entries)})


# ── Apple ─────────────────────────────────────────────────────────────────────

@router.get("/api/releases/apple")
async def releases_apple(days: int = Query(90, ge=1, le=365),
                         force: int = Query(0)):
    """New Apple releases by the artists on the watchlist — compilations
    included, which is the whole reason watchlist.py resolves them via tracks."""
    key = f"apple|{days}"
    if not force:
        hit = _serve_or_refresh(key, lambda: releases_apple(days=days, force=1))
        if hit is not None:
            return hit

    import httpx
    from ripster.routes.watchlist import _apple_artist_collections

    entries = [e for e in _watch_entries("apple") if e.get("artist_id")]
    if not entries:
        return _store(key, {"ok": True, "releases": [], "sources": 0,
                            "hint": "no_artists"})

    cfg        = _s.get("config") or {}
    storefront = cfg.get("storefront", "us") or "us"
    with_comps = cfg.get("watchlist-compilations", True) is not False
    cutoff     = _cutoff(days)
    today      = datetime.now().strftime("%Y-%m-%d")
    sem        = asyncio.Semaphore(4)

    async def _one(client, entry: dict) -> list:
        async with sem:
            try:
                rels = await _apple_artist_collections(
                    client, entry["artist_id"], storefront, with_comps)
            except Exception as e:
                print(f"[radar] apple {entry.get('name')}: {e}", flush=True)
                return []
        out = []
        for x in rels:
            date = (x.get("date") or "")[:10]
            # Pre-orders carry a future date — they are not out yet.
            if not date or date < cutoff or date > today:
                continue
            is_comp = bool(x.get("compilation"))
            out.append({
                "id":        x.get("url", "") or x.get("name", ""),
                "title":     x.get("name", ""),
                "artist":    entry.get("name", ""),
                "artist_id": entry.get("artist_id", ""),
                "alb_artist": x.get("artist", ""),
                "type":      "compilation" if is_comp else "album",
                "group":     "compilation" if is_comp else "album",
                "date":      date,
                "year":      date[:4],
                "tracks":    None,
                "cover":     x.get("cover", ""),
                "url":       x.get("url", ""),
                "service":   "apple",
            })
        return out

    async with httpx.AsyncClient(timeout=20) as client:
        results = await asyncio.gather(*(_one(client, e) for e in entries),
                                       return_exceptions=True)
    releases = [r for res in results if isinstance(res, list) for r in res]
    # The same compilation is reachable through several watched artists.
    seen, uniq = set(), []
    for r in releases:
        if r["id"] in seen:
            continue
        seen.add(r["id"])
        uniq.append(r)
    uniq.sort(key=lambda x: x["date"], reverse=True)
    # Склад: то, что источник уже не показывает, всё равно остаётся.
    uniq = _durable_merge("apple", uniq, days)
    return _store(key, {"ok": True, "releases": uniq, "sources": len(entries)})
