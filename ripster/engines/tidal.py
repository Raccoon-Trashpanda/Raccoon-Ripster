"""
Tidal engine — downloads via **OrpheusDL** (orpheus/modules/tidal).

Why not streamrip: streamrip can only refresh a Tidal access_token with its own
built-in client_id. A token pasted from the browser (cid=8049) or an old config
refresh-token (different uid/cid) can never be refreshed by streamrip → every
~16 h the access_token expired and downloads died with 401. It also can't parse
Tidal's DASH manifests (Hi-Res 24-bit), so MQA/Hi-Res silently fell back.

OrpheusDL keeps its OWN self-refreshing session (orpheus/config/loginstorage.bin)
and understands DASH, so it delivers true 24-bit Hi-Res FLAC and is the path to
Atmos (AC-4). The session is created once via:
    * TV login   — link.tidal.com device code (needs a browser once), OR
    * Mobile     — username + password (no browser; Tidal may block it, TV is
                   the reliable fallback)
Both are driven from Settings → Tidal; after that the session refreshes itself.

Download path: OrpheusDL CLI (`orpheus.py <tidal.com/browse/.../ID>`), cwd =
orpheus/. Quality is taken from orpheus settings.json (global.general).

Search / album / artist metadata still use Tidal's public API with the pasted
``tidal-token`` (access) — that path is unchanged and independent of downloads.
"""
from __future__ import annotations

import json
import pickle
import re
import sys
import time
from pathlib import Path

from .base import EngineBase, EngineResult, Event, EventKind, LineLevel, _strip_ansi
from .errors import classify_download_error
from .registry import register
from ripster import http_client as _HTTP


# ── OrpheusDL paths ───────────────────────────────────────────────────────────
def _base_dir() -> Path:
    return Path(sys.argv[0]).resolve().parent if sys.argv else Path(".").resolve()

def _orpheus_dir() -> Path:
    return _base_dir() / "orpheus"

def _orpheus_python() -> str:
    """OrpheusDL runs in its OWN venv (tools/orpheusvenv) — its protobuf==3.15.8
    pin would break AMD/pywidevine in the shared bundled python. The venv (made
    via virtualenv) is also non-isolated, so `python orpheus.py` finds orpheus.core.
    Falls back to the current interpreter. See ripster-dependency-versions skill."""
    base = _base_dir()
    for sub in (("Scripts", "python.exe"), ("bin", "python")):
        cand = base / "tools" / "orpheusvenv" / sub[0] / sub[1]
        if cand.is_file():
            return str(cand)
    return sys.executable

def _settings_path() -> Path:
    return _orpheus_dir() / "config" / "settings.json"

def _module_path() -> Path:
    return _orpheus_dir() / "modules" / "tidal"

def _session_path() -> Path:
    # OrpheusDL persists its saved sessions (TV / Mobile) here. A non-trivial
    # file means at least one session was created and can be self-refreshed.
    return _orpheus_dir() / "config" / "loginstorage.bin"


# ── live access token from OrpheusDL's self-refreshing session ────────────────
# The pasted `tidal-token` dies in ~16 h and can't be refreshed (wrong
# account/client_id). OrpheusDL's TV refresh_token IS valid and long-lived, so
# search/metadata mint a fresh access_token from it (cached ~4 h) — the same
# self-healing path downloads use. Falls back to the pasted token if unavailable.
_AT_CACHE: dict = {"token": "", "exp": 0.0, "country": "", "user_id": ""}

def _read_tv_session() -> dict | None:
    """TV session dict (refresh_token/country/user_id) from the pickled
    loginstorage. Plain dicts + datetime only — no orpheus import needed."""
    try:
        blob = pickle.loads(_session_path().read_bytes())
        return blob["modules"]["tidal"]["sessions"]["default"]["custom_data"]["sessions"]["TV"]
    except Exception:
        return None

# OrpheusDL-tidal ships these public TV (Atmos) client creds as its module
# defaults (orpheus/modules/tidal/interface.py). Embed them as a fallback so a
# FRESH GitHub clone — which has no orpheus/config/settings.json yet (gitignored)
# — can still run the link.tidal.com device-flow login straight out of the box.
_TV_TOKEN_DEFAULT  = "cgiF7TQuB97BUIu3"
_TV_SECRET_DEFAULT = "1nqpgx8uvBdZigrx4hUPDV2hOwgYAAAG5DYXOr6uNf8="
# Mobile-Atmos client id (orpheus tidal module default) — its session is what
# actually delivers AC-4 Atmos. A refresh_token from ANY Tidal session works with
# any client id, so we derive this session from the TV login automatically.
_MOBILE_ATMOS_TOKEN_DEFAULT = "km8T1xS355y7dd3H"

def _tv_client() -> tuple[str, str]:
    try:
        st = json.loads(_settings_path().read_text(encoding="utf-8"))["modules"]["tidal"]
        tok = st.get("tv_atmos_token") or _TV_TOKEN_DEFAULT
        sec = st.get("tv_atmos_secret") or _TV_SECRET_DEFAULT
        return tok, sec
    except Exception:
        # No settings.json (fresh clone) → use the shipped module defaults.
        return _TV_TOKEN_DEFAULT, _TV_SECRET_DEFAULT

def _mobile_atmos_client() -> str:
    try:
        st = json.loads(_settings_path().read_text(encoding="utf-8"))["modules"]["tidal"]
        return st.get("mobile_atmos_hires_token") or _MOBILE_ATMOS_TOKEN_DEFAULT
    except Exception:
        return _MOBILE_ATMOS_TOKEN_DEFAULT

async def _orpheus_access_token() -> tuple[str, str]:
    """Return a FRESH (access_token, country) from the OrpheusDL TV session,
    cached ~4 h. ('', '') if no session / refresh fails."""
    now = time.time()
    if _AT_CACHE["token"] and now < _AT_CACHE["exp"]:
        return _AT_CACHE["token"], _AT_CACHE["country"]
    tv = _read_tv_session()
    cid, csec = _tv_client()
    if not (tv and tv.get("refresh_token") and cid):
        return "", ""
    try:
        async with _HTTP.ashared() as c:
            r = await c.post(
                "https://auth.tidal.com/v1/oauth2/token",
                data={"refresh_token": tv["refresh_token"], "client_id": cid,
                      "client_secret": csec, "grant_type": "refresh_token"},
            )
        if r.status_code != 200:
            return "", ""
        j = r.json()
        _AT_CACHE["token"]   = j["access_token"]
        _AT_CACHE["exp"]     = now + max(60, int(j.get("expires_in", 3600)) - 120)
        _AT_CACHE["country"] = (tv.get("country_code") or "").upper()
        _AT_CACHE["user_id"] = str(tv.get("user_id") or "")
        return _AT_CACHE["token"], _AT_CACHE["country"]
    except Exception:
        return "", ""

async def _tidal_token_country(config: dict) -> tuple[str, str]:
    """Prefer the fresh OrpheusDL-session token; fall back to the pasted one."""
    tok, cc = await _orpheus_access_token()
    if tok:
        return tok, (cc or (config.get("tidal-country") or "US").strip().upper() or "US")
    return ((config.get("tidal-token") or "").strip(),
            (config.get("tidal-country") or "US").strip().upper() or "US")


# ── patterns ──────────────────────────────────────────────────────────────────
_RE_DOWNLOADING = re.compile(r'===\s*Downloading\s+(track|album|playlist|artist)\s+(.+?)\s*(?:\(|===)', re.I)
_RE_TRACK_FILE  = re.compile(r'Downloading track file|Saving\s*:', re.I)
_RE_TRACK_DONE  = re.compile(r'===\s*Track\s+\S+\s+downloaded|===\s*Done', re.I)
_RE_ERROR       = re.compile(r'\berror\b|\bfailed\b|\bexception\b|\bTraceback|Unsupported URL', re.I)
_RE_SKIP        = re.compile(r'skip|already exist|ignore', re.I)
_RE_PROGRESS    = re.compile(r'\bTrack\s+(\d+)\s*/\s*(\d+)', re.I)
# OrpheusDL streams a tqdm bar for the DASH segment download, e.g.
# " 33%|###2      | 21/64 [00:13<02:28, 3.45s/it]". Parse the percent so the
# progress bar actually moves per track instead of sitting at 0 (which read as
# "not downloading"). Track-level "=== Downloading track" bumps the counter.
_RE_PERCENT     = re.compile(r'(\d{1,3})%\|')
# Session/auth problems. EOFError appears when OrpheusDL hits an interactive
# login prompt (no/invalid session) with no stdin — we guard against that in
# build_cmd, but classify it too so the user gets a clear message.
_RE_AUTH_FAIL   = re.compile(
    r'TIDAL_NOT_AUTHED|no saved session|relogin|invalid.*session|unauthorized|'
    r'\b401\b|\b403\b|EOFError|Choose a login method',
    re.I,
)


# ── URL normalisation ─────────────────────────────────────────────────────────
# OrpheusDL's tidal module only accepts `tidal.com/(browse/)?<type>/<id>` — NOT
# `listen.tidal.com/...`. Rewrite whatever the user/search produced.
_RE_TIDAL_URL = re.compile(
    r'tidal\.com/(?:browse/)?(track|album|playlist|artist|mix|video)/([0-9a-fA-F-]+)',
    re.I,
)

def _to_orpheus_url(url: str) -> str:
    m = _RE_TIDAL_URL.search(url or "")
    if m:
        return f"https://tidal.com/browse/{m.group(1).lower()}/{m.group(2)}"
    return url


# ── quality mapping (Ripster id → OrpheusDL global.general.download_quality) ──
# OrpheusDL tidal: LOW=96 AAC, HIGH=320 AAC, LOSSLESS=16/44.1 FLAC,
# HI_RES (hifi)=<=24/48 FLAC + MQA.
# NB: the keys must cover EVERY quality code the front-ends emit, or an unknown
# code silently falls back to "lossless" (FLAC) while the card shows the picked
# label → "asked for AAC 320, got FLAC" mismatch. The guest picker (app.js) and
# player (player.js) send "hires" (no underscore) and "mp3" for AAC-320, so those
# aliases are mapped explicitly below alongside the canonical ids.
_QUALITY_ORPHEUS = {
    "hi_res":   "hifi",
    "hires":    "hifi",   # app.js/player.js emit "hires" without the underscore
    "atmos":    "hifi",   # Dolby Atmos rides the top FLAC tier + prefer_ac4 (AC-4)
    "mqa":      "hifi",
    "master":   "hifi",
    "hifi":     "hifi",
    "lossless": "lossless",
    "flac":     "lossless",
    "high":     "high",   # Tidal HIGH = AAC 320
    "320":      "high",   # raw "320" code from some pickers
    "aac":      "high",
    "mp3":      "high",   # player.js labels AAC-320 as "High" with code "mp3"
    "medium":   "high",
    "low":      "low",
    "minimum":  "low",
}


def _tidal_cover(uuid: str, size: int = 160) -> str:
    # Default 160px — small covers for search/release cards (less traffic, faster
    # load). Tidal CDN serves fixed sizes (80/160/320/640/1280). The downloader
    # fetches full-size art separately, so this only affects card display.
    if not uuid:
        return ""
    return f"https://resources.tidal.com/images/{uuid.replace('-', '/')}/{size}x{size}.jpg"


def is_installed() -> bool:
    return (_orpheus_dir() / "orpheus.py").exists() and (_module_path() / "interface.py").exists()


def is_authenticated(config: dict | None = None) -> bool:
    p = _session_path()
    try:
        return p.exists() and p.stat().st_size > 10
    except OSError:
        return False


def _update_orpheus_settings(quality: str, save_path: str, config: dict, atmos: bool = False) -> None:
    """Point OrpheusDL at the right quality + save folder (mirrors beatport)."""
    sp = _settings_path()
    if not sp.exists():
        return
    try:
        cfg = json.loads(sp.read_text(encoding="utf-8"))
        gen = cfg.setdefault("global", {}).setdefault("general", {})
        if quality:
            gen["download_quality"] = quality
        if save_path:
            gen["download_path"] = save_path.rstrip("/\\") + "\\"

        covers = cfg["global"].setdefault("covers", {})
        covers["embed_cover"]         = True
        # Embedded (in-audio) cover is pinned to 1000×1000 across ALL services
        # by request — uniform tag artwork. Sources natively smaller (e.g.
        # SoundCloud 500) just deliver their max. The EXTERNAL cover file stays
        # high-res (1400) so the on-disk original is unaffected.
        covers["main_resolution"]     = 1000
        covers["save_external"]       = True
        covers["external_format"]     = "jpg"
        covers["external_resolution"] = 1400

        # Atmos via AC-4 needs prefer_ac4; only request it on the top tier so
        # normal FLAC downloads stay stereo. (Mobile-Atmos session required for
        # actual AC-4 delivery — harmless when only TV session exists.)
        tm = cfg.setdefault("modules", {}).setdefault("tidal", {})
        tm["prefer_ac4"] = bool(atmos)

        sp.write_text(json.dumps(cfg, indent=4, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


@register
class TidalEngine(EngineBase):
    name = "tidal"

    _QUALITIES = [
        {"id": "hi_res",   "label": "MQA / Hi-Res", "sub": "Up to 24/192 MQA",  "badge": "HI-RES",   "color": "#ffd60a", "bitrate": "3000+ kbps", "ext": "flac", "req": "premium", "flag": "-q 3"},
        {"id": "atmos",    "label": "Dolby Atmos",  "sub": "AC-4 spatial (где доступно)", "badge": "ATMOS", "color": "#9090c8", "bitrate": "~768 kbps", "ext": "m4a", "req": "premium", "flag": ""},
        {"id": "lossless", "label": "FLAC",         "sub": "16-bit / 44.1 kHz", "badge": "LOSSLESS", "color": "#3ecfaa", "bitrate": "1411 kbps",  "ext": "flac", "req": "premium", "flag": "-q 2"},
        {"id": "high",     "label": "AAC 320",      "sub": "Lossy high",        "badge": "LOSSY",    "color": "#EF9F27", "bitrate": "320 kbps",   "ext": "m4a",  "req": "free",    "flag": "-q 1"},
        {"id": "low",      "label": "AAC 96",       "sub": "Lossy low",         "badge": "LOSSY",    "color": "#EF9F27", "bitrate": "96 kbps",    "ext": "m4a",  "req": "free",    "flag": "-q 0"},
    ]

    def qualities(self) -> list[dict]:
        return [{**q, "engine": self.name} for q in self._QUALITIES]

    def working_dir(self) -> str | None:
        # OrpheusDL resolves its modules relative to CWD — must run from orpheus/.
        return str(_orpheus_dir())

    def build_cmd(self, url: str, quality: str, config: dict) -> list[str]:
        if not (_orpheus_dir() / "orpheus.py").exists():
            raise ValueError("OrpheusDL не установлен — см. Settings → Tidal")
        if not (_module_path() / "interface.py").exists():
            raise ValueError(
                "Модуль Tidal не установлен. Клонируй orpheusdl-tidal в "
                "orpheus/modules/tidal/"
            )
        if not is_authenticated(config):
            # Never let OrpheusDL reach its interactive input() prompt headless —
            # it would hang (or EOFError). Force a clear, actionable error.
            raise ValueError(
                "TIDAL_NOT_AUTHED: войди в Tidal в Settings → Tidal "
                "(TV-логин через link.tidal.com или mobile логин/пароль)"
            )

        save_path = config.get("tidal-save-path") or config.get("save-path") or ""
        orpheus_quality = _QUALITY_ORPHEUS.get(quality, "lossless")
        # Atmos (AC-4) when the user explicitly picks the atmos quality, OR on
        # hi_res with the global tidal-atmos flag (legacy behaviour preserved).
        atmos = (quality == "atmos") or (orpheus_quality == "hifi" and bool(config.get("tidal-atmos")))
        _update_orpheus_settings(orpheus_quality, save_path, config, atmos=atmos)

        # Run under the isolated OrpheusDL venv. Bootstrap via -c so orpheus.core
        # imports even on an isolated interpreter (matches spotify/beatport engines).
        orph_dir   = str(_orpheus_dir())
        orpheus_py = str(_orpheus_dir() / "orpheus.py")
        _boot = (
            "import sys, runpy; "
            f"sys.path.insert(0, {orph_dir!r}); "
            f"sys.argv = [{orpheus_py!r}] + sys.argv[1:]; "
            f"runpy.run_path({orpheus_py!r}, run_name='__main__')"
        )
        cmd = [_orpheus_python(), "-c", _boot]
        if save_path:
            cmd += ["-o", save_path.rstrip("/\\")]
        cmd.append(_to_orpheus_url(url))
        return cmd

    def iter_events(self, line: str, *, progress: tuple[int, int]):
        clean = _strip_ansi(line).strip()
        if not clean:
            return
        if "TIDAL_NOT_AUTHED" in clean or _RE_AUTH_FAIL.search(clean):
            yield Event(
                kind=EventKind.FATAL,
                message="Tidal: сессия недействительна — переавторизуйся в Settings → Tidal",
                level=LineLevel.ERROR,
            )
            return
        yield from super().iter_events(clean, progress=progress)

    def classify_line(self, line: str) -> str:
        if _RE_ERROR.search(line):     return "error"
        if _RE_AUTH_FAIL.search(line): return "error"
        if _RE_SKIP.search(line):      return "warn"
        if _RE_DOWNLOADING.search(line) or _RE_TRACK_FILE.search(line):
            return "success"
        return "stdout"

    def parse_progress(self, line: str, current: int, total: int) -> tuple[int, int]:
        m = _RE_PROGRESS.search(line)
        if m:
            return max(0, int(m.group(1)) - 1), int(m.group(2))
        mp = _RE_PERCENT.search(line)
        if mp:
            return int(mp.group(1)), 100
        return current, total

    def is_finished(self, log_text: str, rc: int = -1) -> EngineResult:
        if "TIDAL_NOT_AUTHED" in log_text:
            return EngineResult(False, error="TIDAL_NOT_AUTHED: нужна авторизация Tidal (Settings → Tidal)")

        # Count REAL completions only. OrpheusDL prints "Downloading track file"
        # at the START of a track (music_downloader.py:389) and
        # "=== Track <id> downloaded ===" only AFTER it fully lands (line 633).
        # Counting the start marker caused FALSE SUCCESS: a DASH download that
        # dropped mid-way (slow/cut VPN) reported "done" with an empty folder.
        done = len(re.findall(r'===\s*Track\s+\S+\s+downloaded\s*===', log_text, re.I))
        if done > 0:
            errs = len(re.findall(r'\berror\b|\bfailed\b|\bexception\b|Traceback', log_text, re.I))
            return EngineResult(success=True, tracks_ok=done, tracks_err=errs)

        if rc == 0 and _RE_SKIP.search(log_text):
            return EngineResult(success=True, tracks_ok=0)

        if _RE_AUTH_FAIL.search(log_text):
            return EngineResult(
                success=False,
                error="Tidal: сессия истекла/недействительна — переавторизуйся в Settings → Tidal.",
            )

        # Region-lock / phantom-removed link (e.g. OrpheusDL: "Album [X] not found.
        # This might be region-locked.") — surface the REAL cause via the shared
        # classifier, not the misleading "track didn't download (DASH/network)".
        _cls = classify_download_error(log_text)
        if _cls:
            return EngineResult(False, error=f"Tidal: {_cls[1]}")

        if not log_text.strip():
            return EngineResult(False, error="Tidal/OrpheusDL: нет вывода — проверь сессию Tidal")

        # No completion marker → the track did not finish. Return error so the
        # runner's salvage-from-disk can still deliver any real files that landed;
        # if none did, the user gets an honest "retry" instead of a phantom done.
        return EngineResult(
            success=False,
            error="Tidal: трек не докачался (DASH/сеть прервалась) — повтори; при медленном VPN смени нод",
        )

    @staticmethod
    def _avail_from_item(item: dict) -> list[str]:
        """Real per-release quality badges from Tidal's own fields — quality genuinely
        VARIES per album (many are lossy-only, some Hi-Res, some Atmos), so we read the
        item's audioQuality / audioModes / mediaMetadata.tags rather than assume a max."""
        tags = set((item.get("mediaMetadata") or {}).get("tags") or [])
        modes = set(item.get("audioModes") or [])
        q = (item.get("audioQuality") or "").upper()
        out: list[str] = []
        if "DOLBY_ATMOS" in modes or "DOLBY_ATMOS" in tags:
            out.append("ATMOS")
        if "HIRES_LOSSLESS" in tags or q in ("HI_RES", "HI_RES_LOSSLESS"):
            out.append("HI-RES")
        if "LOSSLESS" in tags or q == "LOSSLESS" or "HI-RES" in out:
            out.append("FLAC")
        if q in ("HIGH", "LOW") and not out:
            out.append("AAC")
        # De-dupe keep order; fall back to Tidal's lossless baseline if fields absent.
        seen, uniq = set(), []
        for t in out:
            if t not in seen:
                seen.add(t); uniq.append(t)
        return uniq or ["FLAC"]

    async def search(self, query: str, search_type: str, limit: int, config: dict) -> list[dict]:
        import httpx as _httpx
        token, country = await _tidal_token_country(config)
        if not token:
            return []
        headers  = {"Authorization": f"Bearer {token}"}
        type_map = {"album": "albums", "track": "tracks", "artist": "artists"}
        t_type   = type_map.get(search_type, "albums")
        try:
            async with _HTTP.ashared() as c:
                r = await c.get(
                    f"https://api.tidal.com/v1/search/{t_type}",
                    headers=headers,
                    params={"query": query, "limit": limit, "countryCode": country},
                )
                if r.status_code == 401:
                    return []
                data = r.json()
            results = []
            for item in data.get("items") or []:
                if t_type == "albums":
                    alb_id = str(item.get("id", ""))
                    results.append({
                        "id":      alb_id,
                        "title":   item.get("title", ""),
                        "artist":  (item.get("artist") or {}).get("name", ""),
                        "artist_id": str((item.get("artist") or {}).get("id", "")),
                        "type":    search_type,
                        "url":     f"https://listen.tidal.com/album/{alb_id}",
                        "cover":   _tidal_cover(item.get("cover", "")),
                        "year":    (item.get("releaseDate") or "")[:4],
                        "tracks":  item.get("numberOfTracks"),
                        "available": self._avail_from_item(item),
                        "service": "tidal",
                    })
                elif t_type == "tracks":
                    tr_id = str(item.get("id", ""))
                    results.append({
                        "id":      tr_id,
                        "title":   item.get("title", ""),
                        "artist":  (item.get("artist") or {}).get("name", ""),
                        "artist_id": str((item.get("artist") or {}).get("id", "")),
                        "type":    search_type,
                        "url":     f"https://listen.tidal.com/track/{tr_id}",
                        "cover":   _tidal_cover((item.get("album") or {}).get("cover", "")),
                        "available": self._avail_from_item(item),
                        "service": "tidal",
                    })
                elif t_type == "artists":
                    art_id = str(item.get("id", ""))
                    results.append({
                        "id":      art_id,
                        "title":   item.get("name", ""),
                        "artist":  item.get("name", ""),
                        "type":    search_type,
                        "url":     f"https://listen.tidal.com/artist/{art_id}",
                        "cover":   _tidal_cover(item.get("picture", "")),
                        "service": "tidal",
                    })
            return results
        except Exception:
            return []

    async def get_artist(self, artist_id: str, types: str, config: dict) -> dict:
        import httpx as _httpx
        token, country = await _tidal_token_country(config)
        if not token:
            return {"error": "Tidal access_token не настроен", "releases": []}
        headers = {"Authorization": f"Bearer {token}"}
        wanted  = {t.strip() for t in types.split(",") if t.strip()} if types else set()
        try:
            async with _HTTP.ashared() as c:
                info_r = await c.get(f"https://api.tidal.com/v1/artists/{artist_id}",
                                     headers=headers, params={"countryCode": country})
                if info_r.status_code == 401:
                    return {"error": "Tidal: токен истёк. Обнови access_token в Settings → Tidal.", "releases": []}
                info = info_r.json()
                albums_r = await c.get(f"https://api.tidal.com/v1/artists/{artist_id}/albums",
                                       headers=headers,
                                       params={"countryCode": country, "limit": 100, "offset": 0})
                albums_data = albums_r.json()
            releases = []
            for alb in albums_data.get("items") or []:
                alb_id   = str(alb.get("id", ""))
                alb_type = alb.get("type", "ALBUM").lower()
                type_norm = {"album": "album", "ep": "single", "single": "single",
                             "compilation": "compilation"}.get(alb_type, "album")
                if wanted and type_norm not in wanted:
                    continue
                releases.append({
                    "id":      alb_id,
                    "title":   alb.get("title", ""),
                    "artist":  info.get("name", ""),
                    "type":    type_norm,
                    "url":     f"https://listen.tidal.com/album/{alb_id}",
                    "cover":   _tidal_cover(alb.get("cover", "")),
                    "year":    (alb.get("releaseDate") or "")[:4],
                    "date":    alb.get("releaseDate", ""),
                    "tracks":  alb.get("numberOfTracks"),
                    "service": "tidal",
                })
            return {
                "artist": {
                    "id":      str(info.get("id", artist_id)),
                    "name":    info.get("name", ""),
                    "cover":   _tidal_cover(info.get("picture", "")),
                    "url":     f"https://listen.tidal.com/artist/{artist_id}",
                    "service": "tidal",
                },
                "releases": releases,
            }
        except Exception as e:
            return {"error": str(e), "releases": []}

    async def get_album(self, album_id: str, config: dict) -> dict:
        import httpx as _httpx
        token, country = await _tidal_token_country(config)
        if not token:
            return {"error": "Tidal access_token не настроен"}
        headers = {"Authorization": f"Bearer {token}"}
        try:
            async with _HTTP.ashared() as c:
                alb_r = await c.get(f"https://api.tidal.com/v1/albums/{album_id}",
                                    headers=headers, params={"countryCode": country})
                if alb_r.status_code == 401:
                    return {"error": "Tidal: токен истёк. Обнови access_token в Settings → Tidal."}
                a = alb_r.json()
                tr_r = await c.get(f"https://api.tidal.com/v1/albums/{album_id}/tracks",
                                   headers=headers,
                                   params={"countryCode": country, "limit": 100})
                tr_data = tr_r.json()
            tracks = []
            for t in tr_data.get("items") or []:
                tr_id = str(t.get("id", ""))
                tracks.append({
                    "id":       tr_id,
                    "track_no": t.get("trackNumber"),
                    "disc":     t.get("volumeNumber"),   # multi-disc support
                    "title":    t.get("title", ""),
                    "artist":   (t.get("artist") or {}).get("name", ""),
                    "duration": t.get("duration", 0),
                    "preview":  "",
                    "explicit": t.get("explicit", False),
                    "url":      f"https://listen.tidal.com/track/{tr_id}",
                })
            real_id = str(a.get("id", album_id))
            return {
                "album": {
                    "id":      real_id,
                    "title":   a.get("title", ""),
                    "artist":  (a.get("artist") or {}).get("name", ""),
                    "cover":   _tidal_cover(a.get("cover", ""), 640),
                    "year":    (a.get("releaseDate") or "")[:4],
                    "date":    a.get("releaseDate", ""),
                    "label":   a.get("label", ""),
                    "upc":     a.get("upc", ""),
                    "genre":   a.get("genre", ""),
                    "tracks":  a.get("numberOfTracks"),
                    "url":     f"https://listen.tidal.com/album/{real_id}",
                    "service": "tidal",
                },
                "tracks": tracks,
            }
        except Exception as e:
            return {"error": str(e)}
