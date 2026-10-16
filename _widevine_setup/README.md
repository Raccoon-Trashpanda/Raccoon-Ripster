# Widevine L3 — mint your OWN `device.wvd`

SoundCloud's DRM tracks (Go+ 256k CTR/CENC) need a Widevine **L3 CDM**
(`device.wvd`) to fetch decryption keys. For legal and security reasons Ripster
ships **no** `.wvd` — **each user mints their own, locally, on their own
machine**. This folder is the open recipe + tooling to do that.

> Nothing here is a secret. The `device.wvd`, the extracted
> `client_id`/`private_key`, and `frida-server` are **never** committed — they
> are generated/downloaded on your machine. Google revokes L3 devices every
> ~6–24 months, so you may need to re-mint.

## The method that works: Android Studio AVD + KeyDive

MEmu / LDPlayer x86 ship **ClearKey only** — dead end. Android Studio's
**`google_apis`** x86_64 system image ships a real **Widevine L3**
(`libwvdrmengine.so`). Use `google_apis`, **not** `google_play` (so `adb root`
works). A headless emulator runs Chrome, KeyDive (`-a web`) drives a real
Widevine provision + license exchange and captures the `client_id` +
`private_key`, which become your `device.wvd`.

## Prerequisites (downloaded/compiled on YOUR machine — none bundled)

- **Android SDK** cmdline-tools + `platform-tools`, `emulator`,
  `system-images;android-30;google_apis;x86_64` (several GB). `_sdk_install.bat`
  is a resilient, re-runnable installer.
- **JDK/JRE 17** (sdkmanager/avdmanager need Java 17).
- **AEHD hypervisor driver** — x86_64 emulation won't boot without it. One-time
  install needs **admin/UAC**:
  `…\extras\google\Android_Emulator_Hypervisor_Driver\silent_install.bat`
  (conflicts with Hyper-V/WHPX). This is the one step that cannot be silent.
- **frida-server** (matching your host `frida`) — downloaded from the
  open-source [frida releases](https://github.com/frida/frida/releases).
- **KeyDive** ([hyugogirubato/KeyDive](https://github.com/hyugogirubato/KeyDive))
  and the L3 dumper ([wvdumper/dumper](https://github.com/wvdumper/dumper)).

## One console for the whole pipeline

Double-click **`wvd.bat`** (or `powershell -ExecutionPolicy Bypass -File
wvd_console.ps1`):

| # | Action |
|---|--------|
| 1 | Boot emulator (headless) + start frida-server |
| 2 | Extract `device.wvd` via KeyDive (`-a web` → Chrome plays DRM) → auto-installs |
| 3 | Install the last extracted `.wvd` into Ripster (`tools/widevine/device.wvd`) |
| 4 | **Verify** the installed `.wvd` against SoundCloud's license server |
| 5 | Stop emulator |

## Gotchas baked into the console

- **adb push (sync) is broken** on this emulator for big files — frida-server is
  streamed in via `base64` over `adb shell` instead.
- KeyDive does **not** start frida-server; the console does.
- `-a player` (Kaltura) only provisions → no keys. **`-a web`** (Chrome) does a
  full provision+license → captures `client_id`+`private_key`. If Chrome stops on
  its "Welcome" screen, tap *Accept & continue* once, then retry extract.
- **Runtime pin:** `construct==2.8.8` (pywidevine + pymp4 need it; KeyDive pulls
  2.10.70 only during extraction). If pywidevine import breaks with
  `subcon should be a Construct field`, run
  `pip install --no-cache-dir construct==2.8.8`.

## Files

```
wvd.bat            entry point
wvd_console.ps1    the console (status + all operations)
verify_wvd.py      SoundCloud license verification helper (reads your config.yaml)
_sdk_install.bat   resilient detached SDK component installer (re-run safe)
_emu_launch.bat    headless emulator launcher
```

> Paths default to `C:\Android\Sdk` / `C:\Android\jre17`; override in
> `wvd_console.ps1` if you install the SDK elsewhere.
