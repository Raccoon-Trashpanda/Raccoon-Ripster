"""Shared helpers for streamrip-based engines (Qobuz, Tidal)."""
from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path


def config_dir() -> Path:
    """Streamrip config directory: %APPDATA%\\streamrip on Windows, ~/.config/streamrip elsewhere."""
    if sys.platform.startswith("win"):
        import os
        base = os.environ.get("APPDATA")
        if base:
            return Path(base) / "streamrip"
    return Path.home() / ".config" / "streamrip"


def write_config(toml_text: str) -> Path:
    """Write toml_text to streamrip's config.toml and return its path."""
    d = config_dir()
    d.mkdir(parents=True, exist_ok=True)
    p = d / "config.toml"
    p.write_text(toml_text, encoding="utf-8")
    return p


def find_rip() -> str:
    """Return path to the `rip` binary that belongs to the SAME interpreter the
    app runs on (sys.executable), so streamrip's version matches the importable
    `streamrip` package. Critical: pip puts `rip.exe` next to python.exe in a
    venv (python is in Scripts/), but under a SYSTEM python it lands in the
    `Scripts/` subdir. The old code only checked the adjacent path, so on a
    system-python install it fell through to PATH and picked a STALE rip from a
    different env (e.g. .venv's streamrip 2.0.5, whose config schema lacks
    `disc_subdirectories` → 'unexpected keyword argument' → every Tidal/Qobuz
    download died with 'без треков'). Check both layouts."""
    exe_dir = Path(sys.executable).parent
    for cand in (exe_dir / "rip.exe", exe_dir / "rip",
                 exe_dir / "Scripts" / "rip.exe", exe_dir / "Scripts" / "rip"):
        if cand.exists():
            return str(cand)
    return shutil.which("rip") or "rip"


# ── Shared regex patterns ──────────────────────────────────────────────────────

RE_PERCENT      = re.compile(r'(\d{1,3})%')
RE_TRACK_DONE   = re.compile(r'Downloaded track|✓ Downloaded|Finished .+ downloads?|Done downloading', re.I)
RE_TRACK_START  = re.compile(r'Downloading .+|Added \d+ items|Processing track', re.I)
RE_ERROR        = re.compile(r'\berror\b|\bfailed\b|\bunauthorized\b|\binvalid\b|'
                             r'\bexpired\b|\bforbidden\b', re.I)
RE_CONFIG_MISMATCH = re.compile(r'Need to update config from (\S+) to (\S+)', re.I)


def classify_line(line: str) -> str:
    if RE_ERROR.search(line):      return "error"
    if RE_TRACK_DONE.search(line): return "success"
    return "stdout"


def parse_progress(line: str, current: int, total: int) -> tuple[int, int]:
    if RE_TRACK_DONE.search(line):
        return current + 1, max(total, current + 1)
    m = RE_PERCENT.search(line)
    if m and total == 0:
        return int(m.group(1)), 100
    return current, total


class StreamripMixin:
    """
    Stateful progress mixin for streamrip engines (Qobuz, Tidal).
    Tracks how many tracks have completed so the frontend can show "3/12".

    Replaces the stateless parse_progress: engines using this mixin emit two
    independent PROGRESS events per line:
      - PROGRESS(current=pct, total=100)   — file-level % for the progress bar
      - PROGRESS(current=N_done, total=0)  — N tracks completed (on each track done)

    total=0 is a sentinel meaning "track counter, engine doesn't know total".
    The runner broadcasts both; the frontend uses meta.trackCount as the total.
    """

    def __init__(self):
        self._done: int = 0
        self._track_active: bool = False

    def iter_events(self, line: str, *, progress: tuple[int, int]):
        from .base import Event, EventKind, LineLevel, _strip_ansi
        clean = _strip_ansi(line)
        yield Event(kind=EventKind.LINE, message=clean,
                    level=LineLevel(self.classify_line(clean)))

        if RE_TRACK_DONE.search(clean):
            self._done += 1
            self._track_active = False
            yield Event(kind=EventKind.PROGRESS, current=self._done, total=0)
        elif RE_PERCENT.search(clean):
            pct = int(RE_PERCENT.search(clean).group(1))
            self._track_active = True
            yield Event(kind=EventKind.PROGRESS, current=pct, total=100)
        elif RE_TRACK_START.search(clean) and not self._track_active:
            self._track_active = True
            yield Event(kind=EventKind.PROGRESS, current=5, total=100)


# ── TOML sections shared by every streamrip config ────────────────────────────
# Service-specific sections ([qobuz], [tidal]) and [downloads] are NOT included
# here — each engine writes those with its own credentials and settings.
# [filepaths] and [lastfm] are also service-specific and belong to the engine.

COMMON_TOML_SECTIONS = """\
[deezer]
quality = 2
arl = ""
use_deezloader = false
deezloader_warnings = false

[soundcloud]
quality = 0
client_id = ""
app_version = ""

[youtube]
quality = 0
download_videos = false
video_downloads_folder = ""

[database]
downloads_enabled = false
downloads_path = ""
failed_downloads_enabled = false
failed_downloads_path = ""

[conversion]
enabled = false
codec = "ALAC"
sampling_rate = 48000
bit_depth = 24
lossy_bitrate = 320

[qobuz_filters]
extras = false
repeats = false
non_albums = false
features = false
non_studio_albums = false
non_remaster = false

[artwork]
embed = true
embed_size = "large"
# Embedded (in-audio) cover capped to 1000 px across all services by request —
# uniform tag artwork. The SAVED external cover stays full-size (saved_max_width
# = -1) so the on-disk original is unaffected.
embed_max_width = 1000
save_artwork = true
saved_max_width = -1

[metadata]
set_playlist_to_album = true
renumber_playlist_tracks = true
exclude = []

[cli]
text_output = true
progress_bars = true
max_search_results = 100

[misc]
# Must match the installed streamrip's CURRENT_CONFIG_VERSION, else `rip` tries
# to auto-migrate our generated config (and 2.1.0's migrator crashes). We pin
# streamrip to 2.0.5, whose config version is 2.0.3. NOTE: streamrip 2.1.0 has
# Tidal-client regressions (crashes on "track not found" logging + unhandled
# lyrics-401), so do NOT bump — see CLAUDE.md.
version = "2.0.3"
check_for_updates = false
"""
