"""Qobuz metadata — public catalog API (basic metadata, no auth required)."""
from __future__ import annotations

import datetime
from typing import Optional

import httpx
from ripster import http_client as _HTTP

_cfg: dict = {}

_DEFAULT_APP_ID = "798273057"


def install(cfg: dict) -> None:
    global _cfg
    _cfg = cfg


async def fetch_meta_qobuz(url: str) -> Optional[dict]:
    """Fetch album/track metadata from Qobuz's public API."""
    import urllib.parse as up
    try:
        parsed = up.urlparse(url)
        parts  = [p for p in parsed.path.split("/") if p]

        tp, id_ = None, None
        for i, p in enumerate(parts):
            if p in ("album", "track"):
                tp  = p
                id_ = parts[-1].split("?")[0] if i + 1 < len(parts) else None
                break
        if not tp or not id_:
            return None

        app_id  = (_cfg.get("qobuz-app-id") or "").strip() or _DEFAULT_APP_ID
        token   = (_cfg.get("qobuz-auth-token") or "").strip()
        headers = {"X-User-Auth-Token": token} if token else {}

        # UPC → internal album id
        if tp == "album" and id_.isdigit() and len(id_) >= 12:
            orig_upc = id_
            async with _HTTP.ashared() as c:
                sr = await c.get(
                    "https://www.qobuz.com/api.json/0.2/album/search",
                    params={"query": id_, "limit": 5, "app_id": app_id},
                    headers=headers,
                )
                if sr.status_code == 200:
                    items = (sr.json().get("albums") or {}).get("items") or []

                    def _valid(it) -> bool:
                        cand = str(it.get("id", "") or "")
                        return bool(cand) and cand != orig_upc

                    match = next(
                        (it for it in items
                         if str(it.get("upc", "")) == orig_upc and _valid(it)),
                        None,
                    )
                    if not match:
                        real = [it for it in items if _valid(it)]
                        if len(real) == 1:
                            match = real[0]
                    if match:
                        id_ = str(match.get("id", "")) or id_

        async with _HTTP.ashared() as c:
            r = await c.get(
                f"https://www.qobuz.com/api.json/0.2/{tp}/get",
                params={f"{tp}_id": id_, "app_id": app_id},
                headers=headers,
            )
            if r.status_code != 200:
                return None
            d = r.json()
        if d.get("status") == "error":
            return None

        def _cover(obj) -> str:
            if not isinstance(obj, dict):
                return ""
            img = obj.get("image") or {}
            if not isinstance(img, dict):
                return ""
            return img.get("large") or img.get("small") or img.get("thumbnail") or ""

        def _year_from_ts(ts) -> str:
            if not ts:
                return ""
            try:
                return datetime.datetime.fromtimestamp(int(ts)).strftime("%Y")
            except (ValueError, OSError):
                return ""

        def _label(obj) -> str:
            lbl = obj.get("label")
            if isinstance(lbl, dict):
                return lbl.get("name", "")
            return str(lbl) if lbl else ""

        if tp == "album":
            artist_name = (d.get("artist") or {}).get("name", "")
            year        = _year_from_ts(d.get("released_at")) or (d.get("release_date_original") or "")[:4]
            date        = d.get("release_date_original", "")
            tracks = [
                {
                    "title":    t.get("title", ""),
                    "duration": t.get("duration", 0),
                    "artist":   (t.get("performer") or {}).get("name", "") or artist_name,
                }
                for t in ((d.get("tracks") or {}).get("items") or [])
            ]
            tc = d.get("tracks_count") or len(tracks)
            return {
                "id":          str(d.get("id", id_)),
                "type":        "album",
                "title":       d.get("title", ""),
                "artist":      artist_name,
                "album":       d.get("title", ""),
                "artworkUrl":  _cover(d),
                "year":        year,
                "date":        date,
                "label":       _label(d),
                "genre":       (d.get("genre") or {}).get("name", "") if isinstance(d.get("genre"), dict) else "",
                "upc":         d.get("upc", ""),
                "trackCount":  tc,
                "totalTracks": tc,
                "tracks":      tracks,
                "hires":       d.get("hires", False),
                "service":     "qobuz",
            }

        # tp == "track"
        alb   = d.get("album") or {}
        cover = _cover(alb)
        return {
            "id":          str(d.get("id", id_)),
            "type":        "track",
            "title":       d.get("title", ""),
            "artist":      (d.get("performer") or {}).get("name", "") or (alb.get("artist") or {}).get("name", ""),
            "album":       alb.get("title", ""),
            "artworkUrl":  cover,
            "year":        _year_from_ts(alb.get("released_at")) or (alb.get("release_date_original") or "")[:4],
            "date":        alb.get("release_date_original", ""),
            "label":       _label(alb),
            "trackCount":  1,
            "totalTracks": 1,
            "isrc":        d.get("isrc", ""),
            "tracks":      [],
            "service":     "qobuz",
        }

    except Exception as e:
        print(f"[meta:qobuz] {e}", flush=True)
        return None
