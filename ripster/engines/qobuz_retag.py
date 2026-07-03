"""Qobuz post-download artist retag.

streamrip (which tags Qobuz downloads) reads only the single `performer.name`
field, so a collaboration lands with ONE artist (e.g. "Friendly Fire" by four
artists → tag "Kiskadee"), or all artists crammed into one comma string. This
re-derives the FULL artist list from the Qobuz API and writes proper *multiple*
ARTIST values (native for FLAC/Vorbis; TPE1 multi-value for MP3; ©ART for M4A).

Artist sources per track, in order:
  1. `performers` credits string — names whose roles are artist roles.
  2. else split `performer.name` on separators (", " / " & " / "feat" / …).
  3. else (small NON-Various collab album) fall back to album-level `artists`.
"""
from __future__ import annotations

import re
from pathlib import Path

_QOBUZ_DEFAULT_APP_ID = "798273057"

_ARTIST_ROLES = {"mainartist", "performer", "featuredartist", "featuring",
                 "associatedperformer", "artist", "soloist"}
_SPLIT_RE = re.compile(
    r'\s*(?:,|;|&|/|\bfeat\.?|\bft\.?|\bfeaturing\b|\bvs\.?|\s+x\s+|\bи\b)\s*', re.I)


def parse_performers(s: str) -> list[str]:
    """Qobuz `performers` string → artist names (only artist-role entries).
    Format: 'Name, Role1, Role2 - Name2, Role - …' (entries split by ' - ')."""
    out: list[str] = []
    if not s:
        return out
    for entry in s.split(" - "):
        parts = [p.strip() for p in entry.split(",")]
        if not parts or not parts[0]:
            continue
        name = parts[0]
        roles = {r.strip().lower().replace(" ", "") for r in parts[1:]}
        if roles & _ARTIST_ROLES and name not in out:
            out.append(name)
    return out


def split_joined(name: str) -> list[str]:
    """Split a joined artist string into individual names (order-preserving)."""
    if not name:
        return []
    seen, out = set(), []
    for p in _SPLIT_RE.split(name):
        p = p.strip()
        if p and p.lower() not in seen:
            seen.add(p.lower())
            out.append(p)
    return out or [name]


def _dedupe(names: list[str]) -> list[str]:
    seen, out = set(), []
    for n in names:
        if n and n.lower() not in seen:
            seen.add(n.lower())
            out.append(n)
    return out


def track_artists(track: dict, album_artists: list[str], album_is_va: bool) -> list[str]:
    """Full artist list for one Qobuz track."""
    names = parse_performers(track.get("performers") or "")
    if not names:
        perf = track.get("performer")
        pn = perf.get("name", "") if isinstance(perf, dict) else ""
        names = split_joined(pn)
    # Album-level enrichment: a small real collab (not Various Artists) where the
    # track resolved to ≤1 artist → use the album's full artist list.
    if len(names) <= 1 and not album_is_va and 2 <= len(album_artists) <= 6:
        if not names or names[0] in album_artists:
            names = list(album_artists)
    return _dedupe(names)


# ── tag I/O ──────────────────────────────────────────────────────────────────
def _read_match_keys(f: Path):
    """Return (isrc, disc, trackno, album) for matching a file to a Qobuz track."""
    ext = f.suffix.lower()
    try:
        if ext == ".flac":
            from mutagen.flac import FLAC
            m = FLAC(f)
            isrc = (m.get("isrc", [""])[0] or "").upper()
            disc = int((m.get("discnumber", ["1"])[0] or "1").split("/")[0])
            tn = int((m.get("tracknumber", ["0"])[0] or "0").split("/")[0])
            album = (m.get("album", [""])[0] or "")
        elif ext == ".mp3":
            from mutagen.easyid3 import EasyID3
            m = EasyID3(f)
            isrc = (m.get("isrc", [""])[0] or "").upper()
            disc = int((m.get("discnumber", ["1"])[0] or "1").split("/")[0])
            tn = int((m.get("tracknumber", ["0"])[0] or "0").split("/")[0])
            album = (m.get("album", [""])[0] or "")
        elif ext in (".m4a", ".mp4"):
            from mutagen.mp4 import MP4
            m = MP4(f)
            isrc = ""
            disc = (m.get("disk", [(1, 0)]) or [(1, 0)])[0][0]
            tn = (m.get("trkn", [(0, 0)]) or [(0, 0)])[0][0]
            album = (m.get("\xa9alb", [""]) or [""])[0]
        else:
            return None
    except Exception:
        return None
    return isrc, disc, tn, album


def _write_artists(f: Path, arts: list[str]) -> bool:
    ext = f.suffix.lower()
    try:
        if ext == ".flac":
            from mutagen.flac import FLAC
            m = FLAC(f)
            if list(m.get("artist", [])) == arts:
                return False
            m["artist"] = arts
            m.save()
            return True
        if ext == ".mp3":
            from mutagen.easyid3 import EasyID3
            from mutagen.id3 import ID3NoHeaderError, ID3
            try:
                m = EasyID3(f)
            except ID3NoHeaderError:
                ID3().save(f)
                m = EasyID3(f)
            if list(m.get("artist", [])) == arts:
                return False
            m["artist"] = arts
            m.save(v2_version=4)     # ID3v2.4 → multi-value TPE1
            return True
        if ext in (".m4a", ".mp4"):
            from mutagen.mp4 import MP4
            m = MP4(f)
            if list(m.get("\xa9ART", [])) == arts:
                return False
            m["\xa9ART"] = arts
            m.save()
            return True
    except Exception:
        return False
    return False


async def retag_qobuz_album(album_id: str, config: dict, folder: str) -> int:
    """Fetch the Qobuz album and rewrite ARTIST tags on the downloaded files.
    Returns how many files were retagged. Best-effort — never raises."""
    try:
        import httpx
    except Exception:
        return 0
    app = (config.get("qobuz-app-id") or "").strip() or _QOBUZ_DEFAULT_APP_ID
    tok = (config.get("qobuz-auth-token") or "").strip()
    headers = {"X-User-Auth-Token": tok} if tok else {}
    try:
        async with httpx.AsyncClient(timeout=25) as c:
            r = await c.get("https://www.qobuz.com/api.json/0.2/album/get",
                            params={"album_id": album_id, "app_id": app}, headers=headers)
            a = r.json()
    except Exception:
        return 0
    if not isinstance(a, dict) or a.get("status") == "error":
        return 0

    album_artists = [x.get("name") for x in (a.get("artists") or []) if x.get("name")]
    aartist = (a.get("artist") or {}).get("name", "") if isinstance(a.get("artist"), dict) else ""
    album_is_va = ("various" in aartist.lower()) or len(album_artists) > 6
    album_title_norm = re.sub(r'\W+', '', (a.get("title") or "")).lower()

    by_isrc: dict[str, list[str]] = {}
    by_pos: dict[tuple[int, int], list[str]] = {}
    for t in (a.get("tracks") or {}).get("items", []):
        arts = track_artists(t, album_artists, album_is_va)
        if not arts:
            continue
        isrc = (t.get("isrc") or "").upper()
        if isrc:
            by_isrc[isrc] = arts
        by_pos[(t.get("media_number") or 1, t.get("track_number") or 0)] = arts

    if not by_isrc and not by_pos:
        return 0

    n = 0
    for f in Path(folder).rglob("*"):
        if f.suffix.lower() not in (".flac", ".mp3", ".m4a", ".mp4"):
            continue
        keys = _read_match_keys(f)
        if not keys:
            continue
        isrc, disc, tn, album = keys
        arts = by_isrc.get(isrc) if isrc else None
        if not arts:
            # Position fallback only when the file's album tag EXACTLY matches this
            # Qobuz album (normalised) — a substring check is unsafe: "…2026" is a
            # substring of "…2026 (Mixed)" and would apply the wrong master's artists.
            file_album_norm = re.sub(r'\W+', '', album or "").lower()
            if album_title_norm and file_album_norm == album_title_norm:
                arts = by_pos.get((disc, tn))
        if arts and _write_artists(f, arts):
            n += 1
    return n
