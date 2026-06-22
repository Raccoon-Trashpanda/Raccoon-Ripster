"""Spotify engine — metadata only (artist / album browsing).

Spotify is **convert-only** in Ripster: tasks are never downloaded directly via
an engine — they're redirected to a target service (Deezer/Qobuz/Tidal/Apple).
This engine exists so Spotify has a uniform place in the engine registry for
browsing, matching the BaseSourceAdapter contract used by every other service.

The actual metadata logic + Spotify app-token + rate-limit state live in
``ripster.routes.discovery`` (where the rest of Spotify's plumbing is), so the
methods here delegate to it. Imports are **lazy** (inside the methods) to avoid
an engine→route import cycle at registration time.
"""
from __future__ import annotations

from typing import Optional

from .base import EngineBase
from .registry import register


@register
class SpotifyEngine(EngineBase):
    name = "spotify"

    def build_cmd(self, url: str, quality: str, config: dict) -> list[str]:
        # Spotify is convert-only — it is never downloaded through an engine.
        # The queue/router redirects Spotify URLs to a target service first.
        raise NotImplementedError(
            "Spotify is convert-only; redirect the task to a target service "
            "(Deezer/Qobuz/Tidal/Apple) instead of downloading via this engine."
        )

    async def get_artist(self, artist_id: str, types: str, config: dict) -> Optional[dict]:
        from ripster.routes.discovery import _artist_spotify
        return await _artist_spotify(artist_id, types)

    async def get_album(self, album_id: str, config: dict) -> Optional[dict]:
        from ripster.routes.discovery import _album_spotify
        return await _album_spotify(album_id)
