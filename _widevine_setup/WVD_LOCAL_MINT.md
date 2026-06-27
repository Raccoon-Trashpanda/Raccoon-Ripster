---
name: wvd-mint-local
description: Mint a Widevine L3 device.wvd on THIS PC for Ripster SoundCloud DRM. Use when the in-app auto-mint / Android emulator can't get network ("Active default network: none") and needs hands-on local fixing that a remote/SSH session can't do.
---

# Widevine L3 device.wvd — local mint playbook (tester PC)

You are Claude running **locally on the tester's Windows PC** (interactive desktop
session). A remote operator already built the whole pipeline but hit ONE wall over
SSH that a **local, interactive** session may be able to clear. Your job: get the
Android emulator ONLINE, then let the mint finish.

## The goal
Produce `C:\Programs\Ripster\tools\widevine\device.wvd` (a Widevine L3 CDM) so
Ripster can download SoundCloud DRM tracks (Go+ / private mixes). It's minted by
booting an Android emulator and running KeyDive against it while a DRM video plays.

## Current state on THIS PC (verified by the remote operator)
- Ripster install: `C:\Programs\Ripster` (bundled python at `python\python.exe`).
- Android SDK: `C:\Android\Sdk`. JRE: `C:\Android\jre17\jdk-*`. AVD name: `wvd`.
- ✅ Toolchain complete: JRE17, cmdline-tools, platform-tools, emulator, system-image
  `android-30;google_apis;x86_64`, AVD `wvd`.
- ✅ **AEHD hypervisor installed + RUNNING** (`sc query aehd` -> RUNNING). Virtualization
  ON in BIOS, Hyper-V OFF.
- ✅ KeyDive installed into the bundled python (`python\python.exe -m keydive`, v3.0.6).
  Its frida client version: **17.15.3**.
- ✅ AVD RAM fixed to 2048 (was a broken 96M default) + `hw.device.name=pixel`
  in `%USERPROFILE%\.android\avd\wvd.avd\config.ini`.
- ✅ Emulator boots, KeyDive **attaches its hook** to the Widevine process and
  **captures the keybox** (device_aes_key/device_id).
- ❌ **THE WALL: the emulator has NO network.** `dumpsys connectivity` shows
  `Active default network: none`; the DRM web player (bitmovin.com/demos) shows
  "No internet" -> no license exchange -> KeyDive never gets the device RSA key ->
  no `.wvd`.

## What the remote operator ALREADY TRIED (don't repeat — all failed)
- Bumped AVD RAM 96M -> 2G (was a real bug; necessary but didn't fix network).
- `-dns-server 8.8.8.8,8.8.4.4 -netdelay none -netspeed full` on launch.
- `-feature -Wifi` (force classic eth0 NAT instead of netsim WiFi).
- `ip route add default via 10.0.2.2 dev eth0` (Android uses per-network tables; no effect).
- `svc wifi enable` / `svc data enable` / airplane-mode toggle.
- **Restarted the host network.** **Disabled Windows firewall + network protections.**
- Result every time: `Active default network: none`, `ping` from adb shell
  "Network is unreachable" (note: adb-shell ping is unreliable on Android — the shell
  has no fwmark/network; trust `dumpsys connectivity` instead).

## KEY LEAD (why a LOCAL session may win)
1. **Same setup mints fine on the operator's PC on the SAME internet.** Difference:
   operator's **emulator 36.5.11** vs this PC's **36.6.11** (36.6 uses the new
   "netsim" network stack — a known source of emulator no-internet).
2. The remote operator ran everything **headless over SSH = Windows Session 0**
   (non-interactive, no desktop). Some emulator networking + UAC + GUI behave
   differently there. **You are in the interactive desktop session — use that.**

## DO THIS (in order; stop when `device.wvd` exists)

### Step 1 — boot the emulator WITH A WINDOW and watch it
```powershell
$env:ANDROID_SDK_ROOT="C:\Android\Sdk"
C:\Android\Sdk\emulator\emulator.exe -avd wvd -no-snapshot -dns-server 8.8.8.8 -verbose
```
Watch the window. Does Android show "Connected"/network bars? Open the built-in
browser/Chrome and load a normal site. If a setup wizard or a dialog blocks it,
dismiss it (that's the thing SSH/Session-0 couldn't do).

### Step 2 — verify network the AUTHORITATIVE way
```powershell
$adb="C:\Android\Sdk\platform-tools\adb.exe"
& $adb -s emulator-5554 shell "dumpsys connectivity | grep 'Active default network'"
```
Anything other than `none` = network is up -> skip to Step 5.

### Step 3 — if still no network, try the version downgrade (strongest lead)
The operator's working PC has emulator **36.5.11**. Downgrade this one:
- Easiest: install Android Studio, SDK Manager -> SDK Tools -> uncheck "hide obsolete",
  pick **Android Emulator 36.5.x**; OR download the emulator-windows_x64 zip for
  36.5.11 from the Google emulator archive and unzip over `C:\Android\Sdk\emulator`.
- Reboot the AVD, recheck Step 2.

### Step 4 — other local things to try if still dead
- Boot the AVD from **Android Studio GUI** (Device Manager -> ▶) and check its
  network — AS sometimes fixes network config the bare emulator doesn't.
- Telnet the emulator console: `telnet localhost 5554` (auth token in
  `%USERPROFILE%\.emulator_console_auth_token`), then `network status`, `gsm data on`.
- Check Windows: is there a 3rd-party VPN/proxy or a virtual network adapter
  conflict? `ipconfig /all` — the emulator NATs through the host stack.
- Try a different system image: `google_apis` **API 33** x86_64 (newer image + 36.6
  emulator network is often happier than API 30).

### Step 5 — once the emulator HAS internet, run the auto-mint (zero-touch)
```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File `
  C:\Programs\Ripster\_widevine_setup\wvd_console.ps1 -Auto
```
Watch for `AUTO_RESULT: OK`. KeyDive's web player (bitmovin) must actually PLAY the
DRM clip; if Chrome stops on a welcome screen, accept it once, then re-run.
Success = `C:\Programs\Ripster\tools\widevine\device.wvd` exists. Verify:
```powershell
C:\Programs\Ripster\python\python.exe `
  C:\Programs\Ripster\_widevine_setup\verify_wvd.py
```

## If the emulator network simply won't work here
Fall back to the product-correct path (no per-PC minting):
- The repo has a **WV-wrapper** (`/api/wv-wrapper/key` in `ripster/routes/soundcloud.py`)
  — the OPERATOR mints once on their working PC and shares the key over their wrapper,
  exactly like `wm.wol.moe` does for Apple. Tell the operator; this tester PC then
  needs no emulator at all. Set `sc-widevine-wrapper-url` (or equivalent) to the
  operator's wrapper in Settings -> SoundCloud.

## Handy paths / commands
- AVD config: `%USERPROFILE%\.android\avd\wvd.avd\config.ini`
- Manual mint menu (interactive): `C:\Programs\Ripster\_widevine_setup\wvd.bat`
- Auto mint: `wvd_console.ps1 -Auto`
- KeyDive: `C:\Programs\Ripster\python\python.exe -m keydive -s emulator-5554 --output _keydive_out -w -a web`
- AEHD check: `sc query aehd` (want RUNNING)
- Report back to the operator what `dumpsys connectivity` shows after each attempt.
