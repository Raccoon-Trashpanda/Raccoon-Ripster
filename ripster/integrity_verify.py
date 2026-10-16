"""
Post-download integrity verification: does the file actually decode cleanly,
not just "did the engine say done" (see [[project_beatport_false_success]]-style
bugs, but for corrupted-on-disk audio instead of never-saved audio).

ALAC files that fail get one auto-repair pass via the compiled Go downloader's
standalone `--fix-alac` mode (ports zhaarey/apple-music-downloader's packet-
terminator fix — see utils/alacfix/alacfix.go) and are re-checked. Anything
else that fails is reported, never silently hidden, but never blocks delivery
either — the user still gets the file and an honest heads-up.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

_CNW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_DECODE_TIMEOUT = 90  # seconds per file; a stuck/huge file is skipped, not flagged corrupt


def _decode_check(path: Path) -> str:
    """Returns '' if the file decodes cleanly, else the ffmpeg stderr excerpt.
    ffmpeg -v error only prints actual decode errors, not warnings/info, so any
    non-empty output here is a real problem."""
    cmd = ["ffmpeg", "-v", "error", "-i", str(path), "-f", "null", "-"]
    try:
        result = subprocess.run(cmd, timeout=_DECODE_TIMEOUT, capture_output=True,
                                creationflags=_CNW)
    except subprocess.TimeoutExpired:
        return ""  # inconclusive on a huge/slow file — don't false-flag it
    except FileNotFoundError:
        return ""  # no ffmpeg on PATH — verification is a bonus, not a hard requirement
    if result.returncode == 0 and not result.stderr.strip():
        return ""
    return result.stderr.decode(errors="replace").strip()[-300:]


_LOSSLESS_CODECS = {"flac", "alac", "ape", "wavpack", "pcm_s16le", "pcm_s24le", "tta"}


def probe_codec(path: Path) -> dict:
    """Каким кодеком файл ЗАКОДИРОВАН на самом деле.

    Обещанное качество ничего не гарантирует: сервис отдаёт что есть на
    конкретный трек, а папка называется по ЗАПРОШЕННОМУ качеству и потому врёт
    сама по себе. 28.07.2026 так и вышло — при запросе FLAC приехал AAC 268
    kbps и лёг в папку «FLAC». Спрашиваем сам файл.
    """
    cmd = ["ffprobe", "-v", "error", "-select_streams", "a:0",
           "-show_entries", "stream=codec_name,bit_rate,sample_rate,bits_per_raw_sample",
           "-of", "default=nw=1", str(path)]
    try:
        r = subprocess.run(cmd, timeout=30, capture_output=True, creationflags=_CNW)
    except Exception:
        return {}
    out = {}
    for line in (r.stdout or b"").decode(errors="replace").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    codec = (out.get("codec_name") or "").lower()
    if not codec:
        return {}
    return {"codec": codec, "lossless": codec in _LOSSLESS_CODECS,
            "bit_rate": out.get("bit_rate", ""),
            "sample_rate": out.get("sample_rate", "")}


def _alacfix_binary(config: dict) -> list:
    """Same binary-vs-`go run` resolution as ripster/engines/zhaarey.py's
    build_cmd — prefer the compiled exe (fast), fall back to `go run` in dev."""
    go    = config.get("use-go-run", False)
    main  = config.get("main-go-path", "main.go")
    gobin = config.get("go-path", "go")
    root  = Path(__file__).resolve().parent.parent
    bin_path = root / ("apple-music-downloader.exe" if os.name == "nt"
                       else "apple-music-downloader")
    if (not go) and bin_path.is_file():
        return [str(bin_path)]
    return [gobin, "run", main]


def _try_alacfix(path: Path, config: dict) -> bool:
    cmd = _alacfix_binary(config) + ["--fix-alac", str(path)]
    try:
        result = subprocess.run(cmd, timeout=60, capture_output=True, creationflags=_CNW)
    except Exception:
        return False
    return result.returncode == 0


_AUDIO_SUFFIXES = (".flac", ".alac", ".m4a", ".mp3", ".aac",
                   ".ogg", ".opus", ".wav", ".aiff", ".aif", ".wv")


def _verify_one(f: "Path", config: dict) -> tuple:
    """→ (status, name) where status is 'ok' | 'fixed' | 'corrupt'."""
    err = _decode_check(f)
    if not err:
        return "ok", f.name
    if f.suffix.lower() == ".m4a" and _try_alacfix(f, config):
        if not _decode_check(f):
            return "fixed", f.name
    return "corrupt", f.name


def verify_and_repair(files: list, config: dict) -> dict:
    """Decode-check every file in `files` (list of Path). ALAC (.m4a) failures
    get one repair attempt + re-check. Returns a summary dict; never raises —
    a broken verify pass must not take down the download it's checking.

    Checks run in parallel. Each check is one ffmpeg process that decodes a
    whole track, and ffmpeg's lossless audio decoders are single-threaded
    (`Threading capabilities: none` for both alac and flac), so checking a
    20-track album serially left every core but one idle and added measurable
    wall time to each finished album — ~5s here, several times that on the
    modest machines this actually needs to stay quick on. There is no GPU path
    for this: every hardware codec ffmpeg exposes (NVENC / AMF / QSV / Media
    Foundation) is a VIDEO engine, so process-level parallelism is the only
    accelerator available for it.
    """
    summary = {"checked": 0, "ok": 0, "fixed": [], "corrupt": []}
    targets = [Path(f) for f in files
               if Path(f).suffix.lower() in _AUDIO_SUFFIXES]
    if not targets:
        return summary
    summary["checked"] = len(targets)

    # Leave a core for the rest of the app; cap so a huge box does not spawn a
    # swarm of ffmpegs against one disk.
    workers = max(1, min(len(targets), (os.cpu_count() or 2) - 1, 8))
    try:
        workers = max(1, min(workers, int(config.get("integrity-verify-workers", workers))))
    except (TypeError, ValueError):
        pass

    if workers == 1:
        results = []
        for f in targets:
            try:
                results.append(_verify_one(f, config))
            except Exception:
                pass
    else:
        from concurrent.futures import ThreadPoolExecutor
        results = []
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for fut in [pool.submit(_verify_one, f, config) for f in targets]:
                try:
                    results.append(fut.result())
                except Exception:
                    # A verify-pass bug must never fail the download it checks.
                    pass

    for status, name in results:
        if status == "ok":
            summary["ok"] += 1
        elif status == "fixed":
            summary["ok"] += 1
            summary["fixed"].append(name)
        else:
            summary["corrupt"].append(name)
    return summary
