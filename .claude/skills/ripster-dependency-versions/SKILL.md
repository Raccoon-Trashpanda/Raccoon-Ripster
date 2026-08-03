---
name: ripster-dependency-versions
description: MUST READ before adding/installing any Python dependency or engine into Ripster's bundled interpreter. Ripster's engines pin MUTUALLY INCOMPATIBLE versions of construct/protobuf into one shared site-packages; installing the wrong thing silently breaks Apple decrypt, SoundCloud DRM, or Spotify. Use when a tester reports "device.wvd not shown / invalid", "subcon should be a Construct field", "cannot import builder", AMD/streamrip breakage after a Setup step, or any pip dependency-conflict.
---

# Ripster dependency-version rules (DO NOT break the shared interpreter)

Ripster ships ONE bundled Python (`<install>/python`). Every engine `pip install`s into
that SAME `site-packages`. Several engines pin **mutually exclusive** versions, so the
LAST install wins and silently breaks the others. This caused: device.wvd showing as
invalid, SC DRM decrypt failing, AMD breaking — all on a box where the files were fine.

## The conflict matrix (memorize this)

| Engine / tool | construct | protobuf | other |
|---|---|---|---|
| **pymp4** (Apple / AppleMusicDecrypt parse) | **==2.8.8** (hard pin) | — | — |
| **pywidevine** 1.9.0 (SoundCloud DRM decrypt + device.wvd validate) | works with 2.8.8 | **>=6.33,<7** (needs `google.protobuf.internal.builder`) | — |
| **keydive** 3.0.6 (WVD minting only) | >=2.10.70,<3 | 5.29.5 | pathvalidate>=3.2.1 |
| **AMD / streamrip / grpcio** | — | **>=6.31** | — |
| **OrpheusDL** (Spotify/Beatport) — THE POLLUTER | — | **3.15.8** (DOWNGRADES!) | — |

**Irreconcilable in one env:** pymp4 wants `construct==2.8.8`, keydive wants `>=2.10.70`.
OrpheusDL's `protobuf 3.15.8` is BELOW what AMD (6.31) and pywidevine (6.33) need — and
3.15.8 lacks `google.protobuf.internal.builder` (added in protobuf 3.20) → any modern
protobuf import dies with `ImportError: cannot import name 'builder'`.

## THE RULES

1. **NEVER `pip install -r <engine>/requirements.txt` into the bundled python if it pins
   `protobuf<6` or `construct!=2.8.8`.** That's OrpheusDL and keydive. Install them in
   their OWN venv instead (see below). OrpheusDL downgrading protobuf to 3.15.8 is what
   broke AMD *and* SoundCloud DRM simultaneously on the clean test machine.
2. **The shared bundled python's baseline is `construct==2.8.8` + `protobuf>=6.33`.**
   Apple (pymp4) needs construct 2.8.8; pywidevine + AMD are happy at protobuf 6.33.
   Proven: a CLEAN env with just `pip install pywidevine` resolves to pywidevine 1.9.0 +
   construct 2.8.8 + protobuf 6.33.6 and `Device.load(device.wvd)` → `WVD LOADS OK`.
3. **Isolate conflicting engines in a dedicated venv** (`<install>/tools/<engine>venv`)
   and invoke them as a subprocess with that venv's `python.exe`. This is the ONLY robust
   fix; do not try to find one global version set — there isn't one.
   **GOTCHA: the bundled embeddable python ships WITHOUT the stdlib `venv` module**
   (`python -m venv` → "No module named venv"). Always fall back to virtualenv:
   `python -m venv <dir>`; if `<dir>/Scripts/python.exe` didn't appear,
   `pip install --break-system-packages virtualenv` then `python -m virtualenv <dir>`
   (verified: creates a working py3.12 venv on the embeddable).
   **Proven isolations (both shipped):** `tools/wvdvenv` = pywidevine+httpx+mutagen
   (SoundCloud DRM, commit cb56c41); `tools/orpheusvenv` = OrpheusDL+modules
   (Spotify/Beatport, commit 37bebd1). Live-verified on the clean test machine: installing
   OrpheusDL into its venv leaves shared protobuf at 6.33.6 (AMD/pywidevine stay healthy)
   while the venv holds its own 3.15.8.
4. **keydive is mint-time only** (runs in the WVD console toolchain), so its
   construct>=2.10.70 / protobuf 5.29.5 must NOT touch the shared env — keep it in the
   Android/WVD toolchain side, never `pip install keydive` into the bundled python.
5. After ANY engine install, re-verify the others still import: at minimum
   `python -c "import google.protobuf; from pywidevine.device import Device"`.

## Diagnosis cheat-sheet (the exact error → cause)

- `TypeError: subcon should be a Construct field` (from `construct/core.py`) on
  `Device.load` → **construct version mismatch / polluted env**. (Seen even with the
  "right" versions when the env was polluted; a clean isolated venv fixes it.)
- `ImportError: cannot import name 'builder' from 'google.protobuf.internal'` →
  **protobuf too old (<3.20)** — almost always OrpheusDL's 3.15.8 downgrade.
- `/api/widevine/status` returns `valid:false, error:"subcon..."` while the file exists
  and is ~3KB → the device.wvd is FINE; the env is broken. Don't re-mint; fix deps.
- `pip ... ResolutionImpossible` mentioning protobuf 5 vs 6 → you over-pinned; let pip
  resolve the single engine's deps in its OWN venv.

## How a healthy device.wvd looks
KeyDive-minted L3 v2 is ~3.3 KB. In a clean venv: `Device.load` →
`type=DeviceTypes.ANDROID, system_id=<int>`. If that works in isolation but not in the
server, the SHARED env is polluted — isolate, don't re-mint.

## Setup-order gotcha
The Setup tab installs AMD deps (protobuf 6.33) and LATER OrpheusDL (protobuf 3.15.8) into
the same env, so OrpheusDL silently re-breaks AMD + pywidevine. Until OrpheusDL is
venv-isolated, installing OrpheusDL last = WVD/AMD broken. Isolation is the fix.
