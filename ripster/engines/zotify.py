"""Zotify engine — downloads Spotify URLs using your Spotify account.

Zotify connects to Spotify's internal streaming protocol (not the public Web
API). It needs a Spotify username + password (stored in config.yaml); after
the first successful run, credentials are cached in tokens/zotify_creds.json
and subsequent calls don't need the password over the wire.

Requires Spotify Premium for very_high quality (320kbps OGG Vorbis).
Free accounts max out at normal (96kbps).

Install via Settings → Spotify → Zotify → Установить Zotify.
"""
from __future__ import annotations

import base64
import importlib.util
import json
import re
import sys
from pathlib import Path

from .base import EngineBase, EngineResult, Event, EventKind, LineLevel, _strip_ansi
from .registry import register

_ZOTIFY_BAD_CREDS_MARKER = "ZOTIFY_BAD_CREDS"

# Set to True after user_pass() fails — prevents retrying until user re-auths.
# Cleared when a valid creds file appears (e.g. after OAuth login).
_password_auth_failed: bool = False

_RE_PROGRESS   = re.compile(r'(\d+)\s*/\s*(\d+)')
_RE_DOWNLOADED = re.compile(r'downloaded|saving', re.I)
_RE_SKIP       = re.compile(r'skip', re.I)
_RE_ERROR      = re.compile(r'\berror\b|\bfailed\b|\bexception\b', re.I)
_RE_TOTAL      = re.compile(r'(\d+)\s+(?:songs?|tracks?)', re.I)
_RE_BAD_CREDS  = re.compile(r'BadCredentials|SpotifyAuthenticationException', re.I)
_RE_RATE_LIMIT = re.compile(r'429|rate.?limit', re.I)

_QUALITY_MAP = {
    "auto":      "auto",
    "very_high": "very_high",
    "high":      "high",
    "normal":    "normal",
}

_QUALITIES = [
    {
        "id": "very_high", "label": "Very High",  "engine": "zotify",
        "sub": "320 kbps OGG Vorbis (Spotify Premium)",
        "badge": "320kbps", "color": "#30d158", "bitrate": "320 kbps",
        "ext": "ogg", "req": "premium",
    },
    {
        "id": "high",      "label": "High",       "engine": "zotify",
        "sub": "160 kbps OGG Vorbis",
        "badge": "160kbps", "color": "#ff9f0a", "bitrate": "160 kbps",
        "ext": "ogg", "req": "free",
    },
    {
        "id": "normal",    "label": "Normal",     "engine": "zotify",
        "sub": "96 kbps OGG Vorbis",
        "badge": "96kbps", "color": "#636366", "bitrate": "96 kbps",
        "ext": "ogg", "req": "free",
    },
    {
        "id": "auto",      "label": "Auto",       "engine": "zotify",
        "sub": "Лучшее доступное для аккаунта",
        "badge": "AUTO",   "color": "#0a84ff", "bitrate": "auto",
        "ext": "ogg", "req": "none",
    },
]


def _base_dir() -> Path:
    base = Path(sys.argv[0]).resolve().parent if sys.argv else Path(".").resolve()
    return base


def _creds_path() -> Path:
    return _base_dir() / "tokens" / "zotify_creds.json"


def _cfg_path() -> Path:
    return _base_dir() / "tokens" / "zotify_cfg.json"


def is_installed() -> bool:
    return importlib.util.find_spec("zotify") is not None


def _delete_creds() -> None:
    """Remove expired credential cache so Zotify re-authenticates on next run."""
    p = _creds_path()
    if p.exists():
        try:
            p.unlink()
        except OSError:
            pass


def _seed_creds_from_password(username: str, password: str) -> None:
    """Pre-write a user/pass credential file so librespot's stored_file() has
    something to load. Zotify always calls stored_file() regardless of whether
    --username/--password flags are passed; without this seed file it falls back
    to empty credentials → BadCredentials."""
    p = _creds_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    creds_b64 = base64.b64encode(password.encode("utf-8")).decode("ascii")
    p.write_text(
        json.dumps({"username": username, "credentials": creds_b64,
                    "type": "AUTHENTICATION_USER_PASS"}, indent=2),
        encoding="utf-8",
    )


def _write_zotify_cfg(config: dict) -> Path:
    """Write a zotify config JSON and return its path."""
    save_path = (config.get("zotify-save-path")
                 or config.get("save-path")
                 or "downloads")
    Path(save_path).mkdir(parents=True, exist_ok=True)

    creds = _creds_path()
    creds.parent.mkdir(parents=True, exist_ok=True)

    quality = _QUALITY_MAP.get(config.get("zotify-quality", "very_high"), "very_high")

    cfg = {
        "CREDENTIALS_LOCATION": str(creds),
        "ROOT_PATH":            str(Path(save_path).resolve()),
        "ROOT_PODCAST_PATH":    str(Path(save_path).resolve()),
        "DOWNLOAD_FORMAT":      config.get("zotify-format", "ogg"),
        "DOWNLOAD_QUALITY":     quality,
        "SKIP_EXISTING":        True,
        "PRINT_SPLASH":         False,
        "PRINT_SKIPS":          True,
        "PRINT_DOWNLOAD_PROGRESS": True,
        "PRINT_ERRORS":         True,
        "PRINT_DOWNLOADS":      True,
    }
    cfg_p = _cfg_path()
    cfg_p.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return cfg_p


@register
class ZotifyEngine(EngineBase):
    name = "zotify"

    def qualities(self) -> list[dict]:
        return list(_QUALITIES)

    def build_cmd(self, url: str, quality: str, config: dict) -> list[str]:
        global _password_auth_failed
        username = (config.get("zotify-username") or "").strip()
        password = (config.get("zotify-password") or "").strip()
        creds    = _creds_path()

        if creds.exists():
            # Valid (or at least present) creds file — clear the failed flag
            _password_auth_failed = False
        elif _password_auth_failed:
            # user_pass() already failed this session — stop immediately, don't retry
            raise ValueError(
                f"{_ZOTIFY_BAD_CREDS_MARKER}: пароль Spotify не принят — "
                "войди через OAuth в Settings → Spotify → Zotify"
            )
        elif not (username and password):
            raise ValueError(
                f"{_ZOTIFY_BAD_CREDS_MARKER}: нет учётных данных Spotify — "
                "войди через Settings → Spotify → Zotify"
            )

        cfg_p = _write_zotify_cfg({**config, "zotify-quality": quality})
        cmd = [sys.executable, "-m", "zotify", "--config-location", str(cfg_p)]
        if username:
            cmd += ["--username", username]
        if password:
            cmd += ["--password", password]
        cmd.append(url)
        return cmd

    def iter_events(self, line: str, *, progress: tuple[int, int]):
        global _password_auth_failed
        clean = _strip_ansi(line)
        if _RE_BAD_CREDS.search(clean):
            _password_auth_failed = True
            _delete_creds()
            yield Event(
                kind=EventKind.FATAL,
                message=f"{_ZOTIFY_BAD_CREDS_MARKER}: пароль Spotify не принят — войди через OAuth в Settings → Spotify → Zotify",
                level=LineLevel.ERROR,
            )
            return
        yield from super().iter_events(clean, progress=progress)

    def classify_line(self, line: str) -> str:
        l = line.lower()
        if _RE_BAD_CREDS.search(line):  return "error"
        if _RE_RATE_LIMIT.search(line): return "warn"
        if _RE_ERROR.search(l):         return "error"
        if _RE_SKIP.search(l):          return "warn"
        if _RE_DOWNLOADED.search(l):    return "success"
        return "stdout"

    def parse_progress(self, line: str, current: int, total: int) -> tuple[int, int]:
        # "Downloading N Songs" — sets total
        m = _RE_TOTAL.search(line)
        if m:
            return 0, int(m.group(1))
        # "N / M" — per-track progress
        m = _RE_PROGRESS.search(line)
        if m:
            return int(m.group(1)), int(m.group(2))
        return current, total

    def is_finished(self, log_text: str, rc: int = -1) -> EngineResult:
        global _password_auth_failed
        if _RE_BAD_CREDS.search(log_text):
            _password_auth_failed = True
            _delete_creds()
            return EngineResult(False, error=f"{_ZOTIFY_BAD_CREDS_MARKER}: пароль Spotify не принят — войди через OAuth")
        ok   = len(re.findall(r'downloaded|saving', log_text, re.I))
        skip = len(re.findall(r'skip', log_text, re.I))
        err  = len(re.findall(r'\berror\b.*track|\bfailed\b.*track', log_text, re.I))
        if ok > 0:
            return EngineResult(success=True, tracks_ok=ok, tracks_err=err)
        if skip > 0 and rc == 0:
            return EngineResult(success=True, tracks_ok=0, tracks_err=0)
        if rc == 0 and log_text.strip():
            # rc=0 but no download markers — treat as skip/done if log has content
            return EngineResult(success=True)
        if rc == 0 and not log_text.strip():
            # Completely silent exit — something went wrong (encoding crash, etc.)
            return EngineResult(False, error="Zotify: нет вывода — возможно rate limit или сбой соединения")
        if "Invalid username or password" in log_text:
            return EngineResult(False, error="Zotify: неверный логин или пароль")
        if "PremiumRequired" in log_text or "premium" in log_text.lower():
            return EngineResult(False, error="Zotify: требуется Spotify Premium для этого качества")
        if _RE_RATE_LIMIT.search(log_text):
            return EngineResult(False, error="Zotify: Spotify API rate limit (429) — подожди несколько минут и попробуй снова")
        if "KeyError" in log_text:
            return EngineResult(False, error="Zotify: ошибка ответа Spotify API — возможно rate limit или временный сбой, повтори позже")
        return EngineResult(False, error="Zotify: нет маркера завершения — проверь логин/пароль")
