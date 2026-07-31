r"""Out-of-box Spotify login via librespot's PKCE OAuth flow (browser only — no
desktop app, no extension, no token paste).

It produces the SAME durable blob the rest of Ripster already relies on:
orpheus/config/.librespot_cache/reusable_credentials.json — which the OGG keeper
mints Bearers from and which orpheus streams audio with. So a FRESH GitHub clone
authenticates Spotify with one click.

Flow (driven by the /api/spotify/auth/* endpoints):
  1. librespot builds a PKCE auth URL → we write it to .sp_oauth_url.txt and print
     it (the backend relays it to the GUI; the user opens it and logs in).
  2. librespot runs a local callback server on http://127.0.0.1:5588/login to
     catch Spotify's redirect, exchanges the code, and — because store_credentials
     is on — saves the reusable blob to reusable_credentials.json.
  3. We drop .sp_oauth_done.txt on success / .sp_oauth_err.txt on failure.

Run: python tools/spotify_oauth_login.py
"""
from __future__ import annotations

import os
import sys

# librespot's protobuf stubs require the pure-Python parser; set BEFORE import.
os.environ.setdefault("PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION", "python")
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_HERE  = os.path.dirname(os.path.abspath(__file__))
_REPO  = os.path.dirname(_HERE)
_CACHE = os.path.join(_REPO, "orpheus", "config", ".librespot_cache")
_BLOB  = os.path.join(_CACHE, "reusable_credentials.json")
# librespot's Builder.oauth() starts with:
#     if os.path.isfile(self.conf.stored_credentials_file): return self.stored_file(None)
# i.e. when the target blob ALREADY exists it silently skips the whole browser
# flow and reuses the old credential. Since /api/spotify/auth/start deliberately
# leaves the live blob in place (so an abandoned re-login can't log you out),
# every re-login hit that branch: no auth URL was ever emitted, and the UI died
# with "нет auth URL — проверь, свободен ли порт 5588". Logging in as a
# DIFFERENT account was therefore impossible. Fix: run the flow against a
# scratch path nothing else reads, and promote it only once it really succeeds.
_NEW_BLOB  = os.path.join(_CACHE, "reusable_credentials.new.json")
_URL_FILE  = os.path.join(_CACHE, ".sp_oauth_url.txt")
_DONE_FILE = os.path.join(_CACHE, ".sp_oauth_done.txt")
_ERR_FILE  = os.path.join(_CACHE, ".sp_oauth_err.txt")

# Where to send the browser once Spotify has redirected back. The whole point is
# that the person does not end up stranded on a bare "Login successful — you can
# close this window" page (which is also what the in-window login flow needs, as
# there is no popup to close). Overridable so a non-default port still returns.
_RETURN_URL = os.environ.get("RIPSTER_RETURN_URL", "http://127.0.0.1:7799/?spotify_login=ok")


def _success_page(return_url: str) -> str:
    return (
        "<!doctype html><html lang='ru'><head><meta charset='utf-8'>"
        "<title>Spotify подключён</title>"
        f"<meta http-equiv='refresh' content='2;url={return_url}'>"
        "<style>html,body{height:100%;margin:0;background:#0a0a0c;color:#f0f0f4;"
        "font-family:-apple-system,Segoe UI,sans-serif;display:flex;align-items:center;"
        "justify-content:center;text-align:center}a{color:#1db954}"
        "h2{color:#1db954;margin:0 0 8px}p{color:#9a9aa4;font-size:13px;line-height:1.6}</style>"
        "</head><body><div><h2>&#10003; Spotify подключён</h2>"
        "<p>Возвращаю в Ripster…</p>"
        f"<p><a href='{return_url}'>Открыть Ripster вручную</a></p>"
        "</div></body></html>"
    )


def _w(path: str, text: str) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    except Exception:
        pass


def main() -> int:
    os.makedirs(_CACHE, exist_ok=True)
    for p in (_URL_FILE, _DONE_FILE, _ERR_FILE, _NEW_BLOB):
        try:
            os.remove(p)
        except OSError:
            pass

    def url_cb(url: str) -> None:
        _w(_URL_FILE, url)
        print("AUTH_URL " + url, flush=True)

    try:
        from librespot.core import Session
        # store_credentials=True → on a successful login librespot writes the
        # reusable blob to stored_credential_file (the format orpheus reads).
        conf = (Session.Configuration.Builder()
                .set_store_credentials(True)
                .set_stored_credential_file(_NEW_BLOB)
                .set_cache_enabled(False)
                .build())
        # oauth() prints/relays the auth URL, then BLOCKS on the local callback
        # server until the user authorises in the browser. The success page is
        # ours so the browser lands back in Ripster instead of on a dead-end.
        sess = Session.Builder(conf).oauth(url_cb, _success_page(_RETURN_URL)).create()
        try:
            sess.close()
        except Exception:
            pass
        if os.path.isfile(_NEW_BLOB):
            os.replace(_NEW_BLOB, _BLOB)     # promote only a real, fresh login
            _w(_DONE_FILE, "ok")
            print("LOGIN_OK", flush=True)
            return 0
        _w(_ERR_FILE, "login finished but no blob was written")
        print("LOGIN_ERR no blob", flush=True)
        return 1
    except Exception as e:
        _w(_ERR_FILE, str(e))
        print("LOGIN_ERR " + str(e), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
