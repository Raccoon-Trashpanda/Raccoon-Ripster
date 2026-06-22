"""Writes and reads the _ripster.txt marker file placed in every download folder.

The marker serves two purposes:
  1. Machine-readable: lets /api/download-file find the exact output directory
     without heuristics by scanning for a file containing "task-id: <id>".
  2. Human-readable: a thank-you note with release info and a short description
     of the service, written in Russian and English.
"""
from __future__ import annotations

from pathlib import Path

MARKER_FILENAME = "_ripster.txt"
_TAG = "task-id:"   # prefix used for lookup


_TEMPLATE = """\
╔═══════════════════════════════════════════════╗
║                  R I P S T E R                   ║
║       Загрузчик музыки высшего качества       ║
╚═══════════════════════════════════════════════╝

  Спасибо, что используете Ripster!
  Thank you for using Ripster!

  Ripster — персональный загрузчик музыки в максимально доступном
  качестве (ALAC, Hi-Res FLAC, Dolby Atmos).
  Только для личного использования.

  Ripster is a personal music downloader providing
  the highest available quality (ALAC, Hi-Res FLAC,
  Dolby Atmos). For personal use only.

─────────────────────────────────────────────────
{release_block}─────────────────────────────────────────────────

task-id: {task_id}
"""


def _short_id(task_id: str) -> str:
    """First 8 hex chars of task_id — display-friendly download number."""
    clean = task_id.replace("-", "")
    return clean[:8].upper() if clean else task_id[:8].upper()


def write_marker(directory: Path, task_id: str, task: dict) -> bool:
    """Write _ripster.txt into *directory*.  Returns True on success."""
    meta    = task.get("meta") or {}
    title   = meta.get("title")       or meta.get("album")  or ""
    artist  = meta.get("artist")      or meta.get("albumArtist") or ""
    album   = meta.get("album")       or title or ""
    service = (task.get("service")    or "").capitalize()
    quality = task.get("quality")     or ""

    lines: list[str] = []
    if artist: lines.append(f"  Исполнитель / Artist:    {artist}")
    if album and album != title:
        lines.append(f"  Альбом / Album:      {album}")
    elif title:
        lines.append(f"  Название / Title:      {title}")
    if service: lines.append(f"  Сервис / Service:    {service}")
    if quality: lines.append(f"  Качество / Quality:    {quality.upper()}")
    lines.append(f"  ID загрузки / Download #: {_short_id(task_id)}")

    release_block = "\n".join(lines) + "\n\n"
    content = _TEMPLATE.format(task_id=task_id, release_block=release_block)
    try:
        (directory / MARKER_FILENAME).write_text(content, encoding="utf-8")
        return True
    except OSError:
        return False


def find_dir_by_task_id(task_id: str, base: Path, max_depth: int = 4) -> Path | None:
    """Walk *base* up to *max_depth* levels looking for a marker with *task_id*.
    Returns the directory containing the matching marker, or None.
    """
    needle = f"{_TAG} {task_id}"

    def _walk(d: Path, depth: int) -> Path | None:
        marker = d / MARKER_FILENAME
        if marker.is_file():
            try:
                if needle in marker.read_text(encoding="utf-8"):
                    return d
            except OSError:
                pass
        if depth <= 0:
            return None
        try:
            for child in sorted(d.iterdir()):
                if child.is_dir():
                    result = _walk(child, depth - 1)
                    if result:
                        return result
        except OSError:
            pass
        return None

    return _walk(base, max_depth)
