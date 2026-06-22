"""Time-based disk cleanup.

Deletes a finished release's folder N minutes after it completed, so the
download disk doesn't fill up over a long session. N is the ``auto-delete-minutes``
config key (1–60; 0 or absent = disabled), driven by the slider in the web UI.

Safe because: files are mirrored to the Telegram cache channel (they live on
Telegram's servers, so a future request is still served instantly), and guests
download straight after completion — well inside any 1–60 minute window. The
sweep only ever removes a folder that sits STRICTLY inside a known save root, so
it can never wipe the library root itself.
"""
from __future__ import annotations

import asyncio
import shutil
import time
from pathlib import Path

_TICK = 30        # seconds between sweeps
_MIN_GRACE = 90   # never delete within this many seconds of DELIVERY (final race guard)
# Hard ceiling for a task the bot has NOT acked as delivered yet. Bot delivery of
# a multi-track album over a flaky link / behind a queue can take far longer than
# the configured retention, so a download-completion timer alone eats undelivered
# files (observed: a 14-track Spotify album deleted mid-send). We therefore keep
# UNdelivered folders until this ceiling — long enough for any real delivery, but
# bounded so a never-delivered (e.g. blocked chat) folder is still reclaimed.
_UNDELIVERED_CEILING = 6 * 3600   # 6h


def _save_roots() -> list[Path]:
    try:
        from ripster.routes.download import _all_save_roots
        out = []
        for p in _all_save_roots():
            try:
                out.append(Path(p).resolve())
            except Exception:
                pass
        return out
    except Exception:
        return []


def _under_root(d: Path, roots: list[Path]) -> bool:
    """True only if d is strictly inside a save root (never equal to it) — so we
    delete a release folder, never the whole library."""
    try:
        d = d.resolve()
    except Exception:
        return False
    for r in roots:
        if d == r:
            return False
        try:
            d.relative_to(r)
            return True
        except ValueError:
            continue
    return False


async def run(config: dict) -> None:
    """Background loop — start once at app startup. Reads the retention minutes
    fresh each tick so the UI slider takes effect without a restart."""
    from ripster import download_manifest as _dm
    while True:
        await asyncio.sleep(_TICK)
        try:
            mins = int(config.get("auto-delete-minutes", 0) or 0)
        except Exception:
            mins = 0
        if mins <= 0:
            continue
        mins = max(1, min(60, mins))
        # Grace floor: never delete within ~90s of completion, regardless of the
        # setting, so the sweep can't race the bot's delivery — _finalize polls the
        # manifest up to ~60s before sending, and a slow convert/restart can delay
        # it further. Keeps even a "1 min" setting from eating undelivered files.
        now = time.time()
        roots = _save_roots()
        if not roots:
            continue
        removed: list[str] = []
        for tid, entry in _dm.all_entries().items():
            dts = entry.get("delivered_ts")
            if dts:
                # Delivered → reclaim `mins` after delivery (with the grace floor).
                if now - dts < max(mins * 60, _MIN_GRACE):
                    continue
            else:
                # NOT delivered yet → keep until the safety ceiling so a slow /
                # queued bot delivery is never eaten mid-send. (Web-guest tasks
                # have no ack either; they download well within the ceiling.)
                if now - entry.get("ts", 0) < _UNDELIVERED_CEILING:
                    continue
            raw = entry.get("dir", "")
            if not raw:
                removed.append(tid)
                continue
            d = Path(raw)
            if not d.exists():
                removed.append(tid)          # already gone → just drop the entry
                continue
            if not _under_root(d, roots):    # safety net: outside the library → skip
                continue
            try:
                # Delete ONLY this task's own folder — never climb to shared
                # parents (e.g. 'ALAC (Lossless)/' holds many tasks' content).
                await asyncio.to_thread(shutil.rmtree, d, ignore_errors=True)
                removed.append(tid)
                print(f"[autodelete] removed {d} (older than {mins}m)", flush=True)
            except Exception as e:
                print(f"[autodelete] failed {d}: {e}", flush=True)
        if removed:
            _dm.remove(removed)
