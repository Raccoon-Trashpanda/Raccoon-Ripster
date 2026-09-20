"""
BBC Sounds integration — browse programmes, stream via HLS, download as MP3 320kbps.

Stream flow (no yt-dlp needed):
  1. GET bbc.co.uk/programmes/{pid}.json  → versions[0].pid = VPID
  2. GET mediaselector/6/select/...vpid/{vpid}/format/json  → HLS m3u8 URL
"""
from __future__ import annotations

import asyncio
import json
import re
import subprocess
from asyncio.subprocess import PIPE
from pathlib import Path

import httpx
from ripster import http_client as _HTTP
from ripster.py_runtime import app_python
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel

from ripster.metadata.mixesdb import fetch_mix_detail, search_mixesdb

router = APIRouter(prefix="/api/bbc")

_config:       dict = {}
_broadcast     = None
_queue_snapshot = None

_RMS      = "https://rms.api.bbc.co.uk/v2"
_PROG_API = "https://www.bbc.co.uk/programmes"
# mediaset "iptv-all" only ever exposes ONE HLS rendition (51 kbps HE-AAC,
# mp4a.40.5) for every show/episode tested (Essential Mix, Radio 1 Dance,
# Pete Tong) — MediaSelector's own "bitrate": 320 field is a nominal quality-
# tier label, not the real encoded rate, so downstream MP3-320K re-encodes
# were just inflating file size around an already-narrow-band ~16.5 kHz-
# lowpassed source (confirmed via ffprobe + spectrogram — this is what a
# tester's spectrum screenshot flagged). mediaset "pc" exposes the SAME
# 51k variant plus a real 102 kbps HE-AAC one in its master playlist
# (verified across 3 different shows) — yt-dlp picks the highest-BANDWIDTH
# HLS variant by default, so this alone doubles real delivered fidelity
# with no other code change. Still well under a genuine 320 kbps source —
# every other mediaset id tried (iptv-uk/iptv-nonuk/apple-ipad-hls/pc-tablet/
# podcast-*/audio-syndication-*) returned "selectionunavailable" from this
# vantage point, so 102 kbps HE-AAC appears to be BBC's real ceiling for
# on-demand Sounds audio reachable without a UK-residential IP.
_MSEL     = "https://open.live.bbc.co.uk/mediaselector/6/select/version/2.0/mediaset/pc/vpid/{vpid}/format/json"
_T        = httpx.Timeout(connect=10, read=20, write=10, pool=5)

BRANDS = [
    {"id": "b006wkfp", "label": "Essential Mix"},
    {"id": "b00f3pc4", "label": "Classic Essential Mix"},
    {"id": "b006ww0v", "label": "Pete Tong"},
    {"id": "m0009y7t", "label": "Radio 1 Dance"},
    {"id": "b01dmw9x", "label": "Dance Anthems"},
    {"id": "m002d2x6", "label": "The 6 Mix"},
    {"id": "m001dkv1", "label": "Rave Forever"},
    {"id": "b01fm4ss", "label": "Gilles Peterson"},
    {"id": "b0072ky7", "label": "Craig Charles Funk & Soul"},
    {"id": "m0021281", "label": "DnB Allstars"},
    {"id": "b006tp52", "label": "Late Junction"},
]


def install(app, ctx) -> None:
    global _config, _broadcast, _queue_snapshot
    _config         = ctx.config
    _broadcast      = ctx.broadcast
    _queue_snapshot = ctx.queue_snapshot
    app.include_router(router)


def _img(template: str, sz: int = 320) -> str:
    """BBC image_url has '{recipe}' placeholder → replace with e.g. '320x320'."""
    return template.replace("{recipe}", f"{sz}x{sz}") if template else ""


def yt_dlp_cmd() -> list[str]:
    # НЕ Scripts\yt-dlp.exe и не shutil.which(): console-script shim под
    # изолированным embeddable-питоном молча выходит с кодом 1
    # (preflight Gate 0.5). Запуск — тем же интерпретатором, `-m yt_dlp`.
    py = app_python()
    probe = subprocess.run([py, "-c", "import yt_dlp"], capture_output=True)
    if probe.returncode != 0:
        raise RuntimeError("yt-dlp не установлен в рабочий питон-окружение Ripster")
    return [py, "-m", "yt_dlp"]


_TC_RE = re.compile(
    r'^\s*(\d{1,2}:\d{2}(?::\d{2})?)\s*[-–—·|•]?\s*(.+)',
    re.MULTILINE,
)


def _parse_timecodes(text: str) -> list[dict]:
    """Parse HH:MM:SS / MM:SS timestamp lines from a YouTube description."""
    result = []
    for m in _TC_RE.finditer(text):
        ts    = m.group(1).strip()
        title = m.group(2).strip().rstrip(" |·-–—")
        if not title or len(title) > 300:
            continue
        parts = [int(x) for x in ts.split(":")]
        seconds = parts[0] * 3600 + parts[1] * 60 + (parts[2] if len(parts) == 3 else 0) if len(parts) == 3 else parts[0] * 60 + parts[1]
        result.append({"time": ts, "seconds": seconds, "title": title})
    return result


def _save_dir() -> Path:
    p = Path(_config.get("save-path", "downloads")) / "BBC"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _parse_dur(d) -> int:
    """BBC duration can be int seconds or {'value': 7200, 'label': '...'}."""
    if isinstance(d, dict):
        return int(d.get("value", 0) or 0)
    return int(d or 0)


def _parse_ep(ep: dict) -> dict:
    titles  = ep.get("titles") or {}
    img_url = ep.get("image_url") or ""
    if not img_url:
        img_pid = (ep.get("image") or {}).get("pid", "")
        img_url = f"https://ichef.bbci.co.uk/images/ic/{{recipe}}/{img_pid}.jpg" if img_pid else ""
    # id = VPID; real episode PID lives in urn:bbc:radio:episode:{pid}
    vpid = ep.get("id", "")
    urn  = ep.get("urn", "")
    pid  = urn.split(":")[-1] if urn else vpid
    # Отложенная запись эфира (ripster/bbc_schedule.py): RMS отдаёт сеть канала
    # и точное время доступности — availability.from для будущей передачи и есть
    # начало эфира, release.date слишком грубое (до дней).
    from ripster import bbc_live_channels as _chl
    channel = (ep.get("network") or {}).get("id") or ""
    return {
        "pid":      pid,
        "vpid":     vpid,
        "title":    titles.get("primary", ep.get("title", "")),
        "subtitle": titles.get("secondary", ""),
        "synopsis": (ep.get("synopses") or {}).get("short", ""),
        "date":     (ep.get("release") or {}).get("date", ""),
        "duration": _parse_dur(ep.get("duration")),
        "image":    _img(img_url),
        "channel":      channel,
        "avail_from":   (ep.get("availability") or {}).get("from") or "",
        "schedulable":  _chl.known(channel),
    }


# ── Brands ────────────────────────────────────────────────────────────────────

@router.get("/brands")
async def get_brands():
    return {"brands": BRANDS}


# ── Episodes ──────────────────────────────────────────────────────────────────

@router.get("/episodes")
async def get_episodes(
    brand_id: str = Query("b006wkfp"),
    offset:   int = Query(0, ge=0),
    limit:    int = Query(20, ge=1, le=50),
):
    url = (
        f"{_RMS}/programmes/playable"
        f"?container={brand_id}&sort=sequential&type=episode"
        f"&experience=domestic&offset={offset}&limit={limit}"
    )
    async with _HTTP.ashared() as c:
        r = await c.get(url, headers={"Accept": "application/json"})
    if r.status_code != 200:
        raise HTTPException(502, f"BBC API {r.status_code}")
    data = r.json()
    return {
        "total":  data.get("total", 0),
        "offset": offset,
        "items":  [_parse_ep(ep) for ep in data.get("data", [])],
    }


# ── Search ────────────────────────────────────────────────────────────────────

@router.get("/search")
async def search_bbc(q: str = Query(..., min_length=1)):
    url = f"{_RMS}/experience/inline/search"
    async with _HTTP.ashared() as c:
        r = await c.get(url, params={"q": q}, headers={"Accept": "application/json"})
    if r.status_code != 200:
        raise HTTPException(502, f"BBC search {r.status_code}")
    data = r.json()
    items = []
    # The response groups hits into blocks — a "Shows" block of container_item
    # (brands/rubrics, e.g. "Radio 1's Essential Mix" the show) and an
    # "Episodes" block of playable_item (actual downloadable episodes). Only
    # the latter has a real vpid/urn:...:episode:... pid that _bbc_preflight
    # can resolve to a stream — a container_item's "pid" is a BRAND id, which
    # has no `versions` and always fails preflight ("нет доступных версий").
    # The "Shows" block sorts first and alone fills the caller's top-N slice,
    # so without this filter real episodes never surface in search results.
    for block in data.get("data", []):
        for ep in block.get("data", []):
            if ep.get("type") != "playable_item":
                continue
            items.append(_parse_ep(ep))
    return {"items": items}


# ── Отложенная запись эфира ───────────────────────────────────────────────────

class ScheduleReq(BaseModel):
    channel:   str
    start_utc: str          # ISO (с смещением или Z) — храним в UTC
    duration:  int          # секунд
    title:     str = ""
    subtitle:  str = ""
    cover:     str = ""


@router.get("/schedule")
async def list_scheduled():
    """Все планы (pending + история fired) — фронт подсвечивает карточки
    уже запланированных эфиров."""
    from ripster import bbc_schedule as _bs
    try:
        return {"items": _bs.get_store().all()}
    except RuntimeError:
        return {"items": []}


@router.post("/schedule")
async def schedule_live(req: ScheduleReq, request: Request):
    """Запланировать запись будущего эфира: план в bbc_scheduled.json +
    карточка «scheduled» в очереди (её ведёт ripster/bbc_schedule.py)."""
    if _is_guest(request):
        raise HTTPException(403, "guests cannot schedule recordings")
    from ripster import bbc_schedule as _bs
    from ripster import bbc_live_channels as _chl
    if not _chl.known(req.channel):
        raise HTTPException(400, f"Неизвестный канал эфира: {req.channel}")
    try:
        start = _bs.parse_utc(req.start_utc)
        row = _bs.schedule_recording(channel=req.channel, start_utc=start,
                                     duration=req.duration,
                                     title=req.title, subtitle=req.subtitle,
                                     cover=req.cover)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if _broadcast and _queue_snapshot:
        await _broadcast({"type": "queue_update", "queue": _queue_snapshot()})
    return {"ok": True, "row": row}


@router.delete("/schedule/{sid}")
async def cancel_scheduled(sid: str, request: Request):
    if _is_guest(request):
        raise HTTPException(403, "guests cannot cancel recordings")
    from ripster import bbc_schedule as _bs
    ok = _bs.cancel_recording(sid)
    if not ok:
        raise HTTPException(404, "план не найден")
    if _broadcast and _queue_snapshot:
        await _broadcast({"type": "queue_update", "queue": _queue_snapshot()})
    return {"ok": True}


# ── VPID + stream URL helpers ─────────────────────────────────────────────────

async def _get_vpid(pid: str, client: httpx.AsyncClient) -> str:
    """Fetch VPID (version PID) from the programmes JSON API."""
    r = await client.get(f"{_PROG_API}/{pid}.json")
    if r.status_code != 200:
        raise HTTPException(502, f"BBC programmes API {r.status_code} for {pid}")
    versions = r.json().get("programme", {}).get("versions", [])
    if not versions:
        raise HTTPException(404, f"No versions found for {pid}")
    return versions[0]["pid"]


async def _get_hls_url(vpid: str, client: httpx.AsyncClient) -> str:
    """Query BBC MediaSelector for the best HLS m3u8 URL."""
    url = _MSEL.format(vpid=vpid)
    r   = await client.get(url)
    if r.status_code != 200:
        raise HTTPException(502, f"MediaSelector {r.status_code} for {vpid}")
    # Prefer: https + HLS + cloudfront, fallback to akamai
    best_cf = best_ak = None
    for media in r.json().get("media", []):
        for conn in media.get("connection", []):
            href = conn.get("href", "")
            if conn.get("protocol") != "https" or ".m3u8" not in href:
                continue
            sup = conn.get("supplier", "")
            if "cloudfront" in sup and not best_cf:
                best_cf = href
            elif "akamai" in sup and not best_ak:
                best_ak = href
    chosen = best_cf or best_ak
    if not chosen:
        raise HTTPException(502, f"No HLS stream found for {vpid}")
    return chosen


# ── Stream endpoint ───────────────────────────────────────────────────────────

@router.get("/stream")
async def get_stream(request: Request, pid: str = Query(...), vpid: str = Query(""), name: str = Query("")):
    """Resolve HLS m3u8 URL via BBC MediaSelector. If vpid is known, skips programmes.json lookup."""
    async with _HTTP.ashared() as c:
        if not vpid:
            vpid = await _get_vpid(pid, c)
        url = await _get_hls_url(vpid, c)
    try:
        from ripster import stats_collector as _sc
        from ripster.guest_manager import get_manager as _gm
        sid = _gm().get_session_id_from_request(request) or ""
        ip  = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip() or \
              (request.client.host if request.client else "")
        _sc.record_stream("bbc", name or pid, url, session_id=sid, client_ip=ip)
    except Exception:
        pass
    return {"url": url, "vpid": vpid}


# ── Download (HLS → MP3 320 kbps, per-episode folder + cover 1000×1000) ──────

class DownloadReq(BaseModel):
    pid:       str
    vpid:      str = ""   # version PID — skips programmes.json lookup if provided
    title:     str = ""
    artist:    str = "BBC Radio"
    image_url: str = ""
    cover_url: str = ""   # explicit cover override (e.g. chosen from MixesDB)


def _safe(s: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', '_', s).strip(" .")


def _ep_dir(artist: str, title: str, pid: str) -> Path:
    """Returns downloads/BBC/{Artist} - {Title}/ , created."""
    a = _safe(artist) or "BBC Radio"
    t = _safe(title)  or pid
    folder = _save_dir() / f"{a} - {t}"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


@router.post("/download")
async def download_episode(req: DownloadReq, request: Request):
    # BBC downloads run outside the task queue (no task_id to key ownership
    # off), so we stamp the guest session id directly on the progress events
    # instead — "" means owner-triggered, matching the queue's own convention.
    from ripster.guest_manager import get_manager as _gm_fn
    sid = _gm_fn().get_session_id_from_request(request) or ""

    ep_dir = _ep_dir(req.artist, req.title, req.pid)
    title  = _safe(req.title or req.pid)
    out    = str(ep_dir / f"{title}.mp3")

    # Fresh HLS token
    async with _HTTP.ashared() as c:
        vpid = req.vpid or await _get_vpid(req.pid, c)
        hls  = await _get_hls_url(vpid, c)

    try:
        ytdlp = yt_dlp_cmd()
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    cmd = [
        *ytdlp,
        "--quiet",
        "--downloader", "ffmpeg",
        "--hls-use-mpegts",
        "-x", "--audio-format", "mp3", "--audio-quality", "320K",
        "--add-metadata",
        "--ignore-errors",
        "-o", out,
        hls,
    ]
    # store duration for progress calculation
    if req.pid:
        dur_raw = None
        # try to get it via programmes.json (already fetched vpid above)
        try:
            async with _HTTP.ashared() as c2:
                rp = await c2.get(f"{_PROG_API}/{req.pid}.json")
                if rp.status_code == 200:
                    versions = rp.json().get("programme", {}).get("versions", [])
                    dur_raw = (versions[0] if versions else {}).get("duration")
        except Exception:
            pass
        if dur_raw:
            BBC_DURATION_MAP[req.pid] = int(dur_raw)

    asyncio.create_task(_bg_download(cmd, req.pid, req.title, req.artist,
                                     req.image_url, ep_dir, req.cover_url, sid))
    return {"status": "started", "pid": req.pid, "dir": str(ep_dir)}


async def _bg_download(cmd: list, pid: str, title: str, artist: str,
                       image_url: str, ep_dir: Path, cover_url: str = "", sid: str = ""):
    async def _bcast(msg: dict):
        if _broadcast:
            try:
                await _broadcast({**msg, "session_id": sid})
            except Exception:
                pass

    await _bcast({"type": "bbc_dl_start", "pid": pid, "title": title, "artist": artist})
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        # Stream stderr to parse ffmpeg progress lines
        last_pct = -1
        async for raw in proc.stderr:
            line = raw.decode(errors="replace").rstrip()
            # ffmpeg progress: "size=   1234kB time=00:12:34.56 bitrate= ..."
            m = re.search(r'time=(\d+):(\d+):(\d+)', line)
            if m and BBC_DURATION_MAP.get(pid):
                dur  = BBC_DURATION_MAP[pid]
                secs = int(m.group(1))*3600 + int(m.group(2))*60 + int(m.group(3))
                pct  = min(99, int(secs / dur * 100)) if dur else 0
                if pct != last_pct:
                    last_pct = pct
                    await _bcast({"type": "bbc_dl_progress", "pid": pid,
                                  "title": title, "pct": pct, "elapsed": secs})
        await proc.wait()
    except Exception:
        pass

    fallback_q = f"{artist} {title}".strip() if (artist or title) else ""
    await _save_cover(image_url, ep_dir, _safe(title or pid),
                      fallback_query=fallback_q, cover_override=cover_url)
    await _try_write_cue(pid, title, artist, ep_dir)
    await _bcast({"type": "bbc_dl_done", "pid": pid, "title": title,
                  "dir": str(ep_dir)})

# pid → episode duration in seconds (stored when download is queued)
BBC_DURATION_MAP: dict[str, int] = {}


async def _save_cover(image_url: str, ep_dir: Path, stem: str,
                     fallback_query: str = "", cover_override: str = "") -> str:
    """Download cover to ep_dir/{stem}.jpg. Returns final artwork URL used (or "")."""
    cover_path = ep_dir / f"{stem}.jpg"

    # 0) Explicit user-chosen override (e.g. picked from MixesDB in UI)
    if cover_override:
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(30), follow_redirects=True) as c:
                r = await c.get(cover_override)
            if r.status_code == 200:
                cover_path.write_bytes(r.content)
                return str(cover_path)
        except Exception:
            pass

    # 1) BBC image
    if image_url:
        url = re.sub(r'\{recipe\}|\d+x\d+', "1200x1200", image_url)
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(30)) as c:
                r = await c.get(url)
            if r.status_code == 200:
                cover_path.write_bytes(r.content)
                return str(cover_path)
        except Exception:
            pass

    # 2) MixesDB fallback
    if fallback_query:
        try:
            results = await search_mixesdb(fallback_query, limit=3)
            for hit in results:
                detail = await fetch_mix_detail(hit["page_title"])
                if detail and detail.get("artworkUrl"):
                    async with httpx.AsyncClient(timeout=httpx.Timeout(30)) as c:
                        r = await c.get(detail["artworkUrl"])
                    if r.status_code == 200:
                        cover_path.write_bytes(r.content)
                        print(f"[bbc] cover from mixesdb: {hit['page_title']}", flush=True)
                        return str(cover_path)
        except Exception as e:
            print(f"[bbc] mixesdb cover fallback failed: {e}", flush=True)

    return ""


async def _cue_tracks(pid: str, title: str, artist: str,
                      net_1001: bool = True) -> list[dict]:
    """Timed rows for a CUE sheet from the best source (BBC → 1001TL → MixesDB),
    in the `offset` shape _build_cue expects. Untimed lists make a CUE of
    00:00 markers — useless, so they yield nothing."""
    art = "" if artist in ("", "BBC Radio") else artist
    ttl = "" if title in ("", "BBC Mix") else title
    best = await best_tracklist(pid, ttl, art, net_1001=net_1001)
    if not (best.get("found") and best.get("timed")):
        return []
    return [{"offset": int(t["seconds"]), "title": t["title"], "artist": t["artist"]}
            for t in best["tracks"] if t.get("seconds") is not None and not t.get("is_with")]


async def _try_write_cue(pid: str, title: str, artist: str, ep_dir: Path):
    try:
        tracks = await _cue_tracks(pid, title, artist)
        if not tracks:
            return
        stem = _safe(title or pid)
        cue  = _build_cue(title or pid, artist or "BBC Radio", tracks)
        (ep_dir / f"{stem}.cue").write_text(cue, encoding="utf-8")
    except Exception:
        pass


# ── Tracklist ─────────────────────────────────────────────────────────────────

@router.get("/tracklist")
async def tracklist(pid: str = Query(...)):
    return {"tracks": await _fetch_tracklist(pid)}


async def _fetch_tracklist(pid: str) -> list[dict]:
    async with _HTTP.ashared() as c:
        r = await c.get(
            f"{_PROG_API}/{pid}/segments.json",
            headers={"Accept": "application/json"},
        )
    if r.status_code != 200:
        return []
    tracks = []
    for ev in r.json().get("segment_events", []):
        seg    = ev.get("segment", {})
        raw    = ev.get("version_offset", ev.get("offset"))
        offset = raw or 0
        title  = seg.get("title", "")
        artist = (seg.get("primary_contributor") or {}).get("name", "") or seg.get("artist", "")
        if title:
            # `timed` = BBC gave a real position in the episode; old archive
            # episodes list names only (version_offset null) — those still
            # identify the set but can't drive chapters/CUE.
            tracks.append({"offset": int(offset), "title": title, "artist": artist,
                           "timed": raw is not None})
    return tracks


# ── Best tracklist for an episode: BBC → 1001Tracklists → MixesDB ────────────

def _ts_to_sec(ts: str):
    try:
        p = [int(x) for x in str(ts).strip().split(":")]
    except ValueError:
        return None
    if len(p) == 3:
        return p[0] * 3600 + p[1] * 60 + p[2]
    if len(p) == 2:
        return p[0] * 60 + p[1]
    return None


def _norm_rows(rows: list[dict]) -> list[dict]:
    """One row shape for every source: {n, artist, title, seconds, timestamp}."""
    out = []
    for i, t in enumerate(rows, 1):
        sec = t.get("seconds")
        if sec is None and t.get("timestamp"):
            sec = _ts_to_sec(t["timestamp"])
        out.append({"n": str(t.get("n") or i), "artist": t.get("artist", "") or "",
                    "title": t.get("title", "") or "",
                    "seconds": sec, "timestamp": _sec_to_ts(sec) if sec is not None else "",
                    "is_with": bool(t.get("is_with"))})
    return out


def _is_timed(rows: list[dict]) -> bool:
    secs = {t["seconds"] for t in rows if t.get("seconds") is not None}
    return len(secs) >= 3


async def _episode_meta(pid: str) -> dict:
    """Title / DJ / air date / length from the programmes API (short timeout)."""
    try:
        async with httpx.AsyncClient(timeout=6.0, follow_redirects=True) as c:
            r = await c.get(f"{_PROG_API}/{pid}.json")
        if r.status_code != 200:
            return {}
        p = r.json().get("programme") or {}
        brand = ((p.get("parent") or {}).get("programme") or {}).get("title", "")
        vers = p.get("versions") or []
        return {"title": brand or p.get("title", ""), "artist": p.get("title", ""),
                "date": str(p.get("first_broadcast_date") or "")[:10],
                "duration": int((vers[0].get("duration") if vers else 0) or 0)}
    except Exception:
        return {}


_TL_REFETCHED: set[str] = set()


def _tl1001_reject_reason(res: dict, bbc: list[dict], date: str,
                          years: set | None = None) -> str:
    """Hard gates on top of 1001TL's weighted score ('' = accept). For a BBC
    episode we KNOW the broadcast date and often BBC's own track names, so a
    page aired on another day, or whose tracks disagree with BBC's names, is
    another set however well the title matched. ("The Essential Mix 30", 2020,
    got Bontan's 2024-11-30 list: its only identity token "30" sat in the other
    page's date.) Recomputed from the result itself, not its stored checks, so
    a disk-cached entry found by an older/looser rule is re-judged on read."""
    if not res.get("ok") or (res.get("match") or {}).get("tier") == "definitive":
        return ""
    from ripster import tracklist_match as TM
    ov = TM.check_name_overlap({"tracklist": bbc}, {"tracks": res.get("tracks") or []})
    if ov["applicable"]:
        # BBC listed the set: agreement on names is the proof (Classic Essential
        # Mix re-airs carry the ORIGINAL date on the page, so date can't be).
        return "" if ov["score"] >= 0.4 else "names disagree with BBC " + ov["detail"]
    page = TM._dates(str(res.get("url") or ""))
    if years and page and not ({str(d.year) for d in page} & set(years)):
        # "Danny Howells 2002" must not get his 2007 set.
        return f"set year {sorted(years)} vs page {page[0]}"
    air = TM._dates(date)
    if not air:
        return ""
    if not page:
        return "no air date on the page to confirm"
    gap = min(abs((air[0] - d).days) for d in page)
    return "" if gap <= 3 else f"aired {air[0]}, page {page[0]}"


def _is_guest(request: Request | None) -> bool:
    """A guest session (tunnel link) — they may read BBC data, but must not make
    the owner's machine scrape 1001Tracklists (GUEST_BLOCKED_PATHS blocks the
    direct route for the same reason)."""
    if request is None:
        return False
    try:
        from ripster import auth as _auth
        fn = getattr(_auth, "_guest_session_fn", None)
        return bool(fn and fn(request))
    except Exception:
        return False


async def best_tracklist(pid: str, title: str = "", artist: str = "",
                         dur: int = 0, force: bool = False,
                         net_1001: bool = True) -> dict:
    """Timed tracklist for a BBC episode, sources in order of trust:
      1. BBC's own segments (only when they carry positions),
      2. 1001Tracklists — verified with DJ + air date (+ BBC's names when BBC
         listed the set untimed, + the Sounds URL as a backlink),
      3. BBC's own names without times (right set, no chapters),
      4. MixesDB — relevance-scored, DJ-checked, air-date-checked.
    A wrong tracklist is worse than none: every foreign source is gated, and
    any failure (captcha, timeout, cooldown) just falls to the next source.
    Returns {found, source, tracks, timed, url?, match?, tried}."""
    tried: list[str] = []
    try:
        bbc_rows = await _fetch_tracklist(pid)
    except Exception:
        bbc_rows = []
    tried.append("bbc")
    bbc = _norm_rows([{**t, "seconds": t["offset"] if t.get("timed") else None}
                      for t in bbc_rows])
    if len(bbc) >= 3 and _is_timed(bbc):
        return {"found": True, "source": "bbc", "tracks": bbc, "timed": True,
                "url": f"https://www.bbc.co.uk/programmes/{pid}", "tried": tried}

    meta = await _episode_meta(pid)
    title = title or meta.get("title", "")
    artist = artist or meta.get("artist", "")
    dur = int(dur or meta.get("duration") or 0)
    date = meta.get("date", "")
    # Classic Essential Mix = a re-air ("deadmau5 2008" broadcast 2026-09-13):
    # the programmes date is the repeat, the set's own pages carry the original
    # year. A year in the title that isn't the air year → the date proves nothing.
    _yrs = set(re.findall(r"\b((?:19|20)\d{2})\b", f"{title} {artist}"))
    if date and _yrs and date[:4] not in _yrs:
        date = ""

    if title or artist:
        tried.append("1001tracklists")
        from fastapi.concurrency import run_in_threadpool
        from ripster import tl1001
        ref = [{"artist": t["artist"], "title": t["title"]} for t in bbc]

        async def _lookup(frc: bool) -> dict:
            try:
                return await run_in_threadpool(
                    tl1001.tracklist_for, title or artist, artist if title else "", dur,
                    ref, [f"https://www.bbc.co.uk/sounds/play/{pid}"], "bbc_" + pid,
                    frc, date)
            except Exception as e:
                print(f"[bbc] 1001TL lookup failed for {pid}: {e}", flush=True)
                return {}

        if net_1001:
            res = await _lookup(force)
        else:
            # Cache only (guests): an already-verified list is free to show.
            hit = tl1001._disk_get("id:bbc_" + pid) or {}
            res = ({**hit, "cached": True}
                   if hit.get("ok") and tl1001._cached_identity_ok(hit, artist, title) else {})
        why = _tl1001_reject_reason(res, bbc, date, _yrs)
        if why and net_1001 and res.get("cached") and pid not in _TL_REFETCHED:
            # Self-heal: the cached page is someone else's set — look again once
            # per process (not on every play) instead of hiding the right list
            # behind the wrong one for the cache's 10 days.
            _TL_REFETCHED.add(pid)
            print(f"[bbc] 1001TL cache for {pid} rejected ({why}) — re-searching", flush=True)
            res = await _lookup(True)
            why = _tl1001_reject_reason(res, bbc, date, _yrs)
        if why:
            print(f"[bbc] 1001TL {res.get('url')} rejected for {pid}: {why}", flush=True)
            res = {}
        if res.get("ok") and res.get("tracks"):
            rows = _norm_rows(res["tracks"])
            if _is_timed(rows) or not bbc:
                return {"found": True, "source": "1001tracklists", "tracks": rows,
                        "timed": _is_timed(rows), "url": res.get("url"),
                        "match": {k: (res.get("match") or {}).get(k)
                                  for k in ("score", "tier", "reason")},
                        "cached": bool(res.get("cached")), "tried": tried}

    if len(bbc) >= 3:
        return {"found": True, "source": "bbc", "tracks": bbc, "timed": False,
                "url": f"https://www.bbc.co.uk/programmes/{pid}", "tried": tried}

    tried.append("mixesdb")
    # MixesDB search is plain full-text: "Radio 1's Classic … 2008" drowns it,
    # "<DJ> Essential Mix" finds the page. Clean both halves; the year (classic
    # re-airs) or the exact air date then picks the right edition.
    mdb_show = re.sub(r"(?i)\b(bbc|radio\s*1'?s?|classic)\b", " ", title)
    mdb_show = re.sub(r"\s+", " ", mdb_show).strip() or title
    mdb_dj = re.sub(r"\b(?:19|20)\d{2}\b", " ", re.sub(r"(?i)^with\s+", "", artist))
    mdb_dj = re.sub(r"\s+", " ", mdb_dj.split(" @ ")[0]).strip() or artist
    mdb_date = date or (next(iter(_yrs)) if len(_yrs) == 1 else "")
    try:
        m = await mixesdb_match(title=mdb_show, artist=mdb_dj, brand="", date=mdb_date)
    except Exception as e:
        print(f"[bbc] MixesDB lookup failed for {pid}: {e}", flush=True)
        m = {}
    rows = _norm_rows(m.get("tracklist") or []) if m.get("found") else []
    if len(rows) >= 3:
        return {"found": True, "source": "mixesdb", "tracks": rows,
                "timed": _is_timed(rows), "url": m.get("url"),
                "match": {"score": m.get("score"), "tier": "scored"}, "tried": tried}
    return {"found": False, "source": "", "tracks": [], "timed": False, "tried": tried}


@router.get("/tracklist-best")
async def tracklist_best(pid: str = Query(..., pattern=r"^[a-z0-9]{6,12}$"),
                         title: str = Query(""), artist: str = Query(""),
                         dur: int = Query(0, ge=0), *, request: Request):
    return await best_tracklist(pid, title.strip(), artist.strip(), dur,
                                net_1001=not _is_guest(request))


# ── MixesDB search / detail ──────────────────────────────────────────────────

@router.get("/mixesdb/search")
async def mixesdb_search(q: str = Query(..., min_length=1), limit: int = Query(10, ge=1, le=30)):
    results = await search_mixesdb(q, limit=limit)
    return {"results": results}


@router.get("/mixesdb/detail")
async def mixesdb_detail(title: str = Query(...)):
    detail = await fetch_mix_detail(title)
    if not detail:
        raise HTTPException(404, "Mix not found on MixesDB")
    return detail


# Filler words that carry no matching signal for mix/show titles.
_MATCH_STOP = {
    "the", "a", "an", "and", "mix", "set", "live", "at", "with", "feat", "ft",
    "featuring", "presents", "pres", "radio", "show", "episode", "ep", "edition",
    "podcast", "dj", "vol", "volume", "part", "pt", "in", "on", "of", "for",
}


def _match_toks(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (s or "").lower())
            if w not in _MATCH_STOP and len(w) > 1}


def _match_nums(s: str) -> set[str]:
    # Episode/edition numbers (1–4 digits). Excludes long runs like years-in-dates only
    # when read from `show`, where dates aren't present.
    return set(re.findall(r"\d{1,4}", s or ""))


def _score_mixesdb_hit(q_title: str, q_artist: str, hit: dict) -> float:
    """Relevance score of a MixesDB hit vs the source mix. Higher = better.

    Token overlap + episode-number gating: if the source title has a number
    (e.g. "Anjunadeep Edition 500") and the candidate show has a DIFFERENT number,
    it's almost certainly the wrong episode → heavy penalty. This is what stops the
    "first hit with artwork" logic from returning a stranger's tracklist."""
    q_tokens = _match_toks(f"{q_artist} {q_title}")
    if not q_tokens:
        return 0.0
    cand = f"{hit.get('artist','')} {hit.get('show','')}"
    c_tokens = _match_toks(cand)
    # Identity veto: the DJ must be in the hit. "Radio 1's Essential Mix" alone
    # matches every episode of the show (HAAi got Hot Since 82's list, 19.09.2026).
    from ripster.tracklist_match import identity_tokens as _ident, _tokens as _tmt
    ident = _ident(q_artist, q_title)
    if ident and not (ident & _tmt(cand)):
        return 0.0
    overlap = len(q_tokens & c_tokens) / len(q_tokens)

    q_nums = _match_nums(q_title)
    c_nums = _match_nums(hit.get("show", ""))   # show only — avoids date digits
    num_adj = 0.0
    if q_nums:
        if q_nums & c_nums:
            num_adj = 0.45            # episode number matches → strong confirm
        elif c_nums:
            num_adj = -0.6            # candidate has a DIFFERENT number → wrong episode
        else:
            num_adj = -0.1            # source numbered, candidate not → mild doubt
    return overlap + num_adj


@router.get("/mixesdb/match")
async def mixesdb_match(title: str = Query(""), artist: str = Query(""), brand: str = Query(""),
                        date: str = Query("")):
    """Auto-match a BBC episode / SoundCloud mix to a MixesDB entry.

    Scores every search hit for relevance (token overlap + episode-number gating)
    and returns the BEST match above a confidence threshold — never just the first
    hit that happens to have artwork. Returns {found: false} when nothing is a
    confident match (an empty tracklist beats a wrong one)."""
    parts = [p for p in [artist, title, brand] if p]
    q = " ".join(parts).strip()
    if not q:
        return {"found": False}
    print(f"[bbc] mixesdb_match q={q!r}", flush=True)
    try:
        results = await search_mixesdb(q, limit=8)
        scored = sorted(
            ((_score_mixesdb_hit(title or brand, artist, h), h) for h in results),
            key=lambda x: x[0], reverse=True,
        )
        print(f"[bbc] mixesdb scored: {[(round(s,2), h['page_title']) for s,h in scored]}",
              flush=True)
        # Need a confident lead; below threshold we'd rather show nothing.
        _MIN_SCORE = 0.5
        from ripster.tracklist_match import _dates as _tmd
        air = _tmd(str(date or "")[:10])
        year = str(date or "") if re.fullmatch(r"(?:19|20)\d{2}", str(date or "")) else ""
        for score, hit in scored:
            if score < _MIN_SCORE:
                break
            if year and not str(hit.get("date") or "").startswith(year):
                continue
            # Same DJ on the same show years apart (HAAi 2018 vs 2026): when the
            # air date is known, a hit dated more than 3 days away is another set.
            hd = _tmd(str(hit.get("date") or "")[:10])
            if air and hd and abs((air[0] - hd[0]).days) > 3:
                continue
            detail = await fetch_mix_detail(hit["page_title"])
            if detail and (detail.get("tracklist") or detail.get("artworkUrl")):
                return {
                    "found":      True,
                    "score":      round(score, 2),
                    "artworkUrl": detail.get("artworkUrl", ""),
                    "tracklist":  detail.get("tracklist", []),
                    "page_title": hit["page_title"],
                    "url":        hit.get("url", ""),
                    "date":       detail.get("date", ""),
                }
    except Exception as e:
        print(f"[bbc] mixesdb_match failed: {e}", flush=True)
    return {"found": False}


# ── YouTube timecodes ─────────────────────────────────────────────────────────

def _yt_timecodes_from_info(info: dict) -> list[dict]:
    chapters = info.get("chapters") or []
    if chapters:
        return [
            {"time": _sec_to_ts(int(c.get("start_time", 0))),
             "seconds": int(c.get("start_time", 0)),
             "title": c.get("title", "").strip()}
            for c in chapters if c.get("title")
        ]
    return _parse_timecodes(info.get("description") or "")


@router.get("/youtube-timecodes")
async def youtube_timecodes(q: str = Query(..., min_length=1), dur: int = Query(0, ge=0)):
    """Search YouTube via yt-dlp and return timecodes from the BEST-matching video.

    Picks among several candidates by duration closeness (a 90-min mix matches a
    ~90-min upload, not a 3-min clip) + title overlap, and prefers a video that
    actually HAS timecodes — instead of blindly taking the first search result."""
    try:
        cmd = [*yt_dlp_cmd(), "--dump-json", "--no-playlist", "--quiet", f"ytsearch5:{q}"]
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=PIPE, stderr=PIPE)
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=25)
    except asyncio.TimeoutError:
        return {"found": False, "error": "timeout"}
    except Exception as e:
        return {"found": False, "error": str(e)}

    if not stdout:
        return {"found": False}

    candidates = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            candidates.append(json.loads(line))
        except Exception:
            continue
    if not candidates:
        return {"found": False}

    q_tokens = _match_toks(q)
    q_nums   = _match_nums(q)            # episode/edition numbers, e.g. {"593"}

    def _cand_score(info: dict) -> float:
        score = 0.0
        title = info.get("title", "")
        # Duration closeness (strongest signal for full mixes).
        vdur = info.get("duration") or 0
        if dur and vdur:
            ratio = min(dur, vdur) / max(dur, vdur)
            score += ratio * 2.0            # up to +2.0 for a near-exact length match
            if ratio < 0.7:
                score -= 1.0                # very different length → probably a clip/other set
        # Title overlap.
        if q_tokens:
            t_tokens = _match_toks(title)
            score += len(q_tokens & t_tokens) / len(q_tokens)
        # Episode-number gating — "Edition 593" must not match "Edition 580".
        if q_nums:
            c_nums = _match_nums(title)
            if q_nums & c_nums:
                score += 1.5                # exact episode number → strong confirm
            elif c_nums:
                score -= 1.2                # a DIFFERENT number → wrong episode
        # Prefer videos that actually carry timecodes.
        if _yt_timecodes_from_info(info):
            score += 1.0
        return score

    ranked = sorted(((_cand_score(c), c) for c in candidates), key=lambda x: x[0], reverse=True)
    best_score, best = ranked[0]
    btitle = best.get("title", "")
    print(f"[bbc] yt candidates: {[(round(s,2), c.get('title','')[:48]) for s,c in ranked]}",
          flush=True)

    # Confidence gate — a wrong mix's timecodes are worse than none.
    if q_nums:
        bnums = _match_nums(btitle)
        if bnums and not (q_nums & bnums):
            return {"found": False, "reason": "episode-number mismatch", "title": btitle}
    if dur and best.get("duration"):
        ratio = min(dur, best["duration"]) / max(dur, best["duration"])
        if ratio < 0.6:
            return {"found": False, "reason": "duration mismatch", "title": btitle}
    if best_score < 1.0:
        return {"found": False, "reason": "low confidence", "title": btitle}

    video_id  = best.get("id", "")
    timecodes = _yt_timecodes_from_info(best)
    thumb     = (best.get("thumbnail")
                 or (f"https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg" if video_id else ""))

    if not timecodes:
        return {"found": False, "video_id": video_id, "title": btitle, "thumbnail": thumb}

    return {"found": True, "video_id": video_id, "title": btitle,
            "thumbnail": thumb, "timecodes": timecodes}


def _sec_to_ts(sec: int) -> str:
    h, rem = divmod(sec, 3600)
    m, s   = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


# ── CUE download ──────────────────────────────────────────────────────────────

@router.get("/cue")
async def download_cue(
    pid:    str = Query(...),
    title:  str = Query("BBC Mix"),
    artist: str = Query("BBC Radio"),
    *,
    request: Request,
):
    tracks = await _cue_tracks(pid, title, artist, net_1001=not _is_guest(request))
    if not tracks:
        raise HTTPException(404, "No tracklist found for this episode")
    safe = re.sub(r'[\\/:*?"<>|]', '_', title)
    return Response(
        content=_build_cue(title, artist, tracks).encode("utf-8"),
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{safe}.cue"'},
    )


def _build_cue(title: str, artist: str, tracks: list[dict]) -> str:
    lines = [
        f'TITLE "{title}"',
        f'PERFORMER "{artist}"',
        f'FILE "{title}.mp3" MP3',
    ]
    for i, t in enumerate(tracks, 1):
        s  = t.get("offset", 0)
        mm = s // 60
        ss = s % 60
        lines += [
            f"  TRACK {i:02d} AUDIO",
            f'    TITLE "{t.get("title","")}"',
            f'    PERFORMER "{t.get("artist", artist)}"',
            f"    INDEX 01 {mm:02d}:{ss:02d}:00",
        ]
    return "\n".join(lines)
