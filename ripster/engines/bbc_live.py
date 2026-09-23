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
from ripster.engines.bbc import _attach_cover, _safe, ep_dir

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
        self._cover: str = ""
        self._expected: Path | None = None
        self._duration: int = 0
        self._elapsed: int = 0
        self._channel: str = ""
        self.abort_reason: str = ""   # контракт ProcessRunner: непустая строка глушит процесс

    def qualities(self) -> list[dict]:
        # «320» здесь — не обещание, а измерение: промер 21.09.2026 по всем десяти
        # каналам — мастер-плейлист live-потока отдаёт 48/96 кбит/с HE-AAC и
        # 128/320 кбит/с AAC-LC, а ffmpeg -c copy с варианта 320000 кладёт в файл
        # 319 937…320 046 бит/с профиля LC. Проверяется ВСЕГДА заново — и перед
        # записью (маршрут /schedule/forecast), и по факту файла (is_finished).
        return [{"id": "live320", "label": "AAC-LC 320 · эфир", "label_key": "qual.bbc_live.live320.label", "label_args": {"codec": "AAC-LC 320"}, "sub": "BBC live worldwide",
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

        # Лестницу канала меряем ПЕРЕД записью: если ступени 320 в ней сейчас нет,
        # пишем лучшую фактическую и называем её числом (verdict по файлу всё
        # равно спрашивает файл, а не этот выбор). Прочитать мастер не удалось —
        # идём по адресу 320, как ходили всегда.
        want = _CH.BITRATE // 1000
        try:
            from ripster import bbc_quality as _q
            pick = _q.best_live_variant(channel, want)
        except Exception:
            pick = {}
        if pick.get("url"):
            stream = pick["url"]
        got_kbps = int(pick.get("kbps") or 0)
        if got_kbps and got_kbps < want:
            print(f"[bbc_live] {_CH.label(channel)}: варианта {want} кбит/с в лестнице "
                  f"нет — пишем {got_kbps} кбит/с {pick.get('codec') or ''}".strip(),
                  flush=True)

        self._duration = duration
        self._channel = channel
        title = _safe(str(config.get("_bbc_title") or "")) or \
            f"{_safe(_CH.label(channel)) or channel} " + \
            datetime.now(timezone.utc).strftime("%Y-%m-%d %H%M")
        pid = str(config.get("_bbc_pid") or "").strip()
        out_dir = ep_dir(str(config.get("save-path") or "downloads"),
                         _CH.label(channel), title, pid or channel)
        self._out_dir = str(out_dir)
        self._cover = str(config.get("_bbc_cover") or "")
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

    def _actual_quality(self, path) -> str:
        """«live320» только если файл действительно 320 кбит/с AAC-LC.

        Поток на то и живой: лестница канала может измениться, а вместе с ней и
        то, что приехало. Мёртвый вариант 320000 ffmpeg не валит — он может отдать
        и менее полный звук, поэтому спрашиваем файл, а не адрес (ripster/
        bbc_quality.py: промер 21.09.2026 — 319 937…320 046 бит/с, профиль LC).
        """
        try:
            from ripster import bbc_quality as _q
            v = _q.verdict(path, _CH.BITRATE // 1000)
            if v.get("state") == "as_promised":
                return "live320"
            if v.get("state") == "below":
                return f"live{v.get('kbps') or 0}k"
        except Exception:
            pass       # не сумели померить — не вешаем на задачу ложный ярлык
        return ""

    def is_finished(self, log_text: str, rc: int = -1) -> EngineResult:
        got = self._expected if self._expected and self._expected.exists() else None
        size = got.stat().st_size if got else 0
        written = f" {size // 1024} КБ" if size else ""
        if rc == 0 and got and size > 10_000:
            _attach_cover(self._out_dir, self._cover)
            return EngineResult(success=True, tracks_ok=1,
                                quality_actual=self._actual_quality(got))
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
