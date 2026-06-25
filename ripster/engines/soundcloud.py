"""SoundCloud engine — downloads via the Lucida Node.js library.

Lucida wraps SoundCloud's API directly (no account needed for public tracks).
For HQ (Go+) quality an OAuth token is required.

Install via Settings → SoundCloud → Установить Lucida.
This writes tools/lucida/package.json and runs `npm install` once.

Requirements:
  - Node.js 18+ in PATH
  - ffmpeg in PATH (Lucida uses it for HLS → file conversion)
"""
from __future__ import annotations

import importlib.util
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


def _base_dir() -> Path:
    return Path(sys.argv[0]).resolve().parent if sys.argv else Path(".").resolve()


def _runner_path() -> Path:
    return _base_dir() / "tools" / "lucida" / "runner.mjs"


def _lucida_build() -> Path:
    """The built Lucida entry point — cloned + compiled by the installer."""
    return _base_dir() / "tools" / "lucida" / "lucida-src" / "build" / "index.js"


def is_installed() -> bool:
    return _runner_path().exists() and _lucida_build().exists()


def node_available() -> bool:
    return shutil.which("node") is not None


@register
class SoundcloudEngine(EngineBase):
    name = "soundcloud"

    _QUALITIES = [
        {
            "id": "mp3", "label": "MP3 128", "engine": "soundcloud",
            "sub": "MP3 128kbps — публичные треки, без аккаунта",
            "badge": "128kbps", "color": "#ff5500", "bitrate": "128 kbps",
            "ext": "mp3", "req": "none",
        },
        {
            "id": "hq", "label": "HQ AAC", "engine": "soundcloud",
            "sub": "AAC HQ — требует SoundCloud Go+ и OAuth токен",
            "badge": "HQ", "color": "#ff8800", "bitrate": "256 kbps",
            "ext": "m4a", "req": "premium",
        },
    ]

    def qualities(self) -> list[dict]:
        return self._QUALITIES

    def build_cmd(self, url: str, quality: str, config: dict) -> list[str]:
        runner    = _runner_path()
        save_path = (config.get("soundcloud-save-path")
                     or config.get("save-path")
                     or "downloads")
        Path(save_path).mkdir(parents=True, exist_ok=True)

        cmd = ["node", str(runner), url, f"--output={save_path}"]

        oauth = (config.get("soundcloud-oauth-token") or "").strip()
        if oauth:
            cmd.append(f"--oauth-token={oauth}")

        if quality == "hq":
            cmd.append("--hq")

        # Cover-source override picked in the mix drawer (MixesDB / YouTube art).
        cover = (config.get("_sc_cover_override") or "").strip()
        if cover:
            cmd.append(f"--cover-url={cover}")

        return cmd

    def iter_events(self, line: str, *, progress: tuple[int, int]):
        clean = _strip_ansi(line)
        level = LineLevel(self.classify_line(clean))
        yield Event(kind=EventKind.LINE, message=clean, level=level)

        new_cur, new_tot = self.parse_progress(clean, *progress)
        if (new_cur, new_tot) != progress:
            yield Event(kind=EventKind.PROGRESS, current=new_cur, total=new_tot)

    def classify_line(self, line: str) -> str:
        l = line.lower()
        if "failed" in l and "[" in l:    return "error"
        if "error" in l:                  return "error"
        if "success" in l and "[" in l:   return "success"
        if "summary:" in l:               return "success"
        if "found" in l or "queued" in l: return "info"
        if "downloading" in l:            return "stdout"
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
        Deezer/Qobuz/Apple engines. Each SC download now has its own subfolder."""
        matches = _RE_OUTDIR.findall(log_text or "")
        return matches[-1].strip() if matches else None

    def is_finished(self, log_text: str, rc: int = -1) -> EngineResult:
        m = _RE_SUMMARY.search(log_text)
        # FairPlay CBC m3u8 → ffmpeg "Invalid data" → all tracks fail with the
        # same message. Surface a clearer hint instead of a generic counter.
        drm_block = (
            "Invalid data found when processing input" in log_text
            and "cbcs" in log_text
        )
        if m:
            ok, failed = int(m.group(1)), int(m.group(2))
            if failed == 0:
                return EngineResult(success=True, tracks_ok=ok)
            if drm_block and ok == 0:
                return EngineResult(
                    success=False, tracks_ok=ok, tracks_err=failed,
                    error=(
                        "SC: все треки только в FairPlay-зашифрованном HLS — "
                        "Lucida (ffmpeg) не дешифрует. "
                        "Workaround: ISRC-upgrade (поиск того же трека на "
                        "Apple/Qobuz/Deezer), либо ждать pywidevine/pyplayready downloader."
                    ),
                )
            return EngineResult(
                success=False, tracks_ok=ok, tracks_err=failed,
                error=f"SoundCloud: {failed} трек(ов) не удалось скачать",
            )
        if rc == 0:
            return EngineResult(success=True)
        low = log_text.lower()
        # Lucida не найден / Node отсутствует — реальная проблема установки.
        if ("lucida не найден" in low or "cannot find module" in low
                or "is not recognized" in low or "command not found" in low):
            return EngineResult(
                success=False,
                error="SoundCloud: движок не установлен или нет Node.js. Открой "
                      "Настройки → SoundCloud → «Установить движок» и проверь, что "
                      "установлен Node.js 18+.",
            )
        # Сетевой обрыв (undici «terminated»/fetch failed) — после 3 ретраев в
        # runner.mjs всё равно упало. Это не проблема установки, а сеть/CDN.
        if re.search(r"terminated|fetch failed|econnreset|etimedout|socket hang|"
                     r"network|premature close|und_err", low):
            return EngineResult(
                success=False,
                error="SoundCloud: сетевой обрыв при загрузке (соединение с SoundCloud/CDN "
                      "разорвано). Повтори позже; если повторяется — проверь VPN/сеть.",
            )
        return EngineResult(
            success=False,
            error="SoundCloud: нет маркера завершения — проверь Node.js и установку Lucida",
        )
