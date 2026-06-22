"""
Multi-stage tracklist verification.

Identifying the RIGHT tracklist for a DJ mix is the hard part — search engines
and YouTube happily return a *different* set (e.g. kloyd's instead of Braxton @
Anjunakitchen). So instead of one fuzzy title check we run every identifying
signal we can compute and aggregate them into a confidence score + verdict.

A "target" is the mix we already know (from SoundCloud): title, artist,
duration, the SC-description tracklist (names, our reference of truth), and the
mix's own source URLs (its SC/YouTube links). A "candidate" is a tracklist we
found elsewhere (1001TL / MixesDB / YouTube): title, duration, tracks, and
optionally the raw page html (for backlink detection).

`score_candidate` → {score 0..1, tier, checks:[...]}. `pick_best` chooses among
candidates. `cross_verify` rewards agreement between independent sources.

Tiers: definitive > high > medium > low > reject.
"""
from __future__ import annotations

import re
import difflib

# ── normalisation ────────────────────────────────────────────────────────────
_STOP = {"the", "a", "an", "feat", "ft", "featuring", "vs", "with", "and",
         "remix", "edit", "mix", "extended", "original", "radio", "version",
         "live", "set", "id"}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _tokens(s: str) -> set[str]:
    return {w for w in _norm(s).split() if w and w not in _STOP and len(w) > 1}


def _track_name(tr: dict) -> str:
    a = (tr.get("artist") or "").strip()
    t = (tr.get("title") or "").strip()
    return (a + " " + t).strip() if a else t


def _real_tracks(tracks: list) -> list:
    """Drop empty / unknown 'ID - ID' / 'w/' sub-rows for counting purposes."""
    out = []
    for tr in tracks or []:
        if tr.get("is_with"):
            continue
        nm = _track_name(tr)
        if not nm or _norm(nm) in ("id", "id id", ""):
            continue
        out.append(tr)
    return out


def _ratio(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, _norm(a), _norm(b)).ratio()


# ── individual checks ────────────────────────────────────────────────────────
# Each returns dict(name, applicable, score 0..1, weight, detail). score is only
# meaningful when applicable; non-applicable checks are skipped in the average.
def _chk(name, applicable, score, weight, detail):
    return {"name": name, "applicable": bool(applicable),
            "score": round(float(score), 3) if applicable else None,
            "weight": weight, "detail": detail}


def check_source_backlink(target, cand):
    """Definitive: candidate page links the mix's own SC/YouTube URL/id."""
    html = cand.get("html") or ""
    urls = target.get("source_urls") or []
    hit = None
    for u in urls:
        if not u:
            continue
        # match the stable id part of the url (last path segment) too
        seg = re.sub(r"[?#].*$", "", str(u)).rstrip("/").split("/")[-1]
        if (u and u in html) or (seg and len(seg) >= 6 and seg in html):
            hit = u
            break
    return _chk("source_backlink", bool(urls), 1.0 if hit else 0.0, 1.0,
                f"matched {hit}" if hit else "no backlink")


def check_title_similarity(target, cand):
    r = _ratio(target.get("title", ""), cand.get("title", ""))
    return _chk("title_similarity", bool(cand.get("title")), r, 0.20,
                f"ratio={r:.2f}")


def check_artist_match(target, cand):
    ta = _tokens(target.get("artist", ""))
    if not ta:
        return _chk("artist_match", False, 0, 0.15, "no target artist")
    ct = _tokens(cand.get("title", "")) | _tokens(
        " ".join(_track_name(t) for t in cand.get("tracks", [])[:1]))
    inter = ta & ct
    score = len(inter) / len(ta)
    return _chk("artist_match", True, score, 0.15,
                f"{sorted(inter)}/{sorted(ta)}")


def check_duration(target, cand):
    td, cd = target.get("duration"), cand.get("duration")
    if not td or not cd:
        return _chk("duration", False, 0, 0.20, f"target={td} cand={cd}")
    diff = abs(td - cd)
    tol = max(120, 0.10 * td)  # within 2 min or 10%
    score = max(0.0, 1.0 - diff / (tol * 2))
    return _chk("duration", True, score, 0.20, f"|{td}-{cd}|={diff}s tol={int(tol)}s")


def check_track_count(target, cand):
    ref = _real_tracks(target.get("tracklist", []))
    cnt = _real_tracks(cand.get("tracks", []))
    if not ref or not cnt:
        return _chk("track_count", False, 0, 0.10,
                    f"ref={len(ref)} cand={len(cnt)}")
    a, b = len(ref), len(cnt)
    score = 1.0 - abs(a - b) / max(a, b)
    return _chk("track_count", True, score, 0.10, f"ref={a} cand={b}")


def check_name_overlap(target, cand):
    ref = _real_tracks(target.get("tracklist", []))
    cnt = _real_tracks(cand.get("tracks", []))
    if not ref or not cnt:
        return _chk("name_overlap", False, 0, 0.20, "missing tracklist")
    ref_sets = [_tokens(_track_name(t)) for t in ref]
    matched = 0
    for c in cnt:
        ct = _tokens(_track_name(c))
        if not ct:
            continue
        # a candidate track matches if it shares a strong token overlap with any ref track
        for rs in ref_sets:
            if rs and len(ct & rs) / max(1, min(len(ct), len(rs))) >= 0.6:
                matched += 1
                break
    score = matched / max(len(ref), len(cnt))
    return _chk("name_overlap", True, score, 0.20,
                f"matched={matched}/{max(len(ref),len(cnt))}")


def check_first_track(target, cand):
    ref = _real_tracks(target.get("tracklist", []))
    cnt = _real_tracks(cand.get("tracks", []))
    if not ref or not cnt:
        return _chk("first_track", False, 0, 0.10, "missing")
    r = _ratio(_track_name(ref[0]), _track_name(cnt[0]))
    return _chk("first_track", True, r, 0.10, f"ratio={r:.2f}")


def check_last_timestamp_plausible(target, cand):
    dur = target.get("duration") or cand.get("duration")
    secs = [t.get("seconds") for t in cand.get("tracks", []) if t.get("seconds")]
    if not dur or not secs:
        return _chk("last_ts_plausible", False, 0, 0.05, "missing dur/ts")
    last = max(secs)
    ok = last <= dur * 1.05
    return _chk("last_ts_plausible", True, 1.0 if ok else 0.0, 0.05,
                f"last={last}s dur={dur}s")


def check_monotonic_timestamps(target, cand):
    secs = [t.get("seconds") for t in cand.get("tracks", []) if t.get("seconds") is not None]
    if len(secs) < 3:
        return _chk("monotonic_ts", False, 0, 0.05, "too few ts")
    bad = sum(1 for i in range(1, len(secs)) if secs[i] < secs[i - 1])
    score = 1.0 - bad / len(secs)
    return _chk("monotonic_ts", True, score, 0.05, f"out-of-order={bad}/{len(secs)}")


def check_event_date(target, cand):
    """Shared year / date / venue tokens between the two titles."""
    tt, ct = target.get("title", ""), cand.get("title", "")
    ty = set(re.findall(r"\b(19|20)\d{2}\b", tt))
    cy = set(re.findall(r"\b(19|20)\d{2}\b", ct))
    yrs_t = set(re.findall(r"\b((?:19|20)\d{2})\b", tt))
    yrs_c = set(re.findall(r"\b((?:19|20)\d{2})\b", ct))
    if not yrs_t and not _tokens(tt) & _tokens(ct):
        return _chk("event_date", False, 0, 0.05, "no shared tokens")
    year_hit = bool(yrs_t & yrs_c)
    venue = _tokens(tt) & _tokens(ct)
    score = 1.0 if year_hit else min(1.0, len(venue) / 3.0)
    return _chk("event_date", True, score, 0.05,
                f"years={yrs_t & yrs_c} shared={sorted(venue)[:4]}")


_CHECKS = [check_source_backlink, check_title_similarity, check_artist_match,
           check_duration, check_track_count, check_name_overlap,
           check_first_track, check_last_timestamp_plausible,
           check_monotonic_timestamps, check_event_date]


def _tier(score: float, backlink: bool) -> str:
    if backlink:
        return "definitive"
    if score >= 0.80:
        return "high"
    if score >= 0.62:
        return "medium"
    if score >= 0.45:
        return "low"
    return "reject"


def score_candidate(target: dict, cand: dict) -> dict:
    checks = [fn(target, cand) for fn in _CHECKS]
    backlink = next((c for c in checks if c["name"] == "source_backlink"), None)
    if backlink and backlink["applicable"] and backlink["score"] == 1.0:
        return {"score": 1.0, "tier": "definitive", "checks": checks,
                "reason": "source backlink"}
    applic = [c for c in checks if c["applicable"] and c["name"] != "source_backlink"]
    if not applic:
        return {"score": 0.0, "tier": "reject", "checks": checks,
                "reason": "no comparable signals"}
    wsum = sum(c["weight"] for c in applic)
    score = sum(c["score"] * c["weight"] for c in applic) / wsum if wsum else 0.0
    # hard guards: a wildly wrong duration or near-zero name overlap vetoes
    dur = next((c for c in applic if c["name"] == "duration"), None)
    ov = next((c for c in applic if c["name"] == "name_overlap"), None)
    if dur and dur["score"] == 0.0 and ov and ov["score"] < 0.15:
        score = min(score, 0.40)
    return {"score": round(score, 3), "tier": _tier(score, False),
            "checks": checks, "reason": "weighted"}


def pick_best(target: dict, candidates: list[dict]) -> dict | None:
    """Score every candidate; return the best whose tier != reject (with its
    verdict attached as ['match']). Returns None if all reject."""
    best = None
    for c in candidates:
        v = score_candidate(target, c)
        c = {**c, "match": v}
        if v["tier"] == "definitive":
            return c
        if best is None or v["score"] > best["match"]["score"]:
            best = c
    if best and best["match"]["tier"] != "reject":
        return best
    return None


def cross_verify(target: dict, by_source: dict[str, list]) -> dict:
    """Reward agreement between independent sources. `by_source` maps a source
    name → its parsed tracklist. Returns {confidence, agree, sources, detail}.
    Two sources 'agree' when their track-name overlap is high."""
    names = {s: _real_tracks(tl) for s, tl in by_source.items() if tl}
    srcs = [s for s in names if names[s]]
    agree = 0
    pairs = []
    for i in range(len(srcs)):
        for j in range(i + 1, len(srcs)):
            ov = check_name_overlap({"tracklist": names[srcs[i]]},
                                    {"tracks": names[srcs[j]]})
            if ov["applicable"] and ov["score"] >= 0.5:
                agree += 1
                pairs.append((srcs[i], srcs[j], ov["score"]))
    confidence = min(1.0, 0.5 + 0.25 * agree) if len(srcs) >= 2 else (
        0.5 if srcs else 0.0)
    return {"confidence": round(confidence, 3), "agree": agree,
            "sources": srcs, "detail": pairs}
