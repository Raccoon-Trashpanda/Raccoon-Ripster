"""Standalone PKCE OAuth helper for orpheusdl-spotify.

Prints:
  ORPHEUS_AUTH_URL:{url}    — Spotify authorization URL for the browser popup
  ORPHEUS_AUTH_DONE         — credentials saved, ready for downloads
  ORPHEUS_AUTH_FAILED:{msg} — error

Credentials are saved to orpheus/config/credentials.json in the format
that SpotifyAPI._load_existing_credentials() expects.
"""
from __future__ import annotations
import base64, hashlib, json, os, secrets, sys, time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread
from urllib.parse import urlencode, urlparse, parse_qs
import requests

_HERE = Path(__file__).parent.resolve()
_CREDS = _HERE / "config" / "credentials.json"
_CREDS.parent.mkdir(parents=True, exist_ok=True)

CLIENT_ID    = "65b708073fc0480ea92a077233ca87bd"
REDIRECT_URI = "http://127.0.0.1:4381/login"
AUTH_BASE    = "https://accounts.spotify.com/"
SCOPES = [
    "streaming",
    "user-read-email",
    "user-read-private",
    "playlist-read-collaborative",
    "playlist-read-private",
    "user-library-read",
    "user-read-playback-state",
    "user-read-currently-playing",
    "user-read-recently-played",
    "user-read-playback-position",
    "user-top-read",
]

# ── PKCE helpers ─────────────────────────────────────────────────────────────
def _verifier() -> str:
    return secrets.token_urlsafe(64)[:64]

def _challenge(v: str) -> str:
    digest = hashlib.sha256(v.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()

# ── OAuth callback server ─────────────────────────────────────────────────────
_code: list[str] = []
_error: list[str] = []

# Where the browser goes after the callback. Two reasons this is not just a
# "you can close this window" note: (1) the in-window login option has no popup
# to close — the person IS in the Ripster window and must be brought back;
# (2) the old bare English line left users unsure whether anything happened,
# and Ripster's own screen still said "не авторизован" until an app restart.
_RETURN_URL = os.environ.get("RIPSTER_RETURN_URL", "http://127.0.0.1:7799/?spotify_login=ok")


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        qs = parse_qs(urlparse(self.path).query)
        if "code" in qs:
            _code.append(qs["code"][0])
            self._respond("&#10003; Spotify подключён", "Возвращаю в Ripster…",
                          ok=True)
        elif "error" in qs:
            _error.append(qs["error"][0])
            self._respond("Вход не удался", qs["error"][0], ok=False)
        else:
            self._respond("Неожиданный ответ Spotify", "Попробуй войти заново.",
                          ok=False)

    def _respond(self, title: str, msg: str, ok: bool):
        meta = (f"<meta http-equiv='refresh' content='2;url={_RETURN_URL}'>"
                if ok else "")
        body = (
            "<!doctype html><html lang='ru'><head><meta charset='utf-8'>"
            f"<title>{title}</title>{meta}"
            "<style>html,body{height:100%;margin:0;background:#0a0a0c;color:#f0f0f4;"
            "font-family:-apple-system,Segoe UI,sans-serif;display:flex;"
            "align-items:center;justify-content:center;text-align:center}"
            f"h2{{color:{'#1db954' if ok else '#fc3c44'};margin:0 0 8px}}"
            "p{color:#9a9aa4;font-size:13px;line-height:1.6}a{color:#1db954}</style>"
            f"</head><body><div><h2>{title}</h2><p>{msg}</p>"
            f"<p><a href='{_RETURN_URL}'>Открыть Ripster</a></p>"
            "</div></body></html>"
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_): pass  # silence access log


def _run():
    client_id = sys.argv[1] if len(sys.argv) > 1 else CLIENT_ID

    verifier   = _verifier()
    challenge  = _challenge(verifier)

    params = {
        "client_id":             client_id,
        "response_type":         "code",
        "redirect_uri":          REDIRECT_URI,
        "scope":                 " ".join(SCOPES),
        "code_challenge_method": "S256",
        "code_challenge":        challenge,
    }
    auth_url = AUTH_BASE + "authorize?" + urlencode(params)

    # Start callback server
    parsed = urlparse(REDIRECT_URI)
    srv = HTTPServer((parsed.hostname, parsed.port), _Handler)
    t = Thread(target=srv.serve_forever, daemon=True)
    t.start()

    print(f"ORPHEUS_AUTH_URL:{auth_url}", flush=True)

    # Wait for callback (3 min timeout)
    deadline = time.time() + 180
    while not _code and not _error and time.time() < deadline:
        time.sleep(0.4)

    srv.shutdown()

    if _error:
        print(f"ORPHEUS_AUTH_FAILED:{_error[0]}", flush=True)
        sys.exit(1)
    if not _code:
        print("ORPHEUS_AUTH_FAILED:timeout", flush=True)
        sys.exit(1)

    # Exchange code → token
    payload = {
        "client_id":     client_id,
        "grant_type":    "authorization_code",
        "code":          _code[0],
        "redirect_uri":  REDIRECT_URI,
        "code_verifier": verifier,
    }
    resp = requests.post(AUTH_BASE + "api/token", data=payload, timeout=30)
    resp.raise_for_status()
    tok = resp.json()

    # Fetch real Spotify username
    spotify_username = "orpheus_pkce_user"
    try:
        me = requests.get(
            "https://api.spotify.com/v1/me",
            headers={"Authorization": f"Bearer {tok['access_token']}"},
            timeout=10,
        )
        if me.ok:
            spotify_username = me.json().get("id", spotify_username)
    except Exception:
        pass

    # Save in SpotifyAPI-compatible format (scope as space-separated string)
    creds = {
        "access_token":      tok["access_token"],
        "refresh_token":     tok.get("refresh_token", ""),
        "expires_in":        tok.get("expires_in", 3600),
        "token_type":        tok.get("token_type", "Bearer"),
        "scope":             tok.get("scope", " ".join(SCOPES)),
        "spotify_username":  spotify_username,
        "client_id":         client_id,
        "timestamp":         int(time.time() * 1000),
        "issued_at":         time.time(),
    }
    _CREDS.write_text(json.dumps(creds, indent=2), encoding="utf-8")
    print(f"ORPHEUS_AUTH_DONE:{spotify_username}", flush=True)


if __name__ == "__main__":
    try:
        _run()
    except Exception as exc:
        print(f"ORPHEUS_AUTH_FAILED:{exc}", flush=True)
        sys.exit(1)
