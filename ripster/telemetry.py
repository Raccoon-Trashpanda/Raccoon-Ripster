"""
Diagnostics telemetry — distributed Ripster instances forward their warn/error
console lines to the OWNER instance so we can debug a tester's problem remotely
without asking them to copy/paste logs.

Two roles live here, both gated by config so the SAME file ships everywhere:

  • CLIENT  (tester builds): broadcast() feeds every console line to record();
    warn/error lines are SECRET-REDACTED, batched, and POSTed to the owner ingest
    URL by a background flusher. 100% best-effort — it never blocks a download and
    never raises into the app.

  • INGEST  (owner build): receives batches and stores them per-instance under
    logs/remote/<instance_id>.jsonl, plus a small index for the owner UI.

Config keys (see config.example.yaml):
  telemetry-forward        bool  client sends                       (default False, opt-in)
  telemetry-url            str   owner ingest base URL (the tunnel)
  telemetry-level          str   min level to forward: warn|error   (default warn)
  telemetry-instance-id    str   anonymous UUID, auto-generated once
  telemetry-token          str   ingest gate. On the CLIENT: what we send (falls
                                 back to the baked-in public constant). On the
                                 INGEST side it picks a TIER, not a yes/no:
                                 public token = accepted under hard limits,
                                 install token = accepted free, empty/foreign =
                                 rejected.  See token_tier() below.
  telemetry-ingest-enabled bool  THIS instance accepts ingest       (default False)
  telemetry-limit-*      int    жёсткие потолки для записей с ПУБЛИЧНЫМ токеном
                                 (байт в сутки и строк в минуту на экземпляр,
                                 архивов в сутки, размер архива, потолок диска
                                 на весь приём). Дефолты — в _LIMITS; любое
                                 нечисловое/нулевое значение = дефолт, а не «выкл».
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any, Optional

# ── module state ─────────────────────────────────────────────────────────────
_cfg: dict = {}
_save_cfg = None
_base_dir: Path = Path(".")
_buf: deque = deque(maxlen=800)          # pending client lines (warn/error)
_started = False

_LEVEL_RANK = {"debug": 0, "info": 1, "stdout": 1, "success": 1,
               "warn": 2, "warning": 2, "error": 3, "critical": 3}

# Lines/strings we must NEVER forward — scrub before they leave the machine.
_REDACT = [
    (re.compile(r"(media-user-token|authorization-token|bearer|x-user-auth-token|"
                r"auth[-_]?token|access[-_]?token|refresh[-_]?token|client[-_]?secret|"
                r"arl|sp[-_]?dc|password|api[-_]?key)"
                # separator: =, :, OR quote-colon-quote in BOTH single and double
                # quotes (streamrip DEBUG logs Python-dict repr → 'key': 'value').
                r"(['\"]?\s*[=:]\s*['\"]?\s*|\s+)([^\s\"',}]{6,})", re.I),
     r"\1\2«…»"),
    # Секрет, переданный движку АРГУМЕНТОМ командной строки. Правило выше ловит
    # только «имя=значение» и знакомые имена, а `--token <значение>` проходило
    # насквозь: живой яндекс-токен нашёлся в errors.log при проверке отчёта
    # 28.07.2026. Флаг оставляем, значение режем.
    (re.compile(r"(--?(?:token|password|passwd|pass|secret|arl|cookie|apikey|api[-_]key|"
                r"auth|key)(?:\s+|=))(\S{6,})", re.I),
     r"\1«…»"),
    (re.compile(r"Bearer\s+[A-Za-z0-9._\-]{12,}", re.I), "Bearer «…»"),
    (re.compile(r"eyJ[A-Za-z0-9._\-]{20,}"), "«jwt…»"),            # JWTs
    (re.compile(r"[A-Za-z0-9_\-]{32,}\.[A-Za-z0-9_\-]{32,}"), "«token…»"),
]


def _id_file() -> Path:
    """A dedicated, stable home for the instance id so it survives even when
    config.yaml can't be persisted (e.g. a read-only Program Files install) or
    gets reset. Prefer a per-user writable dir; fall back to the app dir."""
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or ""
    d = (Path(base) / "Ripster") if base else _base_dir
    return d / "instance_id.txt"


def configure(cfg: dict, save_cfg, base_dir: Path) -> None:
    """Wire globals once at startup. Mint a STABLE anonymous instance id — assigned
    on first start and never changing thereafter, so the owner can identify each
    tester reliably. Recovered from a dedicated file even if config.yaml lost it."""
    global _cfg, _save_cfg, _base_dir
    _cfg, _save_cfg, _base_dir = cfg, save_cfg, Path(base_dir)
    iid = (_cfg.get("telemetry-instance-id") or "").strip()
    # 1) recover from the dedicated id file if the config doesn't have it
    if not iid:
        try:
            f = _id_file()
            if f.is_file():
                iid = (f.read_text(encoding="utf-8").strip() or "")[:12]
        except Exception:
            pass
    # 2) mint exactly once if still missing
    if not iid:
        iid = uuid.uuid4().hex[:12]
    _cfg["telemetry-instance-id"] = iid
    # 3) persist to BOTH config and the dedicated file (idempotent → never changes)
    try:
        if _save_cfg:
            _save_cfg(_cfg)
    except Exception:
        pass
    try:
        f = _id_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        if (not f.is_file()) or f.read_text(encoding="utf-8").strip() != iid:
            f.write_text(iid, encoding="utf-8")
    except Exception:
        pass


def _instance_id() -> str:
    return (_cfg.get("telemetry-instance-id") or "").strip() or "unknown"


def redact(text: str) -> str:
    """Strip credentials from a single line. Always-on, both roles."""
    s = str(text)
    for rx, repl in _REDACT:
        try:
            s = rx.sub(repl, s)
        except Exception:
            pass
    return s[:2000]


# ── CLIENT ───────────────────────────────────────────────────────────────────
# Адрес и токен приёмной стороны держим В КОДЕ, а не только в config.example.yaml.
# Пример конфига применяется лишь к чистой установке (onlyifdoesntexist), поэтому
# у всех, кто ставил раньше, эти поля навсегда остались бы пустыми — а пустой
# адрес отключает отправку целиком, сколько ни щёлкай тумблер. Именно так вся
# диагностика и оказалась мёртвой при живом на вид переключателе. Значение из
# конфига по-прежнему главнее: свой сервер приёма никто не запрещает.
#
# ВАЖНО: это токен ОТПРАВИТЕЛЯ. На ПРИЁМЕ он не «пускать/не пускать», а ярус
# записи: публичная константа из открытой сборки принимается, но под жёсткими
# лимитами — см. token_tier().
_DEFAULT_URL   = "https://raccoon-ripster.serveousercontent.com"
_DEFAULT_TOKEN = "OtHdzmO7GiZPTPjxSaj9lEUCy0A__rhW"


def ingest_url() -> str:
    """Куда стучаться. Свой `telemetry-url` главнее всего; иначе — адрес из
    discovery-файла (ripster/endpoint.py, кэш на сутки), а его нет — вшитый."""
    cfg = (_cfg.get("telemetry-url") or "").strip()
    if cfg:
        return cfg.rstrip("/")
    from ripster import endpoint
    return endpoint.resolve(_DEFAULT_URL)


def ingest_token() -> str:
    return (_cfg.get("telemetry-token") or "").strip() or _DEFAULT_TOKEN


# ── INGEST GATE: КТО пишет, и что из этого следует ───────────────────────────
# `telemetry-token` — ЕДИНСТВЕНная проверка публичных /api/telemetry/ingest и
# /api/telemetry/report (CSRF-изъятие + публичный путь, см. app.py). Значение по
# умолчанию вшито в публичную сборку, то есть секретом НЕ является: кто достал
# исходники или APK — тот умеет писать и строчки, и 12-мегабайтные архивы в
# приёмник владельца (disk-fill DoS + хвост для stored-XSS в UI владельца).
# Ровно этот хвост был назван 18.09.2026 после дыры `/api/pair/*`, и коммит
# 9e5de41 закрыл его строго: публичную константу приём не брал вовсе.
#
# РЕШЕНИЕ ВЛАДЕЛЬЦА 23.09.2026 — строгость бьёт по своим. В logs/remote/*.jsonl
# ~10 живых установок тестеров, и все они шлют ИМЕННО публичную константу: с
# ближайшего перезапуска приложения их отчёты молча перестали бы приезжать.
# Поэтому публичный токен ПРИНИМАЕТСЯ, но как гость худшего сорта — на него
# навешаны жёсткие лимиты (см. «ЛИМИТЫ» ниже) и его записи не исполняются в UI.
# Секретом этот ключ быть не может, значит защита не в токене, а в объёме.
#
# Приватная механика сохранена намеренно: токен установки (выданный владельцем в
# `telemetry-token` либо выданный один раз и лежащий ВНЕ репозитория и вне
# config.yaml, рядом с instance_id.txt) принимается без публичных лимитов. Но
# ТРЕБОВАТЬСЯ он больше не может.
_PRIVATE_TOKEN_FILE = "ingest_token.txt"
_required_token: Optional[str] = None


def _private_token_file() -> Path:
    """Дом приватного токена — тот же, что у instance_id: writes into the user
    profile, never into the install dir (which is mirrored into the public repo)."""
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or ""
    return ((Path(base) / "Ripster") if base else _base_dir) / _PRIVATE_TOKEN_FILE


def _issued_private_token() -> str:
    """Приватный токен ЭТОЙ установки, если он уже есть: значение из
    `telemetry-token` (не публичная константа) либо ранее выданный файл.
    НИЧЕГО не генерирует и не пишет — gate дёргается на каждом чужом запросе,
    заводить файл профиля из-за пришедшей строки нельзя."""
    want = (_cfg.get("telemetry-token") or "").strip()
    if want and want != _DEFAULT_TOKEN:
        return want
    try:
        f = _private_token_file()
        if f.is_file():
            saved = f.read_text(encoding="utf-8").strip()
            if saved and saved != _DEFAULT_TOKEN:
                return saved
    except Exception:
        pass
    return ""


def required_ingest_token() -> str:
    """Ключ установки, при необходимости выданный один раз. С 23.09.2026 это НЕ
    требование приёма (см. token_tier): публичный токен сборки принимается, но
    под лимитами. Механика оставлена намеренно — этим ключом пользуется владелец
    и те, кому он выдан вручную, и их записи идут без публичных ограничений."""
    global _required_token
    if _required_token:
        return _required_token
    found = _issued_private_token()
    if found:
        _required_token = found
        return found
    # выдаём один раз на установку (переживает перезапуск через файл)
    import secrets
    fresh = secrets.token_urlsafe(24)
    try:
        f = _private_token_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(fresh, encoding="utf-8")
    except Exception:
        # Файл не записался (read-only профиль и т.п.) — живём с токеном этого
        # процесса: он всё равно пускает без лимитов, а чужому не известен.
        pass
    _required_token = fresh
    return fresh


def forget_required_token() -> None:
    """Тестам и переоцифровке: забыть кэш требуемого токена."""
    global _required_token
    _required_token = None


def token_tier(presented: str, owner: bool = False) -> str:
    """Кто пишет приёмнику: 'owner' | 'private' | 'public' | '' (пусто = отказ).

    `owner` — запрос пришёл с неподделываемой owner-cookie (браузер владельца
    жмёт «отправить отчёт» у себя); тогда токен не нужен вовсе. 'private' — ключ
    этой установки, пишется свободно. 'public' — публичная константа из сборки:
    принимаем (решение владельца 23.09.2026 — её шлют ~10 тестерских установок),
    но под жёсткими лимитами. ПУСТОЙ и чужой токен отвергаются: принимать надо
    публичный токен сборки, а не любой мусор."""
    if owner:
        return "owner"
    got = str(presented or "").strip()
    if not got:
        return ""
    if got == _DEFAULT_TOKEN:
        return "public"
    if got == _issued_private_token():
        return "private"
    return ""


def token_gate_ok(presented: str, owner: bool = False) -> bool:
    """Пустить ли запись вообще; каким ярусом — см. token_tier()."""
    return bool(token_tier(presented, owner=owner))


def _log_reject(presented, iid, why: str = "") -> None:
    """Отказ виден в консоли владельца, но НЕ содержит секретов: токен не
    печатаем вообще, только его природу. Без этой строки отказ молчит, а
    «тихий отказ» хуже отказа громкого (см. разбор с /api/config/reload).
    `iid` печатается через _safe_id: чужая строка не должна формировать то, что
    увидит владелец."""
    got = str(presented or "").strip()
    why = why or ("пустой токен" if not got else "не тот токен")
    print(f"[telemetry] запись отклонена ({why}) от instance="
          f"{_safe_id(iid) if iid else '—'}; ключ установки: {_private_token_file()}",
          flush=True)


def forwarding_enabled() -> bool:
    # Opt-in (default OFF, security audit 2026-07-21): a fresh public install
    # must not silently phone home before the user has agreed to it. An
    # instance that itself ingests should also never forward to itself.
    if _cfg.get("telemetry-ingest-enabled"):
        return False
    return bool(_cfg.get("telemetry-forward", False)) and bool(ingest_url())


def record(level: str, text: str) -> None:
    """Called from broadcast() for every console line. Cheap + never raises."""
    try:
        if not forwarding_enabled():
            return
        floor = _LEVEL_RANK.get((_cfg.get("telemetry-level") or "warn").lower(), 2)
        if _LEVEL_RANK.get((level or "info").lower(), 1) < floor:
            return
        _buf.append({"t": int(time.time()), "level": (level or "info").lower(),
                     "text": redact(text)})
    except Exception:
        pass


async def _flush_once(client) -> None:
    if not _buf:
        return
    batch = []
    while _buf and len(batch) < 200:
        batch.append(_buf.popleft())
    payload = {
        "instance_id": _instance_id(),
        "name":        (_cfg.get("telemetry-name") or "").strip()[:48],
        "app_version": str(_cfg.get("_release_version") or ""),
        "platform":    f"{os.name}",
        "token":       ingest_token(),
        "lines":       batch,
    }
    base = ingest_url()
    try:
        r = await client.post(f"{base}/api/telemetry/ingest", json=payload, timeout=15)
        if r.status_code >= 400:
            # On a server error keep the newest lines for one more try (bounded).
            for ln in reversed(batch[-100:]):
                _buf.appendleft(ln)
    except Exception:
        for ln in reversed(batch[-100:]):
            _buf.appendleft(ln)


def _enqueue_heartbeat() -> None:
    """Register presence even with ZERO warn/error: append a heartbeat line so an
    idle / error-free instance still shows up in the owner's tester list (otherwise
    only instances that hit a warn/error ever appear). Appended directly — bypasses
    the telemetry-level filter that record() applies."""
    try:
        _buf.append({"t": int(time.time()), "level": "info", "text": "● online"})
    except Exception:
        pass


async def run_forwarder() -> None:
    """Background loop: flush the client buffer every ~15 s, plus a presence
    heartbeat on launch and every ~10 min. No-op if disabled.

    Раз в сутки заодно освежает discovery-адрес приёмника — но ТОЛЬКО когда
    согласие уже дано: опрос GitHub сам по себе означает «эта установка есть»,
    а спрашивать адрес нам не у кого, пока пользователь не сказал «да».
    """
    global _started
    if _started:
        return
    _started = True
    try:
        import httpx
    except Exception:
        return
    from ripster import endpoint
    async with httpx.AsyncClient() as client:
        if forwarding_enabled():
            _enqueue_heartbeat()                 # announce presence the moment we start
            try:
                await endpoint.refresh(_DEFAULT_URL, client=client)
                await _flush_once(client)
            except Exception:
                pass
        _since_hb = 0
        _since_ep = 0
        while True:
            try:
                await asyncio.sleep(15)
                if forwarding_enabled():
                    _since_hb += 15
                    _since_ep += 15
                    if _since_hb >= 600:         # heartbeat every ~10 min
                        _enqueue_heartbeat()
                        _since_hb = 0
                    if _since_ep >= endpoint.TTL_S:
                        await endpoint.refresh(_DEFAULT_URL, client=client)
                        _since_ep = 0
                    await _flush_once(client)
            except asyncio.CancelledError:
                return
            except Exception:
                await asyncio.sleep(5)


# ── INGEST / STORE (owner) ────────────────────────────────────────────────────
def _remote_dir() -> Path:
    d = _base_dir / "logs" / "remote"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _safe_id(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_\-]", "", str(s))[:32] or "unknown"


_MAX_LINES_PER_INSTANCE = 4000


# ── ЛИМИТЫ ПУБЛИЧНОГО ЯРУСА ──────────────────────────────────────────────────
# Публичный токен сборки секретом не является (он лежит в APK), поэтому с
# 23.09.2026 он принимается, но писать с ним можно маленькими дозами. Это
# последняя граница между «кто достал APK» и диском владельца, значит она
# обязана переживать опечатку: любое нечисловое, нулевое или отрицательное
# значение в конфиге означает дефолт, а не «лимит выключен».
_LIMITS = {
    "telemetry-limit-instance-bytes-day":  2 * 1024 * 1024,   # байт в сутки на экземпляр
    "telemetry-limit-instance-lines-min":  120,               # строк в минуту на экземпляр
    "telemetry-limit-batch-lines":         300,               # строк в одном батче
    "telemetry-limit-line-chars":          2000,              # длина одной строки
    "telemetry-limit-reports-day":         3,                 # архивов в сутки на экземпляр
    "telemetry-limit-report-bytes":        12 * 1024 * 1024,  # размер одного архива
    "telemetry-limit-remote-bytes":        200 * 1024 * 1024, # весь приём: logs/remote
}


def _limit(key: str) -> int:
    """Значение лимита из конфига, иначе безопасный дефолт."""
    try:
        v = int(_cfg.get(key))
    except (TypeError, ValueError):
        return _LIMITS[key]
    return v if v > 0 else _LIMITS[key]


# Id задаёт клиент — путь в файловой системе владельца он получать не должен.
# Все живые id тестеров (logs/remote/*.jsonl, девять штук на 23.09.2026) — 12
# hex-символов, правило их покрывает; `../../config`, `%00` и имя на мегабайт —
# нет. Нижняя граница отсекает и мусор вроде "1", который плодит экземпляры.
_IID_RX = re.compile(r"^[A-Za-z0-9_\-]{4,32}$")


def _clean_iid(s) -> str:
    """Валидный instance_id либо пустая строка (то есть отказ записи)."""
    s = str(s or "").strip()
    return s if _IID_RX.match(s) else ""


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def _quota_used(rec: dict, day_key: str, n_key: str) -> int:
    return int(rec.get(n_key) or 0) if rec.get(day_key) == _today() else 0


def _quota_add(rec: dict, day_key: str, n_key: str, n: int) -> None:
    """Счётчик суток живёт в записи индекса, а не только в памяти: перезапуск
    приёмника не должен обнулять лимит тестера."""
    day = _today()
    if rec.get(day_key) != day:
        rec[day_key], rec[n_key] = day, 0
    rec[n_key] = int(rec.get(n_key) or 0) + n


def _tree_bytes(d: Path) -> int:
    total = 0
    try:
        for p in d.rglob("*"):
            try:
                if p.is_file():
                    total += p.stat().st_size
            except OSError:
                pass
    except Exception:
        pass
    return total


def _prune_reports(target: int) -> int:
    """Снести самые старые архивы, пока приём не влезет в `target` байт;
    вернуть число снесённых. Индекс правится заодно: запись без файла
    обещала бы владельцу скачивание того, чего нет."""
    idx = _read_index()
    items = sorted(((int(r.get("t") or 0), iid, r)
                    for iid, rec in idx.items() for r in (rec.get("reports") or [])),
                   key=lambda x: (x[0], x[1], str(x[2].get("code") or "")))
    if not items:
        return 0
    total = _tree_bytes(_remote_dir())
    removed = 0
    for _ts, iid, r in items:
        if total <= target:
            break
        name = os.path.basename(str(r.get("file") or ""))
        try:
            fp = _reports_dir() / name
            size = fp.stat().st_size if fp.is_file() else 0
            fp.unlink(missing_ok=True)
        except OSError:
            size = 0
        rec = idx.get(iid)
        if rec is not None:
            rec["reports"] = [x for x in (rec.get("reports") or [])
                              if os.path.basename(str(x.get("file") or "")) != name]
        total -= size
        removed += 1
    if removed:
        try:
            _index_path().write_text(json.dumps(idx, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
        print(f"[telemetry] приём упирался в потолок диска: снесено старых отчётов: "
              f"{removed} (logs/remote, лимит {_limit('telemetry-limit-remote-bytes')} байт)",
              flush=True)
    return removed


_min_lines: dict = {}          # iid -> deque[(ts, n)]; перезапуск сбрасывает минуту


def _lines_ok(iid: str, add: int) -> bool:
    cap = _limit("telemetry-limit-instance-lines-min")
    now = time.time()
    w = _min_lines.setdefault(iid, deque())
    while w and now - w[0][0] > 60:
        w.popleft()
    if sum(n for _ts, n in w) + add > cap:
        return False
    w.append((now, add))
    return True


def _public_budget(iid: str, add_bytes: int, add_lines: int = 0,
                   reports: bool = False) -> str:
    """Почему публичному ярусу писать нельзя; '' — можно. Все проверки ДО
    записи: отказ не должен оставлять на диске половину батча."""
    cap_disk = _limit("telemetry-limit-remote-bytes")
    rec = _read_index().get(iid) or {}
    if reports:
        if _quota_used(rec, "r_day", "r_count") >= _limit("telemetry-limit-reports-day"):
            return "report limit"
    elif _quota_used(rec, "q_day", "q_bytes") + add_bytes > \
            _limit("telemetry-limit-instance-bytes-day"):
        return "day limit"
    total = _tree_bytes(_remote_dir()) + add_bytes
    if total > cap_disk:
        _prune_reports(int(cap_disk * 0.9))     # сначала старые архивы, потом отказ
        if _tree_bytes(_remote_dir()) + add_bytes > cap_disk:
            return "disk limit"
    if add_lines and not _lines_ok(iid, add_lines):
        return "rate"
    return ""


def store_ingest(payload: dict, client_ip: str = "", owner: bool = False) -> dict:
    """Owner side: persist one batch. Returns {ok, stored}. Never raises."""
    if not _cfg.get("telemetry-ingest-enabled"):
        return {"ok": False, "error": "ingest disabled"}
    if not isinstance(payload, dict):
        return {"ok": False, "error": "bad payload"}
    tier = token_tier(payload.get("token"), owner=owner)
    if not tier:
        _log_reject(payload.get("token"), payload.get("instance_id"))
        return {"ok": False, "error": "bad token"}
    iid = _clean_iid(payload.get("instance_id"))
    if not iid:
        _log_reject(payload.get("token"), payload.get("instance_id"),
                    why="некорректный instance_id")
        return {"ok": False, "error": "bad instance_id"}
    if not _instance_allowed(iid):
        return {"ok": False, "error": "instance limit"}
    lines = payload.get("lines") or []
    if not isinstance(lines, list):
        return {"ok": False, "error": "bad lines"}
    lines = lines[:_limit("telemetry-limit-batch-lines")]
    chars = _limit("telemetry-limit-line-chars")
    kept = [ln for ln in lines if isinstance(ln, dict)]      # не-объект не пишем
    blob = "".join(json.dumps({
        "t":     int(ln.get("t") or time.time()),
        "level": str(ln.get("level") or "info")[:12],
        "text":  redact(ln.get("text") or "")[:chars],
    }, ensure_ascii=False) + "\n" for ln in kept)
    size = len(blob.encode("utf-8"))
    if tier == "public":
        why = _public_budget(iid, size, len(kept))
        if why:
            _log_reject(payload.get("token"), iid, why=f"публичный ярус, {why}")
            return {"ok": False, "error": why}
    fp = _remote_dir() / f"{iid}.jsonl"
    try:
        with fp.open("a", encoding="utf-8") as f:
            f.write(blob)
        _trim_file(fp, _MAX_LINES_PER_INSTANCE)
        _update_index(iid, payload, client_ip, len(kept), add_bytes=size)
        return {"ok": True, "stored": len(kept)}
    except Exception as e:
        return {"ok": False, "error": str(e)[:120]}



def _trim_file(fp: Path, keep: int) -> None:
    try:
        lines = fp.read_text(encoding="utf-8", errors="replace").splitlines()
        if len(lines) > keep:
            fp.write_text("\n".join(lines[-keep:]) + "\n", encoding="utf-8")
    except Exception:
        pass


def _index_path() -> Path:
    return _remote_dir() / "_index.json"


def _read_index() -> dict:
    try:
        return json.loads(_index_path().read_text(encoding="utf-8-sig"))
    except Exception:
        return {}


def _clean_label(s, n: int) -> str:
    """Текст от тестера, который владелец увидит в интерфейсе. Экранирование в
    UI есть, но 23.09.2026 имя подставлялось ВНУТРЬ onclick="…('имя')", где
    HTML-сущности раскодируются до исполнения JS: имя `');alert(1)//` стало
    бы кодом у владельца. Кавычки, угловые скобки и управляющие символы в
    имени не нужны никому — режем на входе, второй рубеж к UI."""
    s = re.sub(r"[\x00-\x1f\x7f<>\"'`\\]", "", str(s or ""))
    return s.strip()[:n]


# Потолок числа экземпляров: id задаёт клиент, и без потолка каждый запрос с
# новым id создавал бы свои файлы — лимиты «на экземпляр» не держат диск.
_MAX_INSTANCES = 200


def _instance_allowed(iid: str) -> bool:
    idx = _read_index()
    return iid in idx or len(idx) < _MAX_INSTANCES


def _update_index(iid: str, payload: dict, client_ip: str, n: int,
                  add_bytes: int = 0) -> None:
    idx = _read_index()
    rec = idx.get(iid) or {"instance_id": iid, "first_seen": int(time.time()),
                           "total": 0, "errors": 0}
    rec["last_seen"]   = int(time.time())
    rec["name"]        = _clean_label(payload.get("name") or rec.get("name"), 48)   # tester-chosen
    rec["app_version"] = _clean_label(payload.get("app_version") or rec.get("app_version"), 32)
    rec["platform"]    = _clean_label(payload.get("platform") or rec.get("platform"), 64)
    rec["ip"]          = (client_ip or rec.get("ip") or "")[:45]
    rec["total"]       = int(rec.get("total", 0)) + n
    rec["errors"]      = int(rec.get("errors", 0)) + sum(
        1 for ln in (payload.get("lines") or [])
        if isinstance(ln, dict) and str(ln.get("level", "")).lower() in ("error", "critical"))
    if add_bytes:
        _quota_add(rec, "q_day", "q_bytes", add_bytes)
    idx[iid] = rec
    try:
        _index_path().write_text(json.dumps(idx, ensure_ascii=False, indent=0), encoding="utf-8")
    except Exception:
        pass


def list_instances() -> list:
    """Owner UI: instances sorted by most-recent activity."""
    idx = _read_index()
    return sorted(idx.values(), key=lambda r: r.get("last_seen", 0), reverse=True)


def get_instance_lines(iid: str, limit: int = 500, level: str = "") -> list:
    """Owner UI: last `limit` stored lines for one instance, optional level floor."""
    fp = _remote_dir() / f"{_safe_id(iid)}.jsonl"
    out: list = []
    try:
        raw = fp.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return out
    floor = _LEVEL_RANK.get((level or "").lower(), 0)
    for line in raw[-limit * 2:]:
        try:
            d = json.loads(line)
        except Exception:
            continue
        if _LEVEL_RANK.get(str(d.get("level", "info")).lower(), 1) >= floor:
            out.append(d)
    return out[-limit:]


def set_label(iid: str, label: str) -> bool:
    """Owner-side rename: store a label that overrides the tester-reported name in
    the owner UI (e.g. real person). Persists in the index."""
    try:
        iid = _safe_id(iid)
        idx = _read_index()
        rec = idx.get(iid)
        if not rec:
            return False
        rec["label"] = str(label or "")[:48]
        idx[iid] = rec
        _index_path().write_text(json.dumps(idx, ensure_ascii=False), encoding="utf-8")
        return True
    except Exception:
        return False


# ── FULL REPORTS (owner side) ────────────────────────────────────────────────
# Построчный поток warn/error годится для «что-то сломалось прямо сейчас», но
# разбирать по нему чужую установку нельзя: не видно версии, окружения, что было
# ДО ошибки. Люди в итоге слали скриншоты, а по скриншоту диагноза не поставить —
# два бага 27.07.2026 нашлись только потому, что удалось расспросить человека.
# Поэтому отдельный канал: пользователь жмёт кнопку и присылает полный архив.

_MAX_REPORTS_PER_INSTANCE = 10      # сколько архивов одного тестера держим на диске
                                    # (размер одного — telemetry-limit-report-bytes)


def _reports_dir() -> Path:
    d = _remote_dir() / "reports"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _report_code() -> str:
    """Короткий код, который человек продиктует голосом или в чате."""
    return uuid.uuid4().hex[:6].upper()


def store_report(meta: dict, blob: bytes, client_ip: str = "",
                 owner: bool = False, force_tier: str = "") -> dict:
    """Owner side: сохранить присланный архив логов. Никогда не бросает.

    `force_tier` — для канала, где ключа нет по построению (аварийные отчёты
    мобилки, см. `/api/telemetry/crash`). Ярус задаётся явным аргументом, а не
    «пустой токен = пустить»: гейт `token_tier()` остаётся единственной
    границей для всех остальных, и его решение от 23.09.2026 («пустой и чужой
    токен — отказ») не размывается. Пустое имя = как раньше, по токену.
    """
    if not _cfg.get("telemetry-ingest-enabled"):
        return {"ok": False, "error": "ingest disabled"}
    if not isinstance(meta, dict):
        return {"ok": False, "error": "bad payload"}
    tier = force_tier or token_tier(meta.get("token"), owner=owner)
    if not tier:
        _log_reject(meta.get("token"), meta.get("instance_id"))
        return {"ok": False, "error": "bad token"}
    iid = _clean_iid(meta.get("instance_id"))
    if not iid:
        _log_reject(meta.get("token"), meta.get("instance_id"),
                    why="некорректный instance_id")
        return {"ok": False, "error": "bad instance_id"}
    if not blob:
        return {"ok": False, "error": "empty"}
    if len(blob) > _limit("telemetry-limit-report-bytes"):
        return {"ok": False, "error": "too big"}
    if not blob.startswith(b"PK"):                 # только zip, ничего исполняемого
        return {"ok": False, "error": "not a zip"}
    if not _instance_allowed(iid):
        return {"ok": False, "error": "instance limit"}
    if tier == "public":
        # чужая сборка с ключом из APK: архивов — три в сутки, и только те, что
        # влезут в общий потолок диска (сначала сносятся старые архивы)
        why = _public_budget(iid, len(blob), reports=True)
        if why:
            _log_reject(meta.get("token"), iid, why=f"публичный ярус, {why}")
            return {"ok": False, "error": why}
    code = _report_code()
    ts   = int(time.time())
    try:
        fp = _reports_dir() / f"{iid}_{ts}_{code}.zip"
        fp.write_bytes(blob)

        idx = _read_index()
        rec = idx.get(iid) or {"instance_id": iid, "first_seen": ts, "total": 0, "errors": 0}
        rec["last_seen"] = ts
        for k, n in (("name", 48), ("app_version", 32), ("platform", 64)):
            v = _clean_label(meta.get(k), n)
            if v:
                rec[k] = v
        rec["ip"] = (client_ip or rec.get("ip") or "")[:45]
        if tier == "public":
            _quota_add(rec, "r_day", "r_count", 1)
        reports = list(rec.get("reports") or [])
        reports.append({"code": code, "t": ts, "size": len(blob),
                        "note": str(meta.get("note") or "")[:300], "file": fp.name})
        # старые архивы удаляем вместе с записями — иначе диск утечёт незаметно
        while len(reports) > _MAX_REPORTS_PER_INSTANCE:
            old = reports.pop(0)
            try:
                (_reports_dir() / str(old.get("file") or "")).unlink(missing_ok=True)
            except Exception:
                pass
        rec["reports"] = reports
        idx[iid] = rec
        _index_path().write_text(json.dumps(idx, ensure_ascii=False), encoding="utf-8")

        _schedule_bot_notify({**reports[-1], "name": rec.get("label") or rec.get("name") or "",
                              "app_version": rec.get("app_version", ""),
                              "platform": rec.get("platform", ""),
                              "instance_id": iid}, fp)
        return {"ok": True, "code": code}
    except Exception as e:
        return {"ok": False, "error": str(e)[:120]}


# ── доставка отчёта владельцу в Telegram ─────────────────────────────────────
# Отчёт, который лежит на диске и ждёт, пока владелец откроет страницу, — это
# почти то же самое, что скриншот в переписке: узнаёшь о проблеме поздно.
# Поэтому архив сразу уходит в личку владельцу вместе с описанием.

def _bot_cfg() -> dict:
    """Токен и id владельца из tgbot/config.json.

    Файл приватный и в публичный репозиторий не попадает, так что у обычного
    пользователя его просто нет — доставка молча пропускается. Никакого токена
    в коде и в поставке.
    """
    try:
        p = _base_dir / "tgbot" / "config.json"
        d = json.loads(p.read_text(encoding="utf-8-sig"))
        if d.get("bot_token") and d.get("owner_id"):
            return d
    except Exception:
        pass
    return {}


async def notify_owner_bot(rec: dict, zip_path: Path) -> tuple[bool, str]:
    """Отправить владельцу карточку отчёта и сам архив. Возвращает (ок, причина)."""
    cfg = _bot_cfg()
    if not cfg:
        return False, "tgbot/config.json недоступен"
    token   = str(cfg["bot_token"])
    owner   = str(cfg["owner_id"])
    # Локальный Bot API снимает потолок на размер файла; если контейнер лежит,
    # уходим на официальный адрес.
    bases = [str(cfg.get("local_bot_api") or "").rstrip("/"), "https://api.telegram.org"]

    when = time.strftime("%d.%m.%Y %H:%M", time.localtime(rec.get("t") or time.time()))
    who  = rec.get("name") or rec.get("instance_id") or "—"
    note = (rec.get("note") or "").strip()
    text = (
        f"📨 <b>Отчёт о логах</b>  <code>{rec.get('code','')}</code>\n"
        f"От: <b>{_esc_html(str(who))}</b>\n"
        f"Версия: {_esc_html(str(rec.get('app_version') or '—'))}\n"
        f"Система: {_esc_html(str(rec.get('platform') or '—'))}\n"
        f"Когда: {when}\n"
        f"Размер: {round((rec.get('size') or 0) / 1024)} КБ"
    )
    if note:
        text += f"\n\n💬 <i>{_esc_html(note[:600])}</i>"

    import httpx
    last = "нет ответа"
    for base in [b for b in bases if b]:
        try:
            async with httpx.AsyncClient(timeout=120) as c:
                r = await c.post(f"{base}/bot{token}/sendMessage",
                                 data={"chat_id": owner, "text": text,
                                       "parse_mode": "HTML"})
                if r.status_code != 200:
                    last = f"sendMessage {r.status_code}"
                    continue
                if zip_path and zip_path.exists():
                    with zip_path.open("rb") as fh:
                        r2 = await c.post(
                            f"{base}/bot{token}/sendDocument",
                            data={"chat_id": owner,
                                  "caption": f"Логи · отчёт {rec.get('code','')}"},
                            files={"document": (zip_path.name, fh, "application/zip")})
                    if r2.status_code != 200:
                        last = f"sendDocument {r2.status_code}"
                        continue
            return True, base
        except Exception as e:                                # noqa: BLE001
            last = f"{type(e).__name__}: {str(e)[:90]}"
    return False, last


def _esc_html(s: str) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _schedule_bot_notify(rec: dict, zip_path: Path) -> None:
    """Отправка не должна задерживать ответ отправителю и не должна ронять приём."""
    async def _run():
        try:
            ok, why = await notify_owner_bot(rec, zip_path)
            print(f"[telemetry] report {rec.get('code')} -> bot: "
                  f"{'delivered via ' + why if ok else 'NOT delivered (' + why + ')'}",
                  flush=True)
        except Exception as e:                                # noqa: BLE001
            print(f"[telemetry] report delivery to bot failed: {e}", flush=True)
    try:
        asyncio.get_running_loop().create_task(_run())
    except RuntimeError:
        pass                       # не в цикле событий — просто не отправляем


def list_reports() -> list:
    """Owner UI: все присланные архивы, свежие сверху."""
    out: list = []
    for rec in _read_index().values():
        for r in rec.get("reports") or []:
            out.append({**r,
                        "instance_id": rec.get("instance_id", ""),
                        "name":        rec.get("label") or rec.get("name") or "",
                        "app_version": rec.get("app_version", ""),
                        "platform":    rec.get("platform", "")})
    return sorted(out, key=lambda r: r.get("t", 0), reverse=True)


def report_path(code: str) -> Optional[Path]:
    code = _safe_id(code).upper()
    for r in list_reports():
        if str(r.get("code", "")).upper() == code:
            fp = _reports_dir() / str(r.get("file") or "")
            if fp.exists():
                return fp
    return None


def clear_instance(iid: str) -> bool:
    try:
        for r in (_read_index().get(_safe_id(iid), {}) or {}).get("reports") or []:
            try:
                (_reports_dir() / str(r.get("file") or "")).unlink(missing_ok=True)
            except Exception:
                pass
        (_remote_dir() / f"{_safe_id(iid)}.jsonl").unlink(missing_ok=True)
        idx = _read_index()
        idx.pop(_safe_id(iid), None)
        _index_path().write_text(json.dumps(idx, ensure_ascii=False), encoding="utf-8")
        return True
    except Exception:
        return False
