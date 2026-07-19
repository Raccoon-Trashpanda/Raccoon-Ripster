"""OrpheusDL-Beatport engine — downloads Beatport URLs via OrpheusDL + orpheusdl-beatport.

Authentication: username + password stored in orpheus/config/settings.json
Quality tiers:
  hifi/lossless  → FLAC 16-bit (requires Beatport Professional subscription)
  high           → AAC 256 kbps (requires Beatport Professional)
  minimum        → AAC 128 kbps (Link subscription)

Module must be cloned to orpheus/modules/beatport/ before first use.
Repo: https://github.com/Dniel97/orpheusdl-beatport
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from .base import EngineBase, EngineResult, Event, EventKind, LineLevel, _strip_ansi
from .registry import register


def _base_dir() -> Path:
    return Path(sys.argv[0]).resolve().parent if sys.argv else Path(".").resolve()

def _orpheus_dir() -> Path:
    return _base_dir() / "orpheus"


def _orpheus_python() -> str:
    """Interpreter for OrpheusDL — prefer the ISOLATED venv (tools/orpheusvenv) so
    OrpheusDL's protobuf==3.15.8 never pollutes the shared bundled python (which
    would break AMD + pywidevine). See the ripster-dependency-versions skill."""
    base = _base_dir()
    for sub in (("Scripts", "python.exe"), ("bin", "python")):
        cand = base / "tools" / "orpheusvenv" / sub[0] / sub[1]
        if cand.is_file():
            return str(cand)
    return sys.executable

def _settings_path() -> Path:
    return _orpheus_dir() / "config" / "settings.json"

def _module_path() -> Path:
    return _orpheus_dir() / "modules" / "beatport"

def _session_path() -> Path:
    return _orpheus_dir() / "config" / "loginstorage.bin"


# ── live access token from OrpheusDL's saved Beatport session ─────────────────
# Beatport has no public metadata API, so queue cards used to come back blank
# ("beatport · <id>", no cover/title). But the AUTHENTICATED catalog API does
# return full metadata, and the download flow already keeps a valid session in
# orpheus/config/loginstorage.bin. Mint a token from it (refreshing via the
# Serato client if expired) so ripster.metadata can enrich Beatport cards the
# same way Tidal does. Cached in-process; re-reads the pickle every ~2 min so it
# picks up tokens OrpheusDL itself refreshed.
_BP_CLIENT_ID = "Zy2K9Wvy6DkUds7g8s1GNMHfk17E5Ch2BWHlyaGY"  # Serato DJ Lite (== beatport_api.py)
_BP_AT_CACHE: dict = {"token": "", "exp": 0.0}

def _read_bp_session() -> dict | None:
    """The beatport module's saved {access_token, refresh_token, expires} dict."""
    import pickle
    try:
        blob = pickle.loads(_session_path().read_bytes())
        return blob["modules"]["beatport"]["sessions"]["default"]["custom_data"]
    except Exception:
        return None

async def _beatport_access_token() -> str:
    """Return a valid Beatport access_token, or '' if no session. Refreshes via
    the Serato client + saved refresh_token when the stored token has expired."""
    import time
    from datetime import datetime
    now = time.time()
    if _BP_AT_CACHE["token"] and now < _BP_AT_CACHE["exp"]:
        return _BP_AT_CACHE["token"]
    sess = _read_bp_session()
    if not sess:
        return ""
    at = sess.get("access_token") or ""
    exp_dt = sess.get("expires")
    try:
        if at and exp_dt and exp_dt > datetime.now():
            _BP_AT_CACHE["token"] = at
            _BP_AT_CACHE["exp"]   = now + 120   # re-check the pickle periodically
            return at
    except Exception:
        pass
    rt = sess.get("refresh_token") or ""
    if not rt:
        return at
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as c:
            r = await c.post("https://api.beatport.com/v4/auth/o/token/",
                             data={"client_id": _BP_CLIENT_ID,
                                   "refresh_token": rt,
                                   "grant_type": "refresh_token"})
        if r.status_code == 200:
            j = r.json()
            _BP_AT_CACHE["token"] = j["access_token"]
            _BP_AT_CACHE["exp"]   = now + max(60, int(j.get("expires_in", 3600)) - 120)
            return _BP_AT_CACHE["token"]
    except Exception:
        pass
    return at


# ── patterns ─────────────────────────────────────────────────────────────────
_RE_DOWNLOADING  = re.compile(r'===\s*Downloading\s+(track|release|playlist|artist)\s+(.+?)\s*(?:\(|===)', re.I)
_RE_TRACK_FILE   = re.compile(r'Downloading track file|Saving\s*:', re.I)
_RE_DONE         = re.compile(r'===\s*Done|Download complete', re.I)
_RE_ERROR        = re.compile(r'\berror\b|\bfailed\b|\bexception\b|\bTraceback', re.I)
_RE_SKIP         = re.compile(r'skip|already exist|ignore', re.I)
_RE_PROGRESS     = re.compile(r'(\d+)\s*/\s*(\d+)')
_RE_AUTH_FAIL    = re.compile(r'invalid.*creden|wrong.*password|login.*fail|auth.*fail|401|403', re.I)
_RE_SUBSCRIPTION = re.compile(r'subscription|professional|upgrade.*plan|higher.*tier', re.I)


_QUALITIES = [
    {
        "id": "hifi",    "label": "FLAC",    "engine": "orpheus_beatport",
        "sub": "FLAC 16-bit · Professional",
        "badge": "FLAC", "color": "#3ecfaa", "bitrate": "lossless",
        "ext": "flac",   "req": "professional",
    },
    {
        "id": "high",    "label": "AAC 256", "engine": "orpheus_beatport",
        "sub": "AAC 256 kbps · Professional",
        "badge": "256k", "color": "#EF9F27", "bitrate": "256 kbps",
        "ext": "aac",    "req": "professional",
    },
    {
        "id": "minimum", "label": "AAC 128", "engine": "orpheus_beatport",
        "sub": "AAC 128 kbps · Link",
        "badge": "128k", "color": "#6a6a8a", "bitrate": "128 kbps",
        "ext": "aac",    "req": "link",
    },
]

_QUALITY_ORPHEUS = {
    "hifi":     "hifi",
    "lossless": "hifi",   # UI / stale queue alias
    "high":     "high",
    "minimum":  "minimum",
}


def is_installed() -> bool:
    return ((_orpheus_dir() / "orpheus.py").exists()
            # inner orpheus/ package must be present too — a partial clone with only
            # orpheus.py crashes at runtime with "ModuleNotFoundError: orpheus.core".
            and (_orpheus_dir() / "orpheus" / "core.py").exists()
            and (_module_path() / "__init__.py").exists())


def is_authenticated(config: dict) -> bool:
    return bool(
        (config.get("beatport-username") or "").strip() and
        (config.get("beatport-password") or "").strip()
    )


def _update_orpheus_settings(quality: str, save_path: str, config: dict) -> None:
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

        # The owner's Beatport account has an active (Pro Plus) subscription, but the
        # module's post-login `get_account()` introspect can transiently return an
        # empty `subscription` (e.g. right after a refresh-token rotation), which
        # makes OrpheusDL raise a FALSE "Account does not have an active 'Link'
        # subscription" and blocks every download. Skip that check — a real lack of
        # entitlement still surfaces later as a stream/territory error, so we lose no
        # honesty, only the false-negative that paralysed Beatport for guests.
        adv = cfg["global"].setdefault("advanced", {})
        adv["disable_subscription_checks"] = True

        covers = cfg["global"].setdefault("covers", {})
        covers["embed_cover"]         = True
        # Embedded (in-audio) cover pinned to 1000×1000 across ALL services.
        covers["main_resolution"]     = 1000
        # Beatport treats every track as its own single, so OrpheusDL writes the
        # external cover named AFTER THE TRACK ("<title>.jpg") — an album of N
        # tracks ends up with N redundant sidecar images. Disable the external
        # cover: art stays embedded in each file, and the app drops ONE folder
        # cover.jpg from the embedded art (_apply_cover_to_folder). No clutter.
        covers["save_external"]       = False

        bp = cfg.setdefault("modules", {}).setdefault("beatport", {})
        username = (config.get("beatport-username") or "").strip()
        password = (config.get("beatport-password") or "").strip()
        if username:
            bp["username"] = username
        if password:
            bp["password"] = password

        sp.write_text(json.dumps(cfg, indent=4, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


@register
class OrpheusBeatportEngine(EngineBase):
    name = "orpheus_beatport"

    def qualities(self) -> list[dict]:
        return list(_QUALITIES)

    def working_dir(self) -> str:
        return str(_orpheus_dir())

    def build_cmd(self, url: str, quality: str, config: dict) -> list[str]:
        if not (_orpheus_dir() / "orpheus.py").exists():
            raise ValueError("OrpheusDL не установлен — перейди в Settings → Beatport")
        if not (_module_path() / "__init__.py").exists():
            raise ValueError(
                "Модуль Beatport не установлен. Клонируй "
                "https://github.com/Dniel97/orpheusdl-beatport в orpheus/modules/beatport/"
            )
        if not is_authenticated(config):
            raise ValueError(
                "BEATPORT_NOT_AUTHED: введи логин и пароль Beatport в Settings → Beatport"
            )

        save_path = config.get("beatport-save-path") or config.get("save-path") or ""
        orpheus_quality = _QUALITY_ORPHEUS.get(quality, "hifi")
        _update_orpheus_settings(orpheus_quality, save_path, config)

        # The bundled embeddable Python runs ISOLATED (sys.flags.isolated==1, a side
        # effect of the ._pth) → it does NOT add the script's directory to sys.path AND
        # ignores PYTHONPATH. So a plain `python orpheus.py` dies with
        # "ModuleNotFoundError: No module named 'orpheus.core'" even though the inner
        # orpheus/ package sits right next to orpheus.py (proven on a bundled install;
        # the dev .venv is non-isolated so it never reproduced). Bootstrap via -c: put
        # the OrpheusDL dir on sys.path, restore argv, then run orpheus.py as __main__.
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
        cmd.append(url)
        return cmd

    def iter_events(self, line: str, *, progress: tuple[int, int]):
        clean = _strip_ansi(line).strip()
        if not clean:
            return

        if "BEATPORT_NOT_AUTHED" in clean or _RE_AUTH_FAIL.search(clean):
            yield Event(
                kind=EventKind.FATAL,
                message="BEATPORT_NOT_AUTHED: неверный логин/пароль Beatport — проверь Settings",
                level=LineLevel.ERROR,
            )
            return

        yield from super().iter_events(clean, progress=progress)

    def classify_line(self, line: str) -> str:
        if _RE_ERROR.search(line):        return "error"
        if _RE_AUTH_FAIL.search(line):    return "error"
        if _RE_SUBSCRIPTION.search(line): return "warn"
        if _RE_SKIP.search(line):         return "warn"
        if _RE_DOWNLOADING.search(line) or _RE_TRACK_FILE.search(line):
            return "success"
        return "stdout"

    def parse_progress(self, line: str, current: int, total: int) -> tuple[int, int]:
        m = _RE_PROGRESS.search(line)
        if m:
            cur, tot = int(m.group(1)), int(m.group(2))
            return max(0, cur - 1), tot
        return current, total

    def is_finished(self, log_text: str, rc: int = -1) -> EngineResult:
        # Territory restriction is a 403 too, but it is NOT an auth failure — must be
        # checked first so it isn't mislabelled "неверный логин" (which sent the bot
        # down the wrong path / looked like a freeze). The track exists but isn't
        # licensed in this Beatport account's region; retrying can't help → no-retry.
        if re.search(r'Territory\s+Restricted|territory.?restrict|region\s+locked|BeatportError.*region|not.*available.*your.*(region|country)', log_text, re.I):
            return EngineResult(False, error="Beatport: трек недоступен в регионе твоего аккаунта "
                                             "(Territory Restricted) — нужен Beatport-аккаунт/прокси в "
                                             "разрешённой стране. Скачать нельзя.")
        if "BEATPORT_NOT_AUTHED" in log_text or _RE_AUTH_FAIL.search(log_text):
            return EngineResult(False, error="BEATPORT_NOT_AUTHED: неверный логин/пароль Beatport")

        # Success/skip markers MUST be checked before the subscription gate:
        # Orpheus prints "Professional subscription detected, allowing high and
        # lossless quality" on EVERY successful run, and _RE_SUBSCRIPTION matches
        # the word "Professional"/"subscription" in it — so checking subscription
        # first flagged real downloads (and skips of already-present files) as a
        # false "subscription required" error. Real subscription-lack errors emit a
        # different message AND produce no "Downloading track file" + rc!=0, so they
        # still reach the gate below.
        downloads = len(re.findall(r'Downloading track file|Saving\s*:', log_text, re.I))
        if downloads > 0:
            errs = len(re.findall(r'\berror\b|\bfailed\b', log_text, re.I))
            return EngineResult(success=True, tracks_ok=downloads, tracks_err=errs)

        if rc == 0 and log_text.strip():
            skips = len(re.findall(r'skip|already exist', log_text, re.I))
            if skips:
                return EngineResult(success=True, tracks_ok=0)
            return EngineResult(success=True)

        if _RE_SUBSCRIPTION.search(log_text):
            return EngineResult(False, error="Требуется подписка Beatport Professional для FLAC/AAC 256")

        if rc == 0 and not log_text.strip():
            return EngineResult(False, error="OrpheusDL: нет вывода — проверь логин Beatport")

        # Surface the REAL exception from a Python traceback instead of a bare
        # "exit code 1" — the last "SomeError: message" line names the actual cause
        # (a connection drop, a parse error, a missing track), which the generic
        # message hid. A bare network error is also flagged so it reads as transient.
        _exc = ""
        for ln in reversed(log_text.splitlines()):
            s = ln.strip()
            if re.match(r'^[A-Za-z_][\w.]*(Error|Exception|Warning):\s', s):
                _exc = s[:200]
                break
        if _exc:
            if re.search(r'Connection|Timeout|terminated|Max retries|ConnectError|'
                         r'temporarily|Read timed out', _exc, re.I):
                return EngineResult(False, error=f"Beatport: сетевой обрыв ({_exc}) — повтори позже.")
            return EngineResult(False, error=f"OrpheusDL Beatport: {_exc}")
        return EngineResult(False, error=f"OrpheusDL Beatport: завершился с кодом {rc}")
