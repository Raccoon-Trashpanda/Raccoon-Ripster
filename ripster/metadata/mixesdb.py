"""MixesDB metadata — MediaWiki API (no auth, public)."""
from __future__ import annotations

import hashlib
from typing import Optional

import httpx
from ripster import http_client as _HTTP

_BASE = "https://www.mixesdb.com/w/api.php"
_CDN  = "https://www.mixesdb.com/w/images"
_T    = httpx.Timeout(connect=6, read=12, write=6, pool=4)


async def _api(params: dict) -> dict:
    params.setdefault("format", "json")
    async with _HTTP.ashared() as c:
        r = await c.get(_BASE, params=params)
    if r.status_code != 200:
        return {}
    return r.json()


def _direct_cdn_url(filename: str) -> str:
    """Compute the MediaWiki CDN URL directly from the filename.

    MediaWiki stores images at /images/{md5[0]}/{md5[:2]}/{filename}.
    The first character of the filename is capitalised by convention.
    """
    if filename and filename[0].islower():
        filename = filename[0].upper() + filename[1:]
    md5 = hashlib.md5(filename.encode()).hexdigest()
    return f"{_CDN}/{md5[0]}/{md5[:2]}/{filename}"


async def _image_url(filename: str) -> str:
    """Resolve a File:X.jpg name to a direct CDN URL.

    Primary strategy: compute URL from md5 hash (no API call needed).
    Fallback: MediaWiki imageinfo API (sometimes returns empty on MixesDB).
    """
    # --- Primary: direct hash-based URL (no API call) ---
    direct = _direct_cdn_url(filename)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(8), follow_redirects=True) as c:
            # Try HEAD first; some servers reject HEAD with 405 → fall to GET range
            r = await c.head(direct)
            if r.status_code in (200, 206):
                return direct
            if r.status_code == 405:
                r2 = await c.get(direct, headers={"Range": "bytes=0-0"})
                if r2.status_code in (200, 206):
                    return direct
        print(f"[meta:mixesdb] HEAD {direct} → {r.status_code}", flush=True)
    except Exception as ex:
        print(f"[meta:mixesdb] HEAD failed for {filename}: {ex}", flush=True)

    # --- Fallback: imageinfo API ---
    d = await _api({
        "action": "query",
        "titles": f"File:{filename}",
        "prop":   "imageinfo",
        "iiprop": "url",
    })
    pages = (d.get("query") or {}).get("pages") or {}
    for page in pages.values():
        ii = (page.get("imageinfo") or [{}])[0]
        url = ii.get("url", "")
        if url:
            return url
    return ""


async def search_mixesdb(query: str, limit: int = 10) -> list[dict]:
    """Search MixesDB for mixes matching *query*.

    Returns a list of lightweight dicts suitable for UI display:
      {title, page_title, date, artist, url, thumbnail, pageid}
    """
    d = await _api({
        "action":    "query",
        "list":      "search",
        "srsearch":  query,
        "srlimit":   limit,
        "srprop":    "snippet|titlesnippet",
    })
    results = (d.get("query") or {}).get("search") or []
    out = []
    for item in results:
        title = item.get("title", "")
        out.append({
            "pageid":     item.get("pageid"),
            "page_title": title,
            **_parse_page_title(title),
            "url":        f"https://www.mixesdb.com/w/{title.replace(' ', '_')}",
        })
    return out


def _parse_page_title(title: str) -> dict:
    """
    MixesDB titles are formatted as:
      2023-01-15 - DJ Name - Show Name
    Parse into date / artist / show.
    """
    parts = [p.strip() for p in title.split(" - ", 2)]
    if len(parts) == 3:
        return {"date": parts[0], "artist": parts[1], "show": parts[2]}
    if len(parts) == 2:
        return {"date": parts[0], "artist": parts[1], "show": ""}
    return {"date": "", "artist": "", "show": title}


def _extract_image_from_wikitext(wikitext: str) -> str:
    """Extract image filename from wikitext [[File:...]] syntax."""
    import re
    for m in re.finditer(r'\[\[File:([^\]|]+\.(?:jpg|jpeg|png))', wikitext, re.I):
        fn = m.group(1).strip()
        fn_lower = fn.lower()
        if "logo" not in fn_lower and "icon" not in fn_lower:
            return fn
    return ""


async def fetch_mix_detail(page_title: str) -> Optional[dict]:
    """Fetch full metadata for a MixesDB mix page.

    Returns:
      {title, date, artist, show, artworkUrl, tracklist, categories, url}
    """
    try:
        d = await _api({
            "action": "query",
            "titles": page_title,
            "prop":   "images|categories|revisions",
            "rvprop": "content",
            "rvslots": "main",
        })
        pages = (d.get("query") or {}).get("pages") or {}
        page  = next(iter(pages.values()), {})
        if page.get("missing") is not None:
            print(f"[meta:mixesdb] page missing: {page_title}", flush=True)
            return None

        meta = _parse_page_title(page_title)

        # Wikitext content (needed for tracklist + image fallback)
        revisions = page.get("revisions") or []
        wikitext  = ""
        if revisions:
            slots = revisions[0].get("slots") or {}
            wikitext = (slots.get("main") or revisions[0]).get("*", "")
        tracklist = _parse_tracklist(wikitext)

        # Images — pick first JPG/PNG that is NOT a logo/icon
        # Primary: images prop from API; fallback: parse [[File:...]] from wikitext
        images = page.get("images") or []
        print(f"[meta:mixesdb] {page_title}: images_prop={len(images)}", flush=True)

        artwork_url = ""
        candidate_fns: list[str] = []
        for img in images:
            fn = img.get("title", "").removeprefix("File:")
            fn_lower = fn.lower()
            if fn_lower.endswith((".jpg", ".jpeg", ".png")) and "logo" not in fn_lower and "icon" not in fn_lower:
                candidate_fns.append(fn)

        # If API returned no images, try wikitext
        if not candidate_fns and wikitext:
            wt_fn = _extract_image_from_wikitext(wikitext)
            if wt_fn:
                candidate_fns.append(wt_fn)
                print(f"[meta:mixesdb] image from wikitext: {wt_fn}", flush=True)

        for fn in candidate_fns:
            print(f"[meta:mixesdb] trying image: {fn}", flush=True)
            artwork_url = await _image_url(fn)
            if artwork_url:
                print(f"[meta:mixesdb] resolved: {artwork_url}", flush=True)
                break

        # Categories
        cats = [c["title"].removeprefix("Category:") for c in (page.get("categories") or [])]

        return {
            "source":     "mixesdb",
            "page_title": page_title,
            "title":      f"{meta['artist']} — {meta['show']}" if meta.get("artist") else page_title,
            "artist":     meta.get("artist", ""),
            "show":       meta.get("show", ""),
            "date":       meta.get("date", ""),
            "artworkUrl": artwork_url,
            "categories": cats,
            "tracklist":  tracklist,
            "url":        f"https://www.mixesdb.com/w/{page_title.replace(' ', '_')}",
        }
    except Exception as e:
        print(f"[meta:mixesdb] {e}", flush=True)
        return None


def _parse_tracklist(wikitext: str) -> list[dict]:
    """Extract timestamped tracks from MixesDB wikitext.

    Lines look like:
      # [[00:00]] Artist - Title (Label)
      # [00:00] Artist - Title
    """
    import re
    tracks = []
    for m in re.finditer(
        r'\[+(\d{1,2}:\d{2}(?::\d{2})?)\]+\s*(.+)',
        wikitext,
    ):
        ts, rest = m.group(1), m.group(2).strip()
        rest = re.sub(r'\[\[([^\]|]+)(?:\|[^\]]*)?\]\]', r'\1', rest)  # unwrap wikilinks
        rest = re.sub(r'\[https?://\S+\s+([^\]]+)\]', r'\1', rest)      # external links
        rest = rest.strip("# ").strip()
        if " - " in rest:
            artist, title = rest.split(" - ", 1)
        else:
            artist, title = "", rest
        tracks.append({"timestamp": ts, "artist": artist.strip(), "title": title.strip()})
    return tracks
