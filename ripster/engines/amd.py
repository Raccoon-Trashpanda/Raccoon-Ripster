"""AppleMusicDecrypt v2 engine."""
from __future__ import annotations
import re
import sys
from pathlib import Path
from .base import EngineBase, EngineResult, Event, EventKind, LineLevel, _strip_ansi
from .registry import register

_QUALITIES = [
    {"id":"alac-hires",  "label":"ALAC Hi-Res","sub":"Hi-Res Lossless до 24/192 · публичный wrapper","badge":"HI-RES",  "color":"#ffd60a","bitrate":"≤9216 kbps",    "ext":"m4a","req":"public"},
    {"id":"alac",        "label":"ALAC",       "sub":"Lossless · публичный wrapper (нет Apple ID!)", "badge":"LOSSLESS","color":"#c084a0","bitrate":"≤1411 kbps",    "ext":"m4a","req":"public"},
    {"id":"atmos",       "label":"Atmos EC-3", "sub":"Dolby Atmos · публичный wrapper",              "badge":"SPATIAL", "color":"#9090c8","bitrate":"2448–2768 kbps","ext":"m4a","req":"public"},
    {"id":"ac3",         "label":"Dolby AC-3", "sub":"AC-3 spatial audio · публичный wrapper",       "badge":"SPATIAL", "color":"#9090c8","bitrate":"~640 kbps",     "ext":"m4a","req":"public"},
    {"id":"aac",         "label":"AAC 256",    "sub":"Без wrapper, без Apple ID",                    "badge":"LOSSY",   "color":"#EF9F27","bitrate":"256 kbps",      "ext":"m4a","req":"none"},
    {"id":"aac-legacy",  "label":"AAC Legacy", "sub":"Старый AAC формат",                            "badge":"LOSSY",   "color":"#EF9F27","bitrate":"~256 kbps",     "ext":"m4a","req":"none"},
    {"id":"aac-binaural","label":"Binaural",   "sub":"Бинауральный стерео",                          "badge":"3D",      "color":"#9090c8","bitrate":"~256 kbps",     "ext":"m4a","req":"public"},
    {"id":"aac-downmix", "label":"Downmix",    "sub":"Downmix стерео",                               "badge":"STEREO",  "color":"#6a6a8a","bitrate":"~256 kbps",     "ext":"m4a","req":"public"},
]

_CODEC_MAP = {
    "alac": "alac", "alac-hires": "alac", "atmos": "ec3", "ac3": "ac3",
    "aac": "aac", "aac-legacy": "aac-legacy",
    "aac-binaural": "aac-binaural", "aac-downmix": "aac-downmix",
}

_RE_CONN_FAIL   = re.compile(r"Unable to connect|UNAVAILABLE|connection refused", re.I)
_RE_DONE        = re.compile(r"All done|Finished", re.I)
_RE_PROGRESS    = re.compile(r"Track\s+(\d+)[/ ]+(\d+)|(\d+)[/ ]+(\d+)\s+tracks?", re.I)
# AMD log format: "[809978] 2026-05-29 11:17:29.573 | SONG | <title> | INFO - Start ripping..."
# Matches "| SONG | ... | INFO - Start ripping" (start) and the SUCCESS - Finished
# ripping line that fires once each track finishes its decrypt + save cycle.
_RE_SONG_START  = re.compile(r"\|\s*SONG\s*\|.+?\|\s*INFO\s*-\s*Start\s+ripping", re.I)
_RE_SONG_SAVED  = re.compile(r"\|\s*SONG\s*\|.+?\|\s*SUCCESS\s*-\s*Finished\s+ripping", re.I)
# amd_runner's OWN authoritative per-track + total lines (the AMD-internal
# "SUCCESS - Finished ripping" isn't always emitted, which left the bar at 0/N).
# "OK SONG: SAVED id=…" fires once per saved track; "album tracklist = N tracks"
# (and the "have X/N" summary) give the real album total for a proper N/total bar.
_RE_RUNNER_SAVED = re.compile(r"\bSONG:\s+SAVED\s+id=", re.I)
_RE_RUNNER_TOTAL = re.compile(r"tracklist\s*=\s*(\d+)\s+track|have\s+\d+\s*/\s*(\d+)\b", re.I)
# Verbose AMD/yt-dlp noise — suppress from console
_RE_NOISY       = re.compile(
    r'^\[(?:download|ExtractAudio|Merger|MoveFiles|mp4decrypt|FixupM4a|hlsnative)\]'
    r'|\bDownloading\s+(?:fragment|segment)\b'
    r'|^(?:DEBUG|VERBOSE)\s*[:\|]'
    r'|\bGET\s+https?://'           # raw HTTP request logs
    r'|\bm3u8\b.*\.ts\b'            # HLS segment URLs
    r'|!\s+WAIT:\s+PENDING\b'       # AMD task queue churn (repeats hundreds of times)
    r'|\bi\s+PROGRESS:\s+background_tasks='   # AMD internal progress summary
    r'|\bTASK:\s+(?:created|DONE)\b',         # AMD internal task lifecycle spam
    re.I,
)
_RE_DL_PCT      = re.compile(r'\[download\]\s+(\d+(?:\.\d+)?)\s*%')
_RE_SAVE_DIR    = re.compile(r'\[AMD\][^\[]*\[OK\]\s+OK\s+SAVE_DIR:\s+dir=(.+)')
# Segment decrypt completion — used for throttled progress feedback
_RE_SEG_DONE    = re.compile(r'\bTASK:\s+DONE\b.*\bon_decrypt_success\b', re.I)
_RE_AMD_ELAPSED = re.compile(r'\[\s*(\d+(?:\.\d+)?)s\]')   # "[58.8s]" from AMD log prefix
_RE_ALBUM_LOG   = re.compile(r'\|\s*ALBUM\s*\|\s*(.+?)\s*\|\s*(?:INFO|SUCCESS|ERROR)\s*-\s*Start ripping', re.I)


@register
class AMDEngine(EngineBase):
    name = "amd"

    def __init__(self):
        self._conn_hint_shown = False
        self._song_started = 0
        self._song_saved = 0
        self._song_total = 0     # real album track count (from amd_runner tracklist)
        self._decrypt_segs = 0   # running count of decrypted HLS segments

    def qualities(self) -> list[dict]:
        return [{**q, "engine": self.name} for q in _QUALITIES]

    def build_cmd(self, url: str, quality: str, config: dict) -> list[str]:
        runner = Path(__file__).parent.parent.parent / "amd_runner.py"
        codec  = _CODEC_MAP.get(quality, "alac")
        lang   = config.get("language", "en-US").strip() or "en-US"
        # Strip iTunes affiliate params (?uo=4 etc.) — they can confuse AMD's amp-api calls
        import urllib.parse as _up
        _p = _up.urlparse(url)
        _qs = {k: v for k, v in _up.parse_qs(_p.query).items() if k in ("i",)}
        clean_url = _up.urlunparse(_p._replace(query=_up.urlencode(_qs, doseq=True)))
        return [sys.executable, str(runner), clean_url, codec, lang]

    def working_dir(self) -> str | None:
        from ripster import amd as _amd_mod
        return str(_amd_mod.get_amd_dir())

    def iter_events(self, line: str, *, progress: tuple[int, int]):
        clean = _strip_ansi(line)

        # Count decrypted segments — emit a short status line every 10 segments
        # so the user can see download is progressing even without an exact total.
        if _RE_SEG_DONE.search(clean):
            self._decrypt_segs += 1
            if self._decrypt_segs % 10 == 0:
                t_m = _RE_AMD_ELAPSED.search(clean)
                elapsed = f"{t_m.group(1)}s" if t_m else "…"
                yield Event(
                    kind=EventKind.LINE,
                    message=f"⬦ AMD: {self._decrypt_segs} сегм. [{elapsed}]",
                    level=LineLevel.INFO,
                    extra={"msg_key": "console.amd_segments",
                           "params": {"n": self._decrypt_segs, "elapsed": elapsed}},
                )
            return  # suppress raw TASK: DONE line

        # Suppress segment/verbose noise, forward % to progress bar
        if _RE_NOISY.search(clean):
            m = _RE_DL_PCT.search(clean)
            if m:
                yield Event(kind=EventKind.PROGRESS, current=int(float(m.group(1))), total=100)
            return

        # Connection failure — show hint once per task run
        if _RE_CONN_FAIL.search(clean):
            yield Event(kind=EventKind.LINE, message=clean, level=LineLevel.ERROR)
            if not self._conn_hint_shown:
                self._conn_hint_shown = True
                yield Event(kind=EventKind.LINE,
                            message="  💡 Убедись что instance = wm.wol.moe",
                            level=LineLevel.WARN,
                            extra={"msg_key": "console.amd_instance_hint"})
            return

        # Learn the real album total from amd_runner ("tracklist = N tracks" /
        # "have X/N") so the bar reads N/total instead of N/started.
        m_tot = _RE_RUNNER_TOTAL.search(clean)
        if m_tot:
            _t = int(m_tot.group(1) or m_tot.group(2) or 0)
            if _t > self._song_total:
                self._song_total = _t

        # Track progress per-track. amd_runner's "SONG: SAVED id=" is the reliable
        # completion signal; "SUCCESS - Finished ripping" is a best-effort fallback.
        if _RE_SONG_START.search(clean):
            self._song_started += 1
            total = self._song_total or max(self._song_started, self._song_saved)
            if total > 0:
                yield Event(kind=EventKind.PROGRESS, current=self._song_saved, total=total)
            return
        elif _RE_RUNNER_SAVED.search(clean) or _RE_SONG_SAVED.search(clean):
            self._song_saved += 1
            total = self._song_total or max(self._song_started, self._song_saved)
            if total > 0:
                yield Event(kind=EventKind.PROGRESS, current=min(self._song_saved, total), total=total)
            return  # don't also emit a LINE for internal progress lines

        # Track progress: "Track 3/12" or "3/12 tracks" (fallback for other patterns)
        m = _RE_PROGRESS.search(clean)
        if m:
            g = m.groups()
            done  = int(g[0] or g[2] or 0)
            total = int(g[1] or g[3] or 1)
            yield Event(kind=EventKind.PROGRESS, current=done, total=total)

        # Line classification
        if re.search(r"ERROR|✗ |Failed|Exception|Traceback", clean):
            level = LineLevel.ERROR
        elif re.search(r"WARNING|⚠|Skipping|unavailable", clean, re.I):
            level = LineLevel.WARN
        elif re.search(r"INFO|✓|Saving|Saved|Finished|Done|Downloading|Connected", clean):
            level = LineLevel.SUCCESS
        else:
            level = LineLevel.STDOUT

        yield Event(kind=EventKind.LINE, message=clean, level=level)

    def extract_save_dir(self, log_text: str) -> str | None:
        # 1. Explicit SAVE_DIR logged by patched runner (future-proof)
        dirs = _RE_SAVE_DIR.findall(log_text)
        if dirs:
            return dirs[-1].strip()

        # 2. Reconstruct from AMD config.toml + ALBUM log line
        try:
            return self._infer_dir_from_log(log_text)
        except Exception:
            return None

    @staticmethod
    def _amd_sanitize(s: str) -> str:
        """Mirrors AMD's get_valid_dir_name: strip forbidden FS chars + trailing dots."""
        return "".join(c for c in s if c not in '<>:"/\\|?*').rstrip(". ")

    def _infer_dir_from_log(self, log_text: str) -> str | None:
        # Parse "artist - album" from the ALBUM log line
        m = _RE_ALBUM_LOG.search(log_text)
        if not m:
            return None
        full = m.group(1).strip()
        # logger escapes < and > as \< \> for display — unescape before sanitising
        full = full.replace("\\<", "<").replace("\\>", ">")
        # Split at first " - " to separate albumArtist from album title
        if " - " not in full:
            return None
        artist_raw, album_raw = full.split(" - ", 1)

        # Read dirPathFormat from AMD config.toml
        try:
            from ripster import amd as _amd_mod
            import re as _re
            toml = (_amd_mod.get_amd_dir() / "config.toml").read_text(encoding="utf-8")
            m2 = _re.search(r'dirPathFormat\s*=\s*"([^"]+)"', toml)
            if not m2:
                return None
            dir_fmt = m2.group(1)  # e.g. "C:/Users/.../downloads/{album_artist}/{album}"
            # Extract the base path (the part before {album_artist})
            idx = dir_fmt.find("{album_artist}")
            if idx < 0:
                return None
            base = dir_fmt[:idx].rstrip("/\\")
        except Exception:
            return None

        candidate = Path(base) / self._amd_sanitize(artist_raw) / self._amd_sanitize(album_raw)
        if candidate.is_dir():
            return str(candidate)
        return None

    def is_finished(self, log_text: str, rc: int = -1) -> EngineResult:
        if _RE_CONN_FAIL.search(log_text):
            return EngineResult(False, error="wrapper-manager unreachable (wm.wol.moe)")
        # Per-track signals: a freshly-saved track logs "SUCCESS - Finished
        # ripping"; an already-downloaded one logs "Song already exists" (still a
        # success — the file IS there). Both count as OK. `ready=false` on the
        # public wrapper is NOT itself a failure — it decrypts fine anyway, so we
        # trust the actual per-track + summary counters, not the readiness flag.
        n_saved  = len(re.findall(r"SUCCESS - Finished ripping", log_text))
        n_exists = len(re.findall(r"already exists", log_text, re.I))
        n_ok     = n_saved + n_exists

        # amd_runner prints an authoritative summary line:
        #   "DONE: Finished in Ns — tasks: N total, X OK, Y failed, Z cancelled"
        m_done = re.search(r"DONE:.*?tasks:\s*\d+\s*total,\s*(\d+)\s*OK,\s*(\d+)\s*failed",
                           log_text, re.I)
        # A track whose lossless asset doesn't exist logs "Audio does not exist" /
        # "Failed to download song" — yet AMD's summary may still count a sibling
        # task (album info, lyrics) as OK. If NOTHING was actually saved, that's a
        # real failure regardless of the OK counter.
        no_audio = re.search(r"Audio does not exist|Failed to download song", log_text, re.I)
        if no_audio and n_saved == 0 and n_exists == 0:
            return EngineResult(False, tracks_err=1,
                                error="AMD: у этого релиза нет lossless-ассета (ALAC/Atmos недоступны) — попробуй AAC")

        if m_done:
            ok_count, failed = int(m_done.group(1)), int(m_done.group(2))
            ok_total = max(ok_count, n_ok)
            if failed > 0 and ok_total == 0:
                return EngineResult(False, tracks_err=failed,
                                    error="AMD: все треки с ошибкой (проверь регион/wrapper)")
            if ok_total > 0:
                return EngineResult(True, tracks_ok=ok_total, tracks_err=failed)
            # 0 OK and 0 failed → wrapper returned no device for the region
            return EngineResult(False, tracks_err=1,
                                error="AMD: 0 треков (wm.wol.moe не вернул device для региона) — Settings → Apple → Wrapper")

        # Fallback: legacy "Finished with N error(s)" line (older AMD builds).
        m_fin = re.search(r"Finished with (\d+) error", log_text, re.I)
        if m_fin:
            errors = int(m_fin.group(1))
            if errors == 0 and n_ok == 0:
                return EngineResult(False, tracks_err=1,
                                    error="AMD рапортовал 'Finished' но 0 треков сохранено — wm.wol.moe wrapper не ready (нет available device для региона)")
            return EngineResult(success=(errors == 0), tracks_ok=n_ok, tracks_err=errors)
        if _RE_DONE.search(log_text):
            if n_ok == 0:
                return EngineResult(False, tracks_err=1,
                                    error="AMD: ни один трек не сохранён (wm.wol.moe ready=false). Подожди или открой Settings → Apple → Wrapper")
            return EngineResult(True, tracks_ok=n_ok)
        return EngineResult(success=(rc == 0))
