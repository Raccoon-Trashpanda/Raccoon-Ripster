"""BBC LIVE engine — пишет эфир worldwide-потока нужной длительности.

Движок-близнец ``bbc`` по контракту (build_cmd → subprocess → события →
is_finished), но источник другой: не on-demand HLS выпуска, а живой эфир
канала (``ripster/bbc_live_channels.py``) — единственный звук BBC с настоящей
полосой до ~20 кГц (AAC-LC 320). On-demand версия того же эфира — 96..102 кбит/с
HE-AAC, см. HANDOFF_2026-09-19_qoder_session_MASTER.md §4.

Пишем ``ffmpeg -c copy`` через промежуточный .ts, а в .m4a перекладываем в
конце: поток и так AAC, любое перекодирование было бы вторым lossy поверх
честных 320. Промежуточный транспортный контейнер нужен не для совместимости,
а для устойчивости: у mp4 таблица кадров (moov) дописывается в конце, и
убитая на середине запись была бы нечитаема вовсе; .ts переживает обрыв и
поддаётся спасению (ripster/bbc_live_recovery.py).

С 24.09.2026 запись ПЕРЕД вердиктом «готово» проходит decode-check: двухчасовой
эфир D3A370FB пришёл «успешным» файлом, который не декодировался (7667 строк
ошибок). Обещать человеку «done» по битому файлу движок больше не может:
не прошёл проверку — задача падает с честной причиной, файл остаётся рядом с
меткой «(повреждено)», а оркестратор запускает восстановление.

url/конфиг: как у ``bbc``, планировщик кладёт ``_bbc_live_channel`` и
``_bbc_duration`` в per-task view конфига (runner.py), поэтому build_cmd
остаётся синхронным.
"""
from __future__ import annotations

import re
import shutil
import subprocess
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
# Decode-check порождает служебную строку на старте каждого нового декодера
# («channel element 0.0 duplicate») — это не повреждение.
_MAX_DECODE_ERRORS = 2
_MIN_KEPT_RATIO = 0.98      # rc=0 без полного таймлайна — тоже повреждение


@register
class BBCLiveEngine(EngineBase):
    name = "bbc_live"

    def __init__(self):
        self._out_dir: str = ""
        self._cover: str = ""
        self._expected: Path | None = None
        self._ts: Path | None = None
        self._duration: int = 0
        self._elapsed: int = 0
        self._channel: str = ""
        self._artist: str = ""
        self._title: str = ""
        self._date: str = ""
        self.abort_reason: str = ""   # контракт ProcessRunner: непустая строка глушит процесс
        self.recovery_info: dict = {}  # заполняется при verdict «повреждено»; читает runner

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
        self._channel  = channel
        artist = str(config.get("_bbc_artist") or "").strip() or _CH.label(channel)
        title  = str(config.get("_bbc_title") or "").strip()
        date   = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self._artist, self._title, self._date = artist, title, date
        # Имя файла говорит сам за себя: «Канал - Выпуск (дата).m4a». Теги
        # дописываются после проверки файла (is_finished), а переименование по
        # тегам движок-эфира не касается — иначе общий ренеймер превращает
        # эфир в «00. -» при первых же пустых тегах (24.09.2026).
        safe_a, safe_t = _safe(artist), _safe(title)
        stem = (f"{safe_a} - {safe_t} ({date})"
                if safe_t and safe_t != safe_a else f"{safe_a} ({date})")
        pid = str(config.get("_bbc_pid") or "").strip()
        out_dir = ep_dir(str(config.get("save-path") or "downloads"),
                         artist, title or stem, pid or channel)
        self._out_dir = str(out_dir)
        self._cover = str(config.get("_bbc_cover") or "")
        self._expected = out_dir / f"{stem}.m4a"
        self._ts = out_dir / f"{stem}.part.ts"

        hls = ".m3u8" in stream
        cmd = [
            ffmpeg, "-hide_banner", "-loglevel", "info", "-progress", "pipe:2",
            "-timeout", "15000000",                # 15 с на сетевой паузе
            "-reconnect", "1", "-reconnect_delay_max", "5",
        ]
        if not hls:
            # eof/streamed-реконнект приложены к HLS, каждый сегмент которого —
            # отдельное соединение, — он переносит в поток уже прочитанные байты
            # (24.09.2026: двухчасовой эфир задекодировался в мусор). Для
            # прогрессивного HTTP они по-прежнему нужны.
            cmd += ["-reconnect_at_eof", "1", "-reconnect_streamed", "1"]
        else:
            # HLS: один держатель соединения на сегменты и старт со свежего
            # края плейлиста вместо перемотки через весь хвост окна.
            cmd += ["-http_persistent", "1", "-live_start_index", "-1"]
        cmd += [
            "-i", stream,
            "-vn", "-sn", "-dn",
            "-c", "copy",
            "-t", str(duration),
            "-f", "mpegts",
            "-y", str(self._ts),
        ]
        return cmd

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

    # ── финализация и проверка ───────────────────────────────────────────────

    def _remux_ts(self) -> bool:
        """Промежуточный .ts → итоговый .m4a (copy + faststart). True — файл лег."""
        ffmpeg = shutil.which("ffmpeg")
        ts, dst = self._ts, self._expected
        if not (ffmpeg and ts and ts.exists() and dst):
            return dst.exists() if dst else False
        try:
            r = subprocess.run(
                [ffmpeg, "-hide_banner", "-v", "error", "-y",
                 "-i", str(ts), "-c", "copy", "-movflags", "+faststart", str(dst)],
                capture_output=True, text=True, timeout=1800)
        except Exception:
            return False
        return r.returncode == 0 and dst.exists() and dst.stat().st_size > 10_000

    def _decode_errors(self, path: Path) -> int:
        from ripster import bbc_live_recovery as _REC
        return _REC.decode_errors(path)

    def _file_duration(self, path: Path) -> float:
        from ripster import bbc_live_recovery as _REC
        return _REC.probe_duration(path)

    def _write_tags(self, path: Path) -> None:
        from ripster.tagger import write_tags
        album = f"{self._artist} (live {self._date})"
        write_tags(path, {"title":  self._title or self._artist,
                          "artist": self._artist, "albumartist": self._artist,
                          "album":  album, "year": self._date,
                          "track": "1", "tracktotal": "1"})

    def _mark_corrupt(self) -> Path:
        """Битый файл остаётся в библиотеке с честным именем — его ещё может
        спасти восстановление (ripster/bbc_live_recovery.py), а перезаписать или
        выбросить его молча движок не вправе."""
        src = self._expected
        dst = src.with_name(f"{src.stem} (повреждено).{src.suffix.lstrip('.')}")
        try:
            if src.exists():
                if dst.exists():
                    dst.unlink()
                src.rename(dst)
        except OSError:
            dst = src
        return dst

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
        if (got is None or got.stat().st_size <= 10_000) and self._remux_ts():
            got = self._expected
        size = got.stat().st_size if got else 0
        written = f" {size // 1024} КБ" if size else ""
        if rc == 0 and got and size > 10_000:
            # Вердикт «готово» спрашивает у ФАЙЛА, а не у кода возврата: ffmpeg
            # выходит с нулём и после того, как сеть подмешала в поток мусор.
            errs = self._decode_errors(got)
            fdur = self._file_duration(got)
            short = self._duration and fdur + 1 < self._duration * _MIN_KEPT_RATIO
            if errs > _MAX_DECODE_ERRORS or short:
                bad = self._mark_corrupt()
                self.recovery_info = {
                    "file": str(bad), "channel": self._channel,
                    "artist": self._artist, "title": self._title,
                    "date": self._date, "duration": self._duration,
                    "errors": errs, "file_duration": fdur}
                why = (f"эфир записан, но файл не декодируется чисто "
                       f"({errs} ошибок{' ' + f'{fdur:.0f} с из {self._duration}' if short else ''}) "
                       f"— файл сохранён с меткой «повреждено», запускаю восстановление")
                return EngineResult(success=False, tracks_err=1, error=why, corrupt=True)
            try:
                self._write_tags(got)
            except Exception as e:
                print(f"[bbc_live] теги не записаны: {e}", flush=True)
            if self._ts and self._ts.exists():
                try:
                    self._ts.unlink()       # успешная запись: промежуточный ts не нужен
                except OSError:
                    pass
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
