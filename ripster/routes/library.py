"""Local music library — index, cover, file streaming.

Walks the configured save-paths, reads tag metadata via mutagen, exposes:

  GET /api/library/scan?refresh=0  → flat list of tracks with metadata
  GET /api/library/cover/{cid}     → embedded cover art (cached)
  GET /api/library/file?p=<path>   → audio file stream with HTTP Range support
  POST /api/library/folder         → add + scan a folder picked in the native
                                     dialog (Tracker #9014), polled job
  GET  /api/library/folder/status  → progress of such a job (incl. cancel)
  GET  /api/library/folder/tracks  → incremental result batches
  POST /api/library/peek           → tags from the FIRST BYTES of a dropped
                                     file (same mutagen reader as the scanner)

Path traversal is blocked: every file path must resolve under one of the
configured roots (commonpath check). All endpoints are owner-only; guests are
denied by the auth middleware allowlist.

Install: library.install(app, ctx)
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import os
import re
import subprocess
import threading
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
# поток job-сканера дописывает индекс вне event loop — asyncio.Lock ему не подходит
_index_muthread           = threading.Lock()
_cover_cache: dict[str, tuple[bytes, str]] = {}   # cid → (bytes, mime)
_COVER_CACHE_MAX          = 200


def install(app, ctx) -> None:
    global _cfg, _base_dir
    _cfg = ctx.config
    _base_dir = getattr(ctx, "base_dir", None) or Path.cwd()
    # Без этого строки добавленные папки живут до перезапуска: файл состояния
    # читается, но не читается НИКЕМ. Поймано прогоном «перезапусти и посмотри».
    _load_extra_roots()
    app.include_router(router)


# ── Roots / safety ────────────────────────────────────────────────────────────

_ROOT_KEYS = ("save-path", "qobuz-save-path", "tidal-save-path",
              "deezer-save-path", "soundcloud-save-path", "orpheus-save-path")

_base_dir = None

# Папки, добавленные через нативный выбор (Tracker #9014). СВОЙ файл состояния:
# в config.yaml пишет только ядро, и трогать его отсюда нельзя.
_ROOTS_FILE_NAME = "library_extra_roots.json"
_extra_roots: list[Path] = []
_roots_lock = threading.Lock()


def _roots_file() -> Path:
    return Path(_base_dir) / "config" / _ROOTS_FILE_NAME


def _load_extra_roots() -> None:
    global _extra_roots
    try:
        raw = json.loads(_roots_file().read_text(encoding="utf-8"))
    except Exception:
        return
    out, seen = [], set()
    for v in raw if isinstance(raw, list) else []:
        try:
            p = Path(str(v)).expanduser().resolve()
        except Exception:
            continue
        if p.is_dir() and str(p).lower() not in seen:
            seen.add(str(p).lower())
            out.append(p)
    with _roots_lock:
        _extra_roots = out


def _save_extra_roots() -> None:
    try:
        f = _roots_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        with _roots_lock:
            data = [str(p) for p in _extra_roots]
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, f)
    except Exception:
        pass   # папка добавлена на этот запуск; на следующий просто не вспомнится


def register_folder(path: str, extra: list[Path] | None = None) -> Path | None:
    """Проверить выбранную пользователем папку и, если нужно, запомнить её.

    Возвращает Path — папка стала новым корнем библиотеки (её увидят и обычный
    скан, и /api/library/file после перезапуска). Возвращает None — запоминать
    нечего: это уже корень или ветка уже индексируемого корня, сканировать её
    можно и так. Бросает HTTPException — папки нет, либо она стоит ВЫШЕ
    библиотеки и накрывает уже настроенный корень: такое добавление только
    расширило бы круг папок, откуда отдаются файлы, а пользы бы не дало.

    `extra` = уже запомненный набор (тесты подменяют живую копию)."""
    try:
        p = Path(str(path)).expanduser().resolve()
    except Exception:
        raise HTTPException(400, "Bad path")
    if not p.is_dir():
        raise HTTPException(404, "Folder does not exist")
    pool = extra if extra is not None else _roots()
    for r in pool:
        if os.path.normcase(str(r)) == os.path.normcase(str(p)):
            return None                      # этот же корень — просто пересканируем
        try:
            r.relative_to(p)
            raise HTTPException(400, "folder contains a library root")
        except ValueError:
            pass
        try:
            p.relative_to(r)
            return None                      # ветка уже индексируемого корня
        except ValueError:
            pass
    with _roots_lock:
        low = os.path.normcase(str(p))
        if any(os.path.normcase(str(e)) == low for e in _extra_roots):
            return p
        _extra_roots.append(p)
    _save_extra_roots()
    return p


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
    with _roots_lock:
        extra = list(_extra_roots)
    for p in extra:
        key = str(p).lower()
        if key not in seen and p.is_dir():
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


# ── Added folders: native-picker jobs (Tracker #9014) ─────────────────────────
#
# Тот же walks+mutagen, что и у обычного скана, НО: job живёт в словаре, ход
# отдаётся пачками (фронт дописывает трек-лист по мере скана, а не ждёт весь),
# есть отмена и честный счётчик пропущенного не-аудио. Поток-воркер, а не
# сопрограмма: mutagen'у блокирующий доступ к диску только на руку.

_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()
_JOB_TTL = 30 * 60
_KEEP_ITEMS = 20000        # дальше — только счётчики: трек-лист столько не ест


def _prune_jobs():
    now = time.time()
    with _jobs_lock:
        for jid in [k for k, j in _jobs.items() if now - j.get("ts", now) > _JOB_TTL]:
            _jobs.pop(jid, None)


def _item_from(path: Path, root: Path) -> dict | None:
    tags = _read_tags(path)
    if not tags:
        return None
    try:
        stat = path.stat()
    except OSError:
        return None
    try:
        rel = str(path.relative_to(root))
    except ValueError:
        rel = path.name
    return {**tags, "id": _cid(path), "path": str(path), "rel": rel,
            "root": str(root), "ext": path.suffix.lstrip(".").lower(),
            "size": stat.st_size, "mtime": int(stat.st_mtime)}


def _cancel_job(job: dict, scanned: int, skipped: int, unreadable: int) -> None:
    """Отмена: заморозить счётчики и уйти. Без этого фронт видит «сканирую…»
    до конца обхода, хотя человека уже не слушают."""
    job["scanned"], job["skipped"], job["unreadable"] = scanned, skipped, unreadable
    job["state"] = "cancelled"
    job["ts"] = time.time()


def _scan_folder_job(jid: str, root: Path) -> None:
    scanned = skipped = unreadable = 0
    for dirpath, dirs, files in os.walk(root):
        job = _jobs.get(jid)
        if job is None:
            return                    # job истёк — писать уже некому
        if job.get("cancel"):
            _cancel_job(job, scanned, skipped, unreadable)
            return
        dirs.sort(key=lambda s: s.lower())
        for fn in sorted(files, key=_natkey):
            # Проверка и на каждом файле: плоская папка «вся музыка» из полутора
            # тысяч файлов иначе игнорировала бы отмену до самого её конца.
            if job.get("cancel"):
                _cancel_job(job, scanned, skipped, unreadable)
                return
            ext = os.path.splitext(fn)[1].lower()
            if ext not in _AUDIO_EXTS:
                skipped += 1
                continue
            path = Path(dirpath) / fn
            item = _item_from(path, root)
            if item is None:
                # расширение аудио, а тег не прочитался: это НЕ «не-аудио»,
                # и валить два разных дефекта в один счётчик — врать человеку
                unreadable += 1
                continue
            scanned += 1
            if len(job["items"]) < _KEEP_ITEMS:
                job["items"].append(item)
            job["found"] += 1
        job["scanned"] = scanned
        job["skipped"] = skipped
        job["unreadable"] = unreadable
        job["ts"] = time.time()
    global _index, _index_ts
    job = _jobs.get(jid)
    if job is None:
        return
    with _index_muthread:
        known = {it["id"] for it in _index}
        for it in _jobs[jid]["items"]:
            if it["id"] not in known:
                _index.append(it); known.add(it["id"])
        _index_ts = time.time()
    job = _jobs[jid]
    job["state"] = "done"
    job["ts"] = time.time()


_NAT_RE = re.compile(r"(\d+)")


def _natkey(name: str):
    """Порядок как в Проводе: цифры сравниваются числами (Track 2 < Track 10)."""
    low = str(name).lower()
    return [int(t) if t.isdigit() else t for t in _NAT_RE.split(low)]


@router.post("/api/library/folder")
async def lib_folder_add(req: Request):
    """Занять выбранные папки и завести на каждую job-сканер.

    Отказ по ОДНОЙ папке не отменяет остальные: человек принёс десять, девять
    нормальные. Причина отказа возвращается рядом с путём и показывается фронту.
    """
    _prune_jobs()
    try:
        body = await req.json()
    except Exception:
        body = {}
    paths = body.get("paths") or ([body["path"]] if body.get("path") else [])
    if not isinstance(paths, list) or not paths:
        raise HTTPException(400, "path or paths[] required")
    if len(paths) > 20:
        raise HTTPException(400, "too many folders at once")
    started, refused = [], []
    for raw in paths[:20]:
        shown = str(raw)
        try:
            p = Path(shown).expanduser().resolve()
            # None = уже внутри библиотеки: сканируем, но вторым корнем не делаем
            register_folder(str(p))
        except HTTPException as e:
            refused.append({"path": shown, "error": str(e.detail)})
            continue
        except Exception:
            refused.append({"path": shown, "error": "Bad path"})
            continue
        jid = hashlib.sha1(f"{p}|{time.time()}".encode()).hexdigest()[:12]
        with _jobs_lock:
            _jobs[jid] = {"id": jid, "root": str(p), "state": "running",
                          "items": [], "found": 0, "scanned": 0, "skipped": 0,
                          "unreadable": 0, "cancel": False, "ts": time.time()}
        threading.Thread(target=_scan_folder_job, args=(jid, p), daemon=True).start()
        started.append({"job": jid, "path": str(p)})
    return {"ok": bool(started), "started": started, "refused": refused}


@router.get("/api/library/folder/status")
async def lib_folder_status(job: str = Query(...), cancel: int = 0):
    j = _jobs.get(job)
    if not j:
        raise HTTPException(404, "no such job")
    if cancel:
        j["cancel"] = True
    return {"ok": True, "state": j["state"], "root": j["root"],
            "found": j["found"], "scanned": j["scanned"], "skipped": j["skipped"],
            "unreadable": j.get("unreadable", 0),
            "total": j["found"] + j["skipped"] + j.get("unreadable", 0)}


@router.get("/api/library/folder/tracks")
async def lib_folder_tracks(job: str = Query(...), after: int = 0):
    j = _jobs.get(job)
    if not j:
        raise HTTPException(404, "no such job")
    after = max(0, min(after, len(j["items"])))
    batch = j["items"][after:after + 500]
    return {"ok": True, "items": batch, "next": after + len(batch),
            "state": j["state"], "found": j["found"], "skipped": j["skipped"],
            "unreadable": j.get("unreadable", 0)}


# ── Peek: ярлыки перетащенных файлов по ПЕРВЫМ БАЙТАМ ─────────────────────────
#
# Заголовок у mp3/flac/ogg/mp4 живёт в начале файла. Читает mutagen — ТОТ ЖЕ
# код, что у сканера библиотеки, поэтому разойтись в показаниях две ветки
# уже не могут. Ответ не проходит через _safe_resolve: наружу уходят поля,
# которые сам пользователь принёс в окно.

_PEEK_BUDGET = {".mp3": 320000, ".flac": 120000, ".ogg": 150000,
                ".opus": 150000, ".m4a": 700000, ".aac": 96000, ".wav": 64,
                ".alac": 700000, ".aif": 700000, ".aiff": 700000}


@router.post("/api/library/peek")
async def lib_peek(req: Request):
    try:
        body = await req.json()
    except Exception:
        raise HTTPException(400, "json required")
    files = (body.get("files") or [])[:40]
    out = []
    for f in files:
        name = str(f.get("name") or "")[:512]
        ext = os.path.splitext(name)[1].lower()
        try:
            blob = base64.b64decode(str(f.get("b64") or ""), validate=True)
        except Exception:
            blob = b""
        entry = {"name": name}
        if ext not in _AUDIO_EXTS:
            entry["audio"] = False
            out.append(entry); continue
        if not blob:
            entry.update(audio=True, tags=None); out.append(entry); continue
        try:
            audio = mutagen.File(io.BytesIO(blob), easy=False)
        except Exception:
            audio = None
        if audio is None:
            entry.update(audio=True, tags=None, truncated=len(blob) < _PEEK_BUDGET.get(ext, 320000))
            out.append(entry); continue
        t = {"title": "", "artist": "", "album": "", "year": "", "has_cover": False, "duration": 0}
        try:
            if getattr(audio, "info", None) and getattr(audio.info, "length", None):
                t["duration"] = int(audio.info.length)
        except Exception:
            pass
        try:
            if isinstance(audio, FLAC):
                tg = audio.tags or {}
                t["title"] = (tg.get("title") or [""])[0]
                t["artist"] = (tg.get("artist") or [""])[0]
                t["album"] = (tg.get("album") or [""])[0]
                t["year"] = ((tg.get("date") or tg.get("year") or [""])[0] or "")[:4]
                t["track"] = str((tg.get("tracknumber") or [""])[0] or "")
                t["disc"] = str((tg.get("discnumber") or [""])[0] or "")
                t["has_cover"] = bool(audio.pictures)
            elif isinstance(audio, MP4):
                tg = audio.tags or {}
                t["title"] = (tg.get("\xa9nam") or [""])[0]
                t["artist"] = (tg.get("\xa9ART") or [""])[0]
                t["album"] = (tg.get("\xa9alb") or [""])[0]
                t["year"] = str((tg.get("\xa9day") or [""])[0])[:4]
                tr = (tg.get("trkn") or [None])[0]
                t["track"] = str(tr[0]) if isinstance(tr, (list, tuple)) and tr else ""
                dst = (tg.get("disk") or [None])[0]
                t["disc"] = str(dst[0]) if isinstance(dst, (list, tuple)) and dst else ""
                t["has_cover"] = bool(tg.get("covr"))
            else:
                tg = audio.tags
                if tg:
                    for key, dst_k in (("TIT2", "title"), ("TPE1", "artist"),
                                       ("TALB", "album"), ("TRCK", "track"),
                                       ("TPOS", "disc")):
                        v = tg.get(key)
                        if v: t[dst_k] = str(v)[:200]
                    v = tg.get("TDRC") or tg.get("TYER")
                    if v: t["year"] = str(v)[:4]
                    if hasattr(tg, "getall"):
                        t["has_cover"] = bool(tg.getall("APIC"))
        except Exception:
            pass
        stem = os.path.splitext(name)[0]
        # Как и сканер: заголовок из тегов, иначе — имя файла.
        if not t["title"]:
            t["title"] = stem
        entry.update(audio=True, tags=t)
        out.append(entry)
    return {"ok": True, "results": out}


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
