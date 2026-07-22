"""
Security policy — all hard-coded access restrictions in one place.

Importing this module is safe at any point; it has no side effects.
"""
from __future__ import annotations

# ── Config write whitelist ─────────────────────────────────────────────────────
# Keys (or prefixes) that the frontend is allowed to mutate via POST /api/config.
# Anything not matched here is silently dropped.  This prevents RCE via path
# injection or overwriting internal-only keys.

CONFIG_WRITABLE_PREFIXES: tuple[str, ...] = (
    "quality", "engine", "language", "font", "theme",
    "embed-", "save-path", "cover-", "lrc-", "lyric",
    "truncate",
    "gamdl-cookies-path", "gamdl-use-wrapper", "gamdl-wrapper-account-url", "gamdl-download-mode",
    "gamdl-song-codec", "gamdl-overwrite", "gamdl-save-playlist",
    "gamdl-synced-lyrics-format", "gamdl-no-synced-lyrics",
    "gamdl-lyrics-only", "gamdl-use-album-date",
    "gamdl-fetch-extra-tags", "gamdl-artist-auto-select",
    "gamdl-mv-remux-mode", "gamdl-mv-resolution",
    "gamdl-cover-size", "gamdl-cover-format",
    "gamdl-album-template", "gamdl-file-template",
    "gamdl-exclude-tags", "gamdl-truncate",
    "gamdl-nm3u8dlre-path", "gamdl-ffmpeg-path",   # found unsaveable in the 2026-07-22 settings audit
    "authorization-token",   # Apple bearer — found unsaveable via REST in the 2026-07-22 audit
                              # (only ever landed via the WS token_update path in practice)
    "amd-dir", "amd-instance-url", "amd-instance-secure",
    "amd-parallel", "amd-save-lyrics", "amd-lyrics-format",
    "amd-codec-alt",
    "apple-parallel",   # apple-parallel-tracks / apple-parallel-count (zhaarey)
    "atmos-max", "max-memory",
    "bearer", "media-user",
    "storefront",
    "qobuz-app-id", "qobuz-secrets",
    "qobuz-email", "qobuz-password",
    "qobuz-user-id", "qobuz-auth-token",
    "qobuz-quality", "qobuz-save-path",
    "qobuz-max-connections", "qobuz-requests-per-minute",
    "qobuz-accounts",   # multi-account Qobuz pool (load-balanced) — list of {user_id/auth_token or email/password, label}
    "deezer-arl", "deezer-quality", "deezer-save-path", "deezer-arl-label",
    "deezer-accounts",   # multi-account Deezer pool (load-balanced) — list of {arl,label}
    "tidal-token", "tidal-refresh",
    "tidal-user-id", "tidal-country", "tidal-token-expiry",
    "tidal-quality", "tidal-save-path",
    "spotify-client-id", "spotify-client-secret", "spotify-sp-dc",
    "spotify-release-days", "spotify-release-types", "spotify-auto-convert",
    "spotify-default-target", "spotify-engine",
    "spotify-proxy", "spotify-crawl-interval", "spotify-bg-scan",
    "spotify-totp",   # spotify-totp-secret / -ver (owner drops in a fresh TOTP when Spotify rotates)
    "orpheus-",   # orpheus-quality, orpheus-save-path, orpheus-convert-mp3, …
    "beatport-username", "beatport-password", "beatport-quality", "beatport-save-path",
    "soundcloud-save-path", "soundcloud-oauth-token", "soundcloud-hq",
    "sc-isrc-fallback", "sc-widevine-wrapper-url",   # found unsaveable in the 2026-07-22 settings audit
    "yandex-token", "yandex-quality", "yandex-save-path",
    "amazon-token", "amazon-quality", "amazon-save-path", "amazon-cli-path",
    "releases-services", "releases-days", "releases-types",
    "queue-autostart", "max-parallel",
    "minimize-to-tray",
    "minimize-to",      # where a plain minimize goes: taskbar (default) / tray
    "wrapper-apple-id", "wrapper-password", "wrapper-mode", "apple-wrapper",
    "wrapper-accounts",   # multi-account Apple wrapper pool — list of {id,password,label}
    "apple-pool", "apple-pool-size",
    "decrypt-port", "m3u8-port",
    "url-quality", "album-folder", "single-disc",
    "show-", "auto-", "_last_svc",
    "use-go-run",
    "transcode-",
    "coder-",
    "discogs-token",
    "tunnel-subdomain", "tunnel-provider", "tl1001-",
    "file-rename-template",
    "remote-enabled", "public-url", "ngrok-",
    "service-colors",
    "player-",
    "notify-",        # notify-on-done (native desktop toast on download finish)
    # NOTE: "ripster-repo" (the update source slug) is deliberately NOT here —
    # writing it via the API lets anyone who can reach /api/config point
    # self-update at an arbitrary repo, which auto-updates a running install
    # to run whatever code is there (RCE). It's only settable by editing
    # config.yaml directly (filesystem access = already a higher trust level).
    # The auth token for a private fork is still API-writable — it doesn't
    # change WHERE updates come from, only whether a fixed repo is reachable.
    "ripster-repo-token",
    "telemetry-",     # diagnostics forwarding (telemetry-forward/url/level/token/...)
)


def config_key_allowed(k: str) -> bool:
    """Return True if *k* is in the frontend-writable whitelist."""
    return any(k == p or k.startswith(p) for p in CONFIG_WRITABLE_PREFIXES)


# ── WebSocket guest filter ─────────────────────────────────────────────────────
# Event types that must never be forwarded to guest WebSocket connections.

# The WS fan-out (app.py broadcast) is DENY-BY-DEFAULT for guests (security
# audit 2026-07-21): queue_update / log / progress / dl_counter /
# sc_fallback_added / bbc_dl_* are scoped to the guest's own session/tasks,
# guest_link_revoked has its own handling, queue_done/ping carry no payload
# and are always safe, and anything NOT explicitly handled is now dropped —
# a NEW owner-sensitive event type is safe by default instead of leaking
# until someone remembers to add it here. This blocklist still exists as a
# belt-and-suspenders early-exit for the known-sensitive types (checked
# before the per-type branches), not because the fan-out depends on it.
# (This mirror doesn't ship ripster/routes/guest.py — no guest session can
# ever exist here — but the shared app.py/security.py logic stays aligned
# with the owner build for maintainability.)
GUEST_BLOCKED_WS_TYPES: frozenset[str] = frozenset({
    "history_updated",
    "queue_started", "queue_stopped", "queue_paused", "queue_resumed",
    "releases_scan_start", "releases_scan_progress", "releases_scan_done",
    "watchlist_new_release", "watchlist_scan_progress",
    "watchlist_check_start", "watchlist_check_progress", "watchlist_check_done",
    "spotify_authed", "spotify_sp_dc_updated",
    "orpheus_authed", "orpheus_not_authed",
    "apple_authed", "bearer_updated",
    "engine_changed", "restart_required",
    "remote_stopped", "tunnel_status",
    # Apple wrapper infra — Docker logs / fixed ports / owner Apple-session state.
    "wrapper_built", "wrapper_log", "wrapper_login_failed",
    "wrapper_started", "wrapper_status", "pool_update", "amd_ready",
    # Setup tab — install logs can leak filesystem paths / tool versions.
    "install_log", "install_step", "setup_done", "tools_status",
    "gamdl_deps_fixed", "soundcloud_installed", "widevine_minted",
    # Coder is an owner-only local-file tool (reveals owner folders/paths).
    "coder_progress", "coder_done", "coder_cancelled",
})


# ── HTTP guest route guard ─────────────────────────────────────────────────────
# Path prefixes that guests are never allowed to call, regardless of auth.
# Checked by the guest-guard middleware in app.py.

GUEST_BLOCKED_PATHS: tuple[str, ...] = (
    "/api/config",          # no config reads/writes
    "/api/engine",          # no engine switching
    "/api/settings",
    "/api/setup/",
    "/api/wrapper/",
    "/api/watchlist/",
    "/api/releases/",
    "/api/apple-auth/",
    "/api/fetch-bearer",
    "/api/upload-cookies",
    "/api/admin/",
    "/api/stats",           # global stats reveal owner info
    "/api/import-token/",   # writes tokens to config.yaml
    "/api/test-auth/",      # leaks subscription/email info
    "/api/release/smart-resolve",  # acquisition helper — uses owner Qobuz/Tidal tokens
    "/api/isrc",            # ISRC resolve/upgrade — uses owner tokens (download helper)
    "/api/soundcloud/tracklist-1001",  # scrapes 1001tracklists on the owner's login/IP
    "/api/soundcloud/upload-wvd",   # installs/overwrites the owner's Widevine CDM (device.wvd)
    "/api/soundcloud/login",        # writes the owner's SoundCloud OAuth token
    "/api/spectrogram/",            # owner-only tool — heavy ffmpeg analysis; nav is
                                    # owner-only but had no backend guard (DoS surface)
    "/api/yandex/auth",     # OAuth device flow — writes the owner's yandex-token to config
    "/api/coder/",          # owner-only local file tool — lists/reads/writes arbitrary owner folders
    "/api/tagger/",         # owner-only local file tool — reads/writes arbitrary owner folders
    "/api/library/",        # owner-only — scan/cover/file expose & stream the owner's
                            # entire local music collection under the save-path roots.
                            # library.py is documented owner-only but had no enforcement
                            # of its own (relies on this allowlist).
    "/api/history",         # owner's GLOBAL download log (all users' titles/timestamps).
                            # Guests have their own scoped view at /api/guest/history and
                            # never need this; leaving GET open leaked the owner's log.
                            # Prefix also covers DELETE /api/history{,/<id>} (the app.py
                            # DELETE special-case is now defence-in-depth). NOTE: matched
                            # as a prefix — /api/guest/history does NOT start with this.
)
