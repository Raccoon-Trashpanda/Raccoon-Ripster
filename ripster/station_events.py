"""События прослушивания станций — фундамент подстройки под вкус.

Зачем. Разбор приложений Яндекса, Spotify и Apple (research/stations_apk_research_2026-09-19.md)
показал одно общее: станция у них подстраивается не по «лайкам», а по тому,
СКОЛЬКО СЕКУНД трек реально слушали и какой он длины. Скип на пятой секунде и
дослушанный до конца трек — противоположные сигналы, и без них станция вечно
собирает «что попало». У нас такого следа не было вовсе: `phone_plays` приходят
без секунд и без скипов.

Что здесь есть: отдельная база `stations.db` (рядом с `ripster_stats.db`), одна
таблица событий, только добавление. Никаких внешних вызовов — это наша
собственная статистика.

Почему отдельный файл, а не `ripster_stats.db`: события станции пишутся часто
(каждый трек, каждый скип), а `ripster_stats.db` читают отчёты и «Раскопки».
Разводим, чтобы блокировка записи не мешала чтению аналитики.

ГОСТИ НЕ ФОРМИРУЮТ ВКУС ВЛАДЕЛЬЦА. Событие гостя пишется с `is_guest=1` и в
расчёт профиля не берётся (правило скилла ripster-taste-profile): иначе чужие
скипы переучат станцию на чужой вкус.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

# Словарь событий — повторяет то, что шлют разобранные приложения, плюс два
# своих: `download` (самый сильный плюс, его нет ни у кого из них) и `ban_artist`.
EVENTS = {
    "station_started", "track_started", "track_finished", "skip",
    "like", "unlike", "dislike", "undislike", "download", "ban_artist",
    "settings_changed", "station_stopped",
}

# Причина окончания трека — по образцу Apple: отладка без неё превращается в
# гадание «почему трек оборвался».
END_REASONS = {"natural", "skip_forward", "skip_back", "ban", "queue_change", "error"}

_DB: Optional[Path] = None
_LOCK = threading.Lock()
_MAX_TEXT = 300


def init(base_dir: str | Path = ".") -> Path:
    """Создать/открыть базу. Вызывается один раз при старте приложения."""
    global _DB
    _DB = Path(base_dir) / "stations.db"
    with _LOCK, sqlite3.connect(_DB) as c:
        c.execute("""CREATE TABLE IF NOT EXISTS station_events (
            id INTEGER PRIMARY KEY,
            ts TEXT NOT NULL, session_id TEXT NOT NULL, batch_id TEXT,
            source TEXT, event TEXT NOT NULL, track_key TEXT,
            service TEXT, service_id TEXT, artist TEXT, title TEXT,
            played_s REAL, length_s REAL, device TEXT,
            is_guest INTEGER DEFAULT 0, extra TEXT)""")
        c.execute("CREATE INDEX IF NOT EXISTS ix_se_track ON station_events(track_key, ts)")
        c.execute("CREATE INDEX IF NOT EXISTS ix_se_sess ON station_events(session_id)")
        c.execute("CREATE INDEX IF NOT EXISTS ix_se_artist ON station_events(artist, ts)")
    return _DB


def _norm(s) -> str:
    return (str(s or "").strip())[:_MAX_TEXT]


def track_key(item: dict) -> str:
    """Ключ трека: ISRC вернее названия, но названием добираем то, где его нет."""
    isrc = _norm(item.get("isrc")).upper().replace("-", "")
    if isrc:
        return f"isrc:{isrc}"
    a = _norm(item.get("artist")).lower()
    t = _norm(item.get("title")).lower()
    return f"name:{a}|{t}" if (a or t) else ""


def record(ev: dict) -> bool:
    """Записать событие. НИКОГДА не роняет вызывающего: потеря одной строки
    статистики не стоит прерванного прослушивания."""
    if _DB is None:
        return False
    try:
        name = _norm(ev.get("event"))
        if name not in EVENTS:
            return False
        played = ev.get("played_s")
        length = ev.get("length_s")
        # Отрицательные секунды — признак битого клиента, а не «немного слушали».
        played = float(played) if isinstance(played, (int, float)) and played >= 0 else None
        length = float(length) if isinstance(length, (int, float)) and length >= 0 else None
        extra = ev.get("extra")
        row = (
            datetime.now().isoformat(timespec="seconds"),
            _norm(ev.get("session_id")) or "?", _norm(ev.get("batch_id")),
            _norm(ev.get("source")), name,
            _norm(ev.get("track_key")) or track_key(ev),
            _norm(ev.get("service")), _norm(ev.get("service_id")),
            _norm(ev.get("artist")), _norm(ev.get("title")),
            played, length, _norm(ev.get("device")) or "pc",
            1 if ev.get("is_guest") else 0,
            json.dumps(extra, ensure_ascii=False)[:1000] if extra else None,
        )
        with _LOCK, sqlite3.connect(_DB, timeout=5) as c:
            c.execute("""INSERT INTO station_events
                (ts,session_id,batch_id,source,event,track_key,service,service_id,
                 artist,title,played_s,length_s,device,is_guest,extra)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", row)
        return True
    except Exception as e:                                     # noqa: BLE001
        print(f"[station-events] не записал {ev.get('event')!r}: {type(e).__name__}: {e}", flush=True)
        return False


# ── Чтение для ранкера ───────────────────────────────────────────────────────
# Считаем только по СТАНЦИЯМ и только по владельцу: скип в альбоме значит
# другое («не та песня сейчас»), а гость слушает своё.

def _rows(sql: str, args: tuple) -> list:
    if _DB is None:
        return []
    try:
        with _LOCK, sqlite3.connect(_DB, timeout=5) as c:
            return c.execute(sql, args).fetchall()
    except Exception as e:                                     # noqa: BLE001
        print(f"[station-events] чтение упало: {type(e).__name__}: {e}", flush=True)
        return []


def artist_stats(since_days: int = 90) -> dict:
    """{артист: {plays, finished, skips, likes, dislikes, downloads, skip_rate}}.

    `skip_rate` считается только когда треков этого артиста было хотя бы три:
    один скип из одного прослушивания — это не «не любит», это случайность.
    """
    since = (datetime.now() - timedelta(days=max(1, since_days))).isoformat(timespec="seconds")
    out: dict[str, dict] = {}
    for artist, event, n in _rows(
            """SELECT artist, event, COUNT(*) FROM station_events
               WHERE ts >= ? AND is_guest = 0 AND artist <> ''
               GROUP BY artist, event""", (since,)):
        d = out.setdefault(artist, {"plays": 0, "finished": 0, "skips": 0,
                                    "likes": 0, "dislikes": 0, "downloads": 0})
        key = {"track_started": "plays", "track_finished": "finished", "skip": "skips",
               "like": "likes", "dislike": "dislikes", "download": "downloads"}.get(event)
        if key:
            d[key] += n
    for d in out.values():
        seen = d["finished"] + d["skips"]
        d["skip_rate"] = round(d["skips"] / seen, 3) if seen >= 3 else None
    return out


def recent_track_keys(days: int = 7) -> set:
    """Что уже звучало в станциях за N дней — чтобы не повторять (кулдаун)."""
    since = (datetime.now() - timedelta(days=max(1, days))).isoformat(timespec="seconds")
    return {r[0] for r in _rows(
        """SELECT DISTINCT track_key FROM station_events
           WHERE ts >= ? AND is_guest = 0 AND event IN ('track_started','track_finished')
             AND track_key <> ''""", (since,)) if r[0]}


def banned_track_keys() -> set:
    """Дизлайк трека — навсегда, пока не снят (`undislike`)."""
    banned, unbanned = set(), set()
    for key, event in _rows(
            """SELECT track_key, event FROM station_events
               WHERE is_guest = 0 AND event IN ('dislike','undislike') AND track_key <> ''
               ORDER BY ts""", ()):
        (banned if event == "dislike" else unbanned).add(key)
        if event == "undislike":
            banned.discard(key)
        else:
            unbanned.discard(key)
    return banned


def session_feedback(session_id: str) -> dict:
    """Что человек сказал ВНУТРИ этой сессии: {artist: вес}. Скип раньше 30 с —
    сильный минус, дослушанное (≥80 % длины) — плюс, скачивание — самый сильный.
    Этим станция подстраивается на ходу, а не только между запусками."""
    w: dict[str, float] = {}
    for artist, event, played, length in _rows(
            """SELECT artist, event, played_s, length_s FROM station_events
               WHERE session_id = ? AND artist <> ''""", (_norm(session_id),)):
        if event == "skip":
            w[artist] = w.get(artist, 0.0) + (-0.6 if (played or 0) < 30 else -0.2)
        elif event == "track_finished":
            if length and played and played >= 0.8 * length:
                w[artist] = w.get(artist, 0.0) + 0.3
        elif event == "like":
            w[artist] = w.get(artist, 0.0) + 0.6
        elif event == "download":
            w[artist] = w.get(artist, 0.0) + 1.5
        elif event == "dislike":
            w[artist] = w.get(artist, 0.0) - 1.0
    return w


def counts() -> dict:
    """Короткая сводка для проверок и сторожа."""
    r = _rows("SELECT COUNT(*), MIN(ts), MAX(ts) FROM station_events", ())
    n, first, last = (r[0] if r else (0, None, None))
    g = _rows("SELECT COUNT(*) FROM station_events WHERE is_guest = 1", ())
    return {"events": n, "first": first, "last": last, "guest_events": (g[0][0] if g else 0)}
