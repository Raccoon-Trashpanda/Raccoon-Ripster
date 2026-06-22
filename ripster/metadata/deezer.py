"""Deezer metadata — public API, no auth required."""
from __future__ import annotations

import sys
from typing import Optional

import httpx


async def fetch_meta_deezer(url: str) -> Optional[dict]:
    """Fetch album/track/playlist/artist metadata from the public Deezer API.

    Normalises the result to the shared shape used by all metadata providers
    so the UI renderer doesn't need to know which service it came from.
    """
    import urllib.parse as up
    try:
        parsed = up.urlparse(url)
        parts  = [p for p in parsed.path.split("/") if p]

        tp, id_ = None, None
        for i, p in enumerate(parts):
            if p in ("album", "track", "playlist", "artist"):
                tp  = p
                id_ = parts[i + 1].split("?")[0] if i + 1 < len(parts) else None
                break
        if not tp or not id_:
            return None

        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.get(f"https://api.deezer.com/{tp}/{id_}")
            if r.status_code != 200:
                return None
            d = r.json()
        if "error" in d:
            return None

        if tp == "album":
            tracks = []
            try:
                for t in (d.get("tracks", {}).get("data") or []):
                    tracks.append({
                        "title":    t.get("title", ""),
                        "duration": t.get("duration", 0),
                        "artist":   (t.get("artist") or {}).get("name", ""),
                    })
            except Exception as e:
                print(f"[meta] deezer track parse failed: {e}", file=sys.stderr, flush=True)
            return {
                "id":          str(d.get("id", id_)),
                "type":        "album",
                "title":       d.get("title", "—"),
                "artist":      (d.get("artist") or {}).get("name", "—"),
                "album":       d.get("title", "—"),
                "year":        (d.get("release_date") or "")[:4],
                "date":        d.get("release_date", ""),
                "genre":       ((d.get("genres") or {}).get("data") or [{}])[0].get("name", "—"),
                "trackCount":  d.get("nb_tracks", len(tracks)),
                "totalTracks": d.get("nb_tracks", len(tracks)),
                "label":       d.get("label", ""),
                "upc":         d.get("upc", ""),
                "explicit":    bool(d.get("explicit_lyrics")),
                "artworkUrl":  d.get("cover_xl") or d.get("cover_big") or d.get("cover", ""),
                "duration":    (d.get("duration", 0) or 0) * 1000,
                "tracks":      tracks,
                "service":     "deezer",
            }

        if tp == "track":
            alb = d.get("album") or {}
            return {
                "id":          str(d.get("id", id_)),
                "type":        "song",
                "title":       d.get("title", "—"),
                "artist":      (d.get("artist") or {}).get("name", "—"),
                "album":       alb.get("title", "—"),
                "year":        (d.get("release_date") or "")[:4],
                "date":        d.get("release_date", ""),
                "trackNumber": d.get("track_position"),
                "discNumber":  d.get("disk_number"),
                "isrc":        d.get("isrc", ""),
                "explicit":    bool(d.get("explicit_lyrics")),
                "artworkUrl":  alb.get("cover_xl") or alb.get("cover_big", ""),
                "duration":    (d.get("duration", 0) or 0) * 1000,
                "trackCount":  1,
                "service":     "deezer",
            }

        if tp == "playlist":
            return {
                "id":          str(d.get("id", id_)),
                "type":        "playlist",
                "title":       d.get("title", "—"),
                "artist":      (d.get("creator") or {}).get("name", "—"),
                "album":       d.get("title", "—"),
                "year":        (d.get("creation_date") or "")[:4],
                "date":        d.get("creation_date", ""),
                "trackCount":  d.get("nb_tracks", 0),
                "totalTracks": d.get("nb_tracks", 0),
                "artworkUrl":  d.get("picture_xl") or d.get("picture_big") or d.get("picture", ""),
                "duration":    (d.get("duration", 0) or 0) * 1000,
                "service":     "deezer",
            }

        if tp == "artist":
            return {
                "id":         str(d.get("id", id_)),
                "type":       "artist",
                "title":      d.get("name", "—"),
                "artist":     d.get("name", "—"),
                "album":      "—",
                "artworkUrl": d.get("picture_xl") or d.get("picture_big") or d.get("picture", ""),
            }

    except Exception as e:
        print(f"[meta:deezer] {e}", flush=True)
    return None
