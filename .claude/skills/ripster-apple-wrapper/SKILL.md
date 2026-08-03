---
name: ripster-apple-wrapper
description: Diagnose and recover Ripster's Apple Music download wrapper (the ripster-wrapper docker container the zhaarey/amd engines decrypt through). MUST READ when "Apple downloads fail / dead", "Invalid CKC", "device limit", "maximum concurrent playing devices", "lease code 3062", "wrapper not running", "playback error loop", "10020 dead", or before touching apple-parallel-tracks / the wrapper pool / adding a second Apple account. As of 2026-07-22 most "device limit" errors trace to a SHARED baked-in device identity in the docker image, not actually-concurrent account usage — check that FIRST, before assuming someone else is logged in.
---

# Ripster Apple wrapper — diagnosis & recovery

Apple downloads (engines `zhaarey`/`amd`) decrypt CKC through a **docker container** that
logs into an Apple Music account and serves three ports:
`10020` decrypt · `20020` m3u8 · `30020` account-API (media-user-token harvest).
`config.yaml` points at these: `decrypt-port`, `m3u8-port`, `gamdl-wrapper-account-url`.

## 🆕 2026-07-23 — two findings, don't re-derive these from scratch

**1. "Track decoded/saved fine but plays with glitches/errors" (ffmpeg: `invalid element
channel count`) is NOT a wrapper/decrypt bug** — some Apple-encoded ALAC tracks ship
packets missing the bitstream's TYPE_END terminator. **Already fixed**: `utils/alacfix`
(ported from upstream `zhaarey/apple-music-downloader`) runs automatically after every
track save (`main.go:1166`) and silently no-ops on clean/non-ALAC files. If you see this
error on a file downloaded BEFORE this fix landed (commit `7efbc20`), just re-download —
don't debug the wrapper. Full writeup: [[project_alacfix_and_public_wrapper_2026-07-23]].

**2. The public `wm.wol.moe` wrapper-manager genuinely goes empty for real stretches** —
confirmed live: gRPC channel opens fine, but the volunteer-hosted instance pool reports
`no healthy and ready instances available` for real, extended periods (not a config
mistake, not local-network-specific — `wm1.wol.moe` backup doesn't even resolve via DNS
right now). It DOES recover on its own (confirmed: a failed test succeeded ~15min later).
A **self-heal health gate** now exists (`apple_router.mark_public_wrapper_unhealthy()`,
mirrors the local-wrapper CKC gate, 5min cooldown) so a dead pool doesn't burn ~30-45s
PER TRACK in retries — `amd.py` sets it automatically on the exact error string. **Don't
tell the owner "should work, check the code" if they report the public wrapper down —
verify live** (`POST /api/config {"apple-wrapper":"public"}`, queue a real fresh album,
watch for `no healthy and ready instances` in console.log) before concluding either way.

The canonical container is **`amd-wrapper`** (image `ripster-wrapper:premium`, owner account
baked in). The pool adds `rip-wrapper-1..N` for parallel downloads.

## 🔴🔴 #0 — READ THIS FIRST: the "device limit" is usually OUR device, not theirs

**Found 2026-07-22, changes everything below.** `ripster-wrapper:premium` ships a
**baked-in Apple device identity** — `adi.pb` (Android Device Identity protobuf) plus
`accounts.sqlitedb`/`cookies.sqlitedb`, at
`/app/rootfs/data/data/com.apple.android.music/files/` inside the image. **Every container
run from the stock image presents that SAME device identity to Apple, no matter which
`-L email:pass` account you log in with.** If that shared identity's lease is stuck
(from a previous ungraceful `docker rm -f`, from someone else's box that built/uses the
same image, or just because Apple hasn't expired it yet), **every account you try through
the unmodified image hits "device limit" identically** — this was mistaken for "someone
else is using this account" for hours before being diagnosed. Tell: **two genuinely
different Apple accounts both fail with the exact same error, immediately, through the
same image** — real concurrent-account collisions don't do that; a shared broken device
identity does.

**The fix:** mount an EMPTY directory over `/app/rootfs/data` so the wrapper generates a
FRESH device identity on first boot instead of reusing the image's baked-in one:

```bash
mkdir -p dist/docker/rootfs_working/data   # once, persists across restarts after first login
docker run -d --name amd-wrapper --restart no \
  -v "$(pwd)/dist/docker/rootfs_working/data:/app/rootfs/data" \
  -p 127.0.0.1:10020:10020 -p 127.0.0.1:20020:20020 -p 127.0.0.1:30020:30020 \
  -e "args=-H 0.0.0.0 -L ${AID}:${PW}" ripster-wrapper:premium
docker logs amd-wrapper --tail 20    # "response type 6" may still flash once, then
                                       # "account info cached successfully" — that's a PASS
```

Verified: a real "device limit"-rejected account succeeded immediately with a fresh
identity; a SECOND, entirely different account also hit "device limit" through the
UNMODIFIED image and then ALSO succeeded once given its own fresh identity. Don't skip
this check and jump straight to "wait 15 min" / "ask the account owner if someone else is
logged in" — verify the device-identity theory first, it's usually the real cause and the
fix takes under a minute.

**Do NOT reuse one fresh identity directory for two different accounts, and do NOT reuse
the SAME identity across two simultaneous containers.** The identity itself is what Apple
rate-limits per-device — two sessions sharing one identity re-triggers this exact error
even with different account credentials. One persistent `rootfs/data` directory per
account, always.

### 🔴 Git Bash СЪЕДАЕТ путь монтирования → снова baked-in identity → device-limit (02.08.2026)

Ручной `docker run -v "$(pwd)/…/data:/app/rootfs/data"` из Git Bash на Windows молча
ломается: MSYS конвертит Unix-путь назначения `/app/rootfs/data` в виндовый
`C:\Program Files\Git\app\rootfs\data`, и `docker inspect` показывает мусорный mount
типа `…acct3\data;C -> \Program Files\Git\app\rootfs\data`. Каталог НЕ смонтирован →
контейнер берёт **общую baked-in identity из образа** → та самая ловушка device-limit.
Логин при этом «cached successfully» (лиза случайно свободна), поэтому баг не виден,
пока не проверишь `docker inspect …--format '{{range .Mounts}}{{.Source}} -> {{.Destination}}'`.
Признак свежей identity сработавшего монтирования — `adi.pb` РЕАЛЬНО появился в
host-каталоге `dist/docker/rootfs_pool/acct{i}/data/…/files/adi.pb` (иначе — сломано).

**Фикс:** `MSYS_NO_PATHCONV=1 docker run … -v "$(pwd -W)/…/data:/app/rootfs/data" …`
(`pwd -W` даёт виндовый путь источника, `MSYS_NO_PATHCONV=1` не трогает путь
назначения). Пул (`ripster/wrapper_pool.py`) поднимает слоты через **Python docker
SDK** — там мангла нет, поэтому пул-контейнеры (`_ensure_instance`) всегда монтируют
верно; баг ловит ТОЛЬКО ручной docker из Git Bash.

### Переселение identity при смене состава пула
Identity-каталоги привязаны к НОМЕРУ слота (`acct{i}`), не к аккаунту. Убираешь/
переставляешь аккаунт в `wrapper-accounts` (индексы сдвигаются) — **перемести и
каталоги**, чтобы каждый аккаунт остался на СВОЁМ `adi.pb` (иначе два аккаунта делят
одну identity → device-limit). 02.08.2026: убрали дубль primary из `wrapper-accounts`
(он же slot 0 → 2 сессии на одном аккаунте = killer), затем `rm acct1(дубль); mv
acct2 acct1; mv acct3 acct2`. Проверка успеха — параллельная загрузка: оба доп-слота
`account info cached successfully`, ноль `maximum concurrent`, файлы легли.

**Итог 02.08.2026:** три РАЗНЫХ живых аккаунта (primary + 2), у каждого своя
персистентная identity, параллель работает без device-limit. Метка «device_limit» на
аккаунте могла быть СТАРОЙ (эпоха общей identity) — с собственным adi.pb он оживает.

## 🔴 #0b — the app's OWN start path can resurrect a dead session (found 2026-07-25)

There are **two** wrapper-start paths and they don't agree. `ripster/amd.py::start_wrapper()`
(docker-local mode — what `POST /api/wrapper/start` and on-demand download use) runs image
**`ripster-wrapper`** (`:latest`, WRAPPER_LOCAL_IMAGE) mounting **`dist/docker/rootfs/data`**
and, when `_has_saved_session()` sees an `adi.pb` there, passes **no `-L` credentials at all** —
it just trusts the saved session. If that session is expired, the container loops
`[+] starting… [.] playback error` forever, `30020` serves nothing, and every decrypt returns
Invalid CKC — **while `10020` still accepts TCP connections**, so any port-only health check
says green. `start_wrapper()` also short-circuits on `check_wrapper_running()`, so the sick
container keeps being treated as fine.

**Tell:** `docker inspect amd-wrapper` shows `IMG=ripster-wrapper` + mount `…/rootfs/data`
(healthy is `:premium` + a per-account identity dir). **Fix:** the standard runbook below with
`ripster-wrapper:premium` + `-L` + `-v …/rootfs_working/data:/app/rootfs/data` — logged in and
cached in 6 s on 2026-07-25, no device-limit, decrypt verified with a real ALAC download.
`tools/ripster_healthcheck.py` now auto-heals this (30020-silent branch → `_heal_wrapper()`,
which mounts `rootfs_working/data` instead of the shared baked-in identity). The underlying
`amd.py` divergence is **not** fixed — it will recreate the bad container the next time the
app starts the wrapper itself.

## 🟡 #0c — Invalid CKC that is NOT a dead session (found 2026-07-28)

A **single old / rights-restricted album** returns Invalid CKC on a perfectly healthy session.
Seen 2026-07-28: `.../album/its-high-time-i-got-mine-ep/279606055` (adamId 279606058) threw
`Invalid CKC` at 14:45 and 15:12, while the same container decrypted a 9/9 album at 15:15 and a
live ALAC probe at 20:02. The user-facing message still says «Apple-сессия протухла — перелогинь
wrapper», which points at the wrong repair: a relogin burns a scarce device lease and fixes nothing.

**Tell them apart before touching the container:** `docker logs amd-wrapper --tail 60` — if other
`adamId:` lines around the same time have NO `Invalid CKC` after them, the session is alive and the
failure is per-album (also shows up as `Failed to rip album: error getting album response` + the
honest `Exit 0`, bucket `apple-album-unavailable`). Only relogin when **every** decrypt in the
window fails and `30020` is silent (that's #0b).

The engine learned the difference (`engines/zhaarey.py:145` asks `local_wrapper_session_alive()`),
but on 2026-07-29 the console still contradicted itself: in `apple-wrapper: local` mode the runner
printed the engine's correct «сессия ЖИВА, перелогин НЕ поможет» and then appended a *fixed*
follow-up advising exactly that relogin. **A second message that "helpfully" restates the repair is
where a good diagnosis leaks back out** — the runner now asks the same probe and picks
`console.wrapper_local_region_fail` (advise `auto`/other storefront) vs `console.wrapper_local_drm_fail`
(advise relogin). If you add a third place that reacts to Invalid CKC, branch on the probe too.

## 🔴 #1 killer: parallel tracks / pool on a SINGLE Apple account

**One Apple account cannot hold several concurrent wrapper sessions**, REGARDLESS of the
device-identity fix above — this is a separate, Apple-account-side concurrency limit, not
the device-identity bug. Each container does a fresh `-L email:pass` login = one "playing
device" lease. 2+ sessions on the SAME account at once →
Apple returns **"maximum concurrent playing devices" (lease code 3062, response type 6)** →
no wrapper caches the account → port 10020 is dead → **ALL Apple downloads on that account fail**.

Trigger: `config.yaml` `apple-parallel-tracks: true`. **Keep `apple-parallel-tracks: false`
unless you have ≥2 Apple accounts** — and even with 2+ accounts, see the multi-account pool
section below (`ripster/wrapper_pool.py` was rewritten 2026-07-22; it used to spin up N
containers on ONE account, which was never actually safe and is no longer how it works).

Guards (both must stay in place):
- `config.yaml`: `apple-parallel-tracks: false`, `apple-pool: 'off'`.
- `ripster/wrapper_pool.py` `pool_enabled()` is **opt-in**: returns False unless
  `apple-pool in (True,"on","1",1)` AND ≥2 distinct accounts are configured (primary +
  `wrapper-accounts` list). (Do NOT revert to "on when creds present".) Mirror any change
  to `github_setup/ripster/wrapper_pool.py`.

## Multi-account pool (2026-07-22 rewrite — real parallelism, not the old broken mode)

`ripster/wrapper_pool.py`'s `WrapperPool` now takes a list of DISTINCT accounts
(`accounts[0]` = primary `wrapper-apple-id`/`wrapper-password`; `accounts[1:]` = the
`wrapper-accounts` config list, each `{id, password, label}`). Pool size is capped at
`len(accounts)` — there is no more "spin up N sessions on 1 account" elastic mode, since
that was never actually safe (see #1 above) and would ALSO have hit the device-identity
collision from #0 for every slot beyond 0 (the old code never mounted a per-slot identity
at all). Each slot now gets its own persistent identity dir at
`dist/docker/rootfs_pool/acct{i}/data`, created once and reused across restarts (so a
restart doesn't burn a fresh device lease every time).

API: `GET /api/wrapper/accounts` (list + live status), `POST /api/wrapper/accounts/add`
(`{id, password, label}` — persists to `wrapper-accounts`, starts its slot if
`apple-pool` is on), `POST /api/wrapper/accounts/{slot}/remove` (slot ≥ 1 only — slot 0 is
the primary account, managed through the regular Apple settings, not this list).

**Not yet proven under load as of 2026-07-22**: 2 accounts running simultaneously,
concurrent downloads actually spread across both slots by the queue runner. Backend is
wired; live concurrent-download verification was blocked mid-session on needing a second
real account (the original test account's credentials weren't retained). If you're
picking this up: add a 2nd real account via the API above, queue 2+ Apple tasks at once,
confirm via `docker ps`/each container's own logs which downloads went through which slot.

## 🔴 #2 trap: repeated logins → auth throttle

Rapid re-login attempts (e.g. a restart-loop, or you retrying by hand) burn device leases and
then flip Apple into a **temporary auth throttle**: symptom changes from device-limit
(response type 6) to **"login failed" (response type 4)**. Recovery is ONLY time:
stop ALL wrapper containers, wait **~15 min with zero running**, then ONE clean login.
`--restart unless-stopped` makes this worse (container re-logins in a loop) — use `--restart no`
until the first success, then `docker update --restart unless-stopped amd-wrapper`.

## Recovery runbook (single account, the normal case)

```bash
# 1. Stop everything holding a lease
docker rm -f amd-wrapper rip-wrapper-1 rip-wrapper-2
# 2. If you just saw device-limit/login-failed, WAIT ~15 min with nothing running.
# 3. Start ONE, no restart-loop, publish all three ports, premium image:
AID=$(grep '^wrapper-apple-id:' config.yaml | cut -d' ' -f2)
PW=$(grep '^wrapper-password:' config.yaml | cut -d' ' -f2)
docker run -d --name amd-wrapper --restart no \
  -p 127.0.0.1:10020:10020 -p 127.0.0.1:20020:20020 -p 127.0.0.1:30020:30020 \
  -e "args=-H 0.0.0.0 -L ${AID}:${PW}" ripster-wrapper:premium
# 4. Watch for success:
docker logs amd-wrapper --tail 20      # want: "account info cached successfully" + "listening"
# 5. Make it survive reboots WITHOUT a re-login:
docker update --restart unless-stopped amd-wrapper
```

Verify serving + harvest token:
```bash
curl -s http://127.0.0.1:30020        # {storefront_id, dev_token, music_token(244 chars)}
```
Then `POST /api/apple/sync-from-wrapper` (owner cookie) to sync media-user-token into config.

**Image matters:** use `ripster-wrapper:premium` (account baked in). `ripster-wrapper:latest`
relies on a mounted `rootfs/data` session — that session can be **dead** and loops
`[.] playback error` forever without ever caching the account.

## App / bot control endpoints (owner-gated, need session cookie + Origin)
- `POST /api/wrapper/start` (`setup.py` → `amd.start_wrapper(force_login=False)`)
- `POST /api/wrapper/relogin` (force_login=True — tears down + logs in fresh; triggers 2FA)
- `GET  /api/wrapper-status`
- Wrapper is started ONLY on demand (no auto-start at app boot), so restarting app.py is safe.

## Forge the owner cookie (for curl/scripts)
`ripster-session = "{ts}.{hmac_sha256(session-secret, str(ts))}"` — secret is a **string**,
`.encode()` it (NOT hex). Add header `Origin: http://127.0.0.1:7799` on POST (CSRF).

## Live-test a download
Queue an Apple track via `POST /api/queue/add` `{url, engine:"zhaarey", quality:"alac"}`, then
watch `logs/console.log`. Success = `apple/ALAC (Lossless)/<artist>/<album>/NN. ….m4a`;
verify with `ffprobe` → `codec=alac`, no decode errors. "Invalid CKC" = wrapper not really
serving (dead session / wrong account / no subscription for THAT content).


## apple-parallel-tracks: tested end to end (2026-07-25)

Settled with two real album downloads, not reasoning. **It works, and it is safe
in both configurations** — but the benefit exists only while there are two
DISTINCT Apple IDs.

**With 2 distinct accounts** (Random Access Memories, ALAC hi-res):
- Both containers decrypted tracks of the SAME album concurrently — consecutive
  adamIds landed on different containers, so the fan-out is real.
- The second wrapper logged in cleanly: `account info cached successfully`, no
  device-limit, no lease 3062.
- 13/13 files, 847 MB, decode-check found **zero** corruption. Parallelism does
  not damage output.

**With one account** (simulated by `apple-pool: false`, which takes the identical
code path — `pool_enabled()` False → `ensure_all_decrypt_ports()` → `[]`):
- Only `amd-wrapper` decrypted; the second container stayed completely idle.
- 14/14 files, 405 MB, zero corruption, zero CKC or device-limit errors across
  the whole run.

**Why it is now safe to leave the toggle on permanently.** `main.go`
`ripTracksMaybeParallel` clamps `workers` to `len(decryptPortList())`. One
endpoint means one worker, so the moment the second account goes away the toggle
degrades to the ordinary serial loop by itself — nobody has to remember to switch
it off. Without that clamp it would have launched `apple-parallel-count` (4)
goroutines onto a SINGLE wrapper session, which is the documented "Invalid CKC"
overload path.

**What the 2026-07-09 disaster actually was, restated:** it was not "parallelism
is broken", it was *N containers logging into ONE Apple ID*. `pool_enabled()`
requiring 2+ distinct accounts is what blocks that, and it is why the toggle is
harmless today. Do not weaken that check.

Not measured: the actual speed-up. The two runs used different albums, so there
is no clean A/B — run the same album with the pool on and off if a number is
needed.

## «unexpected finish» у gamdl — это протухший cookies.txt, а не wrapper (31.07.2026)

gamdl печатает `CRITICAL No active Apple Music subscription found` строкой
уровня STDOUT, а наружу шло `unexpected finish` — фраза, из которой не следует
ничего. Раннер добросовестно тратил на неё три повтора (15/45/120с ≈ 4 минуты)
на заведомо мёртвую попытку, а в бакете ошибок она лежала безымянной.

### 🔴 Уточнение 01.08.2026: «протух» — ЛОЖНЫЙ диагноз в половине случаев

Состояния ДВА, и снаружи они неразличимы, а лечатся по-разному:

| Что на самом деле | Признак | Что делать |
|---|---|---|
| Сессия ПРОТУХЛА | `/v1/me/account` → **401/403** | заново экспортировать cookies из браузера |
| Сессия ЖИВА, подписка КОНЧИЛАСЬ | **200**, `meta.subscription.active=false` | экспорт ТЕХ ЖЕ куки не поможет — нужен аккаунт С подпиской либо продлить текущий |

**Не диагностируй по возрасту файла и по срокам самих куки — это врёт.**
01.08.2026: `cookies.txt` лежал с 06.05 (почти 3 месяца), внутри `mut-refresh`
истёк 09.06, `geo` — 07.05. По всем внешним признакам «мёртв». На деле сессия
отвечала **200**, storefront `gb` — умерла ПОДПИСКА, а не куки. Совет «экспортируй
заново», который висел здесь и в healthcheck, отправлял владельца делать работу,
которая ничего бы не изменила.

Проверка (read-only, ничего не качает) — `dev_token` берётся у локального враппера:

```bash
curl -s http://127.0.0.1:30020            # → dev_token + music_token (контроль)
curl -s "https://amp-api.music.apple.com/v1/me/account?meta=subscription" \
  -H "Authorization: Bearer <dev_token>" -H "Media-User-Token: <mut из cookies.txt>" \
  -H "Origin: https://music.apple.com"
```

Это уже автоматизировано: `check_gamdl_cookies()` в `tools/ripster_healthcheck.py`
на каждом пробуждении печатает точный диагноз вместо догадки.

Учти: **аккаунт cookies.txt и аккаунт враппера — разные** (01.08: `gb` без подписки
против `ca` с подпиской). Живой `media-user-token` у враппера НЕ означает, что жив
gamdl-путь, и наоборот.

### 🔴 Уточнение 02.08.2026: dev_token с 30020 живёт 5 МИНУТ и врёт третьим способом

Проверка выше сама себя же и сломала. Враппер генерирует `dev_token` **один раз при
старте контейнера**, `exp − iat = 300 секунд`, и дальше сутками отдаёт тот же самый.
Через пять минут после запуска с ним всё отвечает **401** — включая
`/v1/me/account`. И проверка снова печатала «сессия протухла, экспортируй куки»,
хотя сессия была жива, а мёртв был токен, которым её спрашивали.

**Правило: перед выводом о чужой сессии убедись, что жив ТВОЙ dev_token.**
Единственный признак — публичный каталог отвечает 200:

```bash
curl -s -o /dev/null -w "%{http_code}\n" \
  "https://amp-api.music.apple.com/v1/catalog/us/artists/909253" \
  -H "Authorization: Bearer <dev_token>" -H "Origin: https://music.apple.com"
```

Свежий токен берётся со страницы Apple и живёт ~2 месяца: `https://music.apple.com/us/browse`
→ бандл `/assets/index~*.js` → в нём три JWT-подобные строки, годится **не всякая**
(у одной из трёх каталог отвечает 401) — бери первую, которая реально ответила.
Автоматизировано: `_apple_dev_token()` + `_scrape_dev_token()` в `tools/ripster_healthcheck.py`;
без живого токена проверка честно молчит вместо догадки.

Следствие для gamdl: при `gamdl-use-wrapper: true` он ходит на 30020 САМ и скрейпом
подстраховаться не умеет — эти качества получают 401, пока враппер не перезапущен.
**Ради этого враппер не перезапускать:** слоты устройств исчерпаны (см. 01.08), а
лечение действует ровно 5 минут.

Чинится **только руками владельца** — автоматически не лечится, поэтому healthcheck
выносит это отдельной строкой (бакет `gamdl-cookies`), а не прячет в сводку.

Важно: **wrapper тут ни при чём**. Загрузки через `zhaarey`/`AMD` идут через
docker-wrapper со своей сессией и продолжают работать, пока gamdl-путь мёртв —
поэтому «Apple работает» и «Apple сломан» могут быть верны одновременно, и
проверять надо тот путь, по которому реально пошла задача.

`gamdl.py:is_finished()` теперь называет эту ошибку по имени, и слово «cookies» в
тексте выключает повторы через `_RE_NO_RETRY` (runner.py). Если будешь менять
формулировку — сохрани слово, иначе вернутся четырёхминутные пустые циклы.
