"""
Deezer engine via deemix CLI.

Install:  pip install deemix
Config:   put ARL token in Settings -> Deezer.

NOTE: deemix CLI has no --arl flag. We write the ARL to deemix's
config directory (``%APPDATA%\\deemix\\.arl`` on Windows,
``~/.config/deemix/.arl`` on Linux/macOS) before each run.
"""
from __future__ import annotations
import os
import re
import shutil
import platform
from pathlib import Path
from .base import EngineBase, EngineResult
from .registry import register
from ripster import http_client as _HTTP

_QUALITIES = [
    {"id": "flac",    "label": "FLAC",    "sub": "Lossless CD quality", "badge": "LOSSLESS", "color": "#3ecfaa", "bitrate": "1411 kbps", "ext": "flac", "req": "premium"},
    {"id": "mp3_320", "label": "MP3 320", "sub": "High quality lossy",  "badge": "LOSSY",    "color": "#EF9F27", "bitrate": "320 kbps",  "ext": "mp3",  "req": "premium"},
    {"id": "mp3_128", "label": "MP3 128", "sub": "Standard quality",    "badge": "LOSSY",    "color": "#EF9F27", "bitrate": "128 kbps",  "ext": "mp3",  "req": "free"},
]

# deemix --bitrate integers: 1=128 kbps, 3=320 kbps, 9=FLAC
_BITRATE = {"flac": "9", "mp3_320": "3", "mp3_128": "1"}

_RE_DONE       = re.compile(r"\bDone\b|\bFinished\b|\bsaved\b|\bCompleted\b", re.I)
_RE_ERR        = re.compile(r"\berror\b|\bfailed\b|cannot|invalid", re.I)
_RE_TRACK      = re.compile(r"(\d+)\s*/\s*(\d+)")
_RE_ARL        = re.compile(r"\b(arl|login|credentials|unauthori[sz]ed|not\s+logged)\b", re.I)
# Per-track download percentage: "[album_123_9] Download at 86%"
_RE_PERCENT    = re.compile(r"Download\s+at\s+(\d+)\s*%", re.I)
# Per-track success marker: "Completed download of \01 - Artist - Title.flac"
_RE_TRACK_DONE = re.compile(r"Completed\s+download\s+of", re.I)


def _deemix_config_dir() -> Path:
    """Return the default deemix config folder for the current OS."""
    system = platform.system()
    if system == "Windows":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "deemix"
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "deemix"
    # Linux / other Unix
    xdg = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(xdg) / "deemix"


def _write_arl(arl: str) -> Path:
    """Persist the ARL token where deemix expects to read it."""
    cfg = _deemix_config_dir()
    cfg.mkdir(parents=True, exist_ok=True)
    arl_file = cfg / ".arl"
    arl_file.write_text(arl.strip(), encoding="utf-8")
    return arl_file


def _write_deemix_config() -> None:
    """Pin deemix's EMBEDDED (in-audio) cover to 1000 px — uniform tag artwork
    across all services by request. deemix defaults to embeddedArtworkSize 800.
    The SAVED external cover stays large (localArtworkSize 1400) so the on-disk
    original is unaffected. Merges into the existing config.json so no other
    deemix setting is clobbered; deemix creates the file on first run otherwise."""
    import json
    cfg_dir = _deemix_config_dir()
    cfg_dir.mkdir(parents=True, exist_ok=True)
    cfg_file = cfg_dir / "config.json"
    data: dict = {}
    if cfg_file.exists():
        try:
            data = json.loads(cfg_file.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data["embeddedArtworkSize"] = 1000
    data["saveArtwork"]         = True
    # Keep the saved cover.jpg high-res (only set a default if unset, so a
    # user-customised value survives).
    data.setdefault("localArtworkSize", 1400)
    try:
        cfg_file.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        print(f"[deezer] cannot write deemix config: {e}", flush=True)


_MEDIA_BAD = "result['preview'] = track['MEDIA'][0]['HREF']"
_MEDIA_GOOD = "result['preview'] = track['MEDIA'][0]['HREF'] if track.get('MEDIA') else ''"


def _patch_media_src(src: str) -> str:
    """Add the empty-MEDIA guard to deezer-py's map_track source. Idempotent:
    returns src unchanged if already guarded (``_MEDIA_GOOD`` present) or if the
    target line isn't there (upstream changed)."""
    if _MEDIA_BAD in src and _MEDIA_GOOD not in src:
        return src.replace(_MEDIA_BAD, _MEDIA_GOOD)
    return src


def _ensure_media_patch() -> None:
    """Guard deezer-py's ``map_track`` against a track with an empty ``MEDIA`` list.

    Upstream does ``track['MEDIA'][0]['HREF']`` unguarded, so an album that contains
    ONE unavailable / no-preview track (empty ``MEDIA``) raises ``IndexError`` during
    metadata generation and kills the ENTIRE album download (issue #23). deemix runs
    as a SUBPROCESS, so an in-process monkeypatch wouldn't reach it — instead patch
    the installed vendor file on disk, idempotently. Self-healing: re-applied before
    every run, survives a ``pip install --upgrade`` of deezer-py.
    """
    try:
        import deezer.utils as _du
        p = Path(_du.__file__)
        src = p.read_text(encoding="utf-8")
        patched = _patch_media_src(src)
        if patched != src:
            p.write_text(patched, encoding="utf-8")
            print("[deezer] patched deezer-py map_track (empty-MEDIA guard, issue #23)", flush=True)
    except Exception as e:
        print(f"[deezer] media-guard patch skipped: {e}", flush=True)


@register
class DeezerEngine(EngineBase):
    name = "deezer"

    def qualities(self) -> list[dict]:
        return [{**q, "engine": self.name} for q in _QUALITIES]

    def build_cmd(self, url: str, quality: str, config: dict) -> list[str]:
        arl      = (config.get("deezer-arl") or "").strip()
        out_path = config.get("deezer-save-path") or config.get("save-path", "downloads")
        bitrate  = _BITRATE.get(quality, "3")
        deemix   = shutil.which("deemix") or "deemix"

        # Ensure output folder exists so deemix doesn't bail on a missing path
        try:
            Path(out_path).mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

        # Pin embedded cover to 1000 px (uniform across services).
        _write_deemix_config()
        # Heal the deemix subprocess against the empty-MEDIA IndexError (issue #23).
        _ensure_media_patch()

        # Write ARL to the location deemix reads from. deemix CLI has NO --arl flag.
        if arl:
            try:
                _write_arl(arl)
            except Exception as e:
                # Fall through — deemix will fail with a clear "login required" message
                # and is_finished() will map that to a user-visible error.
                print(f"[deezer] cannot write ARL file: {e}", flush=True)

        return [deemix, "--bitrate", bitrate, "--path", str(out_path), url]

    def classify_line(self, line: str) -> str:
        low = line.lower()
        # "Finished downloading" and "All done!" are endgame signals, not errors
        if "error" in low or "failed" in low or "invalid arl" in low:
            return "error"
        if "completed download" in low or "finished downloading" in low or "all done" in low:
            return "success"
        return "stdout"

    def parse_progress(self, line: str, current: int, total: int) -> tuple[int, int]:
        # deemix prints "Download at 86%" per track. Treat current = percent, total = 100.
        m_pct = _RE_PERCENT.search(line)
        if m_pct:
            return int(m_pct.group(1)), 100
        # Fallback: some older deemix versions print "n/N" counters
        m_frac = _RE_TRACK.search(line)
        if m_frac:
            return int(m_frac.group(1)), int(m_frac.group(2))
        return current, total

    def is_finished(self, log_text: str, rc: int = -1) -> EngineResult:
        low = log_text.lower()
        tracks_ok = len(_RE_TRACK_DONE.findall(log_text))
        # Bitrate gate: deemix prints "All done!" with NO audio file when the track
        # isn't available in the requested bitrate. Two distinct wordings:
        #   • "can't stream the track at the desired bitrate"  → free/expired ARL
        #   • "track not found at desired bitrate and no alternative found" → the
        #     release simply has no FLAC/320 master (common on niche/regional
        #     catalogues). Both end in 0 saved tracks. NOTE: the second wording
        #     contains "not found", which the shared GONE classifier would wrongly
        #     read as a dead/phantom link — so we return a SPECIFIC message here
        #     (the runner only re-classifies generic errors), and the wording below
        #     deliberately avoids "not found"/"removed".
        bitrate_blocked = (
            "can't stream the track at the desired bitrate" in low
            or "not found at desired bitrate" in low
            or "no alternative found" in low
        )
        if bitrate_blocked and tracks_ok == 0:
            return EngineResult(
                False,
                error="Deezer: трек недоступен в выбранном качестве "
                      "(нет FLAC/320 на источнике) — выбери MP3 320/128 или поищи "
                      "тот же релиз на другом сервисе. Если ARL стал free — обнови "
                      "его в Settings → Deezer.",
            )
        # Success first: "All done!" is deemix's end-of-run marker.
        if "all done" in low or _RE_DONE.search(low):
            if tracks_ok == 0:
                # "All done" but nothing was actually saved — report failure
                return EngineResult(False, error="Deezer: ни один трек не скачался (проверь ARL и качество)")
            return EngineResult(True, tracks_ok=tracks_ok)
        # ARL / login failures get a dedicated message
        if _RE_ARL.search(low):
            return EngineResult(False, error="Неверный ARL — проверь Settings → Deezer")
        # Any other error
        last_err = ""
        for line in reversed(log_text.splitlines()):
            if "error" in line.lower() or "failed" in line.lower():
                last_err = line.strip()[:200]
                break
        if last_err:
            return EngineResult(False, error=last_err)
        return EngineResult(False, error="Deezer: неожиданное завершение (нет 'All done!' в логе)")

    async def search(self, query: str, search_type: str, limit: int, config: dict) -> list[dict]:
        import httpx as _httpx
        import asyncio as _aio
        ent_map = {"album": "album", "track": "track", "artist": "artist", "playlist": "playlist"}
        ep = ent_map.get(search_type, "album")
        try:
            async with _HTTP.ashared() as c:
                r = await c.get(f"https://api.deezer.com/search/{ep}",
                                params={"q": query, "limit": limit})
                data = r.json()
                # /search/album doesn't return release_date — sort by date won't
                # work without it. Hydrate via parallel /album/{id} fetches.
                album_dates: dict[str, str] = {}
                album_labels: dict[str, str] = {}
                # /search/{album,track} don't return release_date, so "sort by
                # newest" can't work without it. Hydrate dates via parallel
                # /album/{id} fetches — for albums by their own id, for tracks by
                # their (de-duplicated) album ids.
                if ep == "album":
                    ids = [str(it.get("id")) for it in (data.get("data") or []) if it.get("id")]
                elif ep == "track":
                    ids = list(dict.fromkeys(
                        str((it.get("album") or {}).get("id"))
                        for it in (data.get("data") or [])
                        if (it.get("album") or {}).get("id")))
                else:
                    ids = []
                if ids:
                    async def _detail(aid: str):
                        try:
                            rr = await c.get(f"https://api.deezer.com/album/{aid}")
                            if rr.status_code == 200:
                                j = rr.json() or {}
                                return aid, j.get("release_date", "") or "", j.get("label", "") or ""
                        except Exception:
                            pass
                        return aid, "", ""
                    for aid, dt, lbl in await _aio.gather(*[_detail(i) for i in ids]):
                        if dt: album_dates[aid] = dt
                        if lbl: album_labels[aid] = lbl
            results = []
            for item in data.get("data") or []:
                if ep == "album":
                    aid = str(item.get("id", ""))
                    date = album_dates.get(aid) or item.get("release_date", "") or ""
                    label = album_labels.get(aid) or item.get("label", "") or ""
                    results.append({
                        "id":      aid,
                        "title":   item.get("title", ""),
                        "artist":  (item.get("artist") or {}).get("name", ""),
                        "type":    search_type,
                        "url":     item.get("link", ""),
                        # Small cover for the search grid — faster + far less
                        # traffic than cover_xl (~1000px). The downloader fetches
                        # full-size art separately, so this only affects display.
                        "cover":   (item.get("cover_medium") or item.get("cover_big")
                                    or item.get("cover", "")),
                        "year":    date[:4],
                        "date":    date,
                        "label":   label,
                        "tracks":  item.get("nb_tracks"),
                        "service": "deezer",
                    })
                elif ep == "track":
                    alb = item.get("album") or {}
                    t_date = album_dates.get(str(alb.get("id", "")), "") or ""
                    results.append({
                        "id":      str(item.get("id", "")),
                        "title":   item.get("title", ""),
                        "artist":  (item.get("artist") or {}).get("name", ""),
                        "type":    search_type,
                        "url":     item.get("link", ""),
                        "cover":   (alb.get("cover_medium") or alb.get("cover_big")
                                    or alb.get("cover", "")),
                        "year":    t_date[:4],
                        "date":    t_date,
                        "service": "deezer",
                    })
                elif ep == "artist":
                    results.append({
                        "id":      str(item.get("id", "")),
                        "title":   item.get("name", ""),
                        "artist":  item.get("name", ""),
                        "type":    search_type,
                        "url":     item.get("link", ""),
                        "cover":   item.get("picture_medium", ""),
                        "service": "deezer",
                    })
            return results
        except Exception:
            return []

    async def get_artist(self, artist_id: str, types: str, config: dict) -> dict:
        import httpx as _httpx
        wanted = {t.strip() for t in types.split(",") if t.strip()}
        try:
            async with _HTTP.ashared() as c:
                info_r = await c.get(f"https://api.deezer.com/artist/{artist_id}")
                info = info_r.json()
                if info.get("error"):
                    return {"error": info["error"].get("message", "Deezer error"), "releases": []}
                releases = []
                next_url: str | None = f"https://api.deezer.com/artist/{artist_id}/albums?limit=100"
                while next_url and len(releases) < 200:
                    r = await c.get(next_url)
                    data = r.json()
                    for a in data.get("data", []):
                        rec_type = a.get("record_type", "album")
                        if rec_type == "compile":
                            rec_type = "compilation"
                        releases.append({
                            "id":      str(a.get("id", "")),
                            "title":   a.get("title", ""),
                            "cover":   a.get("cover_medium", "") or a.get("cover", ""),
                            "year":    (a.get("release_date", "") or "")[:4],
                            "date":    a.get("release_date", ""),
                            "tracks":  a.get("nb_tracks"),
                            "type":    rec_type,
                            "url":     a.get("link", ""),
                            "explicit":a.get("explicit_lyrics", False),
                            "service": "deezer",
                        })
                    next_url = data.get("next")
            if wanted and wanted != {"all"}:
                releases = [r for r in releases if r["type"] in wanted]
            releases.sort(key=lambda r: r.get("date", ""), reverse=True)
            return {
                "artist": {
                    "id":      str(info.get("id", "")),
                    "name":    info.get("name", ""),
                    "picture": info.get("picture_xl", "") or info.get("picture_big", ""),
                    "fans":    info.get("nb_fan"),
                    "albums_total": info.get("nb_album"),
                    "url":     info.get("link", ""),
                    "service": "deezer",
                },
                "releases": releases,
            }
        except Exception as e:
            return {"error": str(e), "releases": []}

    async def get_album(self, album_id: str, config: dict) -> dict:
        import httpx as _httpx
        try:
            async with _HTTP.ashared() as c:
                r = await c.get(f"https://api.deezer.com/album/{album_id}")
                a = r.json()
            if a.get("error"):
                return {"error": a["error"].get("message", "Deezer error")}
            tracks = []
            for t in (a.get("tracks") or {}).get("data", []):
                tracks.append({
                    "id":       str(t.get("id", "")),
                    "title":    t.get("title", ""),
                    "artist":   (t.get("artist") or {}).get("name", ""),
                    "duration": t.get("duration"),
                    "track_no": t.get("track_position"),
                    "disc":     t.get("disk_number"),
                    "preview":  t.get("preview", ""),
                    "explicit": t.get("explicit_lyrics", False),
                    "url":      t.get("link", ""),
                })
            dz_id = str(a.get("id", ""))
            return {
                "album": {
                    "id":     dz_id,
                    "title":  a.get("title", ""),
                    "artist": (a.get("artist") or {}).get("name", ""),
                    "cover":  a.get("cover_xl", "") or a.get("cover_big", ""),
                    "year":   (a.get("release_date", "") or "")[:4],
                    "date":   a.get("release_date", ""),
                    "label":  a.get("label", ""),
                    "upc":    a.get("upc", ""),
                    "genre":  ", ".join(
                        g.get("name", "") for g in ((a.get("genres") or {}).get("data", []) or [])
                    ),
                    "tracks": a.get("nb_tracks"),
                    "url":    a.get("link", "") or f"https://www.deezer.com/album/{dz_id}",
                    "service":"deezer",
                },
                "tracks": tracks,
            }
        except Exception as e:
            return {"error": str(e)}
