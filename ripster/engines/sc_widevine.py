"""SoundCloud Widevine engine — pywidevine-based downloader for DRM tracks.

This is a drop-in replacement for the Lucida-based `soundcloud` engine when a
track is CENC-encrypted (which is the default since 2024 for Go+ and most
private/uploader tracks). It runs `sc_widevine_runner.py` as a subprocess so it
shares the same engine lifecycle, output parser, save-dir logic, and queue
plumbing as everything else.

Requirements:
  - pywidevine + a working `tools/widevine/device.wvd`
  - mp4decrypt.exe (already shipped in AppleMusicDecrypt/)
  - Ripster's local API up (the runner calls /api/stream/soundcloud/{id} and
    /api/sc_license against the same server it's a subprocess of)
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
_RE_OUTDIR   = re.compile(r'Output dir:\s*(.+?)\s*$', re.M)
_RE_NO_WVD   = re.compile(r'device\.wvd\s+not\s+found', re.I)
_RE_REVOKED  = re.compile(r'device may be revoked|No CONTENT key returned', re.I)


def _base_dir() -> Path:
    return Path(sys.argv[0]).resolve().parent if sys.argv else Path(".").resolve()


def _runner_path() -> Path:
    return _base_dir() / "sc_widevine_runner.py"


def _wvd_python() -> str:
    """Interpreter for the DRM runner. Prefer the ISOLATED pywidevine venv
    (tools/wvdvenv) — pywidevine needs protobuf>=6.33 which OrpheusDL clobbers to
    3.15.8 in the shared bundled python. Falls back to the current interpreter.
    See the ripster-dependency-versions skill."""
    base = _base_dir()
    for sub in (("Scripts", "python.exe"), ("bin", "python")):
        cand = base / "tools" / "wvdvenv" / sub[0] / sub[1]
        if cand.is_file():
            return str(cand)
    return sys.executable


def _wvd_path(config: dict) -> Path:
    p = (config.get("sc-widevine-device") or "").strip()
    if p and Path(p).is_file():
        return Path(p)
    return _base_dir() / "tools" / "widevine" / "device.wvd"


def is_available(config: dict) -> bool:
    if not _runner_path().is_file():
        return False
    # Either a local .wvd OR a peer wrapper URL counts as available
    if _wvd_path(config).is_file():
        return True
    return bool((config.get("sc-widevine-wrapper-url") or "").strip())


@register
class SoundcloudWidevineEngine(EngineBase):
    name = "sc_widevine"

    _QUALITIES = [
        {"id": "hq",  "label": "AAC 256 (CDM)", "engine": "sc_widevine",
         "sub": "AAC 256 kbps, расшифровка через pywidevine L3 CDM",
         "badge": "HQ-DRM", "color": "#ff5500", "bitrate": "256 kbps",
         "ext": "m4a", "req": "wvd"},
    ]

    def qualities(self) -> list[dict]:
        return self._QUALITIES

    def build_cmd(self, url: str, quality: str, config: dict) -> list[str]:
        runner = _runner_path()
        save_path = (config.get("soundcloud-save-path")
                     or config.get("save-path")
                     or "downloads")
        Path(save_path).mkdir(parents=True, exist_ok=True)

        cmd = [_wvd_python(), "-u", str(runner), url, f"--output={save_path}"]
        oauth = (config.get("soundcloud-oauth-token") or "").strip()
        if oauth:
            cmd.append(f"--oauth-token={oauth}")
        wrapper_url = (config.get("sc-widevine-wrapper-url") or "").strip()
        if wrapper_url:
            cmd.append(f"--wrapper-url={wrapper_url}")
        return cmd

    def working_dir(self) -> str | None:
        return str(_base_dir())

    def iter_events(self, line: str, *, progress: tuple[int, int]):
        clean = _strip_ansi(line)
        yield Event(kind=EventKind.LINE, message=clean,
                    level=LineLevel(self.classify_line(clean)))
        new_cur, new_tot = self.parse_progress(clean, *progress)
        if (new_cur, new_tot) != progress:
            yield Event(kind=EventKind.PROGRESS, current=new_cur, total=new_tot)

    def classify_line(self, line: str) -> str:
        l = line.lower()
        if "failed" in l and "[" in l:                return "error"
        if l.startswith("error:"):                    return "error"
        if "success" in l and "[" in l:               return "success"
        if l.startswith("summary:"):                  return "success"
        if l.startswith("found ") or "queued" in l:   return "info"
        if "downloading" in l or "segment" in l:      return "stdout"
        return "stdout"

    def parse_progress(self, line: str, current: int, total: int) -> tuple[int, int]:
        m = _RE_QUEUED.search(line)
        if m:
            return 0, int(m.group(1))
        m = _RE_PROGRESS.search(line)
        if m:
            return int(m.group(1)), int(m.group(2))
        return current, total

    def extract_save_dir(self, log_text: str) -> "str | None":
        """Parse the runner's `Summary: … Output dir: <path>` line so the queue
        runner can record _save_dir + write the task-id marker — unified with the
        Lucida `soundcloud` engine. Without this override the base returns None, so
        the manifest never records the per-track folder and the bot delivers
        NOTHING for DRM SoundCloud tracks (which route here, not to `soundcloud`)."""
        matches = _RE_OUTDIR.findall(log_text or "")
        return matches[-1].strip() if matches else None

    def is_finished(self, log_text: str, rc: int = -1) -> EngineResult:
        if _RE_NO_WVD.search(log_text):
            return EngineResult(False, error=(
                "device.wvd не найден. Положи L3 device в tools/widevine/device.wvd. "
                "Инструкция: tools/widevine/README.md"
            ))
        if _RE_REVOKED.search(log_text):
            return EngineResult(False, error=(
                "Widevine device отозван Google или не подходит SC license server. "
                "Попробуй другой .wvd (см. tools/widevine/README.md)."
            ))
        m = _RE_SUMMARY.search(log_text)
        if m:
            ok, failed = int(m.group(1)), int(m.group(2))
            if failed == 0:
                return EngineResult(success=True, tracks_ok=ok)
            return EngineResult(success=False, tracks_ok=ok, tracks_err=failed,
                                error=f"SC Widevine: {failed} трек(ов) не удалось расшифровать")
        if rc == 0:
            return EngineResult(success=True)
        return EngineResult(success=False,
            error="SC Widevine: нет маркера завершения — проверь логи runner'а")
