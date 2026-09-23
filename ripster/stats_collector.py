"""Ripster stats — SQLite-backed analytics collector.

Records:
  - Completed downloads (from runner._add_to_history hook)
  - WebSocket connect/disconnect (guest online time)
  - Stream events (BBC + Qobuz/Tidal preview)

All writes are synchronous (called from async code via threadpool or directly).
Reads are used only by /api/stats (low frequency).
"""
from __future__ import annotations

import hashlib
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "ripster_stats.db"


@contextmanager
def _db():
    con = sqlite3.connect(str(DB_PATH), check_same_thread=False, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def init_db() -> None:
    with _db() as con:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS downloads (
                id          TEXT    PRIMARY KEY,
                ts          INTEGER NOT NULL,
                service     TEXT    NOT NULL DEFAULT '',
                engine      TEXT    NOT NULL DEFAULT '',
                quality     TEXT    NOT NULL DEFAULT '',
                artist      TEXT    NOT NULL DEFAULT '',
                album       TEXT    NOT NULL DEFAULT '',
                title       TEXT    NOT NULL DEFAULT '',
                url         TEXT    NOT NULL DEFAULT '',
                tracks      INTEGER NOT NULL DEFAULT 1,
                status      TEXT    NOT NULL DEFAULT 'done',
                session_id  TEXT    NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_dl_ts      ON downloads(ts);
            CREATE INDEX IF NOT EXISTS idx_dl_service ON downloads(service);
            CREATE INDEX IF NOT EXISTS idx_dl_status  ON downloads(status);

            CREATE TABLE IF NOT EXISTS stream_events (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts          INTEGER NOT NULL,
                event       TEXT    NOT NULL,
                stream_type TEXT    NOT NULL DEFAULT 'generic',
                stream_name TEXT    NOT NULL DEFAULT '',
                stream_url  TEXT    NOT NULL DEFAULT '',
                session_id  TEXT    NOT NULL DEFAULT '',
                client_ip   TEXT    NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_se_ts  ON stream_events(ts);
            CREATE INDEX IF NOT EXISTS idx_se_evt ON stream_events(event);

            CREATE TABLE IF NOT EXISTS ws_sessions (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                connect_ts    INTEGER NOT NULL,
                disconnect_ts INTEGER,
                session_id    TEXT    NOT NULL DEFAULT '',
                client_ip     TEXT    NOT NULL DEFAULT '',
                is_guest      INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_ws_ts ON ws_sessions(connect_ts);
        """)


def import_history(history_list: list) -> None:
    """Bulk-import existing history.json into SQLite (INSERT OR IGNORE)."""
    rows = []
    for h in history_list:
        ts = _parse_ts(h.get("ts", ""))
        hid = str(h.get("id") or "")
        if not hid:
            # id='' у двух записей PRIMARY KEY схлопнул бы в одну строку
            # (INSERT OR IGNORE молча съедает вторую). Детерминированный хеш
            # сохраняет обе и не дублирует их при повторном импорте.
            hid = "hist-" + hashlib.sha1(
                f"{h.get('ts', '')}|{h.get('url', '')}|{h.get('title', '')}"
                .encode("utf-8", "replace")).hexdigest()[:16]
        rows.append((
            hid,
            ts,
            h.get("service", ""),
            h.get("engine", ""),
            h.get("quality", ""),
            h.get("artist", ""),
            h.get("album", ""),
            h.get("title", ""),
            h.get("url", ""),
            h.get("tracks", 1) or 1,
            h.get("status", "done"),
            h.get("session_id", ""),
        ))
    if not rows:
        return
    with _db() as con:
        con.executemany("""
            INSERT OR IGNORE INTO downloads
            (id, ts, service, engine, quality, artist, album, title, url, tracks, status, session_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, rows)


def _parse_ts(ts_str: str) -> int:
    if not ts_str:
        return int(time.time())
    try:
        dt = datetime.fromisoformat(ts_str)
    except Exception:
        return int(time.time())
    try:
        return int(dt.timestamp())
    except Exception:
        if dt.tzinfo is None:
            # Windows кидает OSError на наивной .timestamp() у дат до 1970
            # (исторический мусор в history.json превращался в «сейчас» —
            # валидный ISO молча сдвигался на годы). Тот же смысл арифметикой:
            # «как если UTC» минус локальное смещение текущего момента.
            off = datetime.now(timezone.utc).astimezone().utcoffset()
            if off is not None:
                return int(dt.replace(tzinfo=timezone.utc).timestamp() - off.total_seconds())
        return int(time.time())


def record_download(entry: dict) -> None:
    if entry.get("status") not in ("done", "error"):
        return
    ts = _parse_ts(entry.get("ts", ""))
    try:
        with _db() as con:
            con.execute("""
                INSERT OR REPLACE INTO downloads
                (id, ts, service, engine, quality, artist, album, title, url, tracks, status, session_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                entry.get("id", ""),
                ts,
                entry.get("service", ""),
                entry.get("engine", ""),
                entry.get("quality", ""),
                entry.get("artist", ""),
                entry.get("album", ""),
                entry.get("title", ""),
                entry.get("url", ""),
                entry.get("tracks", 1) or 1,
                entry.get("status", "done"),
                entry.get("session_id", ""),
            ))
    except Exception as e:
        print(f"[stats] record_download error: {e}", flush=True)


def record_stream(stream_type: str, stream_name: str, stream_url: str,
                  session_id: str = "", client_ip: str = "") -> None:
    try:
        with _db() as con:
            con.execute("""
                INSERT INTO stream_events (ts, event, stream_type, stream_name, stream_url, session_id, client_ip)
                VALUES (?, 'start', ?, ?, ?, ?, ?)
            """, (int(time.time()), stream_type, stream_name, stream_url, session_id, client_ip))
    except Exception as e:
        print(f"[stats] record_stream error: {e}", flush=True)


def record_ws_connect(session_id: str = "", client_ip: str = "", is_guest: bool = False) -> int:
    try:
        with _db() as con:
            cur = con.execute("""
                INSERT INTO ws_sessions (connect_ts, session_id, client_ip, is_guest)
                VALUES (?, ?, ?, ?)
            """, (int(time.time()), session_id, client_ip, int(is_guest)))
            return cur.lastrowid or 0
    except Exception as e:
        print(f"[stats] record_ws_connect error: {e}", flush=True)
        return 0


def record_ws_disconnect(row_id: int) -> None:
    if not row_id:
        return
    try:
        with _db() as con:
            con.execute("UPDATE ws_sessions SET disconnect_ts = ? WHERE id = ?",
                        (int(time.time()), row_id))
    except Exception as e:
        print(f"[stats] record_ws_disconnect error: {e}", flush=True)


# ── Aggregations ──────────────────────────────────────────────────────────────

_PERIOD_SECS = {
    "day":   86_400,
    "week":  7 * 86_400,
    "month": 30 * 86_400,
    "year":  365 * 86_400,
    "all":   0,
}

_WEEKDAY_NAMES = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
_SERVICE_LABELS = {
    "apple": "Apple Music", "deezer": "Deezer", "qobuz": "Qobuz",
    "tidal": "Tidal", "spotify": "Spotify", "soundcloud": "SoundCloud",
    "beatport": "Beatport", "bbc": "BBC", "jiosaavn": "JioSaavn",
    "yandex": "Yandex Music", "amazon": "Amazon Music",
}


def get_stats(period: str = "week") -> dict:
    now  = int(time.time())
    secs = _PERIOD_SECS.get(period, _PERIOD_SECS["week"])
    since = now - secs if secs else 0

    try:
        with _db() as con:
            return _aggregate(con, since, now, period)
    except Exception as e:
        print(f"[stats] get_stats error: {e}", flush=True)
        return {"period": period, "error": str(e)}


def _aggregate(con, since: int, now: int, period: str) -> dict:
    # ── Downloads ────────────────────────────────────────────────────────────
    row = con.execute(
        "SELECT COUNT(*) cnt, COALESCE(SUM(tracks),0) trk FROM downloads WHERE ts>=? AND status='done'",
        (since,)
    ).fetchone()
    total_dl  = row["cnt"]  or 0
    total_trk = row["trk"]  or 0

    guests = con.execute(
        "SELECT COUNT(DISTINCT session_id) FROM downloads WHERE ts>=? AND status='done' AND session_id!=''",
        (since,)
    ).fetchone()[0] or 0

    by_service = [
        {**dict(r), "label": _SERVICE_LABELS.get(r["name"], r["name"] or "—")}
        for r in con.execute(
            "SELECT service name,COUNT(*) count FROM downloads WHERE ts>=? AND status='done'"
            " GROUP BY service ORDER BY count DESC LIMIT 10", (since,)
        ).fetchall()
    ]

    by_quality = [dict(r) for r in con.execute(
        "SELECT quality name,COUNT(*) count FROM downloads WHERE ts>=? AND status='done'"
        " GROUP BY quality ORDER BY count DESC LIMIT 10", (since,)
    ).fetchall()]

    by_artist = [dict(r) for r in con.execute(
        "SELECT artist name,COUNT(*) count FROM downloads WHERE ts>=? AND status='done' AND artist!=''"
        " GROUP BY artist ORDER BY count DESC LIMIT 20", (since,)
    ).fetchall()]

    # weekday: SQLite %w → 0=Sun; shift to Mon=0. Ведра — локальные ('localtime'):
    # записи лежат как настоящий epoch из локального wall-clock (runner пишет
    # datetime.now(), _parse_ts читает наивный ISO как локальный), а «unixepoch»
    # без модификатора дал бы UTC-часы и график жил бы со сдвигом на пояс.
    wd_raw = con.execute(
        "SELECT CAST(strftime('%w',ts,'unixepoch','localtime') AS INT) wd, COUNT(*) count"
        " FROM downloads WHERE ts>=? AND status='done' GROUP BY wd", (since,)
    ).fetchall()
    wd_map: dict[int, int] = {}
    for r in wd_raw:
        wd_map[(r["wd"] - 1) % 7] = r["count"]
    by_weekday = [{"day": i, "name": _WEEKDAY_NAMES[i], "count": wd_map.get(i, 0)} for i in range(7)]

    by_hour = [{"hour": h, "count": 0} for h in range(24)]
    for r in con.execute(
        "SELECT CAST(strftime('%H',ts,'unixepoch','localtime') AS INT) h, COUNT(*) count"
        " FROM downloads WHERE ts>=? AND status='done' GROUP BY h", (since,)
    ).fetchall():
        by_hour[r["h"]]["count"] = r["count"]

    by_day = [dict(r) for r in con.execute(
        "SELECT strftime('%Y-%m-%d',ts,'unixepoch','localtime') date, COUNT(*) count"
        " FROM downloads WHERE ts>=? AND status='done' GROUP BY date ORDER BY date", (since,)
    ).fetchall()]

    # ── Streams ──────────────────────────────────────────────────────────────
    total_streams = con.execute(
        "SELECT COUNT(*) FROM stream_events WHERE ts>=? AND event='start'", (since,)
    ).fetchone()[0] or 0

    bbc_streams = con.execute(
        "SELECT COUNT(*) FROM stream_events WHERE ts>=? AND event='start' AND stream_type='bbc'", (since,)
    ).fetchone()[0] or 0

    top_streams = [dict(r) for r in con.execute(
        "SELECT stream_name name, stream_type, COUNT(*) count FROM stream_events"
        " WHERE ts>=? AND event='start' AND stream_name!=''"
        " GROUP BY stream_name, stream_type ORDER BY count DESC LIMIT 12", (since,)
    ).fetchall()]

    recent_listens = [
        {"ts": r["ts"], "type": r["stream_type"], "name": r["stream_name"]}
        for r in con.execute(
            "SELECT ts, stream_type, stream_name FROM stream_events"
            " WHERE ts>=? AND event='start' AND stream_name!=''"
            " ORDER BY ts DESC LIMIT 50", (since,)
        ).fetchall()
    ]

    by_stream_type = [dict(r) for r in con.execute(
        "SELECT stream_type name,COUNT(*) count FROM stream_events WHERE ts>=? AND event='start'"
        " GROUP BY stream_type ORDER BY count DESC", (since,)
    ).fetchall()]

    # ── Guest online time (WS sessions) ──────────────────────────────────────
    ws_rows = con.execute(
        "SELECT connect_ts, disconnect_ts FROM ws_sessions WHERE connect_ts>=? AND is_guest=1", (since,)
    ).fetchall()
    guest_minutes = round(sum(
        ((r["disconnect_ts"] or now) - r["connect_ts"]) / 60 for r in ws_rows
    ), 1)

    unique_guests_ws = con.execute(
        "SELECT COUNT(DISTINCT session_id) FROM ws_sessions WHERE connect_ts>=? AND is_guest=1 AND session_id!=''",
        (since,)
    ).fetchone()[0] or 0

    return {
        "period":   period,
        "totals": {
            "downloads":      total_dl,
            "tracks":         total_trk,
            "guests":         max(guests, unique_guests_ws),
            "stream_sessions": total_streams,
            "bbc_sessions":   bbc_streams,
            "preview_sessions": max(total_streams - bbc_streams, 0),
            "guest_minutes":  guest_minutes,
        },
        "by_service":     by_service,
        "by_quality":     by_quality,
        "by_artist":      by_artist,
        "by_weekday":     by_weekday,
        "by_hour":        by_hour,
        "by_day":         by_day,
        "by_stream_type": by_stream_type,
        "top_streams":    top_streams,
        "recent_listens": recent_listens,
    }
