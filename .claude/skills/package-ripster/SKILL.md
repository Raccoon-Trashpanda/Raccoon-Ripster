---
name: package-ripster
description: Build, package, clean-test and diagnose the Ripster Windows installer (RipsterSetup-*.exe). Use when packaging a new build, when a user reports the installed app crashes on launch ("не нашёл пакет ripster" / silent shutdown), or when verifying the bundled-Python + in-app Setup-tab provisioning works on a clean machine.
---

# Packaging & clean-testing Ripster (Windows installer)

Ripster ships to non-technical users as **`RipsterSetup-<ver>.exe`** — an Inno Setup
installer that bundles a self-contained Python interpreter so the end-user install is
a pure file copy (no download / no pip / no admin). This skill is the full build →
test → diagnose loop. Everything here is verified on Windows 11 + Git Bash.

## Layout (what bundles what)

- `github_setup/` — the PUBLIC mirror; **this is what the installer bundles** (`SrcDir=".."` in the .iss is `github_setup/`).
- `github_setup/app.py`, `github_setup/ripster/` … — the app the user actually runs. **Mirror of the root `app.py` / `ripster/` core** (minus bot/guest secrets). If you fix a packaging bug in root, mirror it here or the exe won't get the fix.
- `github_setup/python/` — the bundled embeddable interpreter (**.gitignored, ~127 MB build artifact**). Built by `build_embedded_python.ps1`.
- `github_setup/installer/ripster.iss` — Inno Setup script (version, shortcuts, file list).
- `github_setup/installer/build_embedded_python.ps1` — downloads python-embeddable, enables `import site`, pip-installs `requirements.txt` into it.
- `github_setup/installer/output/RipsterSetup-<ver>.exe` — ISCC output. Copy to `github_setup/` + `github_setup/dist/` after building.

## Build (2 steps)

```bash
# 1. (only after requirements.txt changed) rebuild the bundled interpreter
powershell -ExecutionPolicy Bypass -File github_setup/installer/build_embedded_python.ps1

# 2. compile the installer (fast, ~30–60s; repackages app changes)
"/c/Users/<you>/AppData/Local/Programs/Inno Setup 6/ISCC.exe" github_setup/installer/ripster.iss
```

Bump `#define AppVersion` in `ripster.iss` so the user can tell new build from old —
the OutputBaseFilename follows it (`RipsterSetup-<ver>.exe`).

After ISCC: copy the exe to `github_setup/` and `github_setup/dist/`, remove the old
version's copies, and record the SHA256:
`certutil -hashfile github_setup/RipsterSetup-<ver>.exe SHA256`.

## Clean-test — ALWAYS do this before handing a build to a user

The dev machine has global packages that hide missing deps. Two independent clean tests:

### A. Fresh venv (proves code + requirements.txt are complete)
```bash
py -3.12 -m venv /c/dev/_clean_venv
/c/dev/_clean_venv/Scripts/python.exe -m pip install -r requirements.txt
/c/dev/_clean_venv/Scripts/python.exe app.py     # boots = OK; only "[Errno 10048] bind" if 7799 already taken
```

### B. Silent-install the actual exe to a throwaway dir (proves the bundle/._pth)
```bash
github_setup/installer/output/RipsterSetup-<ver>.exe //VERYSILENT //SUPPRESSMSGBOXES //NORESTART "//DIR=C:\dev\_install_test"
cat   /c/dev/_install_test/python/python*._pth          # MUST contain `import site` AND `Lib\site-packages`
timeout 35 /c/dev/_install_test/python/python.exe /c/dev/_install_test/app.py   # boots to banner = imports OK
rm -rf /c/dev/_install_test /c/dev/_clean_venv
```
A successful boot reaches the `🎵 Ripster → http://127.0.0.1:7799` banner; if 7799 is
already held by the live app you'll see the `10048` bind error — that still PROVES all
imports succeeded. Use a spare port only if you need a real HTTP 200.

### C. In-app Setup-tab provisioning (heavy per-user engines)
The installer never bundles Go/ffmpeg/Bento4/Widevine — the in-app **Setup tab**
(`ripster/setup/__init__.py`) downloads them per-user. Don't run the full installs on
the dev box (it mutates tools); instead liveness-check the source URLs so they don't 404:
Go `https://go.dev/VERSION?m=text` + `https://go.dev/dl/<ver>.windows-amd64.zip`,
GPAC `download.tsi.telecom-paristech.fr/gpac/...`, Bento4 `www.bok.net/Bento4/binaries/...`,
zhaarey clone `github.com/zhaarey/apple-music-downloader.git`. All should return 200.
The Widevine L3 "virtual machine" is the separate manual `_widevine_setup/` flow
(Android Studio AVD + KeyDive) — needs a real Android env, not part of the exe.

## Diagnosing "не нашёл пакет ripster" / silent crash on the USER's machine

The startup import guard in `app.py` (the `except ModuleNotFoundError` block) now
prints the **real** failing module + a full traceback + the python that ran it. Three
distinct causes — read the message:

1. **`НЕ ХВАТАЕТ зависимости '<x>'`** — ripster is present but a dependency isn't
   installed in *that* interpreter. Fix: `<python> -m pip install -r requirements.txt`,
   or add the dep to `requirements.txt` and rebuild the bundle.
2. **archive un-extracted** (`ripster/__init__.py` missing) — the user used Explorer's
   "Extract" which dropped subfolders; re-extract with 7-Zip/WinRAR.
3. **`bundled-Python не видит ripster (битый ._pth)`** — the interpreter can't see its
   own app dir. The embeddable runs ISOLATED: `sys.path` is built EXCLUSIVELY from
   `python3xx._pth`, whose entries resolve **relative to the python.exe directory**
   (proven empirically — NOT relative to cwd, despite an earlier wrong belief). So the
   file MUST contain ALL of:
   - `import site`  (so `Lib\site-packages` `.pth` hooks run)
   - `Lib\site-packages`  (the bundled deps)
   - `..`  ← **the app dir one level up (…\Ripster) where `import ripster` lives.**
     Without this line `import ripster` fails at the C level even though the package
     is present (`Существует: True`) — this was the real 1.0.0/1.0.1 launch crash.
   `build_embedded_python.ps1` writes all three and has a native-`import ripster`
   sanity check from a foreign cwd. `app.py` ALSO `sys.path.insert`s the app dir at
   runtime, but that's only a fallback for from-source/.venv launches (no ._pth) — do
   NOT rely on it for the embeddable; the `..` ._pth entry is the primary fix.
   Verify a build: `cd C:\ && <install>\python\python.exe -c "import ripster"` must
   print the package path, run from a cwd that has no ripster of its own.

4. **Folder-case mismatch — `'Ripster'` vs `'ripster'` (THE one that actually bit a
   user).** Windows NTFS is case-INSENSITIVE on disk but Python imports are
   case-SENSITIVE. If an OLDER install (or a manual zip extract) created the package
   dir as `Ripster` (capital), a plain reinstall-overwrite KEEPS the old case, so
   `import ripster` fails with `No module named 'ripster'` even though `os.path.isdir`
   / `.exists()` on the path return True (those are case-insensitive) and the dir is
   on `sys.path`. Tell-tale: `python -c "import os; print(os.listdir(<appdir>))"`
   shows `'Ripster'` and no lowercase `'ripster'`.
   - **One-shot fix on the user's machine:** rename to lowercase in two steps
     (Windows won't do a case-only rename in one):
     `Rename-Item <app>\Ripster _t; Rename-Item <app>\_t ripster`
   - **Durable fix (shipped):** `ripster.iss` has an `[InstallDelete]` that deletes
     `{app}\ripster` BEFORE `[Files]`, so every install recreates a fresh lowercase
     folder from the source manifest. Verified: simulate by renaming an installed
     `ripster`→`Ripster` (import breaks), reinstall → folder back to `ripster`, import OK.
   - **`PYTHONCASEOK` does NOT help here** — the bundled embeddable runs with
     `sys.flags.isolated == 1` and `ignore_environment == 1` (a side effect of the
     `._pth`), so it ignores PYTHON* env vars entirely. Don't bother setting it in the
     launchers or in-process; fix the folder case instead.

**Gotcha that wasted a whole session:** the OLD guard printed "не нашёл ripster" for
ALL causes and swallowed the real exception — so a missing *dependency* (or a
*case-mismatched folder*) looked like a missing *package* even though `Существует:
True`. If you ever see that, the first move is to surface `_e` / the traceback and
`os.listdir` the app dir, not to chase the path.

## Privacy

Keep author identity neutral: `.iss` AppPublisher/AppURL = `Raccoon-Trashpanda` (not the
real gmail/username). Git identity for this repo is the noreply address. Don't upload the
exe to VirusTotal (public). Unsigned → SmartScreen "unknown publisher" is expected
(only EV code-signing removes it).

**Defender false-positive (a tester, 2026-06-28, v3.0.23):** unsigned PyInstaller exes —
especially the heavier launcher (now bundles pystray/Pillow) — can trip Windows Defender
heuristics and get the installer BLOCKED/quarantined ("программа не запускается" on open),
not merely SmartScreen-warned. NOT a build bug — verify with a clean cold start (install
to a temp dir, run `Ripster.exe` with `RIPSTER_PORT=<spare>`, confirm `/api/ping` →
`{"app":"ripster","version":...}`). Tester fix: "Подробнее → Выполнить в любом случае",
exclude the install folder in Defender, or restore from quarantine. Durable fix = EV signing.
