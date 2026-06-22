# Ripster

A self-hosted web UI for downloading music from **Apple Music, Qobuz, Deezer,
Tidal, Beatport, SoundCloud and Yandex Music**, plus **Spotify** link conversion
(it finds the same release on a service you have access to). Runs locally and
opens in its own window — no account with *us*, no cloud, your files stay on your
machine.

> You need your own valid subscriptions / credentials for each service you use.
> Ripster automates downloading from services **you already pay for** — it does
> not provide accounts or bypass paid tiers.

---

## Features

- 🎚 **Multiple engines & qualities** — ALAC / AAC / Dolby Atmos for Apple Music,
  FLAC up to 24/192 for Qobuz/Deezer/Tidal, lossless SoundCloud, and more.
- 🔁 **Queue** with parallel downloads, auto-retry, and partial-download recovery.
- 🔍 **Search** across services, **New Releases** feed, and **history**.
- 🎛 **DJ Coder** — stitch a multi-track release into one gapless mix + CUE.
- 📊 **Spectrogram** analysis to verify real audio quality.
- 🎧 Built-in **gapless player** with visualizer.
- 🌍 **5-language UI** (English, Russian, Hindi, Japanese, Chinese).
- 🖥 Opens in a **native desktop window** (via the system WebView) — falls back
  to your browser if unavailable.

---

## Requirements

- **Python 3.12** (3.11+ should work)
- **ffmpeg** on your `PATH` (for tagging, transcoding, and mixes)
- Windows, macOS, or Linux
- Valid credentials for the services you want to use

---

## Quick start

### Windows
```bat
git clone https://github.com/<you>/ripster.git
cd ripster
run.bat
```
`run.bat` creates a virtual environment, installs dependencies, copies
`config.example.yaml` → `config.yaml` on first run, and launches the app.

### macOS / Linux (or manual on Windows)
```bash
git clone https://github.com/<you>/ripster.git
cd ripster
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp config.example.yaml config.yaml
python app.py                       # or: python ripster_launcher.py
```

Then open **http://127.0.0.1:7799**

> ⚠️ Always use `127.0.0.1`, **never** `localhost` — Spotify OAuth has rejected
> `localhost` redirects since April 2025.

---

## Configuration

Most settings are managed from the **Settings** tab in the UI and saved back to
`config.yaml` automatically. To pre-seed credentials, copy
`config.example.yaml` → `config.yaml` and fill in the services you use. Leave a
service blank to skip it.

A few service notes:

| Service | What you need |
|---------|---------------|
| **Apple Music** | `media-user-token` + `authorization-token` (DevTools or the "Login via Apple" button). ALAC/Atmos go through a public decryption wrapper — no Apple ID required. |
| **Qobuz** | auth token (or email + password). |
| **Deezer** | `arl` cookie from deezer.com. |
| **Tidal** | Use the device-flow login button in Settings (auto-refreshing). |
| **Spotify** | Client ID + secret from developer.spotify.com (conversion/metadata). |
| **SoundCloud** | OAuth token from your browser session. |

---

## Notes & gotchas

- **streamrip is pinned to `2.0.5`** (Qobuz/Tidal engines) — do not bump to 2.1.x,
  it has login/Tidal regressions. See `requirements.txt` for the why.
- **protobuf is pinned to `6.33.4`** — required by the Apple wrapper and
  OrpheusDL/pywidevine gencode. Do not loosen it.
- The optional native window needs `pywebview` (already in `requirements.txt`);
  without it Ripster opens in your default browser.

---

## Credits & Acknowledgements

> 🤖 **Built with [Claude](https://claude.com/claude-code) (Anthropic).** The
> entire application — architecture, every download engine, the UI, and this
> README — was designed and written in collaboration with **Claude Code**
> (Claude Opus 4.x). Thank you, Claude. 🦝

Ripster is a UI and orchestration layer — almost all of the heavy lifting is done
by the brilliant open-source projects below. **Huge thanks to every author and
contributor.** If your project is used here and you'd like the credit adjusted or
removed, please open an issue.

### Download engines
- [zhaarey / apple-music-downloader](https://github.com/zhaarey/apple-music-downloader) — Apple Music in ALAC & Dolby Atmos via a local wrapper *(this repo builds on it)*
- [glomatico / gamdl](https://github.com/glomatico/gamdl) — Apple Music via account cookies — AAC & music videos
- [WorldObservationLog / AppleMusicDecrypt](https://github.com/WorldObservationLog/AppleMusicDecrypt) — Apple Music ALAC/Atmos via public wrapper — no Apple ID
- [nathom / streamrip](https://github.com/nathom/streamrip) — Qobuz, Tidal, Deezer, SoundCloud — FLAC up to Hi-Res (core non-Apple engine)
- [lucida](https://codeberg.org/lucida/lucida) — SoundCloud streaming & downloads
- [llistochek / yandex-music-downloader](https://github.com/llistochek/yandex-music-downloader) — Yandex Music FLAC (with Plus)
- [OrpheusDL](https://github.com/OrfiTeam/OrpheusDL) + [Dniel97 / orpheusdl-beatport](https://github.com/Dniel97/orpheusdl-beatport) — Beatport & modular Spotify/metadata engine
- [zotify-dev / zotify](https://github.com/zotify-dev/zotify) — Spotify downloads via account streaming
- [librespot-org / librespot](https://github.com/librespot-org/librespot) & [kokarare1212 / librespot-python](https://github.com/kokarare1212/librespot-python) — open Spotify Connect client
- [Nizarberyan / SpotiFLAC](https://github.com/Nizarberyan/SpotiFLAC) — Spotify → lossless source matching
- [deemix](https://pypi.org/project/deemix/) *(RemixDev)* — Deezer download library

### Decryption & wrappers
- [itouakirai / wrapper](https://github.com/itouakirai/wrapper) — Apple Music (ALAC) decryption in a Docker container
- [WorldObservationLog / wrapper-manager](https://github.com/WorldObservationLog/wrapper-manager) — public wrapper-instance pool (wm.wol.moe)
- [WorldObservationLog / pywidevine](https://github.com/WorldObservationLog/pywidevine) — Widevine CDM for protected streams
- [hyugogirubato / KeyDive](https://github.com/hyugogirubato/KeyDive) & [wvdumper / dumper](https://github.com/wvdumper/dumper) — Widevine L3 device (.wvd) extraction

### Media processing
- [FFmpeg](https://ffmpeg.org) — transcoding & muxing of audio/video
- [GPAC / MP4Box](https://gpac.io) — MP4 packaging incl. Dolby Atmos
- [axiomatic-systems / Bento4](https://github.com/axiomatic-systems/Bento4) — `mp4decrypt`, DRM removal
- [nilaoda / N_m3u8DL-RE](https://github.com/nilaoda/N_m3u8DL-RE) — HLS segment downloading (used by gamdl)

### Metadata & search APIs
- [iTunes Search API](https://performance-partners.apple.com/search-api) — Apple Music catalog search & metadata
- [Deezer API](https://developers.deezer.com/api) — Deezer search & metadata
- [Qobuz API](https://www.qobuz.com/api.json/0.2) — Qobuz Hi-Res catalog
- [Tidal API](https://developer.tidal.com/documentation) — Tidal catalog (FLAC / MQA)
- [MarshalX / yandex-music-api](https://github.com/MarshalX/yandex-music-api) — Yandex Music search, metadata & token

### Ideas & inspiration
- [jaylex32 / Elixium](https://github.com/jaylex32/Elixium) — Deezer/Qobuz web downloader — influenced the search UI
- [nicholasgasior / d-fi](https://github.com/nicholasgasior/d-fi) — Deezer CLI core
- [DJDoubleD / QobuzDownloaderX-Blue](https://github.com/DJDoubleD/QobuzDownloaderX-Blue) — compatible Qobuz App ID & secret
- [exislow / tidal-dl-ng](https://github.com/exislow/tidal-dl-ng) — Tidal download reference

### Built with
[FastAPI](https://github.com/tiangolo/fastapi) ·
[Uvicorn](https://github.com/encode/uvicorn) ·
[websockets](https://github.com/python-websockets/websockets) ·
[HTTPX](https://github.com/encode/httpx) ·
[Mutagen](https://github.com/quodlibet/mutagen) ·
[PyYAML](https://github.com/yaml/pyyaml) ·
[protobuf](https://github.com/protocolbuffers/protobuf) ·
[gRPC](https://github.com/grpc/grpc) ·
[pywebview](https://github.com/r0x0r/pywebview)

---

## Disclaimer

Ripster is **not affiliated with** Apple, Spotify, Qobuz, Tidal, Deezer,
SoundCloud, Beatport, or Yandex. All trademarks belong to their respective
owners.

## License

For personal use. Respect the terms of service of each music provider and only
download content you are entitled to.
