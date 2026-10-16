"""Движок «Премиум Ripster»: качает через сервер владельца по ключу поддержки (ripster/flex_download.py).

Подключается через service_layer.engine_for_svc, когда режим источника — «Премиум» (или «Оба»,
а своих токенов сервиса нет). Сам процесс — `python -m ripster.flex_cli`, как и у остальных движков.
"""
from __future__ import annotations

import re
from pathlib import Path

from .base import EngineBase, EngineResult
from .registry import register
from ripster.py_runtime import app_python

_RE_PROGRESS = re.compile(r"^FLEX_PROGRESS (\d+)")
_RE_SAVED = re.compile(r"^FLEX_SAVED (.+)$", re.M)
_RE_ERROR = re.compile(r"^FLEX_ERROR (\S+) (.*)$", re.M)

_QUALITIES = [
    {"id": "flac", "label": "FLAC", "sub": "Premium (server link)", "badge": "LOSSLESS", "color": "#3ecfaa",
     "bitrate": "1411 kbps", "ext": "flac", "req": "plus"},
    {"id": "mp3_320", "label": "320", "sub": "Premium (server link)", "badge": "LOSSY", "color": "#EF9F27",
     "bitrate": "320 kbps", "ext": "mp3", "req": "plus"},
]


@register
class FlexEngine(EngineBase):
    name = "flex"

    def qualities(self) -> list[dict]:
        return [{**q, "engine": self.name} for q in _QUALITIES]

    def build_cmd(self, url: str, quality: str, config: dict) -> list[str]:
        out = config.get("save-path") or "downloads"
        try:
            Path(out).mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        return [app_python(), "-m", "ripster.flex_cli", "--url", url, "--quality", quality or "lossless",
                "--out", str(out)]

    def working_dir(self) -> str | None:
        return str(Path(__file__).resolve().parent.parent.parent)

    def classify_line(self, line: str) -> str:
        if line.startswith("FLEX_ERROR"):
            return "error"
        if line.startswith("FLEX_SAVED"):
            return "success"
        return "stdout"

    def parse_progress(self, line: str, current: int, total: int) -> tuple[int, int]:
        m = _RE_PROGRESS.match(line)
        if m:
            return int(m.group(1)), 100
        return current, total

    def extract_save_dir(self, log_text: str):
        m = _RE_SAVED.search(log_text or "")
        return str(Path(m.group(1).strip()).parent) if m else None

    def is_finished(self, log_text: str, rc: int = -1) -> EngineResult:
        if _RE_SAVED.search(log_text or ""):
            return EngineResult(True, tracks_ok=1)
        m = _RE_ERROR.search(log_text or "")
        code, why = (m.group(1), m.group(2).strip()) if m else ("other", "")
        hint = {
            "no_token": "Premium is not activated on this PC",
            "invalid_key": "Premium key was rejected",
            "unreachable": "Premium server is unreachable",
            "not_found": "Track not found by Premium",
            "quality_unavailable": "Requested quality is not available",
            "busy": "Premium server is busy, try again shortly",
        }.get(code, why or "Premium download failed")
        return EngineResult(False, error=f"Premium: {hint}")
