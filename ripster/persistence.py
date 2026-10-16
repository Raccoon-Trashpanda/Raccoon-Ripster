"""Persistence helpers: history, pending queue, watchlist."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# How many FINISHED tasks to keep in the persisted queue list. deemix-style: the
# task list survives a restart with its completed entries intact (until the user
# clears it), not just the ones still pending — but capped so queue.json can't
# grow without bound. Env-overridable so a deployment can keep more/fewer with no
# code change (self-update principle). Set RIPSTER_QUEUE_KEEP_DONE=0 to keep none.
def _queue_keep_done() -> int:
    try:
        return max(0, int(os.environ.get("RIPSTER_QUEUE_KEEP_DONE", "") or 200))
    except (TypeError, ValueError):
        return 200


# ── History ───────────────────────────────────────────────────────────────────

def load_history(history_file: Path) -> list:
    try:
        if history_file.exists():
            return json.loads(history_file.read_text(encoding="utf-8")) or []
    except Exception as e:
        print(f"[history] load failed ({history_file}): {e}. Starting empty.",
              file=sys.stderr, flush=True)
    return []


def save_history(h: list, history_file: Path) -> None:
    try:
        history_file.write_text(json.dumps(h, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"[history] save failed ({history_file}): {e}", file=sys.stderr, flush=True)


# ── Queue persistence (auto-resume) ───────────────────────────────────────────

def save_pending_queue(queue: list, queue_file: Path) -> None:
    """Persist the task list so it survives a restart — deemix-style: queued AND
    finished tasks stay in the list (until the user clears it), not just the ones
    still pending. Running tasks come back as 'queued' on load (interrupted →
    resume); finished tasks return as-is for the record. Finished entries are
    capped (oldest dropped) so the file can't grow unbounded; order is preserved
    so the queue view looks identical after a restart."""
    try:
        cap = _queue_keep_done()
        statuses = [t.get("status") for t in queue]
        term_idx = [i for i, s in enumerate(statuses)
                    if s not in ("queued", "running")]
        if cap <= 0:
            drop = set(term_idx)                                    # keep no finished
        elif len(term_idx) > cap:
            drop = set(term_idx[:len(term_idx) - cap])             # drop oldest finished
        else:
            drop = set()
        rows = [
            {k: v for k, v in t.items() if k != "log"}
            for i, t in enumerate(queue) if i not in drop
        ]
        queue_file.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"[queue] save failed: {e}", file=sys.stderr, flush=True)


def load_pending_queue(queue_file: Path) -> list:
    """Load persisted queue; tasks that were 'running' become 'queued' (interrupted)."""
    try:
        if queue_file.exists():
            tasks = json.loads(queue_file.read_text(encoding="utf-8")) or []
            for t in tasks:
                if t.get("status") == "running":
                    t["status"] = "queued"
                    t["progress"] = 0
                t.setdefault("log", [])
            return tasks
    except Exception as e:
        print(f"[queue] load failed: {e}. Starting empty.", file=sys.stderr, flush=True)
    return []


# ── Watchlist ─────────────────────────────────────────────────────────────────

def load_watchlist(watchlist_file: Path) -> list:
    try:
        if watchlist_file.exists():
            return json.loads(watchlist_file.read_text(encoding="utf-8")) or []
    except Exception as e:
        print(f"[watchlist] load failed ({watchlist_file}): {e}. Starting empty.",
              file=sys.stderr, flush=True)
    return []


def save_watchlist(w: list, watchlist_file: Path) -> None:
    try:
        watchlist_file.write_text(json.dumps(w, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"[watchlist] save failed ({watchlist_file}): {e}", file=sys.stderr, flush=True)
