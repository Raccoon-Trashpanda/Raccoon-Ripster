"""
Watchlist routes — CRUD + background new-release checker + smart suggestions.

Install: watchlist.install(app, ctx)
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException, Query

from ripster import compilations as _comps
from ripster import watchlist_suggest as _wls

router = APIRouter()
_s: dict = {}  # items, save, broadcast, config, queue, queue_snapshot, detect_service


def install(app, ctx) -> None:
    _s.update({
        "items":          ctx.watchlist,
        "save":           ctx.save_watchlist,
        "broadcast":      ctx.broadcast,
        "config":         ctx.config,
        "queue":          ctx.queue,
        "queue_snapshot": ctx.queue_snapshot,
        "detect_service": ctx.detect_service,
        "base_dir":       ctx.base_dir,
    })
    app.include_router(router)


# ── Dismissed suggestions ─────────────────────────────────────────────────────
# Kept in a sidecar file so a rejected suggestion stays rejected across restarts
# without polluting watchlist.json with non-watched entries.

def _dismiss_file() -> Path:
    return Path(_s.get("base_dir", ".")) / "watchlist_dismissed.json"


def _load_dismissed() -> set[str]:
    try:
        f = _dismiss_file()
        if f.exists():
            return set(json.loads(f.read_text(encoding="utf-8")) or [])
    except Exception as e:
        print(f"[watchlist] dismissed load failed: {e}", flush=True)
    return set()


def _save_dismissed(keys: set[str]) -> None:
    try:
        _dismiss_file().write_text(json.dumps(sorted(keys), ensure_ascii=False, indent=1),
                                   encoding="utf-8")
    except Exception as e:
        print(f"[watchlist] dismissed save failed: {e}", flush=True)


@router.get("/api/watchlist")
async def api_watchlist_get():
    return {"items": _s["items"]}


async def _resolve_target(name: str, url: str, service: str, artist_id: str) -> dict:
    """Fill in whatever the background checker needs to actually poll this entry.

    The Apple branch of _check_watchlist() only looks at entries that have an
    `artist_id`, so an entry added by name alone would sit there forever without
    ever being checked. Resolve it up front instead: from the URL when the user
    pasted an artist link, otherwise via the public iTunes Search API.
    """
    out = {"name": name, "url": url, "service": service, "artist_id": artist_id}

    if service == "soundcloud":
        hint = _wls.sc_permalink_from_url(url) or _wls.sc_permalink_from_url(name)
        r = await _wls.resolve_sc_channel(name or hint, hint)
        if r:
            out["url"] = r["url"]
        return out

    if service != "apple":
        return out

    if not out["artist_id"]:
        out["artist_id"] = _wls.apple_id_from_url(url)
    if not out["artist_id"] and name:
        r = await _wls.resolve_apple_artist(name)
        if r:
            out["artist_id"] = r["artist_id"]
            out["url"] = out["url"] or r.get("url", "")
            out["name"] = out["name"] or r.get("name", name)
    return out


@router.post("/api/watchlist")
async def api_watchlist_add(body: dict):
    name      = body.get("name", "").strip()
    url       = body.get("url", "").strip()
    service   = body.get("service", _s["detect_service"](url)) or "apple"
    artist_id = body.get("artist_id", "")
    kind      = (body.get("kind") or "artist").strip()
    if not name and not url:
        raise HTTPException(400, "name or url required")

    # ── Label subscription ───────────────────────────────────────────────────
    # A label has no artist id and no channel to resolve — the name IS the key.
    # Verify up front that the label actually returns releases, so a typo can't
    # create an entry that silently never fires.
    if kind == "label":
        if not name:
            raise HTTPException(400, "название лейбла обязательно")
        rels = await _label_releases(name, 10)
        entry = {
            "id":           f"wl_{int(datetime.now().timestamp()*1000)}",
            "name":         name,
            "kind":         "label",
            "url":          url,
            "service":      service,          # где качать, не где следить
            "artist_id":    "",
            "quality":      body.get("quality", _s["config"].get("quality", "alac")),
            "added":        datetime.now().isoformat(timespec="seconds"),
            "last_check":   datetime.now().isoformat(timespec="seconds") if rels else None,
            # Baseline immediately: subscribing must not dump the back catalogue
            # into the queue on the first check.
            "last_release":      (rels[0].get("title", "") if rels else None),
            "last_release_date": (str(rels[0].get("date") or rels[0].get("year") or "")
                                  if rels else ""),
            "auto_download": body.get("auto_download", False),
        }
        _s["items"].append(entry)
        _s["save"](_s["items"])
        return {"ok": True, "item": entry, "resolved": bool(rels),
                "found": len(rels),
                "warning": ("" if rels else
                            f"Лейбл «{name}» не найден в каталогах Spotify/Deezer — "
                            f"проверь написание, иначе отслеживать нечего")}

    resolved = await _resolve_target(name, url, service, artist_id)

    entry = {
        "id":           f"wl_{int(datetime.now().timestamp()*1000)}",
        "name":         resolved["name"] or name,
        "kind":         "artist",
        "url":          resolved["url"],
        "service":      service,
        "artist_id":    resolved["artist_id"],
        "quality":      body.get("quality", _s["config"].get("quality", "alac")),
        "added":        datetime.now().isoformat(timespec="seconds"),
        "last_check":   None,
        "last_release": None,
        "auto_download": body.get("auto_download", True),
    }
    _s["items"].append(entry)
    _s["save"](_s["items"])
    # `resolved` is reported so the UI can warn when an entry went in unpollable
    # (no Apple artist id / unresolvable SC channel) instead of failing silently.
    return {"ok": True, "item": entry,
            "resolved": bool(entry["artist_id"] or service != "apple")}


# ── Smart suggestions (mined from the local stats DB) ────────────────────────

@router.get("/api/watchlist/suggestions")
async def api_watchlist_suggestions(limit: int = Query(12, ge=1, le=50)):
    dismissed = _load_dismissed()
    # 60ms of pure SQLite + string work — off the event loop anyway.
    res = await asyncio.to_thread(_wls.compute, _s["items"], limit + len(dismissed))
    if res.get("suggestions"):
        res["suggestions"] = [s for s in res["suggestions"]
                              if s["key"] not in dismissed][:limit]
    res["dismissed"] = len(dismissed)
    return res


@router.post("/api/watchlist/suggestions/dismiss")
async def api_watchlist_suggestion_dismiss(body: dict):
    key = (body.get("key") or "").strip()
    if not key:
        raise HTTPException(400, "key required")
    keys = _load_dismissed()
    keys.add(key)
    _save_dismissed(keys)
    return {"ok": True, "dismissed": len(keys)}


@router.post("/api/watchlist/suggestions/reset")
async def api_watchlist_suggestions_reset():
    _save_dismissed(set())
    return {"ok": True}


@router.post("/api/watchlist/suggestions/accept")
async def api_watchlist_suggestion_accept(body: dict):
    """Turn a suggestion into a real, pollable watchlist entry."""
    name    = (body.get("name") or "").strip()
    service = (body.get("service") or "apple").strip()
    if not name:
        raise HTTPException(400, "name required")

    resolved = await _resolve_target(
        name,
        body.get("url", "") or (f"https://soundcloud.com/{body['sc_permalink']}"
                                if body.get("sc_permalink") else ""),
        service,
        body.get("apple_id", ""),
    )

    # An SC-classified name that turns out to have no channel is often still a
    # real Apple artist (played as a mix channel, released on Apple) — fall back
    # rather than refusing a suggestion that is perfectly watchable elsewhere.
    if service == "soundcloud" and not resolved["url"]:
        alt = await _resolve_target(name, "", "apple", "")
        if alt["artist_id"]:
            service, resolved = "apple", alt

    # Refusing here beats adding an entry the checker would silently skip.
    if service == "apple" and not resolved["artist_id"]:
        return {"ok": False, "error": "apple_artist_not_found", "name": name}
    if service == "soundcloud" and not resolved["url"]:
        return {"ok": False, "error": "sc_channel_not_found", "name": name}

    entry = {
        "id":           f"wl_{int(datetime.now().timestamp()*1000)}",
        "name":         resolved["name"] or name,
        "kind":         "artist",
        "url":          resolved["url"],
        "service":      service,
        "artist_id":    resolved["artist_id"],
        "quality":      body.get("quality", _s["config"].get("quality", "alac")),
        "added":        datetime.now().isoformat(timespec="seconds"),
        "last_check":   None,
        "last_release": None,
        "auto_download": body.get("auto_download", True),
        "from_suggestion": body.get("key", ""),
    }
    _s["items"].append(entry)
    _s["save"](_s["items"])
    return {"ok": True, "item": entry}


@router.post("/api/watchlist/{item_id}/download-latest")
async def api_watchlist_download_latest(item_id: str, body: dict | None = None):
    """Queue the label's most recent release right now.

    Subscribing records a baseline (otherwise the whole back catalogue lands in
    the queue), so the checker legitimately waits for the NEXT release — which
    reads as "it says new release but downloads nothing". This is the explicit
    "I want the current one" action."""
    entry = next((e for e in _s["items"] if e.get("id") == item_id), None)
    if not entry:
        raise HTTPException(404, "запись не найдена")
    if entry.get("kind") != "label":
        raise HTTPException(400, "только для лейблов")

    how_many = int((body or {}).get("count") or 1)
    how_many = max(1, min(how_many, 5))
    rels = await _label_releases(entry["name"], 20)
    if not rels:
        return {"ok": False, "error": f"У лейбла «{entry['name']}» не нашлось релизов"}

    from ripster.routes import discovery as _disc
    cfg, queue, snapshot = _s["config"], _s["queue"], _s["queue_snapshot"]
    svc = entry.get("service", "apple")
    queued, skipped = [], []
    for rel in rels[:how_many]:
        url = rel.get("url", "")
        if (rel.get("service") or "") != svc:      # источник ≠ цель → переводим
            try:
                m = await _disc._match_seeds_in_service([rel], svc, entry["name"], 1,
                                                        cfg.get("storefront", "us"))
                url = (m[0].get("url") if m else "") or ""
            except Exception:
                url = ""
        if not url:
            skipped.append(rel.get("title", "?"))
            continue
        queue.append({
            "id":       f"wl_{int(datetime.now().timestamp()*1000)}_{len(queued)}",
            "url":      url,
            "quality":  entry.get("quality", cfg.get("quality", "alac")),
            "status":   "queued", "progress": 0, "log": [],
            "source":   "watchlist-label",
        })
        queued.append(rel.get("title", "?"))
    if queued:
        await _s["broadcast"]({"type": "queue_update", "queue": snapshot()})
    return {"ok": bool(queued), "queued": queued, "skipped": skipped,
            "label": entry["name"],
            "error": ("" if queued else f"Не нашёл этих релизов в {svc}")}


@router.post("/api/watchlist/repair")
async def api_watchlist_repair():
    """Backfill missing artist_ids and drop duplicates.

    Entries added before the add path resolved ids (and the UI's own duplicate
    adds, which reused the same generated id) are dead weight: the checker skips
    anything without an artist_id, so those artists were never actually watched.
    """
    items = _s["items"]
    seen: set[tuple] = set()
    unique: list[dict] = []
    dropped = 0
    for it in items:
        # `kind` is part of the signature: a LABEL called "Mesh" and an ARTIST
        # called "Mesh" are different subscriptions and must not collapse.
        sig = (it.get("kind", "artist"), _wls._norm(it.get("name", "")),
               it.get("service", ""), it.get("url", ""))
        if sig in seen:
            dropped += 1
            continue
        seen.add(sig)
        unique.append(it)

    fixed = 0
    for it in unique:
        # Labels have no artist_id by design — never try to "repair" them into
        # an artist. Doing so rewrote the label's name/url to a same-named
        # artist and then the artist_id de-dup below deleted it as a duplicate,
        # which is exactly how label subscriptions vanished after a check.
        if it.get("kind") == "label":
            continue
        if it.get("service", "apple") != "apple" or it.get("artist_id"):
            continue
        aid = _wls.apple_id_from_url(it.get("url", ""))
        if not aid and it.get("name"):
            r = await _wls.resolve_apple_artist(it["name"])
            aid = r.get("artist_id", "")
            if aid and not it.get("url"):
                it["url"] = r.get("url", "")
            if aid and r.get("name"):
                it["name"] = r["name"]
        if aid:
            it["artist_id"] = aid
            fixed += 1

    # De-dup by resolved artist_id too — "max cooper" and a pasted Max Cooper
    # link are the same target once both have an id.
    by_id: set[str] = set()
    final: list[dict] = []
    for it in unique:
        if it.get("kind") == "label":       # labels never de-dup by artist_id
            final.append(it)
            continue
        aid = it.get("artist_id", "")
        if aid and aid in by_id:
            dropped += 1
            continue
        if aid:
            by_id.add(aid)
        final.append(it)

    items[:] = final
    _s["save"](items)
    return {"ok": True, "fixed": fixed, "dropped": dropped, "total": len(items)}


@router.delete("/api/watchlist/{item_id}")
async def api_watchlist_delete(item_id: str):
    items = _s["items"]
    items[:] = [x for x in items if x.get("id") != item_id]
    _s["save"](items)
    return {"ok": True}


@router.post("/api/watchlist/check")
async def api_watchlist_check():
    asyncio.create_task(_check_watchlist())
    return {"ok": True, "msg": "Checking in background…"}


async def _apple_artist_collections(client, artist_id: str, storefront: str,
                                    with_comps: bool = True) -> list:
    """Every release an Apple artist is on — their own albums AND the
    compilations they merely have a track on.

    Two lookups, because one endpoint cannot answer both questions:

      entity=album  the artist's own releases (they are the album artist)
      entity=song   their tracks — and a track always carries its parent
                    `collection*` fields, which is the ONLY way a various-artists
                    compilation shows up. `entity=album` never returns one, so
                    without this half a Hospital/Forza-style label compilation is
                    invisible while its individual tracks are visible.

    See ripster/compilations.py — same blind spot as the Spotify radar.
    """
    out: dict[str, dict] = {}

    async def _lookup(params: dict) -> list:
        try:
            r = await client.get("https://itunes.apple.com/lookup", params=params)
            if r.status_code != 200:
                return []
            return (r.json() or {}).get("results") or []
        except Exception as e:
            print(f"[watchlist] apple lookup {artist_id} ({params.get('entity')}): {e}",
                  flush=True)
            return []

    for x in await _lookup({"id": artist_id, "entity": "album", "limit": 25,
                            "sort": "recent", "country": storefront}):
        if x.get("wrapperType") == "collection" and x.get("collectionId"):
            out[str(x["collectionId"])] = {
                "url":   x.get("collectionViewUrl", ""),
                "name":  x.get("collectionName", ""),
                "date":  (x.get("releaseDate", "") or "")[:10],
                "artist": x.get("artistName", ""),
                # 100px is what the API hands out; ask for a usable card size.
                "cover": (x.get("artworkUrl100", "") or "").replace("100x100", "400x400"),
                "compilation": _comps.is_compilation(
                    album_artist=x.get("artistName", ""), title=x.get("collectionName", "")),
            }

    if with_comps:
        for x in await _lookup({"id": artist_id, "entity": "song", "limit": 200,
                                "sort": "recent", "country": storefront}):
            cid = x.get("collectionId")
            if x.get("wrapperType") != "track" or not cid or str(cid) in out:
                continue
            alb_artist = x.get("collectionArtistName") or x.get("artistName", "")
            out[str(cid)] = {
                "url":   x.get("collectionViewUrl", ""),
                "name":  x.get("collectionName", ""),
                # A track's releaseDate is the collection's release date here.
                "date":  (x.get("releaseDate", "") or "")[:10],
                "artist": alb_artist,
                "cover": (x.get("artworkUrl100", "") or "").replace("100x100", "400x400"),
                "compilation": _comps.is_compilation(
                    album_artist=alb_artist, title=x.get("collectionName", ""),
                    track_artist=x.get("artistName", "")),
            }
    return list(out.values())


async def _apple_latest_album(client, artist_id: str, storefront: str = "us",
                              with_comps: bool = True) -> dict:
    """Newest already-released release for an Apple artist, compilations included.

    Replaces the old `itunes.apple.com/rss/artistnewreleases/id=…` feed, which
    Apple retired — it now answers 400 "Invalid RSS channel name" for every id,
    so the Apple half of the watchlist silently checked nothing. The public
    lookup API still serves this and needs no token.
    """
    rels = await _apple_artist_collections(client, artist_id, storefront, with_comps)
    if not rels:
        return {}
    # Pre-orders carry a future releaseDate — keep them out, they cannot be
    # downloaded yet. Fall back to the newest overall if everything is upcoming.
    today = datetime.now().strftime("%Y-%m-%d")
    rels.sort(key=lambda x: x.get("date", ""), reverse=True)
    released = [x for x in rels if x.get("date", "") <= today]
    return (released or rels)[0]


# ── New-Zealand release window ────────────────────────────────────────────────
# New music drops on Friday 00:00 LOCAL time in each territory, and New Zealand
# is among the first places on earth to get there — a release is out in Auckland
# roughly half a day before it is here. The checker used to run on a bare 6h
# grid, so how early you saw a Friday release depended on where that grid
# happened to land. Aligning one check to the Auckland Friday means the moment
# a release exists anywhere, we look.
#
# Computed by hand rather than via zoneinfo: on Windows zoneinfo needs the
# `tzdata` package, and adding a dependency to this interpreter is exactly what
# ripster-dependency-versions says not to do casually. NZ's rule is simple and
# stable: UTC+13 from the last Sunday of September to the first Sunday of April,
# UTC+12 otherwise.

def _last_sunday(year: int, month: int) -> "datetime":
    from calendar import monthrange
    d = datetime(year, month, monthrange(year, month)[1])
    return d - timedelta(days=(d.weekday() + 1) % 7)


def _first_sunday(year: int, month: int) -> "datetime":
    d = datetime(year, month, 1)
    return d + timedelta(days=(6 - d.weekday()) % 7)


def _nz_offset(utc_dt: "datetime") -> int:
    """Hours NZ is ahead of UTC at this instant (12 or 13)."""
    y = utc_dt.year
    dst_start = _last_sunday(y, 9).replace(hour=2)    # NZDT begins
    dst_end   = _first_sunday(y, 4).replace(hour=3)   # NZDT ends
    # Southern hemisphere: DST spans the new year, so it is the GAP that is
    # standard time, not the span.
    return 12 if (dst_end <= utc_dt < dst_start) else 13


def nz_now(utc_dt: "datetime | None" = None) -> "datetime":
    utc_dt = utc_dt or datetime.utcnow()
    return utc_dt + timedelta(hours=_nz_offset(utc_dt))


def seconds_to_nz_friday(utc_dt: "datetime | None" = None,
                         grace_min: int = 5) -> float:
    """Seconds until the next Friday 00:05 in Auckland."""
    utc_dt = utc_dt or datetime.utcnow()
    nz = nz_now(utc_dt)
    target = (nz.replace(hour=0, minute=grace_min, second=0, microsecond=0)
              + timedelta(days=(4 - nz.weekday()) % 7))
    if target <= nz:
        target += timedelta(days=7)
    return (target - nz).total_seconds()


def next_check_delay(cfg: dict | None = None, base: float = 6 * 3600) -> float:
    """How long the background loop should sleep before the next check.

    Normally the plain interval; but if the Auckland Friday lands sooner, wake
    then instead so a Friday release is seen at the first minute it exists.
    """
    cfg = cfg if cfg is not None else (_s.get("config") or {})
    if cfg.get("watchlist-nz-window", True) is False:
        return base
    return max(60.0, min(base, seconds_to_nz_friday()))


def _notify_release(artist: str, release: str, compilation: bool, queued: bool) -> None:
    """Native desktop toast for a watchlist hit. Best-effort and never fatal —
    a notification failing must not abort the check that found the release."""
    cfg = _s.get("config") or {}
    if cfg.get("notify-on-release", True) is False:
        return
    try:
        from ripster import notify as _notify
        _notify.toast_new_release(artist, release, compilation=compilation,
                                  queued=queued, lang=cfg.get("language", "en"))
    except Exception as e:
        print(f"[watchlist] toast failed: {e}", flush=True)


def _sc_permalink(entry: dict) -> str:
    """Extract a SoundCloud channel permalink from the entry's url/name field —
    accepts either a bare handle or a full soundcloud.com/<handle> link."""
    raw = (entry.get("url") or entry.get("name") or "").strip().strip("/")
    if "soundcloud.com/" in raw:
        raw = raw.split("soundcloud.com/", 1)[1]
    return raw.split("/", 1)[0].split("?", 1)[0]


async def _check_soundcloud_targets(items: list, broadcast, save, cfg, queue, snapshot) -> int:
    """SC channels: newest upload via the same lookup the SC search tab uses
    (sc_user_tracks) — SC has no RSS feed like Apple, so this hits the API
    directly instead."""
    from ripster.routes.soundcloud import sc_user_tracks

    targets = [e for e in items if e.get("service") == "soundcloud"]
    new_found = 0
    for entry in targets:
        permalink = _sc_permalink(entry)
        if not permalink:
            continue
        try:
            r = await sc_user_tracks(permalink=permalink, limit=1)
        except Exception as e:
            print(f"[watchlist] sc:{permalink}: {e}", flush=True)
            continue
        if not r.get("ok") or not r.get("results"):
            continue
        latest = r["results"][0]
        track_id = latest.get("id")
        track_url = latest.get("url", "")
        prev = entry.get("last_release")
        entry["last_check"] = datetime.now().isoformat(timespec="seconds")
        if track_id and str(track_id) != str(prev) and track_url:
            entry["last_release"] = str(track_id)
            # Same baseline rule as the Apple branch: the first check of a newly
            # added channel records where we are, it is not a "new release".
            if prev is None:
                save(items)
                continue
            new_found += 1
            save(items)
            await broadcast({"type": "watchlist_new_release",
                             "artist": entry.get("name") or permalink,
                             "release": latest.get("title", ""),
                             "url": track_url})
            _notify_release(entry.get("name") or permalink,
                            latest.get("title", ""), False,
                            bool(entry.get("auto_download")))
            if entry.get("auto_download"):
                task = {
                    "id":       f"wl_{int(datetime.now().timestamp()*1000)}",
                    "url":      track_url,
                    "quality":  entry.get("quality", cfg.get("quality", "alac")),
                    "status":   "queued",
                    "progress": 0,
                    "log":      [],
                    "source":   "watchlist",
                }
                queue.append(task)
                await broadcast({"type": "queue_update", "queue": snapshot()})
    return new_found


async def _label_releases(label: str, limit: int = 20) -> list[dict]:
    """Releases of a label, newest first.

    Reuses the label search built for /api/search: Spotify and Deezer are the
    only services that can answer "what did this label put out", so they are the
    monitoring source regardless of where the user wants the files from.
    Sorted by release date because those APIs return by relevance, and a
    relevance-ordered list would make an old album look like a new release."""
    from ripster.routes import discovery as _disc
    try:
        seeds = await _disc._label_seeds(label, limit)
    except Exception as e:
        print(f"[watchlist] label «{label}» lookup failed: {e}", flush=True)
        return []
    seeds = [s for s in seeds if (s.get("date") or s.get("year"))]
    seeds.sort(key=lambda s: str(s.get("date") or s.get("year") or ""), reverse=True)
    return seeds


async def _check_label_targets(items, broadcast, save, cfg, queue, snapshot) -> int:
    """Poll label subscriptions.

    Honest monitoring: we compare RELEASE DATES, not list position. A label puts
    out several records a month, so "the newest one changed" is not enough —
    everything dated after the last seen date counts as new, and the first check
    only records a baseline instead of queueing the whole back catalogue."""
    labels = [e for e in items if e.get("kind") == "label" and e.get("name")]
    if not labels:
        return 0
    from ripster.routes import discovery as _disc
    found = 0
    for entry in labels:
        name = entry["name"]
        await broadcast({"type": "watchlist_check_progress", "artist": f"🏷 {name}"})
        try:
            rels = await _label_releases(name, 20)
            entry["last_check"] = datetime.now().isoformat(timespec="seconds")
            if not rels:
                continue
            seen_date = entry.get("last_release_date") or ""
            newest = str(rels[0].get("date") or rels[0].get("year") or "")
            if not seen_date:
                entry["last_release_date"] = newest        # baseline only
                entry["last_release"] = rels[0].get("title", "")
                save(items)
                continue
            fresh = [r for r in rels
                     if str(r.get("date") or r.get("year") or "") > seen_date]
            if not fresh:
                continue
            entry["last_release_date"] = newest
            entry["last_release"] = fresh[0].get("title", "")
            found += len(fresh)
            save(items)

            svc = entry.get("service", "apple")
            for rel in fresh[:5]:            # guard against a catalogue dump
                title = rel.get("title", "")
                await broadcast({"type": "watchlist_new_release",
                                 "artist": f"{rel.get('artist','')} · {name}",
                                 "release": title, "label": name,
                                 "url": rel.get("url", "")})
                _notify_release(f"{name} · {rel.get('artist','')}", title,
                                False, bool(entry.get("auto_download")))
                if not entry.get("auto_download"):
                    continue
                # The monitoring source is not necessarily the download target,
                # so translate whenever they differ. Testing `svc not in
                # (spotify, deezer)` was wrong: a label watched with
                # service="deezer" still gets its releases from SPOTIFY (the
                # first catalogue queried), and that branch queued the raw
                # Spotify link — which Ripster then tried to fetch as an Apple
                # ALAC album and hung on "Fetching metadata…".
                url = rel.get("url", "")
                if (rel.get("service") or "") != svc:
                    try:
                        m = await _disc._match_seeds_in_service([rel], svc, name, 1,
                                                                cfg.get("storefront", "us"))
                        url = (m[0].get("url") if m else "") or ""
                    except Exception:
                        url = ""
                if not url:
                    print(f"[watchlist] «{title}» не найден в {svc} — пропускаю", flush=True)
                    continue
                queue.append({
                    "id":       f"wl_{int(datetime.now().timestamp()*1000)}",
                    "url":      url,
                    "quality":  entry.get("quality", cfg.get("quality", "alac")),
                    "status":   "queued", "progress": 0, "log": [],
                    "source":   "watchlist-label",
                })
                await broadcast({"type": "queue_update", "queue": snapshot()})
        except Exception as e:
            print(f"[watchlist] label {name}: {e}", flush=True)
    save(items)
    return found


async def _check_watchlist():
    items      = _s["items"]
    broadcast  = _s["broadcast"]
    save       = _s["save"]
    cfg        = _s["config"]
    queue      = _s["queue"]
    snapshot   = _s["queue_snapshot"]

    targets = [e for e in items
               if e.get("service") == "apple" and e.get("artist_id")
               and e.get("kind") != "label"]
    sc_count = len([e for e in items
                    if e.get("service") == "soundcloud" and e.get("kind") != "label"])
    label_count = len([e for e in items if e.get("kind") == "label"])
    total = len(targets) + sc_count + label_count
    if total == 0:
        return

    new_found = 0
    await broadcast({"type": "watchlist_check_start", "total": total})

    if label_count:
        new_found += await _check_label_targets(items, broadcast, save, cfg, queue, snapshot)

    if sc_count:
        new_found += await _check_soundcloud_targets(items, broadcast, save, cfg, queue, snapshot)

    storefront = cfg.get("storefront", "us") or "us"
    # Compilations cost one extra lookup per artist; on by default because a
    # label compilation is exactly the release people miss.
    want_comps = cfg.get("watchlist-compilations", True) is not False
    async with httpx.AsyncClient(timeout=15) as client:
        for i, entry in enumerate(targets):
            artist_id = entry["artist_id"]
            await broadcast({
                "type":    "watchlist_check_progress",
                "current": sc_count + i + 1,
                "total":   total,
                "artist":  entry.get("name", "?"),
            })
            try:
                latest = await _apple_latest_album(client, artist_id, storefront,
                                                   with_comps=want_comps)
                # last_check is stamped even when the lookup yields nothing, so
                # "never checked" in the UI means exactly that.
                entry["last_check"] = datetime.now().isoformat(timespec="seconds")
                if not latest:
                    continue
                release_url  = latest["url"]
                release_name = latest["name"]
                prev = entry.get("last_release")
                if release_url and release_url != prev:
                    entry["last_release"] = release_url
                    # First ever check just records a baseline: otherwise adding
                    # an artist would instantly "discover" their whole current
                    # back catalogue and queue it.
                    if prev is None:
                        save(items)
                        continue
                    new_found += 1
                    save(items)
                    await broadcast({"type":     "watchlist_new_release",
                                     "artist":   entry["name"],
                                     "release":  release_name,
                                     "compilation": bool(latest.get("compilation")),
                                     "url":      release_url})
                    _notify_release(entry["name"], release_name,
                                    bool(latest.get("compilation")),
                                    bool(entry.get("auto_download")))
                    if entry.get("auto_download") and release_url:
                        task = {
                            "id":       f"wl_{int(datetime.now().timestamp()*1000)}",
                            "url":      release_url,
                            "quality":  entry.get("quality", cfg.get("quality", "alac")),
                            "status":   "queued",
                            "progress": 0,
                            "log":      [],
                            "source":   "watchlist",
                        }
                        queue.append(task)
                        await broadcast({"type": "queue_update", "queue": snapshot()})
            except Exception as e:
                print(f"[watchlist] {entry['name']}: {e}", flush=True)

    # Persist the last_check stamps of entries that had no news — without this
    # a quiet run leaves every entry looking like it was never checked.
    save(items)

    await broadcast({
        "type":    "watchlist_check_done",
        "checked": total,
        "new":     new_found,
    })
