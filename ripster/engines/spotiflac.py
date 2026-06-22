"""SpotiFLAC CLI engine — downloads Spotify URLs as true FLAC, no account needed.

Uses the Nizarberyan/SpotiFLAC CLI (headless build of afkarxyz/SpotiFLAC).
Audio fetched from public third-party APIs (Tidal via hifi-api, Qobuz via
dabmusic.xyz/squid.wtf, Amazon via doubledouble.top/lucida.to) — no user
tokens or subscriptions required.

Binary: tools/spotiflac.exe (Windows) or tools/spotiflac (Linux/Mac).
Download via Settings → Spotify → Install SpotiFLAC.

CLI output format (from main.go):
  Analyzing Spotify URL: ...
  Found Track/Album/Playlist: ...
  Queued N tracks for download...
  [N/M] Success: Title - Artist
  [N/M] Failed: Title - Artist
  Summary: N Success, N Failed. Output dir: /path
"""
from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

from .base import EngineBase, EngineResult, Event, EventKind, LineLevel, _strip_ansi
from .registry import register

_RE_PROGRESS = re.compile(r'\[(\d+)/(\d+)\]')
_RE_QUEUED   = re.compile(r'Queued\s+(\d+)\s+track', re.I)
_RE_SUMMARY  = re.compile(r'Summary:\s*(\d+)\s+Success,\s*(\d+)\s+Failed', re.I)
_RE_MB       = re.compile(r'\rDownloaded:')   # inline \r progress — skip

_RELEASE_URL = (
    "https://github.com/Nizarberyan/SpotiFLAC/releases/download/v1.1.0/"
    "SpotiFLAC{ext}"
)


def _exe_path() -> Path:
    ext  = ".exe" if sys.platform.startswith("win") else ""
    name = f"spotiflac{ext}"
    base = Path(sys.argv[0]).parent if sys.argv else Path(".")
    local = base / "tools" / name
    if local.exists():
        return local
    found = shutil.which("spotiflac")
    return Path(found) if found else local


def download_url() -> str:
    ext = ".exe" if sys.platform.startswith("win") else ""
    return _RELEASE_URL.format(ext=ext)


def is_installed() -> bool:
    return _exe_path().exists()


@register
class SpotiflacEngine(EngineBase):
    name = "spotiflac"

    _QUALITIES = [
        {
            "id": "flac", "label": "FLAC (best)", "engine": "spotiflac",
            "sub": "True lossless — source: Tidal/Qobuz/Amazon (публичный API)",
            "badge": "LOSSLESS", "color": "#30d158", "bitrate": "lossless",
            "ext": "flac", "req": "none",
        },
    ]

    def qualities(self) -> list[dict]:
        return self._QUALITIES

    def build_cmd(self, url: str, quality: str, config: dict) -> list[str]:
        exe       = _exe_path()
        save_path = (config.get("spotiflac-save-path")
                     or config.get("save-path")
                     or "downloads")
        Path(save_path).mkdir(parents=True, exist_ok=True)

        cmd = [str(exe), "-o", save_path]

        concurrency = str(config.get("spotiflac-concurrency", 3))
        cmd += ["-c", concurrency]

        return cmd + [url]

    def iter_events(self, line: str, *, progress: tuple[int, int]):
        # Carriage-return inline progress lines — skip, they don't end with \n
        # and are already consumed by readline() as partial lines.
        clean = _strip_ansi(line)
        if _RE_MB.match(clean.lstrip()):
            return

        level_str = self.classify_line(clean)
        try:
            level = LineLevel(level_str)
        except ValueError:
            level = LineLevel.STDOUT

        yield Event(kind=EventKind.LINE, message=clean, level=level)

        new_cur, new_tot = self.parse_progress(clean, *progress)
        if (new_cur, new_tot) != progress:
            yield Event(kind=EventKind.PROGRESS, current=new_cur, total=new_tot)

    def classify_line(self, line: str) -> str:
        l = line.lower()
        if "failed" in l and "[" in l:     return "error"
        if "error" in l:                   return "error"
        if "success" in l and "[" in l:    return "success"
        if "summary:" in l:                return "success"
        if "queued" in l or "found" in l:  return "info"
        return "stdout"

    def parse_progress(self, line: str, current: int, total: int) -> tuple[int, int]:
        # "Queued N tracks" — sets the total before download starts
        m = _RE_QUEUED.search(line)
        if m:
            return 0, int(m.group(1))
        # "[N/M] Status: ..." — per-track completion
        m = _RE_PROGRESS.search(line)
        if m:
            return int(m.group(1)), int(m.group(2))
        return current, total

    def is_finished(self, log_text: str, rc: int = -1) -> EngineResult:
        m = _RE_SUMMARY.search(log_text)
        if m:
            ok, failed = int(m.group(1)), int(m.group(2))
            if failed == 0:
                return EngineResult(success=True, tracks_ok=ok)
            return EngineResult(
                success=False,
                tracks_ok=ok, tracks_err=failed,
                error=f"SpotiFLAC: {failed} трек(ов) не удалось скачать",
            )
        if rc == 0:
            return EngineResult(success=True)
        return EngineResult(
            success=False,
            error="SpotiFLAC: нет маркера завершения (бинарь не найден или упал)",
        )
