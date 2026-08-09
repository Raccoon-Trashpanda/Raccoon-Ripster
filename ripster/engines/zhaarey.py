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
# Итоговая сводка Go-загрузчика: «… Errors: N …». Ловим САМО ЧИСЛО, чтобы
# отличать «ошибок ноль» от настоящей ошибки — по слову их не различить.
_RE_SUMMARY = re.compile(r"Completed:\s*\d+/\d+.*?Errors:\s*(\d+)", re.I)
_RE_TRACK   = re.compile(r"Track\s+(\d+)\s+of\s+(\d+)")
_RE_CODEC   = re.compile(r"no codec found", re.I)
_RE_TOKEN   = re.compile(r"Failed to get token", re.I)
_RE_RETRY   = re.compile(r"Error detected, press Enter to try again", re.I)
# Local docker wrapper couldn't mint a content key (expired/unsubscribed Apple
# session): the wrapper logs "Invalid CKC" and the Go side dies decrypting.
_RE_DECRYPT_FAIL = re.compile(r"Failed to run v[23]|decryptFragment|Invalid CKC", re.I)
# zhaarey can exit 0 after logging "Unavailable, trying to dl aac-lc" → "Failed to
# dl aac-lc: Unavailable" for every track (nothing saved) WITHOUT a "Completed: N/M"
# summary line. A genuine success always logs Completed, so this only matters in the
# rc==0 / no-Completed branch of is_finished (don't false-positive a real download).
_RE_UNAVAIL = re.compile(r"Failed to dl[^\n]*Unavailable|\bUnavailable\b", re.I)
# Public wrapper (wm.wol.moe) couldn't decrypt: the Go tool logs
# "Skipping ...: Decryption is not available for media ID: N" for every track and
# then exits 0 with "Finished with 0 error(s)" → looks like success but 0 files
# were saved (the "Готово, а файлов нет" report). Treat as a clear failure.
_RE_DECRYPT_NA = re.compile(r"Decryption is not available", re.I)

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
        # Per-release lyrics checkbox (None = leave config.yaml's embed-lrc/
        # save-lrc-file alone; True/False = this run only, nothing persisted).
        lyrics_ov = config.get("_lyrics_override")
        lyrics_flag = ["--lyrics=" + ("on" if lyrics_ov else "off")] if lyrics_ov is not None else []
        # Single-track album link (/album/<slug>/<id>?i=<trackid>): main.go only
        # honours the ?i= track selection under --song (main.go:1666), so without
        # this flag a one-track link downloaded the WHOLE album. Add it ONLY when
        # ?i= is present — --song without an `i` hits the empty branch above and
        # silently saves nothing.
        song_flag = []
        try:
            from urllib.parse import urlparse, parse_qs
            if parse_qs(urlparse(url).query).get("i"):
                song_flag = ["--song"]
        except Exception:
            pass
        # Хост-псевдоним geo.music.apple.com (кнопка «Поделиться») регексы
        # main.go не принимают вовсе. Очередь чинит это на входе, но задача
        # может прийти и мимо неё — из бота или восстановленной очереди.
        try:
            from ripster.service_layer import normalize_url as _norm
            url = _norm(url)
        except Exception:
            pass
        # --json makes Go print a JSON array of saved tracks at the end so we
        # can extract the exact output directory without guessing.
        return base + ([flag] if flag else []) + lyrics_flag + song_flag + ["--json", url]

    @staticmethod
    def _json_tracks(line: str) -> Optional[list]:
        """Extract the --json track array from a log line, tolerant of a prefix
        (timestamp / log marker / "Saved:" label) before the array. The old code
        required the line to *start* with '[' — any prefix made it return None, so
        the runner got no output dir and the release was orphaned at the bare root
        (= silent non-delivery). Scans for the outermost [...] and json-decodes it."""
        line = line.strip()
        i, j = line.find("["), line.rfind("]")
        if i == -1 or j <= i:
            return None
        try:
            v = _json.loads(line[i:j + 1])
            return v if isinstance(v, list) and v else None
        except Exception:
            return None

    def extract_save_dir(self, log_text: str) -> Optional[str]:
        """Parse the JSON summary line emitted by --json to get the output dir."""
        for line in reversed(log_text.splitlines()):
            tracks = self._json_tracks(line)
            if tracks:
                p = tracks[0].get("path", "") if isinstance(tracks[0], dict) else ""
                if p:
                    return str(_Path(p).parent)
        return None

    def extract_save_files(self, log_text: str) -> Optional[list[str]]:
        """Exact basenames of the tracks THIS task saved, from the --json summary.
        Lets the runner record a per-task file list instead of globbing the output
        dir — two parallel Apple tasks can resolve to a shared/parent directory, and
        a blind glob would pull in the OTHER task's files (issue #19). Returns None
        when no JSON summary is present (caller falls back to the directory glob)."""
        for line in reversed(log_text.splitlines()):
            tracks = self._json_tracks(line)
            if tracks:
                names = [_Path(t.get("path", "")).name
                         for t in tracks if isinstance(t, dict) and t.get("path")]
                return names or None
        return None

    def iter_events(self, line: str, *, progress: tuple[int, int]):
        clean = _strip_ansi(line)
        if _RE_DECRYPT_FAIL.search(clean):
            # «Invalid CKC» ≠ «сессия умерла». Второй, куда более частый случай —
            # у контента просто нет прав в регионе аккаунта враппера. Раньше мы
            # не различали: помечали враппер нездоровым на 15 минут и уводили ВСЮ
            # lossless-загрузку на публичный wrapper (который регулярно лежит), а
            # владельцу советовали перелогиниться — совет не просто бесполезный,
            # а вредный: лишние логины жгут device-lease и загоняют аккаунт в
            # throttle. 28.07.2026 так и вышло на альбоме, изданном только в
            # tr/ru, при канадском аккаунте — в те же минуты этот же враппер
            # расшифровал соседний альбом целиком.
            alive = False
            try:
                from ripster.apple_router import (mark_local_wrapper_unhealthy,
                                                  local_wrapper_session_alive)
                alive = local_wrapper_session_alive()
                if not alive:
                    mark_local_wrapper_unhealthy()
            except Exception:
                pass
            if alive:
                yield Event(
                    kind=EventKind.FATAL,
                    message="✗ Apple не выдал ключ на этот альбом (Invalid CKC), "
                            "но сессия wrapper'а ЖИВА — значит у контента нет прав "
                            "в регионе аккаунта. Перелогин НЕ поможет: возьми "
                            "релиз через AMD (публичный wrapper держит несколько "
                            "регионов) или ссылкой из другой витрины.",
                    level=LineLevel.ERROR,
                )
            else:
                yield Event(
                    kind=EventKind.FATAL,
                    message="✗ Локальный wrapper не выдаёт ключ (Invalid CKC) и "
                            "аккаунт-API молчит — Apple-сессия протухла/без "
                            "подписки. Перелогинь wrapper или качай через AMD. "
                            "Следующие lossless-задачи уйдут на AMD автоматически.",
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
        # ИТОГОВАЯ СВОДКА — сначала. Go-загрузчик заканчивает прогон строкой
        # «Completed: 10/10 | Warnings: 0 | Errors: 0», и в ней есть слово
        # «Errors» — из-за чего успешное завершение окрашивалось в КРАСНОЕ и
        # попадало в счётчик ошибок (09.08.2026: единственная «ошибка» часа
        # оказалась сообщением об успехе). Смотрим на ЧИСЛО, а не на слово.
        m = _RE_SUMMARY.search(line)
        if m:
            return "error" if int(m.group(1)) > 0 else "success"
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

    async def _amp_album_upc(self, album_id: str, region: str, config: dict) -> str:
        """Album barcode (UPC) from amp-api — the exact key that resolves the
        SAME physical release on Deezer/Qobuz for cross-service playback (Apple
        itself can't stream). iTunes lookup never returns it. Empty on any
        failure (no bearer, 401, etc.) → cross-service play simply stays off."""
        import httpx as _httpx
        bearer = (config.get("authorization-token") or "").strip()
        if not bearer or bearer == "your-authorization-token":
            return ""
        mut = (config.get("media-user-token") or "").strip()
        sf = (region or "US").lower()
        headers = {"Authorization": f"Bearer {bearer}",
                   "Origin": "https://music.apple.com"}
        if mut:
            headers["media-user-token"] = mut
        try:
            async with _httpx.AsyncClient(timeout=10) as c:
                r = await c.get(
                    f"https://amp-api.music.apple.com/v1/catalog/{sf}/albums/{album_id}",
                    headers=headers)
                if r.status_code != 200:
                    return ""
                data = (r.json().get("data") or [])
                if not data:
                    return ""
                return str((data[0].get("attributes") or {}).get("upc") or "").strip()
        except Exception:
            return ""

    async def _amp_album_tracks(self, album_id: str, region: str, config: dict) -> list:
        """Full tracklist from the tokened amp-api (music.apple.com catalog).

        The free iTunes lookup omits songs for continuous DJ-mixes; amp-api
        returns them via the album→tracks relationship. Needs the developer
        bearer (`authorization-token`) + `media-user-token` from config; on any
        failure returns [] so the caller keeps the (empty) iTunes result.
        """
        import httpx as _httpx
        bearer = (config.get("authorization-token") or "").strip()
        if not bearer or bearer == "your-authorization-token":
            return []
        mut = (config.get("media-user-token") or "").strip()
        sf = (region or "US").lower()
        headers = {"Authorization": f"Bearer {bearer}",
                   "Origin": "https://music.apple.com"}
        if mut:
            headers["media-user-token"] = mut
        out: list = []
        try:
            async with _httpx.AsyncClient(timeout=12) as c:
                url = (f"https://amp-api.music.apple.com/v1/catalog/{sf}"
                       f"/albums/{album_id}/tracks")
                # Follow pagination so long mixes (40+ segments) come back whole.
                params = {"limit": 100}
                for _ in range(6):
                    r = await c.get(url, params=params, headers=headers)
                    if r.status_code != 200:
                        break
                    data = r.json()
                    for it in data.get("data") or []:
                        a = it.get("attributes") or {}
                        out.append({
                            "id":       str(it.get("id", "")),
                            "title":    a.get("name", ""),
                            "artist":   a.get("artistName", ""),
                            "duration": (a.get("durationInMillis") or 0) // 1000,
                            "track_no": a.get("trackNumber"),
                            "disc":     a.get("discNumber"),
                            "preview":  ((a.get("previews") or [{}])[0].get("url", "")),
                            "explicit": (a.get("contentRating") == "explicit"),
                            "url":      a.get("url", ""),
                        })
                    nxt = data.get("next")
                    if not nxt:
                        break
                    # `next` is a path like /v1/catalog/.../tracks?offset=100
                    url = "https://amp-api.music.apple.com" + nxt.split("?")[0]
                    off = ""
                    if "offset=" in nxt:
                        off = nxt.split("offset=")[-1].split("&")[0]
                    params = {"limit": 100, "offset": off} if off else {"limit": 100}
        except Exception:
            return out
        return out

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
            matched_rc = cc
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
                        matched_rc = rc
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
            # DJ-mixes / continuous mixes: the free iTunes lookup returns the
            # collection but ZERO song rows (trackCount>0, songs=[]). The tokened
            # amp-api DOES expose them (same source mp3tag reads) — pull the
            # tracklist from there so the card isn't a dead "no tracklist" wall.
            if not tracks and (album_rec.get("trackCount") or 0) > 0:
                amp = await self._amp_album_tracks(album_id, matched_rc, config)
                if amp:
                    tracks = amp
            # Album barcode → lets the album play cross-service (same release on
            # Deezer by UPC); Apple has no stream of its own. Best-effort.
            upc = await self._amp_album_upc(album_id, matched_rc, config)
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
                    "upc":    upc,
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
            if ok == 0 and _RE_UNAVAIL.search(log_text):
                # "Completed: 0/N" with an Unavailable line above it is the same
                # quiet give-up _RE_UNAVAIL exists to catch below — just WITH a
                # summary line this time. Surface the real reason instead of an
                # empty-error failure that renders as bare "Exit code 0" after
                # the partial-retry loop exhausts itself (see runner.py msg =
                # result.error or f"Exit code {rc}").
                return EngineResult(False, tracks_ok=0, tracks_err=total, error=(
                    "Apple: трек недоступен в этом регионе/качестве (Unavailable) — "
                    "смени storefront (регион) или качай через AMD (публичный wrapper)."))
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
        if _RE_DECRYPT_NA.search(log_text):
            return EngineResult(False, error=(
                "Apple: декрипт недоступен для этих треков — wrapper не смог их "
                "расшифровать (локальная Apple-сессия wrapper'а протухла/без подписки, "
                "либо публичный wm.wol.moe перегружен). Файлы НЕ сохранены. "
                "Перелогинь wrapper (Setup → Apple → Wrapper) или повтори позже."))
        if _RE_CODEC.search(log_text):
            return EngineResult(False, error="no codec found — wrapper not responding")
        if _RE_TOKEN.search(log_text):
            return EngineResult(False, error="failed to get token — wrapper not authenticated")
        if rc == 0:
            # Exit 0 but no "Completed: N/M" summary above. If the log shows the
            # track(s) were Unavailable / failed to download, zhaarey just gave up
            # quietly — that's NOT success (it used to be reported as done with 0
            # files → silent non-delivery).
            if _RE_UNAVAIL.search(log_text):
                return EngineResult(False, error=(
                    "Apple: трек недоступен в этом регионе/качестве (Unavailable) — "
                    "смени storefront (регион) или качай через AMD (публичный wrapper)."))
            return EngineResult(success=True)
        return EngineResult(False, error="unknown finish state")
