# Ripster 3.7.0

## Stations that actually learn

Stations used to be a random draw from a genre. Two things changed.

**They only play the genre they claim.** A track found by searching the genre
word now has to *prove* its genre — before, an unknown genre counted as a pass,
so a station called Techno served "Techno & Tequila", a "Techno Mix" edit and a
toddler compilation. And an artist is only treated as belonging to a genre when
that genre is one of their top tags, not a side tag — which is the difference
between Jeff Mills and a Eurodance act that once got tagged "techno".

**They listen to how you listen.** Every station play now records what actually
happened: seconds played versus track length, skips, finished tracks, likes and
downloads. Skip something in the first seconds and that artist drops; finish or
download it and they rise; a disliked track never comes back; nothing repeats
for a week. All of this is computed and kept **on your machine** — nothing is
uploaded, and guests listening through your instance never shape your taste.

## Radar

- Sources — Spotify releases, BBC, SoundCloud, Apple — are real toggles above
  the feed, and BBC episodes and SoundCloud uploads finally appear in it (they
  were being dropped, so the feed was Spotify-only).
- One continuous grid: a day with a single release no longer leaves the rest of
  the row empty. The date and the current letter follow you as a floating label,
  and releases inside a day are ordered alphabetically with a letter jump bar.
- BBC and SoundCloud entries get their own cards (stream, duration, their own
  download path) instead of a release card with irrelevant controls.

## New in the app

- **JioSaavn** — search, album and artist pages. Audio is region-locked outside
  India; Ripster says so plainly instead of saving a broken file.
- **Skins** — `Neon`, `Console` (dense, sharp) and `OLED Noir`, on top of the
  five colour themes. The default look is unchanged.
- **Queue tree** — expand an album to see every track with its own progress line.
- **BBC tracklists** are verified against the DJ and the broadcast date, so an
  Essential Mix can no longer inherit another DJ's set.
- **Search** opens in the service you chose in Settings, not whichever one
  happened to be first.

## Fixes worth naming

- **Deezer**: a region-locked track is now reported as region-locked instead of
  "try a lower quality", and the pool tries an account from another country.
- **Coder**: CUE splitting is sample-exact (no duplicated audio at the seams);
  24→16 bit conversion is dithered instead of truncated.
- **Beatport**: a network timeout is no longer reported as a wrong password, and
  a rotated refresh token is written back instead of silently invalidating the
  session.
- **The whole app used to freeze** for seconds after each download while the
  result was analysed on the main loop — that work moved off it.
- Multiple accounts per service can be dragged into order, and the account with
  the better plan is preferred when you haven't set an order yourself.
- Covers, spectrograms, artwork lookups and dozens of log messages that used to
  show internal keys now show real text.
