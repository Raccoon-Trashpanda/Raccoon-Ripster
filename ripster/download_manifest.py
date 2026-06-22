"""Central download manifest — the single source of truth that maps a finished
task (its short release number) to the exact output directory and file list.

Why this exists: serving endpoints (download-file / zip / gofile) used to
*guess* the output directory with mtime/metadata heuristics. With parallel
downloads and engines that don't print their save path, the guess could miss and
the UI reported "папка не найдена" even though the files were right there. This
manifest is written ONCE at completion — when the directory is known for certain
— and read back by id, so resolution is deterministic and survives restarts.

Document format (downloads_manifest.json, atomic write, bounded):
    { "<task_id>": {id, short, dir, files[], title, artist, service,
                    quality, url, ts} }
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from threading import Lock

_LOCK = Lock()
_MANIFEST_FILE: Path = Path("downloads_manifest.json")
_MAX = 4000
_cache: dict | None = None


def init(base_dir) -> None:
    """Point the manifest at <base_dir>/downloads_manifest.json (call once at startup)."""
    global _MANIFEST_FILE, _cache
    _MANIFEST_FILE = Path(base_dir) / "downloads_manifest.json"
    _cache = None


def _load() -> dict:
    global _cache
    if _cache is not None:
        return _cache
    try:
        data = json.loads(_MANIFEST_FILE.read_text(encoding="utf-8"))
        _cache = data if isinstance(data, dict) else {}
    except Exception:
        _cache = {}
    return _cache


def _save(data: dict) -> None:
    tmp = _MANIFEST_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, _MANIFEST_FILE)


def short_id(task_id: str) -> str:
    """Release number shown to the user — first 8 hex chars, uppercase."""
    clean = (task_id or "").replace("-", "")
    return clean[:8].upper() if clean else "?"


def record(task_id: str, directory, files, task: dict) -> bool:
    """Record where a finished task's files live + the exact file list.
    Returns True on success. Safe to call repeatedly (last write wins)."""
    if not task_id or not directory:
        return False
    meta = task.get("meta") or {}
    names = []
    for f in (files or []):
        try:
            names.append(f.name if hasattr(f, "name") else os.path.basename(str(f)))
        except Exception:
            pass
    entry = {
        "id":      task_id,
        "short":   short_id(task_id),
        "dir":     str(directory),
        "files":   names,
        "title":   meta.get("title") or meta.get("album") or "",
        "artist":  meta.get("artist") or meta.get("albumArtist") or "",
        "service": task.get("service") or "",
        "quality": task.get("quality") or "",
        "url":     task.get("url") or "",
        # Partial-download bookkeeping (issue #5). Usually unset at record time —
        # the silent-partial detection runs AFTER the manifest is first written —
        # so these default empty and get patched in by set_partial() below once
        # the shortfall + its reason are known. Kept here too so a single-pass
        # task that already knows it's partial carries the reason immediately.
        "partial":        bool(task.get("_partial")),
        "missing":        int(task.get("_missing") or 0),
        "partial_reason": task.get("_partial_reason") or "",
        "failed_tracks":  task.get("_failed_tracks") or [],   # issue #5b: [{track,reason}]
        "ts":      time.time(),
    }
    try:
        with _LOCK:
            data = _load()
            data[task_id] = entry
            if len(data) > _MAX:
                for k in sorted(data, key=lambda k: data[k].get("ts", 0))[: len(data) - _MAX]:
                    data.pop(k, None)
            _save(data)
        return True
    except Exception as e:
        print(f"[manifest] record failed for {short_id(task_id)}: {e}", flush=True)
        return False


def set_partial(task_id: str, got: int, expected: int, missing: int,
                reason: str = "", failed: list = None) -> bool:
    """Patch an existing manifest entry with partial-download info (issue #5).

    The silent-partial guard in runner.py only learns a release is short AFTER
    record() has written the entry, so the reason is stamped in a second step —
    same pattern as mark_delivered(). Serving endpoints (web history) read
    `partial_reason` to tell the user WHY N of M arrived, not just that some are
    missing."""
    if not task_id:
        return False
    try:
        with _LOCK:
            data = _load()
            ent = data.get(task_id)
            if ent is None:
                return False
            ent["partial"]        = True
            ent["got"]            = int(got or 0)
            ent["expected"]       = int(expected or 0)
            ent["missing"]        = int(missing or 0)
            ent["partial_reason"] = reason or ent.get("partial_reason") or ""
            if failed:
                ent["failed_tracks"] = failed
            _save(data)
            return True
    except Exception as e:
        print(f"[manifest] set_partial failed for {short_id(task_id)}: {e}", flush=True)
    return False


def mark_delivered(task_id: str) -> bool:
    """Stamp a task as fully delivered (bot uploaded every file to the chat).
    auto_cleanup keys retention off this: an UNdelivered task is never eaten by
    the time-sweep within the safety ceiling, so a slow/queued delivery over a
    flaky link can't lose files mid-send."""
    if not task_id:
        return False
    try:
        with _LOCK:
            data = _load()
            if task_id in data:
                data[task_id]["delivered_ts"] = time.time()
                _save(data)
                return True
    except Exception as e:
        print(f"[manifest] mark_delivered failed for {short_id(task_id)}: {e}", flush=True)
    return False


def lookup(task_id: str) -> dict | None:
    """Return the manifest entry for a task id, or None."""
    if not task_id:
        return None
    with _LOCK:
        return _load().get(task_id)


def all_entries() -> dict:
    """Snapshot copy of the whole manifest ({task_id: entry}). Used by the
    time-based disk cleanup to find finished releases past their retention age."""
    with _LOCK:
        return dict(_load())


def remove(task_ids) -> None:
    """Drop the given task ids from the manifest (after their folders are deleted)."""
    ids = set(task_ids or [])
    if not ids:
        return
    with _LOCK:
        data = _load()
        for tid in ids:
            data.pop(tid, None)
        _save(data)
