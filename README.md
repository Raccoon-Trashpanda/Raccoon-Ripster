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

## License

For personal use. Respect the terms of service of each music provider and only
download content you are entitled to.
