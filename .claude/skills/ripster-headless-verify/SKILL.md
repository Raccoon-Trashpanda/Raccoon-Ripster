---
name: ripster-headless-verify
description: Verify a Ripster frontend/backend change (player, radar, launcher, any UI flow) using headless Chrome + raw CDP or Python, without risking the user's live server. Use whenever you're about to spawn a browser or run a "does the app boot" test against this project — read this FIRST.
---

# Verifying Ripster changes without breaking the live server

**2026-07-20**: verifying two unrelated features in one session caused TWO real
outages of the user's actual running Ripster instance (port 7799) — a "clean install
boot" test killed it via an undocumented hot-swap mechanism, and 110 leaked headless
Chrome processes later crashed its asyncio accept loop. Both were self-inflicted by
verification scripts, not by the code changes being tested. This skill exists so that
doesn't happen a third time.

## Golden rules

1. **Never bind or hit port 7799 for a "does it boot cleanly" test.** `app.py` has a
   self-update/hot-swap mechanism that detects a "stale" version already listening and
   KILLS it to take over — running a throwaway boot test straight against 7799 can
   destroy the user's live session. Copy the tree (or use a separate install dir) and
   set `RIPSTER_PORT=<spare>` / `port:` in that copy's `config.yaml` instead. This
   applies to the `preflight-ripster` skill's Gate 4 too — never point it at 7799 if
   the live app is running there.
2. **Every headless Chrome you spawn MUST be killed, on every exit path.** Pattern:
   ```js
   const proc = spawn(CHROME, [...], { stdio: 'ignore' });   // NOT detached+unref
   process.on('exit', () => { try { proc.kill('SIGKILL'); } catch (_) {} });
   ```
   `detached:true` + `unref()` (used in earlier scripts this session) lets the Chrome
   process outlive the Node script if anything goes wrong — across a debugging
   session with many retries this accumulates FAST (110 processes in one afternoon).
   After any burst of test runs — not just at the very end — check:
   ```bash
   powershell -NoProfile -Command "Get-Process chrome -ErrorAction SilentlyContinue | Measure-Object | Select-Object Count"
   ```
   and `Stop-Process -Force` any leftovers before continuing. If a test script is
   flaky, suspect leftover Chrome load from earlier attempts before suspecting the
   script's own logic.
3. **Prefer an isolated unit test over a browser when verifying pure logic.** If
   what you're checking is a state machine / call sequence (e.g. "does this polling
   function eventually call `load_url`"), write a small Python/JS script that imports
   the module and monkeypatches its dependencies (network calls, `time`, a fake
   window/DOM object) instead of driving a real page. Much faster, zero collateral
   risk, and — unlike a headless browser — lets you fast-forward simulated time
   instead of a real 3-minute wait. Example pattern (used to verify
   `launcher_exe._poll_until_ready` — see [[project_launcher_splash_cors_gotcha]]):
   ```python
   import sys, time
   sys.path.insert(0, r"C:\dev\apple_music")
   import launcher_exe as le
   le._log = lambda msg: print("[LOG]", msg)   # don't pollute the real launcher.log
   class FakeWindow:
       def __init__(self): self.calls = []
       def load_url(self, url): self.calls.append(("load_url", url))
       def load_html(self, html): self.calls.append(("load_html", html))
   le.ripster_alive = lambda port, timeout=1.5: <your condition>
   w = FakeWindow()
   le._poll_until_ready(w, "http://127.0.0.1:7799", 7799)
   assert w.calls == [...]
   ```
   For a fake clock (to test a 180s timeout without waiting 180s): monkeypatch
   `time.time`/`time.sleep` themselves (functions imported as `import time as _t`
   inside the target function still resolve through the same `time` module object).

## When a real browser IS needed (visual verification, actual page state)

- Reuse the CDP helper pattern from this session: raw `WebSocket` to
  `ws.webSocketDebuggerUrl` from `/json/new`, a `cdp(ws, id, method, params)` promise
  wrapper keyed on response `id`, and an `evalJs(ws, expr)` wrapper around
  `Runtime.evaluate` with `returnByValue:true, awaitPromise:true`. No Playwright/
  Puppeteer needed/available in this environment.
- **Poll for readiness, don't guess a fixed sleep.** Page/view fragment loads in this
  app (`views.js` fetches `views/*.html` lazily) varied from ~4s to 40s+ across
  otherwise-identical runs this session. A `waitFor(ws, exprBool, timeoutMs)` loop
  (poll every ~500ms, `typeof X === 'function'` / `!!document.getElementById(...)`)
  is far more reliable than `await new Promise(r => setTimeout(r, N))` — a fixed
  sleep either wastes time or times out unpredictably.
- **Global vars declared with `let`/`const` are NOT on `window`.** `Preview`,
  `_scResults`, etc. are top-level `let` bindings — `window.Preview` is `undefined`
  even though bare `Preview` works fine inside `Runtime.evaluate` (same global scope).
  Use the bare identifier, not `window.X`, when checking these from CDP.
  Also `location.href` navigation semantics (query `location.href` bare, not
  `window.location.href`, for the same reason if the page reassigns it).
- **Session auth**: forge the `ripster-session` cookie the same way the app does —
  `f"{issued_at}.{hmac_sha256(session_secret, str(issued_at))}"` — and set it via
  `Network.setCookie` before `Page.navigate`. `session-secret` is in `config.yaml`.
- **A synthetic click beats a programmatic function call for anything gated on a user
  gesture.** `Input.dispatchMouseEvent` (press+release at the element's
  `getBoundingClientRect()` center) counts as a real gesture; calling the onclick
  handler directly via `evalJs` does not, and some autoplay/permission paths care.
  But also: if the JS function you're calling ALREADY starts playback/does the
  action itself (e.g. `playRelease` auto-plays), don't ALSO click the toggle button
  afterward — it'll immediately undo what you just triggered (learned by accidentally
  pausing right after `playRelease` auto-started playback).
- Some helper functions early-return no-op if their own internal state isn't
  populated the "normal" way — e.g. `_scFetchYtTimecodes(id, cb)` does
  `if (!_scResults.find(...)) return;` and silently never calls `cb`. Don't assume a
  function "isn't working"; check its guard clauses against whatever state your script
  actually set up (did you go through the real search flow, or just call the play
  function directly?).

## After you're done

```bash
powershell -NoProfile -Command "Get-Process chrome -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue"
curl -s http://127.0.0.1:7799/api/ping -w "\n%{http_code}\n" --max-time 5   # confirm the live server is still fine
```
If the live server did go down during testing, say so to the user immediately and
explain what happened — don't just quietly restart it and move on.

Related memory: [[feedback_headless_test_isolation]], [[project_launcher_splash_cors_gotcha]].
