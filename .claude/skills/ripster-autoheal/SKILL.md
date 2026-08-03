---
name: ripster-autoheal
description: Catalogue of what Ripster and the Telegram bot repair AUTOMATICALLY, and the pattern for adding a new auto-heal. Read this before hand-fixing any recurring failure — if it is in the table below it is already self-healing and you should verify rather than re-fix, and if it is a NEW mechanical failure you should encode it here instead of fixing it by hand a second time. Triggers: "почини X", a symptom that has now happened twice, "add an auto-fix", "should this self-heal", extending tools/ripster_healthcheck.py, or wondering why something recovered on its own.
---

# Ripster auto-heal catalogue

Two different things live in this repo and they are easy to confuse:

- **`ripster-daily-ops`** is the *routine* — what to run at 08:00/20:00 and how to triage.
- **This skill** is the *catalogue* — what already fixes itself, and how to add the next one.

The rule the owner set: anything mechanical that has failed twice should stop being a
manual fix and become an auto-heal. Fixing it by hand a third time is the bug.

## What repairs itself today

| Symptom | Where it is handled | Verify it worked |
|---|---|---|
| App (7799) down after reboot; Docker not started; hung `RipsterLauncher.exe` | `_heal_app_down()` — starts Docker Desktop, kills hung launchers, relaunches | `check_app()` green on re-run |
| Apple wrapper container dead / stale `media-user-token` | `_heal_wrapper()` + token resync | `check_apple_wrapper()`; deeper cases → **ripster-apple-wrapper** |
| Wrapper **up on a DEAD session**: 10020 listens but 30020 serves nothing (app-side `amd.start_wrapper()` reuses `dist/docker/rootfs/data` with **no `-L`**, so an expired session loops `[.] playback error` and every decrypt would fail Invalid CKC while the TCP check stays green) | `check_apple_wrapper()` 30020-branch → `_heal_wrapper()` + re-probe + token resync (added 2026-07-25) | `curl 127.0.0.1:30020` returns `music_token` 244 chars |
| `tg-bot-api` container stopped | `check_bot()` → `docker start tg-bot-api` | container listed in `docker ps` |
| `tg-bot-api` container **running but wedged** (deliveries hang, `docker ps` still says up) | `check_botapi_responsive()` — probes `getMe`, restarts on hang | `getMe` returns `{"ok":true}` |
| `bot.py` process gone | `check_bot()` — relaunches detached; it self-announces to the owner | `bot.py` in the process list |
| Watchlist entries with no `artist_id` + duplicates (entry exists but is never polled) | `check_watchlist()` → `POST /api/watchlist/repair` | every entry has `last_check` after the next 6h sweep |
| Watchlist entry whose NAME is a joined co-author credit (`«A, B, C»` from the ♥ button on a collab release) — iTunes never resolves it, so it sits dead forever | same `POST /api/watchlist/repair` — since 02.08.2026 it splits the credit: first resolvable part becomes this entry, the rest are spun off and pass the usual de-dup (`split` in the response) | entry names are single artists, each with an `artist_id` |
| Watchlist entry on a **non-Apple** service (`service: deezer/qobuz/tidal/spotify`) never polled at all: `_check_watchlist()` filtered `service == "apple"`, so it fell into no list — not Apple, not SoundCloud, not label — and the add-response still said `resolved: true` (2026-07-31) | `_resolve_target()` резолвит `artist_id` для всех сервисов кроме SoundCloud; repair его добивает; дедуп теперь по паре (сервис, artist_id), иначе один артист на двух сервисах терял подписку | `broken`-фильтр чекера ловит любую не-SC запись без `artist_id`; после свипа `last_check` есть у всех |
| **UTF-8 BOM on a config** (`tgbot/config.json`, `users.json`, `config.yaml`) — a PowerShell `>`/`Set-Content` writes BOM by default on this box, `json.loads` dies on char 0, and since every reader wraps it in a bare `except` the damage is INVISIBLE: no owner report, `check_botapi_responsive()` self-skips as "no bot configured", telemetry says "config недоступен" (2026-08-03) | `check_config_bom()` — runs FIRST in `main()`, before the report path itself needs the config; readers also moved to `utf-8-sig` | `✅ Конфиги без BOM`; `python -c "import json,pathlib;json.loads(pathlib.Path('tgbot/config.json').read_text(encoding='utf-8'))"` |
| Third-party endpoint quietly retired (iTunes lookup/search) | `check_external_apis()` — canary probes; **detects**, cannot fix | warning names the endpoint |
| Spotify `ORPHEUS_NOT_AUTHED` / orphaned librespot blob | app-side self-heal from `.bak` | see [[project_spotify_blob_autonomy]] |
| Orphaned `ssh`/`cloudflared` tunnel processes | `_kill_stray_tunnels` | see [[project_tunnel_orphan_fix]] |
| Corrupt/undecodable file after download; malformed ALAC | `ripster/integrity_verify.py` + `main.go --fix-alac` | `console.integrity_fixed` in the log |
| Task runs on an engine that can't speak its URL's service (missing `service`/`engine` on the task dict defaulted to **apple/zhaarey**, so a Spotify album URL went to `apple-music-downloader.exe` → "Failed to get album response", **exit code 0**, 3 retries, task lost with no history row — 2026-07-27) | `runner._sanity_route()` — fills a missing `service` from the URL and re-routes the engine via `service_layer.engine_for_svc`; an already-set `service` is never overwritten (the Spotify **convert** flow keeps `service=spotify` on an Apple URL and `service` picks the save folder) | new `engine-url-mismatch` bucket in the checker's 24h error study stays at 0 |

Everything above lives in `tools/ripster_healthcheck.py` unless another file is named.

## Auto-fix or report? The test

Auto-fix only when ALL of these hold. Otherwise warn and let a human decide.

1. **Deterministic** — the same symptom always has the same cause. "Wrapper container exited"
   qualifies; "download failed" does not.
2. **Idempotent** — running it on an already-healthy system is a no-op. `POST
   /api/watchlist/repair` on a clean list changes nothing; that is why it is safe every sweep.
3. **Reversible / non-destructive** — restart a process, resync a token, backfill a field.
   Never delete user data, never re-queue downloads, never bump versions or push.
4. **Verifiable in the same run** — you can re-probe and prove it went green. A fix you
   cannot verify is a guess, and it must be reported as a guess.

Account/geo state is NOT a fault: Qobuz "no subscription", Beatport territory blocks, Apple
device-limit from a genuinely concurrent session. Note them, never "fix" them.

## Adding a new auto-heal

Put the check in `tools/ripster_healthcheck.py` so it runs unattended twice a day — not in a
skill, not in a one-off script.

```python
def check_thing():
    st, data = _api("/api/thing")          # _api signs an owner cookie for you
    if <healthy>:
        ok("Thing здоров"); return
    warn("Thing сломан: <что именно и почему это важно>")
    if NO_FIX:                             # --no-fix must always be honoured
        return
    <do the safe repair>
    if <re-probe passes>:
        fixed("Thing починен: <что изменилось>")   # fixed() feeds the owner report
    else:
        warn("Авто-починка не удалась — нужен ручной разбор")
```

Then register it in `main()`, and write the comment that says **when this failure was first
seen and what it silently broke** — that context is what makes the next reader trust it.

Test the failure branch without breaking the live box by loading the module and faking `_api`:

```python
import importlib.util, sys
spec = importlib.util.spec_from_file_location("hc", "tools/ripster_healthcheck.py")
hc = importlib.util.module_from_spec(spec); sys.argv = ["hc", "--no-bot"]
spec.loader.exec_module(hc)
hc._api = lambda path, method="GET", timeout=15: (200, <broken payload>)
hc.check_thing(); print(hc._report, hc._fixes)
```

Then a real dry run: `python tools/ripster_healthcheck.py --no-bot --no-fix` (exit 0 = green).

## Gotchas that have bitten this checker

- **Never kill "extra" `app.py` processes.** `app.py` spawns a child worker that inherits
  socket 7799; killing by netstat owner takes the whole service down.
- **Restart `app.py` from PowerShell, never Bash `nohup`.** Under the Bash tool the child
  inherits a POSIX-style PATH and loses `ffmpeg` — spectrogram and integrity-verify then fail
  in ways that look like code bugs. See [[feedback_bash_nohup_path_gotcha]].
- **A container in `docker ps` is not a working container.** Probe the actual API; that gap is
  exactly how the wedged `tg-bot-api` went unnoticed.
- **Write configs from Python, never from PowerShell.** `>`, `>>`, `Out-File` and
  `Set-Content` add a UTF-8 BOM (or ANSI-encode) on this box; Python's `write_text(...,
  encoding="utf-8")` does not. Every config reader now uses `utf-8-sig` for the same reason —
  it reads both, so hardening a reader is always free.
- **A silent success is the dangerous failure mode.** The watchlist "worked" for months while
  checking nothing, because a filter dropped every entry and nothing asserted the result was
  non-empty. When a check can pass vacuously, assert on the count.
- **Idempotency is the owner-facing contract**: a sweep must not double-download, double-restart
  or spam the owner. Report a fix only when something actually changed.
