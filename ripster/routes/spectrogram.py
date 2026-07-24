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

# Windows: don't flash a console window for the ffprobe/ffmpeg spawns when the
# server runs windowless (frozen build). 0 on non-Windows.
_CNW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
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
        out = subprocess.check_output(cmd, timeout=30, stderr=subprocess.DEVNULL,
                                      creationflags=_CNW)
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
        info["sample_rate_hz"] = int(sr)

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


def _decode_pcm_mono(path: str, sr: int, total_dur: float,
                     duration_cap: float = 240.0):
    """Decode a representative slice to mono float32 PCM at the file's own
    sample rate (so the spectrum reaches all the way to true Nyquist). DJ
    mixes run to 2h — analyzing the whole thing is both slow and unnecessary
    for a spectral fingerprint, so take up to duration_cap seconds centered in
    the track (skips intro/outro fades, which could bias the "silence above
    cutoff" reading toward false quiet)."""
    import numpy as np
    start = max(0.0, (total_dur - duration_cap) / 2) if total_dur > duration_cap else 0.0
    take = min(duration_cap, total_dur) if total_dur else duration_cap
    cmd = ["ffmpeg", "-v", "quiet", "-ss", f"{start:.2f}", "-i", path,
           "-t", f"{take:.2f}", "-f", "f32le", "-ac", "1", "-ar", str(sr), "-"]
    raw = subprocess.check_output(cmd, timeout=60, stderr=subprocess.DEVNULL,
                                  creationflags=_CNW)
    return np.frombuffer(raw, dtype="<f4")


def _spectral_fingerprint(path: str, sr: int, total_dur: float) -> dict:
    """Averaged power spectrum (Welch-style: windowed FFT, 50% overlap, Hann
    window, averaged in the power domain) → cutoff frequency, brickwall
    steepness, overall rolloff slope, and high-frequency spectral variance.

    This is what actually distinguishes genuine lossless/high-bitrate audio
    from a transcode wearing a lossless container: the codec name in the
    file's own metadata is a CLAIM, not evidence — a lossy source re-encoded
    to FLAC still reports "flac", but its frequency content still shows the
    lossy encoder's low-pass cutoff. Analyzing the actual samples is the only
    way to catch that. Returns {} if ffmpeg/numpy fail (caller falls back to
    the plain codec-name verdict)."""
    import numpy as np
    if sr <= 0:
        return {}
    samples = _decode_pcm_mono(path, sr, total_dur)
    win_size = 16384
    if samples.size < win_size:
        return {}
    hop = win_size // 2
    window = np.hanning(win_size)
    n_frames = max(1, (samples.size - win_size) // hop)

    acc = np.zeros(win_size // 2 + 1, dtype=np.float64)
    count = 0
    for i in range(n_frames):
        seg = samples[i * hop: i * hop + win_size]
        if seg.size < win_size:
            break
        spec = np.fft.rfft(seg * window)
        acc += np.abs(spec) ** 2
        count += 1
    if count == 0:
        return {}
    power = acc / count
    freqs = np.fft.rfftfreq(win_size, d=1.0 / sr)

    # dB relative to the loudest bin (0 dB = peak content in this slice).
    peak = power.max() or 1e-12
    db = 10 * np.log10(np.maximum(power / peak, 1e-12))

    # Smooth to suppress single-bin noise before hunting for the cutoff. Pad
    # with EDGE values (not zeros) before convolving — plain `mode="same"`
    # implicitly zero-pads past the boundary, and since 0 dB is the LOUDEST
    # point after peak-normalization (not silence), that zero-padding drags
    # the smoothed level right at Nyquist artificially UP, making the cutoff
    # detector below think real content extends all the way to Nyquist even
    # when the true spectrum is silent there. Edge-padding avoids that bias.
    pad = 30
    kernel = np.ones(2 * pad + 1) / (2 * pad + 1)
    db_smooth = np.convolve(np.pad(db, pad, mode="edge"), kernel, mode="valid")

    nyquist = sr / 2
    # Noise floor = median level in the top slice of the spectrum — the
    # baseline "nothing here" level above any real cutoff.
    top_slice = db_smooth[freqs > nyquist * 0.95]
    noise_floor = float(np.median(top_slice)) if top_slice.size else float(db_smooth[-1])

    # Cutoff = highest frequency, scanning down from Nyquist, where the level
    # first rises 10 dB above the noise floor (real content resumes there).
    above = np.where(db_smooth > noise_floor + 10.0)[0]
    cutoff_hz = float(freqs[above[-1]]) if above.size else float(freqs[-1])

    # Level exactly at 20 kHz (interpolated) — the classic "is there anything
    # up here" checkpoint, independent of where the detected cutoff lands.
    level_20k = float(np.interp(20000, freqs, db_smooth)) if nyquist > 20000 else noise_floor

    # Brickwall steepness: dB/kHz drop in the 1 kHz band right BEFORE the
    # cutoff. An artificial low-pass filter (every lossy encoder has one) cuts
    # off sharply; genuine full-bandwidth material rolls off gradually even
    # near its own top end.
    band_lo = max(float(freqs[0]), cutoff_hz - 1000)
    lo_db = float(np.interp(band_lo, freqs, db_smooth))
    hi_db = float(np.interp(cutoff_hz, freqs, db_smooth))
    brickwall = (hi_db - lo_db) / max((cutoff_hz - band_lo) / 1000, 0.01)

    # Overall rolloff slope: linear fit of dB vs kHz from 1 kHz up to just
    # below the cutoff (excludes the brickwall region itself and the noisy
    # low end) — the underlying material's general "how fast energy falls
    # off with frequency" trend.
    fit_mask = (freqs >= 1000) & (freqs <= max(1001.0, cutoff_hz - 1000))
    if fit_mask.sum() >= 2:
        slope = float(np.polyfit(freqs[fit_mask] / 1000, db_smooth[fit_mask], 1)[0])
    else:
        slope = 0.0

    # Spectral variance above the cutoff: real dither/noise fluctuates even at
    # near-silent levels; a hard-filtered lossy source is closer to dead flat.
    hf_mask = freqs > cutoff_hz
    variance = float(np.std(db[hf_mask])) if hf_mask.sum() >= 4 else 0.0

    return {
        "cutoff_hz":            round(cutoff_hz, 1),
        "level_20k_db":         round(level_20k, 1),
        "brickwall_db_per_khz": round(brickwall, 1),
        "slope_db_per_khz":     round(slope, 2),
        "spectral_variance":    round(variance, 2),
        "nyquist_hz":           round(nyquist, 1),
    }


# Verdict text in ru/en — same coverage tier as the bot's other `tb.*` i18n
# keys (this project's established pattern: ru+en only, unsupported langs
# fall back to en). Callers: web Coder tab and the bot's /spek command, both
# via the shared analysis endpoint — neither should ever see hardcoded Russian.
_VERDICT_I18N = {
    "ru": {
        "lossless_basic":     "✓ Lossless ({codec}) — полный спектр до Nyquist",
        "lossy_basic":        "✗ Lossy ({codec}) — сжатый формат, срез на спектрограмме норма",
        "m4a_alac":           "✓ M4A/ALAC — lossless",
        "m4a_aac":            "✗ M4A/AAC — lossy",
        "unknown":            "⚠ Неизвестный кодек — проверь спектрограмму визуально",
        "fake_lossless":      "⚠ Контейнер заявляет lossless ({codec}), но спектр резко обрывается "
                              "на {cutoff:.1f} kHz ({ratio:.0f}% от Найквиста), brickwall {brick:.0f} "
                              "dB/kHz — похоже на перекодировку из lossy-источника",
        "confirmed_lossless": "✓ Lossless ({codec}), подтверждено спектром — контент до {cutoff:.1f} "
                              "kHz, естественный спад",
        "confirmed_lossy":    "✗ Lossy ({codec}) — срез на {cutoff:.1f} kHz согласуется с заявленным кодеком",
    },
    "en": {
        "lossless_basic":     "✓ Lossless ({codec}) — full spectrum up to Nyquist",
        "lossy_basic":        "✗ Lossy ({codec}) — compressed format, spectrogram cutoff is normal",
        "m4a_alac":           "✓ M4A/ALAC — lossless",
        "m4a_aac":            "✗ M4A/AAC — lossy",
        "unknown":            "⚠ Unknown codec — check the spectrogram visually",
        "fake_lossless":      "⚠ Container claims lossless ({codec}), but the spectrum cuts off sharply "
                              "at {cutoff:.1f} kHz ({ratio:.0f}% of Nyquist), brickwall {brick:.0f} "
                              "dB/kHz — looks like a transcode from a lossy source",
        "confirmed_lossless": "✓ Lossless ({codec}), confirmed by spectrum — content up to {cutoff:.1f} "
                              "kHz, natural rolloff",
        "confirmed_lossy":    "✗ Lossy ({codec}) — cutoff at {cutoff:.1f} kHz matches the claimed codec",
    },
}


def _vt(lang: str, key: str, **kw) -> str:
    table = _VERDICT_I18N.get(lang) or _VERDICT_I18N["en"]
    tmpl = table.get(key) or _VERDICT_I18N["en"][key]
    return tmpl.format(**kw)


def _verdict(info: dict, path: str, lang: str = "ru") -> tuple[str, str]:
    """
    Determine lossless / suspicious / lossy verdict from codec + bit depth.
    Returns (verdict_key, verdict_text).
    """
    codec = (info.get("codec") or "").lower()
    ext = Path(path).suffix.lower()

    lossless_codecs = ("flac", "alac", "pcm", "wavpack", "ape", "dsd", "truehd", "mlp")
    lossy_codecs    = ("mp3", "aac", "vorbis", "opus", "ac3", "eac3", "mp2", "speex", "gsm")

    is_lossless_ext = ext in (".flac", ".wav", ".aiff", ".aif", ".alac", ".wv", ".ape")
    is_lossy_ext    = ext in (".mp3", ".ogg", ".aac", ".m4a", ".opus", ".ac3")

    lossless_match = any(lc in codec for lc in lossless_codecs)
    lossy_match    = any(lc in codec for lc in lossy_codecs)

    codec_disp = info.get("codec", "")
    if lossless_match or (is_lossless_ext and not lossy_match):
        return "lossless", _vt(lang, "lossless_basic", codec=codec_disp)
    if lossy_match or is_lossy_ext:
        return "lossy", _vt(lang, "lossy_basic", codec=codec_disp)
    # M4A could be ALAC or AAC
    if ext == ".m4a":
        if "alac" in codec:
            return "lossless", _vt(lang, "m4a_alac")
        if "aac" in codec or not codec:
            return "lossy", _vt(lang, "m4a_aac")
    return "suspicious", _vt(lang, "unknown")


def _verdict_with_fingerprint(info: dict, path: str, fp: dict, lang: str = "ru") -> tuple[str, str]:
    """Combine the container's own codec claim with the ACTUAL spectral
    fingerprint. A lossy source transcoded into a lossless container still
    reports "flac"/"alac" — this is what actually catches that: the claim
    says lossless, but the real audio content shows a lossy encoder's
    brickwall cutoff well below Nyquist. Falls back to the plain codec-name
    verdict if the FFT analysis didn't run (fp empty — ffmpeg/numpy failure)."""
    codec_verdict, codec_text = _verdict(info, path, lang)
    if not fp:
        return codec_verdict, codec_text

    nyquist = fp.get("nyquist_hz") or 1.0
    cutoff  = fp.get("cutoff_hz", 0.0)
    brick   = fp.get("brickwall_db_per_khz", 0.0)
    ratio   = cutoff / nyquist
    codec_disp = info.get("codec", "")

    # Brickwall steepness alone is the reliable signal — every lossy encoder's
    # low-pass filter cuts off sharply (measured -40..-55 dB/kHz on real MP3/
    # AAC test files), while genuine masters roll off gradually (-1..-5 dB/kHz)
    # EVEN when they're naturally quiet up near Nyquist already (most program
    # material has little energy above ~19-20 kHz on its own, with no encoding
    # involved at all). So cutoff/Nyquist RATIO on its own is not trustworthy —
    # a real 44.1kHz FLAC test file measured only 90% of Nyquist and a gentle
    # -3.8 dB/kHz slope; gating on ratio alone flagged that false positive.
    is_brickwalled = brick <= -20.0

    if codec_verdict == "lossless" and is_brickwalled:
        return "suspicious", _vt(lang, "fake_lossless", codec=codec_disp,
                                 cutoff=cutoff / 1000, ratio=ratio * 100, brick=brick)
    if codec_verdict == "lossless":
        return "lossless", _vt(lang, "confirmed_lossless", codec=codec_disp, cutoff=cutoff / 1000)
    if codec_verdict == "lossy":
        return "lossy", _vt(lang, "confirmed_lossy", codec=codec_disp, cutoff=cutoff / 1000)
    return codec_verdict, codec_text


# Ripster's own dark palette (mirrors :root in static/css/main.css) — the
# spectrogram frame is drawn with these instead of ffmpeg's stock legend, so
# the output looks like it belongs to the app instead of to "libavfilter".
_SPEC_BG     = (17, 19, 24)      # --bg
_SPEC_BORDER = (58, 58, 88)      # --border
_SPEC_TEXT   = (232, 232, 248)   # --text
_SPEC_MUTED  = (144, 144, 168)   # --muted
_SPEC_ACCENT = (192, 132, 160)   # --red / --accent
_SPEC_GRID   = (58, 58, 88, 90)  # --border at low alpha, for tick guide-lines

_SPEC_FONT_PATH  = "C:/Windows/Fonts/segoeui.ttf"
_SPEC_FONT_BPATH = "C:/Windows/Fonts/segoeuib.ttf"


def _nice_ticks(max_val: float, target_count: int = 6) -> list:
    """Round-number tick positions from 0 to max_val (~target_count of them) —
    same idea as a charting library's axis generator, kept dependency-free."""
    import math
    if max_val <= 0:
        return [0]
    raw_step = max_val / target_count
    magnitude = 10 ** math.floor(math.log10(raw_step))
    residual = raw_step / magnitude
    step = (10 if residual > 5 else 5 if residual > 2 else 2 if residual > 1 else 1) * magnitude
    ticks, v = [], 0.0
    while v <= max_val + step * 0.001:
        ticks.append(v)
        v += step
    return ticks


def _spec_font(size: int, bold: bool = False):
    from PIL import ImageFont
    try:
        return ImageFont.truetype(_SPEC_FONT_BPATH if bold else _SPEC_FONT_PATH, size)
    except OSError:
        return ImageFont.load_default()


def _generate_spectrogram(src_path: str, out_png: str, duration: float = 0,
                          sample_rate_hz: int = 0) -> None:
    """Render the raw spectrum via ffmpeg (no built-in legend/branding), then
    draw Ripster's own axis frame around it with PIL. ffmpeg's legend=1 prints
    "CREATED BY LIBAVFILTER" into the image itself — third-party attribution
    baked into OUR export — so we take the bare pixels (legend=0, exactly
    W x H, no letterboxing) and own the chrome around them instead."""
    from PIL import Image, ImageDraw

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
            ":legend=0"
        ),
        "-frames:v", "1",
        "-update", "1",
        out_png,
    ]
    result = subprocess.run(cmd, timeout=60, capture_output=True, creationflags=_CNW)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode(errors="replace")[-400:])

    nyquist = (sample_rate_hz / 2) if sample_rate_hz else 0
    pad_l, pad_t, pad_r, pad_b = 64, 20, 16, 36

    spectrum = Image.open(out_png).convert("RGB")
    canvas_w, canvas_h = pad_l + w + pad_r, pad_t + h + pad_b
    canvas = Image.new("RGB", (canvas_w, canvas_h), _SPEC_BG)
    canvas.paste(spectrum, (pad_l, pad_t))

    grid = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
    gdraw = ImageDraw.Draw(grid)
    draw = ImageDraw.Draw(canvas)
    font = _spec_font(12)
    font_wm = _spec_font(11, bold=True)

    # Frequency axis (left) — 0 Hz at bottom, Nyquist at top, linear.
    if nyquist:
        for hz in _nice_ticks(nyquist, target_count=7):
            y = pad_t + h - int((hz / nyquist) * h)
            y = max(pad_t, min(pad_t + h, y))
            gdraw.line([(pad_l, y), (pad_l + w, y)], fill=_SPEC_GRID, width=1)
            label = "0" if hz == 0 else f"{hz/1000:g}k"
            tw = draw.textlength(label, font=font)
            draw.text((pad_l - tw - 8, y - 6), label, fill=_SPEC_MUTED, font=font)

    # Time axis (bottom) — 0s at left, duration at right, linear.
    if duration:
        for sec in _nice_ticks(duration, target_count=7):
            x = pad_l + int((sec / duration) * w)
            x = max(pad_l, min(pad_l + w, x))
            gdraw.line([(x, pad_t), (x, pad_t + h)], fill=_SPEC_GRID, width=1)
            label = f"{int(sec)//60}:{int(sec)%60:02d}" if sec >= 60 else f"{int(sec)}s"
            draw.text((x + 4, pad_t + h + 6), label, fill=_SPEC_MUTED, font=font)

    canvas = Image.alpha_composite(canvas.convert("RGBA"), grid).convert("RGB")
    draw = ImageDraw.Draw(canvas)

    # Frame + Ripster wordmark (replaces ffmpeg's "CREATED BY LIBAVFILTER").
    draw.rectangle([pad_l, pad_t, pad_l + w, pad_t + h], outline=_SPEC_BORDER, width=1)
    wm = "R I P S T E R"
    tw = draw.textlength(wm, font=font_wm)
    draw.text((pad_l + w - tw, pad_t + h + 8), wm, fill=_SPEC_ACCENT, font=font_wm)

    canvas.save(out_png)


def _analyze(path: str, lang: str = "ru") -> dict:
    info = _probe_info(path)

    fp: dict = {}
    try:
        fp = _spectral_fingerprint(path, info.get("sample_rate_hz", 0),
                                   info.get("duration_sec", 0))
    except Exception as e:
        # ffmpeg PCM decode or numpy FFT failed — fall back to the plain
        # codec-name verdict rather than losing the whole analysis.
        print(f"[spectrogram] fingerprint failed: {e}", flush=True)
        fp = {}
    verdict_key, verdict_text = _verdict_with_fingerprint(info, path, fp, lang)

    out_png  = path + "_spec.png"
    img_data = ""
    try:
        _generate_spectrogram(path, out_png, info.get("duration_sec", 0),
                              info.get("sample_rate_hz", 0))
        img_data = base64.b64encode(Path(out_png).read_bytes()).decode()
    finally:
        try:
            os.unlink(out_png)
        except OSError:
            pass

    return {
        **info,
        **({"fingerprint": fp} if fp else {}),
        "verdict":      verdict_key,
        "verdict_text": verdict_text,
        "image":        img_data,
    }


# ── routes ────────────────────────────────────────────────────────────────────

class PathRequest(BaseModel):
    path: str
    lang: str = "ru"


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
        return await asyncio.to_thread(_analyze, p, req.lang)
    except FileNotFoundError:
        raise HTTPException(500, "ffmpeg/ffprobe не найден — установи ffmpeg и добавь в PATH")
    except Exception as e:
        raise HTTPException(500, str(e))


@router.post("/api/spectrogram/upload")
async def analyze_upload(file: UploadFile = File(...), lang: str = Form("ru")):
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
            return await asyncio.to_thread(_analyze, dst, lang)
        except FileNotFoundError:
            raise HTTPException(500, "ffmpeg/ffprobe не найден — установи ffmpeg и добавь в PATH")
        except Exception as e:
            raise HTTPException(500, str(e))
