---
name: preflight-ripster
description: Pre-package verification gate for Ripster. Run this BEFORE every ISCC build / release to catch the recurring "works on my dev box, broken on the tester's clean install" class of bugs (missing tools, missing default app keys, cryptic hangs, mirror drift, version skew, deanon leaks). Use whenever about to package RipsterSetup-*.exe or cut a release; if any gate fails, fix it and re-run — do not package raw.
---

# Ripster pre-package preflight

**Why this exists.** Every recent tester bug had the same shape: it works on the
dev box because `config.yaml` already has credentials, tools, and tuning the dev
machine accumulated — and the **clean tester install has none of that**. The dev
box is the worst place to test a distributable. This gate forces a clean-config,
clean-tools check before packaging.

**THE GOLDEN RULE:** verify against **`config.example.yaml`** (what a tester gets),
**never** the dev `config.yaml`. If a feature only works because of a value in
`config.yaml` that is NOT in `config.example.yaml`, it is BROKEN for testers.

Run every gate below. If one fails → fix → re-run from the top. Only package when
all pass. Record pass/fail inline for the user.

---

## Gate 0 — Code sanity (fast)

```bash
# Python compiles (root + mirror) for everything touched this session
.venv/Scripts/python.exe -m py_compile app.py github_setup/app.py <changed .py + mirror>
# JS syntax for any changed static/js/*.js
node --check static/js/app.js   # + any other changed js

# Совокупность загруженных скриптов — то, чего node --check не видит в принципе:
# два файла с одним `let X` на верхнем уровне → второй не выполняется ЦЕЛИКОМ.
# Так у КАЖДОГО публичного пользователя был мёртв cookies_ui.js (31.07.2026).
# С 08.08.2026 сканер проверяет ЕЩЁ И view-фрагменты: контейнер #view-<n> без
# файла views/<n>.html = 404 на старте. Ровно этого в нём не было, когда он
# пропустил чёрный экран 3.5.0.
python tools/check_js_collisions.py

# Ключи i18n: и t()/ti() в JS, и data-i18n* в разметке, по обоим деревьям.
# Пропущенный ключ НЕ показывает сырой ключ — он оставляет авторский русский
# текст, то есть выглядит ровно как хардкод. Так и жили s.default_search_svc_*.
python tools/check_i18n_keys.py
```
Все три должны быть чисты (сканер возвращает 0). Подробности и правила —
скилл `ripster-frontend-file-drift`.

Сканер молчит про находки, если консоль в cp1251 и он падает на печати `✗` —
он сам себе это чинит (`reconfigure(encoding="utf-8")`), но если добавляешь свою
проверку, помни: **`EXIT=1` без списка проблем — это сломанный инструмент, а не
чистый прогон.** См. скилл `ripster-honest-diagnostics`.

## Gate 0.5 — No console-script `.exe` shims under the embeddable Python

**Why (cost a tester's Qobuz AND Deezer simultaneously):** setuptools console-script
`.exe` shims (`rip.exe`, `deemix.exe`, `gamdl.exe`, any `<tool>.exe` in
`python\Scripts`) **do NOT execute under the isolated embeddable Python** — they exit
1 with ZERO output. An engine that shells out to one sees "no tracks / no output" and
emits a misleading creds/subscription error. And `shutil.which("<tool>")` is a trap:
`app.py` prepends `<python>\Scripts` to PATH (so AMD finds ffmpeg), so `which()`
SUCCEEDS in-server and returns the broken shim — it returns None only in a bare shell,
so standalone tests pass while the running app fails.

```bash
# No engine build_cmd may invoke a Python tool via a .exe shim or shutil.which():
grep -rnE "shutil\.which\(|['\"][a-z_]+\.exe['\"]|/Scripts/|\\\\Scripts\\\\" \
  ripster/engines github_setup/ripster/engines \
  | grep -viE "ffmpeg|mp4(decrypt|extract)|N_m3u8|node|spotiflac" | grep -vE ":\s*#"
# (ffmpeg/Bento4/Node/spotiflac are NATIVE binaries — they DO run as .exe and are fine;
#  exclude comments. Anything left for rip/deemix/streamrip/gamdl/amz is a REAL shim bug.)
```
Every Python CLI must be spawned on the SAME interpreter:
`[sys.executable, "-m", "<pkg>"]` (if it ships `__main__`, e.g. `deemix`) or
`[sys.executable, "-c", "from <pkg>.<mod> import <entry>; sys.exit(<entry>())"]`
(streamrip ships NO `__main__`, so `-m streamrip` fails → use the `-c` entry form).
The grep must return nothing for `rip`/`deemix`/`streamrip`/Python-tool paths (native
binaries like ffmpeg/Bento4/Node are fine — they DO run as `.exe`).

## Gate 1 — Mirror parity

The exe bundles `github_setup/`, NOT the root. Any core fix made in root MUST be
mirrored (it may be a divergent/public-stripped file → patch pointwise, don't blind-copy).

```bash
# For each file you changed in root, confirm the mirror carries the same fix:
for f in <changed core files>; do
  echo "== $f =="; grep -n "<sentinel string from your fix>" "$f" "github_setup/$f"
done
```
Every fix must appear in BOTH trees (or be intentionally root-only — e.g. owner-only
UI like the telemetry tab, which must NOT ship to testers).

## Gate 2 — Version consistency

```bash
grep -m1 'RELEASE_VERSION = ' app.py
grep -m1 'RELEASE_VERSION = ' github_setup/app.py
grep -m1 'AppVersion "' github_setup/installer/ripster.iss
```
All three identical and bumped above the last release tag (self-update compares the
running RELEASE_VERSION to the latest GitHub release tag).

## Gate 3 — Deanon / privacy

```bash
# No real identity anywhere that ships or is public. The pattern itself must live
# OUTSIDE this file: writing the maintainer's handle into a gate that ships in a
# public repo IS the leak the gate exists to prevent (it shipped that way from
# 3.6.0 to 3.6.2 — removed 22.08.2026). Keep it in an untracked local file:
#   echo 'my-handle\|my-old-nick' > .deanon-patterns    # gitignored
PAT_FILE=.deanon-patterns
[ -f "$PAT_FILE" ] || { echo "no $PAT_FILE — Gate 3 cannot run"; exit 1; }
grep -rniI "$(cat $PAT_FILE)\|@gmail" github_setup/ --include=*.md --include=*.py \
  --include=*.iss --include=*.yaml --include=*.html --include=*.js | grep -vi binary
# Release bodies on GitHub (public even on a private repo's release page):
#   curl -s -H "Authorization: Bearer $PAT" .../releases | grep -f "$PAT_FILE"
```
Must be empty. (Commit-author history is a separate, KNOWN item — scrub via history
rewrite + force-push BEFORE flipping the repo public; see memory.)
Also: `.iss` AppPublisher/AppURL stay neutral (Raccoon-Trashpanda).

**Provenance of shipped files** — same class, different origin: a generated image
carries a C2PA manifest (`trainedAlgorithmicMedia` in JPEG APP11 / PNG `caBX`),
and chat-pasted text carries invisible characters that break word search.

```bash
python tools/check_ai_traces.py            # 0 = clean, 1 = something ships marked
python tools/check_ai_traces.py --selftest # proves the check can go red
```
Red ONLY on what actually ships (`github_setup/`, `static/`, `installer/`);
`design/` is a workshop and is reported for information only. Fixing is lossless
for images — see the `ai-traces-hygiene` skill. Do NOT "rewrite text so a detector
won't flag it": that trades the author's wording for the rewriter model's ceiling
and cannot be verified.

## Gate 4 — Clean install + boot (the bundle is intact)

```bash
SRC=github_setup/installer/output/RipsterSetup-<ver>.exe   # after ISCC
rm -rf /c/dev/_preflight
"$SRC" //VERYSILENT //SUPPRESSMSGBOXES //NORESTART "//DIR=C:\dev\_preflight"
# ._pth must be RELATIVE (import site / Lib\site-packages / ..) so portable works
cat /c/dev/_preflight/python/python*._pth
# folder is lowercase 'ripster' (NTFS case bug); key runtime files present
ls /c/dev/_preflight | grep -x ripster
ls /c/dev/_preflight/amd_runner.py /c/dev/_preflight/ripster_launcher.py
# config.yaml created on install == a COPY of config.example (NO dev secrets leaked)
diff <(grep -vE '^\s*#|^\s*$' /c/dev/_preflight/config.yaml) \
     <(grep -vE '^\s*#|^\s*$' /c/dev/_preflight/config.example.yaml) && echo "config clean"
# boots to the banner (10048 bind = live app holds 7799 = imports still OK)
timeout 35 /c/dev/_preflight/python/python.exe /c/dev/_preflight/app.py 2>&1 \
  | grep -iE "Ripster →|7799|10048|Traceback|ModuleNotFound"
```

## Gate 4.5 — The installed app's UI actually LOADS (added 2026-08-08)

**Why this gate exists:** Gate 4 proves the bundle imports and the server starts.
It greps the console banner — and a console banner is exactly what 3.5.0 printed
while every downloaded copy showed a **black screen**. The backend was perfect;
the frontend died on the first `await` of its boot handler because the public
build ships 13 view fragments and `views.js` fetched all 21 through
`Promise.all`. Nothing in Gates 0–4 looks at the page, so all of them passed.

Boot the INSTALLED copy on a spare port and require **zero** console errors.
Never point this at 7799 (see `ripster-headless-verify` — the app kills a
"stale" server holding its port).

```bash
RIPSTER_PORT=7803 /c/dev/_preflight/python/python.exe /c/dev/_preflight/app.py &
# headless CDP: subscribe to Runtime.exceptionThrown + Log.entryAdded(level=error)
# + Network.responseReceived(status>=400), navigate, wait for the socket, then assert:
```

Pass requires ALL of:

| проверка | порог |
|---|---|
| `Runtime.exceptionThrown` | **0** |
| 404 на собственные ресурсы (`/static/**`, `/api/**`) | **0** |
| заполненных экранов (`section.view` с содержимым) | **все** |
| WebSocket | `readyState === 1` |
| версия в шапке | реальная, **не** `v—` |

Последние две — самые полезные: `v—` и закрытый сокет означают, что обработчик
загрузки оборвался на середине, даже когда каркас страницы выглядит нормально.
Сравнивать длину основной области, а не «страница открылась»: при этом баге
`document.body` весил 35 КБ вместо 320 КБ.

И: **прогонять на чистой распаковке публикуемого дерева** (`git archive HEAD`),
а не на рабочей копии — иначе гейт увидит файлы, которых у пользователя нет.
Если установщик пакует рабочий каталог, спрятать незакоммиченное (`git stash`)
до сборки.

Связано: `ship-what-you-tested` (тот же вывод в общем виде),
`ripster-frontend-file-drift` (сам баг и сканер под него).

## Gate 5 — Shipped default credentials are ALIVE (not stale)

App-level keys baked into the build (so a tester needs no setup for catalog/search)
go stale and get revoked. Test each LIVE, the way the app uses it:

```bash
# Qobuz app_id (search) — 200 = alive, 401 = DEAD (replace the default)
APPID=$(grep -m1 'qobuz-app-id' github_setup/config.example.yaml | grep -oE '[0-9]{6,}')
curl -s "https://www.qobuz.com/api.json/0.2/album/search?query=test&limit=1&app_id=$APPID" \
  -o /dev/null -w "qobuz app_id %{http_code} (200=ok 401=dead)\n"
# Qobuz DOWNLOAD also needs the PAIRED secret — config.example must carry BOTH
grep -E "qobuz-app-id|qobuz-secrets" github_setup/config.example.yaml   # both non-empty
# SoundCloud client_id is scraped at runtime (no bake) — confirm the scraper route exists
```
History of this gate: the public Qobuz `798273057` started 401-ing (search broke);
and shipping the app_id without its paired secret made downloads silently return
0 tracks while search worked. Both were dev-only-config gaps.

## Gate 6 — Clean-config SERVICE behavior matrix (the big one)

With the clean `_preflight` install (config.example, NO creds), every service must
do ONE of: **(A) work without creds**, or **(B) fail FAST with a clear, actionable
message** — never hang, never spin, never dump a raw traceback.

| Service | No-creds expectation |
|---------|----------------------|
| Apple ALAC (amd / public wrapper) | **(A) works** — no token; Bento4 auto-installs on first ALAC; gRPC INTERNAL → fast retryable message |
| Apple AAC / video (gamdl) | (B) clear "needs cookies.txt" |
| Qobuz | search **(A) works** (app_id+secret); download **(B)** clear "needs your paid Qobuz account" |
| Deezer | (B) clear "set deezer-arl" |
| Tidal | (B) clear "use device login in Settings" |
| Spotify | (B) clear "set client-id/secret + add account to dashboard" |
| SoundCloud (non-DRM) | **(A) works** (scraped client_id); DRM → clear "needs .wvd" |
| Beatport | (B) clear "needs OrpheusDL + Beatport account" (auto-installs OrpheusDL) |
| Yandex | (B) clear "set yandex-token" |

**No-hang / no-spin check (codified from the Qobuz 28-min spin):** a PERMANENT
failure (no account, bad link, missing creds) must be in `_RE_NO_RETRY`, NOT
`_RE_PATIENT`. Only genuinely transient wrapper-overload conditions (wm.wol.moe /
"не вернул device" / ready=false) belong in `_RE_PATIENT`.

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -c "
import sys; sys.path.insert(0,'.'); import ripster.runner as r
for msg in ['Qobuz: 0 треков','needs deezer-arl','set yandex-token']:
    print(msg, '-> patient:', bool(r._RE_PATIENT.search(msg)),
                 'no_retry:', bool(r._RE_NO_RETRY.search(msg)))
"
# Any permanent 'needs creds' message that is patient=True is a SPIN BUG.
```

## Gate 7 — Telemetry pipeline (so tester logs actually reach us)

```bash
# Owner tunnel up + ingest reachable cross-site (it is CSRF-exempt + token-gated)
curl -s https://raccoon-ripster.serveousercontent.com/api/ping -w " %{http_code}\n"   # 200
curl -s -X POST https://raccoon-ripster.serveousercontent.com/api/telemetry/ingest \
  -H "Content-Type: application/json" \
  -d '{"instance_id":"preflight","token":"<shared token>","lines":[{"t":0,"level":"error","text":"preflight"}]}' \
  -w " %{http_code}\n"     # {"ok":true,...}  NOT 403 (CSRF) / 502 (tunnel down)
# then clean it: rm logs/remote/preflight.jsonl + remove from _index.json
```
Owner needs `remote-enabled: true` (tunnel auto-start) + `telemetry-ingest-enabled: true`.
`/api/telemetry/ingest` must be in `_CSRF_EXEMPT_PATHS` (it's cross-origin).

## Gate 7.5 — Light-theme palette sanity (added 2026-07-01)

Recurring class of bug: UI reads fine on the dark (default) theme but is unreadable
on light theme. Two root causes caught live:

```bash
# (1) A `var(--X, <fallback>)` whose --X is NEVER defined always uses the fallback.
# If the fallback is a hard dark colour, the element is black on the light theme
# (e.g. var(--panel,#1a1a1a) → black telemetry cards; --panel was undefined).
# List every custom prop USED, subtract every prop DEFINED — the difference are
# guaranteed theme bugs when their fallback is dark:
grep -rhoE 'var\(--[a-z0-9-]+' static static/js static/views | sed 's/var(//' | sort -u > /tmp/used
grep -oE '^\s*--[a-z0-9-]+' static/css/main.css | tr -d ' ' | sort -u > /tmp/defined
comm -23 /tmp/used /tmp/defined    # any --X here with a dark fallback = fix it

# (2) A status/chip that sets ONLY a text colour on a HARDCODED dark box
# (background:rgba(0,0,0,.25)) washes out on light theme. Status chips must carry
# their OWN theme-aware tinted bg + border, not a bare colour on a fixed-dark box.
grep -rn "background:rgba(0,0,0" static/views static/js | grep -i "status\|chip\|badge"
```
Every custom prop referenced with a dark fallback MUST be defined in BOTH theme
blocks of main.css (`:root` dark + `html.light`). Fix, bump `main.css?v=`, mirror.

## Gate 8 — Cleanup + sign-off

```bash
rm -rf /c/dev/_preflight
```
Report each gate's pass/fail. Only after ALL pass:
- ISCC compile → copy exe to `github_setup/` + `dist/` → certutil SHA256
- silent-install → Compress-Archive portable zip → SHA256
- push code, create release vX.Y.Z, upload exe + portable zip
(see the **package-ripster** skill for the exact build/release/upload commands).

---

## Quick reference — recurring root causes caught here
- Missing per-machine tool (Bento4 `mp4extract`, ffmpeg, Node) → auto-install or clear Setup pointer.
- Stale/missing shared app key (Qobuz app_id 401; app_id without paired secret).
- Permanent error mis-classified as transient → 15× patient-retry → "stuck, won't cancel".
- Mirror drift (fix in root, not in `github_setup/` → exe doesn't get it).
- Version skew (RELEASE_VERSION vs .iss) → self-update never offers the build.
- Deanon (old username/gmail in a release body or history).
- Telemetry dead (tunnel 502 / CSRF 403) → no remote diagnostics.
- Light-theme unreadability (undefined `var(--X, #dark)` → black-on-light; status chip = bare colour on hardcoded-dark box). See Gate 7.5.
- One-click gap: a Setup button that runs the *final* step (e.g. `wvd_console.ps1 -Auto`) but skips the prerequisite toolchain installer (`setup_widevine_toolchain`) → works on the dev box (prereqs already there), fails rc≠0 on a clean install. Every one-click button must install its OWN prerequisites idempotently, not assume a prior step ran.
- self-update overlay gap: a code/script dir edited for a fix but absent from `_OVERLAY_PATHS` in updater.py → testers on an existing install never receive it via "Обновить сейчас" (only a full reinstall). If you fix files under a NEW top-level dir, add it to `_OVERLAY_PATHS` (both trees).
- **Backend green, frontend dead** (3.5.0): server banner prints, imports fine, every file present — and the page is a black rectangle because one missing view fragment rejected the `Promise.all` at the head of the boot handler. Nothing but Gate 4.5 sees this. Tells: version stuck at `v—`, WebSocket never opens, `document.body` a tenth of its normal size.
- **Installer packs the WORKING TREE, not the commit** (`SrcDir=".."`): everything uncommitted in `github_setup/` ships. Stash before ISCC, or build from `git archive HEAD`.
