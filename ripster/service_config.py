"""Shared service-to-config helpers used by runner, engines, and download routes."""
from __future__ import annotations

from pathlib import Path

_SVC_PATH_KEYS: dict[str, str] = {
    "deezer":     "deezer-save-path",
    "qobuz":      "qobuz-save-path",
    "tidal":      "tidal-save-path",
    "soundcloud": "soundcloud-save-path",
    "beatport":   "beatport-save-path",
    "orpheus":    "orpheus-save-path",
    "spotify":    "orpheus-save-path",  # OrpheusDL is the only Spotify downloader
    "yandex":     "yandex-save-path",
}

# Quality folder names — what subdirectory to use when quality-subfolders is on.
# Per-service overrides live in _QUALITY_FOLDER_SVC; fallback is _QUALITY_FOLDER.
_QUALITY_FOLDER: dict[str, str] = {
    # Apple Music
    "alac":         "ALAC (Lossless)",
    "atmos":        "Atmos",
    "ac3":          "Atmos",
    "binaural":     "Binaural",
    "aac-binaural": "Binaural",
    "aac-downmix":  "AAC Downmix",
    "downmix":      "AAC Downmix",
    "aac":          "AAC 256",
    "aac-legacy":   "AAC 256",
    "aac-lc":       "AAC 256",
    # Deezer
    "flac":         "FLAC",
    "mp3_320":      "MP3 320",
    "mp3_128":      "MP3 128",
    # Qobuz (numeric IDs)
    "27":           "FLAC HiRes 24-192",
    "7":            "FLAC HiRes 24-96",
    "6":            "FLAC CD 16-44",
    "5":            "MP3 320",
    # Tidal
    "hi_res":       "FLAC HiRes",
    "lossless":     "FLAC CD",
    "low":          "AAC 96",
    # Spotify (OrpheusDL) — hifi = OGG ~320 kbps Premium
    "hifi":         "OGG 320",
    "normal":       "OGG 160",
    # Zotify
    "very_high":    "OGG 320",
    # SoundCloud
    "mp3":          "MP3 128",
    "hq":           "AAC 256",
    # Beatport / other
    "minimum":      "MP3 128",
}


def _quality_folder_name(service: str, quality: str) -> str:
    """Return the normalized folder name for a quality+service combination."""
    svc = (service or "apple").lower()
    qid = (quality or "").lower()
    # "high" means different things per service
    if qid == "high":
        if svc == "tidal":       return "AAC 320"
        if svc == "spotify":     return "OGG 160"
        if svc == "beatport":    return "AAC 256"
        if svc in ("zotify",):   return "OGG 160"
        return "MP3 320"
    # Beatport hifi = FLAC (not ~320 OGG like Spotify)
    if qid == "hifi" and svc == "beatport":
        return "FLAC"
    return _QUALITY_FOLDER.get(qid) or qid or "other"


def get_save_path(config: dict, service: str, quality: str = "") -> str:
    """UNIFIED layout — ONE base path for every service, organized as
    ``<save-path>/<service>/<quality>``:

        <base>/beatport/MP3 320
        <base>/deezer/FLAC
        <base>/apple/ALAC (Lossless)
        <base>/tidal/FLAC CD

    Per-service save paths and the Apple per-codec folders are GONE — this is the
    single source of truth. The runner shadows the result into every engine's
    config view (``_cfg_view``), so an engine's ``build_cmd`` and the runner's
    disk-truth scan always resolve to the SAME directory (no "files not found").
    """
    base = config.get("save-path", "downloads")
    svc  = (service or "apple").lower()
    parts = [base, svc]
    if quality:
        parts.append(_quality_folder_name(svc, quality))
    return str(Path(*parts))


def all_save_paths(config: dict) -> list[str]:
    """Roots to scan for delivered files. Unified layout puts everything under the
    single ``save-path`` (in <service>/<quality> subfolders), so the base is the
    only root. Any legacy per-service paths still set in an old config are included
    so a freshly-migrated install can still find previously-downloaded files."""
    paths: set[str] = {config.get("save-path", "downloads")}
    for key in _SVC_PATH_KEYS.values():          # legacy configs only
        v = config.get(key, "")
        if v:
            paths.add(v)
    return [p for p in paths if p]
