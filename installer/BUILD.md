# Building the Ripster installer

`RipsterSetup-<version>.exe` is built with [Inno Setup 6](https://jrsoftware.org/isdl.php)
and bundles a **self-contained Python interpreter** with all dependencies, so the
end user's install is a pure file copy.

## What the installer does

At install time it simply **copies files** — no download, no PowerShell, no pip:

1. Lays down the app + a bundled `python\` interpreter (Python embeddable with all
   `requirements.txt` deps preinstalled, incl. `pywebview` → native window).
2. Seeds `config.yaml` from the example (also done by the launcher on first run).
3. Installs Start-Menu / Desktop shortcuts to a console-less launcher
   (`Ripster.vbs` → `Ripster.cmd` → `python\pythonw.exe ripster_launcher.py`).

This works **fully offline** and avoids the AV "downloader/dropper" heuristic that
flagged the older provisioning installer (which silently downloaded + ran Python).

Heavy / per-user / secret engines (Apple Go downloader, ffmpeg, Widevine L3
device) are still pulled/compiled per-user from the in-app **Setup tab** — never
bundled. Those steps need internet.

## Prerequisites

- Inno Setup 6 (`ISCC.exe`):
  ```powershell
  winget install -e --id JRSoftware.InnoSetup --accept-package-agreements --accept-source-agreements
  ```

## Build (two steps)

```powershell
# 1. Build the bundled Python (downloads embeddable + pip-installs requirements
#    into github_setup\python\ — a .gitignored 127 MB build artifact).
powershell -ExecutionPolicy Bypass -File installer\build_embedded_python.ps1

# 2. Compile the installer (bundles python\ + app → installer\output\RipsterSetup-<ver>.exe)
& "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe" installer\ripster.iss
```

Re-run step 1 only after a `requirements.txt` change; otherwise step 2 alone
repackages app changes.

## Notes

- Per-user install (`PrivilegesRequired=lowest`) → no admin; installs to
  `%LocalAppData%\Programs\Ripster`.
- The resulting `.exe` is ~33 MB (LZMA-compressed Python + app).
- **Unsigned** → SmartScreen may still warn "unknown publisher" until the file
  earns reputation or is code-signed (an EV cert removes the warning instantly).
- `provision.ps1` is retained for **from-source** installs (`git clone` → builds a
  `.venv`); the bundled installer does not use it.
