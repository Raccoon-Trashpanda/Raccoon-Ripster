# Ripster 3.7.1

A follow-up to 3.7.0: the release radar's Apple source now brings DJ mixes instead of ordinary albums, the Telegram bot records *why* a download ended the way it did, and two panels stopped reporting things that were not true.

## Release radar — Apple means mixes now

The Apple source used to duplicate what the watchlist already tracks: ordinary albums. It now looks for DJ mixes and live sets.

- A release is treated as a mix when it says so (`DJ Mix`, `Mixed by`, `Continuous Mix`), when Apple files it under `DJ-Mixes`, or when its running times give it away — one continuous track of 40 minutes or more, or an average track length above 12 minutes across a 40-minute-plus release.
- Words like *Live*, *Sessions* and *Mixtape* no longer qualify on their own. "Live at Wembley" is a concert album, not a mix, so those need the genre or the running time to back them up.
- A mix found through several artists' tracks arrives once, not once per artist. The card now links to the album rather than to one track inside it.

## Telegram bot — outcomes, not just "queued"

The bot wrote a line when a download was queued and a line when it succeeded. Everything else — failures, cancellations, partial deliveries, tasks lost to a restart — left no trace, so guest history showed "unknown" forever.

- Errors, partial deliveries, cancellations and lost tasks are now recorded with a reason, across all nine paths that reach the guest.
- What the guest sees is what gets stored: raw engine output (file paths, tokens, internal flags) never reaches guest-visible history. The owner still gets the unedited text.
- The "N of M tracks are cached" warning is now written in the guest's own language.

## Apple Music

- The bundled downloader binary was two fixes behind the source it ships with — most importantly, it treated Apple's rate limiter as a hard failure instead of waiting it out. It is rebuilt from the current sources.
- A link Apple's catalog doesn't answer for now fails in seconds with "nothing here — check the link and the storefront", instead of "the downloader gave no reason" repeated three times over four minutes.

## Honest panels

- **SoundCloud**: the service panel checked the first account in the config while downloads ran on the Go+ account from the pool, so it kept reporting "Free (128 kbps)" on a Go+ setup. It now describes the account that actually downloads.
- **Incomplete releases**: naming the tracks that did not arrive used to work for Deezer only. Apple Music, Qobuz and Tidal now get the same per-track report, built from the same tracklist the queue tree shows. When a track's availability is genuinely unknown, Ripster offers a retry instead of claiming the track does not exist.

## Upgrading

Install over the top of 3.7.0 — settings, tokens and download history are kept.
