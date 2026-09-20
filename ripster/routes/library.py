"""Local music library — index, cover, file streaming.

Walks the configured save-paths, reads tag metadata via mutagen, exposes:

  GET /api/library/scan?refresh=0  → flat list of tracks with metadata
  GET /api/library/cover/{cid}     → embedded cover art (cached)
  GET /api/library/file?p=<path>   → audio file stream with HTTP Range support

Path traversal is blocked: every file path must resolve under one of the
configured roots (commonpath check). All endpoints are owner-only; guests are
denied by the auth middleware allowlist.

Install: library.install(app, ctx)
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import re
import subprocess
import time
from pathlib import Path
from urllib.parse import quote

# Windows: без этого флага КАЖДЫЙ запуск ffmpeg открывает и тут же закрывает
# окно консоли. На локальном миксе это происходит на каждый трек (ALAC не
# играется в браузере и перегоняется в FLAC на лету) — экран мигает чёрными
# прямоугольниками всю дорогу. На не-Windows флага нет, значение 0.
_CNW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

import mutagen
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from mutagen.flac import FLAC
from mutagen.mp4 import MP4

router = APIRouter()
_cfg: dict = {}

_AUDIO_EXTS = {".flac", ".m4a", ".mp3", ".ogg", ".opus", ".wav",
               ".aac", ".alac", ".aif", ".aiff"}

_index: list[dict]        = []
_index_ts: float          = 0.0
_INDEX_TTL                = 600          # 10 min in-memory cache
_index_lock               = asyncio.Lock()
_cover_cache: dict[str, tuple[bytes, str]] = {}   # cid → (bytes, mime)
_COVER_CACHE_MAX          = 200


def install(app, ctx) -> None:
    global _cfg
    _cfg = ctx.config
    app.include_router(router)


# ── Roots / safety ────────────────────────────────────────────────────────────

_ROOT_KEYS = ("save-path", "qobuz-save-path", "tidal-save-path",
              "deezer-save-path", "soundcloud-save-path", "orpheus-save-path")


def _roots() -> list[Path]:
    seen: set[str] = set()
    out: list[Path] = []
    for k in _ROOT_KEYS:
        v = (_cfg.get(k) or "").strip()
        if not v:
            continue
        try:
            p = Path(v).expanduser().resolve()
        except Exception:
            continue
        if not p.is_dir():
            continue
        key = str(p).lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


def _safe_resolve(p: str) -> Path:
    """Resolve *p* and ensure it lives under one of the library roots."""
    try:
        real = Path(p).expanduser().resolve()
    except Exception:
        raise HTTPException(400, "Bad path")
    for root in _roots():
        try:
            real.relative_to(root)
            return real
        except ValueError:
            continue
    raise HTTPException(403, "Path is outside the library roots")


def _cid(path: Path) -> str:
    return hashlib.sha1(str(path).encode("utf-8")).hexdigest()[:12]


# ── Tag reading ────────────────────────────────────────────────────────────────

def _read_tags(path: Path) -> dict | None:
    """Return {title, artist, album, year, duration, has_cover} or None."""
    try:
        audio = mutagen.File(path, easy=False)
    except Exception:
        return None
    if audio is None:
        return None

    duration = 0
    try:
        if hasattr(audio, "info") and getattr(audio.info, "length", None):
            duration = int(audio.info.length)
    except Exception:
        pass

    title = artist = album = year = ""
    has_cover = False

    if isinstance(audio, FLAC):
        t = audio.tags or {}
        title  = (t.get("title")  or [""])[0]
        artist = (t.get("artist") or [""])[0]
        album  = (t.get("album")  or [""])[0]
        year   = ((t.get("date") or t.get("year") or [""])[0] or "")[:4]
        has_cover = bool(audio.pictures)
    elif isinstance(audio, MP4):
        t = audio.tags or {}
        title  = (t.get("\xa9nam") or [""])[0]
        artist = (t.get("\xa9ART") or [""])[0]
        album  = (t.get("\xa9alb") or [""])[0]
        year   = str((t.get("\xa9day") or [""])[0])[:4]
        has_cover = bool(t.get("covr"))
    else:
        tags = audio.tags
        if tags:
            try:
                v = tags.get("TIT2"); title  = str(v) if v else ""
                v = tags.get("TPE1"); artist = str(v) if v else ""
                v = tags.get("TALB"); album  = str(v) if v else ""
                v = tags.get("TDRC") or tags.get("TYER")
                year = str(v)[:4] if v else ""
                if hasattr(tags, "getall"):
                    has_cover = bool(tags.getall("APIC"))
            except Exception:
                pass

    return {
        "title":     title or path.stem,
        "artist":    artist,
        "album":     album,
        "year":      year,
        "duration":  duration,
        "has_cover": has_cover,
    }


def _read_cover_bytes(path: Path) -> tuple[bytes, str] | None:
    try:
        audio = mutagen.File(path, easy=False)
    except Exception:
        return None
    if audio is None:
        return None
    if isinstance(audio, FLAC):
        if audio.pictures:
            pic = audio.pictures[0]
            return pic.data, pic.mime or "image/jpeg"
    elif isinstance(audio, MP4):
        covers = (audio.tags or {}).get("covr") or []
        if covers:
            c = covers[0]
            mime = "image/png" if getattr(c, "imageformat", 13) == 14 else "image/jpeg"
            return bytes(c), mime
    else:
        tags = audio.tags
        if tags and hasattr(tags, "getall"):
            apics = tags.getall("APIC")
            if apics:
                return apics[0].data, apics[0].mime or "image/jpeg"
    return None


# ── Scan ──────────────────────────────────────────────────────────────────────

async def _walk_roots() -> list[dict]:
    out: list[dict] = []
    for root in _roots():
        rstr = str(root)
        for dirpath, _dirs, files in os.walk(root):
            for fn in files:
                ext = os.path.splitext(fn)[1].lower()
                if ext not in _AUDIO_EXTS:
                    continue
                path = Path(dirpath) / fn
                tags = _read_tags(path)
                if not tags:
                    continue
                try:
                    stat = path.stat()
                except OSError:
                    continue
                try:
                    rel = str(path.relative_to(root))
                except ValueError:
                    rel = path.name
                out.append({
                    **tags,
                    "id":    _cid(path),
                    "path":  str(path),
                    "rel":   rel,
                    "root":  rstr,
                    "ext":   ext.lstrip("."),
                    "size":  stat.st_size,
                    "mtime": int(stat.st_mtime),
                })
            await asyncio.sleep(0)
    return out


@router.get("/api/library/scan")
async def lib_scan(refresh: int = 0):
    global _index, _index_ts
    now = time.time()
    if not refresh and _index and (now - _index_ts) < _INDEX_TTL:
        return {"ok": True, "items": _index, "count": len(_index),
                "cached": True, "ts": _index_ts, "roots": [str(r) for r in _roots()]}
    async with _index_lock:
        if not refresh and _index and (now - _index_ts) < _INDEX_TTL:
            return {"ok": True, "items": _index, "count": len(_index),
                    "cached": True, "ts": _index_ts, "roots": [str(r) for r in _roots()]}
        items = await _walk_roots()
        _index = items
        _index_ts = now
    return {"ok": True, "items": _index, "count": len(_index),
            "cached": False, "ts": _index_ts, "roots": [str(r) for r in _roots()]}


@router.get("/api/library/cover/{cid}")
async def lib_cover(cid: str):
    if cid in _cover_cache:
        data, mime = _cover_cache[cid]
        return Response(content=data, media_type=mime,
                        headers={"Cache-Control": "public, max-age=86400"})
    path_str = next((it["path"] for it in _index if it["id"] == cid), None)
    if not path_str:
        raise HTTPException(404, "Track not in index")
    path = Path(path_str)
    if not path.is_file():
        raise HTTPException(404, "File missing")
    cover = _read_cover_bytes(path)
    if not cover:
        raise HTTPException(404, "No embedded cover")
    if len(_cover_cache) > _COVER_CACHE_MAX:
        for k in list(_cover_cache)[: _COVER_CACHE_MAX // 4]:
            _cover_cache.pop(k, None)
    _cover_cache[cid] = cover
    data, mime = cover
    return Response(content=data, media_type=mime,
                    headers={"Cache-Control": "public, max-age=86400"})


# ── File streaming with Range ─────────────────────────────────────────────────

_MIME = {
    ".flac": "audio/flac",
    ".mp3":  "audio/mpeg",
    ".m4a":  "audio/mp4",
    ".aac":  "audio/aac",
    ".ogg":  "audio/ogg",
    ".opus": "audio/opus",
    ".wav":  "audio/wav",
    ".aif":  "audio/aiff",
    ".aiff": "audio/aiff",
}
_RNG_RE = re.compile(r"bytes=(\d+)-(\d*)")


@router.get("/api/library/file")
async def lib_file(request: Request, p: str = Query(...)):
    real = _safe_resolve(p)
    if not real.is_file():
        raise HTTPException(404, "File not found")
    size = real.stat().st_size
    mime = _MIME.get(real.suffix.lower(), "application/octet-stream")
    rng = request.headers.get("range") or request.headers.get("Range")

    if not rng:
        return FileResponse(real, media_type=mime,
                            headers={"Accept-Ranges": "bytes"})

    m = _RNG_RE.match(rng)
    if not m:
        raise HTTPException(416, "Bad Range header")
    start = int(m.group(1))
    end   = int(m.group(2)) if m.group(2) else size - 1
    end   = min(end, size - 1)
    if start > end:
        raise HTTPException(416, "Bad range")
    length = end - start + 1

    def gen():
        with open(real, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(remaining, 64 * 1024))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    return StreamingResponse(
        gen(),
        status_code=206,
        media_type=mime,
        headers={
            "Content-Range":  f"bytes {start}-{end}/{size}",
            "Accept-Ranges":  "bytes",
            "Content-Length": str(length),
        },
    )


# ── Local mix (Apple/local album → ordered playable tracks) ───────────────────

def _track_no(path: Path) -> int:
    """Best-effort track number from tags → for ordering. 0 if unknown."""
    try:
        audio = mutagen.File(path, easy=False)
    except Exception:
        return 0
    if audio is None:
        return 0
    try:
        if isinstance(audio, MP4):
            trkn = (audio.tags or {}).get("trkn") or []
            if trkn and isinstance(trkn[0], (list, tuple)) and trkn[0]:
                return int(trkn[0][0])
        elif isinstance(audio, FLAC):
            v = (audio.tags or {}).get("tracknumber") or []
            if v:
                return int(str(v[0]).split("/")[0])
        else:
            tags = getattr(audio, "tags", None)
            if tags:
                v = tags.get("TRCK")
                if v:
                    return int(str(v).split("/")[0])
    except Exception:
        pass
    return 0


def _fname_no(name: str) -> int:
    """Leading NN. prefix of a downloaded filename → int, else 0."""
    m = re.match(r"\s*(\d{1,3})[.\s_-]", name)
    return int(m.group(1)) if m else 0


@router.get("/api/localmix/flac")
async def local_mix_flac(p: str = Query(...)):
    """Transcode a local ALAC/AAC/… track to stereo FLAC on the fly.

    Chromium's Web-Audio decodeAudioData cannot decode ALAC, so the gapless
    engine needs a FLAC copy. ALAC→FLAC is lossless (identical PCM) → the
    sample-accurate crossfade-free join between tracks of the same mix holds.
    Stereo downmix keeps memory sane for surround (Atmos) sources and matches
    the player's output. No Range: the WA engine fetches the whole buffer.
    """
    real = _safe_resolve(p)
    if not real.is_file():
        raise HTTPException(404, "File not found")

    cmd = [
        "ffmpeg", "-v", "error", "-nostdin",
        "-i", str(real),
        "-vn", "-map", "0:a:0", "-ac", "2",
        "-c:a", "flac", "-compression_level", "5",
        "-f", "flac", "pipe:1",
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        creationflags=_CNW,
    )

    async def gen():
        try:
            while True:
                chunk = await proc.stdout.read(64 * 1024)
                if not chunk:
                    break
                yield chunk
        finally:
            if proc.returncode is None:
                try:
                    proc.kill()
                except Exception:
                    pass
            try:
                await proc.wait()
            except Exception:
                pass

    return StreamingResponse(
        gen(), media_type="audio/flac",
        headers={"Cache-Control": "no-store", "X-Ripster-Transcode": "alac-flac"},
    )


_STABLE_AGE = 4.0   # сек: файл считается дописанным, если mtime старше этого

_NORM_RE = re.compile(r"[^a-z0-9а-я]+")


def _norm_name(s: str) -> str:
    """Имя папки/альбома → сравнимый ключ: нижний регистр, только буквы/цифры.
    Гасит разницу в пунктуации, скобках, «feat.», лишних пробелах."""
    return _NORM_RE.sub("", (s or "").lower())


@router.get("/api/localmix/find")
async def local_mix_find(album: str = Query(...), artist: str = Query("")):
    """Найти папку альбома под корнями по имени (и опц. артисту).

    Нужно для «играть, пока качается»: как только загрузчик создаёт папку и
    кладёт первый трек, фронт получает её путь и начинает следить. Возвращает
    {ok, dir, tracks} — tracks = число ДОПИСАННЫХ аудиофайлов сейчас.
    """
    want_alb = _norm_name(album)
    want_art = _norm_name(artist)
    if not want_alb:
        raise HTTPException(400, "album required")
    best = None
    for root in _roots():
        for dirpath, dirs, files in os.walk(root):
            if _norm_name(os.path.basename(dirpath)) != want_alb:
                continue
            if want_art and _norm_name(os.path.basename(os.path.dirname(dirpath))) != want_art:
                # артист не совпал — держим как запасной вариант, но ищем точный
                if best is None:
                    best = dirpath
                continue
            best = dirpath
            break
        if best and (not want_art or _norm_name(os.path.basename(os.path.dirname(best))) == want_art):
            break
    if not best:
        return {"ok": False, "dir": "", "tracks": 0}
    now = time.time()
    n = 0
    try:
        for e in os.scandir(best):
            if e.is_file() and os.path.splitext(e.name)[1].lower() in _AUDIO_EXTS:
                if now - e.stat().st_mtime >= _STABLE_AGE:
                    n += 1
    except OSError:
        pass
    return {"ok": True, "dir": best, "tracks": n}


@router.get("/api/localmix")
async def local_mix(dir: str = Query(...), stable: int = 0):
    """Ordered, locally-playable tracks of a downloaded album folder.

    Powers the Apple-mix "play from local" path: returns each track's
    same-origin /api/library/file URL (Web-Audio decodable → gapless),
    ordered by tag track-number then filename prefix.

    stable=1 → отдать только ДОПИСАННЫЕ файлы (mtime старше _STABLE_AGE). Нужно
    для «играть, пока качается»: загрузчик пишет трек на месте + тегает MP4Box,
    и наполовину записанный файл нельзя ни декодировать, ни транскодировать.
    """
    d = _safe_resolve(dir)
    if not d.is_dir():
        raise HTTPException(404, "Not a directory")

    now = time.time()
    files: list[Path] = []
    try:
        for entry in os.scandir(d):
            if entry.is_file() and os.path.splitext(entry.name)[1].lower() in _AUDIO_EXTS:
                if stable:
                    try:
                        if now - entry.stat().st_mtime < _STABLE_AGE:
                            continue        # ещё пишется — пропускаем
                    except OSError:
                        continue
                files.append(Path(entry.path))
    except OSError:
        raise HTTPException(404, "Cannot read directory")
    if not files:
        # при stable=1 это нормальный «пока пусто», не ошибка
        if stable:
            return {"ok": True, "album": "", "artist": "", "cover": "",
                    "count": 0, "tracks": []}
        raise HTTPException(404, "No audio files")

    def _key(p: Path):
        no = _track_no(p) or _fname_no(p.name)
        return (no if no else 10_000, p.name.lower())
    files.sort(key=_key)

    tracks: list[dict] = []
    album = artist = ""
    for p in files:
        tags = _read_tags(p) or {}
        title = tags.get("title") or p.stem
        if not album:
            album = tags.get("album") or ""
        if not artist:
            artist = tags.get("albumartist") or tags.get("artist") or ""
        ext = p.suffix.lower()
        # FLAC decodes natively in Web Audio (→ true gapless). ALAC/AAC/… do NOT
        # (Chromium decodeAudioData throws EncodingError on ALAC), so route them
        # through the on-the-fly FLAC transcode so the gapless engine can decode.
        if ext == ".flac":
            url = "/api/library/file?p=" + quote(str(p))
        else:
            url = "/api/localmix/flac?p=" + quote(str(p))
        tracks.append({
            "title":    title,
            "artist":   tags.get("artist") or artist,
            "duration": int(tags.get("duration") or 0),
            "url":      url,
            "path":     str(p),
            "local":    True,
        })
        await asyncio.sleep(0)

    cover = ""
    for cn in ("cover.jpg", "cover.png", "folder.jpg", "cover.jpeg"):
        if (d / cn).is_file():
            cover = "/api/library/file?p=" + quote(str(d / cn))
            break

    return {"ok": True, "album": album, "artist": artist,
            "cover": cover, "count": len(tracks), "tracks": tracks}
