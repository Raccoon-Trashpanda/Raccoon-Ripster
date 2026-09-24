# Ripster 3.9.0

Two months of work in one build. This page collects everything new since 3.6.5 —
including 3.7.0, 3.7.1 and 3.8.0 — because most of it reached people through
self-update and never had one place to be read.

The theme is the same throughout: where Ripster used to stop with a bare error, it
now finds a way, or tells you exactly why there isn't one.

## When a store says no

- **A refusal no longer ends a download.** If your own accounts can't serve a
  release — regional rights, a key server having a bad hour — Ripster takes the
  same recording from another service you have access to, and the history entry
  names both: "Deezer refused → downloaded from Qobuz".
- **A swap is only allowed if it is the same recording.** The backup search
  matches on ISRC/UPC, so a karaoke cover or an alternate remaster can't land in
  your folder in place of what you asked for. A release with no ISRC/UPC is
  reported as "nothing to match on, not searching blind".
- **A ladder of availability replaced "not available anywhere".** Your own
  storefronts, then the public pool, then another service — each rung is tried in
  order, and the console line says which one delivered.
- **A quality downgrade is now loud.** If only a lower tier could be served, the
  task says "downloaded in reduced quality: X instead of Y" instead of quietly
  handing you the smaller file.

## Apple Music

- **One account, one device fingerprint.** New accounts get their own device-info
  instead of sharing a single one; a key refusal retries a different storefront,
  and an account now lives independently of the container it was added to.
- **Keys are asked for in the release's own storefront**, not the one your link
  happened to point at — releases that used to answer "no keys" now come back.
- **"This account doesn't buy in its own country" is told apart from "no keys".**
  Two different problems, two different messages.
- **Public-wrapper modes.** Six modes decide *when* the shared public wrapper is
  consulted (off / only after mine failed / only for region-locked releases / …),
  with a per-task checkbox in the download dialog and a Settings panel that shows
  the real daily quota, the storefronts on offer and the 24-hour cooldown after
  repeated rate-limit hits. Default: **off**, until you switch it on.

## Pre-orders and releases that are not out yet

- **A pre-order is no longer a failure.** Ripster reads the store's own release
  timestamps, waits, and unlocks the download at midnight in *your account's
  country* plus a small margin — you queue it once and come back to finished
  files.
- **The upcoming radar covers Bandcamp and Beatport pre-orders**, not just the
  streaming services.
- **A partial arrival is stated as partial.** When only part of a release exists
  yet, you get "2 of 5 available so far", not a retry storm against the store.

## Files and folders

- **One folder = one release.** The edition suffix (Deluxe, 2019 Remaster, Live)
  is resolved *before* the download, so two editions of an album stop sharing a
  folder.
- **Multi-disc releases keep their discs straight** on Qobuz, Deezer and Tidal as
  well as Apple.
- **Cleanup only ever touches Ripster's own files** — your own artwork or cue
  sheet in that folder stays.

## Radio, BBC and taste

- **A live recording is checked, not trusted.** Captures are staged, decoded and
  verified before they count as finished; a show that died mid-broadcast is
  recovered from what is on disk, and the recording schedule is restored after an
  app restart.
- **Stations only play the genre they claim**, and umbrella genres are populated
  again (subgenres now count as their parent — Metal and friends work). A dead
  source explains itself instead of returning a silent zero.
- **Stations learn from how you listen** — skips, finished tracks, likes,
  downloads. All of it is computed and stored on your machine, and guests
  listening through your instance never shape it.
- **Same-named artists are told apart**, and what shows up as yours is anchored in
  what you actually downloaded, liked or listened to.

## Player, window, lyrics

- **The player can leave the app**: an external player window, openable from any
  tab, with the launch guarded by a one-time pass instead of failing silently.
- **The window degrades gracefully**: a 1060×600 floor, the quality pill no
  longer overlaps the transport controls, and title/artist keep a readable width.
- **Synced lyrics pick the best source available** and now reach a paired phone.
- **Queue cards start folded** — expand a tracklist only when you want it, so a
  batch of tasks is no longer one endless page.
- **Windows notifications**: one toast per release, rate-limited, and every one of
  them logged.

## Per-service fixes

- **Beatport** — a regional refusal falls over honestly instead of reporting an
  empty catalogue, with the same quality-downgrade guard.
- **Tidal** — an expired session mints a fresh token from your refresh token in
  the downloader, stations and search; one-click device login (link.tidal.com)
  works out of the box.
- **Deezer** — releases listed as "Artist - Album" now resolve, and only the files
  your task asked for are taken.
- **Qobuz** — long albums stop arriving short: the tracklist is fetched with a
  proper page limit.

## Security and privacy

- **Apple account labels and tokens are masked** in every API answer.
- **Sharing your accounts with a phone is an explicit opt-in**, per phone, off by
  default.
- Nothing changed about where your data lives: no Ripster account, no cloud, your
  files and credentials stay on your machine.

## Language

- Errors, history rows and settings panels that used to answer only in Russian now
  follow the interface language — the untranslated backlog on Watchlist and
  History went from roughly 1100 spots to 9.
