"""zhaarey/apple-music-downloader engine."""
from __future__ import annotations
import json as _json
import re
import sys
from pathlib import Path as _Path
from typing import Optional
from .base import EngineBase, EngineResult, Event, EventKind, LineLevel, _strip_ansi
from .registry import register

_QUALITIES = [
    {"id":"alac-hires","label":"ALAC Hi-Res","sub":"audio-alac-stereo (до 24/192)","badge":"HI-RES","color":"#ffd60a","bitrate":"≤9216 kbps","ext":"m4a","req":"wrapper","flag":""},
    {"id":"alac",    "label":"ALAC",    "sub":"audio-alac-stereo",    "badge":"LOSSLESS","color":"#c084a0","bitrate":"≤1411 kbps",    "ext":"m4a","req":"wrapper","flag":""},
    {"id":"atmos",   "label":"Atmos",   "sub":"audio-atmos / EC-3",   "badge":"SPATIAL", "color":"#9090c8","bitrate":"2448–2768 kbps","ext":"m4a","req":"wrapper","flag":"--atmos"},
    {"id":"aac",     "label":"AAC 256", "sub":"audio-stereo",         "badge":"LOSSY",   "color":"#EF9F27","bitrate":"256 kbps",      "ext":"m4a","req":"token",  "flag":"--aac"},
    {"id":"aac-lc",  "label":"AAC-LC",  "sub":"audio-stereo",         "badge":"LOSSY",   "color":"#EF9F27","bitrate":"128–256 kbps",  "ext":"m4a","req":"token",  "flag":"--aac-lc"},
    {"id":"binaural","label":"Binaural","sub":"audio-stereo-binaural","badge":"3D",      "color":"#9090c8","bitrate":"~256 kbps",     "ext":"m4a","req":"wrapper","flag":"--binaural"},
    {"id":"downmix", "label":"Downmix", "sub":"audio-stereo-downmix", "badge":"STEREO",  "color":"#6a6a8a","bitrate":"~256 kbps",     "ext":"m4a","req":"wrapper","flag":"--downmix"},
    {"id":"mv",      "label":"MV",      "sub":"music video",          "badge":"VIDEO",   "color":"#c084a0","bitrate":"HD 1080p",      "ext":"mp4","req":"token",  "flag":"--mv"},
]

_FLAGS = {q["id"]: q["flag"] for q in _QUALITIES}

_RE_DONE    = re.compile(r"Completed:\s*(\d+)/(\d+)")
_RE_TRACK   = re.compile(r"Track\s+(\d+)\s+of\s+(\d+)")
_RE_CODEC   = re.compile(r"no codec found", re.I)
_RE_TOKEN   = re.compile(r"Failed to get token", re.I)
_RE_RETRY   = re.compile(r"Error detected, press Enter to try again", re.I)
# Local docker wrapper couldn't mint a content key (expired/unsubscribed Apple
# session): the wrapper logs "Invalid CKC" and the Go side dies decrypting.
_RE_DECRYPT_FAIL = re.compile(r"Failed to run v[23]|decryptFragment|Invalid CKC", re.I)

# yt-dlp segment noise — hide from console, extract % for progress bar if present
_RE_NOISY   = re.compile(
    r'^\[(?:download|ExtractAudio|Merger|MoveFiles|mp4decrypt|FixupM4a|FixupM3u8|hlsnative)\]'
    r'|\bDownloading\s+(?:fragment|segment)\b',
    re.I,
)
_RE_DL_PCT  = re.compile(r'\[download\]\s+(\d+(?:\.\d+)?)\s*%')

@register
class ZhaereyEngine(EngineBase):
    name = "zhaarey"

    def qualities(self) -> list[dict]:
        return [{**q, "engine": self.name} for q in _QUALITIES]

    def build_cmd(self, url: str, quality: str, config: dict) -> list[str]:
        go    = config.get("use-go-run", False)
        main  = config.get("main-go-path", "main.go")
        gobin = config.get("go-path", "go")
        flag  = _FLAGS.get(quality, "")
        # Prefer the compiled binary — `go run` recompiles main.go on EVERY
        # download (wastes seconds per task). Auto-detect the built binary at the
        # project root; fall back to `go run` only in dev mode or if it's absent.
        import os
        _root = _Path(__file__).resolve().parent.parent.parent
        bin_path = _root / ("apple-music-downloader.exe" if os.name == "nt"
                            else "apple-music-downloader")
        if (not go) and bin_path.is_file():
            base = [str(bin_path)]
        else:
            base = [gobin, "run", main]
        # --json makes Go print a JSON array of saved tracks at the end so we
        # can extract the exact output directory without guessing.
        return base + ([flag] if flag else []) + ["--json", url]

    def extract_save_dir(self, log_text: str) -> Optional[str]:
        """Parse the JSON summary line emitted by --json to get the output dir."""
        for line in reversed(log_text.splitlines()):
            line = line.strip()
            if not line.startswith("["):
                continue
            try:
                tracks = _json.loads(line)
                if isinstance(tracks, list) and tracks:
                    p = tracks[0].get("path", "")
                    if p:
                        return str(_Path(p).parent)
            except Exception:
                pass
        return None

    def extract_save_files(self, log_text: str) -> Optional[list[str]]:
        """Exact basenames of the tracks THIS task saved, from the --json summary.
        Lets the runner record a per-task file list instead of globbing the output
        dir — two parallel Apple tasks can resolve to a shared/parent directory, and
        a blind glob would pull in the OTHER task's files (issue #19). Returns None
        when no JSON summary is present (caller falls back to the directory glob)."""
        for line in reversed(log_text.splitlines()):
            line = line.strip()
            if not line.startswith("["):
                continue
            try:
                tracks = _json.loads(line)
                if isinstance(tracks, list) and tracks:
                    names = [_Path(t.get("path", "")).name
                             for t in tracks if t.get("path")]
                    return names or None
            except Exception:
                pass
        return None

    def iter_events(self, line: str, *, progress: tuple[int, int]):
        clean = _strip_ansi(line)
        if _RE_DECRYPT_FAIL.search(clean):
            # Mark the local wrapper as CKC-unhealthy so the router auto-routes
            # subsequent lossless tasks to the public wrapper (AMD) instead.
            try:
                from ripster.apple_router import mark_local_wrapper_unhealthy
                mark_local_wrapper_unhealthy()
            except Exception:
                pass
            yield Event(
                kind=EventKind.FATAL,
                message="✗ Локальный wrapper не выдаёт ключ (Invalid CKC) — "
                        "Apple-сессия протухла/без подписки. Перелогинь wrapper "
                        "или качай через AMD (публичный wrapper). Следующие "
                        "lossless-задачи уйдут на AMD автоматически.",
                level=LineLevel.ERROR,
            )
            return
        if _RE_RETRY.search(clean):
            yield Event(
                kind=EventKind.FATAL,
                message="✗ zhaarey: ошибка загрузки (ALAC без враппера?). "
                        "Запусти враппер в Setup или переключись на AMD.",
                level=LineLevel.ERROR,
            )
            return
        # yt-dlp segment/fragment noise — suppress from console
        if _RE_NOISY.search(clean):
            m = _RE_DL_PCT.search(clean)
            if m:
                yield Event(kind=EventKind.PROGRESS, current=int(float(m.group(1))), total=100)
            return
        yield from super().iter_events(line, progress=progress)

    def classify_line(self, line: str) -> str:
        l = line.lower()
        if any(k in l for k in ("error","panic","fatal","exception")): return "error"
        if "warning" in l or "no codec found" in l:                    return "warn"
        if any(k in l for k in ("completed","saved","done")):          return "success"
        return "stdout"

    def parse_progress(self, line: str, current: int, total: int) -> tuple[int, int]:
        # Match "Track N of M" (in-progress marker) first, then fall back to
        # "Completed: N/M" which the Go tool emits at end of each track.
        m = _RE_TRACK.search(line)
        if not m:
            m = _RE_DONE.search(line)
        if m:
            return int(m.group(1)), int(m.group(2))
        return current, total

    async def search(self, query: str, search_type: str, limit: int, config: dict) -> list[dict]:
        import httpx as _httpx
        entity_map = {"album": "album", "track": "song", "artist": "musicArtist", "playlist": "playlist"}
        entity = entity_map.get(search_type, "album")
        lang = config.get("language", "en-US")
        cc = lang.split("-")[-1].upper() if "-" in lang else "US"
        try:
            async with _httpx.AsyncClient(timeout=8) as c:
                r = await c.get("https://itunes.apple.com/search", params={
                    "term": query, "entity": entity, "limit": limit,
                    "country": cc, "media": "music",
                })
                data = r.json()
            results = []
            for item in data.get("results") or []:
                full_date = (item.get("releaseDate") or "")[:10]
                results.append({
                    "id":     str(item.get("collectionId") or item.get("artistId") or item.get("trackId", "")),
                    "title":  item.get("collectionName") or item.get("trackName") or item.get("artistName", ""),
                    "artist": item.get("artistName", ""),
                    "type":   search_type,
                    "url":    item.get("collectionViewUrl") or item.get("trackViewUrl") or item.get("artistViewUrl", ""),
                    "cover":  (item.get("artworkUrl100") or "").replace("100x100", "400x400"),
                    "year":   full_date[:4],
                    "date":   full_date,
                    "label":  item.get("copyright", ""),
                    "tracks": item.get("trackCount"),
                    "service": "apple",
                })
            return results
        except Exception:
            return []

    async def get_artist(self, artist_id: str, types: str, config: dict) -> dict:
        import httpx as _httpx
        wanted = {t.strip() for t in types.split(",") if t.strip()}
        lang = config.get("language", "en-US")
        cc = lang.split("-")[-1].upper() if "-" in lang else "US"
        try:
            async with _httpx.AsyncClient(timeout=10) as c:
                r = await c.get("https://itunes.apple.com/lookup", params={
                    "id": artist_id, "entity": "album", "limit": 200, "country": cc,
                })
                data = r.json()
            results = data.get("results") or []
            if not results:
                return {"error": "Artist not found", "releases": []}
            artist_rec = next((x for x in results if x.get("wrapperType") == "artist"), None)
            albums = [x for x in results if x.get("wrapperType") == "collection"]
            releases = []
            for a in albums:
                coll_type = (a.get("collectionType") or "").lower()
                tracks = a.get("trackCount") or 0
                if coll_type == "compilation":
                    rtype = "compilation"
                elif tracks <= 3:
                    rtype = "single"
                elif tracks <= 6:
                    rtype = "ep"
                else:
                    rtype = "album"
                releases.append({
                    "id":      str(a.get("collectionId", "")),
                    "title":   a.get("collectionName", ""),
                    "cover":   (a.get("artworkUrl100", "") or "").replace("100x100", "600x600"),
                    "year":    (a.get("releaseDate", "") or "")[:4],
                    "date":    (a.get("releaseDate", "") or "")[:10],
                    "tracks":  tracks,
                    "type":    rtype,
                    "url":     a.get("collectionViewUrl", ""),
                    "explicit":(a.get("collectionExplicitness") == "explicit"),
                    "service": "apple",
                })
            if wanted and wanted != {"all"}:
                releases = [r for r in releases if r["type"] in wanted]
            releases.sort(key=lambda r: r.get("date", ""), reverse=True)
            if artist_rec:
                artist = {
                    "id":      str(artist_rec.get("artistId", "")),
                    "name":    artist_rec.get("artistName", ""),
                    "picture": "",
                    "genre":   artist_rec.get("primaryGenreName", ""),
                    "url":     artist_rec.get("artistLinkUrl", ""),
                    "service": "apple",
                }
            else:
                artist = {
                    "id":     artist_id,
                    "name":   albums[0].get("artistName", "") if albums else "",
                    "url":    albums[0].get("artistViewUrl", "") if albums else "",
                    "service":"apple",
                }
            return {"artist": artist, "releases": releases}
        except Exception as e:
            return {"error": str(e), "releases": []}

    async def get_album(self, album_id: str, config: dict) -> dict:
        import httpx as _httpx
        lang = config.get("language", "en-US")
        cc = lang.split("-")[-1].upper() if "-" in lang else "US"
        # Probe the account region first, then others (NZ early — pre-releases
        # land there first), so a release not yet live in the account region
        # still opens its card instead of "Album not found".
        regions = [cc] + [r for r in ("NZ", "US", "AU", "CA", "JP", "DE", "GB") if r != cc]
        try:
            results = []
            async with _httpx.AsyncClient(timeout=10) as c:
                for rc in regions:
                    try:
                        r = await c.get("https://itunes.apple.com/lookup", params={
                            "id": album_id, "entity": "song", "limit": 200, "country": rc,
                        })
                        results = r.json().get("results") or []
                    except Exception:
                        results = []
                    if results:
                        break
            if not results:
                return {"error": "Album not found"}
            album_rec = next((x for x in results if x.get("wrapperType") == "collection"), None)
            songs = [x for x in results if x.get("wrapperType") == "track"]
            if not album_rec:
                return {"error": "Album record missing"}
            tracks = []
            for s in songs:
                tracks.append({
                    "id":       str(s.get("trackId", "")),
                    "title":    s.get("trackName", ""),
                    "artist":   s.get("artistName", ""),
                    "duration": (s.get("trackTimeMillis") or 0) // 1000,
                    "track_no": s.get("trackNumber"),
                    "disc":     s.get("discNumber"),
                    "preview":  s.get("previewUrl", ""),
                    "explicit": (s.get("trackExplicitness") == "explicit"),
                    "url":      s.get("trackViewUrl", ""),
                })
            return {
                "album": {
                    "id":     str(album_rec.get("collectionId", "")),
                    "title":  album_rec.get("collectionName", ""),
                    "artist": album_rec.get("artistName", ""),
                    "cover":  (album_rec.get("artworkUrl100", "") or "").replace("100x100", "1200x1200"),
                    "year":   (album_rec.get("releaseDate", "") or "")[:4],
                    "date":   (album_rec.get("releaseDate", "") or "")[:10],
                    "label":  album_rec.get("copyright", ""),
                    "genre":  album_rec.get("primaryGenreName", ""),
                    "tracks": album_rec.get("trackCount"),
                    "url":    album_rec.get("collectionViewUrl", ""),
                    "service":"apple",
                },
                "tracks": tracks,
            }
        except Exception as e:
            return {"error": str(e)}

    def is_finished(self, log_text: str, rc: int = -1) -> EngineResult:
        m = _RE_DONE.search(log_text)
        if m:
            ok, total = int(m.group(1)), int(m.group(2))
            return EngineResult(success=ok > 0, tracks_ok=ok, tracks_err=total-ok)
        # Local docker wrapper couldn't mint a content key — the wrapper's saved
        # Apple SESSION is expired/unsubscribed (logs "Invalid CKC"). This is the
        # decrypt path, NOT the gamdl cookies (cookies feed AAC/video/metadata and
        # can be perfectly valid here). is_finished is what the card / bot / guest
        # actually display, so surface the REAL, cookies-vs-wrapper-distinct reason
        # instead of the useless "unknown finish state". (iter_events already shows
        # this live and flags the wrapper unhealthy; this mirrors it for the final
        # result so non-console surfaces see it too.)
        if _RE_DECRYPT_FAIL.search(log_text):
            return EngineResult(False, error=(
                "Локальный wrapper не выдал ключ (Invalid CKC) — сессия wrapper'а "
                "протухла или без активной подписки Apple Music. Куки тут ни при "
                "чём (они для AAC/видео/метаданных). Перелогинь wrapper в "
                "Setup → Apple → Wrapper или переключись на AMD (публичный wrapper)."))
        if _RE_CODEC.search(log_text):
            return EngineResult(False, error="no codec found — wrapper not responding")
        if _RE_TOKEN.search(log_text):
            return EngineResult(False, error="failed to get token — wrapper not authenticated")
        if rc == 0:
            return EngineResult(success=True)
        return EngineResult(False, error="unknown finish state")
