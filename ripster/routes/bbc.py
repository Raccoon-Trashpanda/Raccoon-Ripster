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
import shutil
import sys
from asyncio.subprocess import PIPE
from pathlib import Path

import httpx
from ripster import http_client as _HTTP
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel

from ripster.metadata.mixesdb import fetch_mix_detail, search_mixesdb

router = APIRouter(prefix="/api/bbc")

_config:    dict = {}
_broadcast       = None

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
    global _config, _broadcast
    _config    = ctx.config
    _broadcast = ctx.broadcast
    app.include_router(router)


def _img(template: str, sz: int = 320) -> str:
    """BBC image_url has '{recipe}' placeholder → replace with e.g. '320x320'."""
    return template.replace("{recipe}", f"{sz}x{sz}") if template else ""


def _ydl() -> str:
    return str(Path(sys.executable).parent / "yt-dlp.exe")


def _find_yt_dlp() -> str | None:
    found = shutil.which("yt-dlp") or shutil.which("yt-dlp.exe")
    if found:
        return found
    for base in [Path(sys.executable).parent, Path(sys.executable).parent / "Scripts"]:
        for name in ("yt-dlp.exe", "yt-dlp"):
            p = base / name
            if p.exists():
                return str(p)
    # Try alongside other Python installs in AppData
    import glob
    for p in glob.glob(str(Path.home() / "AppData/Local/Programs/Python/*/Scripts/yt-dlp.exe")):
        if Path(p).exists():
            return p
    return None


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
    return {
        "pid":      pid,
        "vpid":     vpid,
        "title":    titles.get("primary", ep.get("title", "")),
        "subtitle": titles.get("secondary", ""),
        "synopsis": (ep.get("synopses") or {}).get("short", ""),
        "date":     (ep.get("release") or {}).get("date", ""),
        "duration": _parse_dur(ep.get("duration")),
        "image":    _img(img_url),
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
    url = f"{_RMS}/experience/inline/search?q={q}"
    async with _HTTP.ashared() as c:
        r = await c.get(url, headers={"Accept": "application/json"})
    if r.status_code != 200:
        raise HTTPException(502, f"BBC search {r.status_code}")
    data = r.json()
    items = []
    for block in data.get("data", []):
        for ep in block.get("data", []):
            items.append(_parse_ep(ep))
    return {"items": items}


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
async def download_episode(req: DownloadReq):
    ep_dir = _ep_dir(req.artist, req.title, req.pid)
    title  = _safe(req.title or req.pid)
    out    = str(ep_dir / f"{title}.mp3")

    # Fresh HLS token
    async with _HTTP.ashared() as c:
        vpid = req.vpid or await _get_vpid(req.pid, c)
        hls  = await _get_hls_url(vpid, c)

    cmd = [
        _ydl(),
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
                                     req.image_url, ep_dir, req.cover_url))
    return {"status": "started", "pid": req.pid, "dir": str(ep_dir)}


async def _bg_download(cmd: list, pid: str, title: str, artist: str,
                       image_url: str, ep_dir: Path, cover_url: str = ""):
    async def _bcast(msg: dict):
        if _broadcast:
            try:
                await _broadcast(msg)
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


async def _try_write_cue(pid: str, title: str, artist: str, ep_dir: Path):
    try:
        tracks = await _fetch_tracklist(pid)
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
        offset = ev.get("version_offset", ev.get("offset", 0)) or 0
        title  = seg.get("title", "")
        artist = (seg.get("primary_contributor") or {}).get("name", "")
        if title:
            tracks.append({"offset": int(offset), "title": title, "artist": artist})
    return tracks


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
async def mixesdb_match(title: str = Query(""), artist: str = Query(""), brand: str = Query("")):
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
        for score, hit in scored:
            if score < _MIN_SCORE:
                break
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
    yt = _find_yt_dlp()
    if not yt:
        return {"found": False, "error": "yt-dlp not found"}
    cmd = [yt, "--dump-json", "--no-playlist", "--quiet", f"ytsearch5:{q}"]
    try:
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
):
    tracks = await _fetch_tracklist(pid)
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
