"""BBC LIVE engine — пишет эфир worldwide-потока нужной длительности.

Движок-близнец ``bbc`` по контракту (build_cmd → subprocess → события →
is_finished), но источник другой: не on-demand HLS выпуска, а живой эфир
канала (``ripster/bbc_live_channels.py``) — единственный звук BBC с настоящей
полосой до ~20 кГц (AAC-LC 320). On-demand версия того же эфира — 96..102 кбит/с
HE-AAC, см. HANDOFF_2026-09-19_qoder_session_MASTER.md §4.

Пишем ``ffmpeg -c copy`` в .m4a: поток и так AAC, а любое перекодирование было
бы вторым lossy поверх честных 320.

url/конфиг: как у ``bbc``, планировщик кладёт ``_bbc_live_channel`` и
``_bbc_duration`` в per-task view конфига (runner.py), поэтому build_cmd
остаётся синхронным.
"""
from __future__ import annotations

import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from .base import EngineBase, EngineResult, Event, EventKind, LineLevel, _strip_ansi
from .registry import register
from ripster import bbc_live_channels as _CH
from ripster.engines.bbc import _safe, ep_dir

_RE_FFMPEG_TIME = re.compile(r"time=(\d+):(\d+):(\d+)")
# Класс поломки, который нельзя лечить повтором: эфир не открылся.
_RE_STREAM_DOWN = re.compile(
    r"Server returned (4\d{2}|5\d{2})|Failed to open (input|playlist)|"
    r"Input/output error|Protocol not on stream|error reading |: (404|410|403) Forbidden",
    re.IGNORECASE)
_MIN_KEEP_RATIO = 0.2       # меньше этого запись считается неудачной, а не «недолитой»


@register
class BBCLiveEngine(EngineBase):
    name = "bbc_live"

    def __init__(self):
        self._out_dir: str = ""
        self._expected: Path | None = None
        self._duration: int = 0
        self._elapsed: int = 0
        self._channel: str = ""
        self.abort_reason: str = ""   # контракт ProcessRunner: непустая строка глушит процесс

    def qualities(self) -> list[dict]:
        return [{"id": "live320", "label": "AAC-LC 320 · эфир", "sub": "BBC live worldwide",
                  "badge": "LOSSY", "color": "#e4003b", "bitrate": "320 kbps",
                  "ext": "m4a", "engine": self.name}]

    def build_cmd(self, url: str, quality: str, config: dict) -> list[str]:
        channel  = str(config.get("_bbc_live_channel") or "").strip()
        duration = int(config.get("_bbc_duration") or 0)
        if not channel:
            raise RuntimeError("Для записи эфира не указан канал")
        if not _CH.known(channel):
            raise RuntimeError(f"Неизвестный канал эфира: {channel}")
        stream = _CH.stream_url(channel)
        if not stream:
            raise RuntimeError(f"Нет адреса потока для канала {_CH.label(channel)}")
        if duration <= 0:
            raise RuntimeError("Не задана длительность эфира")
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("ffmpeg не найден в PATH — запись эфира невозможна")

        self._duration = duration
        self._channel = channel
        title = _safe(str(config.get("_bbc_title") or "")) or \
            f"{_safe(_CH.label(channel)) or channel} " + \
            datetime.now(timezone.utc).strftime("%Y-%m-%d %H%M")
        pid = str(config.get("_bbc_pid") or "").strip()
        out_dir = ep_dir(str(config.get("save-path") or "downloads"),
                         _CH.label(channel), title, pid or channel)
        self._out_dir = str(out_dir)
        self._expected = out_dir / f"{title}.m4a"

        return [
            ffmpeg, "-hide_banner", "-loglevel", "info", "-progress", "pipe:2",
            "-timeout", "15000000",                # 15 с на сетевой паузе
            "-reconnect", "1", "-reconnect_at_eof", "1",
            "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
            "-i", stream,
            "-vn", "-sn", "-dn",
            "-c", "copy",
            "-t", str(duration),
            "-f", "mp4", "-movflags", "+faststart",
            "-y", str(self._expected),
        ]

    def iter_events(self, line: str, *, progress: tuple[int, int]):
        clean = _strip_ansi(line)
        # -progress пишет машиночитаемые пары key=value — они не для консоли.
        if re.fullmatch(r"[a-z_]+=\S.*", clean or ""):
            key, _, val = clean.partition("=")
            if key == "out_time_ms":
                try:
                    self._elapsed = max(0, int(val)) // 1_000_000
                except ValueError:
                    return
                if self._duration:
                    yield Event(kind=EventKind.PROGRESS,
                                current=min(99, int(self._elapsed / self._duration * 100)),
                                total=100)
            return
        level = LineLevel(self.classify_line(clean))
        yield Event(kind=EventKind.LINE, message=clean, level=level)
        if level is LineLevel.ERROR and self._elapsed < 10 and _RE_STREAM_DOWN.search(clean):
            # Эфир не открылся вовсе: тянуть процесс и жечь автоповторы бессмысленно.
            if not self.abort_reason:
                self.abort_reason = (f"эфир «{_CH.label(self._channel)}» не открылся "
                                     f"(поток недоступен) — в другое время может выйти")
        new_cur, new_tot = self.parse_progress(clean, *progress)
        if (new_cur, new_tot) != progress:
            yield Event(kind=EventKind.PROGRESS, current=new_cur, total=new_tot)

    def classify_line(self, line: str) -> str:
        if "[error]" in line.lower() or "Invalid data" in line:
            return "error"
        return "stdout"

    def parse_progress(self, line: str, current: int, total: int) -> tuple[int, int]:
        m = _RE_FFMPEG_TIME.search(line)
        if m and self._duration:
            secs = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
            return min(99, int(secs / self._duration * 100)), 100
        return current, total

    def extract_save_dir(self, log_text: str) -> "str | None":
        return self._out_dir or None

    def is_finished(self, log_text: str, rc: int = -1) -> EngineResult:
        got = self._expected if self._expected and self._expected.exists() else None
        size = got.stat().st_size if got else 0
        written = f" {size // 1024} КБ" if size else ""
        if rc == 0 and got and size > 10_000:
            return EngineResult(success=True, tracks_ok=1, quality_actual="live320")
        if self.abort_reason:
            # Вердикт движка (эфир не открылся / поток встал) — он точнее любого
            # общего «ffmpeg exited»; раннер кладёт его в result.error как есть.
            return EngineResult(success=False, tracks_err=1, error=self.abort_reason)
        if got and self._duration and self._elapsed >= self._duration * _MIN_KEEP_RATIO:
            # Обрыв в середине: то, что записано, — настоящий кусок эфира.
            return EngineResult(success=False, tracks_ok=0, tracks_err=1,
                                error=f"эфир оборвался на {self._elapsed} с "
                                      f"из {self._duration}{written} — файл сохранён")
        return EngineResult(success=False, tracks_err=1,
                            error=f"запись эфира не удалась (ffmpeg rc={rc}){written} — "
                                  "поток мог быть недоступен или сеть оборвалась")
