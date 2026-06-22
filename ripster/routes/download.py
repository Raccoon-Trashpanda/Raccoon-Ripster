"""File & archive download routes.

  GET  /api/download-file?task_id=X   stream single file or zip for a task
  POST /api/zip-request               zip multiple task outputs and stream

Security:
  - Owner: can download any file within save-path
  - Guest: can only download files from tasks tagged with their session_id
  - All paths are validated to be within save-path (no traversal)
"""
from __future__ import annotations

import asyncio
import os
import re
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Iterator, Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse

from ripster.guest_manager import get_manager

router = APIRouter()

_config:  dict = {}
_queue:   list = []
_history: list = []

# Deliverable files: audio + music-video containers. gamdl writes music videos as
# .m4v/.mp4 — they MUST be here or _find_audio_files ignores them, the manifest
# records 0 files, and the bot reports "no files on disk" for a video that's there.
_AUDIO_EXTS = {".m4a", ".mp3", ".flac", ".ogg", ".opus", ".aac", ".alac", ".wav",
               ".mp4", ".m4v", ".mov", ".mkv", ".webm"}

# Per-session rate limit for /api/zip-request: at most one active ZIP build.
_zip_in_progress: set[str] = set()

_AUDIO_MEDIA: dict[str, str] = {
    ".mp4":  "video/mp4",
    ".m4v":  "video/mp4",
    ".mov":  "video/quicktime",
    ".mkv":  "video/x-matroska",
    ".webm": "video/webm",
    ".m4a":  "audio/mp4",
    ".flac": "audio/flac",
    ".mp3":  "audio/mpeg",
    ".ogg":  "audio/ogg",
    ".opus": "audio/ogg",
    ".wav":  "audio/wav",
}


def _err(msg: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": msg}, status_code=status)


_save_history_fn = None
_broadcast_fn    = None


def install(app, ctx) -> None:
    global _config, _queue, _history, _save_history_fn, _broadcast_fn
    _config          = ctx.config
    _queue           = ctx.queue
    _history         = ctx.download_history
    _save_history_fn = ctx.save_history
    _broadcast_fn    = ctx.broadcast
    app.include_router(router)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _save_root() -> Path:
    return Path(_config.get("save-path", "downloads")).resolve()


def _all_save_roots() -> list[Path]:
    """All configured per-service save paths + the global save-path.
    Used to validate that a file is inside at least one legitimate save root."""
    from ripster.service_config import all_save_paths
    roots: set[Path] = set()
    for p in all_save_paths(_config):
        try:
            roots.add(Path(p).resolve())
        except Exception:
            pass
    if not roots:
        roots.add(_save_root())
    return list(roots)


def _safe_path(p: Path, roots: list[Path] | None = None) -> bool:
    """Return True if p is inside any configured save root (no traversal)."""
    check = roots if roots is not None else _all_save_roots()
    rp = p.resolve()
    for root in check:
        try:
            rp.relative_to(root)
            return True
        except ValueError:
            pass
    return False


def _is_audio(p: Path) -> bool:
    return p.suffix.lower() in _AUDIO_EXTS


def _norm(s: str) -> str:
    """Lowercase + drop spaces/punctuation — for fuzzy folder-name matching.
    Keeps Unicode word chars so Cyrillic/CJK names still compare correctly."""
    return re.sub(r"\W+", "", (s or "").casefold())


def _find_audio_files(directory: Path, roots: list[Path] | None = None) -> list[Path]:
    """Recursively collect audio files in a directory, sorted."""
    if not directory.is_dir():
        return []
    r = roots if roots is not None else _all_save_roots()
    return sorted(
        f for f in directory.rglob("*")
        if f.is_file() and _is_audio(f) and _safe_path(f, r)
    )


def _has_audio_near(d: Path) -> bool:
    """True if d contains audio files directly OR in any immediate subdirectory.
    Used by strategy-3 so multi-disc Album/ dirs (no direct audio, only Disc 1/, Disc 2/)
    are included as candidates.
    """
    try:
        for child in d.iterdir():
            if child.is_file() and _is_audio(child):
                return True
            if child.is_dir():
                try:
                    if any(_is_audio(f) for f in child.iterdir() if f.is_file()):
                        return True
                except OSError:
                    pass
    except OSError:
        pass
    return False


def _find_task(task_id: str) -> Optional[dict]:
    for t in _queue:
        if t.get("id") == task_id:
            return t
    return None


def _find_task_or_history(task_id: str) -> Optional[dict]:
    """Return task from live queue; if absent (server restart), reconstruct from history."""
    t = _find_task(task_id)
    if t:
        return t
    for h in _history:
        if h.get("id") == task_id:
            return {
                "id":              h["id"],
                "url":             h.get("url", ""),
                "status":          h.get("status", "done"),
                "session_id":      h.get("session_id", ""),
                "_guest_token":    h.get("_guest_token", ""),
                "_base_save_path": h.get("_base_save_path") or str(_save_root()),
                "_start_time":     h.get("_start_time", 0.0),
                "_done_time":      h.get("_done_time",  0.0),
                "_save_dir":       h.get("_save_dir", ""),
                "_dl_file":        h.get("_dl_file",   0),
                "_dl_zip":         h.get("_dl_zip",    0),
                "_dl_gofile":      h.get("_dl_gofile", 0),
                "_dl_errors":      h.get("_dl_errors", 0),
                "meta": {
                    "title":  h.get("title", ""),
                    "artist": h.get("artist", ""),
                    "album":  h.get("album", "") or h.get("title", ""),
                },
            }
    return None


def _iter_subdirs(p: Path, roots: list[Path] | None = None) -> Iterator[Path]:
    try:
        for child in p.iterdir():
            if child.is_dir() and _safe_path(child, roots):
                yield child
    except OSError:
        pass


def _find_nested_album(base: Path, artist: str, album: str,
                       roots: list[Path] | None = None, max_depth: int = 4) -> Optional[Path]:
    """Find an ``<album>`` folder at ANY depth under ``base`` — covers service/
    quality-nested layouts (e.g. ``apple/ALAC (Lossless)/Artist/Album``) that the
    flat ``base/artist/album`` heuristic misses. Conservative: the folder NAME must
    match the album AND, when an artist is known, its PARENT must match the artist
    — so it can't grab an unrelated same-named folder. Requires audio files; prefers
    the newest match. Bounded depth.

    This is what lets a skip-everything retry resolve (gamdl re-run with every track
    already on disk emits no fresh-mtime files, so the time-scan can't find them, and
    a fresh task_id means no marker — but the album folder is right there on disk)."""
    from ripster.guest_manager import _sanitize
    sa = _sanitize(artist) if (artist or "").strip() else ""
    sal = _sanitize(album)
    if not sal:
        return None
    best, best_mt = None, -1.0
    stack = [(base, 0)]
    while stack:
        d, depth = stack.pop()
        if depth >= max_depth:
            continue
        for child in _iter_subdirs(d, roots):
            if _sanitize(child.name) == sal and (not sa or _sanitize(child.parent.name) == sa):
                if _find_audio_files(child, roots):
                    try:
                        mt = child.stat().st_mtime
                    except OSError:
                        mt = 0.0
                    if mt > best_mt:
                        best, best_mt = child, mt
            stack.append((child, depth + 1))
    return best


def _get_task_dir(task: dict) -> Optional[Path]:
    """Best-effort output directory for a completed task.

    Strategy (in order):
      0. Marker file scan: walk save roots looking for _ripster.txt containing
         the task ID — set on completion for engines that know their output dir,
         and written lazily on first download request for others.
      1. Explicit _save_dir captured from engine JSON output — exact, reliable.
      2. Metadata heuristic: artist/album subdirs matched against sanitized names.
      3. Time-based scan up to 3 levels deep — newest audio-containing dir
         modified after task start. Falls back to a 24-hour window when
         _start_time is unknown (e.g. old history entries).
    After resolving via strategy 1-3 the marker file is written so that
    subsequent lookups always hit strategy 0.
    """
    roots = _all_save_roots()
    task_id = task.get("id", "")

    # -1. Download manifest — the authoritative record written at completion.
    #     Deterministic, survives restarts, immune to the parallel-download mtime
    #     race. This is the "release number → directory" map; checked first.
    if task_id:
        try:
            from ripster import download_manifest as _dm
            ent = _dm.lookup(task_id)
            if ent and ent.get("dir"):
                p = Path(ent["dir"])
                if p.is_dir() and _safe_path(p, roots):
                    return p
        except Exception:
            pass

    def _write_marker_lazy(directory: Path) -> None:
        if not task_id:
            return
        from ripster.task_marker import MARKER_FILENAME, write_marker
        if not (directory / MARKER_FILENAME).exists():
            try:
                write_marker(directory, task_id, task)
            except Exception:
                pass

    # 0. Marker file scan (fastest for repeated requests)
    if task_id:
        from ripster.task_marker import find_dir_by_task_id
        for root in roots:
            found = find_dir_by_task_id(task_id, root)
            if found and found.is_dir():
                return found

    # 1. Explicit path set by runner from Go --json output
    d = task.get("_save_dir")
    if d:
        p = Path(d)
        if p.is_dir() and _safe_path(p, roots):
            _write_marker_lazy(p)
            return p

    # Resolve the base save root for this task
    base = Path(task.get("_base_save_path") or str(_save_root()))
    if not base.is_dir():
        base = _save_root()
    if not base.is_dir():
        return None

    # 2. Metadata heuristic — try artist/album combinations.
    #    Live queue tasks nest metadata under task["meta"]; history entries
    #    (see runner._add_to_history) flatten it to top-level title/artist/album
    #    with no "meta" dict. Read both, or downloads from history resolve with
    #    empty metadata and fall through to the newest-folder mtime scan (wrong
    #    folder for every old task).
    m      = task.get("meta") or {}
    artist = m.get("albumArtist") or m.get("artist") or task.get("artist") or ""
    album  = m.get("album") or m.get("title") or task.get("album") or task.get("title") or ""
    title  = m.get("title") or task.get("title") or ""
    if album:
        from ripster.guest_manager import _sanitize
        candidates: list[Optional[Path]] = [
            (base / _sanitize(artist) / _sanitize(album)) if artist else None,
            base / _sanitize(album),
        ]
        for p in filter(None, candidates):
            if p.is_dir() and _safe_path(p, roots) and _find_audio_files(p, roots):
                _write_marker_lazy(p)
                return p

    # 2b. streamrip flat-folder match — Qobuz/Tidal name their folders
    #     "{albumartist} - {title} (year) [FLAC] …" directly under the save
    #     root, so the strategy-2 "artist/album" candidates never hit. Match a
    #     normalised "artist - X" prefix instead; this stays exact even when
    #     several per-track downloads run in parallel (unlike the mtime scan).
    if artist and (title or album):
        prefixes = [_norm(f"{artist} - {x}") for x in (title, album) if x]
        prefixes = [p for p in prefixes if len(p) >= 4]
        if prefixes:
            best_p: Optional[Path] = None
            best_mt = 0.0
            for d in _iter_subdirs(base, roots):
                nd = _norm(d.name)
                if not any(nd.startswith(pre) for pre in prefixes):
                    continue
                if not _find_audio_files(d, roots):
                    continue
                try:
                    mt = d.stat().st_mtime
                except OSError:
                    continue
                if mt > best_mt:
                    best_mt, best_p = mt, d
            if best_p:
                _write_marker_lazy(best_p)
                return best_p

    # 2c. Nested artist/album match — service/quality-nested layouts (Apple's
    #     apple/<quality>/Artist/Album) sit deeper than strategy-2's flat
    #     base/artist/album candidates. Find the album folder at any depth (its
    #     parent must match the artist when known) so a skip-everything retry
    #     (gamdl re-run, all tracks already on disk → no fresh mtime for the
    #     time-scan, fresh task_id → no marker) still resolves. Additive: only
    #     runs when 0-2b missed, so normal deliveries are untouched.
    if album:
        nested = _find_nested_album(base, artist, album, roots)
        if nested:
            _write_marker_lazy(nested)
            return nested

    # 3. Time-based scan: walk 3 levels, pick audio dir whose mtime falls
    #    within [start_ts - 10, done_ts + 120].  Without the upper bound, a
    #    newer task's folder would silently win over the correct one.
    start_ts = task.get("_start_time", 0.0)
    done_ts  = task.get("_done_time",  0.0)
    # History entries written before _done_time was captured only carry the ISO
    # "ts" (≈ completion time). Use it as the upper-bound fallback so the scan
    # doesn't silently pick a folder created by a much later download.
    if not done_ts and task.get("ts"):
        try:
            from datetime import datetime as _dt
            done_ts = _dt.fromisoformat(task["ts"]).timestamp()
        except (ValueError, TypeError):
            done_ts = 0.0
    cutoff   = (start_ts - 10) if start_ts else 0.0
    # When _done_time is known, reject directories modified more than 2 minutes
    # after the task finished (those belong to subsequent downloads).
    upper    = (done_ts + 120) if done_ts else float("inf")
    try:
        dirs: list[Path] = []
        for lvl1 in _iter_subdirs(base, roots):
            dirs.append(lvl1)
            for lvl2 in _iter_subdirs(lvl1, roots):
                dirs.append(lvl2)
                for lvl3 in _iter_subdirs(lvl2, roots):
                    dirs.append(lvl3)
        best: Optional[Path] = None
        best_mtime = 0.0
        for d in dirs:
            try:
                mt = d.stat().st_mtime
            except OSError:
                continue
            if mt < cutoff or mt > upper:
                continue
            if not _has_audio_near(d):
                continue
            if mt > best_mtime:
                best_mtime = mt
                best = d

        # Promote to nearest ancestor that contains MORE audio files (multi-disc fix):
        # e.g. strategy-3 finds "Album/Disc 2/" but "Album/" contains Disc 1 + Disc 2.
        if best:
            best_count = len(_find_audio_files(best, roots))
            p = best.parent
            while _safe_path(p, roots) and p != base and p != base.parent:
                try:
                    p_mt = p.stat().st_mtime
                except OSError:
                    break
                if p_mt < cutoff:
                    break
                p_count = len(_find_audio_files(p, roots))
                if p_count > best_count:
                    best = p
                    best_count = p_count
                p = p.parent

        # NB: deliberately do NOT _write_marker_lazy(best) here. Strategy 3 is a
        # low-confidence mtime guess; persisting a marker would make a wrong guess
        # permanent (strategy 0 then always returns it) and poison the folder for
        # an unrelated task. Markers are only written for the high-confidence
        # strategies 1/2/2b above, which match on explicit path or metadata.
        return best
    except Exception:
        return None


def _authorize_task(request: Request, task: dict) -> bool:
    """Return True if the requester may access this task's files.

    Security model (mirrors _is_owner in guest.py):
      - A valid guest session is NEVER treated as owner.
      - Guests can only download tasks tagged with their own session_id or link token.
      - Tasks with session_id=="" are owner tasks; guests cannot access them.
    """
    gm  = get_manager()
    sid = gm.get_session_id_from_request(request)

    # Guest session check FIRST — guest is never owner
    if sid and gm.get_session(sid):
        task_sid = task.get("session_id", "")
        if not task_sid:
            return False  # owner task — guests can't access
        # Exact session_id match (normal case)
        if task_sid == sid:
            return True
        # Fallback: same link token — handles session rotation after restart
        # The task stores _guest_token = link token at queue time
        task_token = task.get("_guest_token", "")
        if task_token:
            current_token = gm._sessions.get(sid, "")
            if current_token and current_token == task_token:
                return True
        return False

    # Not a guest session — verify owner auth
    from ripster.auth import verify_session_cookie
    if verify_session_cookie(request.cookies.get("ripster-session", "")):
        return True

    # Auth disabled → anyone without a guest session is owner
    try:
        from ripster.auth import is_enabled
        if not is_enabled():
            return True
    except Exception:
        pass

    return False


def _bump(task_id: str, counter: str) -> None:
    """Increment a download counter on the live task or history entry, then persist."""
    import asyncio as _aio
    for t in _queue:
        if t.get("id") == task_id:
            t[counter] = t.get(counter, 0) + 1
            if _broadcast_fn:
                try:
                    loop = _aio.get_running_loop()
                    loop.create_task(_broadcast_fn({"type": "dl_counter",
                                                    "task_id": task_id,
                                                    "counter": counter,
                                                    "value":   t[counter]}))
                except RuntimeError:
                    pass
            return
    for h in _history:
        if h.get("id") == task_id:
            h[counter] = h.get(counter, 0) + 1
            if _save_history_fn:
                try:
                    _save_history_fn(_history)
                except Exception:
                    pass
            return


def _write_zip_file(files: list[Path], base_dir: Path | None = None) -> str:
    """Write files to a temp ZIP on disk and return the path.
    Caller is responsible for deleting the file (use BackgroundTasks).
    Uses disk instead of RAM — safe for large collections.
    Preserves directory structure relative to base_dir when provided.
    Always includes _ripster.txt from base_dir (if present) at the ZIP root.

    CPU-heavy and synchronous — call via ``asyncio.to_thread`` from async code
    so the event loop is not blocked (a blocked loop makes the tunnel 502).
    """
    from ripster.task_marker import MARKER_FILENAME
    fd, path = tempfile.mkstemp(suffix=".zip", prefix="ripster_")
    os.close(fd)
    seen: set[str] = set()

    # Prepend marker file so it appears first in the archive
    all_files: list[tuple[Path, str | None]] = []  # (file, forced_arcname or None)
    if base_dir:
        marker = base_dir / MARKER_FILENAME
        if marker.is_file():
            all_files.append((marker, MARKER_FILENAME))  # always at ZIP root

    for f in files:
        all_files.append((f, None))

    # ZIP_STORED (no compression): audio files (flac/m4a/mp3) are already
    # compressed — DEFLATE burns CPU for ~0% gain and blocks long enough that
    # the serveo tunnel returns 502. STORED is near-instant (byte copy).
    with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as zf:
        for f, forced in all_files:
            if forced:
                arcname = forced
            elif base_dir:
                try:
                    arcname = str(f.relative_to(base_dir)).replace("\\", "/")
                except ValueError:
                    arcname = f.name
            else:
                arcname = f.name
            # Disambiguate collisions
            orig, n = arcname, 1
            while arcname in seen:
                stem = Path(orig).stem
                suf  = Path(orig).suffix
                arcname = f"{stem}_{n}{suf}"
                n += 1
            seen.add(arcname)
            zf.write(str(f), arcname)
    return path


def _cleanup_zip(path: str, sid: str | None = None) -> None:
    """Background task: delete temp ZIP and release the per-session rate limit slot."""
    try:
        os.unlink(path)
    except OSError:
        pass
    if sid:
        _zip_in_progress.discard(sid)


# ── Routes ────────────────────────────────────────────────────────────────────

def _guest_log(request: Request, **kwargs):
    try:
        gm  = get_manager()
        sid = gm.get_session_id_from_request(request)
        if sid and gm.get_session(sid):
            gm.log_activity(sid, kwargs)
    except Exception:
        pass


@router.get("/api/download-file")
async def download_file(
    request: Request,
    background_tasks: BackgroundTasks,
    task_id: str = Query(...),
    force_zip: bool = Query(False, alias="zip"),
    check: bool = Query(False),
):
    task = _find_task_or_history(task_id)
    if not task:
        _guest_log(request, event="dl_error", reason="task_not_found", task_id=task_id)
        return _err("Task not found", 404)
    if task.get("status") != "done":
        _guest_log(request, event="dl_error", reason="not_finished",
                   task_id=task_id, status=task.get("status"))
        return _err("Task is not finished", 400)
    if not _authorize_task(request, task):
        _guest_log(request, event="dl_error", reason="access_denied", task_id=task_id)
        raise HTTPException(403, "Access denied")

    d = _get_task_dir(task)
    if not d:
        _guest_log(request, event="dl_error", reason="files_missing", task_id=task_id,
                   title=(task.get("meta") or {}).get("title", ""))
        # Hint when SC engine's auto-fallback is the likely culprit so the
        # guest doesn't think files just vanished.
        fb = (task.get("meta") or {}).get("fallback_target") or task.get("_fallback_target")
        if fb:
            return _err(f"Этот трек был перенаправлен на другой сервис — ищи задачу #{fb[:8]} в очереди.", 404)
        if task.get("engine") == "soundcloud" and task.get("tracks_err"):
            return _err("SoundCloud вернул только зашифрованные стримы — Lucida их не дешифрует. "
                        "Авто-fallback на Deezer/Qobuz/Apple запущен; жди новых задач в очереди.", 404)
        return _err("Папка результата не найдена — возможно файлы были перемещены или задача не завершилась успешно.", 404)

    files = _find_audio_files(d)
    if not files:
        _guest_log(request, event="dl_error", reason="no_audio_files", task_id=task_id)
        return _err("No audio files found in output directory.", 404)

    # check=1 — validate only, no file transfer (used by JS preflight to avoid double-ZIP build)
    if check:
        return JSONResponse({"ok": True, "files": len(files)})

    title = (task.get("meta") or {}).get("title") or task_id
    if len(files) == 1 and not force_zip:
        f     = files[0]
        media = _AUDIO_MEDIA.get(f.suffix.lower(), "application/octet-stream")
        _guest_log(request, event="dl_ok", task_id=task_id, title=title, filename=f.name)
        _bump(task_id, "_dl_file")
        return FileResponse(str(f), media_type=media, filename=f.name)

    # Multiple files or force_zip — write to temp file on disk, stream, then delete
    from ripster.guest_manager import _sanitize
    zip_name = _sanitize(title) + ".zip"
    tmp_path = await asyncio.to_thread(_write_zip_file, files, d)
    _guest_log(request, event="dl_ok", task_id=task_id, title=title, filename=zip_name,
               files=len(files))
    _bump(task_id, "_dl_zip")
    background_tasks.add_task(_cleanup_zip, tmp_path)
    return FileResponse(tmp_path, media_type="application/zip", filename=zip_name)


@router.post("/api/cloud-upload")
async def cloud_upload(body: dict, request: Request):
    """Upload task output to Gofile.io and return the download URL."""
    task_id = body.get("task_id", "")
    if not task_id:
        raise HTTPException(400, "task_id required")

    task = _find_task_or_history(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    if task.get("status") != "done":
        raise HTTPException(400, "Task is not finished")
    if not _authorize_task(request, task):
        raise HTTPException(403, "Access denied")

    from ripster.cloud import upload_task_to_gofile
    try:
        url = await upload_task_to_gofile(task)
    except RuntimeError as e:
        raise HTTPException(500, str(e))
    _bump(task_id, "_dl_gofile")
    return {"ok": True, "url": url}


@router.post("/api/zip-request")
async def zip_request(body: dict, request: Request, background_tasks: BackgroundTasks):
    task_ids: list[str] = body.get("task_ids") or []
    if not task_ids or len(task_ids) > 50:
        raise HTTPException(400, "task_ids must be a non-empty list of up to 50 IDs")

    # Rate limit: one active ZIP build per guest session
    gm  = get_manager()
    sid = gm.get_session_id_from_request(request)
    if sid and not gm.get_session(sid):
        sid = None  # expired or invalid
    if sid and sid in _zip_in_progress:
        raise HTTPException(429, "A ZIP request is already in progress for your session")
    if sid:
        _zip_in_progress.add(sid)

    try:
        # Each entry: (Path file, Path base_dir_for_this_task)
        task_files: list[tuple[Path, Path]] = []
        for tid in task_ids:
            task = _find_task_or_history(tid)  # also covers historical tasks
            if not task:
                continue
            if task.get("status") != "done":
                continue
            if not _authorize_task(request, task):
                raise HTTPException(403, f"Access denied for task {tid}")
            d = _get_task_dir(task)
            if d:
                for f in _find_audio_files(d):
                    task_files.append((f, d))

        if not task_files:
            raise HTTPException(404, "No downloadable files found")

        # Deduplicate by resolved path, preserving order
        seen_paths: set[str] = set()
        unique: list[tuple[Path, Path]] = []
        for f, base in task_files:
            k = str(f.resolve())
            if k not in seen_paths:
                seen_paths.add(k)
                unique.append((f, base))

        # When all files share the same base dir, preserve relative structure.
        # When multiple dirs are involved, fall back to flat names.
        bases = {base for _, base in unique}
        common_base = next(iter(bases)) if len(bases) == 1 else None
        files_only = [f for f, _ in unique]

        tmp_path = await asyncio.to_thread(_write_zip_file, files_only, common_base)
        background_tasks.add_task(_cleanup_zip, tmp_path, sid)
        return FileResponse(tmp_path, media_type="application/zip", filename="ripster_download.zip")
    except HTTPException:
        # Release rate-limit slot immediately on known errors
        if sid:
            _zip_in_progress.discard(sid)
        raise
    except Exception:
        if sid:
            _zip_in_progress.discard(sid)
        raise
