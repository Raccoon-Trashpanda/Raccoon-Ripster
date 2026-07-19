# -*- coding: utf-8 -*-
"""Autonomous Spotify WEB-PLAYER access-token getter (the token api-partner accepts).

WHY this exists
---------------
The librespot/keymaster bearer the old keeper minted is REJECTED by Spotify's
download API (`api-partner GraphQL getTrack` → 401). The token that endpoint DOES
accept is the web-player access token you get from the `sp_dc` cookie via
`https://open.spotify.com/api/token` (the modern TOTP flow the browser uses).

THE BLOCKER (measured 2026-07-12 from this box)
-----------------------------------------------
  * `get_access_token` → HTTP 403 "URL Blocked" (Varnish) — Spotify blocks our
    RUSSIAN egress IP outright.
  * `api/token` + TOTP → HTTP 400 `totpVerExpired` — Spotify rotated the TOTP secret.
So a working autonomous token from THIS IP is impossible without:
  1. a NON-RU egress (set `spotify-proxy` in config → this tool routes through it), and
  2. a CURRENT TOTP secret (set `spotify-totp-secret`/`spotify-totp-ver` in config when
     Spotify rotates it; community trackers publish the value).

With those two present, this runs fully autonomously (no browser). Without them it
returns non-zero and the caller keeps the previous token / falls back to convert-first.

Usage:  python tools/spotify_web_token.py <orpheus_dir>
Writes a non-anonymous bearer to <orpheus_dir>/config/spotify-token.txt on success.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import struct
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(ROOT, "config.yaml")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def _cfg(key: str, default: str = "") -> str:
    try:
        txt = open(CONFIG, encoding="utf-8").read()
        m = re.search(rf"^{re.escape(key)}:\s*(.+)$", txt, re.M)
        return m.group(1).strip().strip("'\"") if m else default
    except Exception:
        return default


def _opener():
    proxy = _cfg("spotify-proxy")
    if proxy:
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    return urllib.request.build_opener()


def _totp(server_time_s: int) -> tuple[str, str]:
    """Return (totp, totpVer). Secret/version are config-overridable so the owner
    can drop in a fresh one the moment Spotify rotates it — no code change."""
    ver = _cfg("spotify-totp-ver", "5")
    # Secret: base32 string OR a JSON list of ints (the raw cipher). Config wins.
    raw = _cfg("spotify-totp-secret")
    if raw.startswith("["):
        joined = "".join(str(int(x)) for x in json.loads(raw))
        secret_b32 = base64.b32encode(joined.encode()).decode().rstrip("=")
    elif raw:
        secret_b32 = raw
    else:
        # last-known default (expired 2025-10 from this IP — override via config)
        cipher = [12, 56, 76, 33, 88, 44, 88, 33, 78, 78, 11, 66, 22, 22, 55, 69, 54]
        secret_b32 = base64.b32encode("".join(map(str, cipher)).encode()).decode().rstrip("=")
    key = base64.b32decode(secret_b32 + "=" * ((8 - len(secret_b32) % 8) % 8))
    counter = server_time_s // 30
    h = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    o = h[-1] & 0x0F
    code = (struct.unpack(">I", h[o:o + 4])[0] & 0x7FFFFFFF) % 1000000
    return f"{code:06d}", ver


def main() -> int:
    odir = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "orpheus")
    sp_dc = _cfg("spotify-sp-dc")
    if not sp_dc:
        print("[web-token] no spotify-sp-dc in config", flush=True)
        return 2
    opener = _opener()
    hdr = {"User-Agent": UA, "Cookie": f"sp_dc={sp_dc}", "App-Platform": "WebPlayer",
           "Accept": "application/json", "Origin": "https://open.spotify.com",
           "Referer": "https://open.spotify.com/"}

    def get(url):
        req = urllib.request.Request(url, headers=hdr)
        with opener.open(req, timeout=15) as r:
            return r.status, r.read().decode("utf-8", "ignore")

    server_time = int(time.time())
    totp, ver = _totp(server_time)
    url = (f"https://open.spotify.com/api/token?reason=transport&productType=web_player"
           f"&totp={totp}&totpServer={totp}&totpVer={ver}&ts={server_time}")
    try:
        st, body = get(url)
    except Exception as e:
        print(f"[web-token] request failed: {str(e)[:160]}", flush=True)
        return 3
    try:
        data = json.loads(body)
    except Exception:
        print(f"[web-token] non-JSON (HTTP {st}): {body[:160]}", flush=True)
        return 3
    tok = data.get("accessToken")
    if not tok:
        print(f"[web-token] no token (HTTP {st}): {json.dumps(data)[:200]}", flush=True)
        return 3
    if data.get("isAnonymous"):
        print("[web-token] token is ANONYMOUS (sp_dc not honoured / rejected) — "
              "won't work for downloads", flush=True)
        return 4
    out = os.path.join(odir, "config", "spotify-token.txt")
    with open(out, "w", encoding="utf-8") as f:
        f.write(tok)
    print(f"[web-token] wrote {len(tok)}-char NON-anonymous bearer (sp_dc web token)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
