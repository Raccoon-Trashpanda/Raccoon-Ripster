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


_RE_TRACEBACK_HDR = re.compile(r"^Traceback \(most recent call last\):\s*$", re.M)


def extract_traceback_summary(log_text: str) -> str | None:
    """When *log_text* contains a real Python traceback (an unhandled exception
    in a CLI subprocess, e.g. the ``amz``/AppleMusicDecrypt/etc. tools), return
    the actual ``ExceptionType: message`` tail line instead of the useless
    generic header.

    Bug this fixes: engines that pick "the last line matching an error-ish
    regex, searching backwards" can land on the traceback HEADER line
    ("Traceback (most recent call last):") instead of the real exception
    line beneath it — the header contains the word "traceback" (matches a
    generic error regex) but the actual exception line often does NOT match
    (e.g. "ValueError: ..." has no `\\berror\\b` word boundary — "Error" is
    glued to "Value" with no separator, and doesn't contain "exception" as a
    literal substring either). Confirmed live: a guest's Amazon Music
    download crashed with an unhandled exception and the ONLY text that
    reached them was "Traceback (most recent call last):" — worse than no
    message at all, since it looks like a message but says nothing.

    Traceback frame lines ("File "...", line N, in <func>" and the source
    line under it) are indented; the actual exception line is the last
    NON-indented, non-empty line after the header — take that.
    """
    if not log_text:
        return None
    last_hdr = None
    for m in _RE_TRACEBACK_HDR.finditer(log_text):
        last_hdr = m
    if not last_hdr:
        return None
    tail = log_text[last_hdr.end():]
    exc_line = None
    for line in tail.splitlines():
        if line and not line[0].isspace():
            exc_line = line.strip()
    return exc_line[:300] if exc_line else None


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
