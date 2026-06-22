"""
Spectrogram analysis endpoint.

  POST /api/spectrogram          — analyze file by path, returns PNG + metadata
  POST /api/spectrogram/upload   — upload file (multipart), analyze in temp dir
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel

router = APIRouter()

# Max upload size: 200 MB
_MAX_UPLOAD = 200 * 1024 * 1024


def install(app) -> None:
    app.include_router(router)


# ── helpers ───────────────────────────────────────────────────────────────────

def _ffprobe(path: str) -> dict:
    """Run ffprobe and return stream/format info."""
    cmd = [
        "ffprobe", "-v", "quiet", "-print_format", "json",
        "-show_streams", "-show_format", path
    ]
    try:
        out = subprocess.check_output(cmd, timeout=30, stderr=subprocess.DEVNULL)
        return json.loads(out)
    except Exception:
        return {}


def _format_duration(sec: float) -> str:
    sec = int(sec)
    m, s = divmod(sec, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _probe_info(path: str) -> dict:
    """Extract human-readable metadata from ffprobe output."""
    data = _ffprobe(path)
    info: dict = {}

    fmt = data.get("format", {})
    streams = data.get("streams", [])
    audio = next((s for s in streams if s.get("codec_type") == "audio"), {})

    # Container / codec
    fmt_name = fmt.get("format_long_name") or fmt.get("format_name", "")
    codec = audio.get("codec_long_name") or audio.get("codec_name", "")
    info["format"] = fmt_name or Path(path).suffix.lstrip(".").upper()
    info["codec"] = codec

    # Bitrate
    br = int(audio.get("bit_rate") or fmt.get("bit_rate") or 0)
    if br:
        info["bitrate"] = f"{br // 1000} kbps"

    # Sample rate
    sr = audio.get("sample_rate")
    if sr:
        info["sample_rate"] = f"{int(sr) // 1000} kHz"

    # Bit depth
    bd = audio.get("bits_per_raw_sample") or audio.get("bits_per_coded_sample")
    if bd and int(bd) > 0:
        info["bit_depth"] = f"{bd}-bit"

    # Channels
    ch = audio.get("channels")
    if ch:
        ch_name = {1: "Mono", 2: "Stereo", 6: "5.1", 8: "7.1"}.get(int(ch), f"{ch}ch")
        info["channels"] = ch_name

    # Duration
    dur = float(audio.get("duration") or fmt.get("duration") or 0)
    if dur:
        info["duration"] = _format_duration(dur)
        info["duration_sec"] = dur

    return info


def _verdict(info: dict, path: str) -> tuple[str, str]:
    """
    Determine lossless / suspicious / lossy verdict from codec + bit depth.
    Returns (verdict_key, verdict_text).
    """
    codec = (info.get("codec") or "").lower()
    bd = info.get("bit_depth", "")
    ext = Path(path).suffix.lower()

    lossless_codecs = ("flac", "alac", "pcm", "wavpack", "ape", "dsd", "truehd", "mlp")
    lossy_codecs    = ("mp3", "aac", "vorbis", "opus", "ac3", "eac3", "mp2", "speex", "gsm")

    is_lossless_ext = ext in (".flac", ".wav", ".aiff", ".aif", ".alac", ".wv", ".ape")
    is_lossy_ext    = ext in (".mp3", ".ogg", ".aac", ".m4a", ".opus", ".ac3")

    lossless_match = any(lc in codec for lc in lossless_codecs)
    lossy_match    = any(lc in codec for lc in lossy_codecs)

    if lossless_match or (is_lossless_ext and not lossy_match):
        return "lossless", f"✓ Lossless ({info.get('codec','')}) — полный спектр до Nyquist"
    if lossy_match or is_lossy_ext:
        return "lossy", f"✗ Lossy ({info.get('codec','')}) — сжатый формат, срез на спектрограмме норма"
    # M4A could be ALAC or AAC
    if ext == ".m4a":
        if "alac" in codec:
            return "lossless", "✓ M4A/ALAC — lossless"
        if "aac" in codec or not codec:
            return "lossy", "✗ M4A/AAC — lossy"
    return "suspicious", "⚠ Неизвестный кодек — проверь спектрограмму визуально"


def _generate_spectrogram(src_path: str, out_png: str, duration: float = 0) -> None:
    """Use ffmpeg showspectrumpic to generate the spectrogram PNG."""
    # Width scales with duration, max 1400px
    w = min(max(int((duration or 120) * 6), 800), 1400)
    h = 512

    cmd = [
        "ffmpeg", "-y", "-i", src_path,
        "-lavfi", (
            f"showspectrumpic=s={w}x{h}"
            ":mode=combined"
            ":color=plasma"
            ":scale=cbrt"
            ":gain=2"
            ":legend=1"
        ),
        "-frames:v", "1",
        "-update", "1",
        out_png,
    ]
    result = subprocess.run(cmd, timeout=60, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode(errors="replace")[-400:])


def _analyze(path: str) -> dict:
    info = _probe_info(path)
    verdict_key, verdict_text = _verdict(info, path)

    out_png  = path + "_spec.png"
    img_data = ""
    try:
        _generate_spectrogram(path, out_png, info.get("duration_sec", 0))
        img_data = base64.b64encode(Path(out_png).read_bytes()).decode()
    finally:
        try:
            os.unlink(out_png)
        except OSError:
            pass

    return {
        **info,
        "verdict":      verdict_key,
        "verdict_text": verdict_text,
        "image":        img_data,
    }


# ── routes ────────────────────────────────────────────────────────────────────

class PathRequest(BaseModel):
    path: str


@router.post("/api/spectrogram")
async def analyze_by_path(req: PathRequest):
    p = req.path.strip()
    if not p:
        raise HTTPException(400, "path is required")
    if not os.path.isfile(p):
        raise HTTPException(404, f"Файл не найден: {p}")
    if os.path.getsize(p) > _MAX_UPLOAD:
        raise HTTPException(413, "Файл слишком большой (лимит 200 МБ)")
    try:
        return await asyncio.to_thread(_analyze, p)
    except FileNotFoundError:
        raise HTTPException(500, "ffmpeg/ffprobe не найден — установи ffmpeg и добавь в PATH")
    except Exception as e:
        raise HTTPException(500, str(e))


@router.post("/api/spectrogram/upload")
async def analyze_upload(file: UploadFile = File(...)):
    allowed_exts = {".mp3", ".flac", ".m4a", ".aac", ".ogg", ".wav",
                    ".aif", ".aiff", ".wv", ".opus", ".ape", ".wma"}
    ext = Path(file.filename or "").suffix.lower()
    if ext not in allowed_exts:
        raise HTTPException(400, f"Неподдерживаемый формат: {ext}")

    with tempfile.TemporaryDirectory(prefix="ripster_spec_") as tmp:
        dst = os.path.join(tmp, f"upload{ext}")
        content = await file.read()
        if len(content) > _MAX_UPLOAD:
            raise HTTPException(413, "Файл слишком большой (лимит 200 МБ)")
        Path(dst).write_bytes(content)
        try:
            return await asyncio.to_thread(_analyze, dst)
        except FileNotFoundError:
            raise HTTPException(500, "ffmpeg/ffprobe не найден — установи ffmpeg и добавь в PATH")
        except Exception as e:
            raise HTTPException(500, str(e))
