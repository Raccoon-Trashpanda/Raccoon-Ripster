# -*- coding: utf-8 -*-
"""Свои ключи реле: хранилище, квоты, учёт.

Владелец 24.09.2026: «организовать свои ключи» — стать самим собой тем, чем для
нас является wm.wol.moe: наружу торчит key-сервер на НАШИХ учётках, а доступ к
нему — по ключам, которые выдаём и отзываем сами. Ключи выдаёт бот (`/apikey`),
здесь они лежат.

Что здесь происходит и почему именно так:

* ПЛАЙНТЕКСТ КЛЮЧА НЕ ХРАНИТСЯ. Наружу он отдаётся ровно один раз — в момент
  выпуска (больше его никто не увидит, поэтому в боте он подсвечен как одноразовый).
  В базе лежит только `ref` = HMAC-SHA256(pepper, ключ) и ничего более.
* ПЕПЕР — `session-secret` приложения (``ripster/auth.py``). Без пепера база
  ключей превращалась в радужную таблицу: скопировал файл — и у тебя все ключи
  тех, кто слабее 128 бит. С пепером нужен ещё и сам секрет.
  Побочный эффект обязан быть известен: ПОВОРОТ `session-secret` отзывает ВСЕ
  ключи реле (ref'ы пересчитываются). Это лечится только перевыпуском, и это
  правильно — база на старом секрете больше не проверяется.
* СУТОЧНЫЕ И ЧАСОВЫЕ СЧЁТЧИКИ — не свои, а ``ripster/pacing.py``: он уже
  переживает перезапуск (счётчики на диске), уже различает «час»/«сутки» и уже
  копит штрафы по 429/403. Писать второй такой же счётчик — значит иметь два
  разных ответа на вопрос «сколько человек сегодня дергал Apple».
* QPS и ОДНОВРЕМЕННЫЕ — скользящее окно в памяти. Просить «не чаще N в
  секунду» имеет смысл только здесь и сейчас; после перезапуска окно честнее
  начать заново, чем вспоминать вчерашнюю вспышку.

Никаких учётных данных Apple этот модуль не видит и не хранит: только ключ
доступа, квоты и счётчики.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
import threading
import time
from collections import deque
from pathlib import Path

from . import pacing

#: Префикс ключа: чтобы в логах и в чужом конфиге «rk_…» опознавалось как ключ
#: реле, а не как очередной токен сервиса.
KEY_PREFIX = "rk_"

#: Сколько байт энтропии в ключе. token_urlsafe(24) — 32 символа, ~192 бита:
#: перебор невозможен в принципе, поэтому пеперный HMAC — достаточный примитив
#: (медленный KDF тут защищал бы только от слабых ключей, а таких генератор
#: не производит).
_KEY_BYTES = 24

#: Потолки «по умолчанию» для ключа без своих чисел. Консервативные: один
#: пользователь с одним ключом не должен иметь возможности положить учётку
#: Apple раньше, чем мы увидим проблему в админке.
FALLBACK_QUOTA = {
    "qps": 2,
    "concurrency": 2,
    "per_hour": 120,
    "per_day": 2000,
}

#: Ключи реле в счётчиках пейсинга: `pacing.counts("relay", ref)`.
PACE_SERVICE = "relay"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS keys (
    ref          TEXT PRIMARY KEY,
    label        TEXT NOT NULL DEFAULT '',
    owner        TEXT NOT NULL DEFAULT '',
    created_at   REAL NOT NULL,
    revoked_at   REAL NOT NULL DEFAULT 0,
    last_seen    REAL NOT NULL DEFAULT 0,
    hits         INTEGER NOT NULL DEFAULT 0,
    denied       INTEGER NOT NULL DEFAULT 0,
    qps          INTEGER NOT NULL DEFAULT 0,
    concurrency  INTEGER NOT NULL DEFAULT 0,
    per_hour     INTEGER NOT NULL DEFAULT 0,
    per_day      INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS usage (
    ref      TEXT NOT NULL,
    day      TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    n        INTEGER NOT NULL DEFAULT 0,
    denied   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (ref, day, endpoint)
);
CREATE INDEX IF NOT EXISTS ix_usage_day ON usage(day);
"""

_LOCK = threading.RLock()
_DB: sqlite3.Connection | None = None
_DB_PATH: Path | None = None

#: Скользящее окно QPS: ref → deque[unix]. Живёт до первого обращения после
#: перезапуска — см. шапку модуля, это осознанный выбор, а не забытый.
_WINDOW: dict[str, deque] = {}
#: Сколько ключей сейчас «в работе» (удержание на время обращения к upstream).
_INFLIGHT: dict[str, int] = {}

_secret_provider = None
_base_dir_override: Path | None = None


# ── инфраструктура ───────────────────────────────────────────────────────────

def init(base_dir: Path | str | None = None, secret_provider=None) -> None:
    """Подготовить хранилище. Вызывается при установке маршрутов реле;
    безопасно повторно (смену базы по `base_dir` учитывает)."""
    global _base_dir_override, _secret_provider
    _base_dir_override = Path(base_dir) if base_dir else None
    if secret_provider is not None:
        _secret_provider = secret_provider


def _secret() -> str:
    """Пепер. По умолчанию — хозяйский `session-secret`, тот же, что подписывает
    сессии и шифрует кэш ключей lite. Просроченный/пустой секрет в конфиге
    порождает свежий — как это делает auth._ensure_session_secret."""
    if _secret_provider is not None:
        return str(_secret_provider() or "")
    from . import auth
    return auth._ensure_session_secret()


def _db_path() -> Path:
    base = (_base_dir_override
            or Path(os.environ.get("RIPSTER_BASE_DIR")
                    or Path(__file__).resolve().parent.parent))
    p = base / "dist" / "relay" / "relay_keys.db"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _open() -> sqlite3.Connection:
    global _DB, _DB_PATH
    path = _db_path()
    if _DB is not None and _DB_PATH == path:
        return _DB
    if _DB is not None:
        try:
            _DB.close()
        except Exception:                                      # noqa: BLE001
            pass
    con = sqlite3.connect(str(path), check_same_thread=False, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.executescript(_SCHEMA)
    _DB, _DB_PATH = con, path
    return con


def close() -> None:
    """Только для тестов: закрыть соединение, чтобы файл можно было убрать."""
    global _DB, _DB_PATH
    with _LOCK:
        if _DB is not None:
            try:
                _DB.close()
            except Exception:                                  # noqa: BLE001
                pass
        _DB, _DB_PATH = None, None


# ── ключи ────────────────────────────────────────────────────────────────────

def key_ref(plain: str) -> str:
    """Односторонний указатель ключа. Сам ключ не восстанавливается и не ищется
    по перебору: 192 бита энтропии на входе генератора."""
    s = str(plain or "").strip()
    if not s:
        return ""
    return hmac.new(_secret().encode("utf-8"), s.encode("utf-8"),
                    hashlib.sha256).hexdigest()[:32]


def issue(label: str = "", owner: str = "", quota: dict | None = None,
          ) -> tuple[str, str]:
    """Выпустить ключ → (плаинтекст_один_раз, ref).

    `quota` — любые из qps / concurrency / per_hour / per_day; 0 или отсутствие
    значит «наследовать хозяйский дефолт» (см. defaults()).
    """
    plain = KEY_PREFIX + secrets.token_urlsafe(_KEY_BYTES)
    ref = key_ref(plain)
    q = quota or {}
    now = time.time()
    with _LOCK:
        con = _open()
        con.execute(
            "INSERT INTO keys (ref, label, owner, created_at, qps, concurrency,"
            " per_hour, per_day) VALUES (?,?,?,?,?,?,?,?)",
            (ref, str(label or "")[:80], str(owner or "")[:64], now,
             _num(q.get("qps")), _num(q.get("concurrency")),
             _num(q.get("per_hour")), _num(q.get("per_day"))))
        con.commit()
    return plain, ref


def resolve(plain: str) -> dict | None:
    """Ключ → запись, если он выпущен и НЕ отозван. Иначе None (причину не
    сообщаем: «такого ключа нет» и «ключ отозван» для чужого запроса должны
    звучать одинаково)."""
    ref = key_ref(plain)
    if not ref:
        return None
    with _LOCK:
        row = _open().execute("SELECT * FROM keys WHERE ref = ?", (ref,)).fetchone()
    if row is None or float(row["revoked_at"] or 0) > 0:
        return None
    rec = dict(row)
    rec["ref"] = ref
    return rec


def revoke(ref_or_label: str) -> dict:
    """Отозвать по ref (из админки) или по label (из бота, где ref'а человек не
    знает). Возвращает {"ok", "count"} — отзвать можно и несколько одноимённых:
    label не уникален, и молча отозвать «первый попавшийся» было бы враньем."""
    needle = str(ref_or_label or "").strip()
    if not needle:
        return {"ok": False, "count": 0}
    now = time.time()
    with _LOCK:
        con = _open()
        if len(needle) == 32:
            refs = [needle]
            cur = con.execute(
                "UPDATE keys SET revoked_at = ? WHERE ref = ? AND revoked_at = 0",
                (now, needle))
        else:
            refs = [r["ref"] for r in con.execute(
                "SELECT ref FROM keys WHERE lower(label) = lower(?)"
                " AND revoked_at = 0", (needle,))]
            cur = con.execute(
                "UPDATE keys SET revoked_at = ? WHERE lower(label) = lower(?)"
                " AND revoked_at = 0", (now, needle))
        con.commit()
        n = cur.rowcount
    for ref in refs:
        with _LOCK:
            _WINDOW.pop(ref, None)
            _INFLIGHT.pop(ref, None)
    return {"ok": bool(n), "count": n}


def set_quota(ref: str, **fields) -> dict:
    """Изменить квоту ключа. 0 = «наследовать хозяйский дефолт», -1 = «без
    предела» (хозяйское право сказать «не мешай», ср. pacing.caps, где 0 значит
    ровно это)."""
    ref = str(ref or "").strip()
    allowed = ("qps", "concurrency", "per_hour", "per_day", "label")
    patch = {k: v for k, v in fields.items() if k in allowed}
    if not ref or not patch:
        return {"ok": False, "changed": []}
    sets, vals = [], []
    for k, v in patch.items():
        sets.append(f"{k} = ?")
        vals.append(str(v)[:80] if k == "label" else _num(v))
    vals.append(ref)
    with _LOCK:
        con = _open()
        cur = con.execute(f"UPDATE keys SET {', '.join(sets)} WHERE ref = ?", vals)
        con.commit()
    return {"ok": bool(cur.rowcount), "ref": ref, "changed": sorted(patch)}


def get(ref: str) -> dict:
    with _LOCK:
        row = _open().execute("SELECT * FROM keys WHERE ref = ?",
                              (str(ref or ""),)).fetchone()
    return dict(row) if row else {}


def defaults(config: dict | None = None) -> dict:
    """Хозяйские лимиты по умолчанию (Настройки → «Свои ключи»)."""
    cfg = config or {}
    out = {}
    for k, cfgkey in (("qps", "relay-default-qps"),
                      ("concurrency", "relay-default-concurrency"),
                      ("per_hour", "relay-default-per-hour"),
                      ("per_day", "relay-default-per-day")):
        try:
            out[k] = max(0, int(cfg.get(cfgkey, FALLBACK_QUOTA[k]) or 0))
        except (TypeError, ValueError):
            out[k] = FALLBACK_QUOTA[k]
    return out


def quota_of(rec: dict, config: dict | None = None) -> dict:
    """Действующая квота ключа: свои числа поверх хозяйских дефолтов.

    Ноль в записи = «наследовать» (так ключ выпускается), а не «потолок равен
    нулю»: иначе выпущенный сегодня ключ с дефолтом владельца завтра стал бы
    бесполезен при первой же правке дефолта — что и задумано."""
    d = defaults(config)
    out = {}
    for k in ("qps", "concurrency", "per_hour", "per_day"):
        own = _num((rec or {}).get(k))
        if own == 0:
            out[k] = d[k]              # наследовать хозяйский дефолт
        else:
            out[k] = own if own > 0 else 0   # -1 = без предела → 0 = не считать
    return out


def list_keys(config: dict | None = None, include_revoked: bool = True) -> list[dict]:
    """Список для хозяйской админки. Полей, похожих на секрет, здесь нет и
    физически не может быть: в базе их не лежит."""
    now = time.time()
    sql = "SELECT * FROM keys" + ("" if include_revoked else " WHERE revoked_at = 0")
    with _LOCK:
        rows = [dict(r) for r in _open().execute(sql + " ORDER BY created_at DESC")]
    out = []
    for r in rows:
        ref = r.get("ref") or ""
        c = pacing.counts(PACE_SERVICE, ref, now=now)
        q = quota_of(r, config)
        out.append({
            "ref": ref,
            "hint": ref[:8],
            "label": r.get("label") or "",
            "owner": r.get("owner") or "",
            "created_at": float(r.get("created_at") or 0),
            "revoked": float(r.get("revoked_at") or 0) > 0,
            "last_seen": float(r.get("last_seen") or 0),
            "hits": int(r.get("hits") or 0),
            "denied": int(r.get("denied") or 0),
            "quota": q,
            "inflight": int(_INFLIGHT.get(ref) or 0),
            "used": {"hour": c["hour"], "day": c["day"]},
        })
    return out


def count_active() -> int:
    with _LOCK:
        row = _open().execute("SELECT COUNT(*) AS n FROM keys WHERE revoked_at = 0").fetchone()
    return int(row["n"]) if row else 0


# ── допуск: qps / одновременные / час / сутки ────────────────────────────────

def check(rec: dict, config: dict | None = None,
          now: float | None = None) -> dict:
    """Можно ли этому ключу принять запрос СЕЙЧАС.

    {"ok": True} или {"ok": False, "reason": "qps|concurrency|hour|day",
    "retry_after": секунды}. `retry_after` обязан быть настоящим: клиент,
    получивший 429 без подсказки, повторит молотя."""
    now = time.time() if now is None else now
    ref = str((rec or {}).get("ref") or "")
    if not ref:
        return {"ok": False, "reason": "key", "retry_after": 0.0}
    q = quota_of(rec, config)

    # 1) частота: окно в 1 секунду
    if q["qps"]:
        win = _WINDOW.setdefault(ref, deque(maxlen=max(64, q["qps"] * 4)))
        while win and now - win[0] >= 1.0:
            win.popleft()
        if len(win) >= q["qps"]:
            wait = max(0.05, 1.0 - (now - win[0]))
            return {"ok": False, "reason": "qps", "retry_after": wait}

    # 2) одновременные: удержание снимается в release(); счётчик в памяти
    if q["concurrency"] and _INFLIGHT.get(ref, 0) >= q["concurrency"]:
        return {"ok": False, "reason": "concurrency", "retry_after": 0.25}

    # 3) час и сутки — счётчики пейсинга (переживают перезапуск)
    c = pacing.counts(PACE_SERVICE, ref, now=now)
    if q["per_hour"] and c["hour"] >= q["per_hour"]:
        left = (int(now // 3600) + 1) * 3600 - now
        return {"ok": False, "reason": "hour", "retry_after": max(1.0, left)}
    if q["per_day"] and c["day"] >= q["per_day"]:
        # Бакет «сутки» у pacing — полуночный перекат по UTC-смещению epoch;
        # считать часы до него честно и не нужно: сутки это сутки.
        return {"ok": False, "reason": "day", "retry_after": 3600.0}
    return {"ok": True, "retry_after": 0.0}


def acquire(rec: dict, now: float | None = None) -> None:
    """Занять место в лимитере одновременных (парный release обязателен).

    `now` — часы теста: в проде check и acquire разделены микросекундами, а
    проверять окно в секунду, не умея задать время, нельзя."""
    ref = str((rec or {}).get("ref") or "")
    if not ref:
        return
    now = time.time() if now is None else now
    with _LOCK:
        _WINDOW.setdefault(ref, deque(maxlen=256)).append(now)
        _INFLIGHT[ref] = _INFLIGHT.get(ref, 0) + 1


def release(rec: dict) -> None:
    ref = str((rec or {}).get("ref") or "")
    if not ref:
        return
    with _LOCK:
        n = _INFLIGHT.get(ref, 0) - 1
        if n > 0:
            _INFLIGHT[ref] = n
        else:
            _INFLIGHT.pop(ref, None)


def note_hit(ref: str, endpoint: str, ok: bool = True) -> None:
    """Состоявшийся запрос (ok=True) или отказ по квоте (ok=False).

    Счётчик «час/сутки» двигается ТОЛЬКО на принятый запрос: отказ по нашей
    квоте не должен приближать суточный потолок — иначе злоумышленник с одним
    ключем выбивает его себе сам, и владелец получает «ключ жив, но молчит»."""
    ref = str(ref or "")
    if not ref:
        return
    endpoint = str(endpoint or "?")[:32]
    day = time.strftime("%Y-%m-%d")
    with _LOCK:
        con = _open()
        if ok:
            pacing.note(PACE_SERVICE, ref)
            con.execute("UPDATE keys SET last_seen = ?, hits = hits + 1 WHERE ref = ?",
                        (time.time(), ref))
            con.execute(
                "INSERT INTO usage (ref, day, endpoint, n, denied) VALUES (?,?,"
                " ?,1,0) ON CONFLICT(ref, day, endpoint) DO UPDATE SET n = n + 1",
                (ref, day, endpoint))
        else:
            con.execute("UPDATE keys SET denied = denied + 1 WHERE ref = ?", (ref,))
            con.execute(
                "INSERT INTO usage (ref, day, endpoint, n, denied) VALUES (?,?,"
                " ?,0,1) ON CONFLICT(ref, day, endpoint) DO UPDATE SET denied = denied + 1",
                (ref, day, endpoint))
        con.commit()


def usage_today(ref: str, day: str | None = None) -> list[dict]:
    """Разбор «чем именно этот ключ дёргал реле» за сутки — по эндпоинтам."""
    day = day or time.strftime("%Y-%m-%d")
    with _LOCK:
        rows = _open().execute(
            "SELECT endpoint, n, denied FROM usage WHERE ref = ? AND day = ?"
            " ORDER BY n DESC", (str(ref or ""), day)).fetchall()
    return [{"endpoint": r["endpoint"], "n": int(r["n"]), "denied": int(r["denied"])}
            for r in rows]


def usage_summary(days: int = 7) -> dict:
    """{день: {"n": всего, "denied": отказов}} за последние N суток — для
    хозяйской таблицы, где без графиков, но с числом."""
    days = max(1, min(int(days or 7), 60))
    today = time.time()
    want = [time.strftime("%Y-%m-%d", time.localtime(today - d * 86400))
            for d in range(days)]
    with _LOCK:
        rows = _open().execute(
            "SELECT day, SUM(n) AS n, SUM(denied) AS denied FROM usage"
            " GROUP BY day").fetchall()
    got = {r["day"]: {"n": int(r["n"] or 0), "denied": int(r["denied"] or 0)}
           for r in rows}
    return {d: got.get(d, {"n": 0, "denied": 0}) for d in want}


def _int(v) -> int:
    try:
        return max(0, int(v or 0))
    except (TypeError, ValueError):
        return 0


def _num(v) -> int:
    """Целое КВOTЫ без отсечения низа: -1 здесь осмысленный ответ («без
    предела»), и молча превращать его в ноль нельзя — ноль значит «наследовать»."""
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0
