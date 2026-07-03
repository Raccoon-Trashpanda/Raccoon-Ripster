"""DJ-mix builder: merge the (already beat-mixed / gapless) tracks of a release
into ONE continuous file plus a matching CUE sheet, with a clean "essence" name.

This reproduces the xrecode "merge per folder + create cue" workflow headlessly
via ffmpeg (xrecode's GUI build can't be driven from the CLI). Output file and
CUE share the exact same base name, so the CUE links correctly in players.

Public API:
    clean_mix_name(album, artist) -> str
    build_mix(tracks, out_dir, out_name, fmt, ffmpeg="ffmpeg") -> dict
"""
from __future__ import annotations

import json
import re
import subprocess
import threading
from pathlib import Path

# ── Cooperative cancellation ────────────────────────────────────────────────────
# One global flag (the Coder runs one job at a time — a single local tool). The
# long-running loops below check it BETWEEN files, so the file currently being
# encoded finishes cleanly (no half-written output) before the job stops. The mix
# builder (one long ffmpeg) also checks mid-stream and cleans its partial file.
_CANCEL = threading.Event()


def request_cancel() -> None:
    _CANCEL.set()


def reset_cancel() -> None:
    _CANCEL.clear()


def is_cancelled() -> bool:
    return _CANCEL.is_set()


# Trailing mix/credit tags to drop from a clean name:
# "(DJ Mix)", "[Continuous Mix]", "(Mixed)", "(DJ Set)", "(Mixed by X)",
# "(Selected by X)", "(Compiled by X)", "(Continuous DJ Mix)". Location tags like
# "(Live from …)" are kept — they carry meaning.
_MIX_SUFFIX = re.compile(
    r"\s*[\(\[]\s*(?:"
    r"(?:continuous\s+)?dj[ \-]?mix|continuous(?:\s+mix)?|full\s+mix|megamix|"
    r"mixed(?:\s+by\s+[^\)\]]+)?|dj\s*set|"
    r"(?:selected|compiled|presented|curated)\s+by\s+[^\)\]]+|"
    r"mix"
    r")\s*[\)\]]\s*$",
    re.IGNORECASE,
)
_ILLEGAL = re.compile(r'[<>"/\\|?*\x00-\x1f]')

# Output format → (extension, cue FILE type, ffmpeg encode args). "source" is
# resolved per-input at call time.
_FMT = {
    "mp3":  (".mp3",  "MP3",  ["-c:a", "libmp3lame", "-b:a", "320k"]),
    "flac": (".flac", "WAVE", ["-c:a", "flac", "-compression_level", "8"]),
    "alac": (".m4a",  "WAVE", ["-c:a", "alac"]),
}

# Lossless source codecs — only these concatenate sample-accurately. Lossy codecs
# (mp3/aac) carry encoder delay + padding on every file, so merging them inserts
# a few ms of gap/click at each track boundary and breaks a seamless DJ mix.
_LOSSLESS = {"flac", "alac", "wav", "pcm_s16le", "pcm_s24le", "pcm_s32le",
             "ape", "wavpack", "tak", "aiff"}


def _ffprobe_for(ffmpeg: str) -> str:
    return re.sub(r"ffmpeg(\.exe)?$", lambda m: "ffprobe" + (m.group(1) or ""),
                  ffmpeg) if ffmpeg else "ffprobe"


def _sanitize(name: str) -> str:
    name = _ILLEGAL.sub("", name)
    name = re.sub(r"\s+", " ", name).strip().strip(".")
    return name[:180] or "mix"


def clean_mix_name(album: str, artist: str) -> str:
    """Derive the clean output name from album + artist tags.

    Daniel Avery / "NAINA Presents: … Vol. 58 (DJ Mix)" -> NAINA Presents - …, Vol. 58
    oskø / "Connecting The Dots (DJ Mix)"               -> oskø - Connecting The Dots
    obli / "obli presents: Earth Day 2026 (DJ Mix)"     -> obli - Earth Day 2026
    """
    name   = (album or "").strip()
    artist = (artist or "").strip()
    name = _MIX_SUFFIX.sub("", name).strip()
    # "<presenter> presents:|_ <rest>"  (underscore = a filesystem-sanitized colon)
    m = re.match(r"^(.*?)\s+presents\s*[:_]\s+(.*)$", name, re.IGNORECASE)
    if m:
        pres, rest = m.group(1).strip(), m.group(2).strip()
        if pres.lower() == artist.lower():
            name = f"{pres} - {rest}"            # presenter == artist → drop "presents"
        else:
            name = f"{pres} Presents - {rest}"   # keep as a series label
    else:
        name = re.sub(r"\s*[:_]\s+", " - ", name)  # bare colon/underscore → " - "
    if " - " not in name and artist:
        name = f"{artist} - {name}"
    return _sanitize(name)


def clean_mix_title(album: str, artist: str) -> str:
    """The TAG title/album for the mix — just the essence, no redundant prefix.

    When the presenter is the same as the artist (already in the artist tag),
    drop the "<artist> presents:" part entirely:
      obli / "obli presents: Earth Day 2026 (DJ Mix)" -> Earth Day 2026
    Otherwise keep the full release title (minus the mix suffix):
      Daniel Avery / "NAINA Presents: … Vol. 58 (DJ Mix)" -> NAINA Presents: … Vol. 58
    """
    name   = _MIX_SUFFIX.sub("", (album or "").strip()).strip()
    artist = (artist or "").strip()
    m = re.match(r"^(.*?)\s+presents\s*[:_]?\s+(.*)$", name, re.IGNORECASE)
    if m and m.group(1).strip().lower() == artist.lower():
        return m.group(2).strip()
    return name


def _probe(ffprobe: str, f: Path) -> dict:
    """Return per-track + album-level tags and codec for a source file."""
    try:
        cp = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries",
             "format=duration:format_tags=title,artist,album,album_artist,"
             "date,genre,copyright,publisher,track,disc:stream=codec_name",
             "-select_streams", "a:0", "-of", "json", str(f)],
            capture_output=True, timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        d = json.loads(cp.stdout or b"{}")
        fmt = d.get("format", {}) or {}
        tags = {k.lower(): v for k, v in (fmt.get("tags", {}) or {}).items()}
        streams = d.get("streams", []) or [{}]
        return {
            "duration":     float(fmt.get("duration", 0) or 0),
            "title":        tags.get("title", "") or f.stem,
            "artist":       tags.get("artist", "") or tags.get("album_artist", ""),
            "album":        tags.get("album", ""),
            "album_artist": tags.get("album_artist", "") or tags.get("artist", ""),
            "date":         tags.get("date", ""),
            "genre":        tags.get("genre", ""),
            "copyright":    tags.get("copyright", ""),
            "track":        tags.get("track", ""),
            "disc":         tags.get("disc", ""),
            "codec":        (streams[0] or {}).get("codec_name", ""),
        }
    except Exception:
        return {"duration": 0.0, "title": f.stem, "artist": "", "album": "",
                "album_artist": "", "date": "", "genre": "", "copyright": "",
                "track": "", "disc": "", "codec": ""}


def _fmt_name(template: str, m: dict) -> str:
    """Build a filename from a tag template ({tracknumber}/{artist}/{title}/{album})."""
    tn = str(m.get("track", "")).split("/")[0].strip()
    try:
        tn = f"{int(tn):02d}"
    except Exception:
        pass
    out = template
    for k, v in (("{tracknumber}", tn), ("{track}", tn),
                 ("{artist}", m.get("artist", "")), ("{title}", m.get("title", "")),
                 ("{album}", m.get("album", ""))):
        out = out.replace(k, str(v or ""))
    return _sanitize(out) or _sanitize(m.get("title", "") or "track")


def _prepare_cover(ffmpeg: str, src_dir: Path, track0: Path,
                   tmp_dir: Path) -> Path | None:
    """Produce a 1000×1000 JPEG cover for the mix — from a cover file in the
    release folder, else extracted from the first track's embedded art."""
    src = None
    for n in ("cover.jpg", "cover.jpeg", "cover.png", "folder.jpg",
              "front.jpg", "Cover.jpg"):
        c = src_dir / n
        if c.is_file() and c.stat().st_size > 0:
            src = c
            break
    inp = str(src) if src else str(track0)   # track0 carries embedded art
    out = tmp_dir / "._coder_cover.jpg"
    try:
        cp = subprocess.run(
            [ffmpeg, "-y", "-hide_banner", "-i", inp, "-map", "0:v:0?",
             "-frames:v", "1", "-vf",
             "scale=1000:1000:force_original_aspect_ratio=decrease,"
             "pad=1000:1000:(ow-iw)/2:(oh-ih)/2:color=black",
             "-q:v", "2", str(out)],
            capture_output=True, timeout=120,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if cp.returncode == 0 and out.exists() and out.stat().st_size > 0:
            return out
    except Exception:
        pass
    return None


def _cue_time(sec: float) -> str:
    """Seconds -> CUE MM:SS:FF (75 frames per second)."""
    frames = int(round(sec * 75))
    mm, rem = divmod(frames, 75 * 60)
    ss, ff = divmod(rem, 75)
    return f"{mm:02d}:{ss:02d}:{ff:02d}"


def _cue_escape(s: str) -> str:
    return (s or "").replace('"', "'")


# Per-track convert targets: ext + ffmpeg encode args ({br} = bitrate like "320k").
# Container formats that keep an embedded cover (attached_pic) are flagged.
_CONVERT = {
    "mp3":  (".mp3",  True,  ["-c:a", "libmp3lame", "-b:a", "{br}", "-id3v2_version", "3"]),
    "aac":  (".m4a",  True,  ["-c:a", "aac", "-b:a", "{br}"]),
    "alac": (".m4a",  True,  ["-c:a", "alac"]),
    "flac": (".flac", True,  ["-c:a", "flac", "-compression_level", "8"]),
    "ogg":  (".ogg",  False, ["-c:a", "libvorbis", "-b:a", "{br}"]),
    "opus": (".opus", False, ["-c:a", "libopus", "-b:a", "{br}"]),
    "wav":  (".wav",  False, ["-c:a", "pcm_s16le"]),
}


def convert_tracks(files: list[str], out_dir: str, fmt: str = "mp3",
                   bitrate: str = "320k", ffmpeg: str = "ffmpeg",
                   keep_cover: bool = True, rename_template: str = "",
                   progress=None, sample_rate: str = "", bit_depth: str = "",
                   normalize: bool = False) -> dict:
    """Batch per-track convert. Tags are carried over via -map_metadata 0;
    embedded cover is preserved for container formats that support it. With
    `rename_template` the output is renamed from its tags ({tracknumber}/
    {artist}/{title}/{album}). `sample_rate` (e.g. "44100") resamples; `bit_depth`
    ("16"/"24") sets the lossless sample format. Returns {ok, converted, failed,
    out_dir, files}."""
    import os as _os
    fmt = (fmt or "mp3").lower()
    if fmt not in _CONVERT:
        return {"ok": False, "error": f"unknown format {fmt}"}
    ext, can_cover, enc_t = _CONVERT[fmt]
    enc = [a.replace("{br}", bitrate) for a in enc_t]
    # Bit-depth: WAV swaps the PCM codec; FLAC/ALAC take a sample_fmt (s16/s32).
    sample_rate = (sample_rate or "").strip()
    bit_depth   = (bit_depth or "").strip()
    if bit_depth and fmt == "wav":
        enc = ["-c:a", {"16": "pcm_s16le", "24": "pcm_s24le",
                        "32": "pcm_s32le"}.get(bit_depth, "pcm_s16le")]
    resample: list = []
    if sample_rate:
        resample += ["-ar", sample_rate]
    if bit_depth and fmt in ("flac", "alac"):
        sf = {"16": "s16", "24": "s32", "32": "s32"}.get(bit_depth, "")
        if sf:
            resample += ["-sample_fmt", sf + "p" if fmt == "alac" else sf]
    # EBU R128 loudness normalization (single-pass, streaming-style -14 LUFS).
    afilter = ["-af", "loudnorm=I=-14:TP=-1.5:LRA=11"] if normalize else []
    ffprobe = _ffprobe_for(ffmpeg)
    out_dir_p = Path(out_dir)
    out_dir_p.mkdir(parents=True, exist_ok=True)
    paths = [Path(f) for f in files if Path(f).is_file()]
    done, fail, names = 0, 0, []
    for p in paths:
        if _CANCEL.is_set():           # stop between files — current one already done
            break
        stem = p.stem
        if rename_template:
            stem = _fmt_name(rename_template, _probe(ffprobe, p)) or stem
        out = out_dir_p / (stem + ext)
        if out.resolve() == p.resolve():           # same path → avoid clobber
            out = out_dir_p / (stem + ".conv" + ext)
        cmd = [ffmpeg, "-y", "-hide_banner", "-i", str(p)]
        if can_cover and keep_cover:
            cmd += ["-map", "0:a", "-map", "0:v?", "-c:v", "copy",
                    "-disposition:v:0", "attached_pic"]
        else:
            cmd += ["-map", "0:a"]
        cmd += ["-map_metadata", "0", *afilter, *enc, *resample, str(out)]
        try:
            cp = subprocess.run(cmd, capture_output=True, timeout=1800,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if cp.returncode == 0 and out.exists() and out.stat().st_size > 0:
                done += 1
                names.append(out.name)
            else:
                fail += 1
        except Exception:
            fail += 1
        if progress:
            n = done + fail
            try: progress(n, len(paths), p.name, int(n / len(paths) * 100))
            except Exception: pass
    return {"ok": done > 0, "converted": done, "failed": fail,
            "out_dir": str(out_dir_p), "files": names,
            "cancelled": _CANCEL.is_set()}


def build_mix(tracks: list[str], out_dir: str, out_name: str,
              fmt: str = "mp3", ffmpeg: str = "ffmpeg",
              mix_title: str = "", mix_artist: str = "", progress=None) -> dict:
    """Merge `tracks` (ordered) into one file + CUE in `out_dir` named `out_name`.

    `progress(current_track, total_tracks, label, pct)` is called as ffmpeg
    advances (pct 0-100, current_track = which source the encoder is on).

    fmt: "mp3" (320 CBR) | "flac" | "source" (lossless passthrough/concat-copy).
    Returns {"ok", "file", "cue", "tracks", "error"}.
    """
    ffprobe = _ffprobe_for(ffmpeg)
    paths = [Path(t) for t in tracks if Path(t).is_file()]
    if not paths:
        return {"ok": False, "error": "no input tracks"}
    out_dir_p = Path(out_dir)
    out_dir_p.mkdir(parents=True, exist_ok=True)
    base = _sanitize(out_name)

    metas = [_probe(ffprobe, p) for p in paths]

    # Lossy sources can't be merged gaplessly (encoder delay/padding per file).
    src_codecs = {m["codec"] for m in metas if m["codec"]}
    lossy = bool(src_codecs - _LOSSLESS)
    warning = ("Источник lossy (" + ", ".join(sorted(src_codecs)) + ") — у каждого "
               "трека есть padding, на стыках возможны микро-щелчки. Для идеально "
               "бесшовного микса качай альбом в ALAC/FLAC.") if lossy else ""

    # Resolve output format / extension / encode args.
    if fmt == "source":
        if metas[0]["codec"] == "alac":
            ext, cue_type, enc = _FMT["alac"]
        else:
            ext, cue_type, enc = _FMT["flac"]
    else:
        ext, cue_type, enc = _FMT.get(fmt, _FMT["mp3"])

    out_file = out_dir_p / f"{base}{ext}"
    out_cue  = out_dir_p / f"{base}.cue"

    # Album-level tags for the mix (the file should be tagged as ONE release, not
    # inherit the first track's title/artist). Prefer explicit mix_* overrides.
    am          = metas[0] if metas else {}
    tag_title   = mix_title or base
    tag_artist  = mix_artist or am.get("album_artist") or am.get("artist") or ""
    tag_album   = mix_title or am.get("album") or base
    tag_date    = am.get("date", "")
    tag_genre   = am.get("genre", "")

    # 1000×1000 cover, from a cover file in the folder or the first track's art.
    cover = _prepare_cover(ffmpeg, paths[0].parent, paths[0], out_dir_p)

    # Sample-accurate join: decode every track to PCM via the concat FILTER and
    # concatenate in the PCM domain, then encode ONCE. With lossless sources this
    # is bit-perfect gapless (a single encoder priming at the very start only,
    # not one per track-boundary like demuxer concat of pre-encoded files).
    cmd = [ffmpeg, "-y", "-hide_banner"]
    for p in paths:
        cmd += ["-i", str(p)]
    if cover:
        cmd += ["-i", str(cover)]
    filt = "".join(f"[{i}:a]" for i in range(len(paths))) + \
           f"concat=n={len(paths)}:v=0:a=1[a]"
    cmd += ["-filter_complex", filt, "-map", "[a]"]
    if cover:
        cidx = len(paths)
        cmd += ["-map", f"{cidx}:v", "-c:v", "mjpeg",
                "-disposition:v:0", "attached_pic",
                "-metadata:s:v", "title=Album cover", "-metadata:s:v", "comment=Cover (front)"]
    # Drop ALL inherited per-track metadata, then write clean album-level tags.
    cmd += ["-map_metadata", "-1"]
    for k, v in (("title", tag_title), ("artist", tag_artist),
                 ("album", tag_album), ("album_artist", tag_artist),
                 ("date", tag_date), ("genre", tag_genre),
                 ("comment", f"DJ Mix · {len(paths)} tracks · Ripster Coder")):
        if v:
            cmd += ["-metadata", f"{k}={v}"]
    # Stream ffmpeg's progress so the UI can show a live bar + current track.
    total_dur = sum(m["duration"] for m in metas) or 0.0
    cum, _acc = [], 0.0
    for m in metas:
        _acc += m["duration"]; cum.append(_acc)
    cmd += [*enc, "-progress", "pipe:1", "-nostats", str(out_file)]
    import tempfile as _tf
    errf = _tf.TemporaryFile()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errf,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    _last = -1
    try:
        for raw in proc.stdout:
            if _CANCEL.is_set():
                # Kill the encoder and drop the partial output — a half-rendered
                # mix must never be mistaken for a finished one.
                proc.kill()
                errf.close()
                try: out_file.unlink()
                except Exception: pass
                if cover:
                    try: cover.unlink()
                    except Exception: pass
                return {"ok": False, "cancelled": True, "error": "отменено"}
            line = raw.decode("utf-8", "ignore").strip()
            if line.startswith("out_time_us=") and total_dur > 0 and progress:
                try:
                    t = int(line.split("=", 1)[1]) / 1_000_000
                except Exception:
                    continue
                cur = min(sum(1 for c in cum if t >= c) + 1, len(metas))
                pct = min(99, int(t / total_dur * 100))
                if pct != _last:
                    _last = pct
                    try: progress(cur, len(metas), out_name, pct)
                    except Exception: pass
        proc.wait(timeout=7200)
    except Exception:
        proc.kill()
    errf.seek(0); err_tail = errf.read()[-400:]; errf.close()
    if cover:
        try: cover.unlink()
        except Exception: pass
    if proc.returncode != 0 or not out_file.exists() or out_file.stat().st_size == 0:
        return {"ok": False, "error": f"ffmpeg merge failed: {err_tail!r}"}
    if progress:
        try: progress(len(metas), len(metas), out_name, 100)
        except Exception: pass

    # Build the CUE — INDEX 01 times are cumulative source durations.
    title  = mix_title or base
    perf   = mix_artist or (metas[0]["artist"] if metas else "")
    lines = [
        f'PERFORMER "{_cue_escape(perf)}"',
        f'TITLE "{_cue_escape(title)}"',
        f'FILE "{out_file.name}" {cue_type}',
    ]
    t = 0.0
    for i, m in enumerate(metas, 1):
        lines.append(f"  TRACK {i:02d} AUDIO")
        lines.append(f'    TITLE "{_cue_escape(m["title"])}"')
        if m["artist"]:
            lines.append(f'    PERFORMER "{_cue_escape(m["artist"])}"')
        lines.append(f"    INDEX 01 {_cue_time(t)}")
        t += m["duration"]
    out_cue.write_text("\n".join(lines) + "\n", encoding="utf-8")

    return {"ok": True, "file": str(out_file), "cue": str(out_cue),
            "tracks": len(paths), "warning": warning,
            "format": "alac" if ext == ".m4a" else ext.lstrip(".")}


def _disc_num(tag: str, path: Path) -> int:
    """Disc number from tag (e.g. '2' or '2/2'), else 'D-TT' filename, else a
    'CD 2'/'Disc 2' parent folder, else 1."""
    if tag:
        m = re.match(r"\s*(\d+)", str(tag))
        if m:
            return int(m.group(1))
    m = re.match(r"^(\d)[-_.]\d{2}\b", path.name)   # 1-01 / 2_05 (disc-track)
    if m:
        return int(m.group(1))
    m = re.search(r"(?:cd|disc|disque)\s*(\d+)", path.parent.name, re.I)
    if m:
        return int(m.group(1))
    return 1


def _trk_num(tag: str, path: Path) -> int:
    if tag:
        m = re.match(r"\s*(\d+)", str(tag))
        if m:
            return int(m.group(1))
    m = re.match(r"^(?:\d[-_.])?0*(\d+)", path.name)
    return int(m.group(1)) if m else 0


def build_mixes(tracks: list[str], out_dir: str, out_name: str,
                fmt: str = "mp3", ffmpeg: str = "ffmpeg",
                mix_title: str = "", mix_artist: str = "", progress=None) -> dict:
    """Multi-disc-aware mix builder. Groups tracks by disc and produces ONE
    continuous file + CUE PER DISC — never merges discs together. A single disc
    keeps `out_name`; multiple discs get a ' (CD N)' suffix on file, cue and
    title. Returns {ok, multi, discs, mixes:[build_mix result, ...]}."""
    ffprobe = _ffprobe_for(ffmpeg)
    paths = [Path(t) for t in tracks if Path(t).is_file()]
    if not paths:
        return {"ok": False, "error": "нет треков"}
    info = []
    for p in paths:
        m = _probe(ffprobe, p)
        info.append((_disc_num(m.get("disc", ""), p), _trk_num(m.get("track", ""), p), p))
    discs = sorted({d for d, _, _ in info})
    multi = len(discs) > 1
    mixes = []
    for di, d in enumerate(discs):
        if _CANCEL.is_set():
            break
        grp = sorted([(t, p) for dd, t, p in info if dd == d], key=lambda x: (x[0], str(x[1])))
        files = [str(p) for _, p in grp]
        nm = f"{out_name} (CD {d})" if multi else out_name
        mt = f"{mix_title} (CD {d})" if (multi and mix_title) else mix_title
        # Per-disc progress carries a 'CD N/total' label when multi-disc.
        _pg = None
        if progress:
            _lbl = f"{nm}" if not multi else f"CD {d}/{len(discs)}"
            _pg = lambda cur, tot, label, pct, _l=_lbl: progress(cur, tot, _l, pct)
        r = build_mix(files, out_dir, nm, fmt, ffmpeg, mt, mix_artist, progress=_pg)
        r["disc"] = d
        mixes.append(r)
    return {"ok": any(m.get("ok") for m in mixes), "multi": multi,
            "discs": len(discs), "mixes": mixes,
            "cancelled": _CANCEL.is_set()}


# ── CUE splitter (inverse of the merger) ────────────────────────────────────────

_CUE_SPLIT_AUDIO = (".flac", ".wav", ".m4a", ".alac", ".ape", ".mp3", ".wv",
                    ".dsf", ".opus", ".ogg", ".aac", ".aiff")


def _parse_cue_time(t: str) -> float:
    """CUE 'MM:SS:FF' (75 frames/sec) → seconds."""
    try:
        mm, ss, ff = t.strip().split(":")
        return int(mm) * 60 + int(ss) + int(ff) / 75.0
    except Exception:
        return 0.0


def _cue_unquote(s: str) -> str:
    s = s.strip()
    return s[1:-1] if len(s) >= 2 and s[0] == '"' and s[-1] == '"' else s


def parse_cue(cue_path: str) -> dict:
    """Parse a CUE sheet → {audio, album, albumartist, tracks:[{num,title,artist,start}]}."""
    text = Path(cue_path).read_text(encoding="utf-8", errors="replace")
    audio = None
    album = albumartist = ""
    tracks: list = []
    cur = None
    for raw in text.splitlines():
        line = raw.strip()
        up = line.upper()
        if up.startswith("FILE "):
            m = re.search(r'FILE\s+"?(.+?)"?\s+\w+\s*$', line)
            if m:
                audio = m.group(1)
        elif up.startswith("TRACK "):
            if cur:
                tracks.append(cur)
            m = re.search(r'TRACK\s+(\d+)', line, re.I)
            cur = {"num": int(m.group(1)) if m else len(tracks) + 1,
                   "title": "", "artist": "", "start": 0.0}
        elif up.startswith("TITLE "):
            val = _cue_unquote(line[6:])
            if cur is None:
                album = val
            else:
                cur["title"] = val
        elif up.startswith("PERFORMER "):
            val = _cue_unquote(line[10:])
            if cur is None:
                albumartist = val
            else:
                cur["artist"] = val
        elif up.startswith("INDEX 01"):
            m = re.search(r'INDEX\s+01\s+(\d+:\d+:\d+)', line, re.I)
            if m and cur is not None:
                cur["start"] = _parse_cue_time(m.group(1))
    if cur:
        tracks.append(cur)
    for t in tracks:
        if not t["artist"]:
            t["artist"] = albumartist
    return {"audio": audio, "album": album, "albumartist": albumartist, "tracks": tracks}


def split_cue(cue_path: str, out_dir: str, fmt: str = "source",
              bitrate: str = "320k", ffmpeg: str = "ffmpeg", progress=None) -> dict:
    """Split one album-image audio file into per-track files using its CUE sheet.

    fmt="source" copies the stream (exact, lossless, fast); any _CONVERT key
    re-encodes. Each output is tagged title/artist/album/album_artist/track."""
    cue = Path(cue_path)
    if not cue.is_file():
        return {"ok": False, "error": "CUE-файл не найден"}
    info = parse_cue(cue_path)
    tracks = info["tracks"]
    if not tracks:
        return {"ok": False, "error": "В CUE нет треков"}
    # Resolve the referenced audio file (next to the cue); fall back to a lone
    # audio file in the same folder if the FILE line is missing/wrong.
    audio = None
    if info["audio"]:
        cand = cue.parent / info["audio"]
        if cand.is_file():
            audio = cand
    if audio is None:
        auds = [f for f in cue.parent.iterdir()
                if f.is_file() and f.suffix.lower() in _CUE_SPLIT_AUDIO]
        if len(auds) == 1:
            audio = auds[0]
    if audio is None or not audio.is_file():
        return {"ok": False, "error": f"Аудиофайл из CUE не найден: {info.get('audio') or '?'}"}

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    if fmt == "source" or fmt not in _CONVERT:
        ext, enc = audio.suffix, ["-c:a", "copy"]
    else:
        e, _cc, enc_t = _CONVERT[fmt]
        ext, enc = e, [a.replace("{br}", bitrate) for a in enc_t]

    total = len(tracks)
    done = fail = 0
    names: list = []
    for i, t in enumerate(tracks):
        if _CANCEL.is_set():
            break
        start = t["start"]
        dur = (tracks[i + 1]["start"] - start) if i + 1 < len(tracks) else None
        num = t["num"] or (i + 1)
        stem = _sanitize(f"{num:02d} - {t['artist']} - {t['title']}".strip(" -")) or f"{num:02d}"
        of = out / (stem + ext)
        # -ss before -i = fast seek; -t (duration) avoids the -to-after-seek gotcha.
        cmd = [ffmpeg, "-y", "-hide_banner", "-ss", f"{start:.3f}"]
        if dur is not None and dur > 0:
            cmd += ["-t", f"{dur:.3f}"]
        cmd += ["-i", str(audio), "-map", "0:a", *enc,
                "-metadata", f"title={t['title']}",
                "-metadata", f"artist={t['artist']}",
                "-metadata", f"album={info['album']}",
                "-metadata", f"album_artist={info['albumartist']}",
                "-metadata", f"track={num}/{total}", str(of)]
        try:
            cp = subprocess.run(cmd, capture_output=True, timeout=1800,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if cp.returncode == 0 and of.exists() and of.stat().st_size > 0:
                done += 1
                names.append(of.name)
            else:
                fail += 1
        except Exception:
            fail += 1
        if progress:
            n = done + fail
            try:
                progress(n, total, t.get("title", ""), int(n / total * 100))
            except Exception:
                pass
    return {"ok": done > 0, "converted": done, "failed": fail,
            "out_dir": str(out), "files": names, "album": info["album"],
            "cancelled": _CANCEL.is_set()}
