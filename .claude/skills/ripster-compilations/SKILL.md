---
name: ripster-compilations
description: The compilation blind spot every per-artist release scanner in Ripster has — a various-artists / label compilation is credited to "Various Artists", so it is in NO followed artist's discography and the radar reports success having never seen it, while the individual tracks show up fine. MUST READ before building or touching any release scanner, watchlist checker, or "new releases" feed for ANY service (Spotify, Apple, Deezer, Tidal, Qobuz, SoundCloud, Beatport, BBC). Triggers - "сборника нет в радаре", "компиляции не приходят", "треки есть а альбома нет", "release radar misses X", label sampler / soundtrack / "X Years of <label>" not appearing, adding a new service to the radar or watchlist.
---

# Compilations are invisible to per-artist scanners

## The bug, in one sentence

Every release scanner asks *"which albums is this artist the artist of?"* — and a
various-artists compilation answers *nobody*, because its album artist is
"Various Artists" or the label, not any artist you follow.

Reported 2026-07-24: Hospital Records put out **"Hospital Records: Forza Horizon 6"**
(24 tracks, 24 followed artists on it). Every artist's own single showed up in the
radar. The compilation did not — it was in the store **zero** times.

## Why it stays hidden

This class of bug never raises an error. The scanner runs, the artist has releases,
the pass reports success. Nothing is missing *as far as the code can tell* — it
faithfully answered a question that was the wrong question. Treat "feature X never
returns results of type Y" as a **query-shape** bug before looking for a crash.

The same shape appears in [[project_watchlist_suggestions_and_dead_checker_2026-07-24]]
(a filter that dropped every row, so the loop body never ran and the check "passed").
When a scanner can succeed vacuously, assert on the count of what it found.

## The fix, per service

Do not ask what the artist is the *album artist* of. Ask what they **appear on**,
then keep the compilations. `ripster/compilations.py` holds the shared classifier
(`classify()` → `(type, group)`, `is_compilation()`, `merge_releases()`).

| Service | Endpoint that answers "appears on" | Gotchas |
|---|---|---|
| **Spotify** | `queryArtistAppearsOn` via api-partner GraphQL (`_SP_GQL_APPEARS_HASH`) | Items carry **only** id/name/artists/coverArt — no date, no type. Resolve each unseen album id with `getAlbum` (`_SP_GQL_GETALBUM_HASH`) and cache it durably in `album_meta` (an album's date never changes). **`offset`/`limit` are accepted but IGNORED** — every offset returns the same first 50, so do not paginate; it is one request per artist. The shelf is curated per artist and lags: of 24 artists on the Forza comp only some had it. That is fine — one is enough. |
| **Apple** | `lookup?id=<artist>&entity=song` | A *track* always carries `collectionId` / `collectionName` / `collectionArtistName`. `entity=album` returns only the artist's own albums — that is exactly the blind spot. More reliable than Spotify's shelf: all 3 test artists surfaced the comp. |
| **Anything new** | find the "appears on" / "artist's tracks" endpoint | The general rule: **read the release off the track**. A track always knows its parent collection; an artist's album list does not know about compilations. |

Spotify's `queryWhatsNewFeed` does **not** help — verified, it carries zero
compilations (it is "new from artists you follow", and VA is not followed).

## Rules to keep

- **Store a VA compilation as `type/group = "compilation"`, not `"appears_on"`.**
  The UI already requests `album,single,compilation` by default, so it shows with
  no frontend change. `appears_on` is opt-in and would have kept it hidden. Plain
  guest spots (featured on another artist's album) stay `appears_on`.
- **Budget the extra calls.** The shelf is one request per artist, but with
  thousands of followed artists that still doubles a pass and invites a 429 ban.
  Spend a per-pass budget (`spotify-appears-budget`) on the stalest shelves and
  round-robin; break ties toward artists who released something *recently* — they
  are the ones who just landed on a new label comp.
- **Cache album metadata forever, keyed by album id.** Cold cache is the whole
  cost; comps overlap heavily across artists, so it converges fast.
- **Never let the shelf cost the discography.** Any shelf failure returns `[]`;
  the artist's own releases must still be stored.
- **Merge, never replace.** The crawl overwrites `state[aid]["releases"]` — carry
  shelf entries over on passes where the shelf is not due, or every compilation
  found gets silently dropped on the next pass.

## Verifying a fix (do this, don't assume)

1. Pick a real compilation and get its id. Confirm it is genuinely missing:
   count its occurrences in the store — must be 0 before, >0 after.
2. Confirm which followed artists are on it (fetch the tracklist and intersect
   with the followed set) — that tells you which artist's shelf should surface it.
3. Run the appears-on path for those artists and assert the album id comes back
   with `group == "compilation"` and a correct date.
4. Check the feed the UI actually requests (`types=album,single,compilation`)
   contains it — storing it is not the same as showing it.
