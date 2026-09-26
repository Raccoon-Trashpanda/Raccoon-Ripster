"""Guest link / session management for Ripster.

Links are stored in guest_links.json next to app.py.
A session_id cookie is issued on first visit and maps to the link token.
"""
from __future__ import annotations

import json
import re
import secrets
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from fastapi import Request

LINKS_FILE    = Path(__file__).parent.parent / "guest_links.json"
SESSIONS_FILE = Path(__file__).parent.parent / "guest_sessions.json"
COOKIE_NAME = "ripster-guest"
MAX_LINKS   = 10
LINK_TTL_S  = 365 * 24 * 3600   # 1 year — revoke manually when needed
RATE_WINDOW = 60                  # seconds
RATE_MAX    = 5                   # max queue-add calls per RATE_WINDOW per session

# ── Weekly free-tier caps (per link/token) ──────────────────────────────────
# Владелец: каждый НОВЫЙ пользователь бесплатно получает 7 альбомов + 7 треков
# в неделю на ЛЮБЫЕ сервисы. Старые ссылки (без поля "weekly") не трогаем.
# Активная поддержка (link["support"]["until"] в будущем) снимает лимит.
WEEKLY_WINDOW_S       = 7 * 24 * 3600   # скользящее окно 7 суток
WEEKLY_ALBUMS_DEFAULT = 7
WEEKLY_TRACKS_DEFAULT = 7


def classify_release(service: str, url: str) -> str:
    """Классифицировать ссылку как 'track' (один трек) или 'album' (альбом,
    плейлист, артист, чарт — всё многотрековое). Дефолт для неизвестного —
    'album', НАРОЧНО: так плейлист/сборник нельзя выдать за один трек и обойти
    трековый лимит (см. требование владельца «чтобы не обошли»)."""
    svc = (service or "").lower()
    u = (url or "").lower()
    try:
        parsed = urllib.parse.urlparse(url)
        parts = [p for p in parsed.path.split("/") if p]
        qs = urllib.parse.parse_qs(parsed.query)
    except Exception:
        parts, qs = [], {}
    if svc == "apple" or "music.apple.com" in u:
        if qs.get("i") or "/song/" in u or "/music-video/" in u:
            return "track"
        return "album"
    if svc in ("deezer", "qobuz", "tidal", "spotify", "beatport") or any(
        d in u for d in ("deezer.com", "qobuz.com", "tidal.com", "spotify.com", "beatport.com")
    ):
        return "track" if "/track/" in u else "album"
    if svc == "jiosaavn" or "jiosaavn.com" in u:
        return "track" if "/song/" in u else "album"
    if svc == "soundcloud" or "soundcloud.com" in u:
        # /sets/ = плейлист, голый профиль (1 сегмент) = артист → ведро album;
        # /user/track-slug (≥2 сегмента, без sets) = одиночный трек.
        if "sets" in parts:
            return "album"
        return "track" if len(parts) >= 2 else "album"
    return "album"   # неизвестный сервис — считаем дорогим ведром

# Only these token keys may be stored/updated by guests.
_ALLOWED_GUEST_TOKEN_KEYS = {
    "media-user-token",
    "qobuz-auth-token",
    "qobuz-user-id",
    "deezer-arl",
    "tidal-token",
    "soundcloud-oauth-token",
}

# Characters stripped from filesystem names so computed save-dirs are safe.
_UNSAFE_FS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# Имя файла не должно совпадать с устройством Windows: папку CON создать нельзя,
# а zip "CON.zip" не открывается ничем, кроме самого Windows.
_WIN_RESERVED = re.compile(r'^(?:con|prn|aux|nul|com[0-9]+|lpt[0-9]+)$', re.I)


def _sanitize(s: str) -> str:
    # strip ДО и ПОСЛЕ обрезки: [:80] может снова оставить '.' или ' ' в конце,
    # а Windows молча съедает такой хвост при создании папки — и сверка
    # предсказанного имени с реальным в routes/download.py перестаёт сходиться.
    out = _UNSAFE_FS.sub("_", s or "").strip(". ")[:80].strip(". ")
    if not out:
        return "Unknown"
    if _WIN_RESERVED.match(out.split(".", 1)[0]):
        out = "_" + out
    return out


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(ts: str) -> float:
    """Return POSIX timestamp from ISO-8601 string, or 0 on error."""
    if not ts:
        return 0.0
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return 0.0


class GuestManager:
    def __init__(self):
        self._links: dict[str, dict]   = {}   # token → link dict
        self._sessions: dict[str, str] = {}   # session_id → token
        self._rate: dict[str, list]    = {}   # session_id → [timestamps]
        self._load()

    # ── Persistence ──────────────────────────────────────────────────────────

    def _load(self):
        try:
            if LINKS_FILE.exists():
                data = json.loads(LINKS_FILE.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    self._links = {l["token"]: l for l in data
                                   if isinstance(l, dict) and l.get("token")}
        except Exception as e:
            print(f"[guest] load failed: {e}", flush=True)

        try:
            if SESSIONS_FILE.exists():
                data = json.loads(SESSIONS_FILE.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    # Restore only sessions whose token is still valid
                    for sid, token in data.items():
                        if token in self._links and self._is_active(self._links[token]):
                            self._sessions[sid] = token
        except Exception as e:
            print(f"[guest] sessions load failed: {e}", flush=True)

    def _save(self):
        try:
            LINKS_FILE.write_text(
                json.dumps(list(self._links.values()),
                           ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            print(f"[guest] save failed: {e}", flush=True)

    def _save_sessions(self):
        try:
            SESSIONS_FILE.write_text(
                json.dumps(self._sessions, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as e:
            print(f"[guest] sessions save failed: {e}", flush=True)

    # ── Link management ───────────────────────────────────────────────────────

    def _is_active(self, link: dict) -> bool:
        return link.get("active", False) and _parse_iso(link.get("expires_at", "")) > time.time()

    def active_links(self) -> list[dict]:
        return [l for l in self._links.values() if self._is_active(l)]

    def active_session_count(self) -> int:
        """How many guest sessions are currently live (validated). Used by the
        idle-restart watcher to know when it's safe to restart without cutting a
        guest off mid-use."""
        return sum(1 for sid in list(self._sessions) if self.get_session(sid))

    def all_links(self) -> list[dict]:
        """All links newest-first (for admin view). Annotates each with live session_count."""
        # created_at у двух ссылок часто совпадает (разрешение часов ~0.5 мс), а
        # устойчивая сортировка при равных ключах оставляет порядок вставки —
        # старые шли вперёд. Вторым ключом сам индекс вставки: dict хранит его
        # и после загрузки с диска, reverse=True ставит свежую ссылку выше.
        links = [lk for _, lk in sorted(
            enumerate(self._links.values()),
            key=lambda p: (p[1].get("created_at") or "", p[0]),
            reverse=True)]
        # Count in-memory sessions per token + collect their session ids (don't
        # mutate the stored dicts). The session ids let the admin UI correlate the
        # live download queue (tasks carry session_id) with each guest → a real
        # per-guest "downloading" lamp + progress bar.
        counts: dict[str, int] = {}
        sids_by_tok: dict[str, list] = {}
        for sid, tok in self._sessions.items():
            if self.get_session(sid):  # validate still active
                counts[tok] = counts.get(tok, 0) + 1
                sids_by_tok.setdefault(tok, []).append(sid)
        result = []
        for lk in links:
            d = dict(lk)
            d["session_count"] = counts.get(lk["token"], 0)
            d["sessions"] = sids_by_tok.get(lk["token"], [])
            # Omit guest_tokens from admin listing (security)
            d.pop("guest_tokens", None)
            result.append(d)
        return result

    def create_link(
        self,
        label: str,
        quota_type: str = "unlimited",
        quota_limit: int = 0,
        token_mode: str = "owner",
    ) -> dict:
        # Telegram Mini App links are auto-provisioned per tg-user (kind=="tg")
        # and must not count against the owner's manual share-link budget.
        if len([l for l in self.active_links() if l.get("kind") != "tg"]) >= MAX_LINKS:
            raise ValueError(f"Максимум {MAX_LINKS} активных ссылок")
        token = secrets.token_hex(16)   # 32 hex chars
        now   = _utcnow()
        link: dict = {
            "token":      token,
            "label":      (label or "Guest")[:40],
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(seconds=LINK_TTL_S)).isoformat(),
            "active":     True,
            "quota": {
                "type":       quota_type,   # "unlimited" | "count" | "time"
                "limit":      int(quota_limit),
                "used":       0,
                "started_at": now.isoformat(),
            },
            # Бесплатный недельный кап нового пользователя (старые ссылки его не
            # имеют → check_weekly их пропускает). Активная поддержка снимает.
            "weekly": {
                "albums": WEEKLY_ALBUMS_DEFAULT,
                "tracks": WEEKLY_TRACKS_DEFAULT,
            },
            "weekly_log":   [],           # [{"ts": epoch, "kind": "album"|"track"}]
            "support":      {"until": 0},  # epoch; >now = поддержка активна
            "token_mode":   token_mode,   # "owner" | "guest"
            "guest_tokens": {k: "" for k in _ALLOWED_GUEST_TOKEN_KEYS},
            "activity":     [],
        }
        self._links[token] = link
        self._save()
        return link

    def revoke_link(self, token: str) -> bool:
        if token not in self._links:
            return False
        # Invalidate all sessions for this token
        dead = [sid for sid, t in self._sessions.items() if t == token]
        for sid in dead:
            del self._sessions[sid]
        del self._links[token]
        self._save()
        self._save_sessions()
        return True

    def set_token_mode(self, token: str, mode: str) -> bool:
        if token not in self._links or mode not in ("owner", "guest"):
            return False
        self._links[token]["token_mode"] = mode
        self._save()
        return True

    def update_guest_tokens(self, session_id: str, tokens: dict) -> bool:
        """Called by the guest to store their own credentials."""
        link = self.get_session(session_id)
        if not link:
            return False
        gt = link.setdefault("guest_tokens", {})
        for k, v in tokens.items():
            if k in _ALLOWED_GUEST_TOKEN_KEYS and isinstance(v, str):
                gt[k] = v
        self._save()
        return True

    def validate_token(self, token: str) -> Optional[dict]:
        link = self._links.get(token)
        if not link or not self._is_active(link):
            return None
        return link

    # ── Session management ────────────────────────────────────────────────────

    def create_session(self, token: str) -> Optional[str]:
        if not self.validate_token(token):
            return None
        # Reuse existing session_id for this token so old task auth stays valid
        for sid, tok in self._sessions.items():
            if tok == token:
                return sid
        session_id = secrets.token_hex(24)
        self._sessions[session_id] = token
        self._save_sessions()
        return session_id

    def get_session(self, session_id: str) -> Optional[dict]:
        if not session_id:
            return None
        token = self._sessions.get(session_id)
        if not token:
            return None
        link = self.validate_token(token)
        if not link:
            self._sessions.pop(session_id, None)
            return None
        return link

    def get_session_id_from_request(self, request: Request) -> Optional[str]:
        sid = request.cookies.get(COOKIE_NAME, "") or ""
        return sid if sid else None

    def get_session_from_request(self, request: Request) -> Optional[dict]:
        sid = self.get_session_id_from_request(request)
        return self.get_session(sid) if sid else None

    def is_guest_request(self, request: Request) -> bool:
        return self.get_session_from_request(request) is not None

    # ── Effective token resolution ────────────────────────────────────────────

    def get_effective_tokens(self, session_id: str, owner_config: dict) -> dict:
        """Return the credential set to use when running a task for this session."""
        link = self.get_session(session_id)
        if not link:
            return {}
        if link.get("token_mode") == "owner":
            return {k: owner_config.get(k, "") for k in _ALLOWED_GUEST_TOKEN_KEYS}
        # guest mode: guest tokens override owner, fall back to owner
        gt = link.get("guest_tokens", {})
        return {k: gt.get(k) or owner_config.get(k, "") for k in _ALLOWED_GUEST_TOKEN_KEYS}

    # ── Quota ─────────────────────────────────────────────────────────────────

    def check_quota(self, session_id: str) -> bool:
        link = self.get_session(session_id)
        if not link:
            return False
        q     = link.get("quota", {})
        qtype = q.get("type", "unlimited")
        if qtype == "unlimited":
            return True
        if qtype == "count":
            return q.get("used", 0) < q.get("limit", 0)
        if qtype == "time":
            started = _parse_iso(q.get("started_at", ""))
            return time.time() < started + q.get("limit", 0) * 60
        return True

    def consume_quota(self, session_id: str):
        link = self.get_session(session_id)
        if not link:
            return
        q = link.get("quota", {})
        if q.get("type") == "count":
            q["used"] = q.get("used", 0) + 1
        self._save()

    # ── Weekly free-tier caps (albums / tracks per 7 days) ──────────────────────

    def support_active(self, link: dict) -> bool:
        """True, если у ссылки оплачена поддержка и срок ещё не истёк."""
        try:
            return float((link.get("support") or {}).get("until", 0) or 0) > time.time()
        except Exception:
            return False

    def _prune_weekly(self, link: dict) -> None:
        cutoff = time.time() - WEEKLY_WINDOW_S
        wl = link.get("weekly_log")
        if isinstance(wl, list):
            link["weekly_log"] = [e for e in wl
                                  if isinstance(e, dict) and float(e.get("ts", 0) or 0) >= cutoff]

    def weekly_status(self, session_id: str) -> Optional[dict]:
        """Остаток недельного лимита или None, если правило к ссылке не применяется
        (старый пользователь без поля weekly, либо активная поддержка)."""
        link = self.get_session(session_id)
        if not link:
            return None
        wk = link.get("weekly")
        if not wk or self.support_active(link):
            return None
        self._prune_weekly(link)
        used = {"album": 0, "track": 0}
        for e in link.get("weekly_log", []):
            k = e.get("kind")
            if k in used:
                used[k] += 1
        la = int(wk.get("albums", 0) or 0)
        lt = int(wk.get("tracks", 0) or 0)
        return {"albums_used": used["album"], "albums_limit": la,
                "tracks_used": used["track"], "tracks_limit": lt}

    def check_weekly(self, session_id: str, kind: str, inflight: int = 0) -> bool:
        """True, если ссылке можно скачать ещё одну единицу вида kind
        ('album'|'track') на этой неделе. Учитывает уже начатые (in-flight)
        задачи того же вида, чтобы нельзя было накидать пачку до завершения."""
        link = self.get_session(session_id)
        if not link:
            return False
        wk = link.get("weekly")
        if not wk or self.support_active(link):
            return True                        # старый пользователь / поддержка
        limit = int(wk.get("albums" if kind == "album" else "tracks", 0) or 0)
        if limit <= 0:
            return True
        self._prune_weekly(link)
        used = sum(1 for e in link.get("weekly_log", []) if e.get("kind") == kind)
        return (used + max(0, int(inflight))) < limit

    def record_weekly(self, session_id: str, kind: str) -> None:
        """Отметить завершённую загрузку в недельном журнале (только для ссылок
        с полем weekly). Вызывается на 'done'."""
        if kind not in ("album", "track"):
            return
        link = self.get_session(session_id)
        if not link or not link.get("weekly"):
            return
        self._prune_weekly(link)
        link.setdefault("weekly_log", []).append({"ts": time.time(), "kind": kind})
        self._save()

    def grant_support(self, token: str, add_seconds: float) -> bool:
        """Продлить поддержку ссылки на add_seconds (месяц/полгода/год). Снимает
        недельный кап, пока срок активен. Продление складывается от МАКС(сейчас,
        текущего срока) — остаток не сгорает при досрочной доплате. Оплата
        (перевод/Stars) подключается отдельно и вызывает этот метод."""
        link = self._links.get(token)
        if not link:
            return False
        cur = float((link.get("support") or {}).get("until", 0) or 0)
        base = max(time.time(), cur)
        link["support"] = {"until": base + max(0.0, float(add_seconds))}
        self._save()
        return True

    # ── Rate limiting ─────────────────────────────────────────────────────────

    def check_rate(self, session_id: str) -> bool:
        """Return True if the session is allowed to add a task right now."""
        now       = time.time()
        ts_list   = self._rate.setdefault(session_id, [])
        # Evict old entries
        self._rate[session_id] = [t for t in ts_list if now - t < RATE_WINDOW]
        return len(self._rate[session_id]) < RATE_MAX

    def record_rate(self, session_id: str):
        self._rate.setdefault(session_id, []).append(time.time())

    # ── Activity log ──────────────────────────────────────────────────────────

    def log_activity(self, session_id: str, entry: dict):
        link = self.get_session(session_id)
        if not link:
            return
        acts = link.setdefault("activity", [])
        acts.append({"ts": _utcnow().isoformat(), **entry})
        if len(acts) > 500:
            link["activity"] = acts[-500:]
        self._save()

    # ── Cleanup ───────────────────────────────────────────────────────────────

    def revoke_all_links(self) -> int:
        """Delete all active links and invalidate all sessions. Returns count removed."""
        active_tokens = [t for t, l in self._links.items() if l.get("active")]
        for token in active_tokens:
            del self._links[token]
        self._sessions.clear()
        if active_tokens:
            self._save()
            self._save_sessions()
        return len(active_tokens)

    def cleanup_expired(self):
        """Delete expired or inactive links and evict their sessions."""
        now     = time.time()
        expired = [t for t, l in self._links.items()
                   if not l.get("active") or _parse_iso(l.get("expires_at", "")) < now]
        changed = bool(expired)
        for token in expired:
            dead = [s for s, t in self._sessions.items() if t == token]
            for s in dead:
                del self._sessions[s]
            del self._links[token]
        if changed:
            self._save()
            self._save_sessions()

    # ── Download path helpers ─────────────────────────────────────────────────

    @staticmethod
    def expected_save_dir(task: dict, save_root: str) -> Optional[Path]:
        """Best-effort guess at the output directory for a completed task."""
        m = task.get("meta") or {}
        artist = m.get("albumArtist") or m.get("artist") or ""
        album  = m.get("album") or m.get("title") or ""
        if not artist and not album:
            return None
        root = Path(save_root)
        return root / _sanitize(artist) / _sanitize(album)


# ── Singleton ─────────────────────────────────────────────────────────────────

_instance: Optional[GuestManager] = None


def get_manager() -> GuestManager:
    global _instance
    if _instance is None:
        _instance = GuestManager()
    return _instance
