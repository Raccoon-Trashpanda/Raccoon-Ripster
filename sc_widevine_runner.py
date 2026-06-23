"""Ripster SoundCloud Widevine downloader — drop-in replacement for Lucida
for DRM-protected ("encrypted-hls" / CENC) tracks.

Usage (called by `engines/sc_widevine.py` via subprocess):

  python sc_widevine_runner.py <url> [--output=<dir>] [--oauth-token=<tok>]
                                     [--hq] [--api=http://127.0.0.1:7799]
                                     [--cookie=<ripster-session=...>]

Pipeline per track:
  1. Resolve via Ripster's /api/stream/soundcloud/{id}?prefer=ctr → m3u8 + license_token + artwork
  2. Fetch m3u8, parse PSSH (Widevine) + init segment + media segments
  3. Open Widevine session (pywidevine, L3 device.wvd) → get challenge → POST to
     /api/sc_license?token=... (Ripster proxies to SC's license server)
  4. Parse the license → 16-byte content key
  5. Download init + every segment, concatenate → encrypted.mp4
  6. mp4decrypt --key KID:KEY encrypted.mp4 decrypted.m4a (Bento4)
  7. Embed tags + cover via mutagen
  8. Cleanup

Output lines match the Lucida format so SoundcloudEngine.parse_progress /
classify_line / is_finished can reuse the same matchers:
  Found Track: Artist - Title
  Found Album: Artist - Title
  Queued N tracks for download...
  [N/M] Downloading: Title
  [N/M] Success: Title - Artist
  [N/M] Failed: Title - error
  Summary: N Success, M Failed. Output dir: /path
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import httpx

try:
    from pywidevine.cdm import Cdm
    from pywidevine.device import Device
    from pywidevine.pssh import PSSH
except ImportError as e:
    print(f"Error: pywidevine not installed: {e}", flush=True)
    sys.exit(2)


# ─── Helpers ───────────────────────────────────────────────────────────────────

def _sanitize(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*\r\n]+', '_', (name or 'track')).strip().rstrip('. ') or 'track'


def _print(msg: str) -> None:
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:
        # Windows console is cp1252 — never let a stray glyph kill a download
        enc = sys.stdout.encoding or "utf-8"
        sys.stdout.buffer.write(msg.encode(enc, "replace") + b"\n")
        sys.stdout.flush()


def _resolve_wvd(base: Path) -> Optional[Path]:
    # 1. Explicit override
    env_p = os.environ.get("RIPSTER_WVD_PATH", "").strip()
    if env_p and Path(env_p).is_file():
        return Path(env_p)
    # 2. tools/widevine/device.wvd (default)
    default = base / "tools" / "widevine" / "device.wvd"
    if default.is_file():
        return default
    return None


def _find_mp4decrypt(base: Path) -> Optional[Path]:
    for cand in (base / "AppleMusicDecrypt" / "mp4decrypt.exe",
                 base / "tools" / "mp4decrypt.exe",
                 Path(shutil.which("mp4decrypt") or "")):
        if cand and cand.is_file():
            return cand
    return None


# ─── SC API resolution (direct, no Ripster proxy needed) ──────────────────────

_SC_API = "https://api-v2.soundcloud.com"
_SC_LICENSE = "https://license.media-streaming.soundcloud.cloud/playback/widevine"


def _sc_headers(oauth: str) -> dict:
    h = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/124.0.0.0 Safari/537.36"}
    if oauth:
        h["Authorization"] = (oauth if oauth.lower().startswith("oauth ")
                              else f"OAuth {oauth}")
    return h


async def _scrape_client_id(c: httpx.AsyncClient) -> str:
    """Scrape a working client_id from soundcloud.com's JS bundles."""
    r = await c.get("https://soundcloud.com/")
    scripts = re.findall(r'<script[^>]+src="(https://[^"]+\.js)"', r.text)
    for url in reversed(scripts):
        try:
            jr = await c.get(url)
            m = re.search(r'(?:client_id|clientId)\s*[=:]\s*"([0-9A-Za-z]{28,40})"',
                          jr.text)
            if m:
                return m.group(1)
        except Exception:
            continue
    raise RuntimeError("Cannot scrape SoundCloud client_id")


async def _resolve_track_stream(c: httpx.AsyncClient, tid: int, cid: str,
                                 oauth: str) -> Optional[dict]:
    """Hit /tracks/{id}, pick ctr-encrypted-hls transcoding, exchange for m3u8.
    Returns {url, license_token, artwork} or None."""
    h = _sc_headers(oauth)
    r = await c.get(f"{_SC_API}/tracks/{tid}", params={"client_id": cid}, headers=h)
    if r.status_code != 200:
        _print(f"  /tracks/{tid} → {r.status_code}")
        return None
    j = r.json() or {}
    trans = ((j.get("media") or {}).get("transcodings") or [])
    enc, plain = None, False
    for proto in ("ctr-encrypted-hls", "cbc-encrypted-hls"):
        enc = next((t for t in trans
                    if (t.get("format") or {}).get("protocol") == proto), None)
        if enc:
            break
    if not enc:
        # Non-DRM track: pick a plain stream like the browser extension does —
        # prefer hq + audio/mp4 + progressive. No CDM/decrypt needed.
        def _rank(t):
            f = t.get("format") or {}
            return (t.get("quality") == "hq",
                    "mp4" in (f.get("mime_type") or ""),
                    f.get("protocol") == "progressive")
        cands = [t for t in trans
                 if (t.get("format") or {}).get("protocol") in ("hls", "progressive")
                 and not t.get("snipped")]
        if cands:
            enc, plain = max(cands, key=_rank), True
    if not enc:
        _print(f"  track {tid}: no usable transcoding (DRM-only with no key, or unavailable)")
        return None
    sr = await c.get(enc["url"], params={"client_id": cid}, headers=h)
    if sr.status_code != 200:
        _print(f"  transcoding exchange → {sr.status_code}: {sr.text[:120]}")
        return None
    d = sr.json() or {}
    art = j.get("artwork_url") or (j.get("user") or {}).get("avatar_url", "")
    return {
        "url":           d.get("url", ""),
        "license_token": d.get("licenseAuthToken", ""),
        "artwork":       art,
        "plain":         plain,
    }


async def _resolve_input_url(url: str, oauth: str) -> tuple[str, list[dict]]:
    """Given a SoundCloud URL (track / set / playlist), return:
      (label, [ {id, title, artist, duration, artwork, permalink_url}, ... ])
    """
    # SoundCloud's /resolve only understands canonical soundcloud.com URLs — the
    # mobile/www host (m.soundcloud.com, mobile.soundcloud.com) returns 404.
    url = re.sub(r"^(https?://)(m|mobile|www)\.soundcloud\.com",
                 r"\1soundcloud.com", url.strip(), flags=re.I)
    timeout = httpx.Timeout(connect=4, read=8, write=8, pool=8)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as c:
        cid = await _scrape_client_id(c)
        # on.soundcloud.com / app.goo.gl share links are HTTP redirects to the
        # canonical soundcloud.com/<user>/<track> URL. /resolve 404s on the short
        # form, so follow the redirect first.
        if re.match(r"^https?://(on\.soundcloud\.com|soundcloud\.app\.goo\.gl)/", url, re.I):
            try:
                rr = await c.get(url, headers=_sc_headers(oauth))
                final = str(rr.url).split("?")[0]
                if "soundcloud.com/" in final and "on.soundcloud.com" not in final:
                    url = final
            except Exception:
                pass
        params = {"url": url, "client_id": cid}
        r = await c.get(f"{_SC_API}/resolve", params=params, headers=_sc_headers(oauth))
        if r.status_code != 200:
            raise RuntimeError(f"SC /resolve → {r.status_code}: {r.text[:160]}")
        data = r.json() or {}
        kind = data.get("kind")
        if kind == "track":
            return "Track", [{
                "id":         data.get("id"),
                "title":      data.get("title", ""),
                "artist":     (data.get("user") or {}).get("username", "") or "Unknown",
                "duration":   (data.get("duration") or 0) / 1000,
                "artwork":    data.get("artwork_url") or
                              (data.get("user") or {}).get("avatar_url", ""),
                "permalink":  data.get("permalink_url", ""),
                "release_date": (data.get("release_date") or
                                 data.get("display_date") or "")[:10],
                "genre":      data.get("genre", "") or "",
            }]
        if kind in ("playlist", "system-playlist"):
            # Resolve stub tracks via /tracks?ids=
            raw = data.get("tracks") or []
            stub_ids = [str(t.get("id")) for t in raw if t.get("id")
                        and not (t.get("title") or t.get("user"))]
            resolved: dict[int, dict] = {}
            for i in range(0, len(stub_ids), 50):
                ids = ",".join(stub_ids[i:i+50])
                params2 = {"ids": ids, "client_id": cid}
                rr = await c.get(f"{_SC_API}/tracks", params=params2,
                                 headers=_sc_headers(oauth))
                if rr.status_code == 200:
                    for t in (rr.json() or []):
                        resolved[t.get("id")] = t
            out: list[dict] = []
            for t in raw:
                full = resolved.get(t.get("id"), t)
                if not (full.get("title") and full.get("id")):
                    continue
                out.append({
                    "id":         full.get("id"),
                    "title":      full.get("title", ""),
                    "artist":     (full.get("user") or {}).get("username", "") or "Unknown",
                    "duration":   (full.get("duration") or 0) / 1000,
                    "artwork":    full.get("artwork_url") or
                                  (full.get("user") or {}).get("avatar_url", "") or
                                  data.get("artwork_url", ""),
                    "permalink":  full.get("permalink_url", ""),
                    "release_date": (full.get("release_date") or
                                     full.get("display_date") or "")[:10],
                    "genre":      full.get("genre", "") or "",
                })
            # Album-level title/artist for the folder
            return "Album", out
        raise RuntimeError(f"Unsupported SC kind: {kind}")


# ─── m3u8 parsing ──────────────────────────────────────────────────────────────

_RE_PSSH      = re.compile(
    r'#EXT-X-(?:SESSION-)?KEY:METHOD=SAMPLE-AES[^"\n]*?URI="data:[^"]*?base64,([A-Za-z0-9+/=]+)"'
    r'[^"\n]*?KEYFORMAT="urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed"',
    re.I,
)
_RE_INIT      = re.compile(r'#EXT-X-MAP:URI="([^"]+)"')
_RE_SEGMENT   = re.compile(r'^(https?://[^\s\n]+)$', re.M)
_RE_KEY_ID    = re.compile(r'KEYID=0x([0-9a-fA-F]{32})')


def _parse_m3u8(text: str) -> dict:
    """Return {pssh_b64, kid_hex, init_url, segment_urls}."""
    m_pssh = _RE_PSSH.search(text)
    if not m_pssh:
        raise RuntimeError("Widevine PSSH not found in m3u8 — track may not be Widevine-encrypted")
    m_init = _RE_INIT.search(text)
    if not m_init:
        raise RuntimeError("Init segment URI not found in m3u8")
    m_kid = _RE_KEY_ID.search(text)
    segments = [u for u in _RE_SEGMENT.findall(text)
                if u != m_init.group(1)]
    return {
        "pssh_b64":     m_pssh.group(1),
        "kid_hex":      (m_kid.group(1).lower() if m_kid else ""),
        "init_url":     m_init.group(1),
        "segment_urls": segments,
    }


# ─── Widevine handshake ────────────────────────────────────────────────────────

async def _widevine_key_remote(wrapper_url: str, oauth: str, pssh_b64: str,
                                license_token: str) -> tuple[str, str]:
    """Delegate the Widevine handshake to a peer Ripster wrapper.

    Lets a user without a local .wvd use a friend's instance (the same way
    Apple users share wm.wol.moe). The remote does the CDM work, we get
    (kid_hex, key_hex) back over a single HTTP call.
    """
    url = wrapper_url.rstrip("/") + "/api/wv-wrapper/key"
    timeout = httpx.Timeout(connect=5, read=20, write=20, pool=20)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as c:
        r = await c.post(url, json={
            "pssh_b64": pssh_b64,
            "license_token": license_token,
            "oauth": oauth or "",
        })
    if r.status_code != 200:
        raise RuntimeError(f"Wrapper {wrapper_url}: HTTP {r.status_code} — {r.text[:120]}")
    j = r.json() or {}
    if not j.get("ok"):
        raise RuntimeError(f"Wrapper: {j.get('error', 'unknown')}")
    return j["kid_hex"], j["key_hex"]


async def _widevine_key(oauth: str, pssh_b64: str, license_token: str,
                        device_path: Path) -> tuple[str, str]:
    """Run the Widevine challenge/response directly against SC's license server
    and return (kid_hex, key_hex). license_token is short-lived (≤2 min)."""
    device = Device.load(device_path)
    cdm = Cdm.from_device(device)
    session_id = cdm.open()
    try:
        pssh = PSSH(pssh_b64)
        challenge = cdm.get_license_challenge(session_id, pssh, privacy_mode=False)
        headers = {
            "Content-Type": "application/octet-stream",
            "Origin":       "https://soundcloud.com",
            "Referer":      "https://soundcloud.com/",
            **_sc_headers(oauth),
        }
        timeout = httpx.Timeout(connect=5, read=15, write=15, pool=15)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as c:
            r = await c.post(_SC_LICENSE,
                             params={"license_token": license_token},
                             content=challenge, headers=headers)
        if r.status_code != 200:
            raise RuntimeError(f"SC license server: HTTP {r.status_code} — {r.text[:160]}")
        cdm.parse_license(session_id, r.content)
        keys = [k for k in cdm.get_keys(session_id) if k.type == "CONTENT"]
        if not keys:
            raise RuntimeError("No CONTENT key returned by license server (device may be revoked)")
        k = keys[0]
        # UUID has .hex property (not method); bytes has .hex() method
        kid_hex = k.kid.hex if hasattr(k.kid, "hex") and not callable(k.kid.hex) else k.kid.hex()
        key_hex = k.key.hex()
        return kid_hex, key_hex
    finally:
        try: cdm.close(session_id)
        except Exception: pass


# ─── Segment download + concatenate ────────────────────────────────────────────

async def _seg_get(c: "httpx.AsyncClient", url: str, *, attempts: int = 3,
                   wall: float = 45.0) -> bytes:
    """GET one segment with a HARD wall-clock cap + retries.

    httpx's ``read`` timeout only bounds a single socket read, so a slow-trickle
    CDN (a few bytes every <read>s) can hold a connection open for minutes with no
    new output — the runner's 300s no-output watchdog then kills the whole set
    mid-download (see errors.log: a 12h, 4278-segment set died right after one
    segment stalled). ``asyncio.wait_for`` enforces a real per-segment ceiling;
    transient failures retry with a short backoff before giving up."""
    last: Exception | None = None
    for n in range(1, attempts + 1):
        try:
            r = await asyncio.wait_for(c.get(url), timeout=wall)
            r.raise_for_status()
            return r.content
        except Exception as e:                       # noqa: BLE001 — retry anything
            last = e
            if n < attempts:
                await asyncio.sleep(min(2.0 * n, 5.0))
    raise RuntimeError(f"segment download failed after {attempts} tries: {last}")


def _env_int(name: str, default: int) -> int:
    """Read a positive int from an env var, falling back to a default.

    Used for the concurrency knobs below so they can be retuned WITHOUT a code
    change — important for the self-update story: a tuning tweak must never
    require shipping a new build."""
    try:
        return max(1, int(os.environ.get(name, "") or default))
    except (TypeError, ValueError):
        return default


# How many HLS segments to fetch concurrently. SoundCloud's CDN serves each
# segment independently, so a long DJ set (1000s of ~6s segments) that used to
# download strictly one-at-a-time now fetches a small window in parallel — a
# ~Nx wall-clock win — while still WRITING them in strict order (the output is a
# raw byte concatenation, so order is mandatory). Kept modest to stay polite to
# the CDN and bounded in memory (at most _SEG_CONCURRENCY segment bodies held).
_SEG_CONCURRENCY = _env_int("SC_SEG_CONCURRENCY", 6)

# How many TRACKS of a set to download concurrently. A long DJ set or album is
# many independent tracks; downloading a few at once (each itself segment-
# parallel) is the "parallel-super" win over one-at-a-time. Kept modest: each
# track opens its own CDN window (_SEG_CONCURRENCY) plus one license/CDM
# request, so the real connection count is ~_TRACK_CONCURRENCY × _SEG_CONCURRENCY
# — fast but polite, not a hammer on the CDN or the Widevine wrapper. Drop to 1
# (SC_TRACK_CONCURRENCY=1) for strictly one-track-at-a-time if a Widevine wrapper
# dislikes concurrent license requests.
_TRACK_CONCURRENCY = _env_int("SC_TRACK_CONCURRENCY", 3)


class _SetProgress:
    """Shared, honest progress bar across tracks downloading in parallel.

    Each track reports its own [0..1] fraction; the overall bar = (sum of all
    tracks' fractions) / track count, printed as a single ``[pct/100]`` token
    that SoundcloudEngine.parse_progress already reads. Without a shared tracker
    every parallel track would compute the bar off its own index and fight over
    the value, making it lurch around."""

    def __init__(self, total: int):
        self.total = max(1, total)
        self._frac: dict[int, float] = {}

    def report(self, idx: int, done: int, total_s: int) -> None:
        self._frac[idx] = (done / total_s) if total_s else 0.0
        self._emit(done, total_s)

    def finish(self, idx: int) -> None:
        self._frac[idx] = 1.0
        self._emit(1, 1)

    def _emit(self, done: int, total_s: int) -> None:
        overall = sum(self._frac.values()) / self.total
        pct = max(0, min(100, round(overall * 100)))
        # Single bracket only — the engine takes the FIRST [N/M] on a line, so we
        # must NOT also print a track-counter bracket on this line.
        _print(f"  segment {done}/{total_s} [{pct}/100]")


async def _download_concat(init_url: str, segments: list[str], out: Path,
                            on_progress) -> None:
    """Download init.mp4 + segments → concatenate into a single fragmented MP4.

    Segments are fetched in bounded-concurrency windows for speed but written in
    their original order (the file is a concatenation, so order is mandatory).
    Each window waits for its slowest segment before the next starts — combined
    with _seg_get's per-segment wall-clock cap, a single stuck CDN segment can't
    hang the whole set."""
    timeout = httpx.Timeout(connect=4, read=20, write=20, pool=20)
    limits  = httpx.Limits(max_connections=_SEG_CONCURRENCY * 2,
                           max_keepalive_connections=_SEG_CONCURRENCY)
    n = len(segments)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True,
                                 limits=limits) as c:
        with out.open("wb") as f:
            f.write(await _seg_get(c, init_url))
            for start in range(0, n, _SEG_CONCURRENCY):
                window = segments[start:start + _SEG_CONCURRENCY]
                chunks = await asyncio.gather(*(_seg_get(c, u) for u in window))
                for b in chunks:
                    f.write(b)
                on_progress(min(start + len(window), n), n)


async def _download_plain(stream_url: str, out: Path, on_progress) -> None:
    """Download a NON-DRM SoundCloud stream → out (no CDM, no decrypt).
    Handles HLS (m3u8 → concat EXT-X-MAP init + segments) and progressive
    (the URL is the audio file itself). This is the browser-extension path."""
    timeout = httpx.Timeout(connect=4, read=20, write=20, pool=20)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as c:
        r = await c.get(stream_url)
        r.raise_for_status()
        if r.content[:7] == b"#EXTM3U":
            text = r.text
            m_init = _RE_INIT.search(text)
            segs = [u for u in _RE_SEGMENT.findall(text)
                    if not (m_init and u == m_init.group(1))]
            n = len(segs)
            with out.open("wb") as f:
                if m_init:
                    f.write(await _seg_get(c, m_init.group(1)))
                # Same bounded-concurrency windowing as _download_concat: fetch a
                # window in parallel, write in order, heartbeat per window.
                for start in range(0, n, _SEG_CONCURRENCY):
                    window = segs[start:start + _SEG_CONCURRENCY]
                    chunks = await asyncio.gather(*(_seg_get(c, u) for u in window))
                    for b in chunks:
                        f.write(b)
                    on_progress(min(start + len(window), n), n)
        else:
            out.write_bytes(r.content)  # progressive: response IS the file


# ─── mp4decrypt + tagging ─────────────────────────────────────────────────────

def _mp4decrypt(mp4_path: Path, kid_hex: str, key_hex: str,
                mp4decrypt_exe: Path, out: Path) -> None:
    cmd = [str(mp4decrypt_exe), "--key", f"{kid_hex}:{key_hex}",
           str(mp4_path), str(out)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=180,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if r.returncode != 0:
        raise RuntimeError(f"mp4decrypt failed (rc={r.returncode}): "
                           f"{(r.stderr or r.stdout)[:200]}")
    if not out.is_file() or out.stat().st_size < 1024:
        raise RuntimeError("mp4decrypt produced no/empty output")


def _remux_faststart(path: Path) -> None:
    """Convert the decrypted fragmented (CMAF) MP4 into a standard moov+faststart
    MP4 so every player reports the right duration and can seek. No-op when
    ffmpeg is absent or the remux fails — the fragmented file still plays."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return
    tmp = path.with_suffix(".remux.m4a")
    try:
        r = subprocess.run(
            [ffmpeg, "-y", "-loglevel", "error", "-i", str(path),
             "-c", "copy", "-movflags", "+faststart", str(tmp)],
            capture_output=True, text=True, timeout=180,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if r.returncode == 0 and tmp.exists() and tmp.stat().st_size > 0:
            tmp.replace(path)
        else:
            tmp.unlink(missing_ok=True)
    except Exception:
        try: tmp.unlink(missing_ok=True)
        except Exception: pass


def _embed_tags(audio_path: Path, meta: dict) -> None:
    """Embed title/artist/album/year/genre + cover via mutagen MP4."""
    try:
        from mutagen.mp4 import MP4, MP4Cover
    except ImportError:
        return
    try:
        mp4 = MP4(str(audio_path))
        mp4["\xa9nam"] = meta.get("title") or ""
        mp4["\xa9ART"] = meta.get("artist") or ""
        if meta.get("album"):
            mp4["\xa9alb"] = meta["album"]
        if meta.get("year"):
            mp4["\xa9day"] = str(meta["year"])
        if meta.get("genre"):
            mp4["\xa9gen"] = meta["genre"]
        if meta.get("comment"):
            mp4["\xa9cmt"] = meta["comment"]
        cover_bytes = meta.get("_cover_bytes")
        if cover_bytes:
            fmt = (MP4Cover.FORMAT_PNG
                   if cover_bytes[:8] == b"\x89PNG\r\n\x1a\n"
                   else MP4Cover.FORMAT_JPEG)
            mp4["covr"] = [MP4Cover(cover_bytes, imageformat=fmt)]
        mp4.save()
    except Exception as e:
        _print(f"  ⚠ tag embed failed: {e}")


async def _fetch_cover(url: str) -> Optional[bytes]:
    if not url:
        return None
    # Upscale "large" → "t500x500" → "original" pattern used across SC
    big = re.sub(r"-(large|t\d+x\d+|small|tiny|mini|crop)\.",
                 "-t500x500.", url)
    try:
        timeout = httpx.Timeout(connect=4, read=8, write=8, pool=8)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as c:
            for u in (big, url):
                r = await c.get(u)
                if r.status_code == 200 and len(r.content) > 1024:
                    return r.content
    except Exception:
        pass
    return None


# ─── Per-track + main ──────────────────────────────────────────────────────────

async def _process_one(idx: int, total: int, t: dict, *, dest_dir: Path,
                       oauth: str, cid: str, device_path: Optional[Path],
                       wrapper_url: str, mp4decrypt_exe: Path,
                       progress: "_SetProgress") -> bool:
    label  = f"[{idx}/{total}]"
    title  = t.get("title", "")
    artist = t.get("artist", "")
    _print(f"{label} Downloading: {title}")
    try:
        timeout = httpx.Timeout(connect=4, read=10, write=10, pool=10)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as c:
            stream = await _resolve_track_stream(c, t["id"], cid, oauth)
            if not stream:
                _print(f"{label} Failed: {title} - resolve returned nothing")
                return False
            m3u8_url = stream.get("url", "")
            license_token = stream.get("license_token", "")
            is_plain = stream.get("plain", False)
            if not m3u8_url:
                _print(f"{label} Failed: {title} - no stream url")
                return False
            if not license_token and not is_plain:
                _print(f"{label} Failed: {title} - DRM track but no key available")
                return False
            m3u8_text = ""
            if license_token:
                r = await c.get(m3u8_url)
                r.raise_for_status()
                m3u8_text = r.text

        tmp_root = dest_dir / f".tmp_{t['id']}"
        tmp_root.mkdir(parents=True, exist_ok=True)
        enc_mp4  = tmp_root / "encrypted.mp4"
        dec_m4a  = tmp_root / "decrypted.m4a"

        def _pp(done, total_s):
            # Heartbeat for the runner's no-output watchdog: a long set (1000s of
            # segments) MUST print regularly or process_runner kills it for "no
            # output 300s" mid-download. Print ~every 0.5% (≈200 lines max) plus
            # the first/last segment. (The old 20%-boundary throttle left an
            # ~800-segment silent gap — e.g. seg 43→856 on a 4278-seg set — that
            # tripped the 300s watchdog: see errors.log right after "segment 42".)
            step = max(1, (total_s or 1) // 200)
            if (done == 1 or done == total_s or done % step == 0) and done not in _pp._seen:
                _pp._seen.add(done)
                # Feed the SHARED tracker: with tracks running in parallel the bar
                # must sum every in-flight track's fraction, not compute off this
                # track's idx (which would make parallel tracks fight over the
                # value). The tracker prints the [pct/100] token the engine reads.
                progress.report(idx, done, total_s)
        _pp._seen = set()

        if license_token:
            # DRM (ctr/CENC) path: PSSH → license → mp4decrypt
            parsed = _parse_m3u8(m3u8_text)
            if wrapper_url:
                kid_hex, key_hex = await _widevine_key_remote(
                    wrapper_url, oauth, parsed["pssh_b64"], license_token)
            elif device_path:
                kid_hex, key_hex = await _widevine_key(
                    oauth, parsed["pssh_b64"], license_token, device_path)
            else:
                raise RuntimeError("No CDM source: provide --wrapper-url or device.wvd")
            await _download_concat(parsed["init_url"], parsed["segment_urls"],
                                    enc_mp4, _pp)
            # mp4decrypt is a BLOCKING subprocess — run it off the event loop so a
            # parallel track's network I/O isn't frozen while this one decrypts.
            await asyncio.to_thread(_mp4decrypt, enc_mp4,
                                    parsed["kid_hex"] or kid_hex, key_hex,
                                    mp4decrypt_exe, dec_m4a)
        else:
            # non-DRM plain stream: download directly, no key/decrypt
            await _download_plain(m3u8_url, dec_m4a, _pp)
        # CMAF → standard moov+faststart so duration/seek work in every player.
        # ffmpeg remux also blocks → thread (same reason as mp4decrypt above).
        await asyncio.to_thread(_remux_faststart, dec_m4a)

        cover = await _fetch_cover(t.get("artwork", ""))
        meta = {
            "title":   title,
            "artist":  artist,
            "year":    (t.get("release_date") or "")[:4],
            "genre":   t.get("genre", ""),
            "comment": f"SoundCloud · {t.get('permalink', '')}",
            "_cover_bytes": cover,
        }
        await asyncio.to_thread(_embed_tags, dec_m4a, meta)

        final_name = _sanitize(f"{idx:02d} {artist} - {title}") + ".m4a"
        final = dest_dir / final_name
        if final.exists():
            try: final.unlink()
            except Exception: pass
        shutil.move(str(dec_m4a), str(final))

        # Cover sidecar (matches gamdl/AMD convention)
        if cover:
            try:
                ext = "png" if cover[:8] == b"\x89PNG\r\n\x1a\n" else "jpg"
                cover_p = dest_dir / f"cover.{ext}"
                if not cover_p.exists():
                    cover_p.write_bytes(cover)
            except Exception:
                pass

        shutil.rmtree(tmp_root, ignore_errors=True)
        progress.finish(idx)   # pin this track's fraction to 1.0 in the shared bar
        _print(f"{label} Success: {title} - {artist}")
        return True
    except Exception as e:
        tb = traceback.format_exc().splitlines()[-3:]
        _print(f"{label} Failed: {title} - {e}")
        for line in tb:
            _print(f"    {line}")
        return False


async def _main_async(args) -> int:
    base = Path(__file__).resolve().parent
    device_path = _resolve_wvd(base)
    wrapper_url = (args.wrapper_url or "").strip().rstrip("/")
    if not device_path and not wrapper_url:
        _print("Error: device.wvd not found AND no --wrapper-url given. "
               "Either place a device at tools/widevine/device.wvd, or point at "
               "a peer Ripster wrapper (Settings → SC → Widevine wrapper URL).")
        return 2
    mp4decrypt_exe = _find_mp4decrypt(base)
    if not mp4decrypt_exe:
        _print("Error: mp4decrypt.exe not found (Bento4). "
               "Should live in AppleMusicDecrypt/ or tools/.")
        return 2

    out_dir = Path(args.output).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        label, tracks = await _resolve_input_url(args.url, args.oauth_token)
    except Exception as e:
        _print(f"Error: SC resolve failed: {e}")
        return 1

    if not tracks:
        _print("Error: no tracks resolved from URL")
        return 1

    # Reuse a single client_id for every track in this batch
    async with httpx.AsyncClient(timeout=httpx.Timeout(connect=4, read=8, write=8, pool=8),
                                  follow_redirects=True) as c:
        cid = await _scrape_client_id(c)

    # Album folder
    if label == "Album" and tracks:
        album_artist = tracks[0]["artist"]
        album_title  = "Album"   # the resolve API didn't give us the set's title here;
                                  # the runner caller already knows it via task meta
        # Heuristic: derive from the URL path slug
        try:
            slug = urlparse(args.url).path.rstrip("/").rsplit("/", 1)[-1]
            album_title = " ".join(w.capitalize() for w in slug.replace("-", " ").split())
        except Exception:
            pass
        _print(f"Found Album: {album_artist} - {album_title}")
        dest = out_dir / _sanitize(f"{album_artist} - {album_title}")
        dest.mkdir(parents=True, exist_ok=True)
    else:
        t = tracks[0]
        _print(f"Found Track: {t['artist']} - {t['title']}")
        # Per-track subfolder so each single download is isolated — without it the
        # track landed FLAT in the shared <base>/soundcloud/<quality>/ folder, mixing
        # every SC task together so the bot/manifest could not tell one task's files
        # apart (delivery failed with "look in Ripster").
        dest = out_dir / _sanitize(f"{t['artist']} - {t['title']}")
        dest.mkdir(parents=True, exist_ok=True)

    _print(f"Queued {len(tracks)} tracks for download...")

    # Parallel-super: download several tracks of the set at once (bounded by a
    # semaphore) instead of one-at-a-time. Each track is ALSO segment-parallel
    # inside _download_concat, and its blocking decrypt/remux runs in a worker
    # thread, so one track's CPU work never stalls another's network I/O. A
    # single track (or set ≤ cap) behaves exactly as before, just via gather.
    prog = _SetProgress(len(tracks))
    sem  = asyncio.Semaphore(_TRACK_CONCURRENCY)

    async def _one(idx: int, t: dict) -> bool:
        async with sem:
            return await _process_one(idx, len(tracks), t,
                dest_dir=dest, oauth=args.oauth_token, cid=cid,
                device_path=device_path, wrapper_url=wrapper_url,
                mp4decrypt_exe=mp4decrypt_exe, progress=prog)

    results = await asyncio.gather(
        *(_one(i, t) for i, t in enumerate(tracks, 1)),
        return_exceptions=True)
    ok   = sum(1 for r in results if r is True)
    fail = len(results) - ok

    _print(f"Summary: {ok} Success, {fail} Failed. Output dir: {dest}")
    return 0 if fail == 0 else 1


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("url")
    p.add_argument("--output", default="downloads")
    p.add_argument("--oauth-token", default="")
    p.add_argument("--hq", action="store_true")
    p.add_argument("--api", default="http://127.0.0.1:7799")
    p.add_argument("--cookie", default="")
    p.add_argument("--wrapper-url", default="",
        help="Remote Ripster wrapper URL (peer w/ a working device.wvd)")
    args = p.parse_args()
    try:
        return asyncio.run(_main_async(args))
    except KeyboardInterrupt:
        _print("Error: cancelled")
        return 130


if __name__ == "__main__":
    sys.exit(main())
