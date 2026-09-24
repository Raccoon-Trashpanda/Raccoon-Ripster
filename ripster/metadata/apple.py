"""Apple Music metadata — bearer auto-fetch + catalog API."""
from __future__ import annotations

import asyncio
import re
import urllib.parse
import urllib.request
from typing import Optional

import httpx
from ripster import http_client as _HTTP

_cfg: dict = {}
_broadcast = None
_save_config = None

_JWT_RE = re.compile(
    r"""["']?(eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,})["']?"""
)
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
_TIMEOUT = httpx.Timeout(connect=10, read=15, write=10, pool=5)


def install(cfg: dict, broadcast_fn, save_config_fn) -> None:
    global _cfg, _broadcast, _save_config
    _cfg         = cfg
    _broadcast   = broadcast_fn
    _save_config = save_config_fn


def _extract_jwt(text: str) -> Optional[str]:
    hits = _JWT_RE.findall(text)
    hits = [h for h in hits if 150 < len(h) < 3000]
    return max(hits, key=len) if hits else None


def _safe_print(msg: str) -> None:
    """Print with Unicode sanitized for Windows console compatibility."""
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:
        print(msg.encode("ascii", "replace").decode("ascii"), flush=True)


async def auto_fetch_bearer() -> Optional[str]:
    """Fetch Apple Music public JWT embedded in the web player."""
    loop = asyncio.get_running_loop()

    def _fetch_sync(url: str) -> str:
        req = urllib.request.Request(url, headers=_HEADERS)
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.read().decode("utf-8", errors="replace")

    try:
        _safe_print("[bearer] Fetching music.apple.com...")
        html = await loop.run_in_executor(None, lambda: _fetch_sync("https://music.apple.com/"))

        # Method 1: Ember/Fastboot environment meta tag
        meta_match = re.search(
            r'name="desktop-music-app/config/environment"[^>]+content="([^"]+)"', html)
        if meta_match:
            env_json = urllib.parse.unquote(meta_match.group(1))
            t = _extract_jwt(env_json)
            if t:
                _safe_print(f"[bearer] OK Found in <meta> config ({len(t)} chars)")
                return t

        # Method 2: Fastboot shoebox scripts
        for sb in re.findall(
                r'<script[^>]+type="fastboot/shoebox"[^>]*>(.*?)</script>', html, re.DOTALL):
            t = _extract_jwt(sb)
            if t:
                _safe_print(f"[bearer] OK Found in shoebox ({len(t)} chars)")
                return t

        # Method 3: Any JWT-shaped string directly in the HTML
        t = _extract_jwt(html)
        if t:
            _safe_print(f"[bearer] OK Found inline in HTML ({len(t)} chars)")
            return t

        # Method 4: Scan JS bundles with extended patterns
        js_urls = list(dict.fromkeys(re.findall(r'src="(/[^"]+\.js(?:\?[^"]*)?)"', html)))
        priority = [u for u in js_urls if any(k in u for k in ("index", "chunk", "vendor", "app", "main"))]
        ordered  = (priority + [u for u in js_urls if u not in priority])[:16]

        _safe_print(f"[bearer] Scanning {len(ordered)} JS bundles...")
        for js_path in ordered:
            js_url = f"https://music.apple.com{js_path}" if js_path.startswith("/") else js_path
            try:
                js = await loop.run_in_executor(None, lambda u=js_url: _fetch_sync(u))
                for pattern in [
                    r'token\s*[:=]\s*["\x27](eyJ[A-Za-z0-9_-]{100,})',
                    r'authorization["\x27]?\s*:\s*["\x27]Bearer\s+(eyJ[A-Za-z0-9_-]{100,})',
                    r'"(eyJ[A-Za-z0-9_-]{200,}\.[A-Za-z0-9_-]{50,}\.[A-Za-z0-9_-]{20,})"',
                    r"'(eyJ[A-Za-z0-9_-]{200,}\.[A-Za-z0-9_-]{50,}\.[A-Za-z0-9_-]{20,})'",
                    r'amkToken["\x27]?\s*[:=]\s*["\x27](eyJ[A-Za-z0-9_-]{100,})',
                    r'developerToken["\x27]?\s*[:=]\s*["\x27](eyJ[A-Za-z0-9_-]{100,})',
                    r'musicUserToken["\x27]?\s*[:=]\s*["\x27](eyJ[A-Za-z0-9_-]{100,})',
                ]:
                    m = re.search(pattern, js)
                    if m:
                        t = m.group(1)
                        if 100 < len(t) < 3000:
                            _safe_print(f"[bearer] OK Found in JS bundle ({len(t)} chars)")
                            return t
                # Method 5: find any JWT in JS bundle
                t = _extract_jwt(js)
                if t:
                    _safe_print(f"[bearer] OK Found JWT in JS bundle ({len(t)} chars)")
                    return t
            except Exception:
                continue

        _safe_print("[bearer] FAIL JWT not found in any source")
        return None
    except Exception as e:
        _safe_print(f"[bearer] Failed: {e}")
        return None


def _parse_apple_url(url: str):
    """Return (sf, api_type, api_id) or raise ValueError."""
    parsed = urllib.parse.urlparse(url)
    parts  = [p for p in parsed.path.split("/") if p]
    if len(parts) < 2:
        raise ValueError(f"Cannot parse Apple Music URL: {url}")

    sf_url   = parts[0]
    type_url = parts[1]
    id_url   = parts[-1].split("?")[0]
    song_id  = urllib.parse.parse_qs(parsed.query).get("i", [None])[0]

    # Apple radio stations / DJ-mix episodes (URL has /station/ and an `ra.<id>`
    # id) are a DRM-protected RADIO STREAM (playParams.kind=radioStation,
    # hasDrm=true, supportedDrms=fairplay/playready/widevine), NOT a catalog
    # song/album. The downloader engines decrypt catalog SONG assets (adamId +
    # HLS + FairPlay key via the wrapper) and cannot handle a radioStation stream,
    # so reject it up front with an honest reason instead of a cryptic failure.
    if type_url == "station" or id_url.startswith("ra."):
        raise ValueError(
            "Apple-радио и станции (DJ-миксы, ссылки ra.*) скачать нельзя — это "
            "защищённый DRM radio-поток, а не трек/альбом каталога. Дай ссылку на "
            "трек, альбом или плейлист.")

    # Numeric check — URL must end in a numeric ID
    if not id_url.isdigit():
        # Try finding a numeric segment
        numeric = next((p for p in reversed(parts) if p.isdigit()), None)
        if numeric:
            id_url = numeric
        else:
            raise ValueError(f"No numeric ID in Apple Music URL: {url}")

    if type_url == "album" and song_id:
        return sf_url, "songs", song_id
    if type_url == "album":
        return sf_url, "albums", id_url
    if type_url == "song":
        return sf_url, "songs", id_url
    if type_url == "music-video":
        # Music videos live at the catalog /music-videos/{id} endpoint — without
        # this they fell through to "albums" and the fallback 404'd, so the queue
        # card got no title/cover.
        return sf_url, "music-videos", id_url
    if type_url == "playlist":
        return sf_url, "playlists", id_url
    if type_url == "artist":
        return sf_url, "artists", id_url
    return sf_url, "albums", id_url


async def _fetch_catalog(sf: str, api_type: str, api_id: str, bearer: str, mut: str) -> dict:
    api_url = f"https://api.music.apple.com/v1/catalog/{sf}/{api_type}/{api_id}"
    headers = {
        "Authorization": f"Bearer {bearer}",
        "Origin": "https://music.apple.com",
        "Referer": "https://music.apple.com/",
    }
    if mut:
        headers["media-user-token"] = mut
    async with _HTTP.ashared() as c:
        r = await c.get(api_url, headers=headers)
    if r.status_code == 401:
        raise RuntimeError(f"HTTP 401 Unauthorized — bearer истёк")
    if r.status_code == 403:
        raise RuntimeError(f"HTTP 403 Forbidden — проверь storefront или токен")
    if r.status_code == 404:
        raise RuntimeError(f"HTTP 404 — трек/альбом не найден (ID: {api_id}, sf: {sf})")
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
    return r.json()


async def _catalog_album_tracks(sf: str, api_id: str) -> list:
    """Треклист альбома из каталога Apple — когда iTunes Lookup треков не дал.

    23.09.2026: у DJ-миксов (Hospital30: Laurence Guy) iTunes отдаёт ТОЛЬКО сам
    альбом, `resultCount: 1`, ни одной песни — а загрузчик в ту же минуту видел
    12 треков через каталог. Карточка при этом получала пустой список с
    пометкой «полный». Каталог отдаёт `relationships.tracks` у альбома сам.
    Никогда не бросает: треклист — украшение, не повод ронять метаданные."""
    try:
        bearer = (_cfg.get("authorization-token") or "").strip() or (await auto_fetch_bearer() or "")
        if not bearer:
            return []
        data = await _fetch_catalog(sf, "albums", api_id, bearer,
                                    (_cfg.get("media-user-token") or "").strip())
        item = (data.get("data") or [{}])[0]
        rows = ((item.get("relationships") or {}).get("tracks") or {}).get("data") or []
        out = []
        for t in rows:
            a = t.get("attributes") or {}
            out.append({
                "num":    a.get("trackNumber") or len(out) + 1,
                "title":  a.get("name") or "",
                "artist": a.get("artistName") or "",
                "dur":    int((a.get("durationInMillis") or 0) / 1000),
            })
        return out
    except Exception as e:                              # noqa: BLE001
        print(f"[meta:apple] треклист из каталога не получен: {type(e).__name__}", flush=True)
        return []


async def _itunes_lookup_raw(api_id: str, sf: str) -> list:
    """iTunes public Lookup API — no auth required. Все результаты запроса.

    `entity=song` добавлен ради дерева очереди: тот же самый ответ, что кормит
    карточку, содержит и поимённый треклист альбома (номер, название, исполнитель,
    длительность) — второй запрос за тем же не нужен. Без `limit` iTunes отдаёт
    только первые 25 записей."""
    try:
        async with _HTTP.ashared() as c:
            r = await c.get("https://itunes.apple.com/lookup", params={
                "id":      api_id,
                "country": sf,
                "entity":  "song",
                "limit":   200,
            })
        if r.status_code != 200:
            return []
        return (r.json().get("results") or [])
    except Exception:
        return []


async def _itunes_lookup(api_id: str, sf: str) -> Optional[dict]:
    """Первый результат публичного Lookup — это сам релиб (или трек для /song/)."""
    results = await _itunes_lookup_raw(api_id, sf)
    return results[0] if results else None


def _tracks_from_itunes(results: list) -> list:
    """Треклист альбома из того же ответа iTunes, что и карточка."""
    out = []
    for i, t in enumerate(results, 1):
        if t.get("wrapperType") != "track":
            continue
        out.append({
            "num":     t.get("trackNumber") or len(out) + 1,
            "title":   t.get("trackName") or "",
            "artist":  t.get("artistName") or "",
            "dur":     int((t.get("trackTimeMillis") or 0) / 1000),
        })
    return out


def _art_from_itunes(item: dict, size: int = 600) -> str:
    raw = item.get("artworkUrl100") or item.get("artworkUrl60") or ""
    if not raw:
        return ""
    return re.sub(r'\d+x\d+bb', f'{size}x{size}bb', raw)


async def fetch_meta(url: str) -> Optional[dict]:
    """Fetch Apple Music metadata: iTunes Lookup API first, Catalog API as fallback."""
    try:
        sf, api_type, api_id = _parse_apple_url(url)
    except ValueError as e:
        raise RuntimeError(str(e))

    print(f"[meta:apple] {sf}/{api_type}/{api_id}", flush=True)

    # ── Primary: iTunes public Lookup (no auth) ───────────────────────────────
    results = await _itunes_lookup_raw(api_id, sf)
    item = results[0] if results else None
    if item:
        is_track = item.get("wrapperType") == "track"
        title    = item.get("trackName") if is_track else item.get("collectionName", "")
        art_url  = _art_from_itunes(item)
        # For a SINGLE track (a /song/ link, or an /album/…?i=<track> link) iTunes
        # still reports the PARENT ALBUM's `trackCount` (e.g. 10). Taking it made a
        # one-song request look like a 10-track album: the card showed "10 трек."
        # and the post-download silent-partial guard cried "скачалось 1 из 10".
        # A single track is always 1 track.
        tc       = 1 if is_track else (item.get("trackCount") or 0)
        _tl      = [] if is_track else (_tracks_from_itunes(results)
                                        or await _catalog_album_tracks(sf, api_id))
        return {
            "id":          str(api_id),
            "type":        "songs" if is_track else "albums",
            "albumType":   item.get("collectionType", ""),
            "title":       title or "",
            "artist":      item.get("artistName", ""),
            "album":       item.get("collectionName", ""),
            "year":        (item.get("releaseDate") or "")[:4],
            # Полная дата — preorder.detect: у предзаказа releaseDate в будущем,
            # а «год» для плана «докачать после полуночи учётки» слишком груб.
            "date":        (item.get("releaseDate") or "")[:10],
            "genre":       item.get("primaryGenreName", ""),
            "trackNumber": item.get("trackNumber"),
            "totalTracks": 1 if is_track else item.get("trackCount"),
            "discNumber":  item.get("discNumber"),
            "isrc":        "",
            "upc":         "",
            "label":       "",
            "copyright":   item.get("copyright", ""),
            "explicit":    item.get("trackExplicitness") == "explicit"
                           or item.get("collectionExplicitness") == "explicit",
            "artworkUrl":  art_url,
            "storefront":  sf,
            "duration":    item.get("trackTimeMillis"),
            "trackCount":  tc,
            "tracks":      _tl,
            # iTunes молча не отдаёт треки, закрытые в этом storefront, —
            # 15 из заявленных 16 это полный ответ, а не обрезок: resolver
            # спрашивает тот же iTunes и получит то же самое. Но НОЛЬ треков у
            # альбома — не «полный ответ», а «iTunes этого не знает» (DJ-миксы).
            "tracksComplete": bool(_tl) or is_track,
            "service":     "apple",
        }

    # ── Fallback: Apple Music Catalog API (requires bearer) ───────────────────
    print("[meta:apple] iTunes lookup empty — trying Catalog API…", flush=True)
    bearer = _cfg.get("authorization-token", "").strip()
    mut    = _cfg.get("media-user-token", "").strip()

    if not bearer:
        bearer = await auto_fetch_bearer()
        if bearer:
            _cfg["authorization-token"] = bearer
            if _save_config: _save_config(_cfg)
            if _broadcast:
                await _broadcast({"type": "bearer_updated"})
        else:
            raise RuntimeError("Метаданные недоступны: iTunes lookup пуст и bearer не найден")

    try:
        data = await _fetch_catalog(sf, api_type, api_id, bearer, mut)
    except RuntimeError as e:
        if "401" in str(e):
            bearer = await auto_fetch_bearer()
            if bearer:
                _cfg["authorization-token"] = bearer
                if _save_config: _save_config(_cfg)
                data = await _fetch_catalog(sf, api_type, api_id, bearer, mut)
            else:
                raise RuntimeError("Bearer истёк и не удалось обновить")
        else:
            raise

    item    = (data.get("data") or [{}])[0]
    if not item:
        raise RuntimeError(f"Пустой ответ Apple API для {api_type}/{api_id}")

    a       = item.get("attributes") or {}
    artwork = a.get("artwork") or {}
    art_url = artwork.get("url", "").replace("{w}x{h}", "600x600") if artwork else ""

    # A /song/ or /music-video/ request is a single track — never inherit the
    # parent album's trackCount (same one-vs-many trap as the iTunes path above).
    _single_tc = 1 if api_type in ("songs", "music-videos") else a.get("trackCount")

    return {
        "id":          item.get("id", ""),
        "type":        item.get("type", ""),
        "albumType":   a.get("albumType", ""),
        "title":       a.get("name", ""),
        "artist":      a.get("artistName", ""),
        "album":       a.get("albumName") or a.get("name") or "",
        "year":        (a.get("releaseDate") or "")[:4],
        "date":        (a.get("releaseDate") or "")[:10],
        # Каталог Apple честно говорит «это предзаказ» и когда его открыть.
        "isPreRelease": bool(a.get("isPreRelease")),
        "preReleaseReleaseDate": (a.get("preReleaseReleaseDate") or "")[:10],
        "genre":       (a.get("genreNames") or [""])[0],
        "trackNumber": a.get("trackNumber"),
        "totalTracks": _single_tc,
        "discNumber":  a.get("discNumber"),
        "isrc":        a.get("isrc", ""),
        "upc":         a.get("upc", ""),
        "label":       a.get("recordLabel", ""),
        "copyright":   a.get("copyright", ""),
        "explicit":    a.get("contentRating") == "explicit",
        "artworkUrl":  art_url,
        "artworkBg":   ("#" + artwork["bgColor"]) if artwork.get("bgColor") else None,
        "hasAtmos":    "atmos" in (a.get("audioTraits") or []),
        "hasHiRes":    "hi-res-lossless" in (a.get("audioTraits") or []),
        "formats":     list(set(
            (a.get("audioTraits") or []) + (a.get("audioVariants") or []))),
        "storefront":  sf,
        "duration":    a.get("durationInMillis"),
        "trackCount":  _single_tc,
        "service":     "apple",
    }


async def content_id(url: str) -> str:
    """Canonical content id for cache/dedup: ``isrc:<ISRC>`` (song) or
    ``upc:<UPC>`` (album), '' if unavailable. Unlike fetch_meta this goes
    STRAIGHT to the Catalog API — the iTunes Lookup API (fetch_meta's primary
    source) never returns ISRC/UPC, so it can't be used for cross-service dedup.
    Best-effort: returns '' on any failure so the caller falls back to a url: id."""
    try:
        sf, api_type, api_id = _parse_apple_url(url)
    except ValueError:
        return ""
    if api_type not in ("songs", "albums"):
        return ""
    # Альбом без UPC (каталог молчит / недоступен) → строгий матч в Deezer по
    # имени+артисту+числу треков+году, результат навсегда в дисковой карте.
    if api_type == "albums":
        known = _upc_map_get(api_id)
        if known:
            return f"upc:{known}"
    cid = await _content_id_catalog(sf, api_type, api_id)
    if cid or api_type != "albums":
        return cid
    return await _album_upc_by_name(sf, api_id)


async def _content_id_catalog(sf: str, api_type: str, api_id: str) -> str:
    bearer = (_cfg.get("authorization-token") or "").strip()
    mut    = (_cfg.get("media-user-token") or "").strip()
    if not bearer:
        bearer = await auto_fetch_bearer()
        if bearer:
            _cfg["authorization-token"] = bearer
            if _save_config:
                _save_config(_cfg)
    if not bearer:
        return ""
    try:
        data = await _fetch_catalog(sf, api_type, api_id, bearer, mut)
    except RuntimeError as e:
        if "401" in str(e):
            bearer = await auto_fetch_bearer()
            if not bearer:
                return ""
            _cfg["authorization-token"] = bearer
            if _save_config:
                _save_config(_cfg)
            try:
                data = await _fetch_catalog(sf, api_type, api_id, bearer, mut)
            except Exception:
                return ""
        else:
            return ""
    except Exception:
        return ""
    a = ((data.get("data") or [{}])[0].get("attributes") or {})
    if api_type == "songs" and a.get("isrc"):
        return f"isrc:{a['isrc']}"
    if api_type == "albums" and a.get("upc"):
        _upc_map_put(api_id, str(a["upc"]), "apple")
        return f"upc:{a['upc']}"
    return ""


# ── Apple album-id → UPC: disk map + strict Deezer name match ────────────────
# Каталог Apple иногда не отдаёт `upc` (или сам недоступен: протух bearer,
# даун), и тогда альбом уходил в `url:`-ключ — кэш бота не скрещивал его с тем
# же релизом из Deezer/Qobuz. Запасной путь: публичный iTunes Lookup (без
# авторизации) даёт имя/артиста/число треков/дату → публичный поиск Deezer →
# принимаем ТОЛЬКО строгое совпадение (нормализованные артист и название равны,
# число треков равно, год равен, если известен; несколько разных UPC = отказ).
# Неверный UPC хуже его отсутствия: бот отдал бы ЧУЖОЙ релиз из кэша.
# Найденное пишется на диск: альбом стоит один поиск за всю жизнь. Промах тоже
# пишется, но с TTL — релиз может появиться в Deezer позже.
_UPC_MAP: dict | None = None
_UPC_MISS_TTL = 7 * 86400


def _upc_map_path():
    import os
    from pathlib import Path as _P
    base = _P(os.environ.get("RIPSTER_BASE_DIR") or _P(__file__).resolve().parent.parent.parent)
    return base / "dist" / "apple_upc_map.json"


def _upc_map() -> dict:
    global _UPC_MAP
    if _UPC_MAP is None:
        try:
            import json as _j
            d = _j.loads(_upc_map_path().read_text(encoding="utf-8"))
            _UPC_MAP = d if isinstance(d, dict) else {}
        except Exception:
            _UPC_MAP = {}
    return _UPC_MAP


def _upc_map_save() -> None:
    try:
        import json as _j
        p = _upc_map_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(_j.dumps(_upc_map(), ensure_ascii=False), encoding="utf-8")
        tmp.replace(p)
    except Exception as e:                       # noqa: BLE001
        _safe_print(f"[apple-upc] map not saved: {e!r}")


def _upc_map_get(api_id: str) -> str:
    e = _upc_map().get(str(api_id)) or {}
    return str(e.get("upc") or "")


def _upc_map_miss_fresh(api_id: str) -> bool:
    import time as _t
    e = _upc_map().get(str(api_id)) or {}
    return (not e.get("upc")) and (_t.time() - float(e.get("ts") or 0)) < _UPC_MISS_TTL


def _upc_map_put(api_id: str, upc: str, src: str) -> None:
    import time as _t
    m = _upc_map()
    cur = m.get(str(api_id)) or {}
    if upc and cur.get("upc") == upc:
        return
    m[str(api_id)] = {"upc": upc, "src": src, "ts": _t.time()}
    _upc_map_save()


_APPLE_SUFFIX_RE = re.compile(r"\s+-\s+(single|ep)\s*$", re.I)


def _norm_name(s: str) -> str:
    import unicodedata
    s = _APPLE_SUFFIX_RE.sub("", s or "")
    s = unicodedata.normalize("NFKD", s).casefold()
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.replace("&", " and ")
    return re.sub(r"[^\w]+", "", s)


def _artist_set(s: str) -> frozenset:
    parts = re.split(r"\s*(?:,|&|\band\b|\bfeat\.?|\bft\.?|\bx\b)\s*", s or "", flags=re.I)
    return frozenset(n for n in (_norm_name(p) for p in parts) if n)


def strict_album_match(want: dict, cand: dict) -> bool:
    """Pure predicate (unit-testable). want/cand: {title, artists:[...]|artist,
    tracks:int, year:str}. Title and artist set must be equal after
    normalization, track count equal, year equal when both are known."""
    if not want.get("title") or _norm_name(want["title"]) != _norm_name(cand.get("title", "")):
        return False
    wa = _artist_set(want.get("artist", ""))
    ca = frozenset(n for n in (_norm_name(x) for x in (cand.get("artists") or [])) if n) \
        or _artist_set(cand.get("artist", ""))
    if not wa or wa != ca:
        return False
    wt, ct = int(want.get("tracks") or 0), int(cand.get("tracks") or 0)
    if not wt or wt != ct:
        return False
    wy, cy = str(want.get("year") or "")[:4], str(cand.get("year") or "")[:4]
    if wy and cy and wy != cy:
        return False
    return True


async def _album_upc_by_name(sf: str, api_id: str) -> str:
    if _upc_map_miss_fresh(api_id):
        return ""
    item = await _itunes_lookup(api_id, sf)
    if not item or item.get("wrapperType") not in (None, "collection"):
        return ""          # lookup failed → не пишем промах, попробуем в следующий раз
    want = {"title": item.get("collectionName") or "",
            "artist": item.get("artistName") or "",
            "tracks": item.get("trackCount") or 0,
            "year": (item.get("releaseDate") or "")[:4]}
    if not (want["title"] and want["artist"] and want["tracks"]):
        return ""
    bare = _APPLE_SUFFIX_RE.sub("", want["title"]).strip()
    found: set[str] = set()
    try:
        async with _HTTP.ashared() as c:
            r = await c.get("https://api.deezer.com/search/album",
                            params={"q": f'artist:"{want["artist"]}" album:"{bare}"', "limit": 10})
            hits = (r.json() or {}).get("data") or [] if r.status_code == 200 else []
            if not hits:
                r = await c.get("https://api.deezer.com/search/album",
                                params={"q": f"{want['artist']} {bare}", "limit": 10})
                hits = (r.json() or {}).get("data") or [] if r.status_code == 200 else []
            for h in hits:
                if _norm_name(h.get("title", "")) != _norm_name(bare):
                    continue
                if int(h.get("nb_tracks") or 0) != int(want["tracks"]):
                    continue
                d = (await c.get(f"https://api.deezer.com/album/{h['id']}")).json() or {}
                if d.get("error") or not d.get("upc"):
                    continue
                arts = [x.get("name", "") for x in (d.get("contributors") or [])
                        if (x.get("role") or "Main") == "Main"] or [(d.get("artist") or {}).get("name", "")]
                cand = {"title": d.get("title", ""), "artists": arts,
                        "tracks": d.get("nb_tracks") or 0, "year": (d.get("release_date") or "")[:4]}
                if strict_album_match(want, cand):
                    found.add(str(d["upc"]).lstrip("0").zfill(13))
    except Exception as e:                       # noqa: BLE001
        _safe_print(f"[apple-upc] deezer match failed for {api_id}: {e!r}")
        return ""
    if len(found) == 1:
        upc = found.pop()
        _upc_map_put(api_id, upc, "deezer-match")
        _safe_print(f"[apple-upc] {api_id} -> {upc} (strict Deezer match)")
        return f"upc:{upc}"
    _upc_map_put(api_id, "", "ambiguous" if found else "no-match")
    return ""
