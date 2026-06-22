# Building the Ripster installer

`RipsterSetup-<version>.exe` is built with [Inno Setup 6](https://jrsoftware.org/isdl.php).

## What the installer does

It is a **bootstrap / provisioning** installer — it does **not** freeze a fixed
binary. At install time, on the user's machine, it:

1. Ensures **Python 3.12** (downloads the official installer if missing).
2. Creates a `.venv` and `pip install`s **all** Python dependencies
   (including `pywebview` → the native desktop window, no browser tab).
3. Seeds `config.yaml` from `config.example.yaml`.
4. Installs Start-Menu / Desktop shortcuts to a console-less launcher.

Everything heavy, per-user or secret is **pulled and compiled on the user's own
machine** from the in-app **Setup tab** — never bundled:

- the Go Apple-Music downloader (compiled from open source — zhaarey),
- `ffmpeg`, `N_m3u8DL-RE`, `Bento4` (downloaded from their open-source releases),
- the Docker decryption wrapper,
- the user's **own** Widevine **L3 `device.wvd`** (minted locally — see
  `_widevine_setup/`). No `.wvd` or account token is ever shipped.

## Prerequisites

- Inno Setup 6 (`ISCC.exe`). Install via winget:
  ```powershell
  winget install -e --id JRSoftware.InnoSetup --accept-package-agreements --accept-source-agreements
  ```

## Build

```powershell
& "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer\ripster.iss
```

The output lands in `installer\output\RipsterSetup-<version>.exe`.

## Notes

- The installer is **per-user** (`PrivilegesRequired=lowest`) — no admin needed;
  it installs to `%LocalAppData%\Programs\Ripster`.
- `provision.ps1` is idempotent and can be re-run standalone to repair an install:
  ```powershell
  powershell -ExecutionPolicy Bypass -File installer\provision.ps1 -InstallDir "<dir>"
  ```
- Requires an internet connection on first install (Python + pip dependencies).
