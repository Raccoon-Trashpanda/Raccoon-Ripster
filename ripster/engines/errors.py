"""Shared classifier for cross-service download failures every engine hits.

Two classes the owner flagged as needing real, honest messaging (not a generic
"error") across Apple/Tidal/Spotify/Qobuz/Deezer/etc.:
  • REGION — the content exists but is geo-locked to another region (ALAC hitting
    the region wall, a Tidal album not in the account's country, …).
  • GONE   — a phantom/dead link: the URL still resolves but the service has
    REMOVED the release (common on Spotify — the link shows but the track is gone).

Engines call ``classify_download_error(log_text)`` in their is_finished fallback;
the message propagates to the bot/UI automatically (both render the task error).
"""
from __future__ import annotations

import re

_PATTERNS: list[tuple[str, "re.Pattern[str]", str]] = [
    ("region",
     re.compile(
         r"region[\s\-]?lock|geo[\s\-]?block|geo[\s\-]?restrict|region[\s\-]?restrict"
         r"|not available in (your|this) (country|region)"
         r"|unavailable in (your|this) (country|region)"
         r"|not available in your country", re.I),
     "недоступно в регионе твоего аккаунта (гео-блок) — попробуй другой сервис "
     "(Apple/Qobuz/Deezer/Tidal) или смени регион аккаунта в Настройках."),
    ("gone",
     re.compile(
         r"\bnot found\b|no longer available|has been removed|been deleted"
         r"|does not exist|\b404\b|track (is )?not available|cannot be found"
         r"|removed from", re.I),
     "контент удалён из сервиса или ссылка фантомная (ведёт на уже отсутствующий "
     "релиз) — проверь ссылку или поищи тот же релиз на другом сервисе."),
]


def classify_download_error(log_text: str) -> tuple[str, str] | None:
    """Return ``(category, user_message)`` for a recognized cross-service failure,
    or ``None`` if nothing matched. Categories: ``'region'`` | ``'gone'``.

    REGION is checked before GONE: a geo-locked item often also says "not found"
    in the wrong region, but the actionable cause is the region wall.
    """
    text = log_text or ""
    for cat, rx, msg in _PATTERNS:
        if rx.search(text):
            return cat, msg
    return None
