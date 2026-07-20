"""
Service resolution — URL detection, quality defaults, engine routing.

All functions that map URLs / service-names to capabilities live here so
app.py does not need to own domain logic.

Call install(config) once at startup so quality / engine lookups see the
live, mutable config dict without any extra plumbing.
"""
from __future__ import annotations

from urllib.parse import urlparse

_config: dict = {}


def install(config: dict) -> None:
    global _config
    _config = config


# ── Recognised music service hostnames ────────────────────────────────────────

ALLOWED_HOSTS: frozenset[str] = frozenset({
    "music.apple.com",
    "qobuz.com", "www.qobuz.com", "open.qobuz.com",
    "deezer.com", "www.deezer.com", "deezer.page.link",
    "tidal.com", "listen.tidal.com",
    "spotify.com", "open.spotify.com",
    "soundcloud.com", "www.soundcloud.com", "m.soundcloud.com", "on.soundcloud.com",
    "beatport.com", "www.beatport.com",
    "music.yandex.ru", "music.yandex.com", "music.yandex.kz", "music.yandex.by",
    "music.amazon.com", "music.amazon.co.uk", "music.amazon.de", "music.amazon.co.jp",
    "music.amazon.in", "music.amazon.fr", "music.amazon.es", "music.amazon.it",
    "music.amazon.ca", "music.amazon.com.au", "music.amazon.com.br", "music.amazon.com.mx",
    "bbc.co.uk", "www.bbc.co.uk",
})


def validate_url(url: str) -> bool:
    """Return True if *url* belongs to any supported music service."""
    try:
        h = (urlparse(url).hostname or "").lower()
        return any(h == d or h.endswith("." + d) for d in ALLOWED_HOSTS)
    except Exception:
        return False


def detect_service(url: str) -> str:
    """Map a music URL to its service key."""
    u = url.lower()
    if "music.apple.com" in u: return "apple"
    if "qobuz.com"       in u: return "qobuz"
    if "deezer.com"      in u: return "deezer"
    if "deezer.page"     in u: return "deezer"
    if "tidal.com"       in u: return "tidal"
    if "spotify.com"     in u: return "spotify"
    if "soundcloud.com"  in u: return "soundcloud"
    if "beatport.com"    in u: return "beatport"
    if "music.yandex."   in u: return "yandex"
    if "music.amazon."   in u: return "amazon"
    if "bbc.co.uk"       in u: return "bbc"
    return "unknown"


# Apple music-video detection lives in the single Apple-routing source of truth
# (ripster/apple_router.py); re-exported here for backward compatibility.
from ripster.apple_router import is_apple_music_video  # noqa: F401,E402


def default_quality(svc: str) -> str:
    """Return the configured default quality string for a given service."""
    if svc == "spotify" and _config.get("spotify-engine") == "orpheus_spotify":
        return _config.get("orpheus-quality", "hifi")
    # SoundCloud: with a Go+ OAuth token the CDM/Lucida engines unlock AAC 256
    # ("hq"). Default to it so the quality label + save folder match what is
    # actually downloaded — otherwise an AAC-256 file lands in an "MP3 128"
    # folder. Falls back to the free MP3 128 stream when no token is set.
    sc_default = "hq" if (_config.get("soundcloud-oauth-token") or "").strip() else "mp3"
    return {
        "apple":      _config.get("quality", "alac"),
        "qobuz":      _config.get("qobuz-quality", "27"),
        "deezer":     _config.get("deezer-quality", "flac"),
        "tidal":      _config.get("tidal-quality", "lossless"),
        "spotify":    _config.get("quality", "alac"),   # will be converted
        "soundcloud": sc_default,
        "beatport":   _config.get("beatport-quality", "hifi"),
        "yandex":     _config.get("yandex-quality", "flac"),
        "amazon":     _config.get("amazon-quality", "High"),
        "bbc":        "mp3",
    }.get(svc, "alac")


def engine_for_svc(svc: str) -> str:
    """Map a service key to the engine name that should handle it."""
    if svc == "spotify":
        if _config.get("spotify-engine") == "orpheus_spotify":
            return "orpheus_spotify"
    if svc == "beatport":
        return "orpheus_beatport"
    if svc == "soundcloud":
        # Prefer the pywidevine engine when a device file is available — handles
        # DRM-protected tracks (now the majority of SC content) that Lucida
        # cannot decrypt. Falls back to Lucida for the public/non-DRM ones.
        try:
            from ripster.engines.sc_widevine import is_available
            if is_available(_config):
                return "sc_widevine"
        except Exception:
            pass
        return "soundcloud"
    return {
        "apple":      _config.get("engine", "zhaarey"),
        "deezer":     "deezer",
        "qobuz":      "qobuz",
        "tidal":      "tidal",
        "soundcloud": "soundcloud",
        "yandex":     "yandex",
        "amazon":     "amazon",
        "bbc":        "bbc",
    }.get(svc, _config.get("engine", "zhaarey"))
