"""
Amazon Music engine via the `amz` CLI (AmineSoukara/amazon-music).

Install:  pip install amazon-music   (gives the `amz` console script)
Auth:     token from https://amz.dezalty.com/login → Settings → Amazon (config
          key `amazon-token`). The CLI saves it, but we pass it every run so a
          fresh machine works without `amz --config`.

The `amz` CLI does the full pipeline itself (resolve → stream URL → Widevine key
retrieval via the dezalty service → mp4decrypt → tagged FLAC), so this engine is
a thin subprocess wrapper exactly like the deemix (deezer) engine — no local
.wvd needed (the service returns the key for your token).

Qualities: Max (24-bit/≤192kHz), Master (24/96), High (16/48 FLAC), plus Dolby
Atmos (EC-3 for Sonos / AC-4).
"""
from __future__ import annotations

import os
import re
import shutil
import sys
from pathlib import Path

from .base import EngineBase, EngineResult
from .registry import register

_QUALITIES = [
    {"id": "Max",        "label": "Ultra HD",     "sub": "24-bit / ≤192 kHz", "badge": "24-BIT",   "color": "#1ad5c0", "bitrate": "≥4600 kbps", "ext": "flac", "req": "unlimited"},
    {"id": "Master",     "label": "Hi-Res",       "sub": "24-bit / ≤96 kHz",  "badge": "HI-RES",   "color": "#3ecfaa", "bitrate": "≥2300 kbps", "ext": "flac", "req": "unlimited"},
    {"id": "High",       "label": "HD (FLAC)",    "sub": "16-bit / ≤48 kHz",  "badge": "LOSSLESS", "color": "#3ecfaa", "bitrate": "≤1411 kbps", "ext": "flac", "req": "unlimited"},
    {"id": "Atmos_EC-3", "label": "Dolby Atmos",  "sub": "EAC3-JOC (Sonos)",  "badge": "ATMOS",    "color": "#c084a0", "bitrate": "spatial",    "ext": "ec3",  "req": "unlimited"},
]
_VALID = {q["id"] for q in _QUALITIES} | {"Normal", "Medium", "Low", "Free", "Atmos_AC-4"}

_RE_ERR   = re.compile(r"\berror\b|\bfailed\b|exception|traceback|unauthor|invalid token|token.*(expired|invalid)", re.I)
_RE_AUTH  = re.compile(r"\btoken\b.*(expired|invalid|missing|required)|unauthor|401|403|login", re.I)
_RE_OK    = re.compile(r"\b(downloaded|completed|success|saved|done)\b", re.I)
_RE_PCT   = re.compile(r"(\d{1,3})\s*%")
_RE_FRAC  = re.compile(r"(\d+)\s*/\s*(\d+)")


def _amz_exe() -> str:
    """Locate the `amz` console script (it's often NOT on PATH after a user pip
    install). Order: PATH → the interpreter's Scripts dir → common per-user
    Scripts dirs. (A config override `amazon-cli-path` is checked in build_cmd.)"""
    found = shutil.which("amz")
    if found:
        return found
    cands = []
    try:
        cands.append(Path(sys.executable).parent / "Scripts" / "amz.exe")
        cands.append(Path(sys.executable).parent / "amz.exe")
    except Exception:
        pass
    base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    for pyv in ("Python314", "Python313", "Python312", "Python311"):
        cands.append(Path(base) / "Python" / pyv / "Scripts" / "amz.exe")
    for c in cands:
        try:
            if c.is_file():
                return str(c)
        except Exception:
            pass
    return "amz"   # last resort — relies on PATH


@register
class AmazonEngine(EngineBase):
    name = "amazon"

    def qualities(self) -> list[dict]:
        return [{**q, "engine": self.name} for q in _QUALITIES]

    def build_cmd(self, url: str, quality: str, config: dict) -> list[str]:
        token    = (config.get("amazon-token") or "").strip()
        out_path = config.get("amazon-save-path") or config.get("save-path", "downloads")
        q        = quality if quality in _VALID else "High"
        amz      = (config.get("amazon-cli-path") or "").strip() or _amz_exe()
        try:
            Path(out_path).mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        cmd = [amz, url, "-q", q, "-t", "auto", "-o", str(out_path), "--overwrite"]
        if token:
            cmd += ["--token", token]
        return cmd

    def classify_line(self, line: str) -> str:
        if _RE_AUTH.search(line) or _RE_ERR.search(line):
            return "error"
        if _RE_OK.search(line):
            return "success"
        return "stdout"

    def parse_progress(self, line: str, current: int, total: int) -> tuple[int, int]:
        m = _RE_PCT.search(line)
        if m:
            return min(100, int(m.group(1))), 100
        m = _RE_FRAC.search(line)
        if m:
            try:
                return int(m.group(1)), int(m.group(2))
            except Exception:
                pass
        return current, total

    def is_finished(self, log_text: str, rc: int = -1) -> EngineResult:
        # Auth problems first — give a clear, actionable message.
        if _RE_AUTH.search(log_text):
            return EngineResult(
                False,
                error="Amazon: токен недействителен/истёк — обнови в Settings → Amazon "
                      "(получи на amz.dezalty.com/login).",
            )
        ok = len(_RE_OK.findall(log_text))
        if rc == 0:
            # The runner's silent-partial guard recounts actual files on disk and
            # marks a shortfall; here we just declare success when the process
            # exited cleanly (and didn't hit an auth/error line above).
            return EngineResult(True, tracks_ok=ok)
        # Non-zero exit → surface the last error line.
        last_err = ""
        for line in reversed(log_text.splitlines()):
            if _RE_ERR.search(line):
                last_err = line.strip()[:200]
                break
        return EngineResult(False, error=last_err or f"Amazon: завершился с кодом {rc}")
