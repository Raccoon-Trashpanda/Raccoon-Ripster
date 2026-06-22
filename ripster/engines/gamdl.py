"""glomatico/gamdl engine."""
from __future__ import annotations
import re
import shutil
import subprocess
import sys
from pathlib import Path
from .base import EngineBase, EngineResult
from .registry import register

_QUALITIES = [
    {"id":"alac",        "codec":"alac",        "label":"ALAC",      "sub":"Apple Lossless",         "badge":"LOSSLESS","color":"#c084a0","bitrate":"≤1411 kbps",    "ext":"m4a","req":"cookies"},
    {"id":"atmos",       "codec":"ec3",         "label":"Atmos EC-3","sub":"Dolby Atmos EC-3",       "badge":"SPATIAL", "color":"#9090c8","bitrate":"2448–2768 kbps","ext":"m4a","req":"cookies"},
    {"id":"aac",         "codec":"aac",         "label":"AAC 256",   "sub":"audio-stereo 256 kbps",  "badge":"LOSSY",   "color":"#EF9F27","bitrate":"256 kbps",      "ext":"m4a","req":"cookies"},
    {"id":"aac-legacy",  "codec":"aac-legacy",  "label":"AAC Legacy","sub":"legacy AAC",             "badge":"LOSSY",   "color":"#EF9F27","bitrate":"~256 kbps",     "ext":"m4a","req":"cookies"},
    {"id":"aac-binaural","codec":"aac-binaural","label":"Binaural",  "sub":"audio-stereo-binaural",  "badge":"3D",      "color":"#9090c8","bitrate":"~256 kbps",     "ext":"m4a","req":"cookies"},
    {"id":"aac-downmix", "codec":"aac-downmix", "label":"Downmix",   "sub":"audio-stereo-downmix",   "badge":"STEREO",  "color":"#6a6a8a","bitrate":"~256 kbps",     "ext":"m4a","req":"cookies"},
    {"id":"mv",          "codec":"mv",          "label":"MV 4K",     "sub":"music video up to 4K",   "badge":"4K",      "color":"#c084a0","bitrate":"up to 4K",      "ext":"mp4","req":"cookies"},
    {"id":"ask",         "codec":"ask",         "label":"Auto",      "sub":"gamdl picks best codec", "badge":"AUTO",    "color":"#3ecfaa","bitrate":"best avail.",   "ext":"m4a","req":"cookies"},
]

_RE_FINISH      = re.compile(r"Finished with (\d+) error", re.I)
_RE_SKIP        = re.compile(r"Skipping .+: (?:API Error|Requested format)")
_RE_API1002     = re.compile(r'"status":-1002')
_RE_DRM_KEYERR  = re.compile(r"KeyError.*AUDIO-SESSION-KEY-IDS", re.I)

# yt-dlp segment noise
_RE_NOISY  = re.compile(
    r'^\[(?:download|ExtractAudio|Merger|MoveFiles|mp4decrypt|FixupM4a|FixupM3u8|hlsnative)\]'
    r'|\bDownloading\s+(?:fragment|segment)\b',
    re.I,
)
_RE_DL_PCT = re.compile(r'\[download\]\s+(\d+(?:\.\d+)?)\s*%')

# Module-level flag cache — populated once per process from `gamdl --help`.
_FLAG_CACHE: set[str] | None = None


def _venv_gamdl() -> str:
    """Prefer gamdl from the current venv over any system-wide install."""
    venv = Path(sys.executable).with_name("gamdl")
    if not venv.exists():
        venv = venv.with_suffix(".exe")
    if venv.exists():
        return str(venv)
    return shutil.which("gamdl") or "gamdl"


def _get_flags() -> set[str]:
    global _FLAG_CACHE
    if _FLAG_CACHE is not None:
        return _FLAG_CACHE
    try:
        r = subprocess.run([_venv_gamdl(), "--help"], capture_output=True, text=True, timeout=10)
        _FLAG_CACHE = set(re.findall(r"--([a-z][a-z0-9-]+)", r.stdout + r.stderr))
    except Exception:
        _FLAG_CACHE = set()
    return _FLAG_CACHE


def _flag(name: str, *args) -> list[str]:
    """Return [--name, *args] only if the flag exists in this gamdl version."""
    known = _get_flags()
    if not known or name in known:
        return [f"--{name}"] + [str(a) for a in args]
    return []


@register
class GamdlEngine(EngineBase):
    name = "gamdl"

    def qualities(self) -> list[dict]:
        return [{**q, "engine": self.name} for q in _QUALITIES]

    def build_cmd(self, url: str, quality: str, config: dict) -> list[str]:
        exe = _venv_gamdl()
        base_dir = Path(sys.argv[0]).parent if sys.argv else Path(".")

        cmd = [exe]

        # Auth: wrapper mode or cookies.
        # Video (mv) and AAC decrypt fine with gamdl's bundled L3 CDM + cookies —
        # NO wrapper needed — and the wrapper is frequently down. So for those
        # cookies-friendly qualities we always prefer cookies when a cookies file
        # is present, regardless of the wrapper toggle. ALAC/Atmos lossless still
        # need the wrapper (L3 can't fetch those keys) — but the smart Apple router
        # sends those to the AMD engine anyway, so gamdl effectively always uses
        # cookies here. See ripster/apple_router.py.
        is_mv = quality == "mv"
        _COOKIES_OK = {"mv", "aac", "aac-legacy", "ask"}
        cookies = (config.get("gamdl-cookies-path") or "").strip() or str(base_dir / "cookies.txt")
        cookies_friendly = quality in _COOKIES_OK and Path(cookies).is_file()
        use_wrapper = bool(config.get("gamdl-use-wrapper")) and not cookies_friendly
        if use_wrapper:
            cmd += _flag("use-wrapper")
            cmd += _flag("wrapper-account-url", config.get("gamdl-wrapper-account-url", "http://127.0.0.1:30020"))
            cmd += _flag("wrapper-decrypt-ip",  config.get("decrypt-port", "127.0.0.1:10020"))
            cmd += _flag("wrapper-m3u8-ip",     config.get("m3u8-port",    "127.0.0.1:20020"))
        else:
            cmd += ["--cookies-path", cookies]

        # Output path
        cmd += ["--output-path", config.get("save-path", "./downloads")]

        # Codec priority
        codec_map = {
            "alac": "alac", "atmos": "ec3", "aac": "aac",
            "aac-legacy": "aac-legacy", "aac-binaural": "aac-binaural",
            "aac-downmix": "aac-downmix",
        }
        codec = codec_map.get(quality)
        if codec:
            cmd += _flag("song-codec-priority", codec)

        # Download mode
        dl_mode = config.get("gamdl-download-mode", "ytdlp")
        cmd += _flag("download-mode", dl_mode)
        if dl_mode == "nm3u8dlre":
            cmd += _flag("nm3u8dlre-path", config.get("gamdl-nm3u8dlre-path", "N_m3u8DL-RE"))

        # Cover
        if config.get("save-cover-to-folder"):
            cmd += _flag("save-cover")
        # Embedded cover pinned to 1000 px across services by request (was 1200).
        cmd += _flag("cover-size",   str(config.get("gamdl-cover-size", 1000)))
        cmd += _flag("cover-format", config.get("gamdl-cover-format", "jpg"))

        # Lyrics
        if config.get("gamdl-no-synced-lyrics"):
            cmd += _flag("no-synced-lyrics")
        elif config.get("gamdl-lyrics-only"):
            cmd += _flag("synced-lyrics-only")
        else:
            cmd += _flag("synced-lyrics-format", config.get("gamdl-synced-lyrics-format", "lrc"))

        # File templates
        cmd += _flag("album-folder-template",     config.get("gamdl-album-template", "{album_artist}/{album}"))
        cmd += _flag("single-disc-file-template", config.get("gamdl-file-template", "{track:02d} {title}"))

        # MV options
        if is_mv:
            cmd += _flag("music-video-remux-mode", config.get("gamdl-mv-remux-mode", "ffmpeg"))
            cmd += _flag("music-video-resolution", config.get("gamdl-mv-resolution", "1080p"))
            cmd += _flag("ffmpeg-path",            config.get("gamdl-ffmpeg-path", "ffmpeg"))

        # Boolean flags
        if config.get("gamdl-overwrite"):          cmd += _flag("overwrite")
        if config.get("gamdl-save-playlist"):      cmd += _flag("save-playlist")
        if config.get("gamdl-use-album-date"):     cmd += _flag("use-album-date")
        if config.get("gamdl-fetch-extra-tags"):   cmd += _flag("fetch-extra-tags")
        if config.get("gamdl-artist-auto-select"): cmd += _flag("artist-auto-select", "all")

        excl = config.get("gamdl-exclude-tags", "")
        if excl:
            cmd += _flag("exclude-tags", excl)
        trunc = config.get("gamdl-truncate", 100)
        if trunc:
            cmd += _flag("truncate", str(trunc))

        lang = config.get("language", "en-US")
        if lang:
            cmd += _flag("language", lang)

        # Tool paths
        _mp4d_local = str(base_dir / "tools" / ("mp4decrypt.exe" if sys.platform == "win32" else "mp4decrypt"))
        mp4d = config.get("mp4decrypt-path") or _mp4d_local
        cmd += _flag("mp4decrypt-path", mp4d)
        cmd += _flag("mp4box-path", config.get("mp4box-path", "MP4Box"))

        cmd += _flag("no-config-file")
        cmd.append(url)
        return cmd

    def iter_events(self, line: str, *, progress: tuple[int, int]):
        from .base import Event, EventKind, LineLevel, _strip_ansi
        clean = _strip_ansi(line)
        # Suppress yt-dlp segment noise, but still forward % to progress bar
        if _RE_NOISY.search(clean):
            m = _RE_DL_PCT.search(clean)
            if m:
                yield Event(kind=EventKind.PROGRESS, current=int(float(m.group(1))), total=100)
            return
        yield from super().iter_events(clean, progress=progress)

    def classify_line(self, line: str) -> str:
        l = line.lower()
        if re.search(r"\berror\b|exception|traceback", l):   return "error"
        if re.search(r"\bwarning\b|skipping|unavailable", l): return "warn"
        if "finished" in l or "saving" in l:                  return "success"
        return "stdout"

    def parse_progress(self, line: str, current: int, total: int) -> tuple[int, int]:
        m = re.search(r"\[Track\s+(\d+)/(\d+)\]", line)
        if m:
            return int(m.group(1)), int(m.group(2))
        return current, total

    def is_finished(self, log_text: str, rc: int = -1) -> EngineResult:
        if _RE_DRM_KEYERR.search(log_text):
            return EngineResult(False, error="ALAC без wrapper не работает в gamdl 3.x — включи wrapper в Settings → gamdl или переключись на zhaarey")
        m = _RE_FINISH.search(log_text)
        if m:
            errs = int(m.group(1))
            if _RE_API1002.search(log_text):
                return EngineResult(False, error="-1002: ALAC требует wrapper — включи gamdl-use-wrapper или используй zhaarey")
            if errs:
                return EngineResult(False, tracks_err=errs, error=f"{errs} треков не удалось скачать")
            return EngineResult(True, tracks_err=0)
        return EngineResult(False, error="unexpected finish")
