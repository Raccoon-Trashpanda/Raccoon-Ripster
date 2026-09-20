"""Tagger — Mp3tag-style metadata editor.

Reads tags from local files into a table, fetches canonical metadata from a
service (by embedded ISRC, else by artist+title search), and writes the chosen
tags back. Reuses ripster.tagger for read/write across FLAC/MP3/M4A/OGG.

Endpoints:
  POST /api/tagger/read   {dir?|files?}            -> rows of current tags
  POST /api/tagger/match  {path, service}          -> proposed tags for one file
  POST /api/tagger/apply  {path, fields}           -> write tags to the file

Install: tagger_routes.install(app, ctx)
"""
from __future__ import annotations

from pathlib import Path

import httpx
from fastapi import APIRouter, Request, HTTPException
from fastapi.concurrency import run_in_threadpool

from ripster import tagger as _tg
from ripster.i18n_msg import imsg

router = APIRouter()
_cfg: dict = {}

# Services the tagger can pull from (v1 = no-auth, easy). More are wired as the
# matchers below gain coverage (Qobuz/Tidal via ISRC, Beatport, Bandcamp…).
TAGGER_SERVICES = ["apple", "deezer", "qobuz", "tidal", "spotify"]


def install(app, ctx) -> None:
    global _cfg
    _cfg = ctx.config
    app.include_router(router)


_AUDIO = {".flac", ".mp3", ".m4a", ".ogg", ".opus", ".aac", ".wav", ".aiff"}


def _row(p: Path) -> dict:
    try:
        t = _tg.read_tags(p)
    except Exception:
        t = {}
    return {
        "path":        str(p),
        "file":        p.name,
        "title":       t.get("title", ""),
        "artist":      t.get("artist", ""),
        "album":       t.get("album", ""),
        "albumartist": t.get("albumartist", "") or t.get("album_artist", ""),
        "track":       t.get("track", "") or t.get("tracknumber", ""),
        "disc":        t.get("discnumber", "") or t.get("disc", ""),
        "year":        t.get("year", "") or t.get("date", ""),
        "genre":       t.get("genre", ""),
        "isrc":        t.get("isrc", ""),
    }


def _seq_int(v) -> int:
    """Parse a tag number ('3', '3/12', '1.05') → leading int, 0 on failure."""
    try:
        return int(str(v).replace("\\", "/").split("/")[0].split(".")[0].strip())
    except (ValueError, IndexError):
        return 0


import re as _re_disc
# Disc number from a subfolder name: "CD 1", "CD1", "Disc 2", "Disk 3", "Диск 1",
# "CD-2", or a bare trailing number. Space/dash/dot between label and number is
# optional, leading zeros tolerated. 0 when no number is present.
_DISC_RX = _re_disc.compile(
    r"(?:cd|disc|disk|диск|vol(?:ume)?|часть)\s*[._-]?\s*0*(\d+)", _re_disc.I)
_TRAIL_NUM_RX = _re_disc.compile(r"0*(\d+)\s*$")


def _disc_from_subdir(subdir: str) -> int:
    """Best-effort disc number from a disc-folder name (handles 'CD 1' & 'CD1')."""
    if not subdir:
        return 0
    name = subdir.replace("\\", "/").split("/")[-1]
    m = _DISC_RX.search(name) or _TRAIL_NUM_RX.search(name)
    try:
        return int(m.group(1)) if m else 0
    except (ValueError, IndexError):
        return 0


@router.post("/api/tagger/read")
async def tagger_read(body: dict, request: Request):
    explicit = [Path(f) for f in (body.get("files") or [])
                if Path(f).is_file() and Path(f).suffix.lower() in _AUDIO]
    d = (body.get("dir") or "").strip()
    base = Path(d) if (d and Path(d).is_dir()) else None

    # All of it — recursive rglob + a tag read per file — is disk I/O, so it runs
    # in ONE threadpool call instead of blocking the loop (or N sequential hops).
    def _collect() -> list:
        files = list(explicit)
        if base is not None:
            # Recurse so a multi-disc release (CD1/CD2/… subfolders) loads as ONE
            # sequence — the user picks the release root, every disc comes in.
            files += [x for x in base.rglob("*")
                      if x.is_file() and x.suffix.lower() in _AUDIO]
        seen, rows = set(), []
        for p in files:
            if str(p) in seen:
                continue
            seen.add(str(p))
            r = _row(p)
            try:
                rel = p.parent.relative_to(base) if base else None
                r["subdir"] = "" if (rel is None or str(rel) == ".") else str(rel)
            except (ValueError, TypeError):
                r["subdir"] = ""
            # A disc-folder ("CD 2") is authoritative over the file's own disc tag
            # — per-disc rips often carry a wrong/default "1/1", but the folder
            # split is the real layout. Override so it sorts right AND writes true.
            fd = _disc_from_subdir(r.get("subdir", ""))
            if fd:
                r["disc"] = str(fd)
            rows.append(r)
        # Disc-major, track-minor order so a 3-disc release maps front-to-back.
        # Sort by disc NUMBER (not folder string) so "CD 10" follows "CD 2".
        rows.sort(key=lambda r: (_seq_int(r.get("disc")),
                                 _seq_int(r.get("track")),
                                 r.get("subdir", "").lower(),
                                 r.get("file", "").lower()))
        return rows

    rows = await run_in_threadpool(_collect)
    if not rows:
        raise HTTPException(400, imsg("err.no_audio_files", "Нет аудиофайлов"))
    return {"ok": True, "rows": rows, "services": TAGGER_SERVICES,
            "multidisc": len({r.get("subdir", "") for r in rows}) > 1}


# ── Matchers — return a normalized tag dict or None ─────────────────────────

def _best(items, title, artist, key):
    """Pick the candidate whose title/artist best matches (simple ratio)."""
    import difflib
    tl, al = (title or "").lower(), (artist or "").lower()
    best, score = None, 0.0
    for it in items:
        t, a = key(it)
        s = difflib.SequenceMatcher(None, tl, (t or "").lower()).ratio() * 0.7 \
            + difflib.SequenceMatcher(None, al, (a or "").lower()).ratio() * 0.3
        if s > score:
            best, score = it, s
    return best if score >= 0.5 else (items[0] if items else None)


async def _match_apple(title, artist, isrc):
    term = f"{artist} {title}".strip()
    async with httpx.AsyncClient(timeout=12) as c:
        r = await c.get("https://itunes.apple.com/search",
                        params={"term": term, "entity": "song", "limit": 8})
        if r.status_code != 200:
            return None
        items = [x for x in r.json().get("results", []) if x.get("trackName")]
    it = _best(items, title, artist, lambda x: (x.get("trackName"), x.get("artistName")))
    if not it:
        return None
    art = (it.get("artworkUrl100") or "").replace("100x100", "1000x1000")
    return {
        "title":  it.get("trackName", ""), "artist": it.get("artistName", ""),
        "album":  it.get("collectionName", ""),
        "albumartist": it.get("collectionArtistName") or it.get("artistName", ""),
        "track":  str(it.get("trackNumber", "") or ""),
        "year":   (it.get("releaseDate", "") or "")[:4],
        "genre":  it.get("primaryGenreName", ""), "cover": art, "_src": "apple",
    }


async def _match_deezer(title, artist, isrc):
    async with httpx.AsyncClient(timeout=12) as c:
        tr = None
        if isrc:
            r = await c.get(f"https://api.deezer.com/track/isrc:{isrc}")
            if r.status_code == 200 and not r.json().get("error"):
                tr = r.json()
        if not tr:
            r = await c.get("https://api.deezer.com/search",
                            params={"q": f'artist:"{artist}" track:"{title}"', "limit": 8})
            items = (r.json().get("data") or []) if r.status_code == 200 else []
            tr = _best(items, title, artist,
                       lambda x: (x.get("title"), (x.get("artist") or {}).get("name")))
        if not tr:
            return None
        alb = tr.get("album") or {}
        # album endpoint for genre/album-artist/track total
        return {
            "title":  tr.get("title", ""),
            "artist": (tr.get("artist") or {}).get("name", ""),
            "album":  alb.get("title", ""),
            "albumartist": (tr.get("artist") or {}).get("name", ""),
            "track":  str(tr.get("track_position", "") or ""),
            "year":   (tr.get("release_date", "") or "")[:4],
            "genre":  "",
            "isrc":   tr.get("isrc", "") or isrc,
            "cover":  alb.get("cover_xl") or alb.get("cover_big", ""),
            "_src":   "deezer",
        }


async def _match_spotify(title, artist, isrc):
    from ripster.routes.spotify import get_access_token
    tok = await get_access_token()
    if not tok:
        return None
    # Spotify supports an `isrc:` search filter — exact when the file has an ISRC.
    q = f"isrc:{isrc}" if isrc else f"{artist} {title}".strip()
    async with httpx.AsyncClient(timeout=12, headers={"Authorization": f"Bearer {tok}"}) as c:
        r = await c.get("https://api.spotify.com/v1/search",
                        params={"q": q, "type": "track", "limit": 8})
    items = ((r.json().get("tracks") or {}).get("items") or []) if r.status_code == 200 else []
    it = _best(items, title, artist,
               lambda x: (x.get("name"), ", ".join(a["name"] for a in x.get("artists", []))))
    if not it:
        return None
    alb = it.get("album") or {}
    return {
        "title":  it.get("name", ""),
        "artist": ", ".join(a["name"] for a in it.get("artists", [])),
        "album":  alb.get("name", ""),
        "albumartist": ", ".join(a["name"] for a in alb.get("artists", [])),
        "track":  str(it.get("track_number", "") or ""),
        "disc":   it.get("disc_number") or 1,
        "year":   (alb.get("release_date", "") or "")[:4],
        "genre":  "",
        "isrc":   ((it.get("external_ids") or {}).get("isrc", "")) or isrc,
        "cover":  (alb.get("images") or [{}])[0].get("url", ""),
        "_src":   "spotify",
    }


async def _match_qobuz(title, artist, isrc):
    appid = str(_cfg.get("qobuz-app-id") or "").strip() or "312369995"
    tok   = (_cfg.get("qobuz-auth-token") or "").strip()
    headers = {"X-User-Auth-Token": tok} if tok else {}
    async with httpx.AsyncClient(timeout=12) as c:
        r = await c.get("https://www.qobuz.com/api.json/0.2/track/search",
                        params={"query": f"{artist} {title}".strip(), "limit": 8, "app_id": appid},
                        headers=headers)
    items = ((r.json().get("tracks") or {}).get("items") or []) if r.status_code == 200 else []
    it = _best(items, title, artist,
               lambda x: (x.get("title"), (x.get("performer") or {}).get("name")))
    if not it:
        return None
    alb = it.get("album") or {}
    return {
        "title":  it.get("title", ""),
        "artist": (it.get("performer") or {}).get("name", "") or (alb.get("artist") or {}).get("name", ""),
        "album":  alb.get("title", ""),
        "albumartist": (alb.get("artist") or {}).get("name", ""),
        "track":  str(it.get("track_number", "") or ""),
        "disc":   it.get("media_number") or 1,
        "year":   str(alb.get("release_date_original", "") or "")[:4],
        "genre":  (alb.get("genre") or {}).get("name", "") if isinstance(alb.get("genre"), dict) else "",
        "isrc":   it.get("isrc", "") or isrc,
        "cover":  (alb.get("image") or {}).get("large", ""),
        "_src":   "qobuz",
    }


async def _match_tidal(title, artist, isrc):
    tok = (_cfg.get("tidal-token") or "").strip()
    cc  = (_cfg.get("tidal-country") or "US").strip().upper() or "US"
    if not tok:
        return None
    async with httpx.AsyncClient(timeout=12, headers={"Authorization": f"Bearer {tok}"}) as c:
        r = await c.get("https://api.tidal.com/v1/search/tracks",
                        params={"query": f"{artist} {title}".strip(), "limit": 8, "countryCode": cc})
    items = r.json().get("items", []) if r.status_code == 200 else []
    it = _best(items, title, artist,
               lambda x: (x.get("title"),
                          (x.get("artist") or {}).get("name")
                          or ", ".join(a["name"] for a in x.get("artists", []))))
    if not it:
        return None
    alb = it.get("album") or {}
    cov = (alb.get("cover") or "").replace("-", "/")
    return {
        "title":  it.get("title", ""),
        "artist": (it.get("artist") or {}).get("name", "")
                  or ", ".join(a["name"] for a in it.get("artists", [])),
        "album":  alb.get("title", ""),
        "albumartist": (it.get("artist") or {}).get("name", ""),
        "track":  str(it.get("trackNumber", "") or ""),
        "disc":   it.get("volumeNumber") or 1,
        "year":   "",
        "genre":  "",
        "isrc":   it.get("isrc", "") or isrc,
        "cover":  f"https://resources.tidal.com/images/{cov}/640x640.jpg" if cov else "",
        "_src":   "tidal",
    }


_MATCHERS = {"apple": _match_apple, "deezer": _match_deezer,
             "spotify": _match_spotify, "qobuz": _match_qobuz, "tidal": _match_tidal}


@router.post("/api/tagger/match")
async def tagger_match(body: dict, request: Request):
    path = (body.get("path") or "").strip()
    svc  = (body.get("service") or "apple").strip().lower()
    p = Path(path)
    if not p.is_file():
        raise HTTPException(404, imsg("err.file_not_found", "Файл не найден"))
    cur = await run_in_threadpool(_row, p)
    fn = _MATCHERS.get(svc)
    if not fn:
        raise HTTPException(400, imsg("err.tg_svc_unsupported",
                                      f"Сервис {svc} пока не поддержан в теггере", svc=svc))
    try:
        proposed = await fn(cur["title"] or p.stem, cur["artist"], cur["isrc"])
    except Exception as e:
        raise HTTPException(502, f"{svc}: {e}")
    if not proposed:
        return {"ok": True, "matched": False, "path": path}
    return {"ok": True, "matched": True, "path": path, "proposed": proposed}


# Small in-memory cache so applying one album cover to N tracks downloads it
# once, not N times. Keyed by URL → (bytes, mime). Bounded to avoid growth.
_COVER_CACHE: dict = {}
_COVER_CACHE_MAX = 32
_COVER_MAX_BYTES = 16 * 1024 * 1024


async def _fetch_cover(url: str):
    """Download cover image bytes + MIME from *url*. Cached by URL. Returns
    (data, mime) or (None, '') on any failure."""
    if not url or not url.lower().startswith(("http://", "https://")):
        return None, ""
    hit = _COVER_CACHE.get(url)
    if hit is not None:
        return hit
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True,
                                     headers={"User-Agent": "Mozilla/5.0"}) as c:
            r = await c.get(url)
        if r.status_code != 200 or not r.content:
            return None, ""
        data = r.content
        if len(data) > _COVER_MAX_BYTES:
            return None, ""
        mime = _tg._cover_mime(data, r.headers.get("content-type", "image/jpeg").split(";")[0])
        if len(_COVER_CACHE) >= _COVER_CACHE_MAX:
            _COVER_CACHE.clear()
        _COVER_CACHE[url] = (data, mime)
        return data, mime
    except Exception:
        return None, ""


@router.post("/api/tagger/apply")
async def tagger_apply(body: dict, request: Request):
    path = (body.get("path") or "").strip()
    fields = dict(body.get("fields") or {})
    clear  = bool(body.get("clear"))          # wipe existing tags first
    keepcv = body.get("keep_cover", True)
    # Cover to embed: explicit `cover` field, or carried inside `fields.cover`
    # (album/match payloads put the artwork URL there). `embed_cover` gates it.
    cover_url = (str(body.get("cover") or fields.pop("cover", "") or "")).strip()
    embed     = bool(body.get("embed_cover")) and bool(cover_url)
    p = Path(path)
    if not p.is_file():
        raise HTTPException(404, imsg("err.file_not_found", "Файл не найден"))
    # When embedding fresh art on a clear-write, don't also retain the old cover —
    # otherwise the file ends up with two pictures.
    if embed and clear:
        keepcv = False
    if clear:
        ok = await run_in_threadpool(_tg.clear_and_write, p, fields, bool(keepcv))
    else:
        ok = await run_in_threadpool(_tg.write_tags, p, fields)
    if not ok:
        raise HTTPException(500, imsg("err.tg_write_failed", "Не удалось записать теги"))
    cover_embedded = False
    if embed:
        data, mime = await _fetch_cover(cover_url)
        if data:
            cover_embedded = await run_in_threadpool(_tg.embed_cover, p, data, mime)
    return {"ok": True, "path": path, "cover": cover_embedded}


# ── Album tracklist fetch (paste a URL → full tracklist) ────────────────────

def _bc_clean_title(t: str, a: str) -> str:
    return t[len(a) + 3:] if a and t.lower().startswith((a + " - ").lower()) else t


async def _album_bandcamp(url: str) -> dict:
    import re, json, html
    async with httpx.AsyncClient(timeout=20, follow_redirects=True,
                                 headers={"User-Agent": "Mozilla/5.0"}) as c:
        h = (await c.get(url)).text
    m = re.search(r'data-tralbum="([^"]+)"', h)
    if not m:
        raise HTTPException(502, imsg("err.tg_bandcamp_no_tracklist",
                                      "Bandcamp: не нашёл треклист на странице"))
    d = json.loads(html.unescape(m.group(1)))
    cur = d.get("current", {}) or {}
    ym = re.search(r"\b(20\d\d|19\d\d)\b", cur.get("release_date", "") or "")
    label = d.get("artist", "")
    tracks = []
    for t in d.get("trackinfo", []) or []:
        a = t.get("artist") or label
        tracks.append({"num": t.get("track_num"),
                       "title": _bc_clean_title(t.get("title", ""), t.get("artist") or ""),
                       "artist": a})
    is_comp = len({t["artist"] for t in tracks}) > 1
    return {"album": cur.get("title", ""),
            "albumartist": "Various Artists" if is_comp else label,
            "year": ym.group(1) if ym else "", "label": label,
            "cover": "", "tracks": tracks}


async def _album_apple(url: str) -> dict:
    import re
    m = re.search(r"/album/[^/]+/(\d+)", url)
    if not m:
        raise HTTPException(400, imsg("err.tg_not_album_url",
                                      "Apple: не album-URL", svc="Apple"))
    aid = m.group(1)
    cc = re.search(r"music\.apple\.com/([a-z]{2})/", url)
    cc = cc.group(1) if cc else "us"
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get("https://itunes.apple.com/lookup",
                        params={"id": aid, "entity": "song", "country": cc, "limit": 200})
        res = r.json().get("results", []) if r.status_code == 200 else []
    col = next((x for x in res if x.get("wrapperType") == "collection"), {})
    songs = [x for x in res if x.get("wrapperType") == "track" or x.get("kind") == "song"]
    tracks = [{"num": s.get("trackNumber"), "title": s.get("trackName", ""),
               "artist": s.get("artistName", ""), "disc": s.get("discNumber") or 1}
              for s in songs]
    return {"album": col.get("collectionName", ""),
            "albumartist": col.get("artistName", ""),
            "year": (col.get("releaseDate", "") or "")[:4],
            "label": col.get("copyright", ""),
            "cover": (col.get("artworkUrl100", "") or "").replace("100x100", "1000x1000"),
            "tracks": tracks}


async def _album_deezer(url: str) -> dict:
    import re
    m = re.search(r"/album/(\d+)", url)
    if not m:
        raise HTTPException(400, imsg("err.tg_not_album_url",
                                      "Deezer: не album-URL", svc="Deezer"))
    aid = m.group(1)
    async with httpx.AsyncClient(timeout=15) as c:
        d = (await c.get(f"https://api.deezer.com/album/{aid}")).json()
        # The album object caps tracklist at 25 — paginate the tracks endpoint
        # for the full list (compilations run 40-50+).
        items, nxt = [], f"https://api.deezer.com/album/{aid}/tracks?limit=200"
        while nxt:
            j = (await c.get(nxt)).json()
            items += j.get("data", [])
            nxt = j.get("next")
    if len(items) < len((d.get("tracks") or {}).get("data", [])):
        items = (d.get("tracks") or {}).get("data", [])   # fallback
    tracks = [{"num": t.get("track_position") or (i + 1), "title": t.get("title", ""),
               "artist": (t.get("artist") or {}).get("name", ""),
               "disc": t.get("disk_number") or 1}
              for i, t in enumerate(items)]
    return {"album": d.get("title", ""),
            "albumartist": (d.get("artist") or {}).get("name", ""),
            "year": (d.get("release_date", "") or "")[:4],
            "label": d.get("label", ""),
            "cover": d.get("cover_xl", ""), "tracks": tracks}


async def _album_spotify(url: str) -> dict:
    import re
    m = re.search(r"/album/([A-Za-z0-9]+)", url)
    if not m:
        raise HTTPException(400, imsg("err.tg_not_album_url",
                                      "Spotify: не album-URL", svc="Spotify"))
    from ripster.routes.spotify import get_access_token
    tok = await get_access_token()
    if not tok:
        raise HTTPException(401, imsg("err.tg_spotify_unauth",
                                      "Spotify не авторизован (нужен OAuth в Settings)"))
    h = {"Authorization": f"Bearer {tok}"}
    async with httpx.AsyncClient(timeout=15, headers=h) as c:
        d = (await c.get(f"https://api.spotify.com/v1/albums/{m.group(1)}")).json()
    if d.get("error"):
        raise HTTPException(502, f"Spotify: {d['error'].get('message')}")
    tracks = [{"num": t.get("track_number"), "title": t.get("name", ""),
               "artist": ", ".join(a["name"] for a in t.get("artists", [])),
               "disc": t.get("disc_number") or 1}
              for t in (d.get("tracks") or {}).get("items", [])]
    aa = ", ".join(a["name"] for a in d.get("artists", []))
    return {"album": d.get("name", ""), "albumartist": aa,
            "year": (d.get("release_date", "") or "")[:4], "label": d.get("label", ""),
            "cover": (d.get("images") or [{}])[0].get("url", ""), "tracks": tracks}


async def _album_qobuz(url: str) -> dict:
    import re
    m = re.search(r"/album/(?:[^/]+/)?([a-z0-9]+)(?:[/?#]|$)", url)
    if not m:
        raise HTTPException(400, imsg("err.tg_not_album_url",
                                      "Qobuz: не album-URL", svc="Qobuz"))
    appid = str(_cfg.get("qobuz-app-id") or "").strip() or "312369995"
    async with httpx.AsyncClient(timeout=15) as c:
        d = (await c.get("https://www.qobuz.com/api.json/0.2/album/get",
                         params={"album_id": m.group(1), "app_id": appid})).json()
    if d.get("status") == "error" or not d.get("tracks"):
        raise HTTPException(502, imsg("err.tg_qobuz_msg",
                                      f"Qobuz: {d.get('message','альбом не найден')}",
                                      msg=str(d.get("message") or "")))
    items = (d.get("tracks") or {}).get("items", [])
    tracks = [{"num": t.get("track_number"), "title": t.get("title", ""),
               "artist": (t.get("performer") or {}).get("name", "")
                         or (d.get("artist") or {}).get("name", ""),
               "disc": t.get("media_number") or 1}
              for t in items]
    return {"album": d.get("title", ""),
            "albumartist": (d.get("artist") or {}).get("name", ""),
            "year": str(d.get("release_date_original", "") or "")[:4],
            "label": (d.get("label") or {}).get("name", ""),
            "cover": (d.get("image") or {}).get("large", ""), "tracks": tracks}


async def _album_tidal(url: str) -> dict:
    import re
    m = re.search(r"/album/(\d+)", url)
    if not m:
        raise HTTPException(400, imsg("err.tg_not_album_url",
                                      "Tidal: не album-URL", svc="Tidal"))
    tok = str(_cfg.get("tidal-token") or "").strip()
    cc  = str(_cfg.get("tidal-country") or "US").strip() or "US"
    if not tok:
        raise HTTPException(401, imsg("err.tg_tidal_unauth",
                                      "Tidal не авторизован (нужен токен в Settings)"))
    h = {"Authorization": f"Bearer {tok}"}
    async with httpx.AsyncClient(timeout=15, headers=h) as c:
        info = (await c.get(f"https://api.tidal.com/v1/albums/{m.group(1)}",
                            params={"countryCode": cc})).json()
        tr = (await c.get(f"https://api.tidal.com/v1/albums/{m.group(1)}/tracks",
                          params={"countryCode": cc, "limit": 100})).json()
    items = tr.get("items", []) if isinstance(tr, dict) else []
    tracks = [{"num": t.get("trackNumber"), "title": t.get("title", ""),
               "artist": (t.get("artist") or {}).get("name", "")
                         or ", ".join(a["name"] for a in t.get("artists", [])),
               "disc": t.get("volumeNumber") or 1}
              for t in items]
    cov = (info.get("cover") or "").replace("-", "/")
    return {"album": info.get("title", ""),
            "albumartist": (info.get("artist") or {}).get("name", ""),
            "year": (info.get("releaseDate", "") or "")[:4], "label": "",
            "cover": f"https://resources.tidal.com/images/{cov}/1280x1280.jpg" if cov else "",
            "tracks": tracks}


async def _album_beatport(url: str) -> dict:
    import re, json
    async with httpx.AsyncClient(timeout=20, follow_redirects=True,
                                 headers={"User-Agent": "Mozilla/5.0"}) as c:
        h = (await c.get(url)).text
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.+?)</script>', h, re.S)
    if not m:
        raise HTTPException(502, imsg("err.tg_beatport_no_data",
                                      "Beatport: не нашёл данные на странице"))
    d = json.loads(m.group(1))
    # walk the dehydrated state for the release + its tracks
    def _find(o, key):
        if isinstance(o, dict):
            if key in o and isinstance(o[key], list):
                return o[key]
            for v in o.values():
                r = _find(v, key)
                if r:
                    return r
        elif isinstance(o, list):
            for v in o:
                r = _find(v, key)
                if r:
                    return r
        return None
    tracks_raw = _find(d, "tracks") or []
    tracks = []
    for i, t in enumerate(tracks_raw, 1):
        if not isinstance(t, dict) or not t.get("name"):
            continue
        arts = t.get("artists") or []
        nm = t.get("name", "")
        mix = (t.get("mix_name") or t.get("mix") or "")
        if mix and mix.lower() not in nm.lower():
            nm = f"{nm} ({mix})"
        tracks.append({"num": t.get("number") or i, "title": nm,
                       "artist": ", ".join(a.get("name", "") for a in arts if isinstance(a, dict))})
    rel = _find(d, "release") or {}
    if isinstance(rel, list):
        rel = rel[0] if rel else {}
    return {"album": (rel.get("name") if isinstance(rel, dict) else "") or "",
            "albumartist": "", "year": "", "label": "", "cover": "", "tracks": tracks}


async def _album_discogs(url: str) -> dict:
    import re
    m = re.search(r"/release/(\d+)", url)
    if not m:
        raise HTTPException(400, imsg("err.tg_discogs_not_release",
                                      "Discogs: не release-URL (нужен .../release/<id>)"))
    tok = str(_cfg.get("discogs-token") or "").strip()
    h = {"User-Agent": "Ripster/1.0"}
    params = {"token": tok} if tok else {}
    async with httpx.AsyncClient(timeout=15, headers=h) as c:
        d = (await c.get(f"https://api.discogs.com/releases/{m.group(1)}", params=params)).json()
    aa = ", ".join(a.get("name", "") for a in d.get("artists", []))
    tracks = []
    for i, t in enumerate(d.get("tracklist", []), 1):
        if t.get("type_") and t["type_"] != "track":
            continue
        ta = ", ".join(a.get("name", "") for a in (t.get("artists") or [])) or aa
        tracks.append({"num": t.get("position") or i, "title": t.get("title", ""), "artist": ta})
    return {"album": d.get("title", ""), "albumartist": aa,
            "year": str(d.get("year", "") or ""),
            "label": ", ".join(l.get("name", "") for l in d.get("labels", [])),
            "cover": (d.get("images") or [{}])[0].get("uri", ""), "tracks": tracks}


async def _album_musicbrainz(url: str) -> dict:
    import re
    m = re.search(r"/release/([0-9a-f-]{36})", url)
    if not m:
        raise HTTPException(400, imsg("err.tg_mb_not_release",
                                      "MusicBrainz: нужен .../release/<mbid>"))
    h = {"User-Agent": "Ripster/1.0 (ripster)"}
    async with httpx.AsyncClient(timeout=15, headers=h) as c:
        d = (await c.get(f"https://musicbrainz.org/ws/2/release/{m.group(1)}",
                         params={"inc": "recordings+artist-credits+labels", "fmt": "json"})).json()
    aa = "".join(a.get("name", "") + a.get("joinphrase", "")
                 for a in d.get("artist-credit", []))
    tracks = []
    for med in d.get("media", []):
        disc = med.get("position") or 1
        for t in med.get("tracks", []):
            ta = "".join(a.get("name", "") + a.get("joinphrase", "")
                         for a in t.get("artist-credit", [])) or aa
            tracks.append({"num": t.get("position"), "title": t.get("title", ""),
                           "artist": ta, "disc": disc})
    li = d.get("label-info", [])
    label = (li[0].get("label") or {}).get("name", "") if li else ""
    return {"album": d.get("title", ""), "albumartist": aa,
            "year": (d.get("date", "") or "")[:4], "label": label,
            "cover": "", "tracks": tracks}


_ALBUM_SRC = [
    ("bandcamp.com",     _album_bandcamp),
    ("music.apple.com",  _album_apple),
    ("deezer.com",       _album_deezer),
    ("open.spotify.com", _album_spotify),
    ("qobuz.com",        _album_qobuz),
    ("tidal.com",        _album_tidal),
    ("beatport.com",     _album_beatport),
    ("discogs.com",      _album_discogs),
    ("musicbrainz.org",  _album_musicbrainz),
]


# ── Album SEARCH (no URL → search by "artist album") ────────────────────────

async def _search_apple(q):
    async with httpx.AsyncClient(timeout=12) as c:
        r = await c.get("https://itunes.apple.com/search",
                        params={"term": q, "entity": "album", "limit": 12})
        res = r.json().get("results", []) if r.status_code == 200 else []
    return [{"title": x.get("collectionName", ""), "artist": x.get("artistName", ""),
             "year": (x.get("releaseDate", "") or "")[:4],
             "cover": (x.get("artworkUrl100", "") or "").replace("100x100", "300x300"),
             "url": x.get("collectionViewUrl", "")} for x in res if x.get("collectionViewUrl")]


async def _search_deezer(q):
    async with httpx.AsyncClient(timeout=12) as c:
        r = await c.get("https://api.deezer.com/search/album", params={"q": q, "limit": 12})
        data = (r.json().get("data") or []) if r.status_code == 200 else []
    return [{"title": x.get("title", ""), "artist": (x.get("artist") or {}).get("name", ""),
             "year": "", "cover": x.get("cover_medium", ""), "tracks": x.get("nb_tracks"),
             "url": f"https://www.deezer.com/album/{x.get('id')}"} for x in data]


async def _search_spotify(q):
    from ripster.routes.spotify import get_access_token
    tok = await get_access_token()
    if not tok:
        return []
    async with httpx.AsyncClient(timeout=12, headers={"Authorization": f"Bearer {tok}"}) as c:
        r = await c.get("https://api.spotify.com/v1/search",
                        params={"q": q, "type": "album", "limit": 12})
        items = (r.json().get("albums") or {}).get("items", []) if r.status_code == 200 else []
    return [{"title": x.get("name", ""),
             "artist": ", ".join(a["name"] for a in x.get("artists", [])),
             "year": (x.get("release_date", "") or "")[:4],
             "cover": (x.get("images") or [{}])[-1].get("url", ""),
             "tracks": x.get("total_tracks"),
             "url": f"https://open.spotify.com/album/{x.get('id')}"} for x in items]


async def _search_qobuz(q):
    appid = str(_cfg.get("qobuz-app-id") or "").strip() or "312369995"
    async with httpx.AsyncClient(timeout=12) as c:
        r = await c.get("https://www.qobuz.com/api.json/0.2/album/search",
                        params={"query": q, "limit": 12, "app_id": appid})
        items = (r.json().get("albums") or {}).get("items", []) if r.status_code == 200 else []
    return [{"title": x.get("title", ""), "artist": (x.get("artist") or {}).get("name", ""),
             "year": str(x.get("released_at", "") or "")[:4],
             "cover": (x.get("image") or {}).get("small", ""),
             "url": f"https://www.qobuz.com/album/-/{x.get('id')}"} for x in items]


async def _search_tidal(q):
    tok = str(_cfg.get("tidal-token") or "").strip()
    cc  = str(_cfg.get("tidal-country") or "US").strip() or "US"
    if not tok:
        return []
    async with httpx.AsyncClient(timeout=12, headers={"Authorization": f"Bearer {tok}"}) as c:
        r = await c.get("https://api.tidal.com/v1/search/albums",
                        params={"query": q, "limit": 12, "countryCode": cc})
        items = r.json().get("items", []) if r.status_code == 200 else []
    return [{"title": x.get("title", ""), "artist": (x.get("artist") or {}).get("name", ""),
             "year": (x.get("releaseDate", "") or "")[:4], "cover": "",
             "url": f"https://tidal.com/album/{x.get('id')}"} for x in items]


async def _search_discogs(q):
    tok = str(_cfg.get("discogs-token") or "").strip()
    params = {"q": q, "type": "release", "per_page": 12}
    if tok:
        params["token"] = tok
    async with httpx.AsyncClient(timeout=12, headers={"User-Agent": "Ripster/1.0"}) as c:
        r = await c.get("https://api.discogs.com/database/search", params=params)
        res = r.json().get("results", []) if r.status_code == 200 else []
    return [{"title": x.get("title", ""), "artist": "", "year": str(x.get("year", "") or ""),
             "cover": x.get("thumb", ""),
             "url": f"https://www.discogs.com/release/{x.get('id')}"}
            for x in res if x.get("id")]


async def _search_musicbrainz(q):
    async with httpx.AsyncClient(timeout=12, headers={"User-Agent": "Ripster/1.0 (ripster)"}) as c:
        r = await c.get("https://musicbrainz.org/ws/2/release",
                        params={"query": q, "limit": 12, "fmt": "json"})
        rels = r.json().get("releases", []) if r.status_code == 200 else []
    out = []
    for x in rels:
        aa = "".join(a.get("name", "") + a.get("joinphrase", "")
                     for a in x.get("artist-credit", []))
        out.append({"title": x.get("title", ""), "artist": aa,
                    "year": (x.get("date", "") or "")[:4], "cover": "",
                    "url": f"https://musicbrainz.org/release/{x.get('id')}"})
    return out


_SEARCHERS = {
    "apple": _search_apple, "deezer": _search_deezer, "spotify": _search_spotify,
    "qobuz": _search_qobuz, "tidal": _search_tidal, "discogs": _search_discogs,
    "musicbrainz": _search_musicbrainz,
}


def _rank_results(results: list, query: str) -> list:
    """Re-rank service results by how well artist+title match the query.

    Services rank by their own popularity signal, so a same-titled release by a
    bigger artist outranks the one you actually searched for ('Paurro — Early
    Hours' over 'DJ Food — Early Hours'). We score every result against the full
    query with two signals and sort descending:
      * token overlap — fraction of query words present in 'artist title'
        (so all of "dj food early hours" present beats only "early hours");
      * sequence ratio — fuzzy closeness, as a tie-breaker.
    Token overlap dominates so the right ARTIST wins even on identical titles.
    """
    import re as _re, difflib
    q = (query or "").lower().strip()
    qtok = set(_re.findall(r"\w+", q))
    if not qtok:
        return results

    def score(r):
        hay = f"{r.get('artist','')} {r.get('title','')}".lower()
        htok = set(_re.findall(r"\w+", hay))
        overlap = len(qtok & htok) / len(qtok)
        ratio = difflib.SequenceMatcher(None, q, hay).ratio()
        return overlap * 0.75 + ratio * 0.25

    return sorted(results, key=score, reverse=True)


@router.post("/api/tagger/search")
async def tagger_search(body: dict, request: Request):
    q   = (body.get("query") or "").strip()
    svc = (body.get("service") or "apple").strip().lower()
    if not q:
        raise HTTPException(400, imsg("err.tg_need_query", "Введи запрос (артист альбом)"))
    fn = _SEARCHERS.get(svc)
    if not fn:
        raise HTTPException(400, imsg("err.tg_search_unsupported",
                                      f"Поиск по {svc} не поддержан", svc=svc))
    try:
        results = await fn(q)
    except Exception as e:
        raise HTTPException(502, f"{svc}: {e}")
    ranked = _rank_results([r for r in results if r.get("title")], q)
    return {"ok": True, "results": ranked[:12]}


@router.post("/api/tagger/rename")
async def tagger_rename(body: dict, request: Request):
    """Rename audio files from their tags using a template. `dry_run` returns a
    preview (old → new) without touching disk; otherwise the renames are applied
    with `_2`, `_3` … collision suffixes. Template uses {artist} {title}
    {album} {albumartist} {tracknumber} {tracknumber:02d} {year} {genre}."""
    template = (body.get("template") or "").strip()
    dry      = bool(body.get("dry_run"))
    if not template:
        raise HTTPException(400, imsg("err.tg_need_mask", "Введи маску переименования"))
    files = []
    for f in body.get("files") or []:
        p = Path(f)
        if p.is_file() and p.suffix.lower() in _AUDIO:
            files.append(p)
    d = (body.get("dir") or "").strip()
    if not files and d and Path(d).is_dir():
        files = sorted(x for x in Path(d).iterdir()
                       if x.is_file() and x.suffix.lower() in _AUDIO)
    if not files:
        raise HTTPException(400, imsg("err.no_audio_files", "Нет аудиофайлов"))

    def _work():
        rows = []
        for p in files:
            tags = _tg.read_tags(p)
            new  = _tg.render_filename(template, tags, p.suffix.lstrip(".")) if tags else ""
            rows.append({"old": p.name, "new": new or p.name,
                         "change": bool(new) and new != p.name, "path": str(p)})
        if dry:
            return {"preview": rows, "count": sum(1 for r in rows if r["change"])}
        renamed, used = 0, set()
        for r in rows:
            if not r["change"]:
                continue
            src = Path(r["path"])
            dst = src.with_name(r["new"])
            cand, n = dst, 2
            while (cand.name.lower() in used or (cand.exists() and cand != src)):
                cand = dst.with_name(f"{dst.stem}_{n}{dst.suffix}")
                n += 1
            used.add(cand.name.lower())
            try:
                src.rename(cand)
                renamed += 1
            except OSError:
                pass
        return {"renamed": renamed}

    res = await run_in_threadpool(_work)
    return {"ok": True, **res}


@router.post("/api/tagger/album")
async def tagger_album(body: dict, request: Request):
    url = (body.get("url") or "").strip()
    if not url:
        raise HTTPException(400, imsg("err.tg_need_album_url", "Нужен URL альбома"))
    low = url.lower()
    fn = next((f for host, f in _ALBUM_SRC if host in low), None)
    if not fn:
        raise HTTPException(400, imsg("err.tg_supported_sites",
                                      "Поддержаны: Bandcamp, Apple Music, Deezer, Spotify, "
                                      "Qobuz, Tidal, Beatport, Discogs, MusicBrainz"))
    try:
        data = await fn(url)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, imsg("err.tg_tracklist_fail",
                                      f"Не удалось получить треклист: {e}", err=str(e)[:200]))
    tracks = [t for t in data.get("tracks", []) if t.get("title")]
    # Order disc-major, track-minor so the list lines up with a recursively
    # loaded multi-disc folder (CD1 tracks, then CD2, …) for sequential mapping.
    tracks.sort(key=lambda t: (_seq_int(t.get("disc", 1)), _seq_int(t.get("num"))))
    data["tracks"] = tracks
    data["count"] = len(tracks)
    return {"ok": True, **data}
