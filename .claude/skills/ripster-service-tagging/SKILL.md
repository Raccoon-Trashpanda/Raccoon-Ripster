---
name: ripster-service-tagging
description: How Ripster fixes music tags after download — full multi-artist ARTIST + co-author COMPOSER credits that the raw engines drop. READ THIS when the owner reports "tags missing co-authors / featured artists / composers / только основной исполнитель", when a release tags with one artist for a collaboration, or before touching the post-download retag path (qobuz_retag.py, apple-retag, tagger.py). Covers the Qobuz album/get-vs-track/get credits trap.
---

# Ripster post-download tagging (artists + co-authors)

The download engines under-tag. streamrip (Qobuz/Tidal/Deezer) writes only the single
`performer.name` → a collaboration lands with ONE artist and NO composer. Ripster fixes this
in a **post-download retag** step, re-derived from the service's own API.

## The Qobuz credits trap (the #1 recurring bug)
**`album/get` returns each track WITHOUT the `performers` credits string — it comes back EMPTY.**
The full credits (featured artists, composers, lyricists, producers) are ONLY returned by
**`track/get?track_id=<id>`**. If the retagger reads credits from the album payload it sees
nothing → falls back to the single main artist → **co-authors and featured artists vanish.**

Verify quickly:
```python
# album/get track → performers == ""   (empty)
# track/get   same → performers == "Sound Quelle, MainArtist - Valery Lebedev, Arranger, Lyricist, Author"
```
So `ripster/engines/qobuz_retag.py` `retag_qobuz_album()` fetches `track/get` PER TRACK
(concurrency-capped) to get real credits, then writes:
- **ARTIST** = names with an artist role (`_ARTIST_ROLES`: mainartist/performer/featuredartist/…)
  → multi-value (FLAC `artist`, MP3 TPE1 v2.4, M4A `©ART`).
- **COMPOSER** = names with a writer role (`_COMPOSER_ROLES`: composer/author/writer/lyricist/…)
  → multi-value (FLAC `composer`, MP3 TCOM, M4A `©wrt`). Additive: never clear an existing
  composer with an empty list.

`parse_performers` / `parse_composers` split the `performers` string on ` - ` (entries) then `,`
(name + roles); a name goes to ARTIST or COMPOSER by which role-set its roles intersect. A
lyricist must never leak into ARTIST — keep the role sets disjoint.

## Wiring (must stay connected)
`runner.py` (~line 1632, after `save_dir` is set): for `service == "qobuz"` it regexes the album
id from the URL and `await retag_qobuz_album(...)`. The import is inside the function, so a code
change to `qobuz_retag.py` needs an **app restart** to take effect (sys.modules caches the old
module). Apple has its own `apple-retag` (placeholder/name fix) in the same area.

## Verifying a fix
Run the retagger standalone against a downloaded folder, then ffprobe the files:
```python
import asyncio, yaml; from ripster.engines import qobuz_retag as QR
cfg = yaml.safe_load(open("config.yaml", encoding="utf-8"))
print(asyncio.run(QR.retag_qobuz_album("<album_id>", cfg, r"<folder>")))
```
```bash
ffprobe -v error -show_entries format_tags -of json "track.flac"   # want ARTIST (multi) + composer
```
A real collab (e.g. Sound Quelle – "With You" feat. Lily Denning) must show
`artist = Sound Quelle;Lily Denning` and `composer = …;Valery Lebedev`.

## Gotchas
- Multi-value FLAC/Vorbis shows as `;`-joined in ffprobe — that's correct multi-value, not a
  literal semicolon in one string.
- Match files to tracks by ISRC first, then (disc, track) position ONLY when the file's album tag
  EXACTLY equals the Qobuz album (a substring match applies the wrong master's credits).
- Owner tagging preference: artists as separate multi-values (not one ", "-joined blob), deduped.
- Other services (Tidal/Deezer) tag via streamrip too — if the owner reports the same "one artist"
  symptom there, replicate this per-track-credits approach for that engine.
