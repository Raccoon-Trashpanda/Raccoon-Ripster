"""#5b Phase 2 — authoritative per-track shortfall analysis for a partial release.

When a download comes back short (got M of N), the count alone can't say WHICH
tracks are missing or WHY. This module compares the release's CANONICAL tracklist
(from the source service's own album API) against the files actually delivered,
then classifies each missing track so the UI gives the RIGHT next action:

  * track genuinely UNAVAILABLE on the source (region-lock / no-format on that
    server)  → offer a cross-service top-up (fetch the rest from Qobuz/Apple/…);
  * track that IS available on the source but didn't land → our run failed
    transiently (crash / network / auth) → offer a plain retry on the SAME
    service. NEVER claim "unavailable" for a track the source actually has.

The probe-gate (`available` flag, straight from the source album API) is what
stops the feature from misfiring on a crashed/aborted run — see the discussion in
SESSION_CHANGES.md. Matching is by normalized title (the source album listing
gives title + a per-track availability flag in ONE call — no N per-track ISRC
fetches needed); ISRC matching is a later refinement for resolving the top-up.
"""
from __future__ import annotations

import re
from typing import Optional

from ripster import http_client as _HTTP

_DEEZER_API = "https://api.deezer.com"


def _norm(s: str) -> str:
    """Lowercase alphanumeric-only key for fuzzy title containment matching."""
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def _core(title: str) -> str:
    """Normalized title with a trailing parenthetical dropped (``"Outsight (feat.
    X)"`` → ``"outsight"``), so a catalog title carrying a ``(feat. …)`` suffix
    still matches a filename whose rename template omitted it, and vice-versa."""
    return _norm(re.sub(r"\s*[\(\[].*$", "", title or ""))


def diff_tracklist(canonical: list, delivered_names: list) -> list:
    """Return the canonical tracks NOT represented among the delivered files.

    `canonical`: ``[{"num": int, "title": str, "available": bool}, …]`` from the
    source album API. `delivered_names`: the manifest's delivered basenames (they
    carry the track title, e.g. ``"03. Artist - Title.flac"``). A canonical track
    counts as delivered when its normalized title OR its parenthetical-stripped
    core is contained in some delivered filename. Erring toward "delivered" (not
    over-reporting missing) — a false "missing" would wrongly nag the user to
    re-fetch a track they already have. Order preserved.
    """
    dnorm = [_norm(n) for n in (delivered_names or [])]
    missing = []
    for tr in canonical or []:
        full = _norm(tr.get("title", ""))
        if not full:
            continue
        # `full` first; `core` only when it's substantial (≥4 chars) so a short
        # core ("Go", "U") can't loosely match an unrelated filename.
        keys = [full] + ([_core(tr.get("title", ""))]
                         if len(_core(tr.get("title", ""))) >= 4 else [])
        if not any(k and k in dn for k in keys for dn in dnorm):
            missing.append(tr)
    return missing


# Release-reason tokens (from runner._classify_partial_reason) that mean the
# SOURCE can't deliver the track in the requested form — even a "readable" track
# is missing in the wanted quality/region. A cross-service top-up is the fix.
_SOURCE_LIMITED_REASONS = frozenset({"no-flac", "region", "removed", "unavailable"})


def classify_missing(missing: list, reason: str = "") -> tuple:
    """Split missing tracks into (cross_service, retry_same) — the probe-gate.

    A track needs a CROSS-SERVICE top-up when the source can't give it in the
    requested form:
      * ``available is False`` — Deezer's per-track ``readable`` is False
        (region-blocked / removed); or
      * the run's ``reason`` is a source/format limitation (``no-flac`` etc.) —
        a track can be ``readable`` (streamable in MP3) yet absent in FLAC, so a
        same-service retry of the SAME quality would just fail again. The live
        Toma case proved ``readable`` alone is insufficient here.
    Otherwise (source HAS it, shortfall was transient: crash / network /
    postprocess) → a same-service RETRY is the right fix, never a misleading
    "unavailable" claim. Unknown availability defaults to retry, not cross-service.
    """
    source_limited = (reason or "") in _SOURCE_LIMITED_REASONS
    cross_service, retry_same = [], []
    for m in (missing or []):
        if m.get("available") is False or source_limited:
            cross_service.append(m)
        else:
            retry_same.append(m)
    return cross_service, retry_same


def _deezer_album_id(url_or_id: str) -> str:
    m = re.search(r"/album/(\d+)", url_or_id or "")
    if m:
        return m.group(1)
    s = (url_or_id or "").strip()
    return s if s.isdigit() else ""


async def fetch_deezer_tracklist(url_or_id: str) -> Optional[list]:
    """Canonical tracklist for a Deezer album in ONE call: ``[{num, title,
    available}]``. ``available`` is Deezer's per-track ``readable`` flag (False =
    not playable in this market → genuinely unavailable on the source). Returns
    None on any failure (caller falls back to count-only reporting)."""
    aid = _deezer_album_id(url_or_id)
    if not aid:
        return None
    try:
        async with _HTTP.ashared() as c:
            r = await c.get(f"{_DEEZER_API}/album/{aid}")
            d = r.json()
        if d.get("error") or not d.get("id"):
            return None
        out = []
        for i, t in enumerate(((d.get("tracks") or {}).get("data") or []), 1):
            out.append({
                "num": t.get("track_position") or i,
                "title": t.get("title", ""),
                # Deezer omits `readable` in the album listing for available
                # tracks and only marks it False when blocked → default True.
                "available": bool(t.get("readable", True)),
            })
        return out
    except Exception:
        return None
