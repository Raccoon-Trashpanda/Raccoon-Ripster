# ============================================================================
#  Widevine L3 Console - one place for the whole device.wvd pipeline.
#  Launch:  wvd.bat   (or:  powershell -ExecutionPolicy Bypass -File wvd_console.ps1)
#
#  Method that WORKS: Android Studio AVD (google_apis x86_64 ships real Widevine
#  L3) + KeyDive. MEmu/LDPlayer ship ClearKey-only - dead end, removed.
# ============================================================================
param([switch]$Auto)   # -Auto: run the whole pipeline headless (no menu, no prompts)
$ErrorActionPreference = "SilentlyContinue"
$HERE   = Split-Path -Parent $MyInvocation.MyCommand.Path
$ROOT   = Split-Path -Parent $HERE                       # C:\dev\apple_music
$SDK    = "C:\Android\Sdk"
# JRE: pick the jdk-* dir dynamically (don't hardcode 17.0.x - it changes).
$JRE    = (Get-ChildItem "C:\Android\jre17\jdk-*" -Directory -EA SilentlyContinue | Select-Object -First 1).FullName
if(-not $JRE){ $JRE = "C:\Android\jre17" }
$ADB    = "$SDK\platform-tools\adb.exe"
$EMU    = "$SDK\emulator\emulator.exe"
$SERIAL = "emulator-5554"
$AVD    = "wvd"
$IMAGE  = "system-images;android-30;google_apis;x86_64"
$FRIDA  = "$HERE\frida-server-x86_64"
# Interpreter: dev .venv OR the bundled embeddable python\ that ships in the
# installer. The old code only knew .venv -> on a real install keydive.exe was
# never found and Extract silently produced no .wvd.
if(Test-Path "$ROOT\.venv\Scripts\python.exe"){ $VENVPY = "$ROOT\.venv\Scripts\python.exe" } elseif(Test-Path "$ROOT\python\python.exe"){ $VENVPY = "$ROOT\python\python.exe" } else { $VENVPY = "python" }
$KEYDIVE= Join-Path (Split-Path -Parent $VENVPY) "Scripts\keydive.exe"
$WVDDST = "$ROOT\tools\widevine\device.wvd"
$env:JAVA_HOME = $JRE
$env:ANDROID_SDK_ROOT = $SDK
$env:PATH = "$JRE\bin;$SDK\platform-tools;$SDK\emulator;$env:PATH"

function Mark($ok){ if($ok){"[OK]"}else{"[ -- ]"} }
function Line($label,$ok,$detail){ "{0,-6} {1,-22} {2}" -f (Mark $ok),$label,$detail }

function Show-Status {
    Write-Host "`n==== Widevine L3 pipeline status ====" -ForegroundColor Cyan
    Line "JRE 17"        (Test-Path "$JRE\bin\java.exe")                 $JRE
    Line "platform-tools"(Test-Path $ADB)                               "adb"
    Line "emulator"      (Test-Path $EMU)                               "emulator.exe"
    Line "system-image"  (Test-Path "$SDK\system-images\android-30\google_apis\x86_64\system.img") "android-30 google_apis x86_64"
    $aehd = (sc.exe query aehd 2>$null | Select-String "RUNNING") -ne $null
    Line "AEHD driver"   $aehd                                          "hypervisor (needs admin to install)"
    Line "AVD '$AVD'"    (Test-Path "$env:USERPROFILE\.android\avd\$AVD.avd") ""
    $emuUp = (Get-Process qemu-system-x86_64 -EA SilentlyContinue) -ne $null
    Line "emulator up"   $emuUp                                         $SERIAL
    Line "device.wvd"    (Test-Path $WVDDST)                            $WVDDST
    Write-Host ""
}

function Boot-Emulator {
    if((Get-Process qemu-system-x86_64 -EA SilentlyContinue)){ Write-Host "Emulator already running." -ForegroundColor Yellow; return $true }
    if(-not ((sc.exe query aehd 2>$null | Select-String "RUNNING"))){
        Write-Host "!! AEHD hypervisor not running - x86_64 emulator can't boot." -ForegroundColor Red
        Write-Host "   Install it (one-time, needs admin/UAC):" -ForegroundColor Red
        Write-Host "   $SDK\extras\google\Android_Emulator_Hypervisor_Driver\silent_install.bat" -ForegroundColor Red
        return $false
    }
    Write-Host "Launching emulator (headless)..." -ForegroundColor Cyan
    Start-Process -FilePath $EMU -WindowStyle Hidden -ArgumentList `
        "-avd",$AVD,"-no-window","-no-snapshot","-no-boot-anim","-gpu","swiftshader_indirect","-no-audio",`
        "-dns-server","8.8.8.8,8.8.4.4","-netdelay","none","-netspeed","full"
    & $ADB start-server | Out-Null
    Write-Host "Waiting for boot..." -NoNewline
    for($i=0;$i -lt 30;$i++){
        Start-Sleep 5
        if((& $ADB -s $SERIAL shell getprop sys.boot_completed 2>$null).Trim() -eq "1"){ Write-Host " booted." -ForegroundColor Green; break }
        Write-Host "." -NoNewline
    }
    & $ADB -s $SERIAL root | Out-Null
    Start-Sleep 2
    Ensure-Frida
    return $true
}

function Ensure-Frida {
    # CRITICAL: the frida-server on the device MUST match KeyDive's frida CLIENT
    # version, else KeyDive reports "Frida server is not running" even though it is.
    # Derive the version from the installed frida and fetch the matching android
    # x86_64 server (cached). The bundled frida-server-x86_64 is only a last resort.
    $ver = (& $VENVPY -c "import frida; print(frida.__version__)" 2>$null)
    if($ver){ $ver = $ver.Trim() }
    $srv = $FRIDA
    if($ver){
        $cached = "$HERE\frida-server-$ver-x86_64"
        if(-not (Test-Path $cached)){
            $url = "https://github.com/frida/frida/releases/download/$ver/frida-server-$ver-android-x86_64.xz"
            $xz  = "$cached.xz"
            Write-Host "Downloading frida-server $ver (matches KeyDive)..." -ForegroundColor Cyan
            try {
                Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $xz -TimeoutSec 180
                & $VENVPY -c "import lzma,sys; open(sys.argv[2],'wb').write(lzma.open(sys.argv[1]).read())" $xz $cached
                Remove-Item $xz -Force -EA SilentlyContinue
            } catch { Write-Host "frida-server $ver download failed ($_) - using bundled." -ForegroundColor Yellow }
        }
        if(Test-Path $cached){ $srv = $cached }
    }
    # Kill any stale (possibly wrong-version) frida-server already on the device.
    & $ADB -s $SERIAL shell "pkill -f frida-server" 2>$null | Out-Null
    Write-Host "Pushing frida-server to device..." -ForegroundColor Cyan
    & $ADB -s $SERIAL push "$srv" /data/local/tmp/frida-server 2>$null | Out-Null
    & $ADB -s $SERIAL shell "chmod 755 /data/local/tmp/frida-server" 2>$null | Out-Null
    & $ADB -s $SERIAL shell "nohup /data/local/tmp/frida-server >/data/local/tmp/fs.log 2>&1 &" | Out-Null
    Start-Sleep 3
    $running = & $ADB -s $SERIAL shell "ps -A 2>/dev/null | grep frida-server" 2>$null
    if($running){ Write-Host "frida-server $ver started." -ForegroundColor Green }
    else {
        $log = & $ADB -s $SERIAL shell "cat /data/local/tmp/fs.log 2>/dev/null"
        Write-Host "frida-server did NOT start. Log: $log" -ForegroundColor Red
    }
}

function Skip-ChromeFRE {
    # KeyDive's -a web drives Chrome; a fresh profile stops on the welcome/First-Run
    # screen and KeyDive hangs (the classic "Chrome stuck on welcome" snag). With adb
    # root we pre-seed the chrome-command-line flag file so Chrome skips the FRE - best
    # effort, never fatal.
    & $ADB -s $SERIAL shell "echo 'chrome --no-first-run --disable-fre --no-default-browser-check' > /data/local/tmp/chrome-command-line" 2>$null | Out-Null
    & $ADB -s $SERIAL shell "chmod 644 /data/local/tmp/chrome-command-line" 2>$null | Out-Null
    # Best-effort: mark Play-services/Chrome first-run done so no setup wizard steals focus.
    & $ADB -s $SERIAL shell "settings put global device_provisioned 1" 2>$null | Out-Null
    & $ADB -s $SERIAL shell "settings put secure user_setup_complete 1" 2>$null | Out-Null
}

function Ensure-KeyDive {
    # KeyDive is a pip package; a bundled install has python\ but not keydive yet.
    # Install it into whichever interpreter we resolved so Extract never fails with
    # a silent "no .wvd" just because the tool was missing.
    & $VENVPY -c "import keydive" 2>$null
    if($LASTEXITCODE -eq 0){ return $true }
    Write-Host "Installing KeyDive (pip)..." -ForegroundColor Cyan
    & $VENVPY -m pip install --upgrade --disable-pip-version-check keydive
    & $VENVPY -c "import keydive" 2>$null
    return ($LASTEXITCODE -eq 0)
}

function Extract-Wvd {
    if(-not (Boot-Emulator)){ return }
    if(-not (Ensure-KeyDive)){ Write-Host "KeyDive install failed (pip) - check your internet." -ForegroundColor Red; return }
    Skip-ChromeFRE
    Write-Host "Running KeyDive (-a web -> Chrome plays DRM -> captures CDM)..." -ForegroundColor Cyan
    if(Test-Path "$HERE\_keydive_out"){ Remove-Item "$HERE\_keydive_out" -Recurse -Force }
    Push-Location $HERE
    # Invoke via -m (NOT the .exe shim - console-script .exe shims don't run under
    # the embeddable python; -m keydive works on every layout).
    & $VENVPY -m keydive -s $SERIAL --output "_keydive_out" -w -a web
    Pop-Location
    $wvd = Get-ChildItem "$HERE\_keydive_out" -Recurse -Filter *.wvd | Select-Object -First 1
    if($wvd){ Write-Host "Extracted: $($wvd.FullName)" -ForegroundColor Green; Install-Wvd }
    else { Write-Host "No .wvd produced. If Chrome stuck on welcome screen, accept it and retry." -ForegroundColor Red }
}

function Install-Wvd {
    $wvd = Get-ChildItem "$HERE\_keydive_out" -Recurse -Filter *.wvd | Select-Object -First 1
    if(-not $wvd){ Write-Host "No extracted .wvd to install - run Extract first." -ForegroundColor Red; return }
    New-Item -ItemType Directory -Force "$ROOT\tools\widevine" | Out-Null
    Copy-Item $wvd.FullName $WVDDST -Force
    Write-Host "Installed -> $WVDDST" -ForegroundColor Green
}

function Verify-Wvd {
    if(-not (Test-Path $WVDDST)){ Write-Host "No device.wvd installed." -ForegroundColor Red; return }
    Write-Host "Verifying against SoundCloud Widevine license server..." -ForegroundColor Cyan
    & $VENVPY "$HERE\verify_wvd.py"
}

function Stop-Emulator {
    & $ADB -s $SERIAL emu kill 2>$null | Out-Null
    Get-Process qemu-system-x86_64 -EA SilentlyContinue | Stop-Process -Force
    Write-Host "Emulator stopped." -ForegroundColor Green
}

# ---- AUTO mode: full pipeline headless, no prompts (driven by the Setup tab) ----
if($Auto){
    Show-Status
    if(Test-Path $WVDDST){ Write-Host "AUTO_RESULT: OK (device.wvd already present) $WVDDST" -ForegroundColor Green; exit 0 }
    Write-Host "=== AUTO MINT: boot -> extract -> install -> verify -> stop ===" -ForegroundColor Cyan
    if(-not (Boot-Emulator)){ Write-Host "AUTO_RESULT: FAIL boot (AEHD/emulator)" -ForegroundColor Red; exit 2 }
    Extract-Wvd                              # KeyDive -> auto Install-Wvd
    if(-not (Test-Path $WVDDST)){
        Write-Host "AUTO_RESULT: FAIL extract (KeyDive produced no .wvd - Chrome FRE/DRM?)" -ForegroundColor Red
        Stop-Emulator; exit 3
    }
    Verify-Wvd
    Stop-Emulator
    Write-Host "AUTO_RESULT: OK $WVDDST" -ForegroundColor Green
    exit 0
}

# ---- menu loop ----
while($true){
    Show-Status
    Write-Host "  [1] Boot emulator + frida-server"
    Write-Host "  [2] Extract device.wvd (KeyDive)  -> auto-installs"
    Write-Host "  [3] Install last extracted .wvd into Ripster"
    Write-Host "  [4] Verify .wvd against SoundCloud"
    Write-Host "  [5] Stop emulator"
    Write-Host "  [0] Exit"
    $c = Read-Host "`nChoose"
    switch($c){
        "1"{ Boot-Emulator }
        "2"{ Extract-Wvd }
        "3"{ Install-Wvd }
        "4"{ Verify-Wvd }
        "5"{ Stop-Emulator }
        "0"{ break }
        default{ Write-Host "?" -ForegroundColor Yellow }
    }
}
