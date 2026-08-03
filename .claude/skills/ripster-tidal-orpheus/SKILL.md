# Tidal (OrpheusDL) — session-invalid diagnosis

Tidal downloads go through the vendored `orpheus/` OrpheusDL tree via `ripster/engines/tidal.py`.
Auth is a single **TV-app Quick Login** session persisted in `orpheus/settings/tidal/loginstorage.bin`
(pickled per-session-type storage). There is currently **no multi-account pool** for Tidal (unlike
Apple's `wrapper_pool.py`) — one login session for the whole app.

## 🔴 Root cause found 2026-07-22: misleading "session invalid" masked a config bug

**Symptom:** every real Tidal download failed with `✗ Tidal: сессия недействительна —
переавторизуйся в Settings → Tidal`, even immediately after a fresh Quick Login with a
demonstrably valid, non-expired TV session (`_read_tv_session()` showed a future `expires`).
Re-logging in did NOT fix it — same error on the very next download.

**Why:** `orpheus/modules/tidal/interface.py` validates/refreshes **every** session type in
`self.available_sessions` on module init — by default `[TV, MOBILE_DEFAULT, MOBILE_ATMOS]`
(only collapses to `[TV]` if `modules.tidal.enable_mobile` is `False` in
`orpheus/config/settings.json`). Our Quick Login flow only ever populates the **TV** session.
Tidal refresh tokens are apparently **client_id-scoped**, so reusing the TV session's
refresh_token to validate the Mobile-Default/Mobile-Atmos client types fails —
`TidalSession.get_subscription()` (tidal_api.py:307) throws, and that exception aborts the
**entire** module init, even though the TV session itself is perfectly valid. The generic
error path then reports a misleading "invalid session" for a problem that has nothing to do
with the actual TV credentials.

**Fix (applied):** set `modules.tidal.enable_mobile: false` in `orpheus/config/settings.json`.
This is read fresh per-subprocess (each download spawns a new `orpheus.py` invocation) — **no
app.py or bot restart needed**, unlike `config.yaml` keys that are cached in-process.

```python
import json
p = r"C:\dev\apple_music\orpheus\config\settings.json"
s = json.load(open(p, encoding="utf-8"))
s["modules"]["tidal"]["enable_mobile"] = False
json.dump(s, open(p, "w", encoding="utf-8"), indent=4, ensure_ascii=False)
```

**Verified 2026-07-22:** live download of a 19-track album (`who are you really? [deluxe]`,
Le Youth, `listen.tidal.com/album/543480884`) → 19/19 tracks, `partial:false`, `missing:0`,
`error:""` in history.json. Full FLAC files confirmed on disk.

**Known tradeoff, not yet addressed:** `enable_mobile=False` disables the Mobile-Atmos session
type entirely. If/when real Dolby Atmos (AC-4) Tidal downloads are needed, this will need
revisiting — likely by catching the per-session-type refresh failure in `interface.py` instead
of validating all types unconditionally, rather than reverting the flag.

## Secondary bug fixed same session: misleading error message

`ripster/engines/tidal.py`'s `_RE_AUTH_FAIL` regex didn't match `TidalAuthError` /
`token has expired` text, so genuine auth failures fell through to a **generic "DASH/network
interrupted" message** that sent users toward useless VPN/proxy-switching retries instead of
the real fix (Settings → Tidal reauth). Fixed by extending the regex to also match
`TidalAuthError|token has expired|token.*expired`.

## Country code confusion (NZ vs DE)

`config.yaml`'s `tidal-country` can go stale relative to the actual session's real
`country_code` (read via `_read_tv_session()`) if you log in from a different region later.
Sync it manually via `POST /api/config` if downloads/search geo-behavior looks off — there's
no auto-sync between the TV session's country and this config key yet.

## ✅ FIXED 2026-07-22 late night: re-queuing an already-complete album no longer creates duplicates

Went with option (b) from below — `ripster/runner.py` now has
`_find_completed_duplicate()` / `_reuse_completed_download()`, called for
`engine_name in ("tidal", "orpheus_beatport")` right before the engine would
be invoked. It checks `history.json` for a prior COMPLETE download of the
exact same url+quality+engine, confirms the files are STILL on disk (a
stale/moved-folder history row falls through to a normal fresh download
instead of silently "succeeding" with nothing there), and on a match
completes the task instantly by reusing the existing files — a fresh
manifest entry is recorded under the NEW task id, but the download
subprocess is never started at all, so duplication is structurally
impossible rather than just made less likely. Verified live: re-queuing the
19-track album from the incident below completed in ~1s with the file count
unchanged (19 before, 19 after); a genuinely new track still downloads
through the real engine path with no behavior change. Left OrpheusDL's own
vendored internals completely untouched, and did NOT reintroduce the
orpheus_spotify-style "just skip renaming" workaround (the owner explicitly
wants filenames matching tags for every service — see the incident writeup
below for why that shortcut doesn't work here).

## 🔴🔴 Real bug found 2026-07-22 (root-cause writeup, now fixed above): re-queuing an already-complete album creates duplicate files

**Symptom:** re-queuing a Tidal album URL that's already fully downloaded does NOT skip the
existing tracks — OrpheusDL re-downloads the whole album fresh, and the new files land
ALONGSIDE the old ones under a DIFFERENT name (`Artist, Artist2 - Title.flac` next to the
original `Title.flac`), because Ripster's post-download rename step (`_apply_rename` →
`rename_from_tags`, `ripster/runner.py`) renames files to the `{artist} - {title}` template
after every successful run. Confirmed live: deleting one track and re-queuing the album to
"resume" it re-downloaded ALL 19 tracks, and — because the internal auto-retry loop ran
TWICE within that single task — produced `_2`-suffixed triples per track (3 copies of some
files). Root cause NOT fully pinned down (spent real effort tracing `music_downloader.py`'s
`check_location`/`os.path.isfile` skip-check and never found exactly why it fails to
recognize the pre-existing file on a second run for Tidal specifically — the exact trigger
condition differed between a clean first run and a retry run in a way not yet understood).

**A near-identical bug is already documented AND FIXED for `orpheus_spotify`** —
`runner.py`'s `_apply_rename` call site has a comment explaining exactly this failure mode
and excludes `orpheus_spotify` from renaming for exactly this reason. Tidal (and
`orpheus_beatport`, same OrpheusDL-based mechanism) were NOT given the same guard.

**Deliberately NOT fixed by disabling rename for tidal** — the owner explicitly wants
filenames to match tags for every service (that's the whole point of `_apply_rename`
existing), so blindly excluding tidal from the rename step (mirroring the spotify
workaround) would remove wanted behavior, not just fix the duplicate-file bug. The real fix
needs to either (a) make OrpheusDL's own `track_filename_format`/`single_full_path_format`
already match Ripster's `_RENAME_TEMPLATE` output so the post-hoc rename becomes a no-op
(but `track_tags['artist']` in OrpheusDL is only the PRIMARY artist, not the full
comma-joined collaborator string Ripster's tagger produces — a mismatch that would need
resolving too), or (b) have Ripster itself check "is this track already fully downloaded"
BEFORE invoking OrpheusDL at all, rather than relying on OrpheusDL's own internal check.
Neither was attempted — don't blind-patch vendored OrpheusDL internals without being sure of
the trigger; a rushed fix here risks making the duplication worse, not better.

**Manual cleanup runbook if you hit this again**: for each track number with 2+ flac files,
keep the newest (by mtime), read its tags via mutagen, rename it to
`f"{track_number:02d}. {title}.flac"` (sanitize `<>:"/\|?*`), delete the others. Don't forget
the `.jpg`/`.lrc` sidecars are NEVER renamed by `_apply_rename` (`_AUDIO_EXTS` doesn't
include them) — they stay under OrpheusDL's own plain naming regardless, so after a clean
rename pass they should already match; only check them if something looks orphaned.

**Related, separate bug also found and fixed 2026-07-22: redundant per-track external cover.**
`ripster/engines/tidal.py`'s `_update_orpheus_settings()` hardcoded `covers["save_external"] =
True` on every run — OrpheusDL then wrote a full-resolution (1400px) `.jpg` NEXT TO EVERY
TRACK, identical content each time (~16.8MB × N tracks — ~230MB of pure waste on a 14-track
album). This is separate from the embedded in-file cover (`embed_cover`, pinned to 1000px)
and separate from the one legitimate album-root `cover.jpg` (written unconditionally by
OrpheusDL's `_download_album_files`, unaffected by `save_external`). Fixed: flipped
`save_external` to `False`. Verified clean on two fresh single-track test downloads
post-fix — only the embedded cover + one folder `cover.jpg` remain, no per-track duplicate.
**Note**: editing `ripster/engines/tidal.py` requires an **app.py restart** to take effect —
unlike `orpheus/config/settings.json` (read fresh per-download-subprocess), this file is a
Python module imported once into the long-running app.py process; the FIRST verification
attempt silently used the old in-memory code and appeared to fail until the restart.

## Diagnosis checklist for a future "Tidal won't download" report

1. Check `_read_tv_session()` output — is the TV session's `expires` actually in the future?
   If yes, the session itself is fine — look at `enable_mobile` next, don't just re-login.
2. Confirm `orpheus/config/settings.json` still has `modules.tidal.enable_mobile: false`
   (nothing in the app currently guards against this flipping back — if a future OrpheusDL
   update or manual edit resets it, the same masked failure mode returns).
3. Grep `logs/console.log` for `TidalAuthError` / `token has expired` around the failure —
   if `_RE_AUTH_FAIL` in `ripster/engines/tidal.py` is matching, the UI will show the correct
   "reauth in Settings" message instead of the misleading network one.
4. **Never re-queue an already-fully-downloaded Tidal album "just to test" without expecting
   a full re-download + possible duplicate files** (see the unfixed retry-duplicate bug
   above) — if you need to verify a fix live, use a single FRESH track/album that's never
   been downloaded before, not a resume/retry of existing content.
