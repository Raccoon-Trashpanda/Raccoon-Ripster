"""
Spotify OAuth + releases routes.

Includes:
  /spotify/login          — redirect to Spotify authorization page
  /spotify/callback       — exchange code for tokens
  /api/spotify/status     — check connection status
  /api/spotify/releases   — new releases from followed artists
  /api/spotify/logout     — revoke local token

Also: /api/convert/spotify — search-based Spotify→target conversion
  (kept here because it uses _search helpers from discovery.py)

Install: spotify.install(app, cfg, broadcast_fn, base_dir)
"""
from __future__ import annotations

import base64
import json
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

# Search helpers from discovery — imported lazily to avoid circular issues at
# module load time (discovery.install() must run before any convert call).
from ripster.routes import discovery as _disc

router  = APIRouter()
_cfg: dict       = {}
_broadcast       = None
_token_file: Path = None

REDIRECT_URI = "http://127.0.0.1:7799/spotify/callback"
SCOPES       = "user-follow-read user-library-read"

_sp_token: dict = {}
_sp_403_error: str = ""          # set when Spotify returns 403 "not registered"
_sp_releases_cache: dict = {}    # cache_key -> {releases, artists_checked, market, ts}
_SP_RELEASES_TTL = 3600          # server-side cache TTL: 1 hour
_sp_scan_running: bool = False   # background scan in progress
_sp_scan_started: float = 0.0    # timestamp when scan started (for stuck-scan detection)
_SP_SCAN_TIMEOUT = 7200          # paced crawl of a big follow list (5000+ artists
                                 # @ ~0.8s each) legitimately takes 60-90 min; a lower
                                 # value would spawn a 2nd concurrent crawl mid-flight
_sp_last_error: str = ""         # last scan failure surfaced via GET (auth/cookie expired etc)
_sp_last_done_ts: float = 0.0    # when the last scan finished (success or failure)
_sp_cache_file: Path = None      # disk cache path

# ── Persistent per-artist crawl state (the "like a release-radar site" core) ──
# Instead of bursting N artist/albums requests on every scan (which gets the dev
# Client-ID rate-limit-banned), we keep a durable per-artist store and crawl it
# slowly, one paced request at a time, honouring any active 429 ban. Partial
# crawls accumulate; repeat scans only re-check stale artists, so steady-state
# cost is near-zero and we stay off Spotify's rate-limit radar.
_sp_state_file: Path = None              # spotify_artist_state.json
_sp_artist_state: dict = {}              # artist_id -> {name, releases:[...], ts}
_sp_followed_cache: dict = {"artists": [], "market": "", "ts": 0.0}  # followed list cache
_sp_banned_until: float = 0.0            # epoch until which the dev API 429-bans us
_sp_crawler_started: bool = False        # background crawler loop guard
_SP_FOLLOWED_TTL = 6 * 3600              # refresh the followed-artist list every 6h
_SP_CRAWL_EVERY  = 1800                  # background crawler wakes every 30 min
_SP_ARTIST_REFRESH = 6 * 3600            # re-check a given artist at most every 6h
_SP_STATE_WINDOW_DAYS = 400              # how much release history to keep per artist
_SP_CRAWL_INTERVAL = 0.8                 # min seconds between album requests (paced)
_SP_BAN_CAP = 6 * 3600                   # never trust a Retry-After longer than this

# Web-player (sp_dc) token — avoids developer-API rate limits
_sp_dc_web: dict = {}
_SP_WEB_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


_save_config_fn = None   # set by install()


def install(app, ctx) -> None:
    global _cfg, _broadcast, _token_file, _sp_cache_file, _save_config_fn, _sp_state_file
    _cfg             = ctx.config
    _broadcast       = ctx.broadcast
    _token_file      = ctx.base_dir / "spotify_token.json"
    _sp_cache_file   = ctx.base_dir / "spotify_releases_cache.json"
    _sp_state_file   = ctx.base_dir / "spotify_artist_state.json"
    _save_config_fn  = ctx.save_config
    _load_disk_cache()
    _load_artist_state()
    app.include_router(router)


def _load_artist_state() -> None:
    global _sp_artist_state, _sp_banned_until, _sp_followed_cache
    if _sp_state_file and _sp_state_file.exists():
        try:
            d = json.loads(_sp_state_file.read_text(encoding="utf-8"))
            _sp_artist_state = d.get("artists", {}) or {}
            _sp_banned_until = float(d.get("banned_until", 0.0) or 0.0)
            fc = d.get("followed")
            if isinstance(fc, dict):
                _sp_followed_cache = fc
            ban_left = int(_sp_banned_until - datetime.now().timestamp())
            print(f"[spotify] artist-state: {len(_sp_artist_state)} artists, "
                  f"{len(_sp_followed_cache.get('artists', []))} followed"
                  + (f", rate-limit ban {ban_left}s left" if ban_left > 0 else ""),
                  flush=True)
            # One-time: stores crawled before compilations got their own group
            # call have none. Reset freshness so the next paced crawl refetches
            # and fills compilations (Сборники) for every artist.
            if _sp_artist_state and not d.get("comp_v2"):
                has_comp = any(r.get("group") == "compilation"
                               for st in _sp_artist_state.values()
                               for r in st.get("releases", []))
                if not has_comp:
                    for st in _sp_artist_state.values():
                        st["ts"] = 0
                    print("[spotify] store has 0 compilations → marked all artists "
                          "stale; next crawl will fill Сборники", flush=True)
                _save_artist_state()   # persists comp_v2 so this runs only once
        except Exception as e:
            print(f"[spotify] artist-state load error: {e}", flush=True)
    if not _sp_artist_state:
        _seed_state_from_old_cache()


def _seed_state_from_old_cache() -> None:
    """One-time migration: rebuild the per-artist store from the legacy flat
    releases cache so the feed is populated immediately (no empty UI while the
    new crawler slowly fills the store / waits out a rate-limit ban)."""
    global _sp_artist_state, _sp_followed_cache
    if _sp_artist_state or not _sp_releases_cache:
        return
    best = max(_sp_releases_cache.values(),
               key=lambda v: len(v.get("releases", [])), default=None)
    if not best or not best.get("releases"):
        return
    by_artist: dict = {}
    names: dict = {}
    for rel in best["releases"]:
        aid = rel.get("artist_id")
        if not aid:
            continue
        rel.setdefault("group", rel.get("type", "album"))
        by_artist.setdefault(aid, []).append(rel)
        names[aid] = rel.get("artist", "")
    # ts=0 → every seeded artist counts as stale, so the crawler refreshes them
    for aid, rels in by_artist.items():
        _sp_artist_state[aid] = {"name": names.get(aid, ""), "releases": rels, "ts": 0}
    _sp_followed_cache = {
        "artists": [{"id": a, "name": n} for a, n in names.items()],
        "market":  best.get("market", ""),
        "ts":      0,   # force a /me/following refresh on the first crawl pass
    }
    print(f"[spotify] seeded store from legacy cache: {len(by_artist)} artists, "
          f"{len(best['releases'])} releases", flush=True)
    _save_artist_state()


def _save_artist_state() -> None:
    if _sp_state_file:
        try:
            _sp_state_file.write_text(json.dumps({
                "artists":      _sp_artist_state,
                "followed":     _sp_followed_cache,
                "banned_until": _sp_banned_until,
                "comp_v2":      True,
            }), encoding="utf-8")
        except Exception as e:
            print(f"[spotify] artist-state save error: {e}", flush=True)


def _build_feed(days: int, types: str) -> dict:
    """Pure, network-free: build the releases feed from the durable per-artist
    store, filtered to the currently-followed artists + day window + types."""
    followed = {a.get("id") for a in _sp_followed_cache.get("artists", []) if a.get("id")}
    want_types   = {x.strip() for x in types.split(",") if x.strip()}
    want_main    = want_types - {"appears_on"}
    want_appears = "appears_on" in want_types
    cutoff = "" if days >= 3650 else (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    seen: set = set()
    releases: list = []
    for aid in followed:
        st = _sp_artist_state.get(aid)
        if not st:
            continue
        for rel in st.get("releases", []):
            rid = rel.get("id")
            if not rid or rid in seen:
                continue
            if rel.get("group") == "appears_on":
                if not want_appears:
                    continue
            elif want_main and rel.get("type", "album") not in want_main:
                continue
            if cutoff and rel.get("date", "") < cutoff:
                continue
            seen.add(rid)
            releases.append(rel)
    releases.sort(key=lambda x: x.get("date", ""), reverse=True)
    checked  = sum(1 for a in followed if a in _sp_artist_state)
    ban_left = int(_sp_banned_until - datetime.now().timestamp())
    return {
        "releases":        releases,
        "artists_checked": checked,
        "followed":        len(followed),
        "market":          _sp_followed_cache.get("market", ""),
        "ban_left":        max(0, ban_left),
    }


def _load_disk_cache() -> None:
    global _sp_releases_cache
    if _sp_cache_file and _sp_cache_file.exists():
        try:
            _sp_releases_cache = json.loads(_sp_cache_file.read_text(encoding="utf-8"))
            print(f"[spotify] loaded disk cache: {sum(len(v.get('releases',[])) for v in _sp_releases_cache.values())} releases", flush=True)
        except Exception as e:
            print(f"[spotify] disk cache load error: {e}", flush=True)


def _save_disk_cache() -> None:
    if _sp_cache_file:
        try:
            _sp_cache_file.write_text(json.dumps(_sp_releases_cache), encoding="utf-8")
        except Exception as e:
            print(f"[spotify] disk cache save error: {e}", flush=True)


# ── Token helpers ─────────────────────────────────────────────────────────

def _sp_httpx_kwargs() -> dict:
    """Route Spotify traffic through an optional proxy (config `spotify-proxy`).
    Spotify's dev API geo-blocks unsupported countries (403 "unavailable in this
    country") and rate-limits hard; a proxy in a supported region is what lets the
    crawler actually fetch — same as running the release-radar from a foreign host."""
    px = (_cfg.get("spotify-proxy") or "").strip()
    return {"proxy": px} if px else {}


def _load_sp() -> dict:
    global _sp_token
    if _sp_token:
        return _sp_token
    if _token_file and _token_file.exists():
        try:
            _sp_token = json.loads(_token_file.read_text())
            return _sp_token
        except Exception as e:
            print(f"[spotify] token file unreadable ({_token_file}): {e}",
                  flush=True)
    return {}


def _save_sp(t: dict) -> None:
    global _sp_token
    _sp_token = t
    if _token_file:
        _token_file.write_text(json.dumps(t, indent=2))


async def _sp_refresh(t: dict) -> dict | None:
    cid = _cfg.get("spotify-client-id", "").strip()
    cs  = _cfg.get("spotify-client-secret", "").strip()
    if not (cid and cs and t.get("refresh_token")):
        return None
    creds = base64.b64encode(f"{cid}:{cs}".encode()).decode()
    try:
        async with httpx.AsyncClient(timeout=10, **_sp_httpx_kwargs()) as c:
            r = await c.post(
                "https://accounts.spotify.com/api/token",
                data={"grant_type": "refresh_token", "refresh_token": t["refresh_token"]},
                headers={"Authorization": f"Basic {creds}",
                         "Content-Type": "application/x-www-form-urlencoded"},
            )
            if r.status_code == 200:
                new = r.json()
                t["access_token"] = new["access_token"]
                if "refresh_token" in new:
                    t["refresh_token"] = new["refresh_token"]
                t["expires_at"] = datetime.now().timestamp() + new.get("expires_in", 3600)
                _save_sp(t)
                return t
    except Exception as e:
        print(f"[spotify] refresh: {e}", flush=True)
    return None


async def _sp_get(path: str):
    global _sp_403_error
    t = _load_sp()
    if not t.get("access_token"):
        return None
    if t.get("expires_at", 0) < datetime.now().timestamp() + 60:
        t = await _sp_refresh(t) or t
    hdr = {"Authorization": f"Bearer {t['access_token']}"}
    try:
        async with httpx.AsyncClient(timeout=12, **_sp_httpx_kwargs()) as c:
            r = await c.get(f"https://api.spotify.com/v1{path}", headers=hdr)
            if r.status_code == 401:
                t = await _sp_refresh(t) or {}
                if t.get("access_token"):
                    r = await c.get(
                        f"https://api.spotify.com/v1{path}",
                        headers={"Authorization": f"Bearer {t['access_token']}"},
                    )
            if r.status_code == 403:
                msg = r.json().get("error", {}).get("message", "") if r.headers.get("content-type","").startswith("application/json") else r.text[:200]
                _sp_403_error = msg or "User not registered for this Spotify app"
                print(f"[spotify] 403 {path}: {_sp_403_error}", flush=True)
                return None
            _sp_403_error = ""  # clear on success
            if r.status_code == 200:
                return r.json()
    except Exception as e:
        print(f"[spotify] GET {path}: {e}", flush=True)
    return None


async def get_access_token() -> str | None:
    """Return a valid access token (refreshing if near-expiry). Used by metadata module."""
    t = _load_sp()
    if not t.get("access_token"):
        return None
    if t.get("expires_at", 0) < datetime.now().timestamp() + 60:
        t = await _sp_refresh(t) or t
    return t.get("access_token") or None


async def _sp_dc_get_token() -> str | None:
    """Fetch a web-player access token from the sp_dc cookie.

    The web-player client is not rate-limited like the developer Web API, so
    using it for the releases scan avoids 429 / temporary bans.
    Returns None if sp_dc is not configured or the fetch fails.
    """
    global _sp_dc_web
    sp_dc = _cfg.get("spotify-sp-dc", "").strip()
    if not sp_dc:
        return None
    now = datetime.now().timestamp()
    if _sp_dc_web.get("access_token") and _sp_dc_web.get("expiry", 0) > now + 300:
        return _sp_dc_web["access_token"]
    try:
        async with httpx.AsyncClient(timeout=10, headers={
            "User-Agent": _SP_WEB_UA,
            "Cookie": f"sp_dc={sp_dc}",
            "Accept": "application/json",
        }, **_sp_httpx_kwargs()) as c:
            r = await c.get(
                "https://open.spotify.com/get_access_token",
                params={"reason": "transport", "productType": "web_player"},
            )
            if r.status_code == 200:
                data = r.json()
                if not data.get("isAnonymous", True):
                    expiry_ms = data.get("accessTokenExpirationTimestampMs", 0)
                    _sp_dc_web = {
                        "access_token": data["accessToken"],
                        "expiry": expiry_ms / 1000,
                    }
                    return data["accessToken"]
                print("[spotify] sp_dc: anonymous token returned — cookie expired", flush=True)
            else:
                print(f"[spotify] sp_dc token fetch: HTTP {r.status_code} — cookie expired or blocked", flush=True)
    except Exception as e:
        print(f"[spotify] sp_dc token fetch error: {e}", flush=True)
    return None


# ── OAuth routes ──────────────────────────────────────────────────────────

@router.get("/spotify/login")
async def sp_login(request: Request):
    cid = _cfg.get("spotify-client-id", "").strip()
    if not cid:
        return HTMLResponse(
            """<html><body style="font-family:sans-serif;padding:40px;background:#0a0a0c;color:#f0f0f4">
            <h3 style="color:#fc3c44">Client ID не настроен</h3>
            <p style="color:#888">Перейди в <b>Settings → Spotify</b> и вставь Client ID и Client Secret.</p>
            <p style="color:#888">Затем нажми «Подключить» снова.</p>
            <script>setTimeout(()=>window.close(),4000)</script></body></html>"""
        )

    host = request.headers.get("host", "")
    if host.lower().startswith("localhost"):
        return HTMLResponse(
            """<html><body style="font-family:sans-serif;padding:40px;background:#0a0a0c;color:#f0f0f4;max-width:620px;margin:0 auto;line-height:1.6">
            <h3 style="color:#fc3c44">⚠ Откроется ошибка Spotify</h3>
            <p>С апреля 2025 Spotify больше не принимает <code style="background:#222;padding:2px 6px;border-radius:4px">http://localhost</code> как redirect URI — только <code style="background:#222;padding:2px 6px;border-radius:4px">http://127.0.0.1</code>.</p>
            <p>Ты сейчас открыл Ripster через <code style="background:#222;padding:2px 6px;border-radius:4px">http://localhost:7799</code>. Даже если авторизация в Spotify пройдёт, callback придёт на localhost и обмен токена провалится.</p>
            <p><b>Как исправить:</b></p>
            <ol>
              <li>Открой Ripster заново по адресу <a href="http://127.0.0.1:7799" style="color:#1db954">http://127.0.0.1:7799</a></li>
              <li>Проверь, что в Spotify Dashboard → Your App → Settings → Redirect URIs указан <code style="background:#222;padding:2px 6px;border-radius:4px">http://127.0.0.1:7799/spotify/callback</code></li>
              <li>Нажми «Подключить» ещё раз</li>
            </ol>
            </body></html>"""
        )

    params = urllib.parse.urlencode({
        "client_id":     cid,
        "response_type": "code",
        "redirect_uri":  REDIRECT_URI,
        "scope":         SCOPES,
        "show_dialog":   "false",
    })
    return RedirectResponse(f"https://accounts.spotify.com/authorize?{params}")


@router.get("/spotify/callback")
async def sp_callback(code: str = "", error: str = ""):
    if error:
        return HTMLResponse(
            f"""<html><body style="font-family:sans-serif;padding:40px;background:#0a0a0c;color:#f0f0f4">
            <h3 style="color:#fc3c44">Ошибка: {error}</h3>
            <p style="color:#888">Закрой вкладку и попробуй снова.</p>
            <script>setTimeout(()=>window.close(),3000)</script></body></html>"""
        )
    if not code:
        return HTMLResponse("<h3>Нет кода авторизации</h3>")

    cid = _cfg.get("spotify-client-id", "").strip()
    cs  = _cfg.get("spotify-client-secret", "").strip()
    if not (cid and cs):
        return HTMLResponse(
            """<html><body style="font-family:sans-serif;padding:40px;background:#0a0a0c;color:#f0f0f4">
            <h3 style="color:#fc3c44">Client ID или Secret не заполнены</h3>
            <p>Вставь их в Settings → Spotify и авторизуйся снова.</p></body></html>"""
        )

    creds = base64.b64encode(f"{cid}:{cs}".encode()).decode()
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(
                "https://accounts.spotify.com/api/token",
                data={"grant_type": "authorization_code", "code": code,
                      "redirect_uri": REDIRECT_URI},
                headers={"Authorization": f"Basic {creds}",
                         "Content-Type": "application/x-www-form-urlencoded"},
            )
            if r.status_code != 200:
                return HTMLResponse(
                    f"""<html><body style="font-family:sans-serif;padding:40px;background:#0a0a0c;color:#f0f0f4">
                    <h3 style="color:#fc3c44">Ошибка {r.status_code}</h3>
                    <pre style="color:#888;font-size:12px">{r.text[:500]}</pre>
                    <p style="color:#888">Проверь Client ID, Secret и Redirect URI в Spotify Dashboard.</p>
                    <script>setTimeout(()=>window.close(),8000)</script></body></html>"""
                )
            tok = r.json()
        tok["expires_at"] = datetime.now().timestamp() + tok.get("expires_in", 3600)
        _save_sp(tok)
        if _broadcast:
            await _broadcast({"type": "spotify_authed"})
        return HTMLResponse(
            """<html><body style="font-family:sans-serif;padding:40px;background:#0a0a0c;color:#f0f0f4">
            <h2 style="color:#1db954">✓ Spotify подключён!</h2>
            <p style="color:#888">Вкладка закроется автоматически…</p>
            <script>setTimeout(()=>window.close(),1500)</script></body></html>"""
        )
    except Exception as e:
        return HTMLResponse(
            f"""<html><body style="font-family:sans-serif;padding:40px;background:#0a0a0c;color:#f0f0f4">
            <h3 style="color:#fc3c44">Ошибка соединения</h3>
            <pre style="color:#888;font-size:12px">{e}</pre></body></html>"""
        )


@router.get("/api/spotify/status")
async def sp_status():
    global _sp_403_error
    t = _load_sp()
    sp_dc = _cfg.get("spotify-sp-dc", "").strip()
    if not t.get("access_token"):
        if sp_dc:
            # Verify sp_dc is actually valid by trying to get a token
            web_token = await _sp_dc_get_token()
            if web_token:
                return {"connected": True, "display_name": "sp_dc", "email": "",
                        "image": "", "error": "", "sp_dc_mode": True}
            return {"connected": False,
                    "error": "sp_dc кука истекла — обнови в настройках",
                    "sp_dc_expired": True}
        return {"connected": False, "error": ""}
    me = await _sp_get("/me")
    return {
        "connected":    bool(me),
        "display_name": (me or {}).get("display_name", ""),
        "email":        (me or {}).get("email", ""),
        "image":        (((me or {}).get("images") or [{}])[0]).get("url", ""),
        "error":        _sp_403_error if not me else "",
    }


def _sp_alb_to_rel(alb: dict, artist: dict, group: str = "") -> dict:
    # When fetched via the compilation include_group, mark the type accordingly
    # so the "Сборники" filter chip catches it (Spotify often labels comps as
    # album_type=album, with only album_group=compilation revealing the truth).
    atype = "compilation" if group == "compilation" else alb.get("album_type", "album")
    return {
        "id":        alb.get("id", ""),
        "title":     alb.get("name", ""),
        "artist":    artist["name"],
        "artist_id": artist["id"],
        "type":      atype,
        # album_group distinguishes a real release from an "appears_on" credit;
        # stored so the served feed can be re-filtered by type without refetching.
        "group":     group or alb.get("album_group") or alb.get("album_type", "album"),
        "date":      alb.get("release_date", ""),
        "year":      (alb.get("release_date") or "")[:4],
        "tracks":    alb.get("total_tracks"),
        "cover":     (alb.get("images") or [{}])[0].get("url", ""),
        "url":       alb.get("external_urls", {}).get("spotify", ""),
        "service":   "spotify",
    }


async def _run_sp_scan(days: int, types: str, cache_key: str) -> None:
    """Background scan — runs in asyncio task, stores results in _sp_releases_cache."""
    global _sp_403_error, _sp_releases_cache, _sp_scan_running, _sp_scan_started
    global _sp_last_error, _sp_last_done_ts
    import asyncio
    _sp_last_error = ""
    try:
        await _run_sp_scan_inner(days, types, cache_key)
    except Exception as e:
        import traceback
        print(f"[spotify] scan crashed: {e}", flush=True)
        traceback.print_exc()
        _sp_scan_running = False
        _sp_last_error = str(e) or f"Сканер упал: {type(e).__name__}"
        if _broadcast:
            await _broadcast({"type": "releases_scan_done", "error": _sp_last_error,
                              "artists_checked": 0, "releases_count": 0})
    finally:
        _sp_last_done_ts = datetime.now().timestamp()


async def _run_sp_scan_inner(days: int, types: str, cache_key: str) -> None:
    global _sp_403_error, _sp_releases_cache, _sp_scan_running, _sp_last_error
    global _sp_banned_until, _sp_artist_state, _sp_followed_cache
    import asyncio
    import time as _time

    _sp_403_error = ""

    web_token = await _sp_dc_get_token()
    if web_token:
        hdr = {"Authorization": f"Bearer {web_token}"}
        print("[spotify] scan using web player token (sp_dc)", flush=True)
    else:
        t = _load_sp()
        sp_dc_set = bool((_cfg.get("spotify-sp-dc") or "").strip())
        if not t.get("access_token"):
            _sp_scan_running = False
            print("[spotify] scan aborted: no sp_dc token and no OAuth token", flush=True)
            err = ("sp_dc cookie протух — обнови в Settings → Spotify (bookmarklet)"
                   if sp_dc_set
                   else "Spotify не авторизован — подключи OAuth или sp_dc в Settings → Spotify")
            _sp_last_error = err
            if _broadcast:
                await _broadcast({"type": "releases_scan_done", "error": err,
                                  "artists_checked": 0, "releases_count": 0})
            return
        if t.get("expires_at", 0) < datetime.now().timestamp() + 60:
            t = await _sp_refresh(t) or t
        if not t.get("access_token"):
            _sp_scan_running = False
            _sp_last_error = "Spotify: не удалось обновить OAuth-токен"
            if _broadcast:
                await _broadcast({"type": "releases_scan_done",
                                  "error": _sp_last_error,
                                  "artists_checked": 0, "releases_count": 0})
            return
        hdr = {"Authorization": f"Bearer {t['access_token']}"}

    if _broadcast:
        await _broadcast({"type": "releases_scan_start", "phase": "artists"})

    artists: list = []
    market = ""
    limits = httpx.Limits(max_connections=50, max_keepalive_connections=30)
    async with httpx.AsyncClient(timeout=12, headers=hdr, limits=limits,
                                 **_sp_httpx_kwargs()) as client:
        # Fetch /me once for market
        try:
            me_r = await client.get("https://api.spotify.com/v1/me")
            if me_r.status_code == 200:
                market = (me_r.json().get("country") or "").strip()
        except Exception:
            pass

        # Honour an active 429 / token-expiry ban: do NOT re-hit /me/following
        # (that's what keeps us banned). Use the durable followed-artist list and
        # let the paced delta crawl (which also respects the ban) serve the store.
        _ban_left = _sp_banned_until - datetime.now().timestamp()
        if _ban_left > 0 and (_sp_followed_cache.get("artists")):
            print(f"[spotify] crawl: ban active {int(_ban_left)}s — skip /me/following, "
                  f"use {len(_sp_followed_cache['artists'])} cached artists", flush=True)
            artists = list(_sp_followed_cache.get("artists") or [])
            market = _sp_followed_cache.get("market", "") or market
            url = None
        else:
            url = "https://api.spotify.com/v1/me/following?type=artist&limit=50"
        _429_hits = 0
        while url:
            try:
                r = await client.get(url)
                if r.status_code == 403:
                    # 403 = user not registered for the app, OR a transient block.
                    # Don't blank the UI — serve the durable store and surface the
                    # error so seeded/known releases stay visible.
                    try:
                        _sp_403_error = (r.json().get("error") or {}).get("message", "User not registered")
                    except Exception:
                        _sp_403_error = "User not registered"
                    _sp_last_error = "Spotify 403: " + _sp_403_error
                    print(f"[spotify] /me/following 403: {_sp_403_error} — serving store", flush=True)
                    feed = _build_feed(days, types)
                    _sp_releases_cache[cache_key] = {**feed, "ts": datetime.now().timestamp(),
                                                     "partial": True}
                    _save_disk_cache()
                    _sp_scan_running = False
                    if _broadcast:
                        await _broadcast({"type": "releases_scan_done",
                                          "error": _sp_403_error,
                                          "artists_checked": feed["artists_checked"],
                                          "releases_count": len(feed["releases"]),
                                          "releases": feed["releases"], "partial": True})
                    return
                if r.status_code == 429:
                    # Rate-limited mid-pagination. ONE short retry, then stop and
                    # record a ban — endlessly retrying a 429'd page (the old bug)
                    # just deepens the ban. We keep the artists already collected.
                    _429_hits += 1
                    ra = int(r.headers.get("Retry-After", "30") or 30)
                    if _429_hits >= 2:
                        _sp_banned_until = datetime.now().timestamp() + min(max(ra, 300), _SP_BAN_CAP)
                        _save_artist_state()
                        print(f"[spotify] /me/following 429 ×{_429_hits} after {len(artists)} artists — "
                              f"ban {int(_sp_banned_until - datetime.now().timestamp())}s, using what we have",
                              flush=True)
                        break
                    wait = min(ra, 30)
                    print(f"[spotify] /me/following 429 after {len(artists)} artists — wait {wait}s "
                          f"(retry {_429_hits})", flush=True)
                    await asyncio.sleep(wait)
                    continue
                if r.status_code == 401:
                    # OAuth/web token expired mid-crawl. Re-hitting with a dead
                    # token only earns a 429 ban — stop the pass and back off 10m
                    # so the next scheduled crawl waits for a fresh token.
                    _sp_banned_until = max(_sp_banned_until,
                                           datetime.now().timestamp() + 600)
                    _save_artist_state()
                    print(f"[spotify] /me/following 401 (token expired) after {len(artists)} "
                          f"artists — stop pass, back off 10m", flush=True)
                    break
                if r.status_code != 200:
                    print(f"[spotify] /me/following returned {r.status_code}: {r.text[:200]}", flush=True)
                    break
                blk = r.json().get("artists") or {}
                artists.extend(blk.get("items") or [])
                url = blk.get("next") or None
            except Exception as e:
                print(f"[spotify] following fetch error after {len(artists)}: {e}", flush=True)
                break

        if artists:
            # Refresh the durable followed-artist list (only on a good response, so
            # a transient 429/error never wipes it). The feed is built from this.
            _sp_followed_cache = {
                "artists": [{"id": a.get("id"), "name": a.get("name", "")}
                            for a in artists if a.get("id")],
                "market":  market or _sp_followed_cache.get("market", ""),
                "ts":      datetime.now().timestamp(),
            }
            _save_artist_state()
        else:
            # No artists this pass (ban / transient error). Serve the store rather
            # than overwriting good data with an empty result.
            feed = _build_feed(days, types)
            _sp_releases_cache[cache_key] = {**feed, "ts": datetime.now().timestamp(),
                                             "partial": feed["ban_left"] > 0}
            _save_disk_cache()
            _sp_scan_running = False
            print(f"[spotify] following empty — served store: {len(feed['releases'])} releases", flush=True)
            if _broadcast:
                await _broadcast({"type": "releases_scan_done",
                                  "artists_checked": feed["artists_checked"],
                                  "releases_count": len(feed["releases"]),
                                  "releases": feed["releases"],
                                  "partial": feed["ban_left"] > 0})
            return
        market = _sp_followed_cache.get("market", market)

        # ── Paced, ban-aware, persistent DELTA crawl ────────────────────────
        # Never burst. One throttled request at a time; honour any active 429
        # ban; persist per-artist results so partial crawls accumulate and repeat
        # scans only re-check stale artists. This is what the public release-radar
        # sites effectively do (background crawler + served cache) and what keeps
        # us off Spotify's rate-limit radar.
        incl_appears = "appears_on" in [x.strip() for x in types.split(",")]
        # Crawl each release group in its OWN include_groups call. A combined
        # "album,single,compilation" call lets Spotify dedup compilations away
        # (it returns a release once, preferring album/single), so the artist's
        # compilations never land. Separate calls guarantee each group is fetched
        # and tagged. appears_on (Various-Artists сборники) stays opt-in (heavy).
        crawl_groups = ["album,single", "compilation"] + (["appears_on"] if incl_appears else [])
        mkt          = f"&market={market}" if market else ""
        state_cutoff = (datetime.now() - timedelta(days=_SP_STATE_WINDOW_DAYS)).strftime("%Y-%m-%d")
        interval     = float(_cfg.get("spotify-crawl-interval", _SP_CRAWL_INTERVAL) or _SP_CRAWL_INTERVAL)

        def _norm_date(rd: str, precision: str) -> str:
            """Normalize partial dates so string comparison works correctly."""
            if precision == "year" or (rd and len(rd) == 4):
                return rd + "-07-01"   # mid-year — don't miss H2 releases
            if precision == "month" or (rd and len(rd) == 7):
                return rd + "-15"      # mid-month
            return rd

        total     = len(artists)
        completed = 0
        fetched   = 0          # artists actually hit over the network this pass
        last_req  = [0.0]
        step      = max(1, total // 40)
        now_ts    = datetime.now().timestamp()

        if _broadcast:
            await _broadcast({"type": "releases_scan_start", "phase": "albums",
                               "total": total, "service": "spotify"})

        async def _paced_get(pg: str):
            """One throttled GET. Returns (data|None, status, retry_after)."""
            import time as _t
            dt = _t.monotonic() - last_req[0]
            if dt < interval:
                await asyncio.sleep(interval - dt)
            last_req[0] = _t.monotonic()
            try:
                dr = await client.get(pg)
            except Exception:
                return None, 0, 0
            if dr.status_code == 200:
                return dr.json(), 200, 0
            if dr.status_code == 429:
                return None, 429, int(dr.headers.get("Retry-After", "60") or 60)
            return None, dr.status_code, 0

        banned = _time.time() < _sp_banned_until
        if banned:
            print(f"[spotify] crawl: rate-limit ban active "
                  f"{int(_sp_banned_until - _time.time())}s — serving stored state only",
                  flush=True)

        for idx, artist in enumerate(artists):
            aid  = artist.get("id", "")
            name = artist.get("name", "?")
            if not aid:
                continue
            st    = _sp_artist_state.get(aid)
            fresh = st and (now_ts - st.get("ts", 0)) < _SP_ARTIST_REFRESH
            if fresh or banned:
                # Reuse stored data (fresh), or — if banned — we simply cannot
                # fetch right now; the served feed is rebuilt from the store below.
                if st:
                    completed += 1
            else:
                rels    = []
                hit_429 = False
                for group in crawl_groups:
                    pg = (f"https://api.spotify.com/v1/artists/{aid}/albums"
                          f"?include_groups={group}&limit=20{mkt}")
                    data, status, ra = await _paced_get(pg)
                    if status == 429:
                        _sp_banned_until = _time.time() + min(ra, _SP_BAN_CAP)
                        _save_artist_state()
                        banned = hit_429 = True
                        print(f"[spotify] crawl 429 at '{name}' ({idx+1}/{total}) — "
                              f"ban {min(ra, _SP_BAN_CAP)}s; serving stored state",
                              flush=True)
                        break
                    if not data:
                        continue
                    # Tag the release with the include_group it came from so the
                    # served feed can filter by type (appears_on / compilation).
                    grp_tag = ("appears_on" if group == "appears_on"
                               else "compilation" if group == "compilation" else "")
                    for alb in data.get("items") or []:
                        rd = _norm_date(alb.get("release_date", ""),
                                        alb.get("release_date_precision", "day"))
                        if rd >= state_cutoff:
                            rels.append(_sp_alb_to_rel(alb, artist, grp_tag))
                if not hit_429:
                    _sp_artist_state[aid] = {"name": name, "releases": rels, "ts": now_ts}
                    fetched   += 1
                    completed += 1
                    if fetched % 25 == 0:
                        _save_artist_state()
            if _broadcast and (idx % step == 0 or idx == total - 1):
                await _broadcast({
                    "type": "releases_scan_progress",
                    "current": completed, "total": total,
                    "artist": name, "found": 0, "service": "spotify",
                })
            if banned:
                break   # stop hammering; serve the store

        _save_artist_state()
        print(f"[spotify] crawl pass: {completed}/{total} covered, "
              f"{fetched} fetched live"
              + (f", BANNED {int(_sp_banned_until - _time.time())}s left" if banned else ""),
              flush=True)

    # ── Serve the feed from the durable store (single source of truth) ───────
    feed     = _build_feed(days, types)
    releases = feed["releases"]
    checked  = feed["artists_checked"]
    partial  = feed["ban_left"] > 0
    _sp_releases_cache[cache_key] = {**feed, "ts": datetime.now().timestamp(),
                                     "partial": partial}
    _save_disk_cache()
    _sp_scan_running = False
    print(f"[spotify] scan served: {checked}/{feed['followed']} artists in store, "
          f"{len(releases)} releases"
          + (f" (rate-limit ban {feed['ban_left']}s left — feed fills as it lifts)"
             if partial else ""), flush=True)

    if _broadcast:
        await _broadcast({
            "type":            "releases_scan_done",
            "artists_checked": checked,
            "releases_count":  len(releases),
            "releases":        releases,
            "partial":         partial,
        })


@router.get("/api/spotify/releases")
async def sp_releases(days: int = 30, types: str = "album,single", force: int = 0):
    global _sp_scan_running, _sp_scan_started
    import asyncio

    t = _load_sp()
    sp_dc = _cfg.get("spotify-sp-dc", "").strip()
    if not t.get("access_token") and not sp_dc:
        return {"ok": False, "error": "Not connected", "releases": []}

    cache_key = f"{days}|{types}"
    now_ts    = datetime.now().timestamp()

    # Make sure the background crawler is running — it keeps the durable store
    # fresh independent of UI requests (the "release-radar site" behaviour).
    _ensure_crawler()

    # The feed is ALWAYS served instantly from the durable store (no network on
    # the request path). `force` just kicks an immediate crawl pass on top.
    feed     = _build_feed(days, types)
    scanning = _sp_scan_running

    need_scan = force or scanning is False and (
        not _sp_followed_cache.get("artists")                                # never crawled
        or (now_ts - _sp_followed_cache.get("ts", 0)) > _SP_RELEASES_TTL     # store stale
    )
    if force and _sp_scan_running:
        _sp_scan_running = False   # allow a forced pass to start
    scan_stuck = _sp_scan_running and (now_ts - _sp_scan_started) > _SP_SCAN_TIMEOUT
    if need_scan and (not _sp_scan_running or scan_stuck):
        _sp_scan_running = True
        _sp_scan_started = now_ts
        asyncio.ensure_future(_run_sp_scan(days, types, cache_key))
        scanning = True

    return {
        "ok":              True,
        "releases":        feed["releases"],
        "artists_checked": feed["artists_checked"],
        "followed":        feed["followed"],
        "market":          feed["market"],
        "cached":          True,
        "scanning":        scanning,
        "ban_left":        feed["ban_left"],
        "last_error":      _sp_last_error,
        "last_done_ts":    _sp_last_done_ts,
    }


def _ensure_crawler() -> None:
    """Start the background crawler loop once (idempotent)."""
    global _sp_crawler_started
    if _sp_crawler_started:
        return
    import asyncio
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    _sp_crawler_started = True
    asyncio.ensure_future(_sp_background_crawler())
    print("[spotify] background crawler started", flush=True)


async def _sp_background_crawler() -> None:
    """Continuously keep the per-artist store fresh — paced, ban-aware. Wakes
    every _SP_CRAWL_EVERY seconds; each pass refreshes stale artists only, so
    steady-state load is tiny. While rate-limit-banned it just idles."""
    global _sp_scan_running, _sp_scan_started
    import asyncio
    await asyncio.sleep(5)   # let startup settle
    while True:
        try:
            t = _load_sp()
            sp_dc = (_cfg.get("spotify-sp-dc") or "").strip()
            banned = datetime.now().timestamp() < _sp_banned_until
            if (t.get("access_token") or sp_dc) and not banned and not _sp_scan_running:
                _sp_scan_running = True
                _sp_scan_started = datetime.now().timestamp()
                # Real releases only (no appears_on) → 1 request/artist, half the
                # load for big follow lists. appears_on stays available on demand.
                await _run_sp_scan(3650, "album,single,compilation", "bg")
        except Exception as e:
            print(f"[spotify] bg crawler error: {e}", flush=True)
            _sp_scan_running = False
        # Sleep longer while banned so we wake up roughly when it lifts.
        nap = _SP_CRAWL_EVERY
        ban_left = _sp_banned_until - datetime.now().timestamp()
        if ban_left > 0:
            nap = min(max(int(ban_left) + 10, 60), 3600)
        await asyncio.sleep(nap)


@router.get("/api/spotify/scan-status")
async def sp_scan_status():
    """Lightweight status probe — used by the frontend poll fallback when the
    WS done event was missed (slow client, dropped connection, tab background).
    """
    return {
        "running":      _sp_scan_running,
        "started":      _sp_scan_started,
        "last_error":   _sp_last_error,
        "last_done_ts": _sp_last_done_ts,
        "cached_keys":  list(_sp_releases_cache.keys()),
    }


@router.get("/api/spotify/extract-sp-dc")
async def extract_sp_dc():
    """Try to read sp_dc from local browser profiles (Firefox / Chromium-based).
    Returns the cookie value if found, or a helpful error.
    """
    import asyncio
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, _find_sp_dc_in_browsers)
    if not result.get("ok"):
        result["bookmarklet_hint"] = True
    return result


@router.post("/api/spotify/inject-sp-dc")
async def inject_sp_dc(body: dict, request: Request):
    """Receive sp_dc from the browser bookmarklet running on open.spotify.com.
    No user auth required — server only binds to 127.0.0.1.
    """
    import asyncio
    from fastapi.responses import JSONResponse
    sp_dc = (body.get("sp_dc") or "").strip()
    if not sp_dc:
        r = JSONResponse({"ok": False, "error": "sp_dc is empty"})
    else:
        _cfg["spotify-sp-dc"] = sp_dc
        _sp_dc_web.clear()
        if _save_config_fn:
            try: _save_config_fn(_cfg)
            except Exception: pass
        if _broadcast:
            asyncio.create_task(_broadcast({"type": "spotify_sp_dc_updated"}))
        print(f"[spotify] sp_dc updated via bookmarklet (len={len(sp_dc)})", flush=True)
        r = JSONResponse({"ok": True})
    r.headers["Access-Control-Allow-Origin"] = "https://open.spotify.com"
    return r


@router.options("/api/spotify/inject-sp-dc")
async def inject_sp_dc_preflight():
    from fastapi.responses import Response
    r = Response()
    r.headers["Access-Control-Allow-Origin"]  = "https://open.spotify.com"
    r.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
    r.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return r


def _copy_locked_file(src: str, dst: str) -> bool:
    """Copy a file that may be locked by another process.
    Uses os.open(O_RDONLY) which on Windows requests FILE_SHARE_READ|WRITE|DELETE.
    """
    import os
    try:
        fd = os.open(src, os.O_RDONLY | os.O_BINARY)
        try:
            with open(dst, "wb") as out:
                while True:
                    chunk = os.read(fd, 65536)
                    if not chunk:
                        break
                    out.write(chunk)
        finally:
            os.close(fd)
        return True
    except Exception:
        return False


def _safe_copy_db(src: str, dst: str) -> bool:
    """Try shutil first; fall back to locked-file copy on Windows."""
    import shutil
    try:
        shutil.copy2(src, dst)
        return True
    except OSError:
        return _copy_locked_file(src, dst)


def _find_sp_dc_in_browsers() -> dict:
    """Synchronous helper — runs in a thread pool executor."""
    import os, sqlite3, tempfile

    found: list[dict] = []

    # ── Firefox ──────────────────────────────────────────────────────────────
    ff_base = os.path.join(os.environ.get("APPDATA", ""), "Mozilla", "Firefox", "Profiles")
    if os.path.isdir(ff_base):
        for profile in os.listdir(ff_base):
            db = os.path.join(ff_base, profile, "cookies.sqlite")
            if not os.path.isfile(db):
                continue
            tmp = None
            try:
                fd, tmp = tempfile.mkstemp(suffix=".sqlite")
                os.close(fd)
                if not _safe_copy_db(db, tmp):
                    continue
                con = sqlite3.connect(tmp)
                cur = con.execute(
                    "SELECT value FROM moz_cookies WHERE host LIKE '%spotify.com%' AND name='sp_dc'"
                )
                row = cur.fetchone()
                con.close()
                if row and row[0]:
                    found.append({"browser": "Firefox", "profile": profile, "value": row[0]})
            except Exception:
                pass
            finally:
                if tmp:
                    try: os.unlink(tmp)
                    except OSError: pass

    # ── Chromium-based (Chrome / Edge / Brave / Opera) ───────────────────────
    _CHROMIUM_PATHS = [
        ("Chrome",  os.path.join(os.environ.get("LOCALAPPDATA",""), "Google","Chrome","User Data")),
        ("Edge",    os.path.join(os.environ.get("LOCALAPPDATA",""), "Microsoft","Edge","User Data")),
        ("Brave",   os.path.join(os.environ.get("LOCALAPPDATA",""), "BraveSoftware","Brave-Browser","User Data")),
        ("Opera",   os.path.join(os.environ.get("APPDATA",""), "Opera Software","Opera Stable")),
        ("Vivaldi", os.path.join(os.environ.get("LOCALAPPDATA",""), "Vivaldi","User Data")),
    ]
    for browser_name, user_data in _CHROMIUM_PATHS:
        if not os.path.isdir(user_data):
            continue
        for profile_dir in ["Default", "Profile 1", "Profile 2", "Profile 3"]:
            # Newer Chrome/Edge store cookies in Network/ subdirectory
            local_state = os.path.join(user_data, "Local State")
            profile_path = os.path.join(user_data, profile_dir)
            cookies_db = os.path.join(profile_path, "Network", "Cookies")
            if not os.path.isfile(cookies_db):
                cookies_db = os.path.join(profile_path, "Cookies")
            if not os.path.isfile(cookies_db):
                continue
            tmp = None
            try:
                fd, tmp = tempfile.mkstemp(suffix=".sqlite")
                os.close(fd)
                if not _safe_copy_db(cookies_db, tmp):
                    continue
                con = sqlite3.connect(tmp)
                # Chromium uses 'host_key' and the host is stored without leading dot
                cur = con.execute(
                    "SELECT encrypted_value FROM cookies WHERE host_key LIKE '%spotify.com%' AND name='sp_dc'"
                )
                row = cur.fetchone()
                con.close()
                if not row or not row[0]:
                    continue
                enc_val: bytes = row[0]
                # v10/v20 cookies are AES-256-GCM encrypted
                if enc_val[:3] in (b"v10", b"v20"):
                    key = _chromium_decrypt_key(local_state)
                    if key:
                        plain = _aes_gcm_decrypt(key, enc_val[3:])
                        if plain:
                            found.append({"browser": browser_name,
                                          "profile": profile_dir, "value": plain})
                else:
                    # Old DPAPI-only encryption
                    plain = _dpapi_decrypt(enc_val)
                    if plain:
                        found.append({"browser": browser_name,
                                      "profile": profile_dir, "value": plain})
            except Exception:
                pass
            finally:
                if tmp:
                    try: os.unlink(tmp)
                    except OSError: pass

    if not found:
        return {"ok": False, "error": "sp_dc не найдена ни в одном браузере. Убедись, что вошёл в Spotify в Firefox или Chrome."}

    # Prefer the first valid-looking value (not expired tokens are long strings)
    best = max(found, key=lambda x: len(x["value"]))
    return {"ok": True, "value": best["value"], "browser": best["browser"], "profile": best["profile"]}


def _chromium_decrypt_key(local_state_path: str) -> bytes | None:
    """Extract AES key from Chrome/Edge Local State using DPAPI."""
    import base64 as _b64, json as _json
    try:
        with open(local_state_path, encoding="utf-8") as f:
            ls = _json.load(f)
        enc_key_b64 = ls["os_crypt"]["encrypted_key"]
        enc_key = _b64.b64decode(enc_key_b64)[5:]  # strip "DPAPI" prefix
        return _dpapi_decrypt_bytes(enc_key)
    except Exception:
        return None


def _dpapi_decrypt(data: bytes) -> str | None:
    """Decrypt DPAPI-protected bytes and return as UTF-8 string."""
    result = _dpapi_decrypt_bytes(data)
    if result:
        try:
            return result.decode("utf-8")
        except Exception:
            return None
    return None


def _dpapi_decrypt_bytes(data: bytes) -> bytes | None:
    """Call Windows CryptUnprotectData via ctypes without requiring pywin32."""
    import ctypes, ctypes.wintypes
    try:
        class DATA_BLOB(ctypes.Structure):
            _fields_ = [("cbData", ctypes.wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]
        p = ctypes.create_string_buffer(data, len(data))
        blobin  = DATA_BLOB(ctypes.sizeof(p), p)
        blobout = DATA_BLOB()
        retval  = ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(blobin), None, None, None, None, 0, ctypes.byref(blobout)
        )
        if not retval:
            return None
        result = ctypes.string_at(blobout.pbData, blobout.cbData)
        ctypes.windll.kernel32.LocalFree(blobout.pbData)
        return result
    except Exception:
        return None


def _aes_gcm_decrypt(key: bytes, payload: bytes) -> str | None:
    """Decrypt AES-256-GCM payload: 12-byte nonce + ciphertext + 16-byte tag."""
    try:
        from Crypto.Cipher import AES
        nonce      = payload[:12]
        ciphertext = payload[12:-16]
        tag        = payload[-16:]
        cipher     = AES.new(key, AES.MODE_GCM, nonce=nonce)
        plain      = cipher.decrypt_and_verify(ciphertext, tag)
        return plain.decode("utf-8")
    except Exception:
        return None


@router.post("/api/spotify/logout")
async def sp_logout():
    global _sp_token
    _sp_token = {}
    if _token_file and _token_file.exists():
        _token_file.unlink()
    return {"ok": True}


# ── Spotify → target conversion ───────────────────────────────────────────

@router.post("/api/convert/spotify")
async def api_convert_spotify(body: dict):
    sp_url  = body.get("url", "").strip()
    target  = body.get("target", _cfg.get("engine", "apple"))
    service = "apple" if ("apple" in target or target == "amd") else target

    if "spotify.com" not in sp_url:
        return {"ok": False, "error": "Not a Spotify URL"}

    try:
        async with httpx.AsyncClient(timeout=8) as c:
            oe   = await c.get("https://open.spotify.com/oembed", params={"url": sp_url})
            meta = oe.json()
    except Exception as e:
        return {"ok": False, "error": f"Spotify oEmbed failed: {e}"}

    title = meta.get("title", "")
    parts = title.rsplit(" - ", 1)
    track_name  = parts[0].strip() if len(parts) == 2 else title
    artist_name = parts[1].strip() if len(parts) == 2 else ""
    query = f"{track_name} {artist_name}".strip()

    sp_type = "album" if "/album/" in sp_url else "track" if "/track/" in sp_url else "album"

    if service == "apple":
        res = await _disc._search_apple(query, sp_type, 5, "")
    elif service == "deezer":
        res = await _disc._search_deezer(query, sp_type, 5)
    else:
        res = await _disc._search_apple(query, sp_type, 5, "")

    results = res.get("results", [])
    if not results:
        return {"ok": False, "error": f"Not found on {service}: {query}", "query": query}

    best = results[0]
    return {
        "ok":      True,
        "source":  {"url": sp_url, "title": title},
        "target":  best,
        "query":   query,
        "service": service,
    }
