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
    "amd-dir", "amd-instance-url", "amd-instance-secure",
    "amd-parallel", "amd-save-lyrics", "amd-lyrics-format",
    "amd-codec-alt",
    "atmos-max", "max-memory",
    "bearer", "media-user",
    "storefront",
    "qobuz-app-id", "qobuz-secrets",
    "qobuz-email", "qobuz-password",
    "qobuz-user-id", "qobuz-auth-token",
    "qobuz-quality", "qobuz-save-path",
    "deezer-arl", "deezer-quality", "deezer-save-path",
    "tidal-token", "tidal-refresh",
    "tidal-user-id", "tidal-country", "tidal-token-expiry",
    "tidal-quality", "tidal-save-path",
    "spotify-client-id", "spotify-client-secret", "spotify-sp-dc",
    "spotify-release-days", "spotify-release-types", "spotify-auto-convert",
    "spotify-default-target", "spotify-engine",
    "spotify-proxy", "spotify-crawl-interval",
    "orpheus-",   # orpheus-quality, orpheus-save-path, orpheus-convert-mp3, …
    "beatport-username", "beatport-password", "beatport-quality", "beatport-save-path",
    "soundcloud-save-path", "soundcloud-oauth-token", "soundcloud-hq",
    "yandex-token", "yandex-quality", "yandex-save-path",
    "amazon-token", "amazon-quality", "amazon-save-path", "amazon-cli-path",
    "releases-services", "releases-days", "releases-types",
    "queue-autostart", "max-parallel",
    "wrapper-apple-id", "wrapper-password", "wrapper-mode", "apple-wrapper",
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
    "ripster-repo",   # ripster-repo + ripster-repo-token (self-update)
)


def config_key_allowed(k: str) -> bool:
    """Return True if *k* is in the frontend-writable whitelist."""
    return any(k == p or k.startswith(p) for p in CONFIG_WRITABLE_PREFIXES)


# ── WebSocket guest filter ─────────────────────────────────────────────────────
# Event types that must never be forwarded to guest WebSocket connections.

GUEST_BLOCKED_WS_TYPES: frozenset[str] = frozenset({
    "history_updated",
    "queue_started", "queue_stopped", "queue_paused", "queue_resumed",
    "releases_scan_progress", "releases_scan_done",
    "orpheus_not_authed",
    "watchlist_new_release", "watchlist_scan_progress",
    "spotify_authed",
    "remote_stopped",
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
    "/api/yandex/auth",     # OAuth device flow — writes the owner's yandex-token to config
    "/api/coder/",          # owner-only local file tool — lists/reads/writes arbitrary owner folders
    "/api/tagger/",         # owner-only local file tool — reads/writes arbitrary owner folders
)
