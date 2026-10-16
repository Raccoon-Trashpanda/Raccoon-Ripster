"""ReplayGain measurement and tag writing (087 п.1).

Measures loudness via ffmpeg's ``ebur128`` filter and writes
``REPLAYGAIN_TRACK_GAIN`` / ``REPLAYGAIN_TRACK_PEAK`` tags to FLAC, MP3, M4A, OGG.

Config: ``write-replaygain`` (bool, default off).
Safe: on any failure the original file is untouched (temp file + replace).
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors="replace")
    except Exception:
        pass

_RE_GAIN = re.compile(r"Target gain:\s+([-\d.]+)\s+LUFS")
_RE_PEAK = re.compile(r"Peak:\s+([-\d.]+)\s+dBFS")
_RE_INTEGRATED = re.compile(r"I:\s+([-\d.]+)\s+LUFS")
_RG_TARGET_LUFS = -18.0  # ReplayGain reference level


def measure_loudness(path: Path) -> tuple[float, float] | None:
    """Run ffmpeg ebur128 filter on *path* and return (gain_db, peak_db).

    gain_db: how much to amplify to reach -18 LUFS (positive = too quiet).
    peak_db: true peak in dBFS (0 = clipping).
    Returns None on failure (ffmpeg missing, file unreadable, etc).
    """
    path = Path(path)
    if not path.is_file():
        return None
    try:
        result = subprocess.run(
            ["ffmpeg", "-nostdin", "-i", str(path),
             "-af", "ebur128=peak=true", "-f", "null", "-"],
            capture_output=True, text=True, timeout=120,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None

    stderr = result.stderr or ""
    lines = stderr.splitlines()

    summary_block = ""
    in_summary = False
    for line in lines:
        if "Summary:" in line:
            in_summary = True
            summary_block = line + "\n"
            continue
        if in_summary:
            summary_block += line + "\n"

    gain = None
    peak = None

    m_target = _RE_GAIN.search(summary_block)
    if m_target:
        try:
            target_gain = float(m_target.group(1))
            gain = target_gain
        except ValueError:
            pass

    if gain is None:
        m_int = _RE_INTEGRATED.search(summary_block)
        if m_int:
            try:
                integrated = float(m_int.group(1))
                gain = _RG_TARGET_LUFS - integrated
            except ValueError:
                pass

    m = _RE_PEAK.search(summary_block)
    if m:
        try:
            peak = float(m.group(1))
        except ValueError:
            pass

    if gain is None:
        for line in reversed(lines):
            m_int = _RE_INTEGRATED.search(line)
            if m_int:
                try:
                    integrated = float(m_int.group(1))
                    gain = _RG_TARGET_LUFS - integrated
                except ValueError:
                    pass
                break

    if gain is not None and peak is not None:
        return (gain, peak)
    return None


def write_replaygain_tags(path: Path, gain_db: float, peak_db: float) -> bool:
    """Write REPLAYGAIN_TRACK_GAIN and REPLAYGAIN_TRACK_PEAK to *path*.

    Format-specific tag names:
    - FLAC/OGG/Opus: Vorbis comment REPLAYGAIN_TRACK_GAIN, REPLAYGAIN_TRACK_PEAK
    - MP3: TXXX frames
    - M4A/ALAC: freeform atoms ----:com.apple.iTunes:REPLAYGAIN_*

    Uses atomic write (temp file + replace) to avoid corrupting the original.
    Returns True on success, False on failure.
    """
    path = Path(path)
    if not path.is_file():
        return False

    gain_str = f"{gain_db:+.2f} dB"
    peak_linear = 10 ** (peak_db / 20)
    peak_str = f"{peak_linear:.6f}"

    suffix = path.suffix.lower()
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=suffix, dir=path.parent)
    os.close(tmp_fd)
    tmp = Path(tmp_path)

    try:
        shutil.copy2(path, tmp)

        if suffix == ".flac":
            _rg_flac(tmp, gain_str, peak_str)
        elif suffix == ".mp3":
            _rg_mp3(tmp, gain_str, peak_str)
        elif suffix in (".m4a", ".mp4", ".alac", ".aac"):
            _rg_mp4(tmp, gain_str, peak_str)
        elif suffix in (".ogg", ".opus"):
            _rg_ogg(tmp, gain_str, peak_str)
        else:
            tmp.unlink(missing_ok=True)
            return False

        os.replace(tmp, path)
        return True
    except Exception:
        tmp.unlink(missing_ok=True)
        return False


def _rg_flac(path: Path, gain_str: str, peak_str: str) -> None:
    from mutagen.flac import FLAC
    f = FLAC(path)
    if f.tags is None:
        f.add_tags()
    f.tags["REPLAYGAIN_TRACK_GAIN"] = [gain_str]
    f.tags["REPLAYGAIN_TRACK_PEAK"] = [peak_str]
    f.save()


def _rg_mp3(path: Path, gain_str: str, peak_str: str) -> None:
    from mutagen.id3 import ID3, ID3NoHeaderError, TXXX
    try:
        tags = ID3(path)
    except ID3NoHeaderError:
        tags = ID3()
    tags.add(TXXX(encoding=3, desc="REPLAYGAIN_TRACK_GAIN", text=[gain_str]))
    tags.add(TXXX(encoding=3, desc="REPLAYGAIN_TRACK_PEAK", text=[peak_str]))
    tags.save(path)


def _rg_mp4(path: Path, gain_str: str, peak_str: str) -> None:
    from mutagen.mp4 import MP4, MP4FreeForm
    f = MP4(path)
    if f.tags is None:
        f.add_tags()
    f.tags["----:com.apple.iTunes:REPLAYGAIN_TRACK_GAIN"] = [
        MP4FreeForm(gain_str.encode("utf-8"))]
    f.tags["----:com.apple.iTunes:REPLAYGAIN_TRACK_PEAK"] = [
        MP4FreeForm(peak_str.encode("utf-8"))]
    f.save()


def _rg_ogg(path: Path, gain_str: str, peak_str: str) -> None:
    if path.suffix.lower() == ".opus":
        from mutagen.oggopus import OggOpus as Cls
    else:
        from mutagen.oggvorbis import OggVorbis as Cls
    f = Cls(path)
    if f.tags is None:
        f.add_tags()
    f.tags["REPLAYGAIN_TRACK_GAIN"] = [gain_str]
    f.tags["REPLAYGAIN_TRACK_PEAK"] = [peak_str]
    f.save()


def process_file(path: Path) -> bool:
    """Measure loudness and write ReplayGain tags. Returns True on success."""
    result = measure_loudness(path)
    if result is None:
        return False
    gain_db, peak_db = result
    return write_replaygain_tags(path, gain_db, peak_db)
