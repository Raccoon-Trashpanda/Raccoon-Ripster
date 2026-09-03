"""Watchlist suggestions mined from the user's OWN stats DB.

This is deliberately *not* a general recommender. Every suggestion is backed
by a fact already recorded in `ripster_stats.db`, so the reason shown in the
UI is a statement about the user's own library, never a guess from a model:

  direct     — artist is the main credit on N completed downloads
  collab     — artist only ever shows up inside a multi-artist credit
               ("Volen Sentir, Marsh & XIRA" → Marsh, XIRA)
  remixer    — artist only ever shows up inside a track title
               ("(Gorje Hewek Remix)", "feat. Mokka") — a strong signal:
               they're all over the library but were never grabbed solo
  sc_channel — SoundCloud channel, by plays (stream_events) + downloads

Anything already on the watchlist is filtered out, so the panel empties as
the user acts on it.

Scoring is a weighted sum with a recency multiplier; the absolute number is
meaningless, it only exists to rank. Reasons are returned as i18n key +
params so the frontend renders them in the active language.
"""
from __future__ import annotations

import re
import sqlite3
import time
from collections import defaultdict
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "ripster_stats.db"

# ── Noise filters ─────────────────────────────────────────────────────────────

# Compilation / placeholder credits that are never a real artist to watch.
_STOP_ARTISTS = {
    "various artists", "va", "various", "unknown artist", "unknown", "n/a",
    "sbornik", "сборник", "разные исполнители", "feature presentation",
    "soundtrack", "ost", "original soundtrack", "compilation", "dj mix",
}

# Words that make a "(… Mix)" capture a mix *type*, not a person.
_MIX_NOISE = {
    "radio", "extended", "club", "original", "instrumental", "acoustic",
    "vocal", "dub", "live", "album", "single", "short", "long", "full",
    "continuous", "clean", "dirty", "edit", "version", "remastered",
    "bonus", "intro", "outro", "mixed", "unmixed", "dj", "reprise",
}

# "(Gorje Hewek Remix)", "[RAWSOUL012]"-style noise is excluded by requiring
# one of these suffixes — a bare "(Mix)" is never treated as a credit.
_REMIX_RE = re.compile(
    r"[\(\[]\s*([^()\[\]]{2,60}?)\s+"
    r"(?:Remix(?:es)?|RMX|Rework|Reinterpretation|Bootleg|Refix|Flip|VIP)\s*[\)\]]",
    re.IGNORECASE,
)
_FEAT_RE = re.compile(
    r"\b(?:feat|ft|featuring)\.?\s+([^()\[\],]{2,60}?)"
    r"(?=\s*[\)\]\[,]|\s+-\s|$)",
    re.IGNORECASE,
)

_SC_URL_RE = re.compile(r"soundcloud\.com/([A-Za-z0-9_\-]+)")
_APPLE_ARTIST_RE = re.compile(r"music\.apple\.com/[^/]+/artist/[^/]+/(\d+)")

_WS_RE = re.compile(r"\s+")


def _norm(name: str) -> str:
    """Fold to a comparison key: case, spacing and trailing punctuation only.
    Deliberately does NOT strip diacritics — 'Fejká' and 'Fejka' stay distinct
    because they may genuinely be different artists."""
    s = _WS_RE.sub(" ", (name or "").strip().strip(".,;:-–—/ ").lower())
    return s


def _is_noise(name: str) -> bool:
    n = _norm(name)
    if len(n) < 2 or len(n) > 60:
        return True
    if n in _STOP_ARTISTS:
        return True
    # A capture that is only mix-type words ("Extended", "Radio Edit") — drop.
    words = [w for w in re.split(r"[\s&,]+", n) if w]
    if words and all(w in _MIX_NOISE for w in words):
        return True
    # Pure numbers / catalogue codes.
    if re.fullmatch(r"[\d\W_]+", n):
        return True
    return False


def _split_credit(credit: str) -> list[str]:
    """Split a multi-artist credit string conservatively.

    Commas are a reliable list separator; ' & ' is NOT — 'Above & Beyond' and
    'Gabriel & Dresden' are single acts. So ' & ' is only split when the string
    already looks like a list (i.e. it contains a comma), which is exactly the
    'A, B & C' shape.
    """
    raw = (credit or "").strip()
    if not raw:
        return []
    if "," in raw:
        parts: list[str] = []
        for chunk in raw.split(","):
            chunk = chunk.strip()
            if " & " in chunk:
                parts.extend(p.strip() for p in chunk.split(" & "))
            else:
                parts.append(chunk)
        return [p for p in parts if p]
    return [raw]


def _mine_title_credits(title: str) -> list[str]:
    """Pull remixer / featured-artist names out of a track title."""
    out: list[str] = []
    for m in _REMIX_RE.finditer(title or ""):
        out.append(m.group(1).strip())
    for m in _FEAT_RE.finditer(title or ""):
        out.append(m.group(1).strip())
    return out


def _recency_weight(last_ts: int, now: int) -> float:
    """Recent taste counts more than a one-off from two years ago."""
    age_days = max(0, (now - last_ts) / 86_400)
    if age_days <= 90:
        return 1.0
    if age_days <= 365:
        return 0.8
    return 0.55


# ── Watchlist matching ────────────────────────────────────────────────────────

def watched_keys(items: list) -> set[str]:
    """Normalised keys for everything already on the watchlist — name, and the
    SoundCloud permalink when the entry is a channel."""
    keys: set[str] = set()
    for it in items or []:
        n = _norm(it.get("name", ""))
        if n:
            keys.add(n)
        url = it.get("url", "") or ""
        m = _SC_URL_RE.search(url)
        if m:
            keys.add(_norm(m.group(1)))
        elif it.get("service") == "soundcloud" and url:
            keys.add(_norm(url.strip("/").split("/")[-1]))
    return keys


# ── Main computation ──────────────────────────────────────────────────────────

def compute(watchlist_items: list, limit: int = 12, min_score: float = 8.0) -> dict:
    """Rank suggestion candidates from the stats DB. Pure read, no network."""
    if not DB_PATH.exists():
        return {"ok": False, "error": "no stats db", "suggestions": []}

    now = int(time.time())
    skip = watched_keys(watchlist_items)

    try:
        con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=10)
        con.row_factory = sqlite3.Row
    except Exception as e:
        return {"ok": False, "error": str(e), "suggestions": []}

    try:
        rows = con.execute(
            "SELECT artist, album, title, service, url, tracks, ts FROM downloads"
            " WHERE status='done'"
        ).fetchall()
        sc_plays = con.execute(
            "SELECT stream_name, COUNT(*) c, MAX(ts) last_ts FROM stream_events"
            " WHERE stream_type='soundcloud' AND stream_name!=''"
            " GROUP BY stream_name"
        ).fetchall()
    finally:
        con.close()

    # cand[key] = accumulator
    cand: dict[str, dict] = {}

    def _bump(name: str, kind: str, ts: int, **extra):
        if _is_noise(name):
            return
        key = _norm(name)
        if key in skip:
            return
        c = cand.setdefault(key, {
            "names": defaultdict(int),      # original spellings → frequency
            "direct": 0, "direct_tracks": 0, "direct_albums": set(),
            "collab": 0, "collab_with": defaultdict(int),
            "remix": 0, "remix_titles": [],
            "sc_plays": 0, "sc_downloads": 0,
            "sc_permalink": "", "apple_id": "",
            "services": defaultdict(int),
            "last_ts": 0,
        })
        c["names"][name.strip()] += 1
        c["last_ts"] = max(c["last_ts"], ts)
        if kind == "direct":
            c["direct"] += 1
            c["direct_tracks"] += extra.get("tracks", 1)
            c["direct_albums"].add(extra.get("album", ""))
        elif kind == "collab":
            c["collab"] += 1
            partner = extra.get("partner", "")
            if partner:
                c["collab_with"][partner] += 1
        elif kind == "remix":
            c["remix"] += 1
            if len(c["remix_titles"]) < 3 and extra.get("title"):
                c["remix_titles"].append(extra["title"])
        svc = extra.get("service", "")
        if svc:
            c["services"][svc] += 1
        if extra.get("sc_permalink") and not c["sc_permalink"]:
            c["sc_permalink"] = extra["sc_permalink"]
        if extra.get("apple_id") and not c["apple_id"]:
            c["apple_id"] = extra["apple_id"]

    # ── Pass 1: how often does each credit string appear standalone? ─────────
    # Used to decide whether a split fragment is trustworthy (see below).
    standalone: dict[str, int] = defaultdict(int)
    frag_contexts: dict[str, set[str]] = defaultdict(set)
    for r in rows:
        credit = (r["artist"] or "").strip()
        if not credit:
            continue
        parts = _split_credit(credit)
        if len(parts) == 1:
            standalone[_norm(credit)] += 1
        else:
            for p in parts:
                frag_contexts[_norm(p)].add(_norm(credit))

    # ── Pass 2: accumulate signals ──────────────────────────────────────────
    for r in rows:
        credit = (r["artist"] or "").strip()
        title  = (r["title"] or "").strip()
        svc    = (r["service"] or "").strip()
        url    = (r["url"] or "").strip()
        ts     = int(r["ts"] or 0)
        tracks = int(r["tracks"] or 1)

        sc_permalink = ""
        if svc == "soundcloud":
            m = _SC_URL_RE.search(url)
            if m:
                sc_permalink = m.group(1)
        apple_id = ""
        m = _APPLE_ARTIST_RE.search(url)
        if m:
            apple_id = m.group(1)

        if credit:
            parts = _split_credit(credit)
            if len(parts) == 1:
                _bump(credit, "direct", ts, tracks=tracks, album=(r["album"] or ""),
                      service=svc, sc_permalink=sc_permalink, apple_id=apple_id)
            else:
                for p in parts:
                    key = _norm(p)
                    # A fragment is only trusted when it stands on its own
                    # somewhere in the library, or shows up across ≥2 different
                    # collaborations. Otherwise 'Above & Beyond' would spawn
                    # bogus 'Above' / 'Beyond' suggestions.
                    if standalone.get(key, 0) > 0:
                        _bump(p, "direct", ts, tracks=tracks, service=svc)
                    elif len(frag_contexts.get(key, ())) >= 2:
                        partner = next((q for q in parts if _norm(q) != key), "")
                        _bump(p, "collab", ts, partner=partner, service=svc)

        for name in _mine_title_credits(title):
            _bump(name, "remix", ts, title=title, service=svc)

    # ── Pass 3: SoundCloud channels ─────────────────────────────────────────
    # Play events carry "Channel — Track title"; downloads carry the real
    # permalink. Merge both so a heavily-played channel gets a usable link.
    sc_links: dict[str, str] = {}
    for r in rows:
        if (r["service"] or "") != "soundcloud":
            continue
        m = _SC_URL_RE.search(r["url"] or "")
        if m:
            sc_links.setdefault(_norm(r["artist"] or ""), m.group(1))

    for r in sc_plays:
        raw = r["stream_name"] or ""
        channel = raw.split(" — ", 1)[0].strip() if " — " in raw else ""
        if not channel or _is_noise(channel):
            continue
        key = _norm(channel)
        if key in skip:
            continue
        _bump(channel, "sc", int(r["last_ts"] or 0), service="soundcloud",
              sc_permalink=sc_links.get(key, ""))
        if key in cand:
            cand[key]["sc_plays"] += int(r["c"] or 0)

    for r in rows:
        if (r["service"] or "") != "soundcloud":
            continue
        key = _norm(r["artist"] or "")
        if key in cand:
            cand[key]["sc_downloads"] += 1

    # ── Score + build output ────────────────────────────────────────────────
    out: list[dict] = []
    for key, c in cand.items():
        rw = _recency_weight(c["last_ts"], now)
        score = (
            c["direct"] * 10
            + c["direct_tracks"] * 0.25
            + max(len(c["direct_albums"]) - 1, 0) * 4
            + c["collab"] * 6
            + c["remix"] * 7
            + min(c["sc_plays"], 200) * 0.35
            + c["sc_downloads"] * 6
        ) * rw
        if score < min_score:
            continue

        # Most frequent original spelling wins as the display name.
        display = max(c["names"].items(), key=lambda kv: kv[1])[0]
        # Where should this be *watched*? SoundCloud plays alone are not enough:
        # 'Above & Beyond' is played as an SC mix channel but is an Apple artist
        # with 20 downloads, and watching it as an SC channel finds nothing.
        # Only route to SoundCloud when SC is genuinely where this name lives.
        sc_hits = c["services"].get("soundcloud", 0)
        other_hits = sum(v for k, v in c["services"].items() if k != "soundcloud")
        sc_dominant = sc_hits > other_hits and sc_hits > 0
        service = "soundcloud" if (sc_dominant or (c["sc_plays"] > 0 and other_hits == 0)) else "apple"

        reason = _pick_reason(c, display)
        out.append({
            "name":        display,
            "key":         key,
            "service":     service,
            # Two very different kinds of suggestion, ranked in separate pools:
            # "top" is volume ("you grab this artist a lot"), "discovery" is an
            # artist all over the library who was never downloaded on their own.
            # Without the split, discovery items always lose on raw score.
            "kind":        "discovery" if c["direct"] == 0 else "top",
            "score":       round(score, 1),
            "reason":      reason["key"],
            "reason_args": reason["args"],
            "apple_id":    c["apple_id"],
            "sc_permalink": c["sc_permalink"],
            "stats": {
                "downloads":  c["direct"],
                "tracks":     c["direct_tracks"],
                "albums":     len([a for a in c["direct_albums"] if a]),
                "collabs":    c["collab"],
                "remixes":    c["remix"],
                "sc_plays":   c["sc_plays"],
                "last_ts":    c["last_ts"],
            },
        })

    out.sort(key=lambda x: -x["score"])
    top  = [s for s in out if s["kind"] == "top"]
    disc = [s for s in out if s["kind"] == "discovery"]

    n_top = max(1, round(limit * 0.6))
    picked = top[:n_top] + disc[:limit - n_top]
    # If one pool is short, top the list back up from the other.
    if len(picked) < limit:
        rest = [s for s in out if s not in picked]
        picked += rest[:limit - len(picked)]
    picked.sort(key=lambda x: (x["kind"] != "top", -x["score"]))

    return {
        "ok": True,
        "generated": now,
        "watched": len(skip),
        "candidates": len(cand),
        "suggestions": picked,
    }


# ── Resolvers ─────────────────────────────────────────────────────────────────
# A watchlist entry is only useful if the background checker can actually poll
# it: the Apple branch needs `artist_id` (it hits the artistnewreleases RSS
# feed by id) and the SoundCloud branch needs a real permalink. Adding a bare
# name produces an entry that silently never checks anything, so every add
# path goes through these.

def apple_id_from_url(url: str) -> str:
    """music.apple.com/<store>/artist/<slug>/<id> → id."""
    m = _APPLE_ARTIST_RE.search(url or "")
    return m.group(1) if m else ""


def sc_permalink_from_url(url: str) -> str:
    m = _SC_URL_RE.search(url or "")
    return m.group(1) if m else ""


async def resolve_apple_artist(name: str, storefront: str = "us") -> dict:
    """Look an artist up in the public iTunes Search API (no auth, no token).
    Returns {"artist_id", "url", "name"} or {} when nothing matches."""
    name = (name or "").strip()
    if not name:
        return {}
    import httpx
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                "https://itunes.apple.com/search",
                params={"term": name, "entity": "musicArtist",
                        "limit": 8, "country": storefront},
            )
            if r.status_code != 200:
                return {}
            results = (r.json() or {}).get("results") or []
    except Exception as e:
        print(f"[wl-suggest] apple lookup '{name}': {e}", flush=True)
        return {}

    target = _norm(name)
    exact = [x for x in results if _norm(x.get("artistName", "")) == target]
    pool = exact or results
    pick = (pool or [None])[0]
    if not pick or not pick.get("artistId"):
        return {}
    out = {
        "artist_id": str(pick["artistId"]),
        "url":       pick.get("artistLinkUrl", ""),
        "name":      pick.get("artistName", name),
        "exact":     bool(exact),
    }
    # ЖЁСТКИЕ МЕРЫ ПО ИМЕНАМ (03.09.2026). Раньше при нескольких артистах с
    # одинаковым именем (BOP, Solomon Grey…) молча брался первый из iTunes —
    # и вотчлист/радар навсегда следили за ЧУЖИМ артистом. Теперь, если точных
    # совпадений по имени больше одного, отдаём кандидатов с жанром: вызывающий
    # обязан либо уточнить (вставить ссылку на нужного артиста), либо показать
    # выбор человеку. artist_id при этом всё равно заполнен — «лучшая догадка»,
    # чтобы запись не была битой, — но помечен `ambiguous`.
    _uniq = {str(x.get("artistId")): x for x in pool if x.get("artistId")}
    if len(_uniq) > 1:
        out["ambiguous"] = True
        out["candidates"] = [
            {"artist_id": aid,
             "name":  x.get("artistName", ""),
             "genre": x.get("primaryGenreName", ""),
             "url":   x.get("artistLinkUrl", "")}
            for aid, x in list(_uniq.items())[:6]
        ]
    return out


async def resolve_sc_channel(name: str, permalink_hint: str = "") -> dict:
    """Confirm a SoundCloud channel permalink by actually asking the SC API for
    its tracks — a guessed slug that 404s must not become a dud entry."""
    from ripster.routes.soundcloud import sc_user_tracks

    guesses = []
    if permalink_hint:
        guesses.append(permalink_hint)
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    if slug and slug not in guesses:
        guesses.append(slug)
    bare = slug.replace("-", "")
    if bare and bare not in guesses:
        guesses.append(bare)

    for g in guesses:
        try:
            r = await sc_user_tracks(permalink=g, limit=1)
        except Exception:
            continue
        if r.get("ok") and r.get("results"):
            return {"permalink": g, "url": f"https://soundcloud.com/{g}"}
    return {}


def _pick_reason(c: dict, display: str) -> dict:
    """Choose the single most interesting fact to show on the card."""
    # A remixer/featured artist who was never downloaded solo is the most
    # surprising suggestion, so it wins over a plain download count.
    if c["remix"] >= 2 and c["direct"] == 0:
        return {"key": "wls.r_remixer_only",
                "args": {"n": c["remix"], "title": (c["remix_titles"] or [""])[0]}}
    if c["sc_plays"] >= 10 and c["direct"] == 0:
        return {"key": "wls.r_sc_plays", "args": {"n": c["sc_plays"]}}
    if c["collab"] >= 2 and c["direct"] == 0:
        partner = ""
        if c["collab_with"]:
            partner = max(c["collab_with"].items(), key=lambda kv: kv[1])[0]
        return {"key": "wls.r_collab", "args": {"n": c["collab"], "with": partner}}
    if c["direct"] and c["remix"]:
        return {"key": "wls.r_direct_remix",
                "args": {"n": c["direct"], "r": c["remix"]}}
    if c["direct"] and c["sc_plays"] >= 5:
        return {"key": "wls.r_direct_plays",
                "args": {"n": c["direct"], "p": c["sc_plays"]}}
    if c["sc_downloads"] and not c["direct"]:
        return {"key": "wls.r_sc_downloads", "args": {"n": c["sc_downloads"]}}
    return {"key": "wls.r_direct",
            "args": {"n": c["direct"], "t": c["direct_tracks"]}}
