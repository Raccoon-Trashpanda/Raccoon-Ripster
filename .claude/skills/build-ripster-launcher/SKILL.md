---
name: build-ripster-launcher
description: Rebuild or modify the frozen Ripster.exe launcher (the native pywebview window + system-tray app). Use when changing window/tray/minimize/single-instance behavior, when adding a launcher dependency, or when "the tray / minimize-to-tray / window doesn't do X" — anything in launcher_exe.py that ships as the PyInstaller-frozen Ripster.exe.
---

# Building & modifying the frozen Ripster.exe launcher

`Ripster.exe` is the thing users double-click. It is a **PyInstaller one-file frozen
exe** built from **`launcher_exe.py`** — NOT the bundled embeddable Python, NOT
self-updatable. It starts the real server (`python\python.exe app.py`) as a windowless
child, then opens the UI in a native **pywebview / Edge WebView2** window with a
**system-tray icon**.

## The files (and the decoy files — there are FOUR launcher-ish files)

- **`launcher_exe.py`** ← THE shipped launcher. Edit THIS. Mirrored to
  `github_setup/launcher_exe.py` (keep them byte-identical — they were identical pre-tray).
- `build/pyi/Ripster.spec` ← the PyInstaller spec that builds `Ripster.exe` (name=`Ripster`,
  `console=False`, icon=`github_setup/ripster.ico`, entry=`C:/dev/apple_music/launcher_exe.py`).
  **Has absolute paths + is gitignored at root** (root repo tracks only Go files), so this
  spec is NOT version-controlled — its content is recorded in [[project_tray_minimize_2026-06]].
- DECOYS, do not touch for the shipped app:
  - `launcher.py` = old **PyQt6** control-panel (Start/Stop buttons). Superseded.
  - `RipsterLauncher.spec` / `build_launcher.bat` build `RipsterLauncher.exe` from `launcher.py` — stale.
  - `ripster/launcher.py`, `ripster_launcher.py` = other historical entry points.

## Build (after editing launcher_exe.py)

```bash
# deps live in the BUILD venv (.venv), NOT requirements.txt — the server (app.py)
# never imports webview/pystray/PIL; only the frozen launcher does.
.venv/Scripts/python.exe -m pip install pywebview pystray Pillow

.venv/Scripts/python.exe -m PyInstaller build/pyi/Ripster.spec --clean --noconfirm \
  --distpath dist --workpath build/_pyi_work
# → dist/Ripster.exe   (~22 MB)
cp dist/Ripster.exe github_setup/Ripster.exe      # this is what ISCC bundles
certutil -hashfile github_setup/Ripster.exe SHA256
```

`github_setup/Ripster.exe` is **gitignored but force-tracked** — stage it with
`git add -f Ripster.exe`. The exe ships only via a fresh `RipsterSetup` install
(`package-ripster` skill); self-update can NOT replace it (frozen, not in `_OVERLAY_PATHS`).
So launcher changes reach testers ONLY through a new RipsterSetup build + release.

### Bundling a NEW launcher dependency

PyInstaller's static scan misses imports done lazily inside functions (we import
`webview`/`pystray`/`PIL` inside functions so a missing dep degrades gracefully). So in
`build/pyi/Ripster.spec` add `collect_all('<pkg>')` (datas+binaries+hiddenimports) AND
explicit `hiddenimports += ['pkg.submodule', ...]` for the platform backend
(e.g. `pystray._win32`, `PIL.Image`, `PIL.ImageDraw`).

## Tray / window architecture (in launcher_exe.py)

- pywebview `Window.events.closing` is **cancellable**: the Event's `set()` returns
  "cancelled" iff any handler returns **`False`**. So `on_closing` returns `False` to hide
  to tray, `True` (or nothing) to really close. A `state["quit"]` flag (set by the tray
  "Выход" item → `window.destroy()`) is how a real quit bypasses the hide.
- Window methods used: `hide()`, `show()`, `restore()`, `minimize()`, `destroy()`. Safe to
  call from the tray thread.
- **pystray** runs via `icon.run_detached()` (its own thread); `webview.start()` must own
  the main thread on Windows. Tray menu first item `default=True` = left-click action.
  `icon.notify(msg, title)` shows a balloon. `icon.stop()` removes it on quit.
- **`minimized` event is best-effort** on the edgechromium backend (may not fire) — the
  guaranteed fold-to-tray path is `closing`. Don't rely on `minimized` alone.
- **Single-instance**: `logs/launcher.lock` holds the PID; a 2nd launch with a live PID
  writes `logs/launcher.show` and exits; the running instance's watcher thread polls the
  flag and `show()+restore()`s. `_pid_alive` = `OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)`.
  This matters BECAUSE tray means the app keeps running hidden → relaunches are common.
- **Window geometry memory**: `_load_win_state`/`_save_win_state` persist
  `logs/window_state.json` (x,y,width,height). `create_window(x,y,width,height)` restores
  it; `window.events.resized`/`moved` save it (debounced via threading.Timer ~1s) plus a
  final save in `do_quit`. Off-screen coords are sanity-clamped → fallback centered 1280×860.
  Like `minimized`, resized/moved are best-effort on edgechromium (verify with a real drag).
- Gated by config `minimize-to-tray` (default True; env `RIPSTER_TRAY` overrides for tests).
  Key is whitelisted in `ripster/security.py` and toggled in `static/views/settings.html`
  (`s-minimize-to-tray`) + set in `static/js/urlbar_detect.js` (where the applyConfig setChk
  block lives after the frontend refactor). Default-on uses `c['minimize-to-tray']!==false`.

## Startup splash / readiness detection (2026-07-20 — real user bug)

Cold start (antivirus scanning the freshly-unpacked `python.exe`, a slow disk) can
take longer than opening the window straight at the server URL survives — the user
sees the browser's raw "connection refused" page and assumes Ripster is broken.

**The trap already fallen into once:** don't try to detect readiness with client-side
`fetch()` from a `webview.create_window(html=...)` page. That page has a `null`/opaque
origin, and Chromium blocks its fetches to a private-network address (127.0.0.1)
**outright — both plain fetch AND `mode:'no-cors'` fail** with `TypeError: Failed to
fetch`. This is NOT a CORS-allowlist problem on the server side (tightening/loosening
`CORSMiddleware.allow_origins` in `app.py` doesn't touch it) — it's a browser-level
Private Network Access restriction that `no-cors` does not bypass. v3.0.30 shipped a
splash that self-polled this way; it always burned its full timeout and showed "server
not responding" even when the server bound in ~4 seconds, because the poll silently
failed every single try. Full writeup: [[project_launcher_splash_cors_gotcha]].

**The actual fix (current design, v3.0.32+)**: `main()` already runs a reliable
Python-side `wait_for_ripster()` (plain `urlopen`, no browser) before ever opening a
window. `open_window(url, port, win_open, ready=bool)` uses that result: if ready,
`webview.create_window(title, url=url, ...)` directly — no splash at all, the common
case. If not ready, create with `html=_starting_html()` (a STATIC splash, no JS network
calls) and spawn a background thread (`_poll_until_ready`) that keeps checking
`ripster_alive(port)` from Python and calls `window.load_url(url)` — or
`window.load_html(...)` to update the message — once it knows the answer. Any future
"is the server up yet" signal into the webview MUST go through this Python-thread +
`load_url`/`load_html` pattern, never a fetch from inside the splash page itself.

## Testing the frozen exe (what CAN and CAN'T be verified without a human)

```bash
rm -f dist/logs/launcher.log
RIPSTER_TRAY=1  # then Start-Process dist/Ripster.exe (WorkingDirectory=dist)
# Verifiable headless via dist/logs/launcher.log + process list:
#   "[launcher] tray icon started"      ← pystray+PIL really made it into the frozen exe
#   2nd launch → "already running (pid N) → surfacing it, exiting" + process count unchanged
#   logs/launcher.show gets consumed (deleted) → watcher surfaced the window
#   "opening webview window -> url (ready=True/False)" ← confirms which readiness path fired
# Attaches to a live server on 7799 if present (won't kill it on quit — box[0] is None).
```

**Test on a SPARE port, never 7799 directly** (`RIPSTER_PORT=7801` env, or set `port:`
in the test copy's `config.yaml`) if the exe might spawn its OWN server — otherwise a
clean-install boot test can collide with / kill the user's live instance. See
[[feedback_headless_test_isolation]] for the incident that taught this.

**`Stop-Process`/`taskkill` can silently fail** ("Отказано в доступе"/access denied)
against a launcher process running elevated — don't loop retrying, it won't work from
an unelevated shell; just leave it (isolated test port/dir = harmless) and move on.

**NOT verifiable headless — ask the human to click:** close(X)→folds to tray, minimize→tray,
tray left-click→restore, tray "Выход"→quits for real. Always have the user do this final pass.

For pure logic changes in `launcher_exe.py` (like `_poll_until_ready`'s state machine),
prefer an isolated Python unit test — `import launcher_exe`, monkeypatch
`ripster_alive`/`time.time`/`time.sleep`, pass a fake window object recording
`load_url`/`load_html` calls — over spinning up the real frozen exe or a browser.
Faster, and zero risk to the live server.

Clean up test procs by name/port, never `pkill -f app.py` (MSYS pkill misses Windows procs;
use PowerShell `Get-Process Ripster | Stop-Process` and kill servers by port via
`Get-NetTCPConnection -LocalPort N`).
