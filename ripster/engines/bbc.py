"""BBC Sounds engine — downloads an episode's HLS stream as MP3 320 via yt-dlp.

Unlike every other engine, the ``url`` this receives is NOT the original BBC
Sounds page (``bbc.co.uk/sounds/play/<pid>``) — the runner resolves that to a
fresh HLS m3u8 URL (BBC's MediaSelector tokens are short-lived, ~minutes) in a
preflight step (``runner._bbc_preflight``) *right before* calling
``build_cmd``, and stashes the resolved title/artist into the per-task config
view under ``_bbc_title``/``_bbc_artist`` — mirroring the existing
``_sc_cover_override`` pattern used for SoundCloud's cover picker. This keeps
``EngineBase.build_cmd`` synchronous (its normal contract) while still letting
BBC do async network calls (fetch VPID, then the HLS URL) beforehand.

The subprocess itself is the same yt-dlp + ffmpeg invocation the web BBC tab's
``/api/bbc/download`` already used — this engine just makes it participate in
the shared task queue (progress card, retry, delivery, bot support) instead of
its own bespoke broadcast-only flow.
"""
from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

from .base import EngineBase, EngineResult, Event, EventKind, LineLevel, _strip_ansi
from .registry import register

_RE_PCT   = re.compile(r'\[download\]\s+(\d{1,3}(?:\.\d+)?)%')
_RE_ERROR = re.compile(r'ERROR', re.IGNORECASE)
# `--downloader ffmpeg` means ffmpeg (not yt-dlp's native downloader) does the
# actual fetch, so its own `-stats` line is what carries real progress:
# "size=   1234kB time=00:12:34.56 bitrate= ...". yt-dlp's own [download] N%
# line never appears in this mode.
_RE_FFMPEG_TIME = re.compile(r'time=(\d+):(\d+):(\d+)')


def _safe(s: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', '_', s or '').strip(" .")


def find_yt_dlp() -> str | None:
    found = shutil.which("yt-dlp") or shutil.which("yt-dlp.exe")
    if found:
        return found
    # Fallback: sits next to the running interpreter on some installs.
    candidate = Path(sys.executable).parent / "yt-dlp.exe"
    return str(candidate) if candidate.exists() else None


def ep_dir(save_path: str, artist: str, title: str, pid: str) -> Path:
    """``<save-path>/BBC/{Artist} - {Title}/`` — same layout the web BBC tab
    already used, so an existing library isn't split across two conventions."""
    a = _safe(artist) or "BBC Radio"
    t = _safe(title) or pid
    folder = Path(save_path or "downloads") / "BBC" / f"{a} - {t}"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


@register
class BBCEngine(EngineBase):
    name = "bbc"

    def qualities(self) -> list[dict]:
        return [{"id": "mp3", "label": "MP3 320", "sub": "BBC Sounds stream",
                  "badge": "LOSSY", "color": "#e4003b", "bitrate": "320 kbps",
                  "ext": "mp3", "engine": self.name}]

    def __init__(self):
        self._out_dir: str = ""      # set by build_cmd, read back by extract_save_dir
        self._duration: int = 0      # episode length in seconds, for time=→% math
        self._expected_name: str = ""  # filename build_cmd asked for (see is_finished)
                                      # (a fresh instance is made per task — see registry.get_engine)

    def build_cmd(self, url: str, quality: str, config: dict) -> list[str]:
        # `url` here is the already-resolved HLS m3u8 (see module docstring).
        yt = find_yt_dlp()
        if not yt:
            raise RuntimeError("yt-dlp not found (checked PATH and the interpreter's own folder)")
        title  = config.get("_bbc_title", "") or ""
        artist = config.get("_bbc_artist", "") or ""
        pid    = config.get("_bbc_pid", "") or ""
        self._duration = int(config.get("_bbc_duration") or 0)
        save_path = config.get("save-path") or "downloads"
        out_dir = ep_dir(save_path, artist, title, pid)
        self._out_dir = str(out_dir)
        self._expected_name = f"{_safe(title) or pid}.mp3"
        out = str(out_dir / self._expected_name)
        return [
            yt, "--quiet", "--progress",
            "--downloader", "ffmpeg",
            "--hls-use-mpegts",
            "-x", "--audio-format", "mp3", "--audio-quality", "320K",
            "--add-metadata",
            "--ignore-errors",
            "-o", out,
            url,
        ]

    def iter_events(self, line: str, *, progress: tuple[int, int]):
        clean = _strip_ansi(line)
        level = LineLevel(self.classify_line(clean))
        yield Event(kind=EventKind.LINE, message=clean, level=level)
        new_cur, new_tot = self.parse_progress(clean, *progress)
        if (new_cur, new_tot) != progress:
            yield Event(kind=EventKind.PROGRESS, current=new_cur, total=new_tot)

    def classify_line(self, line: str) -> str:
        if _RE_ERROR.search(line):
            return "error"
        if "[download] 100%" in line or "has already been downloaded" in line:
            return "success"
        return "stdout"

    def parse_progress(self, line: str, current: int, total: int) -> tuple[int, int]:
        # Prefer ffmpeg's own time= (the actual downloader in --downloader
        # ffmpeg mode); fall back to yt-dlp's native [download] N% for the
        # rare case yt-dlp handles the fetch itself.
        m = _RE_FFMPEG_TIME.search(line)
        if m and self._duration:
            secs = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
            pct = min(99, int(secs / self._duration * 100))
            return pct, 100
        m = _RE_PCT.search(line)
        if m:
            return int(float(m.group(1))), 100
        return current, total

    def extract_save_dir(self, log_text: str) -> "str | None":
        return self._out_dir or None

    def _fix_filename(self) -> None:
        """Safety net: for a long HLS master-playlist episode, yt-dlp has been
        observed to ignore our -o template and name the file after the stream's
        own internal rendition name (e.g. 'iptv_hd_abr_v1_nonuk_hls_master.mp3')
        instead — the file lands in the right folder, just under the wrong name.
        Short clips name correctly (verified), so this only fires when needed:
        if our expected filename is missing but exactly one audio file sits in
        the output dir, rename it to what we actually asked for."""
        if not self._out_dir or not self._expected_name:
            return
        try:
            d = Path(self._out_dir)
            wanted = d / self._expected_name
            if wanted.exists():
                return
            candidates = [p for p in d.iterdir() if p.is_file() and p.suffix.lower() == ".mp3"]
            if len(candidates) == 1:
                candidates[0].rename(wanted)
        except Exception:
            pass   # cosmetic fix only — never let a rename failure fail the task

    def is_finished(self, log_text: str, rc: int = -1) -> EngineResult:
        if rc == 0:
            self._fix_filename()
            return EngineResult(success=True, tracks_ok=1)
        return EngineResult(success=False, tracks_err=1,
                            error="yt-dlp exited non-zero — check the log for the ERROR line")
