"""Audio tag fixer + filename renamer.

fix_artist_tags(directory):
    Splits slash-joined artist strings ("A / B / C") into proper multi-value
    tags, deduplicates, and writes back. Works on FLAC, MP3, M4A/ALAC, OGG/Opus.
    Safe no-op for files that don't have the issue (Apple, Qobuz, etc.).

rename_from_tags(directory, template):
    Renames audio files using embedded tags and a user template.

Template variables:
  {tracknumber}     raw string (e.g. "1" or "1/12")
  {tracknumber:02d} zero-padded int (e.g. "01")
  {discnumber}      disc number (int)
  {title}           track title
  {artist}          track artist
  {albumartist}     album artist
  {album}           album title
  {year}            4-digit year
  {genre}           first genre tag
  {ext}             file extension without dot — appended automatically
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

_AUDIO_EXTS = {".flac", ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".alac"}


def _same_content(a: Path, b: Path) -> bool:
    """True if two files are byte-identical (cheap size gate, then full compare).
    Used to recognise a redundant re-download so we delete it instead of keeping
    a ``_2`` copy."""
    try:
        if a.stat().st_size != b.stat().st_size:
            return False
        import filecmp
        return filecmp.cmp(a, b, shallow=False)
    except OSError:
        return False

# Characters forbidden in Windows / cross-platform filenames
_BAD_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_SPACE_RUN = re.compile(r'  +')


def _sanitize(s: str) -> str:
    s = _BAD_CHARS.sub(" ", s).strip(" .")
    s = _SPACE_RUN.sub(" ", s)
    return s[:200] or "_"


def _parse_number(raw: str) -> Optional[int]:
    """Parse "3" or "3/12" → 3. Returns None on failure."""
    try:
        return int(str(raw).split("/")[0].strip())
    except (ValueError, IndexError):
        return None


def _read_tags_flac(path: Path) -> dict:
    from mutagen.flac import FLAC
    f = FLAC(path)
    def g(k): return (f.tags.get(k) or [""])[0] if f.tags else ""
    return {
        "title":       g("title"),
        "artist":      g("artist"),
        "albumartist": g("albumartist"),
        "album":       g("album"),
        "tracknumber": g("tracknumber"),
        "discnumber":  g("discnumber"),
        "year":        (g("date") or "")[:4],
        "genre":       g("genre"),
        "isrc":        g("isrc"),
    }


def _read_tags_mp3(path: Path) -> dict:
    from mutagen.id3 import ID3, ID3NoHeaderError
    try:
        tags = ID3(path)
    except ID3NoHeaderError:
        return {}
    def g(k): v = tags.get(k); return str(v.text[0]) if v and v.text else ""
    def year(): v = tags.get("TDRC"); return str(v.text[0])[:4] if v and v.text else ""
    return {
        "title":       g("TIT2"),
        "artist":      g("TPE1"),
        "albumartist": g("TPE2"),
        "album":       g("TALB"),
        "tracknumber": g("TRCK"),
        "discnumber":  g("TPOS"),
        "year":        year(),
        "genre":       g("TCON"),
        "isrc":        g("TSRC"),
    }


def _read_tags_mp4(path: Path) -> dict:
    from mutagen.mp4 import MP4
    f = MP4(path)
    t = f.tags or {}
    def g(k): v = t.get(k); return str(v[0]) if v else ""
    def num(k):
        v = t.get(k)
        if not v: return ""
        try: return str(v[0][0])
        except Exception: return ""
    def year(): v = t.get("©day"); return (str(v[0]) if v else "")[:4]
    def isrc():
        v = t.get("----:com.apple.iTunes:ISRC")
        if not v:
            return ""
        try:
            x = v[0]
            return (x.decode() if isinstance(x, (bytes, bytearray)) else str(x)).strip()
        except Exception:
            return ""
    return {
        "title":       g("©nam"),
        "artist":      g("©ART"),
        "albumartist": g("aART"),
        "album":       g("©alb"),
        "tracknumber": num("trkn"),
        "discnumber":  num("disk"),
        "year":        year(),
        "genre":       g("©gen"),
        "isrc":        isrc(),
    }


def _read_tags_ogg(path: Path) -> dict:
    try:
        if path.suffix.lower() == ".opus":
            from mutagen.oggopus import OggOpus as Cls
        else:
            from mutagen.oggvorbis import OggVorbis as Cls
        f = Cls(path)
        def g(k): return (f.tags.get(k) or [""])[0] if f.tags else ""
        return {
            "title":       g("title"),
            "artist":      g("artist"),
            "albumartist": g("albumartist"),
            "album":       g("album"),
            "tracknumber": g("tracknumber"),
            "discnumber":  g("discnumber"),
            "year":        (g("date") or "")[:4],
            "genre":       g("genre"),
            "isrc":        g("isrc"),
        }
    except Exception:
        return {}


def read_tags(path: Path) -> dict:
    """Return a dict of tag fields for *path*. Empty dict on any failure."""
    try:
        ext = path.suffix.lower()
        if ext == ".flac":
            return _read_tags_flac(path)
        if ext == ".mp3":
            return _read_tags_mp3(path)
        if ext in (".m4a", ".mp4", ".aac", ".alac"):
            return _read_tags_mp4(path)
        if ext in (".ogg", ".opus"):
            return _read_tags_ogg(path)
    except Exception:
        pass
    return {}


def extract_embedded_cover(path: Path) -> Optional[bytes]:
    """Return the raw bytes of the cover art embedded in *path*, or None.
    Format-agnostic (FLAC/MP3/M4A/OGG) — used to derive a folder cover.jpg for
    services that embed art but don't drop a sidecar image."""
    try:
        ext = path.suffix.lower()
        if ext in (".m4a", ".mp4", ".aac", ".alac"):
            from mutagen.mp4 import MP4
            covr = (MP4(str(path)).tags or {}).get("covr")
            return bytes(covr[0]) if covr else None
        if ext == ".flac":
            from mutagen.flac import FLAC
            pics = FLAC(str(path)).pictures
            return bytes(pics[0].data) if pics else None
        if ext == ".mp3":
            from mutagen.id3 import ID3
            apics = ID3(str(path)).getall("APIC")
            return bytes(apics[0].data) if apics else None
        if ext in (".ogg", ".opus"):
            import base64
            from mutagen import File as _MF
            from mutagen.flac import Picture
            f = _MF(str(path))
            b64 = (f.get("metadata_block_picture") or [None])[0] if f else None
            if b64:
                return bytes(Picture(base64.b64decode(b64)).data)
    except Exception:
        pass
    return None


# ── Artist-tag fixer ──────────────────────────────────────────────────────────

def _split_artists(raw: str) -> list[str]:
    """Split 'Artist1 / Artist2 / Artist1' → ['Artist1', 'Artist2'] (deduped, ordered)."""
    parts = [a.strip() for a in raw.split(" / ") if a.strip()]
    seen: set[str] = set()
    out: list[str] = []
    for p in parts:
        if p.lower() not in seen:
            seen.add(p.lower())
            out.append(p)
    return out or [raw.strip()]


def _dedup_list(values: list[str]) -> list[str]:
    """Deduplicate a list of artist strings, preserving order."""
    seen: set[str] = set()
    out: list[str] = []
    for v in values:
        if v.strip().lower() not in seen:
            seen.add(v.strip().lower())
            out.append(v.strip())
    return out


def _needs_fix(values: list[str]) -> bool:
    # Multi-value artist tags (len > 1) show as "A\\B" in tag editors and
    # collapse to a single wrong artist in players like Telegram — so any
    # multi-value tag is joined into one ", "-separated string.
    return (len(values) > 1
            or any(" / " in v for v in values)
            or len(values) != len(_dedup_list(values)))


_FEAT_CLAUSE = re.compile(
    r'\s*[\(\[]\s*(?:feat\.?|ft\.?|featuring)\s+([^\)\]]+?)\s*[\)\]]',
    re.I,
)


def _dedup_title(title: str) -> str:
    """Drop a parenthesised '(feat. X)' clause when X already appears in the
    rest of the title — e.g. a remixer credited a second time:
    'With You Alex O'Rion Remix (feat. Alex O'Rion)' → '… Alex O'Rion Remix'."""
    if not title:
        return title

    def _n(s: str) -> str:
        return re.sub(r'\W+', '', s.casefold())

    out = title
    for m in _FEAT_CLAUSE.finditer(title):
        who  = _n(m.group(1))
        rest = _n(title[:m.start()] + title[m.end():])
        if len(who) >= 3 and who in rest:
            out = out.replace(m.group(0), '')
    return re.sub(r'\s{2,}', ' ', out).strip() or title


def _dedup_artist_str(raw: str) -> str:
    """Collapse REPEATED artists in a string while preserving the original form
    when there are no repeats — 'A & A' → 'A', 'A / A' → 'A', but 'A & B' stays
    'A & B' (we don't reformat distinct collaborators)."""
    if not raw:
        return raw
    parts = re.split(r'\s*(?:/|,|&|;|·|\bfeat\.?\b|\bft\.?\b|\bvs\.?\b)\s*',
                     raw, flags=re.I)
    parts = [p.strip() for p in parts if p.strip()]
    out = _dedup_list(parts)
    if len(out) == len(parts):     # no real duplicates → keep original formatting
        return raw.strip()
    return ", ".join(out)


def smart_clean_fields(fields: dict) -> dict:
    """Apply the project's tag conventions before writing — kill double names.

    * Strip a leading '<artist> - ' from the title (the artist is already its own
      tag, so it must not be duplicated in the title — common on Bandcamp/Beatport
      compilations). Also strip it when '<albumartist> - ' leads.
    * Deduplicate repeated artists into one ', '-joined string.
    * Drop a redundant '(feat. X)' clause already present in the title.
    """
    f = dict(fields)
    title  = (f.get("title") or "").strip()
    artist = (f.get("artist") or "").strip()
    for pref in (artist, f.get("albumartist", "")):
        pref = (pref or "").strip()
        if pref and " - " in title and title.lower().startswith(pref.lower() + " - "):
            title = title[len(pref) + 3:].strip()
            break
    if artist:
        f["artist"] = _dedup_artist_str(artist)
    if f.get("albumartist") and (f["albumartist"] or "").lower() != "various artists":
        f["albumartist"] = _dedup_artist_str(f["albumartist"])
    f["title"] = _dedup_title(title)
    return f


def _fix_flac_artists(path: Path) -> bool:
    from mutagen.flac import FLAC
    f = FLAC(path)
    if not f.tags:
        return False
    changed = False
    for key in ("artist", "albumartist"):
        vals = list(f.tags.get(key) or [])
        if not vals or not _needs_fix(vals):
            continue
        fixed: list[str] = []
        seen: set[str] = set()
        for v in vals:
            for part in (_split_artists(v) if " / " in v else [v.strip()]):
                if part.lower() not in seen:
                    seen.add(part.lower())
                    fixed.append(part)
        joined = [", ".join(fixed)] if fixed else list(vals)
        if joined != list(vals):
            f.tags[key] = joined
            changed = True
    if changed:
        f.save()
    return changed


def _fix_mp3_artists(path: Path) -> bool:
    from mutagen.id3 import ID3, TPE1, TPE2, ID3NoHeaderError
    try:
        tags = ID3(path)
    except ID3NoHeaderError:
        return False
    changed = False
    for fid, Cls in (("TPE1", TPE1), ("TPE2", TPE2)):
        frame = tags.get(fid)
        if not frame:
            continue
        raw_list = list(frame.text)
        # ID3v2.4 null-byte separators + slash separators both handled
        all_parts: list[str] = []
        for v in raw_list:
            for part in v.split("\x00"):
                all_parts.extend(_split_artists(part) if " / " in part else [part.strip()])
        seen: set[str] = set()
        deduped: list[str] = []
        for a in all_parts:
            if a and a.lower() not in seen:
                seen.add(a.lower())
                deduped.append(a)
        joined = ", ".join(deduped)
        if not deduped or raw_list == [joined]:
            continue
        tags[fid] = Cls(encoding=3, text=[joined])
        changed = True
    if changed:
        tags.save(path)
    return changed


def _fix_mp4_artists(path: Path) -> bool:
    from mutagen.mp4 import MP4
    f = MP4(path)
    if not f.tags:
        return False
    changed = False
    # ©ART = track artist, aART = album artist
    for key in ("©ART", "aART"):
        vals = list(f.tags.get(key) or [])
        if not vals or not _needs_fix(vals):
            continue
        all_parts: list[str] = []
        for v in vals:
            all_parts.extend(_split_artists(v) if " / " in v else [v.strip()])
        seen: set[str] = set()
        deduped: list[str] = []
        for p in all_parts:
            if p and p.lower() not in seen:
                seen.add(p.lower())
                deduped.append(p)
        if not deduped:
            continue
        # Single ", "-joined string — plays correctly in Telegram & friends
        new_val = [", ".join(deduped)] if len(deduped) > 1 else [deduped[0]]
        if new_val != list(vals):
            f.tags[key] = new_val
            changed = True
    if changed:
        f.save()
    return changed


def _fix_ogg_artists(path: Path) -> bool:
    try:
        ext = path.suffix.lower()
        if ext == ".opus":
            from mutagen.oggopus import OggOpus as Cls
        else:
            from mutagen.oggvorbis import OggVorbis as Cls
        f = Cls(path)
        if not f.tags:
            return False
        changed = False
        for key in ("artist", "albumartist"):
            vals = list(f.tags.get(key) or [])
            if not vals or not _needs_fix(vals):
                continue
            fixed: list[str] = []
            seen: set[str] = set()
            for v in vals:
                for part in (_split_artists(v) if " / " in v else [v.strip()]):
                    if part.lower() not in seen:
                        seen.add(part.lower())
                        fixed.append(part)
            joined = [", ".join(fixed)] if fixed else list(vals)
            if joined != list(vals):
                f.tags[key] = joined
                changed = True
        if changed:
            f.save()
        return changed
    except Exception:
        return False


def _fix_file_artists(path: Path) -> bool:
    ext = path.suffix.lower()
    if ext == ".flac":             return _fix_flac_artists(path)
    if ext == ".mp3":              return _fix_mp3_artists(path)
    if ext in (".m4a", ".mp4", ".aac", ".alac"): return _fix_mp4_artists(path)
    if ext in (".ogg", ".opus"):   return _fix_ogg_artists(path)
    return False


def fix_artist_tags(directory: Path) -> list[Path]:
    """Fix slash-joined artist tags in all audio files under *directory* (2 levels deep).

    Returns list of files that were modified.
    """
    if not directory.is_dir():
        return []
    modified: list[Path] = []
    # Top-level files + one level of subdirs (multi-disc albums, etc.)
    candidates = list(directory.iterdir())
    for item in list(candidates):
        if item.is_dir():
            candidates.extend(item.iterdir())
    for f in candidates:
        if not (f.is_file() and f.suffix.lower() in _AUDIO_EXTS):
            continue
        try:
            touched   = _fix_file_artists(f)
            cur       = read_tags(f)
            new_title = _dedup_title(cur.get("title", ""))
            if new_title and new_title != cur.get("title", ""):
                if write_tags(f, {"title": new_title}):
                    touched = True
            if touched:
                modified.append(f)
        except Exception as e:
            print(f"[tagger] fix {f.name}: {e}", flush=True)
    return modified


# ── Filename renderer ──────────────────────────────────────────────────────────

def render_filename(template: str, tags: dict, ext: str) -> str:
    """Apply *template* using *tags*, append *ext* (without dot), sanitize.

    Supports both plain ``{key}`` and format-spec ``{key:02d}`` syntax.
    Missing keys are replaced with empty string so the result is always
    a usable filename.
    """
    # Build substitution values: raw strings + parsed ints for numeric fields
    vals: dict = dict(tags)
    for num_key in ("tracknumber", "discnumber"):
        raw = tags.get(num_key, "")
        n   = _parse_number(raw)
        vals[num_key] = n if n is not None else 0

    # Replace each {key} or {key:fmt} using Python format_map
    class _FallbackMap(dict):
        def __missing__(self, key):
            # strip format spec — return empty string for unknown keys
            return ""

    try:
        name = template.format_map(_FallbackMap(vals))
    except (ValueError, KeyError):
        return ""

    name = _sanitize(name)
    if not name:
        return ""
    return f"{name}.{ext.lstrip('.')}"


def rename_from_tags(directory: Path, template: str) -> list[tuple[Path, Path]]:
    """Rename all audio files in *directory* (non-recursive) using *template*.

    Returns list of (old_path, new_path) for files that were actually renamed.
    Files that would get an empty name or whose new name equals the old name
    are silently skipped.  Duplicate resulting names get ``_2``, ``_3``
    suffixes to avoid overwriting.
    """
    if not template or not directory.is_dir():
        return []

    results: list[tuple[Path, Path]] = []
    used_names: set[str] = set()

    # Collect all audio files in the directory (one level only)
    files = sorted(
        f for f in directory.iterdir()
        if f.is_file() and f.suffix.lower() in _AUDIO_EXTS
    )

    for file in files:
        tags = read_tags(file)
        if not tags:
            continue

        ext      = file.suffix.lstrip(".")
        new_name = render_filename(template, tags, ext)
        if not new_name or new_name == file.name:
            used_names.add(file.name.lower())
            continue

        # Redundant re-download guard: if the desired name already exists on disk
        # and that file is byte-identical to this one, this file is a duplicate
        # (AMD region-rotation / auto-retry re-fetches a track whose final name no
        # longer matches its own songNameFormat, so it can't skip it). Drop the
        # redundant copy instead of stamping out a "_2" twin — otherwise every
        # retry doubles the release.
        desired = directory / new_name
        if desired.exists() and desired != file and _same_content(desired, file):
            try:
                file.unlink()
            except OSError:
                pass
            used_names.add(new_name.lower())
            continue

        # Resolve collisions
        stem_candidate, dot, ext_candidate = new_name.rpartition(".")
        candidate = new_name
        counter   = 2
        while candidate.lower() in used_names or (directory / candidate).exists():
            candidate = f"{stem_candidate}_{counter}.{ext_candidate}"
            counter  += 1

        used_names.add(candidate.lower())
        new_path = directory / candidate
        try:
            file.rename(new_path)
            results.append((file, new_path))
        except OSError:
            pass

    return results


# ── Tag writer ─────────────────────────────────────────────────────────────────

# Vorbis-comment field map (FLAC/OGG/Opus): our field name → tag key.
_VORBIS_MAP = {"title": "title", "artist": "artist", "albumartist": "albumartist",
               "album": "album", "track": "tracknumber", "tracktotal": "tracktotal",
               "disc": "discnumber", "year": "date", "genre": "genre", "label": "label"}


def _write_tags_flac(path: Path, fields: dict) -> None:
    from mutagen.flac import FLAC
    f = FLAC(path)
    if f.tags is None:
        f.add_tags()
    for k, vk in _VORBIS_MAP.items():
        if fields.get(k):
            f.tags[vk] = [str(fields[k])]
    f.save()


def _write_tags_mp3(path: Path, fields: dict) -> None:
    from mutagen.id3 import (ID3, ID3NoHeaderError, TIT2, TPE1, TPE2, TALB,
                             TRCK, TPOS, TDRC, TCON, TPUB)
    try:
        tags = ID3(path)
    except ID3NoHeaderError:
        tags = ID3()
    fmap = {"title": (TIT2, "TIT2"), "artist": (TPE1, "TPE1"),
            "albumartist": (TPE2, "TPE2"), "album": (TALB, "TALB"),
            "disc": (TPOS, "TPOS"), "year": (TDRC, "TDRC"),
            "genre": (TCON, "TCON"), "label": (TPUB, "TPUB")}
    for k, (Cls, fid) in fmap.items():
        if fields.get(k):
            tags[fid] = Cls(encoding=3, text=[str(fields[k])])
    trk = str(fields.get("track", "") or "")
    if trk and fields.get("tracktotal"):
        trk = f"{trk}/{fields['tracktotal']}"
    if trk.strip("/"):
        tags["TRCK"] = TRCK(encoding=3, text=[trk])
    tags.save(path)


def _write_tags_mp4(path: Path, fields: dict) -> None:
    from mutagen.mp4 import MP4
    f = MP4(path)
    if f.tags is None:
        f.add_tags()
    kmap = {"title": "©nam", "artist": "©ART", "albumartist": "aART",
            "album": "©alb", "year": "©day", "genre": "©gen"}
    for k, atom in kmap.items():
        if fields.get(k):
            f.tags[atom] = [str(fields[k])]
    if fields.get("track"):
        try:
            f.tags["trkn"] = [(int(str(fields["track"]).split("/")[0] or 0),
                               int(fields.get("tracktotal") or 0))]
        except ValueError:
            pass
    if fields.get("disc"):
        try:
            f.tags["disk"] = [(int(str(fields["disc"]).split("/")[0] or 0), 0)]
        except ValueError:
            pass
    f.save()


def _write_tags_ogg(path: Path, fields: dict) -> None:
    if path.suffix.lower() == ".opus":
        from mutagen.oggopus import OggOpus as Cls
    else:
        from mutagen.oggvorbis import OggVorbis as Cls
    f = Cls(path)
    if f.tags is None:
        f.add_tags()
    for k, vk in _VORBIS_MAP.items():
        if fields.get(k):
            f.tags[vk] = [str(fields[k])]
    f.save()


def _clear_write_mp3(path: Path, f: dict, keep_cover: bool) -> None:
    from mutagen.id3 import (ID3, ID3NoHeaderError, TIT2, TPE1, TPE2, TALB,
                             TRCK, TPOS, TDRC, TCON, TPUB)
    try:
        tags = ID3(path)
    except ID3NoHeaderError:
        tags = ID3()
    pics = tags.getall("APIC") if keep_cover else []
    tags.clear()
    for p in pics:
        tags.add(p)
    add = lambda Cls, v: tags.add(Cls(encoding=3, text=[str(v)])) if v else None
    add(TIT2, f.get("title")); add(TPE1, f.get("artist"))
    add(TPE2, f.get("albumartist")); add(TALB, f.get("album"))
    trk = str(f.get("track", "") or "")
    if trk and f.get("tracktotal"):
        trk = f"{trk}/{f['tracktotal']}"
    add(TRCK, trk if trk.strip("/") else "")
    add(TPOS, f.get("disc")); add(TDRC, f.get("year"))
    add(TCON, f.get("genre")); add(TPUB, f.get("label"))
    tags.save(path)


def _clear_write_flac(path: Path, f: dict, keep_cover: bool) -> None:
    from mutagen.flac import FLAC
    a = FLAC(path)
    pics = list(a.pictures) if keep_cover else []
    a.delete(); a.clear_pictures()
    for p in pics:
        a.add_picture(p)
    if a.tags is None:
        a.add_tags()
    m = {"title": "title", "artist": "artist", "albumartist": "albumartist",
         "album": "album", "track": "tracknumber", "tracktotal": "tracktotal",
         "disc": "discnumber", "year": "date", "genre": "genre", "label": "label"}
    for k, vk in m.items():
        if f.get(k):
            a.tags[vk] = [str(f[k])]
    a.save()


def _clear_write_mp4(path: Path, f: dict, keep_cover: bool) -> None:
    from mutagen.mp4 import MP4
    a = MP4(path)
    covr = a.tags.get("covr") if (keep_cover and a.tags) else None
    if a.tags is not None:
        a.tags.clear()
    else:
        a.add_tags()
    if covr:
        a.tags["covr"] = covr
    km = {"title": "©nam", "artist": "©ART", "albumartist": "aART",
          "album": "©alb", "year": "©day", "genre": "©gen"}
    for k, atom in km.items():
        if f.get(k):
            a.tags[atom] = [str(f[k])]
    if f.get("track"):
        a.tags["trkn"] = [(int(str(f["track"]).split("/")[0] or 0),
                           int(f.get("tracktotal") or 0))]
    a.save()


def _clear_write_ogg(path: Path, f: dict, keep_cover: bool) -> None:
    if path.suffix.lower() == ".opus":
        from mutagen.oggopus import OggOpus as Cls
    else:
        from mutagen.oggvorbis import OggVorbis as Cls
    a = Cls(path)
    pic = a.get("metadata_block_picture") if keep_cover else None
    a.delete()
    if pic:
        a["metadata_block_picture"] = pic
    m = {"title": "title", "artist": "artist", "albumartist": "albumartist",
         "album": "album", "track": "tracknumber", "year": "date", "genre": "genre"}
    for k, vk in m.items():
        if f.get(k):
            a[vk] = [str(f[k])]
    a.save()


def clear_and_write(path: Path, fields: dict, keep_cover: bool = True) -> bool:
    """Strip ALL existing tags (optionally keep the embedded cover) and write a
    clean set: title/artist/albumartist/album/track/tracktotal/disc/year/genre/label.
    Tags are smart-cleaned first (no '<artist> - ' title prefix, deduped artists)."""
    fields = smart_clean_fields(fields)
    try:
        ext = path.suffix.lower()
        if ext == ".mp3":
            _clear_write_mp3(path, fields, keep_cover)
        elif ext == ".flac":
            _clear_write_flac(path, fields, keep_cover)
        elif ext in (".m4a", ".mp4", ".aac", ".alac"):
            _clear_write_mp4(path, fields, keep_cover)
        elif ext in (".ogg", ".opus"):
            _clear_write_ogg(path, fields, keep_cover)
        else:
            return False
        return True
    except Exception as e:
        print(f"[tagger] clear+write {path.name}: {e}", flush=True)
        return False


def write_tags(path: Path, fields: dict) -> bool:
    """Write title/artist/albumartist/album to *path*. Only non-empty fields
    are written. Smart-cleaned first (no double names). Returns True on success."""
    fields = smart_clean_fields(fields)
    try:
        ext = path.suffix.lower()
        if ext == ".flac":
            _write_tags_flac(path, fields)
        elif ext == ".mp3":
            _write_tags_mp3(path, fields)
        elif ext in (".m4a", ".mp4", ".aac", ".alac"):
            _write_tags_mp4(path, fields)
        elif ext in (".ogg", ".opus"):
            _write_tags_ogg(path, fields)
        else:
            return False
        return True
    except Exception as e:
        print(f"[tagger] write {path.name}: {e}", flush=True)
        return False


# ── Cover-art embedder ─────────────────────────────────────────────────────────

def _cover_mime(data: bytes, fallback: str = "image/jpeg") -> str:
    """Sniff the image MIME from magic bytes (so we tag PNG vs JPEG correctly)."""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return fallback


def _embed_cover_flac(path: Path, data: bytes, mime: str) -> None:
    from mutagen.flac import FLAC, Picture
    f = FLAC(path)
    f.clear_pictures()
    pic = Picture()
    pic.type = 3              # front cover
    pic.mime = mime
    pic.desc = "Cover"
    pic.data = data
    f.add_picture(pic)
    f.save()


def _embed_cover_mp3(path: Path, data: bytes, mime: str) -> None:
    from mutagen.id3 import ID3, ID3NoHeaderError, APIC
    try:
        tags = ID3(path)
    except ID3NoHeaderError:
        tags = ID3()
    tags.delall("APIC")
    tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=data))
    tags.save(path)


def _embed_cover_mp4(path: Path, data: bytes, mime: str) -> None:
    from mutagen.mp4 import MP4, MP4Cover
    f = MP4(path)
    if f.tags is None:
        f.add_tags()
    fmt = MP4Cover.FORMAT_PNG if mime == "image/png" else MP4Cover.FORMAT_JPEG
    f.tags["covr"] = [MP4Cover(data, imageformat=fmt)]
    f.save()


def _embed_cover_ogg(path: Path, data: bytes, mime: str) -> None:
    import base64
    from mutagen.flac import Picture
    if path.suffix.lower() == ".opus":
        from mutagen.oggopus import OggOpus as Cls
    else:
        from mutagen.oggvorbis import OggVorbis as Cls
    f = Cls(path)
    pic = Picture()
    pic.type = 3
    pic.mime = mime
    pic.desc = "Cover"
    pic.data = data
    f["metadata_block_picture"] = [base64.b64encode(pic.write()).decode("ascii")]
    f.save()


def embed_cover(path: Path, data: bytes, mime: str = "") -> bool:
    """Embed *data* (raw image bytes) as the front cover, replacing any existing
    art. Format is detected from magic bytes if *mime* is empty. Returns True on
    success. MP4/M4A only accept JPEG/PNG — a WEBP cover is skipped there."""
    if not data:
        return False
    mime = mime or _cover_mime(data)
    try:
        ext = path.suffix.lower()
        if ext == ".flac":
            _embed_cover_flac(path, data, mime)
        elif ext == ".mp3":
            _embed_cover_mp3(path, data, mime)
        elif ext in (".m4a", ".mp4", ".aac", ".alac"):
            if mime not in ("image/jpeg", "image/png"):
                return False
            _embed_cover_mp4(path, data, mime)
        elif ext in (".ogg", ".opus"):
            _embed_cover_ogg(path, data, mime)
        else:
            return False
        return True
    except Exception as e:
        print(f"[tagger] embed cover {path.name}: {e}", flush=True)
        return False


# ── ISRC re-tagger ─────────────────────────────────────────────────────────────

def _has_cjk(s: str) -> bool:
    """True if *s* contains CJK script (Japanese/Chinese/Korean). A foreign-region
    streaming account (e.g. JP Qobuz) localises Western names to katakana — that
    is exactly the case worth re-tagging from a canonical source."""
    for ch in (s or ""):
        o = ord(ch)
        if (0x3040 <= o <= 0x30FF or   # hiragana + katakana
                0x3400 <= o <= 0x9FFF or   # CJK ideographs
                0xAC00 <= o <= 0xD7AF or   # hangul
                0xFF00 <= o <= 0xFFEF):    # fullwidth forms
            return True
    return False


async def _lookup_deezer_isrc(client, isrc: str) -> Optional[dict]:
    try:
        r = await client.get(f"https://api.deezer.com/2.0/track/isrc:{isrc}")
        if r.status_code != 200:
            return None
        d = r.json()
        if d.get("error") or not d.get("title"):
            return None
        artist = (d.get("artist") or {}).get("name", "")
        return {
            "title":       d.get("title", ""),
            "artist":      artist,
            "album":       (d.get("album") or {}).get("title", ""),
            "albumartist": artist,
        }
    except Exception:
        return None


async def _lookup_apple_isrc(client, isrc: str, storefront: str, bearer: str) -> Optional[dict]:
    if not bearer:
        return None
    try:
        r = await client.get(
            f"https://api.music.apple.com/v1/catalog/{storefront}/songs",
            params={"filter[isrc]": isrc},
            headers={"Authorization": f"Bearer {bearer}"},
        )
        if r.status_code != 200:
            return None
        items = r.json().get("data") or []
        if not items:
            return None
        a = items[0].get("attributes") or {}
        if not a.get("name"):
            return None
        return {
            "title":       a.get("name", ""),
            "artist":      a.get("artistName", ""),
            "album":       a.get("albumName", ""),
            "albumartist": a.get("artistName", ""),
        }
    except Exception:
        return None


async def retag_directory(directory: Path, config: dict, log=None) -> dict:
    """Re-tag CJK-localised audio files in *directory* from a canonical source.

    For each file whose title/artist contains CJK script, the ISRC is looked
    up on Deezer (then Apple Music as fallback) and the Latin metadata is
    written back. Files without CJK or without a usable ISRC are left as-is —
    so already-correct tags are never mangled.
    """
    import httpx
    summary = {"checked": 0, "retagged": 0, "skipped": 0}
    if not directory.is_dir():
        return summary

    storefront = (config.get("storefront") or "us").strip().lower() or "us"
    bearer     = (config.get("authorization-token") or "").strip()

    files = [f for f in directory.iterdir()
             if f.is_file() and f.suffix.lower() in _AUDIO_EXTS]
    for sub in [x for x in directory.iterdir() if x.is_dir()]:
        files.extend(f for f in sub.iterdir()
                     if f.is_file() and f.suffix.lower() in _AUDIO_EXTS)
    if not files:
        return summary

    async with httpx.AsyncClient(timeout=10) as client:
        for f in files:
            summary["checked"] += 1
            tags = read_tags(f)
            if not (_has_cjk(tags.get("title", "")) or _has_cjk(tags.get("artist", ""))):
                summary["skipped"] += 1
                continue
            isrc = (tags.get("isrc") or "").strip().upper().replace("-", "")
            if len(isrc) < 11:
                summary["skipped"] += 1
                continue
            meta = await _lookup_deezer_isrc(client, isrc)
            if not meta:
                meta = await _lookup_apple_isrc(client, isrc, storefront, bearer)
            if not meta or not meta.get("title"):
                summary["skipped"] += 1
                continue
            new_fields = {k: v for k, v in meta.items()
                          if v and v != tags.get(k, "")}
            if new_fields and write_tags(f, new_fields):
                summary["retagged"] += 1
                if log:
                    log(f"{f.name} → {meta.get('artist','')} — {meta.get('title','')}")
            else:
                summary["skipped"] += 1
    return summary


# ── Apple wrong-storefront placeholder fixer ───────────────────────────────────
# A DJ mix / pre-release pulled by the LOCAL zhaarey wrapper in the wrong
# storefront comes out with placeholder metadata: empty title tags and names
# like "00. AppleMusic". The ISRC retagger above can't help (no ISRC, no CJK),
# and rename_from_tags then LOCKS the junk in as "AppleMusic 01.m4a". This fixer
# fetches the authoritative tracklist from amp-api using the storefront IN THE
# URL (not the account's) and writes real tags by matching files to tracks by
# DURATION — the only reliable signal, since broken files carry no track number.
# It must run BEFORE rename_from_tags so the rename uses the corrected tags.

_APPLE_ALBUM_RE = re.compile(r"/([a-z]{2})/album/[^/]+/(\d+)", re.I)


def apple_release_needs_retag(directory: Path) -> bool:
    """True when an Apple release dir holds placeholder files — an 'AppleMusic'
    name, or an empty title tag. Cheap pre-check so we only hit amp-api when
    something is actually broken (healthy releases are never touched)."""
    try:
        files = [f for f in directory.glob("*.m4a")]
    except OSError:
        return False
    if not files:
        return False
    for f in files:
        if "applemusic" in f.name.lower():
            return True
    from mutagen.mp4 import MP4
    for f in files[:4]:
        try:
            if not (MP4(str(f)).get("\xa9nam") or [""])[0].strip():
                return True
        except Exception:
            continue
    return False


async def _apple_album_tracklist(url: str, config: dict):
    """(album_name, [tracks]) from amp-api using the URL's storefront. Tracks
    carry n/disc/name/artist/dur. Raises on a non-album URL or API error."""
    import httpx
    m = _APPLE_ALBUM_RE.search(url or "")
    if not m:
        raise ValueError("not an Apple album URL")
    sf, aid = m.group(1).lower(), m.group(2)
    bearer = (config.get("authorization-token") or "").strip()
    mut    = (config.get("media-user-token") or "").strip()
    auth = bearer if bearer.lower().startswith("bearer") else "Bearer " + bearer
    h = {"Authorization": auth, "Music-User-Token": mut,
         "Origin": "https://music.apple.com"}
    async with httpx.AsyncClient(timeout=25) as cl:
        r = await cl.get(
            f"https://amp-api.music.apple.com/v1/catalog/{sf}/albums/{aid}",
            params={"include": "tracks"}, headers=h)
        r.raise_for_status()
        data = r.json()["data"][0]
        album = data["attributes"]["name"]
        tr = []
        for t in data["relationships"]["tracks"]["data"]:
            a = t["attributes"]
            tr.append({"n": a.get("trackNumber"), "disc": a.get("discNumber", 1),
                       "name": a.get("name", ""), "artist": a.get("artistName", ""),
                       "dur": a.get("durationInMillis", 0) / 1000.0})
        return album, tr


async def retag_apple_placeholders(directory: Path, url: str, config: dict,
                                   log=None) -> int:
    """Fix an Apple release whose local-wrapper download produced 'AppleMusic'
    placeholder tags. Fetches the real tracklist (storefront from *url*), matches
    local *.m4a files to it by duration, and writes proper title/artist/album/
    track-number tags IN PLACE. Returns the number of files retagged (0 = nothing
    to do / couldn't help). Only tags — never deletes or renames audio — so it is
    safe to call speculatively before rename_from_tags."""
    if not directory.is_dir():
        return 0
    if not _APPLE_ALBUM_RE.search(url or ""):
        return 0
    if not apple_release_needs_retag(directory):
        return 0
    try:
        album, tracks = await _apple_album_tracklist(url, config)
    except Exception as e:
        if log:
            log(f"amp-api tracklist failed: {e}")
        return 0
    if not tracks:
        return 0

    from mutagen.mp4 import MP4
    files = sorted(directory.glob("*.m4a"))
    if not files:
        return 0

    # Pair files ↔ tracks by duration rank (broken files carry no track number).
    fd = []
    for f in files:
        try:
            fd.append((f, MP4(str(f)).info.length))
        except Exception:
            fd.append((f, 0.0))
    fd.sort(key=lambda x: x[1])
    tracks_by_dur = sorted(tracks, key=lambda t: t["dur"])
    n = min(len(fd), len(tracks_by_dur))
    mapping = {fd[i][0]: tracks_by_dur[i] for i in range(n)}
    total = len(tracks)

    fixed = 0
    for f, _dur in fd:
        tr = mapping.get(f)
        if not tr:
            continue
        try:
            m = MP4(str(f))
            m["\xa9nam"] = tr["name"]
            m["\xa9ART"] = tr["artist"]
            m["\xa9alb"] = album
            m["aART"]    = tr["artist"]
            m["trkn"]    = [(tr["n"] or 0, total)]
            m["disk"]    = [(tr["disc"], tr["disc"])]
            m.save()
            fixed += 1
            if log:
                log(f"{f.name} → {tr['n']:>2}. {tr['artist']} — {tr['name']}")
        except Exception as e:
            if log:
                log(f"tag err {f.name}: {e}")
    return fixed
