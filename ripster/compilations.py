"""Compilation-awareness — the one blind spot every per-artist release scanner has.

Every release scanner in Ripster (Spotify radar, Apple watchlist checker, and any
future one) is built the same way: for each artist you follow, ask the service for
that artist's releases. That question has a blind spot, and it is not obvious:

    A various-artists compilation is not IN any artist's release list.

Its *album artist* is "Various Artists" or the label — so the artist whose track is
on it is only a track-level credit. Walk that artist's discography and the
compilation simply is not there. The individual tracks show up (each artist put
their track out as a single too), the compilation never does, and nothing errors:
the scanner reports success having never seen it. That is why this went unnoticed —
see [[project_radar_compilation_blindspot]].

The fix is always the same shape, only the transport differs per service:

    Do not ask "which albums is this artist the artist of?"
    Ask "which releases does this artist APPEAR on?" — then keep the compilations.

Per-service transports that answer the second question:

  Spotify   `queryArtistAppearsOn` (api-partner GraphQL) — the web player's own
            "Appears On" shelf. Its items carry no date or type, so each newly
            seen album id needs one `getAlbum` call for metadata.
  Apple     `lookup?id=<artist>&entity=song` — a *track* always carries its parent
            `collectionId`/`collectionName`/`collectionArtistName`, compilations
            included. (`entity=album` returns only albums the artist is credited
            as album artist on, which is exactly the blind spot.)
  Others    Same rule: find the endpoint that answers "appears on" / "tracks by",
            and read the parent release off the track.

This module holds the parts that are genuinely service-independent: deciding
whether a release is a compilation, and merging without duplicates.
"""
from __future__ import annotations

import re

# "Various Artists" as the services actually spell it, across storefront locales.
_VA_NAMES = {
    "various artists", "various", "varios artistas", "vários artistas",
    "verschiedene interpreten", "artistes divers", "artisti vari",
    "различные исполнители", "разные исполнители", "сборник",
    "オムニバス", "群星", "여러 아티스트", "va",
}

# Title shapes that mean "compilation" even when the service labels the release an
# album and credits it to a label rather than to Various Artists. Deliberately
# conservative: each of these is a phrase labels use for a comp, not a normal album.
_COMP_TITLE_RE = re.compile(
    r"\b(?:compilation|sampler|soundtrack|"
    r"(?:various|selected)\s+artists|"
    r"v\.?a\.?\s*[-–—]|"
    r"\d+\s+years\s+(?:of\s+)?|"
    r"best\s+of\s+\d{4}|"
    r"(?:summer|winter|spring|autumn|fall|ibiza|miami|ade)\s+(?:selection|sampler|compilation)"
    r")\b",
    re.IGNORECASE,
)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def is_various_artists(name: str) -> bool:
    """Is this credit the services' placeholder for "lots of people"?"""
    return _norm(name) in _VA_NAMES


def is_compilation(album_type: str = "", album_artist: str = "",
                   title: str = "", track_artist: str = "") -> bool:
    """Should this release be presented to the user as a compilation?

    Checked in order of how much we trust the signal:
      1. the service says so outright (album_type)
      2. the album is credited to Various Artists
      3. the album artist differs from the artist we were scanning for AND the
         title reads like a comp — this is the label-branded case ("Hospital
         Records presents…", "15 Years Sound Avenue")
    """
    if _norm(album_type) == "compilation":
        return True
    if is_various_artists(album_artist):
        return True
    if title and _COMP_TITLE_RE.search(title):
        # A release the scanned artist headlines is their own record even if the
        # title mentions a year count — only call it a comp when someone else
        # (a label, another artist) is the album artist.
        if not track_artist or _norm(album_artist) != _norm(track_artist):
            return True
    return False


def classify(album_type: str = "", album_artist: str = "",
             title: str = "", track_artist: str = "") -> tuple[str, str]:
    """→ (type, group) for the release store.

    Compilations are stored with group="compilation" rather than "appears_on"
    on purpose: the UI already has a "Сборники" filter, and a VA compilation is
    what a user means by one. Plain guest appearances (being featured on another
    artist's album) stay "appears_on" so they only show when explicitly asked for.
    """
    if is_compilation(album_type, album_artist, title, track_artist):
        return "compilation", "compilation"
    return (_norm(album_type) or "album"), "appears_on"


def merge_releases(existing: list, incoming: list) -> int:
    """Add `incoming` releases that aren't already in `existing` (by id).
    Returns how many were genuinely new. Mutates `existing`."""
    have = {r.get("id") for r in existing if r.get("id")}
    added = 0
    for r in incoming:
        rid = r.get("id")
        if not rid or rid in have:
            continue
        have.add(rid)
        existing.append(r)
        added += 1
    return added
