"""
1001Tracklists tracklist source — no browser/Docker required.

Recon (2026-06-01) showed the earlier "connection reset" was the user's own
router (xkeen), not 1001TL anti-bot. On a healthy connection:
  • tracklist pages are fully SERVER-RENDERED — track names (meta itemprop
    "name" / "byArtist"), per-row hidden `cue_seconds`, and a cueValues <script>
    are all in the static HTML; the "enable JavaScript" string is only a
    <noscript> fallback.  → plain httpx + regex parser works (verified 23/23).
  • 1001TL's own /search is JS-rendered, so we resolve the tracklist URL via a
    plain-HTML search engine (DuckDuckGo html/lite) with a `site:` query.

FlareSolverr (config `flaresolverr-url`) is kept ONLY as an optional fallback
for the rare case a page is rate-limited; it is never required.

Output: normalised [{n,artist,title,timestamp,seconds,is_with}], slotted into
the SoundCloud track-list enrichment behind the multi-stage verifier
(`ripster.tracklist_match`).
"""
from __future__ import annotations

import re
import time
import base64
import html as _html
import urllib.parse

import httpx

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
_BASE = "https://www.1001tracklists.com"

_cfg = None
_HTML_CACHE: dict[str, tuple[float, str]] = {}
_HTML_TTL = 3600.0

# ── persistent track-list cache (avoid re-hammering the site) ────────────────
# Parsed tracklists are cached to disk for 10 days; expired entries are pruned
# on load. Keyed by the SoundCloud mix id when available (stable), else by a
# normalised artist|title. Successful results live 10 days; misses are cached
# briefly so a not-yet-indexed set is retried soon (and after login is added).
import json as _json
import os as _os
from pathlib import Path as _Path

_CACHE_TTL = 10 * 86400          # 10 days for hits
_MISS_TTL = 12 * 3600            # 12 h for misses
_CACHE_FILE = _Path(__file__).resolve().parent.parent / "tl1001_cache.json"
_DISK: dict | None = None

# Circuit breaker: when 1001TL goes slow/blocked (e.g. captcha/rate-limit), keep
# hammering it would pin run_in_threadpool workers (each request ~8s) and lag the
# whole app. After a network failure we back off for _COOLDOWN_SEC — during which
# tracklist_for returns instantly without touching the network.
_cooldown_until: float = 0.0
_COOLDOWN_SEC = 300


def install(config) -> None:
    global _cfg
    _cfg = config


def _disk_load() -> dict:
    global _DISK
    if _DISK is not None:
        return _DISK
    data = {}
    try:
        if _CACHE_FILE.is_file():
            data = _json.loads(_CACHE_FILE.read_text(encoding="utf-8")) or {}
    except Exception:
        data = {}
    now = time.time()
    # prune expired
    pruned = {k: v for k, v in data.items()
              if (now - v.get("ts", 0)) < (_CACHE_TTL if v.get("ok") else _MISS_TTL)}
    if len(pruned) != len(data):
        _DISK = pruned
        _disk_flush()
    else:
        _DISK = pruned
    return _DISK


def _disk_flush() -> None:
    try:
        tmp = str(_CACHE_FILE) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            _json.dump(_DISK, f, ensure_ascii=False)
        _os.replace(tmp, _CACHE_FILE)
    except Exception:
        pass


def _disk_get(key: str):
    d = _disk_load()
    v = d.get(key)
    if not v:
        return None
    ttl = _CACHE_TTL if v.get("ok") else _MISS_TTL
    if time.time() - v.get("ts", 0) >= ttl:
        return None
    return v.get("result")


def _disk_set(key: str, result: dict) -> None:
    d = _disk_load()
    d[key] = {"ts": time.time(), "ok": bool(result.get("ok")), "result": result}
    _disk_flush()


def _solver_url() -> str:
    base = str(_cfg.get("flaresolverr-url") or "").strip() if _cfg is not None else ""
    return base


# ── session + login ──────────────────────────────────────────────────────────
_SESSION: httpx.Client | None = None
_LOGGED_IN = False
_BROWSER_HEADERS = {
    "User-Agent": _UA,
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,*/*;q=0.8"),
    "Accept-Language": "en-US,en;q=0.9",
    "Upgrade-Insecure-Requests": "1",
}


def _session() -> httpx.Client:
    """Shared client carrying the `guid` cookie (and the auth cookie once
    logged in). Logs in automatically when credentials are configured —
    an authenticated session bypasses the anti-bot challenge and unlocks
    member-only tracklists."""
    global _SESSION
    if _SESSION is None:
        _SESSION = httpx.Client(headers=dict(_BROWSER_HEADERS), timeout=8,
                                follow_redirects=True)
        try:
            _SESSION.get(_BASE + "/")     # warm: obtain the `guid` cookie
        except Exception:
            pass
        _maybe_login(_SESSION)
    return _SESSION


_login_cooldown_until: float = 0.0


def _maybe_login(client: httpx.Client) -> bool:
    global _LOGGED_IN, _login_cooldown_until
    if _LOGGED_IN:
        return True
    # A failed login to a blocked 1001TL can take tens of seconds — don't retry
    # it on every fresh session; back off and run anonymously meanwhile.
    if time.time() < _login_cooldown_until:
        return False
    email = str(_cfg.get("tl1001-email") or "").strip() if _cfg else ""
    pwd = str(_cfg.get("tl1001-password") or "").strip() if _cfg else ""
    if not email or not pwd:
        return False
    try:
        client.get(_BASE + "/action/login.html", headers={"Referer": _BASE + "/"})
        r = client.post(_BASE + "/action/login.html",
                        data={"email": email, "password": pwd, "login": "login"},
                        headers={"Referer": _BASE + "/action/login.html"})
        # logged-in pages drop the login form and show a logout/account link
        body = r.text.lower()
        _LOGGED_IN = ("logout" in body or "my profile" in body or
                      'name="password"' not in body)
        if not _LOGGED_IN:
            _login_cooldown_until = time.time() + _COOLDOWN_SEC
    except Exception:
        _LOGGED_IN = False
        _login_cooldown_until = time.time() + _COOLDOWN_SEC
    return _LOGGED_IN


def login_status() -> dict:
    """For the Settings probe: report whether credentials log in OK."""
    global _SESSION, _LOGGED_IN
    _SESSION = None
    _LOGGED_IN = False
    c = _session()
    return {"configured": bool((_cfg or {}).get("tl1001-email")),
            "logged_in": _LOGGED_IN}


# ── fetching ───────────────────────────────────────────────────────────────
def _is_challenge(text: str) -> bool:
    low = text.lower()
    return ("just a moment" in low or
            ("captcha" in low and "tlpitem" not in low))


_last_challenged = False  # set when the most recent fetch hit the anti-bot wall


def _fetch_plain(url: str, timeout: float = 25.0) -> str | None:
    """Fetch through the shared (possibly authenticated) session so the auth
    cookie applies and the anti-bot challenge is avoided."""
    global _last_challenged
    try:
        c = _session()
        r = c.get(url, headers={"Referer": _BASE + "/"})
        if r.status_code in (200, 206) and not _is_challenge(r.text):
            return r.text
        _last_challenged = _is_challenge(r.text) or r.status_code == 206
    except Exception:
        return None
    return None


def _fetch_flaresolverr(url: str, timeout: float = 60.0) -> str | None:
    solver = _solver_url()
    if not solver:
        return None
    try:
        r = httpx.post(solver, json={"cmd": "request.get", "url": url,
                                     "maxTimeout": int(timeout * 1000)},
                       timeout=timeout + 10)
        data = r.json()
        if data.get("status") == "ok":
            return (data.get("solution") or {}).get("response")
    except Exception:
        return None
    return None


def fetch_html(url: str) -> str | None:
    now = time.time()
    hit = _HTML_CACHE.get(url)
    if hit and now - hit[0] < _HTML_TTL:
        return hit[1]
    html_text = _fetch_plain(url) or _fetch_flaresolverr(url)
    if html_text:
        _HTML_CACHE[url] = (now, html_text)
    return html_text


# ── URL resolution via plain-HTML search engines ────────────────────────────
_TL_RE = re.compile(r'https?://(?:www\.)?1001tracklists\.com/tracklist/[0-9a-z]+/[^\s"&<>]+\.html')


def _decode_engine_urls(text: str) -> list[str]:
    """Pull 1001TL tracklist URLs out of a search-results page, decoding the
    common redirect wrappers (DuckDuckGo uddg=, Bing /ck/a?u=a1<base64>)."""
    found = list(_TL_RE.findall(text))
    for u in re.findall(r'uddg=([^&"]+)', text):           # DuckDuckGo
        d = urllib.parse.unquote(u)
        if _TL_RE.fullmatch(d):
            found.append(d)
    for b in re.findall(r'u=a1([A-Za-z0-9_\-]+)', text):    # Bing redirect
        try:
            pad = b + "=" * (-len(b) % 4)
            d = base64.urlsafe_b64decode(pad).decode("utf-8", "ignore")
            found += _TL_RE.findall(d)
        except Exception:
            pass
    out = []
    for u in found:
        u = u if u.startswith("http") else "https://" + u
        if u not in out:
            out.append(u)
    return out


_REL_TL_RE = re.compile(r'/tracklist/[0-9a-z]+/[^"\s]+?\.html')


def _native_search(query: str, limit: int) -> list[str]:
    """1001TL's own search. GET renders results via JS, but a POST to
    /search/result.php with a warm session (guid cookie) returns them
    server-side, correctly ranked (exact set first). This is the best
    resolver — no third-party rate-limits."""
    try:
        c = _session()
        r = c.post(_BASE + "/search/result.php",
                   data={"main_search": query, "search_selection": "9"})
        if r.status_code != 200:
            # cookie may have expired — re-warm once
            global _SESSION
            _SESSION = None
            c = _session()
            r = c.post(_BASE + "/search/result.php",
                       data={"main_search": query, "search_selection": "9"})
    except Exception:
        return []
    out = []
    for rel in _REL_TL_RE.findall(r.text):
        u = _BASE + rel
        if u not in out:
            out.append(u)
    return out[:limit]


def search_urls(query: str, limit: int = 8) -> list[str]:
    """Resolve candidate 1001TL tracklist URLs. Native 1001TL search first
    (POST + warm session), then plain-HTML search engines as a fallback. The
    verifier disambiguates the candidates. Cached (resolution is rare)."""
    now = time.time()
    ckey = "search::" + query.lower()
    hit = _HTML_CACHE.get(ckey)
    if hit and now - hit[0] < _HTML_TTL:
        return hit[1].split("\n") if hit[1] else []

    out: list[str] = _native_search(query, limit)

    if not out:  # fallback: external engines (rate-limit on bursts)
        q = f"site:1001tracklists.com {query}".strip()
        engines = [
            ("https://html.duckduckgo.com/html/", {"q": q}),
            ("https://lite.duckduckgo.com/lite/", {"q": q}),
            ("https://www.startpage.com/sp/search", {"query": q}),
            ("https://www.bing.com/search", {"q": q}),
        ]
        for base, params in engines:
            try:
                r = httpx.get(base, params=params, headers={"User-Agent": _UA},
                              timeout=6, follow_redirects=True)
                if r.status_code != 200:
                    continue
                for u in _decode_engine_urls(r.text):
                    if u not in out:
                        out.append(u)
            except Exception:
                continue
            if len(out) >= limit:
                break

    out = out[:limit]
    _HTML_CACHE[ckey] = (now, "\n".join(out))
    return out


# ── parsing (verified against live HTML, 23/23 tracks) ───────────────────────
def _sec_to_ts(s):
    if s is None:
        return ""
    return f"{s // 60}:{s % 60:02d}"


def parse_tracklist_meta(html_text: str) -> dict:
    """Page-level metadata: set title + total duration (seconds) if present."""
    title = ""
    m = re.search(r'<meta itemprop="name" content="([^"]*)"', html_text)
    if m:
        title = _html.unescape(m.group(1)).strip()
    dur = None
    # 1001TL prints "duration: HH:MM" / "MM:MM" near the header; best-effort.
    md = re.search(r'duration[^0-9]{0,12}(\d{1,2}):(\d{2})(?::(\d{2}))?', html_text, re.I)
    if md:
        a, b, c = md.group(1), md.group(2), md.group(3)
        dur = (int(a) * 60 + int(b)) if c is None else (int(a) * 3600 + int(b) * 60 + int(c))
    return {"title": title, "duration": dur}


def parse_tracklist(html_text: str) -> list[dict]:
    """Extract ordered tracks from a 1001TL tracklist page.

    Each track row is `<div id="tlp_<digits>" class="tlpTog bItm tlpItem ...">`
    carrying `meta itemprop="name"`="Artist - Title", `itemprop="byArtist"`, a
    track-number span, and a hidden `cue_seconds`. "w/" rows are mashup
    sub-tracks layered over the previous main track (cue_seconds 0)."""
    tracks: list[dict] = []
    parts = re.split(r'<div id="tlp_\d+" class="tlpTog bItm tlpItem', html_text)
    for seg in parts[1:]:
        seg = seg[:6000]
        name = re.search(r'<meta itemprop="name" content="([^"]*)"', seg)
        art = re.search(r'itemprop="byArtist"[^>]*content="([^"]*)"', seg)
        num = re.search(r'tracknumber_value"[^>]*>\s*([^<]+?)\s*<', seg)
        sec = re.search(r'_cue_seconds"\s+type="hidden"\s+value="(\d+)"', seg)
        if not name and not art:
            continue
        nm = _html.unescape(name.group(1)).strip() if name else ""
        a = _html.unescape(art.group(1)).strip() if art else ""
        title = nm
        if a and nm.startswith(a + " - "):
            title = nm[len(a) + 3:]
        n = (num.group(1).strip() if num else "")
        is_with = (n.lower() == "w/")
        s = int(sec.group(1)) if sec else None
        if s == 0 and is_with:
            s = None  # sub-track has no real cue of its own
        tracks.append({"n": n, "artist": a, "title": title,
                       "seconds": s, "timestamp": _sec_to_ts(s),
                       "is_with": is_with})

    # Some 1001TL sets carry only track order/names with no cue times (every
    # cue is 0). That's a names-only tracklist — blank the bogus 0:00 stamps so
    # downstream shows clean names and builds NO time-chapters.
    distinct_pos = {t["seconds"] for t in tracks if t["seconds"]}
    if len(distinct_pos) < 2:
        for t in tracks:
            t["seconds"] = None
            t["timestamp"] = ""
    return tracks


# ── top-level ────────────────────────────────────────────────────────────────
def tracklist_for(title: str, artist: str = "", duration: int = 0,
                  sc_tracklist: list | None = None,
                  source_urls: list | None = None,
                  mix_id: str = "", force: bool = False) -> dict:
    """Resolve → fetch → parse → VERIFY, with a 10-day disk cache so the same
    mix is never re-fetched from the site. Returns
       {ok, tracks, url, match:{score,tier,checks}, cached?, error?}.
    `mix_id` (the SoundCloud id) is the preferred, stable cache key. `force`
    bypasses the cache. Never raises."""
    from ripster import tracklist_match as TM
    q = (f"{artist} {title}" if artist else title).strip()
    if not q:
        return {"ok": False, "error": "empty query"}

    ckey = ("id:" + str(mix_id)) if mix_id else ("q:" + q.lower())
    if not force:
        cached = _disk_get(ckey)
        if cached is not None:
            return {**cached, "cached": True}

    # Circuit breaker: if 1001TL recently failed (slow/blocked), return instantly
    # instead of pinning a threadpool worker for tens of seconds → no app-wide lag.
    if not force and time.time() < _cooldown_until:
        return {"ok": False, "cooldown": True,
                "error": "1001TL временно недоступен (backoff после сбоя)"}

    def _ret(result: dict) -> dict:
        # On a NETWORK failure (not just "no match"), trip the cooldown so we
        # stop hammering a slow/blocked 1001TL.
        if not result.get("ok") and (result.get("challenged") or result.get("error") in (
                "no search results", "fetch/parse failed for all candidates")):
            globals()["_cooldown_until"] = time.time() + _COOLDOWN_SEC
        _disk_set(ckey, result)
        return result

    urls = search_urls(q)
    if not urls:
        return _ret({"ok": False, "error": "no search results"})

    target = {"title": title, "artist": artist, "duration": duration,
              "tracklist": sc_tracklist or [], "source_urls": source_urls or []}
    best = None
    challenged = 0
    global _last_challenged
    for url in urls[:5]:
        _last_challenged = False
        html_text = fetch_html(url)
        if not html_text:
            if _last_challenged:
                challenged += 1
            continue
        meta = parse_tracklist_meta(html_text)
        tracks = parse_tracklist(html_text)
        if not tracks:
            continue
        cand = {"title": meta["title"], "duration": meta["duration"],
                "tracks": tracks, "url": url, "html": html_text}
        verdict = TM.score_candidate(target, cand)
        cand["match"] = verdict
        if best is None or verdict["score"] > best["match"]["score"]:
            best = cand
        if verdict["tier"] == "definitive":
            break

    if not best:
        if challenged and not _LOGGED_IN:
            return _ret({"ok": False, "challenged": challenged,
                         "error": ("страница(ы) за анти-бот капчей — нужен логин "
                                   "1001TL (tl1001-email/tl1001-password), анонимно "
                                   "эти сеты недоступны")})
        return _ret({"ok": False, "error": "fetch/parse failed for all candidates"})
    m = best["match"]
    if m["tier"] == "reject":
        return _ret({"ok": False, "error": "no confident match",
                     "url": best["url"], "match": m})
    return _ret({"ok": True, "url": best["url"], "tracks": best["tracks"],
                 "match": m, "source": "1001tracklists"})
