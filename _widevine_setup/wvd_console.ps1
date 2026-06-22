# ============================================================================
#  Widevine L3 Console — one place for the whole device.wvd pipeline.
#  Launch:  wvd.bat   (or:  powershell -ExecutionPolicy Bypass -File wvd_console.ps1)
#
#  Method that WORKS: Android Studio AVD (google_apis x86_64 ships real Widevine
#  L3) + KeyDive. MEmu/LDPlayer ship ClearKey-only — dead end, removed.
# ============================================================================
$ErrorActionPreference = "SilentlyContinue"
$HERE   = Split-Path -Parent $MyInvocation.MyCommand.Path
$ROOT   = Split-Path -Parent $HERE                       # C:\dev\apple_music
$SDK    = "C:\Android\Sdk"
$JRE    = "C:\Android\jre17\jdk-17.0.19+10-jre"
$ADB    = "$SDK\platform-tools\adb.exe"
$EMU    = "$SDK\emulator\emulator.exe"
$SERIAL = "emulator-5554"
$AVD    = "wvd"
$IMAGE  = "system-images;android-30;google_apis;x86_64"
$FRIDA  = "$HERE\frida-server-x86_64"
$KEYDIVE= "$ROOT\.venv\Scripts\keydive.exe"
$VENVPY = "$ROOT\.venv\Scripts\python.exe"
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
        Write-Host "!! AEHD hypervisor not running — x86_64 emulator can't boot." -ForegroundColor Red
        Write-Host "   Install it (one-time, needs admin/UAC):" -ForegroundColor Red
        Write-Host "   $SDK\extras\google\Android_Emulator_Hypervisor_Driver\silent_install.bat" -ForegroundColor Red
        return $false
    }
    Write-Host "Launching emulator (headless)..." -ForegroundColor Cyan
    Start-Process -FilePath $EMU -WindowStyle Hidden -ArgumentList `
        "-avd",$AVD,"-no-window","-no-snapshot","-no-boot-anim","-gpu","swiftshader_indirect","-no-audio"
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
    $running = & $ADB -s $SERIAL shell "ps -A 2>/dev/null | grep frida-server" 2>$null
    if($running){ Write-Host "frida-server already running." -ForegroundColor Green; return }
    $present = & $ADB -s $SERIAL shell "[ -f /data/local/tmp/frida-server ] && echo yes" 2>$null
    if(-not ($present -match "yes")){
        Write-Host "Streaming frida-server to device (adb sync is broken for big files, using base64)..." -ForegroundColor Cyan
        $b64 = [Convert]::ToBase64String([IO.File]::ReadAllBytes($FRIDA))
        $b64 | & $ADB -s $SERIAL shell "base64 -d > /data/local/tmp/frida-server; chmod 755 /data/local/tmp/frida-server"
    }
    & $ADB -s $SERIAL shell "nohup /data/local/tmp/frida-server >/data/local/tmp/fs.log 2>&1 &" | Out-Null
    Start-Sleep 2
    Write-Host "frida-server started." -ForegroundColor Green
}

function Extract-Wvd {
    if(-not (Boot-Emulator)){ return }
    Write-Host "Running KeyDive (-a web → Chrome plays DRM → captures CDM)..." -ForegroundColor Cyan
    if(Test-Path "$HERE\_keydive_out"){ Remove-Item "$HERE\_keydive_out" -Recurse -Force }
    Push-Location $HERE
    & $KEYDIVE -s $SERIAL --output "_keydive_out" -w -a web
    Pop-Location
    $wvd = Get-ChildItem "$HERE\_keydive_out" -Recurse -Filter *.wvd | Select-Object -First 1
    if($wvd){ Write-Host "Extracted: $($wvd.FullName)" -ForegroundColor Green; Install-Wvd }
    else { Write-Host "No .wvd produced. If Chrome stuck on welcome screen, accept it and retry." -ForegroundColor Red }
}

function Install-Wvd {
    $wvd = Get-ChildItem "$HERE\_keydive_out" -Recurse -Filter *.wvd | Select-Object -First 1
    if(-not $wvd){ Write-Host "No extracted .wvd to install — run Extract first." -ForegroundColor Red; return }
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
