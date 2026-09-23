"""BBC Sounds engine — отдаёт то, что BBC правда даёт, и называет это своими именами.

⚠ «MP3 320» этого движка было правдой ровно до самого файла: на входе у BBC
Sounds on-demand не больше 102 кбит/с HE-AAC (промер 21.09.2026 — см. докстринг
ripster/bbc_quality.py), а ``--audio-quality 320K`` брало этот звук и писало его
в MP3 триста двадцать. Полоса шире от этого не становилась: второе lossy-поколение
и ×3.3 к объёму, а человек слушал «320», думая, что у него hifi. Теперь цель
выбирается по фактической лестнице потока (``mp3_target_kbps``), и имя качества
говорит то, что есть. Нужен настоящий 320 — это live-поток,
ripster/engines/bbc_live.py, и больше ничего.

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
import subprocess
from pathlib import Path

from .base import EngineBase, EngineResult, Event, EventKind, LineLevel, _strip_ansi
from .registry import register
from ripster import bbc_quality as _Q
from ripster.py_runtime import app_python

_RE_PCT   = re.compile(r'\[download\]\s+(\d{1,3}(?:\.\d+)?)%')
_RE_ERROR = re.compile(r'ERROR', re.IGNORECASE)
# `--downloader ffmpeg` means ffmpeg (not yt-dlp's native downloader) does the
# actual fetch, so its own `-stats` line is what carries real progress:
# "size=   1234kB time=00:12:34.56 bitrate= ...". yt-dlp's own [download] N%
# line never appears in this mode.
_RE_FFMPEG_TIME = re.compile(r'time=(\d+):(\d+):(\d+)')


def _safe(s: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', '_', s or '').strip(" .")


def yt_dlp_cmd() -> list[str]:
    # НЕ shutil.which()/Scripts\yt-dlp.exe: console-script shim под изолированным
    # embeddable-питоном молча выходит с кодом 1 (см. preflight Gate 0.5).
    # Единственный надёжный способ — тот же интерпретатор, `-m yt_dlp`.
    py = app_python()
    probe = subprocess.run([py, "-c", "import yt_dlp"], capture_output=True)
    if probe.returncode != 0:
        raise RuntimeError("yt-dlp не установлен в рабочий питон-окружение Ripster")
    return [py, "-m", "yt_dlp"]


def _attach_cover(out_dir: str, cover_url: str) -> None:
    """Ни один из BBC-движков картинку не пишет (yt-dlp/ffmpeg качают только
    звук), а общая пост-обработка очереди достаёт обложку ИЗ тегов — то есть
    брать ей нечего. Без этого шага файл остаётся серым и в библиотеке, и в
    плеере, хотя адрес обложки был у задачи с самого начала."""
    if not (out_dir and cover_url):
        return
    from ripster.metadata.bbc import attach_artwork
    n = attach_artwork(out_dir, cover_url)
    print(f"[bbc] cover: {n} file(s) embedded in {out_dir}", flush=True)


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
        # Имя качества = правда о источнике. «MP3 320» обещало то, чего BBC в
        # on-demand не отдаёт никогда: потолок лестницы — 102 кбит/с HE-AAC
        # (промер 21.09.2026, см. ripster/bbc_quality.py). MP3 здесь остаётся
        # только контейнером: звук в него перекладывается без добавления герц.
        return [{"id": "mp3", "label": "MP3 · source ≤102k", "sub": "BBC Sounds on-demand ceiling (HE-AAC)",
                  "badge": "LOSSY", "color": "#e4003b", "bitrate": "≤102 kbps source",
                  "ext": "mp3", "engine": self.name}]

    def __init__(self):
        self._out_dir: str = ""      # set by build_cmd, read back by extract_save_dir
        self._cover: str = ""        # ichef-адрес обложки (stash из preflight)
        self._duration: int = 0      # episode length in seconds, for time=→% math
        self._expected_name: str = ""  # filename build_cmd asked for (see is_finished)
                                      # (a fresh instance is made per task — see registry.get_engine)
        self._source_kbps: int = 0   # что отдала лестница варианта (0 — не прочитана)
        self._target_kbps: int = 0   # в какой MP3-битрейт это переложили

    def build_cmd(self, url: str, quality: str, config: dict) -> list[str]:
        # `url` here is the already-resolved HLS m3u8 (see module docstring).
        yt = yt_dlp_cmd()
        if not yt:
            raise RuntimeError("yt-dlp not found (checked PATH and the interpreter's own folder)")
        title  = config.get("_bbc_title", "") or ""
        artist = config.get("_bbc_artist", "") or ""
        pid    = config.get("_bbc_pid", "") or ""
        self._duration = int(config.get("_bbc_duration") or 0)
        save_path = config.get("save-path") or "downloads"
        out_dir = ep_dir(save_path, artist, title, pid)
        self._out_dir = str(out_dir)
        self._cover = str(config.get("_bbc_cover") or "")
        self._expected_name = f"{_safe(title) or pid}.mp3"
        out = str(out_dir / self._expected_name)
        # Сколько бит реально в потоке — спрашиваем у самого плейлиста, а не у
        # поля «bitrate» в ответе MediaSelector (оно и для 51k умеет писать 320).
        ladder = _Q.ladder_sync(url)
        self._source_kbps = int(ladder.get("best_kbps") or 0)
        target = _Q.mp3_target_kbps(self._source_kbps)
        self._target_kbps = target
        if not ladder.get("ok"):
            print(f"[bbc] лестница варианта не прочитана ({ladder.get('error')}): "
                  f"цель берём по потолку on-demand ({_Q.ONDEMAND_CEILING}k) — "
                  f"MP3 {target}K, а не 320K", flush=True)
        else:
            print(f"[bbc] источник {self._source_kbps} kbps "
                  f"({ladder.get('best_codec')}), цель MP3 {target}K — без апконверта",
                  flush=True)
        return [
            yt, "--quiet", "--progress",
            "--downloader", "ffmpeg",
            "--hls-use-mpegts",
            "-x", "--audio-format", "mp3", "--audio-quality", f"{target}K",
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
            _attach_cover(self._out_dir, self._cover)
            return EngineResult(success=True, tracks_ok=1)
        return EngineResult(success=False, tracks_err=1,
                            error="yt-dlp exited non-zero — check the log for the ERROR line")
