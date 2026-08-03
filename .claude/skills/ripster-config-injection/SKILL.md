---
name: ripster-config-injection
description: How Ripster config/token writes actually work — the whitelist, the tokens/*.yaml precedence + sync-back, live in-memory mutation, the owner bot /set /get commands for injecting accounts/tokens from Telegram, settings export/import, and the real owner-vs-guest auth gate. Use when adding a new settable config key, wiring a token/account entry point (UI, bot, API), debugging "I set the token but it didn't take effect" / "key blocked / not whitelisted" / "is this route auth-protected", or touching export/backup of settings.
---

# Ripster config & token injection

## The write path (POST /api/config)
`ripster/routes/core.py:post_config` does, in order:
1. **Whitelist filter** — only keys matching `ripster/security.py CONFIG_WRITABLE_PREFIXES`
   survive; the rest are dropped and returned in `blocked`. (Prevents RCE via path injection.)
2. Drops `_SECRET_KEYS` values that are the redaction placeholder `••…`.
3. **`_cfg.update(safe)`** — mutates the LIVE in-memory config dict → engines read it
   immediately, **no restart**.
4. `save_config(_cfg)` — persists.

Response: `{"ok": true, "blocked": [dropped keys]}`.

## 🔑 tokens/*.yaml precedence + sync-back (the subtle part)
`config_service.load_config`: `merged.update(config.yaml)` then
`merged.update(_load_token_files(tokens_dir))` → **tokens/*.yaml OVERRIDE config.yaml at load**.
There are token files for apple/deezer/qobuz/tidal/soundcloud/tl1001.

`config_service.save_config`: for each `tokens/*.yaml`, any key **already present there** is
**re-written from the new config value**. So a POST /api/config that changes `qobuz-auth-token`
updates config.yaml AND tokens/qobuz.yaml AND the live dict → no stale-override problem.
⚠ But a BRAND-NEW key you only put in config.yaml is fine; you never need to hand-edit token
files. Never hand-edit a token file and ALSO expect config.yaml to win — token file wins at load.

## Adding a new settable key
Add its prefix to `CONFIG_WRITABLE_PREFIXES` in `ripster/security.py` (mirror to
`github_setup/ripster/security.py`). If it is a secret shown to guests, also add it to the
guest redaction set (`_SECRET_KEYS` / core redaction) so it never leaks. Example gap seen:
`apple-pool` was NOT whitelisted → couldn't be toggled from UI/bot, only from the file.

## Owner bot injection: /set and /get (tgbot/bot.py)
Owner-only (`users.is_admin`). Backed by `tgbot/backend.py set_config_detailed()` → POST /api/config
with the same forged owner cookie the bot already uses (`backend.cookie()` from `session-secret`).
- `/set qobuz-auth-token XXX` — one key.
- `/set qobuz <user-id> <auth-token>` — ordered bundle (`_SET_BUNDLES`: qobuz, beatport, tidal).
- `/set` + multi-line `key: value` block.
- Short aliases (`_SET_ALIASES`): deezer→deezer-arl, tidal→tidal-token, spotify→spotify-sp-dc,
  sc→soundcloud-oauth-token, yandex, amazon, mut→media-user-token.
- Reply redacts secret values (`…last4 (N chars)`); shows applied vs blocked keys.
- `/get <key>` reads back (redacted), with near-match hints.
- **Whitelist already covers all service tokens** (qobuz-*/deezer-arl/tidal-*/spotify-sp-dc/
  soundcloud-oauth-token/yandex-token/amazon-token/beatport-*/media-user/wrapper-*).
- The bot is a SEPARATE process (`C:\Python314\python.exe bot.py` in `tgbot/`, owner_id <owner_id>,
  local Bot API 127.0.0.1:8081). New handlers need a **bot restart** to load. Bot is NOT mirrored
  to github_setup (rule: guest/bot code stays private).

## Send an owner-only message (report/notify) without the bot process
Read `tgbot/config.json` → `bot_token` + `owner_id`; POST to the LOCAL api
`http://127.0.0.1:8081/bot<token>/sendMessage` `{chat_id: owner_id, text, parse_mode:"HTML"}`.
(The token is bound to the local Bot API server.) Never print the token to chat.

**Read it with `encoding="utf-8-sig"`, and never rewrite it from PowerShell.** On this box
`>`/`Out-File`/`Set-Content` write UTF-8-**with-BOM**; `json.loads` then fails on character 0.
Every reader of this file wraps the call in a bare `except`, so a BOM does not raise anything
visible — it just silently switches off the owner report, the Bot-API-wedged check and
telemetry delivery at once (2026-08-03). `check_config_bom()` in the healthcheck now strips it
on every sweep. Write configs from Python (`write_text(..., encoding="utf-8")` adds no BOM).

## Settings export/import (added 2026-07-23) — a THIRD, stricter exclusion tier
`GET /api/config/export` (`ripster/routes/core.py`) — downloadable backup of every
non-credential setting. Reuses `POST /api/config` for import (no separate import
route — the whitelist already gates what can be written, no new server-side check
needed). Frontend: `exportSettings()`/`importSettings()` in `service_colors_ui.js`,
UI block in `stab-global-shared` (settings.html).

**Why this is NOT the same list as `_SECRET_KEYS`/guest redaction**: `_redact_config`
(used by `GET /api/config` for the UI) only redacts flat `_SECRET_KEYS` — it does
**not** know that `deezer-accounts`/`qobuz-accounts`/`soundcloud-accounts`/
`yandex-accounts`/`wrapper-accounts` are LISTS of dicts each embedding a real
credential (arl/password/token per entry). `_redact_config` leaves those lists
**fully unredacted** (pre-existing gap, not fixed by this session — GET /api/config
itself is owner-only per the auth layer below, so it's not currently exploitable,
but don't copy `_redact_config`'s exclusion set for anything export/backup-shaped).
`_EXPORT_EXCLUDE_KEYS` in `core.py` is `_SECRET_KEYS` ∪ the 5 account-list keys ∪ a
few identity fields (user-id/email/app-id/username) that aren't secrets but are
useless-without-the-paired-credential PII. **If you add a new multi-account pool
for a service, add its `*-accounts` key to `_EXPORT_ACCOUNT_LIST_KEYS` too** —
`CONFIG_WRITABLE_PREFIXES` won't remind you, only manual review will.

## The REAL owner-vs-guest auth gate (easy to miss — it's not where you'd grep first)
`app.py` has two `@app.middleware("http")` decorators (cache headers, guest path
block) that LOOK like the whole auth story but aren't. The actual deny-by-default
gate is registered from **`ripster/auth.py`** (a separate `install`-style
middleware, not visible from an `app.py`-only grep for `@app.middleware`):
for any `/api/*` or `/ws` path not in `_PUBLIC_PATHS`, it requires EITHER a valid
owner `ripster-session` cookie (`verify_session_cookie`) OR a guest session that's
explicitly allowlisted for that path (`_guest_allowed`) — otherwise `401
{"error":"unauthorized"}` (owner-gated) or `403 {"error":"forbidden"}` (guest,
wrong path). A new `/api/...` route added anywhere automatically inherits this —
you don't need to wire auth per-route. Verify with a plain `curl` (no cookie) →
expect 401, not assume from reading `app.py` alone (this took real digging to
find during the 2026-07-23 export-feature security check — don't re-derive it,
`grep -n "unauthorized" ripster/*.py` finds it in one shot).
