"""Yandex Music engine — wraps the `yandex-music-downloader` CLI (ymd).

Upstream: github.com/llistochek/yandex-music-downloader (console script
``yandex-music-downloader`` / module ``ymd``). Real lossless FLAC at quality 2.

The CLI prints unconditional progress to stdout:
    [N/M] [FLAC 1411kbps] Загружается <path>      ← per-track start (N/M counter)
    [N/M] Трек <artist> - <title> не доступен ...  ← unavailable (skip)
    Альбом "<title>" не доступен для скачивания    ← whole album unavailable
    Параметер url указан в неверном формате        ← bad URL
Auth/quality errors surface as a Python traceback on stderr + non-zero exit.

Requires a Yandex Music OAuth token (config ``yandex-token``) — without it only
30-second previews are available. Token: https://ym.marshal.dev/token/
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from .base import EngineBase, EngineResult
from .registry import register

_QUALITIES = [
    {"id": "flac",    "label": "FLAC",     "sub": "Lossless (CD quality)", "badge": "LOSSLESS", "color": "#3ecfaa", "bitrate": "1411 kbps", "ext": "flac", "req": "plus"},
    {"id": "aac_192", "label": "AAC 192",  "sub": "High quality lossy",    "badge": "LOSSY",    "color": "#EF9F27", "bitrate": "192 kbps",  "ext": "m4a",  "req": "plus"},
    {"id": "aac_64",  "label": "AAC 64",   "sub": "Standard quality",      "badge": "LOSSY",    "color": "#EF9F27", "bitrate": "64 kbps",   "ext": "m4a",  "req": "free"},
]

# our quality id → ymd --quality value (0=AAC64, 1=AAC192, 2=FLAC)
_QMAP = {"flac": "2", "aac_192": "1", "aac": "1", "aac_64": "0", "low": "0"}

_RE_PROGRESS = re.compile(r"\[(\d+)\s*/\s*(\d+)\]")
_RE_DOWNLOADING = re.compile(r"Загружается", re.IGNORECASE)
_RE_UNAVAIL = re.compile(r"не доступен для скачивания|неверном формате", re.IGNORECASE)

# Shared yandex_music client (re-init only when the token changes).
_ym_client = None
_ym_token = ""


def _ym_get_client(token: str):
    global _ym_client, _ym_token
    from yandex_music import Client
    if _ym_client is None or _ym_token != token:
        _ym_client = Client(token).init()
        _ym_token = token
    return _ym_client


def _ym_cover(uri: str, size: str = "600x600") -> str:
    if not uri:
        return ""
    u = uri.replace("%%", size)
    return u if u.startswith("http") else ("https://" + u)


@register
class YandexEngine(EngineBase):
    name = "yandex"

    def qualities(self) -> list[dict]:
        return [{**q, "engine": self.name} for q in _QUALITIES]

    async def get_album(self, album_id: str, config: dict):
        token = (config.get("yandex-token") or "").strip()
        if not token:
            return {"error": "Укажи токен Яндекса в Settings → Яндекс"}
        from fastapi.concurrency import run_in_threadpool

        def _do():
            c = _ym_get_client(token)
            alb = c.albums_with_tracks(album_id)
            if not alb:
                return {"error": "Альбом не найден"}
            tracks, n = [], 0
            for vol in (alb.volumes or []):
                for t in (vol or []):
                    n += 1
                    tracks.append({
                        "id": str(t.id), "title": t.title or "",
                        "artist": ", ".join(ar.name for ar in (t.artists or [])),
                        "duration": round((t.duration_ms or 0) / 1000),
                        "track_no": n,
                        "url": f"https://music.yandex.ru/album/{album_id}/track/{t.id}",
                        "cover": _ym_cover(getattr(t, "cover_uri", "")),
                    })
            return {"album": {
                "id": str(alb.id), "title": alb.title or "",
                "artist": ", ".join(ar.name for ar in (alb.artists or [])),
                "cover": _ym_cover(alb.cover_uri), "year": str(alb.year or ""),
                "date": str(getattr(alb, "release_date", "") or ""),
                "label": ", ".join(alb.labels_names()) if hasattr(alb, "labels_names") else "",
                "tracks": alb.track_count,
                "service": "yandex",
                "url": f"https://music.yandex.ru/album/{album_id}",
            }, "tracks": tracks}

        try:
            return await run_in_threadpool(_do)
        except Exception as e:
            return {"error": f"Yandex: {e}"}

    async def get_artist(self, artist_id: str, types: str, config: dict):
        token = (config.get("yandex-token") or "").strip()
        if not token:
            return {"error": "Укажи токен Яндекса в Settings → Яндекс", "releases": []}
        from fastapi.concurrency import run_in_threadpool

        def _do():
            c = _ym_get_client(token)
            info = c.artists_brief_info(artist_id)
            artist = info.artist if info else None
            albums = (c.artists_direct_albums(artist_id, page_size=100) or [])
            releases = []
            for a in albums:
                releases.append({
                    "id": str(a.id), "title": a.title or "",
                    "artist": ", ".join(ar.name for ar in (a.artists or [])),
                    "cover": _ym_cover(a.cover_uri), "year": str(a.year or ""),
                    "date": str(getattr(a, "release_date", "") or ""),
                    "tracks": a.track_count,
                    "type": (a.type or "album"),
                    "url": f"https://music.yandex.ru/album/{a.id}", "service": "yandex",
                })
            releases.sort(key=lambda r: r.get("date") or r.get("year") or "", reverse=True)
            return {"artist": {
                "id": artist_id, "name": (artist.name if artist else ""),
                "picture": _ym_cover(getattr(getattr(artist, "cover", None), "uri", "") if artist else ""),
                "url": f"https://music.yandex.ru/artist/{artist_id}", "service": "yandex",
            }, "releases": releases}

        try:
            return await run_in_threadpool(_do)
        except Exception as e:
            return {"error": f"Yandex: {e}", "releases": []}

    def build_cmd(self, url: str, quality: str, config: dict) -> list[str]:
        # Run the ymd module with the SAME interpreter as the app (sys.executable)
        # — this sidesteps PATH / multiple-Python / which() issues entirely: the
        # package is always installed in the running env (see startup guard), so
        # `python -m ymd` is the one invocation guaranteed to resolve correctly.
        token = (config.get("yandex-token") or "").strip()
        out_path = config.get("yandex-save-path") or config.get("save-path", "downloads")
        q = _QMAP.get((quality or "").lower(), "2")
        try:
            Path(out_path).mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        # --cover-resolution 1000: embed a uniform 1000×1000 in-audio cover
        # (per request, all services). ymd defaults to 400; Yandex serves up to
        # 1000 natively, so this is its max.
        cmd = [sys.executable, "-m", "ymd", "-u", url, "--quality", q,
               "--dir", str(out_path), "--skip-existing", "--embed-cover",
               "--cover-resolution", "1000"]
        if token:
            cmd += ["--token", token]
        return cmd

    def classify_line(self, line: str) -> str:
        if _RE_UNAVAIL.search(line) or "Traceback" in line or "Error" in line:
            return "error"
        if _RE_DOWNLOADING.search(line):
            return "info"
        return "stdout"

    def parse_progress(self, line: str, current: int, total: int) -> tuple[int, int]:
        m = _RE_PROGRESS.search(line)
        if m:
            return int(m.group(1)), int(m.group(2))
        return current, total

    def is_finished(self, log_text: str, rc: int = -1) -> EngineResult:
        downloaded = len(_RE_DOWNLOADING.findall(log_text))
        low = log_text.lower()

        # Clear auth/token failures (non-zero exit + traceback, or explicit hints)
        if rc != 0:
            if "unauthorized" in low or "token" in low or "401" in low:
                return EngineResult(False, error="Yandex: токен недействителен/просрочен — обнови yandex-token в Settings")
            if "неверном формате" in low:
                return EngineResult(False, error="Yandex: неверный формат URL")
            if downloaded == 0:
                return EngineResult(False, error="Yandex: скачивание не удалось (проверь токен/подписку Plus и регион)")

        if downloaded > 0:
            return EngineResult(True, tracks_ok=downloaded)

        # rc==0 but nothing downloaded — likely all unavailable (no Plus / region)
        if _RE_UNAVAIL.search(log_text):
            return EngineResult(False, error="Yandex: трек(и) недоступны — нужна подписка Plus или другой регион")
        return EngineResult(False, error="Yandex: ни один трек не скачался (проверь токен и качество)")
