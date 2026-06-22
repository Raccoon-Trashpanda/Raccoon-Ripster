"""Ripster Coder — in-app audio joiner/converter (xrecode-style, ffmpeg-driven).

Merges the tracks of a (usually DJ-mix) release into ONE continuous file plus a
matching CUE sheet with a clean "essence" name, written into a `mixed/` folder.
The heavy lifting lives in ripster.mixcue; this module is the HTTP surface.

Endpoints:
  POST /api/coder/preview  {task_id?|dir?}            -> proposed name + track list
  POST /api/coder/mix      {task_id?|dir?, name?, fmt} -> build the mix + cue

Install: ripster_coder.install(app, ctx)
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

from fastapi import APIRouter, Request, HTTPException
from fastapi.concurrency import run_in_threadpool

from ripster.mixcue import (build_mix, build_mixes, clean_mix_name, clean_mix_title,
                            convert_tracks, split_cue, _CONVERT)

router = APIRouter()
_cfg: dict = {}
_broadcast = None


def install(app, ctx) -> None:
    global _cfg, _broadcast
    _cfg = ctx.config
    _broadcast = ctx.broadcast
    app.include_router(router)


def _ffmpeg() -> str:
    return (_cfg.get("gamdl-ffmpeg-path") or "ffmpeg").strip() or "ffmpeg"


def _progress_bridge(loop, task_id: str, op: str):
    """Sync callback (runs in the worker thread) that broadcasts a live progress
    event over the WebSocket so the UI shows an overall bar + current track."""
    def cb(cur, total, label, pct):
        if not _broadcast:
            return
        msg = {"type": "coder_progress", "op": op, "task_id": task_id,
               "current": cur, "total": total, "label": label, "pct": pct}
        try:
            asyncio.run_coroutine_threadsafe(_broadcast(msg), loop)
        except Exception:
            pass
    return cb


def coder_out_dir() -> Path:
    """Where merged mixes land — configurable, defaults to <save-path>/mixed."""
    base = (_cfg.get("coder-out-dir") or "").strip()
    if base:
        return Path(base)
    return Path(_cfg.get("save-path") or "downloads") / "mixed"


def _parse_folder_name(folder: str) -> tuple[str, str]:
    """Best-effort "Artist - Album" split from a download folder name."""
    if " - " in folder:
        a, b = folder.split(" - ", 1)
        return a.strip(), b.strip()
    return "", folder.strip()


def _resolve(task_id: str, dir_: str):
    """Return (dir: Path|None, files: list[Path], album: str, artist: str)."""
    from ripster.routes.download import (_find_task_or_history, _get_task_dir,
                                         _is_audio)
    d = None
    album = artist = ""
    if task_id:
        t = _find_task_or_history(task_id)
        if t:
            meta = t.get("meta") or {}
            album  = meta.get("album") or meta.get("title") or ""
            artist = meta.get("artist") or ""
            d = _get_task_dir(t)
    if not d and dir_:
        d = Path(dir_)
    if not d or not d.is_dir():
        return None, [], album, artist
    if not (album or artist):
        artist, album = _parse_folder_name(d.name)
    # The Coder is an owner-only local tool (guests are blocked at the middleware,
    # see GUEST_BLOCKED_PATHS "/api/coder/"). Unlike the download serving endpoints
    # it must NOT be restricted to the configured save roots — the user points it
    # at any folder on their machine (an external music library, a deemix folder
    # outside save-path, etc.). So scan the chosen directory directly. Without
    # this, a folder outside the save roots returned zero files ("пусто").
    try:
        files = sorted(f for f in d.rglob("*") if f.is_file() and _is_audio(f))
    except OSError:
        files = []
    return d, files, album, artist


@router.post("/api/coder/preview")
async def coder_preview(body: dict, request: Request):
    d, files, album, artist = _resolve((body.get("task_id") or "").strip(),
                                       (body.get("dir") or "").strip())
    if not d:
        raise HTTPException(404, "Папка релиза не найдена")
    if len(files) < 2:
        raise HTTPException(400, "Нужно минимум 2 трека для склейки")
    name = clean_mix_name(album, artist)
    src_ext = files[0].suffix.lower().lstrip(".")
    # Probe the first track's real codec — .m4a can be ALAC (lossless) or AAC
    # (lossy), and only lossless merges gaplessly.
    from ripster.mixcue import _probe, _ffprobe_for, _LOSSLESS
    codec = _probe(_ffprobe_for(_ffmpeg()), files[0]).get("codec", "")
    lossless = codec in _LOSSLESS
    return {
        "ok": True,
        "name": name,
        "tracks": [f.name for f in files],
        "count": len(files),
        "dir": str(d),
        "album": album,
        "artist": artist,
        "source_ext": src_ext,
        "codec": codec,
        "lossless": lossless,
        "out_dir": str(coder_out_dir()),
    }


@router.post("/api/coder/files")
async def coder_files(body: dict, request: Request):
    """List every audio file in a release folder with size (and best-effort
    duration) for the XRECODE-style table. Unlike /preview this works for any
    folder (even a single track) and never raises on <2 tracks. Stat + tag read
    run in a threadpool so a big folder can't block the event loop."""
    d, files, album, artist = _resolve((body.get("task_id") or "").strip(),
                                       (body.get("dir") or "").strip())
    if not d:
        raise HTTPException(404, "Папка не найдена")

    def _scan() -> dict:
        # mutagen gives per-file duration without spawning ffprobe N times.
        try:
            from mutagen import File as _MFile           # type: ignore
        except Exception:
            _MFile = None
        out = []
        total_size = 0
        total_dur = 0.0
        for f in files:
            try:
                sz = f.stat().st_size
            except OSError:
                sz = 0
            dur = 0.0
            if _MFile is not None:
                try:
                    mf = _MFile(str(f))
                    dur = float(getattr(getattr(mf, "info", None), "length", 0) or 0)
                except Exception:
                    dur = 0.0
            total_size += sz
            total_dur += dur
            out.append({"name": f.name, "size": sz,
                        "ext": f.suffix.lower().lstrip("."), "dur": round(dur, 1)})
        return {"files": out, "total_size": total_size,
                "total_dur": round(total_dur, 1)}

    scanned = await run_in_threadpool(_scan)
    return {"ok": True, "dir": str(d), "album": album, "artist": artist,
            "count": len(files), "formats": list(_CONVERT.keys()), **scanned}


@router.get("/api/coder/browse")
async def coder_browse(path: str = ""):
    """Folder-tree navigation for the source picker — immediate subdirectories of
    `path` (or the save roots when empty), each annotated with audio-track count.
    Browsing is sandboxed to within the configured save roots. The disk walk
    (iterdir + audio-deep probe per node) runs in a threadpool so expanding a
    folder with many subdirs never blocks the event loop."""
    from ripster.routes.download import _all_save_roots, _is_audio, _has_audio_near

    def _scan(path: str) -> dict:
        roots = [r for r in _all_save_roots() if r.is_dir()]

        def _node(d: Path) -> dict:
            try:
                children = list(d.iterdir())
            except OSError:
                children = []
            auds = [c for c in children if c.is_file() and _is_audio(c)]
            has_sub = any(c.is_dir() for c in children)
            # audio_deep: a multi-disc release root (CD1/CD2 subfolders, no direct
            # audio) is still selectable — the tagger/converter recurse into it.
            deep = len(auds) > 0 or (has_sub and _has_audio_near(d))
            return {"name": d.name, "path": str(d), "tracks": len(auds),
                    "has_audio": len(auds) > 0, "has_subdirs": has_sub,
                    "audio_deep": deep}

        path = (path or "").strip()
        if not path:
            return {"ok": True, "nodes": [_node(r) for r in roots]}
        p = Path(path)
        # Sandbox: only inside a save root.
        if not any(str(p).startswith(str(r)) for r in roots) or not p.is_dir():
            raise HTTPException(403, "Папка вне разрешённых корней")
        try:
            subs = sorted((c for c in p.iterdir() if c.is_dir()),
                          key=lambda x: x.name.lower())
        except OSError:
            subs = []
        return {"ok": True, "nodes": [_node(c) for c in subs]}

    return await run_in_threadpool(_scan, path)


# Library scan is a full recursive walk of every save root — heavy on a big
# collection. Run it OFF the event loop (threadpool) and cache briefly, so
# opening the source picker can never freeze the whole server.
_LIB_CACHE: dict = {"ts": 0.0, "limit": 0, "data": None}
_LIB_TTL = 60.0


def _scan_library(limit: int) -> list:
    from ripster.routes.download import _all_save_roots, _is_audio
    seen, out = set(), []
    for root in _all_save_roots():
        if not root.is_dir():
            continue
        try:
            for d in root.rglob("*"):
                if not d.is_dir() or d in seen:
                    continue
                try:
                    auds = [f for f in d.iterdir() if f.is_file() and _is_audio(f)]
                except OSError:
                    continue
                if not auds:
                    continue
                seen.add(d)
                out.append({"dir": str(d), "name": d.name, "tracks": len(auds),
                            "mtime": d.stat().st_mtime,
                            "ext": auds[0].suffix.lower().lstrip(".")})
        except Exception:
            continue
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out[:limit]


@router.get("/api/coder/library")
async def coder_library(limit: int = 60):
    """Recent release folders (with ≥1 audio file) from the save roots, newest
    first — the source picker for the converter tab. Threadpooled + cached 60s."""
    import time as _t
    now = _t.time()
    if (_LIB_CACHE["data"] is not None and _LIB_CACHE["limit"] >= limit
            and now - _LIB_CACHE["ts"] < _LIB_TTL):
        return {"ok": True, "folders": _LIB_CACHE["data"][:limit],
                "formats": list(_CONVERT.keys()), "cached": True}
    folders = await run_in_threadpool(_scan_library, max(limit, 60))
    _LIB_CACHE.update(ts=now, limit=max(limit, 60), data=folders)
    return {"ok": True, "folders": folders[:limit],
            "formats": list(_CONVERT.keys())}


@router.post("/api/coder/convert")
async def coder_convert(body: dict, request: Request):
    """Batch per-track convert a release folder to a chosen format."""
    task_id = (body.get("task_id") or "").strip()
    d, files, album, artist = _resolve(task_id, (body.get("dir") or "").strip())
    if not d:
        raise HTTPException(404, "Папка не найдена")
    if not files:
        raise HTTPException(400, "Нет аудиофайлов")
    # XRECODE-style table: convert only the rows the user ticked. `only` is a
    # list of filenames (basenames) — when present, restrict to that subset.
    only = body.get("only")
    if isinstance(only, list) and only:
        _sel = set(only)
        files = [f for f in files if f.name in _sel]
        if not files:
            raise HTTPException(400, "Не выбрано ни одного файла")
    fmt     = (body.get("fmt") or "mp3").strip().lower()
    bitrate = (body.get("bitrate") or "320k").strip()
    keepcov = body.get("keep_cover", True)
    srate   = (body.get("sample_rate") or "").strip()   # "" = keep source
    bdepth  = (body.get("bit_depth") or "").strip()      # "" = keep source
    norm    = bool(body.get("normalize"))                # EBU R128 -14 LUFS
    # Optional rename-by-tags template (per-track). Falls back to the app-wide
    # file-rename-template when the toggle is on but no template was supplied.
    rename_tmpl = ""
    if body.get("rename"):
        rename_tmpl = ((body.get("rename_template") or "").strip()
                       or (_cfg.get("file-rename-template") or "").strip()
                       or "{tracknumber}. {artist} - {title}")
    out_dir = (body.get("out_dir") or "").strip() or str(d / "converted" / fmt.upper())
    if _broadcast:
        await _broadcast({"type": "log", "level": "info",
                          "msg": f"🎛 Ripster Coder: конвертирую {len(files)} → {fmt.upper()} {bitrate}…"})
    _pg = _progress_bridge(asyncio.get_running_loop(), task_id, "convert")
    result = await run_in_threadpool(
        convert_tracks, [str(f) for f in files], out_dir, fmt, bitrate,
        _ffmpeg(), bool(keepcov), rename_tmpl, _pg, srate, bdepth, norm)
    if not result.get("ok"):
        if _broadcast:
            await _broadcast({"type": "log", "level": "error",
                              "msg": f"Ripster Coder: ошибка конвертации — {result.get('error','')}"})
        raise HTTPException(500, result.get("error") or "Конвертация не удалась")
    if _broadcast:
        await _broadcast({"type": "log", "level": "success",
                          "msg": f"✓ Ripster Coder: {result['converted']} файл(ов) → {fmt.upper()} в {out_dir}"
                                 + (f" ({result['failed']} ошибок)" if result.get('failed') else "")})
        await _broadcast({"type": "coder_done", "out_dir": out_dir,
                          "converted": result["converted"]})
    return {"ok": True, **result}


@router.post("/api/coder/split")
async def coder_split(body: dict, request: Request):
    """Split an album-image audio file into per-track files via its CUE sheet.
    Source = an explicit `cue` path or the first *.cue found in `dir`."""
    cue = (body.get("cue") or "").strip()
    d   = (body.get("dir") or "").strip()
    fmt = (body.get("fmt") or "source").strip().lower()
    bitrate = (body.get("bitrate") or "320k").strip()
    cue_path = None
    if cue and Path(cue).is_file():
        cue_path = Path(cue)
    elif d and Path(d).is_dir():
        cues = sorted(Path(d).rglob("*.cue"))
        if cues:
            cue_path = cues[0]
    if not cue_path:
        raise HTTPException(404, "CUE-файл не найден (выбери папку с .cue или укажи файл)")
    out_dir = (body.get("out_dir") or "").strip() or str(cue_path.parent / "split")
    if _broadcast:
        await _broadcast({"type": "log", "level": "info",
                          "msg": f"✂ Ripster Coder: режу «{cue_path.name}» по CUE…"})
    _pg = _progress_bridge(asyncio.get_running_loop(), "", "split")
    res = await run_in_threadpool(split_cue, str(cue_path), out_dir, fmt, bitrate, _ffmpeg(), _pg)
    if not res.get("ok"):
        if _broadcast:
            await _broadcast({"type": "log", "level": "error",
                              "msg": f"Ripster Coder: сплит не удался — {res.get('error','')}"})
        raise HTTPException(500, res.get("error") or "Сплит не удался")
    if _broadcast:
        await _broadcast({"type": "log", "level": "success",
                          "msg": f"✓ Ripster Coder: {res['converted']} треков из CUE → {out_dir}"
                                 + (f" ({res['failed']} ошибок)" if res.get('failed') else "")})
        await _broadcast({"type": "coder_done", "out_dir": out_dir, "converted": res["converted"]})
    return {"ok": True, **res}


@router.post("/api/coder/retag")
async def coder_retag(body: dict, request: Request):
    """Re-tag a release folder from the service by ISRC — fixes region-localized
    (e.g. CJK/katakana) metadata back to canonical Latin via Deezer→Apple.
    Reuses tagger.retag_directory (CJK-safe: leaves already-correct tags alone)."""
    d, _files, _album, _artist = _resolve((body.get("task_id") or "").strip(),
                                          (body.get("dir") or "").strip())
    if not d:
        raise HTTPException(404, "Папка не найдена")
    from ripster import tagger as _tg
    logs: list = []
    if _broadcast:
        await _broadcast({"type": "log", "level": "info",
                          "msg": f"🏷 Ripster Coder: ретег по ISRC в «{d.name}»…"})
    res = await _tg.retag_directory(d, _cfg, lambda m: logs.append(m))
    if _broadcast:
        await _broadcast({"type": "log", "level": "success",
                          "msg": f"✓ Ретег: проверено {res['checked']}, "
                                 f"перетеговано {res['retagged']}, пропущено {res['skipped']}"})
    return {"ok": True, **res, "log": logs[-30:]}


@router.post("/api/coder/mix")
async def coder_mix(body: dict, request: Request):
    task_id = (body.get("task_id") or "").strip()
    d, files, album, artist = _resolve(task_id, (body.get("dir") or "").strip())
    if not d:
        raise HTTPException(404, "Папка релиза не найдена")
    if len(files) < 2:
        raise HTTPException(400, "Нужно минимум 2 трека для склейки")

    name = (body.get("name") or "").strip() or clean_mix_name(album, artist)
    fmt  = (body.get("fmt") or "mp3").strip().lower()
    if fmt not in ("mp3", "flac", "source"):
        fmt = "mp3"

    out_dir = coder_out_dir()
    if _broadcast:
        await _broadcast({"type": "log", "level": "info", "task_id": task_id,
                          "msg": f"🎚 Ripster Coder: склеиваю «{name}» ({len(files)} тр., {fmt})…"})

    # Multi-disc aware: one continuous file + CUE PER disc (' (CD N)' suffix) —
    # never merges discs together. Filename = clean "artist - essence",
    # TAG title/album = just the essence (no '<artist> - ' duplication).
    _pg = _progress_bridge(asyncio.get_running_loop(), task_id, "mix")
    res = await run_in_threadpool(
        build_mixes, [str(f) for f in files], str(out_dir), name, fmt, _ffmpeg(),
        clean_mix_title(album, artist), artist, _pg)

    if not res.get("ok"):
        err = next((m.get("error") for m in res.get("mixes", []) if m.get("error")),
                   res.get("error") or "Ошибка склейки")
        if _broadcast:
            await _broadcast({"type": "log", "level": "error", "task_id": task_id,
                              "msg": f"Ripster Coder: ошибка — {err}"})
        raise HTTPException(500, err)

    made = [m for m in res["mixes"] if m.get("ok")]
    names = [Path(m["file"]).name for m in made]
    if _broadcast:
        disc_note = f" ({res['discs']} диска → {len(made)} миксов)" if res.get("multi") else ""
        await _broadcast({"type": "log", "level": "success", "task_id": task_id,
                          "msg": f"✓ Ripster Coder{disc_note}: {', '.join(names)} + .cue → {out_dir}"})
        await _broadcast({"type": "coder_done", "files": [m["file"] for m in made],
                          "names": names, "multi": res.get("multi")})
    return {"ok": True, "multi": res.get("multi"), "discs": res.get("discs"),
            "names": names, "name": names[0] if names else "",
            "warning": next((m.get("warning") for m in made if m.get("warning")), ""),
            "out_dir": str(out_dir), "mixes": made}
