<#
  Ripster bootstrap provisioner (pure ASCII -- runs under Windows PowerShell 5.1).

  Run by the installer at install time, or standalone:
      powershell -ExecutionPolicy Bypass -File installer\provision.ps1 -InstallDir "C:\path\to\Ripster"

  Scope = the RELIABLE minimum to get a working app that opens in its own window:
    1. ensure Python 3.12 (download the official installer if missing),
    2. create .venv and pip-install ALL Python dependencies (incl. pywebview ->
       the native desktop window; no browser tab),
    3. seed config.yaml from the example,
    4. write a launcher that always uses the provisioned venv.

  Heavy / per-user / secret things are DELIBERATELY NOT done here -- they are
  pulled and compiled on the user's own machine by the in-app Setup tab
  (ripster/setup): the Go Apple downloader (compiled from open source, zhaarey),
  ffmpeg / N_m3u8DL-RE / Bento4, the Docker decryption wrapper, and the user's
  OWN Widevine L3 device.wvd. Nothing secret is ever shipped or downloaded here.

  Every step is idempotent: re-running skips what is already present.
#>
[CmdletBinding()]
param(
    [string]$InstallDir = (Split-Path -Parent $PSScriptRoot),
    [string]$PythonVersion = "3.12.8"
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

function Info($m) { Write-Host "[ripster] $m" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "[ripster] $m" -ForegroundColor Green }
function Warn($m) { Write-Host "[ripster] $m" -ForegroundColor Yellow }
function Have($cmd) { return [bool](Get-Command $cmd -ErrorAction SilentlyContinue) }

# Returns the python launch as an array of arguments, or $null. Either
# @("py","-3.12") or @("C:\path\python.exe").
function Get-Python {
    if (Have "py") {
        & py -3.12 -c "import sys" 2>$null
        if ($LASTEXITCODE -eq 0) { return @("py", "-3.12") }
    }
    foreach ($exe in @("python", "python3")) {
        if (Have $exe) {
            $v = (& $exe --version 2>&1 | Out-String)
            if ($v -match "3\.(1[1-9]|[2-9][0-9])") { return @($exe) }
        }
    }
    return $null
}

# Run a native command (python/pip/venv) so that lines it writes to stderr
# (pip notices, deprecation warnings) do NOT abort the script. Under
# $ErrorActionPreference='Stop' a native stderr line becomes a terminating
# NativeCommandError even when the exe exits 0 -- which silently broke the
# hidden installer run. We merge stderr->stdout, print it, and return the real
# exit code for an explicit check.
function Invoke-Native($cmd) {
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & $cmd[0] @($cmd[1..($cmd.Count - 1)]) 2>&1 | ForEach-Object { Write-Host $_ }
        return $LASTEXITCODE
    } finally { $ErrorActionPreference = $prev }
}

# 1. Python 3.12+
$py = Get-Python
if (-not $py) {
    Warn "Python 3.12 not found - downloading and installing it"
    $u = "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-amd64.exe"
    $inst = Join-Path $env:TEMP "python-$PythonVersion.exe"
    Info "downloading $u"
    Invoke-WebRequest -Uri $u -OutFile $inst -UseBasicParsing
    Info "installing Python (silent, per-user, adds to PATH)"
    $instArgs = @("/quiet", "InstallAllUsers=0", "PrependPath=1", "Include_pip=1", "Include_launcher=1")
    Start-Process -FilePath $inst -Wait -ArgumentList $instArgs
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $machPath = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $env:Path = "$userPath;$machPath"
    $py = Get-Python
    if (-not $py) { throw "Python install did not expose a 3.11+ interpreter on PATH" }
}
Ok ("Python: " + ($py -join " "))

# 2. .venv + ALL Python dependencies
$Venv   = Join-Path $InstallDir ".venv"
$VenvPy = Join-Path $Venv "Scripts\python.exe"
if (-not (Test-Path $VenvPy)) {
    Info "creating virtual environment (.venv)"
    [void](Invoke-Native ($py + @("-m", "venv", $Venv)))
    if (-not (Test-Path $VenvPy)) { throw "venv creation failed" }
}
Info "installing Python dependencies (pulls every dependency, incl. native-window pywebview)"
[void](Invoke-Native @($VenvPy, "-m", "pip", "install", "--upgrade", "pip", "wheel"))
$req = Join-Path $InstallDir "requirements.txt"
$code = Invoke-Native @($VenvPy, "-m", "pip", "install", "-r", $req)
if ($code -ne 0) { throw "pip install failed (exit $code)" }
Ok "all Python dependencies installed"

# 3. Seed config
$cfg   = Join-Path $InstallDir "config.yaml"
$cfgEx = Join-Path $InstallDir "config.example.yaml"
if ((-not (Test-Path $cfg)) -and (Test-Path $cfgEx)) {
    Copy-Item $cfgEx $cfg
    Ok "created config.yaml from example (add your service tokens in the app)"
}

# 4. Launchers. Ripster.cmd uses the provisioned venv and puts bin/ on PATH for
#    any tools the Setup tab later installs there. Ripster.vbs runs it hidden.
$runCmd = Join-Path $InstallDir "Ripster.cmd"
$cmdBody = @"
@echo off
setlocal
set "PATH=%~dp0bin;%PATH%"
start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0ripster_launcher.py" %*
"@
Set-Content -Path $runCmd -Value $cmdBody -Encoding ASCII
Ok "wrote Ripster.cmd launcher"

$runVbs = Join-Path $InstallDir "Ripster.vbs"
$vbsBody = @"
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh  = CreateObject("WScript.Shell")
base = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = base
sh.Run """" & base & "\Ripster.cmd""", 0, False
"@
Set-Content -Path $runVbs -Value $vbsBody -Encoding ASCII
Ok "wrote Ripster.vbs (console-less launcher)"

Write-Host ""
Ok "Bootstrap complete."
Info "Launch Ripster -> it opens in its own window at http://127.0.0.1:7799"
Info "On first run, open the Setup tab to install the heavy/optional engines"
Info "(Apple Go downloader, ffmpeg, Widevine L3 device, ...) on THIS machine."
