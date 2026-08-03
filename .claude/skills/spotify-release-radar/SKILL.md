---
name: spotify-release-radar
description: Work on Ripster's Spotify release radar (new-releases scanning) without getting Spotify rate-limit bans. Use when "release radar bans / 429 / stuck scanning", when changing how releases are crawled/cached, or when comparing to the reference app spotify-release-list.
---

# Spotify release radar — anti-ban design

All logic lives in `ripster/routes/spotify.py` (mirrored to `github_setup/`).

## The golden rule (learned the hard way)
**Bans are a BEHAVIOUR problem, not a token problem.** The reference app
**jakubito/spotify-release-list** (https://github.com/jakubito/spotify-release-list,
client-side React) hits the EXACT same endpoints (`/me/following`,
`/artists/{id}/albums?include_groups=…&limit=50`) with the SAME kind of token
(user OAuth) and never gets banned. The difference:
1. It scans **on demand** (user opens the app / clicks refresh) — no perpetual loop.
2. On **429** it just `sleep(Retry-After + 1)` and **retries the same request**
   (bounded retries; they even *raised* the retry limit in v3.6.3). It NEVER
   freezes itself for hours.

Ripster used to do the opposite and that caused the bans the owner hated:
- a perpetual background crawler every 30 min (sustained bot load), and
- a self-imposed `_sp_banned_until` of up to **6 hours** on a single 429 → "radar
  dead for ~5h" / "ban 17435s left". That long freeze was self-inflicted.

## ⚡ 2026-07-19 — краул переведён на api-partner GraphQL (/v1 у нас МЁРТВ)
**api.spotify.com/v1 перманентно 429-банит наши веб-токены** (любой эндпоинт, даже
artist/albums, даже свежайший blob-bearer) — поэтому радар с 02.07 не сканил. Референс
работает у юзера, т.к. у него свой client_id и браузер. Фикс «раз и навсегда»:
- Краул теперь ходит в **api-partner `queryArtistDiscographyAll`** (hash
  `5e07d323...56599`) — транспорт качалки, НЕ банится: полный пасс 5756 артистов
  = 5756 живых запросов, 0×429, ~70 мин. 1 запрос на артиста (вся дискография
  сразу, до 2 страниц по 50).
- Заголовки: bearer из кипера (`_sp_minted_bearer`) + **client-token**
  (`_sp_client_token()` — файл orpheus/config/spotify-client-token.txt, авто-минт
  clienttoken.spotify.com при стухании >13 дней; протухший client-token = 401 на всё!).
- 401 мид-пасса лечится перечитыванием кипер-файла (bearer живёт ~60 мин, пасс дольше).
- `/me/following` пробуем по-старому; на 429 берём **durable-кэш подписок** (5756
  артистов в spotify_artist_state.json → followed) и краулим дальше — v1-бан больше
  НЕ блокирует краул. Гейт `/api/spotify/releases` пускает и на одном кипер-bearer.
- appears_on через GraphQL не покрыт (discographyAll его не отдаёт) — считаем ок.
- Зеркалится в github_setup/ (запатчено 2026-07-19 одновременно).

## Current design (2026-06-28 rework — on-demand)
Owner's chosen model: **scan only when the user presses the Scan button.**
- `/api/spotify/releases`: `need_scan = bool(force)`. Opening the tab or changing
  the day filter just re-serves the durable per-artist store (`_build_feed`,
  network-free). Never scans autonomously.
- Background crawler is **opt-in**: config `spotify-bg-scan` (default false; toggle
  on the radar tab `rel-bg-scan`). `_ensure_crawler()` returns early unless enabled;
  the loop re-checks the flag each iteration and stops if turned off. When on it is
  paced (`_SP_CRAWL_INTERVAL` 0.8s) + long interval (`_SP_CRAWL_EVERY` 6h) — smart,
  never crawl-to-ban.
- Anti-ban core (always): `_SP_BAN_CAP = 120` (max 2-min cooldown, was 6h).
  `_paced_get` on 429 with short Retry-After (≤ `_SP_RETRY_WAIT_CAP` 90s) sleeps+
  retries the same request (≤3 tries) like the reference; only a long Retry-After
  or exhausted retries surfaces 429 → 120s cooldown + serve store. 401 → 120s cooldown.

## Durable store (the "release-radar site" core — keep this)
- `spotify_artist_state.json`: `artist_id -> {name, releases:[…], ts}`, 400-day
  window. Repeat scans only re-check stale artists (`_SP_ARTIST_REFRESH` 6h), so
  steady-state cost is tiny. The feed is ALWAYS built from this store, so the UI is
  instant and a 429/expired token never blanks it (serves last-known).
- Token preference: sp_dc **web-player** token (`_sp_dc_get_token`, NOT
  rate-limited) first; falls back to user OAuth. There's a durable-token keeper
  (`ripster/spotify_token_keeper.py` + `tools/spotify_pair.py`) — pairing it once
  makes sp_dc auto-refresh so the radar rarely touches the limited dev path.

## ⚡ 2026-07-20 — instant hook (queryWhatsNewFeed) + public-build wiring gotcha

Added `_gql_whatsnew` — Spotify's own personalized "What's New" GraphQL query
(`_SP_GQL_WHATSNEW_HASH`, hash rotates without notice, re-extract from the web-player
JS bundle if it 400s). One request, no per-artist crawl, catches a fresh drop within
`_SP_WHATSNEW_POLL_EVERY` (15 min) instead of waiting for the next full crawl pass.
Feeds a `live: true` flag onto matching releases → ⚡ Live badge in the UI. Runs as a
SEPARATE poller (`_sp_whatsnew_poller`) alongside the existing per-artist crawl (the
crawl stays as the completeness net; the instant hook is a latency shortcut, not a
replacement). When merging results back into `_sp_artist_state`, carry forward
`live=True` for matching ids BEFORE overwriting — a naive overwrite silently wipes the
badge on the next full-crawl pass.

**🔴 The bug that actually shipped (v3.0.30→31):** `_releases.install()` was never
called in `github_setup/app.py` — the whole `/api/spotify/releases` route + nav item
were commented out from a STALE reason ("shared dev token gets banned", true before
the api-partner rewrite above, false after). Meanwhile the README already advertised
"🦝 Ripster Radar" with a screenshot. **The code in `discovery.py`/`spotify.py`/
`releases.py` being correctly mirrored is NOT the same as the feature actually being
reachable in the public build** — always grep `github_setup/app.py` for the route's
`install()` call (and the nav item in `github_setup/static/index.html`) after touching
this area, not just diff the route files themselves.

## Do / Don't
- DON'T reintroduce a default-on perpetual crawler or a multi-hour self-ban.
- DON'T blank the feed on error — always `_build_feed` from the store.
- DO keep scans tied to explicit user action (button) unless `spotify-bg-scan` is on.
- DO handle 429 by waiting Retry-After and retrying, capped — never freeze for hours.
- The Nov-2024 Spotify API deprecations killed related-artists / algorithmic-playlist
  / recommendations access for dev-mode apps — don't design around Release Radar /
  Discover Weekly playlist endpoints; stick to `/me/following` + `/artists/{id}/albums`.

Related memory: [[project_release_radar_ondemand_2026-06]].
