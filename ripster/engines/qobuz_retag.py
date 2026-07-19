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
# "Co-authors" the owner wants preserved: writers/composers/producers behind the
# track. Written to the COMPOSER tag (Qobuz drops them entirely otherwise — streamrip
# only writes the single performing artist). Kept separate from _ARTIST_ROLES so a
# lyricist never pollutes the ARTIST tag.
_COMPOSER_ROLES = {"composer", "composerlyricist", "author", "writer", "lyricist",
                   "songwriter", "author,composer", "musicpublisher"}
_PRODUCER_ROLES = {"producer", "coproducer", "executiveproducer", "mixer",
                   "mixingengineer", "masteringengineer", "engineer"}
_SPLIT_RE = re.compile(
    r'\s*(?:,|;|&|/|\bfeat\.?|\bft\.?|\bfeaturing\b|\bvs\.?|\s+x\s+|\bи\b)\s*', re.I)


def _parse_by_roles(s: str, want_roles: set[str]) -> list[str]:
    """Qobuz `performers` string → names whose roles intersect *want_roles*.
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
        if roles & want_roles and name not in out:
            out.append(name)
    return out


def parse_performers(s: str) -> list[str]:
    """Qobuz `performers` string → artist (performer-role) names only."""
    return _parse_by_roles(s, _ARTIST_ROLES)


def parse_composers(s: str) -> list[str]:
    """Qobuz `performers` string → composer/author/writer ("co-author") names."""
    return _parse_by_roles(s, _COMPOSER_ROLES)


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


def _write_credits(f: Path, arts: list[str], comps: list[str]) -> bool:
    """Write the full ARTIST list (main + featured performers) AND the COMPOSER
    list (co-authors: writers/composers/lyricists). Returns True if anything
    changed. Composers are additive — we never clear an existing composer tag with
    an empty list (Qobuz sometimes omits credits)."""
    ext = f.suffix.lower()
    try:
        if ext == ".flac":
            from mutagen.flac import FLAC
            m = FLAC(f)
            changed = False
            if arts and list(m.get("artist", [])) != arts:
                m["artist"] = arts; changed = True
            if comps and list(m.get("composer", [])) != comps:
                m["composer"] = comps; changed = True
            if changed:
                m.save()
            return changed
        if ext == ".mp3":
            from mutagen.easyid3 import EasyID3
            from mutagen.id3 import ID3NoHeaderError, ID3
            try:
                m = EasyID3(f)
            except ID3NoHeaderError:
                ID3().save(f)
                m = EasyID3(f)
            changed = False
            if arts and list(m.get("artist", [])) != arts:
                m["artist"] = arts; changed = True
            if comps and list(m.get("composer", [])) != comps:
                m["composer"] = comps; changed = True
            if changed:
                m.save(v2_version=4)     # ID3v2.4 → multi-value TPE1/TCOM
            return changed
        if ext in (".m4a", ".mp4"):
            from mutagen.mp4 import MP4
            m = MP4(f)
            changed = False
            if arts and list(m.get("\xa9ART", [])) != arts:
                m["\xa9ART"] = arts; changed = True
            if comps and list(m.get("\xa9wrt", [])) != comps:
                m["\xa9wrt"] = comps; changed = True
            if changed:
                m.save()
            return changed
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

    items = (a.get("tracks") or {}).get("items", [])

    # album/get returns tracks WITHOUT the `performers` credits string (it comes
    # back empty), so featured artists AND co-authors (composers/lyricists) are
    # invisible here. Fetch each track individually — track/get DOES return the full
    # credits — then derive both the artist list and the composer list. Concurrency
    # is capped so a big album doesn't hammer Qobuz.
    import asyncio
    sem = asyncio.Semaphore(4)

    async def _credits(track_id: str) -> str:
        if not track_id:
            return ""
        async with sem:
            try:
                async with httpx.AsyncClient(timeout=20) as c:
                    tr = (await c.get("https://www.qobuz.com/api.json/0.2/track/get",
                                      params={"track_id": track_id, "app_id": app},
                                      headers=headers)).json()
                return tr.get("performers") or ""
            except Exception:
                return ""

    perf_strings = await asyncio.gather(*[_credits(str(t.get("id") or "")) for t in items])

    # (artists, composers) per ISRC and per (disc, track).
    by_isrc: dict[str, tuple[list[str], list[str]]] = {}
    by_pos: dict[tuple[int, int], tuple[list[str], list[str]]] = {}
    for t, perfs in zip(items, perf_strings):
        # Inject the freshly-fetched credits so track_artists()/parse_composers()
        # see them (album/get had them empty).
        if perfs:
            t = {**t, "performers": perfs}
        arts = track_artists(t, album_artists, album_is_va)
        comps = parse_composers(perfs)
        if not arts and not comps:
            continue
        isrc = (t.get("isrc") or "").upper()
        if isrc:
            by_isrc[isrc] = (arts, comps)
        by_pos[(t.get("media_number") or 1, t.get("track_number") or 0)] = (arts, comps)

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
        hit = by_isrc.get(isrc) if isrc else None
        if not hit:
            # Position fallback only when the file's album tag EXACTLY matches this
            # Qobuz album (normalised) — a substring check is unsafe: "…2026" is a
            # substring of "…2026 (Mixed)" and would apply the wrong master's credits.
            file_album_norm = re.sub(r'\W+', '', album or "").lower()
            if album_title_norm and file_album_norm == album_title_norm:
                hit = by_pos.get((disc, tn))
        if hit and _write_credits(f, hit[0], hit[1]):
            n += 1
    return n
