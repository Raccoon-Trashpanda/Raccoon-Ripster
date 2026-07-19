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
    "apple-parallel",   # apple-parallel-tracks / apple-parallel-count (zhaarey)
    "atmos-max", "max-memory",
    "bearer", "media-user",
    "storefront",
    "qobuz-app-id", "qobuz-secrets",
    "qobuz-email", "qobuz-password",
    "qobuz-user-id", "qobuz-auth-token",
    "qobuz-quality", "qobuz-save-path",
    "qobuz-max-connections", "qobuz-requests-per-minute",
    "deezer-arl", "deezer-quality", "deezer-save-path",
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
    "yandex-token", "yandex-quality", "yandex-save-path",
    "amazon-token", "amazon-quality", "amazon-save-path", "amazon-cli-path",
    "releases-services", "releases-days", "releases-types",
    "queue-autostart", "max-parallel",
    "minimize-to-tray",
    "minimize-to",      # where a plain minimize goes: taskbar (default) / tray
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
    "notify-",        # notify-on-done (native desktop toast on download finish)
    "ripster-repo",   # ripster-repo + ripster-repo-token (self-update)
    "telemetry-",     # diagnostics forwarding (telemetry-forward/url/level/token/...)
)


def config_key_allowed(k: str) -> bool:
    """Return True if *k* is in the frontend-writable whitelist."""
    return any(k == p or k.startswith(p) for p in CONFIG_WRITABLE_PREFIXES)


# ── WebSocket guest filter ─────────────────────────────────────────────────────
# Event types that must never be forwarded to guest WebSocket connections.

# ⚠️ The WS fan-out (app.py broadcast) is ALLOW-BY-DEFAULT for guests: queue_update
# / log / progress / guest_link_revoked are special-cased, and EVERYTHING ELSE is
# forwarded to guests verbatim via the final `else`. So every owner-sensitive
# event type MUST be listed here or it leaks. The robust fix is to flip that
# fan-out to a deny-by-default ALLOWLIST (guests need only their own queue/log/
# progress + public search-enrichment meta + their BBC/SC progress) — tracked as a
# follow-up; flipping blind risks silently breaking guest live-updates, so until
# then this blocklist is kept exhaustive. (vuln-sweep pass 3 expanded it.)
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
    "gamdl_deps_fixed", "soundcloud_installed",
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
