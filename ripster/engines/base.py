"""
Base contract every engine implements.

## How engines communicate with the runner

Old contract (v1, still supported for now): engines exposed three independent
methods — ``classify_line``, ``parse_progress``, ``is_finished`` — each called
by the runner in a different place. The runner had to stitch the results
together, and the return types were loose (a free-form string for line level,
a ``(current, total)`` tuple for progress).

New contract (v2): engines implement ``iter_events(line, progress=...)`` which
yields zero or more ``Event`` objects describing *what happened*. The runner
consumes events and translates them to UI/WS broadcasts. This makes engines
responsible for their own output format and removes the class of bugs where
one engine returns ``"success"`` and another ``"ok"``.

Engines can opt into v2 by overriding ``iter_events``; the default impl
bridges v1 → v2 so existing engines keep working unchanged.
"""
from __future__ import annotations

import re as _re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, Optional


class LineLevel(str, Enum):
    """Severity of a log line. Subclasses ``str`` so ``.value`` slots
    straight into existing WebSocket payloads that expect plain strings."""
    INFO    = "info"
    WARN    = "warn"
    ERROR   = "error"
    SUCCESS = "success"
    STDOUT  = "stdout"


class EventKind(str, Enum):
    """What sort of thing the engine is telling us about."""
    LINE         = "line"           # plain stdout/stderr line, for the console view
    TRACK_START  = "track_start"    # engine started working on a track
    TRACK_DONE   = "track_done"     # engine finished a track successfully
    TRACK_ERROR  = "track_error"    # engine failed a track
    PROGRESS     = "progress"       # refined progress update (current/total known)
    AUTH_ERROR   = "auth_error"     # authentication failed — UI should prompt
    FATAL        = "fatal"          # unrecoverable; stop processing the task
    FINISHED     = "finished"       # final summary at end of process


@dataclass
class Event:
    kind:    EventKind
    message: str = ""
    level:   LineLevel = LineLevel.STDOUT
    # Progress fields — only meaningful when kind in {PROGRESS, TRACK_*}.
    current: Optional[int] = None   # 0-based index of the track being worked on
    total:   Optional[int] = None   # total track count, if known
    track:   Optional[str] = None   # track title, when we can parse it
    extra:   dict = field(default_factory=dict)


@dataclass
class EngineResult:
    """Summary produced at the end of a task."""
    success:       bool
    tracks_ok:     int = 0
    tracks_err:    int = 0
    error:         str = ""
    quality_actual: str = ""   # detected actual quality ID (e.g. "6", "7", "27"); "" = unknown


class EngineBase(ABC):
    """Contract for a download engine.

    Required methods: ``build_cmd``. Everything else has a sensible default
    backed by the legacy regex methods (``classify_line``, ``parse_progress``,
    ``is_finished``) so older engine classes keep working.
    """

    name: str = "base"

    # ── Required ────────────────────────────────────────────────────────────

    @abstractmethod
    def build_cmd(self, url: str, quality: str, config: dict) -> list[str]:
        """Return the subprocess command to execute."""
        ...

    def working_dir(self) -> str | None:
        """Return working directory for the subprocess, or None for default."""
        return None

    # ── v1 regex-based API (still supported) ────────────────────────────────

    def classify_line(self, line: str) -> str:
        """Return log level for a stdout line.
        Prefer overriding ``iter_events`` for richer output."""
        return LineLevel.STDOUT.value

    def parse_progress(self, line: str, current: int, total: int) -> tuple[int, int]:
        """Return updated ``(current, total)``.
        Prefer overriding ``iter_events`` which can emit PROGRESS events."""
        return current, total

    def is_finished(self, log_text: str, rc: int = -1) -> EngineResult:
        """Analyse full log and return final result. rc is the subprocess exit code."""
        return EngineResult(success=True)

    # ── v2 structured event API ────────────────────────────────────────────

    def iter_events(self, line: str, *, progress: tuple[int, int]) -> Iterable[Event]:
        """Yield one or more Events for a single output line.

        The runner feeds every stdout/stderr line here and acts on what it gets
        back. ``progress`` is the runner's current ``(current, total)`` state,
        passed in case the engine wants to refine it.

        Default implementation bridges the legacy API: classify + parse_progress
        → a LINE event plus optionally a PROGRESS event.
        """
        clean = _strip_ansi(line)

        level_str = self.classify_line(clean)
        try:
            level = LineLevel(level_str)
        except ValueError:
            # Engine returned an unknown level — don't silently rename, fall back.
            level = LineLevel.STDOUT

        yield Event(kind=EventKind.LINE, message=clean, level=level)

        new_cur, new_tot = self.parse_progress(clean, *progress)
        if (new_cur, new_tot) != progress:
            yield Event(kind=EventKind.PROGRESS, current=new_cur, total=new_tot)

    def qualities(self) -> list[dict]:
        """Return quality definitions for this engine."""
        return []

    def extract_save_dir(self, log_text: str) -> Optional[str]:
        """Parse the output directory from the completed process log.
        Return an absolute path string, or None if not determinable."""
        return None

    # ── BaseSourceAdapter interface (optional, for search-capable services) ──

    async def search(
        self,
        query: str,
        search_type: str,
        limit: int,
        config: dict,
    ) -> list[dict]:
        """Search the service for *query*.

        search_type: "album" | "track" | "artist" | "playlist"
        Returns a list of result dicts (same shape as discovery.py results).
        Default implementation returns [] — override in engines that have a search API.
        """
        return []

    async def get_info(self, url: str, config: dict) -> Optional[dict]:
        """Fetch structured metadata for a URL (album/track/playlist).

        Returns a dict with at minimum: id, title, artist, type, url, service.
        Default returns None — override in engines with a metadata API.
        """
        return None

    async def get_artist(self, artist_id: str, types: str, config: dict) -> Optional[dict]:
        """Return artist info + releases, or None if not implemented.

        types: comma-separated subset of "album,single,ep,compilation,live".
        Return shape: {"artist": {...}, "releases": [{...}, ...]}
        """
        return None

    async def get_album(self, album_id: str, config: dict) -> Optional[dict]:
        """Return album metadata + full track list, or None if not implemented.

        Return shape: {"album": {...}, "tracks": [{...}, ...]}
        """
        return None


# ── Helpers ─────────────────────────────────────────────────────────────────

_ANSI_RE = _re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

def _strip_ansi(s: str) -> str:
    """Remove ANSI escape sequences from a line. Most CLI tools emit colour
    codes when stdout looks like a TTY."""
    return _ANSI_RE.sub("", s)
