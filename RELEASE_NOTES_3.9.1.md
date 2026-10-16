# Ripster 3.9.1

A follow-up to 3.9.0: the player and panel got a proper level meter and seek bar, stations got a redesign, and a batch of reliability fixes landed.

## Player and panel

- **A level meter that works on any stream.** The spectrum bars now follow whatever is playing, including Qobuz previews, through a safe generic audio proxy. The meter holds between sparse frames and decays smoothly instead of stuttering.
- **A real seek bar.** Wide, service-coloured progress with a cache layer, a playhead, drag-to-seek, and previous/next buttons on the mini player.
- **Readable mini player.** Larger text and a calmer background for the meter.
- **Play button on radar tiles** in the panel.

## Stations

- Station cards are now big "carpet" cards: the whole card plays, with a large play button and a live cover mosaic. Knobs that did nothing were removed and the counts moved into tooltips.

## Releases

- **New setting: Settings → Releases → "Upcoming releases".** It switches on the pre-order and announcement radar (Bandcamp, Beatport and label sources). It is off by default.
- A pre-ordered release that has not come out yet now waits for its release day instead of failing with a 404.

## Downloads and reliability

- Apple: Wrapper-Lite decrypts per sample with the correct content-key form, and the public wrapper uses the lite API.
- A load governor decides how many downloads run at once, so the machine stays quiet under load.
- A finished task settles cleanly, and a watchdog catches hangs in disk I/O.
- Auto-cleanup never deletes files that were not delivered yet.
- The queue shows "Artist — Title".
- Radar cards carry the real release artist, with a "featuring" chip.

## Removed

- The Amazon Music engine and the old AMD engine were removed; the Apple path goes through the wrapper.

## Access control

- A verified owner credential now always wins over a guest cookie, and an expired owner token answers 401 instead of dropping into the guest branch.
- Guest links: a weekly free allowance for new links, and a guest can read the tracklist of their own task but cannot cancel it.
