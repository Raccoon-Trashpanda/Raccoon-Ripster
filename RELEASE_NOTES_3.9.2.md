# Ripster 3.9.2

## Setup tab — installs that actually work
- **Secure downloads everywhere.** Every executable or archive Setup downloads (Bento4, FFmpeg, Node.js, Java, Android command-line tools) now goes through one verified-TLS context. The old "retry without certificate checks" fallback is removed; if the certificate cannot be verified, nothing is downloaded and you get a clear message instead. Where the publisher provides a checksum, the SHA-256 is verified before unpacking.
- **Fixes the "certificate has expired" errors seen on some Windows PCs**: the Windows trust store on those machines lacks a current root certificate; Ripster now uses its bundled, up-to-date CA bundle.
- **Apple (zhaarey) status is honest**: it turns green only when the downloader source (pinned revision), MP4Box and mp4decrypt are really present. Installs clone the pinned revision from the registry instead of "latest".
- **Go** is selected from the official release list with retries; the Android command-line tools use a single source of truth (URL and SHA-256).

## Other fixes
- Qobuz / Tidal: track versions are shown in titles (for example "Sky Falls Down (Extended Mix)") instead of several identical rows.
- BBC downloads work again (a nested command list broke the start).
- Discovery radio no longer says "ran dry" for niche artists that do have similar artists.
