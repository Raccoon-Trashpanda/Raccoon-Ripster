"""
Multi-service release radar — Qobuz and Tidal.

Uses asyncio concurrency (semaphore) so 200 artists scan in ~15s instead of hours.

Каждый источник читает подписки СВОЕГО аккаунта — и ровно из-за этого артист
виден только тому сервису, в котором на него подписаны. Витрины наполняются
вразнобой, аккаунты у владельца в разных странах, и та витрина, где релиз
появляется РАНЬШЕ всех, до ленты не доходила вовсе: в служебном Tidal-аккаунте
0 подписок, поэтому Tidal-источник всегда отдавал пустоту, хотя новозеландская
пятница наступает раньше всех остальных (разбор 29.07.2026, Sultan + Shepard —
«Centuries»: релиз лежал в Tidal NZ за сутки до мировой даты).

Поэтому список артистов каждого источника — это подписки его аккаунта ПЛЮС те,
за кем следят в других сервисах, найденные в этом каталоге по имени
(`ripster.artist_xref`, сверка только по точному совпадению). Выключается
ключом `radar-cross-service: false`.
"""
from __future__ import annotations

import asyncio
import base64
import json
from datetime import datetime, timedelta

import httpx
from fastapi import APIRouter

from ripster import artist_xref as _xref

router     = APIRouter()
_config: dict = {}
_broadcast    = None
_watchlist: list = []

_TIDAL_API   = "https://api.tidal.com/v1"
_QOBUZ_API   = "https://www.qobuz.com/api.json/0.2"
_CONCURRENCY = 10   # parallel artist→album requests per service
_TIMEOUT     = httpx.Timeout(connect=10, read=20, write=10, pool=5)


def install(app, ctx) -> None:
    global _config, _broadcast, _watchlist
    _config    = ctx.config
    _broadcast = ctx.broadcast
    _watchlist = ctx.watchlist
    _xref.configure(ctx.config, ctx.base_dir)
    app.include_router(router)


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _get_retry(c: httpx.AsyncClient, url: str, *, tries: int = 3, **kw) -> httpx.Response:
    """GET that retries a transient upstream failure (5xx / timeout) before
    giving up. The radar's per-service scans hard-fail the WHOLE feed on the
    first non-200, so a single Tidal/Qobuz 500 blip zeroed the entire radar
    (жалоба 03.09.2026: «⚠ Tidal API 500» в релиз-радаре). 4xx is returned
    as-is — that's a real answer (401 = token, 404 = gone), not a blip."""
    last: httpx.Response | None = None
    for attempt in range(tries):
        try:
            r = await c.get(url, **kw)
        except (httpx.TimeoutException, httpx.TransportError):
            if attempt == tries - 1:
                raise
            await asyncio.sleep(0.6 * (attempt + 1))
            continue
        if r.status_code < 500:
            return r
        last = r
        if attempt < tries - 1:
            await asyncio.sleep(0.6 * (attempt + 1))
    return last  # type: ignore[return-value]


def _cutoff(days: int) -> str:
    return (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

def _qobuz_app_id() -> str:
    return (_config.get("qobuz-app-id") or "").strip() or "312369995"

def _qobuz_token() -> str:
    return (_config.get("qobuz-auth-token") or "").strip()

def _tidal_token() -> str:
    return (_config.get("tidal-token") or "").strip()

def _tidal_country() -> str:
    return (_config.get("tidal-country") or "US").strip().upper() or "US"

def _tidal_cover(uuid: str, size: int = 320) -> str:
    if not uuid:
        return ""
    return f"https://resources.tidal.com/images/{uuid.replace('-', '/')}/{size}x{size}.jpg"

def _decode_jwt(token: str) -> dict:
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    padded = parts[1] + "=" * (4 - len(parts[1]) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(padded))
    except Exception:
        return {}

async def _watchlist_artists(service: str, artists: list) -> list[dict]:
    """Дополнить подписки сервиса теми, за кем следят в ДРУГИХ сервисах.

    Смысл ровно один: релиз должен искаться там, где он появляется раньше, а не
    только там, где на артиста подписан аккаунт. Пока этого не было, Tidal-
    источник с нулём подписок отдавал пустоту, и новозеландское опережение на
    полсуток пропадало впустую.

    Имена берём из вишлиста — это то, чего владелец действительно ждёт, и это
    десятки записей, а не тысячи: полный кросс-обход всех подписок Spotify стоил
    бы часов запросов и бана. Лейблы и SoundCloud пропускаем — там артиста нет.
    Найденное кэшируется на диск, так что платим только за первый проход.
    """
    if (_config or {}).get("radar-cross-service") is False:
        return []
    if not _xref.has_credentials(service):
        return []

    have = {str(a.get("id")) for a in (artists or [])}
    names = sorted({
        str(e.get("name") or "").strip()
        for e in (_watchlist or [])
        if e.get("kind") != "label" and str(e.get("name") or "").strip()
        and (e.get("service") or "apple") != service
    })
    if not names:
        return []

    found = await _xref.resolve_many(names, service)
    extra = [{"id": v["id"], "name": v.get("name") or nm, "via_xref": True}
             for nm, v in found.items() if str(v.get("id")) not in have]
    if extra:
        print(f"[radar] {service}: subscriptions {len(artists)}, added from watchlist "
              f"{len(extra)} (of {len(names)} names)", flush=True)
    return extra


async def _tidal_user_id() -> str:
    token = _tidal_token()
    if not token:
        return ""
    uid = str(_decode_jwt(token).get("uid", "") or "")
    if uid:
        return uid
    return (_config.get("tidal-user-id") or "").strip()


# ── Qobuz ─────────────────────────────────────────────────────────────────────

async def _qobuz_fetch_artist(sem: asyncio.Semaphore, c: httpx.AsyncClient,
                               artist: dict, app_id: str, headers: dict, cutoff: str) -> list[dict]:
    async with sem:
        try:
            r = await _get_retry(c, f"{_QOBUZ_API}/artist/get",
                params={"artist_id": artist["id"], "extra": "albums",
                        "limit": 100, "app_id": app_id},
                headers=headers, tries=2)
            if r.status_code != 200:
                return []
            items = (r.json().get("albums") or {}).get("items") or []
            out   = []
            for alb in items:
                date = (alb.get("release_date_original") or "")[:10]
                if not date or date < cutoff:
                    continue
                alb_id  = str(alb.get("id", ""))
                lbl_raw = alb.get("label")
                label   = lbl_raw.get("name", "") if isinstance(lbl_raw, dict) else str(lbl_raw or "")
                img_raw = alb.get("image")
                cover   = img_raw.get("large", "") if isinstance(img_raw, dict) else ""
                out.append({
                    "id":      alb_id,
                    "title":   alb.get("title", ""),
                    "artist":  artist.get("name", ""),
                    "type":    (alb.get("release_type") or "album").lower(),
                    "date":    date, "year": date[:4],
                    "tracks":  alb.get("tracks_count"),
                    "label":   label, "cover": cover,
                    "url":     alb.get("url", "") or f"https://open.qobuz.com/album/{alb_id}",
                    "hires":   alb.get("hires", False),
                    "artist_id": str(artist.get("id", "")),   # чтобы имя было кликабельно → страница артиста
                    "service": "qobuz",
                })
            return out
        except Exception:
            return []


@router.get("/api/releases/qobuz")
async def qobuz_releases(days: int = 30):
    app_id = _qobuz_app_id()
    token  = _qobuz_token()
    if not token:
        return {"ok": False, "error_key": "err.qobuz_no_token", "error": "Qobuz auth-token не настроен (Settings → Qobuz)", "releases": []}

    headers = {"X-User-Auth-Token": token, "X-App-Id": app_id}
    cutoff  = _cutoff(days)

    try:
        # 1 — paginate followed artists
        artists: list[dict] = []
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            offset = 0
            while True:
                r = await _get_retry(c, f"{_QOBUZ_API}/favorite/getUserFavorites",
                    params={"type": "artists", "limit": 50, "offset": offset, "app_id": app_id},
                    headers=headers)
                if r.status_code == 401:
                    return {"ok": False, "error_key": "err.qobuz_token_expired", "error": "Qobuz: токен истёк. Обнови qobuz-auth-token в Settings.", "releases": []}
                if r.status_code != 200:
                    if artists:
                        break   # частичный радар лучше пустого
                    return {"ok": False, "error": f"Qobuz API {r.status_code}", "releases": []}
                data  = r.json()
                items = (data.get("artists") or {}).get("items") or []
                artists.extend(items)
                total_rep = (data.get("artists") or {}).get("total", len(artists))
                if len(items) < 50 or len(artists) >= total_rep:
                    break
                offset += 50

        artists += await _watchlist_artists("qobuz", artists)
        if not artists:
            return {"ok": True, "releases": [], "artists_checked": 0}

        total = len(artists)
        if _broadcast:
            await _broadcast({"type": "releases_scan_start", "phase": "albums",
                               "total": total, "service": "qobuz"})

        # 2 — concurrent fan-out: 10 artists at a time
        completed  = [0]
        found_so_far = [0]
        step = max(1, total // 20)

        async with httpx.AsyncClient(timeout=_TIMEOUT, limits=httpx.Limits(max_connections=20)) as c:
            sem = asyncio.Semaphore(_CONCURRENCY)

            async def _fetch_one(artist: dict) -> list[dict]:
                result = await _qobuz_fetch_artist(sem, c, artist, app_id, headers, cutoff)
                completed[0] += 1
                found_so_far[0] += len(result)
                if _broadcast and (completed[0] % step == 0 or completed[0] == total):
                    await _broadcast({
                        "type": "releases_scan_progress",
                        "current": completed[0], "total": total,
                        "artist": artist.get("name", "?"),
                        "found":  found_so_far[0],
                        "service": "qobuz",
                    })
                return result

            batches = await asyncio.gather(*[_fetch_one(a) for a in artists])

        releases: list[dict] = []
        seen: set[str]       = set()
        for batch in batches:
            for rel in batch:
                if rel["id"] not in seen:
                    seen.add(rel["id"])
                    releases.append(rel)

        releases.sort(key=lambda x: x["date"], reverse=True)

        if _broadcast:
            await _broadcast({"type": "releases_scan_done", "artists_checked": total,
                               "releases_count": len(releases), "service": "qobuz"})

        return {"ok": True, "releases": releases, "artists_checked": total}

    except Exception as e:
        return {"ok": False, "error": str(e), "releases": []}


# ── Tidal ─────────────────────────────────────────────────────────────────────

async def _tidal_fetch_artist(sem: asyncio.Semaphore, c: httpx.AsyncClient,
                               artist: dict, cc: str, hdr: dict, cutoff: str) -> list[dict]:
    async with sem:
        try:
            # paginate — some artists have 100+ releases
            items: list[dict] = []
            offset = 0
            while True:
                r = await _get_retry(c, f"{_TIDAL_API}/artists/{artist['id']}/albums",
                    params={"limit": 100, "offset": offset, "countryCode": cc, "filter": "ALL"},
                    headers=hdr, tries=2)
                if r.status_code != 200:
                    break
                page = r.json().get("items") or []
                items.extend(page)
                if len(page) < 100:
                    break
                offset += 100
            out = []
            for alb in items:
                date = (alb.get("releaseDate") or "")[:10]
                if not date or date < cutoff:
                    continue
                alb_id    = str(alb.get("id", ""))
                alb_type  = alb.get("type", "ALBUM").lower()
                type_norm = {"album": "album", "ep": "ep", "single": "single",
                             "compilation": "compilation"}.get(alb_type, "album")
                out.append({
                    "id":      alb_id,
                    "title":   alb.get("title", ""),
                    "artist":  artist.get("name", ""),
                    "type":    type_norm,
                    "date":    date, "year": date[:4],
                    "tracks":  alb.get("numberOfTracks"),
                    "label":   "",
                    "cover":   _tidal_cover(alb.get("cover", "")),
                    "url":     f"https://listen.tidal.com/album/{alb_id}",
                    "artist_id": str(artist.get("id", "")),   # чтобы имя было кликабельно → страница артиста
                    "service": "tidal",
                    # Дата у Tidal бывает завтрашней, а файл уже отдаётся — это и
                    # есть новозеландское опережение. Отличить «уже можно взять»
                    # от анонса позволяет только этот флаг, поэтому несём его
                    # дальше вместо того, чтобы резать всё будущее по дате.
                    "stream_ready": bool(alb.get("streamReady", True)),
                })
            return out
        except Exception:
            return []


@router.get("/api/releases/tidal")
async def tidal_releases(days: int = 30):
    token = _tidal_token()
    if not token:
        return {"ok": False, "error_key": "err.tidal_no_token", "error": "Tidal token не настроен (Settings → Tidal)", "releases": []}

    hdr    = {"Authorization": f"Bearer {token}"}
    cc     = _tidal_country()
    cutoff = _cutoff(days)

    try:
        user_id = await _tidal_user_id()
        if not user_id:
            return {"ok": False, "error_key": "err.tidal_no_user_id", "error": "Не удалось определить Tidal user_id", "releases": []}

        # 1 — paginate favourite artists
        artists: list[dict] = []
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            offset = 0
            while True:
                r = await _get_retry(c, f"{_TIDAL_API}/users/{user_id}/favorites/artists",
                    params={"limit": 100, "offset": offset, "countryCode": cc},
                    headers=hdr)
                if r.status_code == 401:
                    return {"ok": False, "error_key": "err.tidal_token_expired_settings", "error": "Tidal: токен истёк. Обнови в Settings → Tidal.", "releases": []}
                if r.status_code != 200:
                    # Отдали то, что уже собрали до сбоя — частичный радар лучше пустого.
                    if artists:
                        break
                    return {"ok": False, "error": f"Tidal API {r.status_code}", "releases": []}
                data  = r.json()
                items = data.get("items") or []
                for it in items:
                    a = it.get("item") or it
                    if a.get("id"):
                        artists.append(a)
                if len(items) < 100:
                    break
                offset += 100

        # Служебный Tidal-аккаунт держат ради новозеландской витрины, а не ради
        # подписок — их там ноль. Без этой строки источник всегда пуст.
        artists += await _watchlist_artists("tidal", artists)
        if not artists:
            return {"ok": True, "releases": [], "artists_checked": 0}

        total = len(artists)
        if _broadcast:
            await _broadcast({"type": "releases_scan_start", "phase": "albums",
                               "total": total, "service": "tidal"})

        # 2 — concurrent fan-out
        completed    = [0]
        found_so_far = [0]
        step = max(1, total // 20)

        async with httpx.AsyncClient(timeout=_TIMEOUT, limits=httpx.Limits(max_connections=20)) as c:
            sem = asyncio.Semaphore(_CONCURRENCY)

            async def _fetch_one(artist: dict) -> list[dict]:
                result = await _tidal_fetch_artist(sem, c, artist, cc, hdr, cutoff)
                completed[0] += 1
                found_so_far[0] += len(result)
                if _broadcast and (completed[0] % step == 0 or completed[0] == total):
                    await _broadcast({
                        "type": "releases_scan_progress",
                        "current": completed[0], "total": total,
                        "artist": artist.get("name", "?"),
                        "found":  found_so_far[0],
                        "service": "tidal",
                    })
                return result

            batches = await asyncio.gather(*[_fetch_one(a) for a in artists])

        releases: list[dict] = []
        seen: set[str]       = set()
        for batch in batches:
            for rel in batch:
                if rel["id"] not in seen:
                    seen.add(rel["id"])
                    releases.append(rel)

        releases.sort(key=lambda x: x["date"], reverse=True)

        if _broadcast:
            await _broadcast({"type": "releases_scan_done", "artists_checked": total,
                               "releases_count": len(releases), "service": "tidal"})

        return {"ok": True, "releases": releases, "artists_checked": total}

    except Exception as e:
        return {"ok": False, "error": str(e), "releases": []}


# ── Deezer ────────────────────────────────────────────────────────────────────
# Последний источник, которого не хватало радару: Spotify, Qobuz, Tidal, Apple,
# BBC и SoundCloud уже были. ARL — это полноценная сессия аккаунта, и любимые
# артисты читаются из неё напрямую, без отдельного OAuth.

async def _deezer_user_id(arl: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, cookies={"arl": arl}) as c:
            r = await c.post("https://www.deezer.com/ajax/gw-light.php",
                             params={"method": "deezer.getUserData",
                                     "api_version": "1.0", "api_token": ""})
        return str((((r.json() or {}).get("results") or {}).get("USER") or {}).get("USER_ID") or "")
    except Exception:
        return ""


async def _deezer_fetch_artist(sem: asyncio.Semaphore, c: httpx.AsyncClient,
                               artist: dict, cutoff: str) -> list[dict]:
    async with sem:
        try:
            aid = str(artist.get("id") or "")
            if not aid:
                return []
            r = await _get_retry(c, f"https://api.deezer.com/artist/{aid}/albums",
                                 params={"limit": 50}, tries=2)
            out = []
            for alb in (r.json().get("data") or []):
                date = str(alb.get("release_date") or "")[:10]
                if not date or date < cutoff:
                    continue
                alb_id = str(alb.get("id") or "")
                if not alb_id:
                    continue
                rt = (alb.get("record_type") or "album").lower()
                out.append({
                    "id":      f"dz{alb_id}",
                    "title":   alb.get("title", ""),
                    "artist":  artist.get("name", ""),
                    "type":    {"album": "album", "ep": "ep", "single": "single",
                                "compile": "compilation"}.get(rt, rt),
                    "date":    date, "year": date[:4],
                    "tracks":  alb.get("nb_tracks"),
                    "label":   "",
                    "cover":   alb.get("cover_medium") or alb.get("cover") or "",
                    "url":     alb.get("link") or f"https://www.deezer.com/album/{alb_id}",
                    "artist_id": aid,
                    "service": "deezer",
                })
            return out
        except Exception:
            return []


@router.get("/api/releases/deezer")
async def deezer_releases(days: int = 30):
    arl = (_config.get("deezer-arl") or "").strip() if _config else ""
    if not arl:
        return {"ok": False, "error_key": "err.deezer_no_arl", "error": "Deezer ARL не настроен (Settings → Deezer)", "releases": []}

    uid = await _deezer_user_id(arl)
    if not uid:
        return {"ok": False, "error_key": "err.deezer_arl_invalid", "error": "Deezer: ARL недействителен или истёк — обнови его в Settings.",
                "releases": []}

    cutoff = _cutoff(days)
    try:
        artists: list[dict] = []
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            index = 0
            while True:
                r = await _get_retry(c, f"https://api.deezer.com/user/{uid}/artists",
                                     params={"limit": 100, "index": index})
                j = r.json() or {}
                if j.get("error"):
                    return {"ok": False,
                            "error": f"Deezer API: {str(j.get('error'))[:120]}", "releases": []}
                items = j.get("data") or []
                artists.extend(items)
                if len(items) < 100 or not j.get("next"):
                    break
                index += 100

        artists += await _watchlist_artists("deezer", artists)
        if not artists:
            return {"ok": True, "releases": [], "artists_checked": 0}

        total = len(artists)
        if _broadcast:
            await _broadcast({"type": "releases_scan_start", "phase": "albums",
                              "total": total, "service": "deezer"})

        completed = [0]
        found_so_far = [0]
        step = max(1, total // 20)

        async with httpx.AsyncClient(timeout=_TIMEOUT,
                                     limits=httpx.Limits(max_connections=20)) as c:
            sem = asyncio.Semaphore(_CONCURRENCY)

            async def _fetch_one(a: dict) -> list[dict]:
                res = await _deezer_fetch_artist(sem, c, a, cutoff)
                completed[0] += 1
                found_so_far[0] += len(res)
                if _broadcast and (completed[0] % step == 0 or completed[0] == total):
                    await _broadcast({"type": "releases_scan_progress",
                                      "current": completed[0], "total": total,
                                      "artist": a.get("name", "?"),
                                      "found": found_so_far[0], "service": "deezer"})
                return res

            batches = await asyncio.gather(*[_fetch_one(a) for a in artists])

        releases: list[dict] = []
        seen: set[str] = set()
        for batch in batches:
            for rel in batch:
                if rel["id"] not in seen:
                    seen.add(rel["id"])
                    releases.append(rel)
        releases.sort(key=lambda x: x["date"], reverse=True)

        if _broadcast:
            await _broadcast({"type": "releases_scan_done", "artists_checked": total,
                              "releases_count": len(releases), "service": "deezer"})
        return {"ok": True, "releases": releases, "artists_checked": total}
    except Exception as e:
        return {"ok": False, "error": str(e), "releases": []}
