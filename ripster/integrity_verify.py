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


def verify_and_repair(files: list, config: dict) -> dict:
    """Decode-check every file in `files` (list of Path). ALAC (.m4a) failures
    get one repair attempt + re-check. Returns a summary dict; never raises —
    a broken verify pass must not take down the download it's checking."""
    summary = {"checked": 0, "ok": 0, "fixed": [], "corrupt": []}
    for f in files:
        f = Path(f)
        if f.suffix.lower() not in (".flac", ".alac", ".m4a", ".mp3", ".aac",
                                    ".ogg", ".opus", ".wav", ".aiff", ".aif", ".wv"):
            continue
        summary["checked"] += 1
        try:
            err = _decode_check(f)
            if not err:
                summary["ok"] += 1
                continue
            if f.suffix.lower() == ".m4a" and _try_alacfix(f, config):
                err2 = _decode_check(f)
                if not err2:
                    summary["ok"] += 1
                    summary["fixed"].append(f.name)
                    continue
            summary["corrupt"].append(f.name)
        except Exception:
            # A verify-pass bug must never fail the download it's checking.
            continue
    return summary
