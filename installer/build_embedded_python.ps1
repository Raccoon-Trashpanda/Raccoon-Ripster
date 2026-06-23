<#
  Build the bundled Python interpreter for the installer.

      powershell -ExecutionPolicy Bypass -File installer\build_embedded_python.ps1

  Downloads the official Python "embeddable" zip, enables site-packages, bootstraps
  pip, and installs ALL of requirements.txt into it -- producing a self-contained
  github_setup\python\ folder. The installer then bundles this folder, so the end
  user's install is a pure file copy: no download, no PowerShell, no pip at install
  time (works fully offline and avoids the AV "downloader" heuristic that flagged
  the old provisioning installer).

  Re-run to refresh after a requirements.txt change. github_setup\python\ is
  .gitignored (127 MB) -- it is a build artifact, rebuilt here at package time.
#>
[CmdletBinding()]
param(
    [string]$PythonVersion = "3.12.10",
    [string]$Root = (Split-Path -Parent $PSScriptRoot)   # github_setup\
)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$dest = Join-Path $Root "python"
if (Test-Path $dest) { Remove-Item $dest -Recurse -Force }
New-Item -ItemType Directory -Force $dest | Out-Null

$tmp = Join-Path $env:TEMP ("ripster_pyembed_" + $PythonVersion)
if (Test-Path $tmp) { Remove-Item $tmp -Recurse -Force }
New-Item -ItemType Directory -Force $tmp | Out-Null

Write-Host "[build] downloading Python $PythonVersion embeddable"
$zip = Join-Path $tmp "embed.zip"
Invoke-WebRequest "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-embed-amd64.zip" -OutFile $zip -UseBasicParsing
Expand-Archive $zip $dest -Force

# Enable `import site` (so pip + Lib\site-packages load) and add site-packages.
# CRITICAL: also add `..` — embeddable ._pth entries resolve RELATIVE TO THE
# python.exe DIRECTORY (proven empirically; NOT relative to cwd as once believed),
# so `..` = the app dir one level up (…\Ripster), which is where `import ripster`
# must find its package. This makes ripster importable at interpreter startup,
# at the C level, independent of any runtime sys.path hack in app.py — the real
# fix for the "не нашёл пакет 'ripster' … Существует: True" launch crash.
$pth = Get-ChildItem (Join-Path $dest "python*._pth") | Select-Object -First 1
(Get-Content $pth.FullName) -replace '^#\s*import site', 'import site' | Set-Content $pth.FullName -Encoding ASCII
Add-Content $pth.FullName "Lib\site-packages"
Add-Content $pth.FullName ".."

Write-Host "[build] bootstrapping pip"
$getpip = Join-Path $tmp "get-pip.py"
Invoke-WebRequest "https://bootstrap.pypa.io/get-pip.py" -OutFile $getpip -UseBasicParsing
$py = Join-Path $dest "python.exe"

# Native commands below: stderr lines (pip notices) must NOT abort the script.
$prev = $ErrorActionPreference; $ErrorActionPreference = "Continue"
& $py $getpip --no-warn-script-location 2>&1 | ForEach-Object { Write-Host $_ }
Write-Host "[build] installing requirements into the embedded interpreter"
& $py -m pip install --no-input --disable-pip-version-check `
    -r (Join-Path $Root "requirements.txt") 2>&1 | ForEach-Object { Write-Host $_ }
$code = $LASTEXITCODE
$ErrorActionPreference = $prev
if ($code -ne 0) { throw "pip install into embedded Python failed (exit $code)" }

# Sanity: the app's critical imports must resolve in the bundled interpreter.
& $py -c "import fastapi,uvicorn,streamrip,deemix,mutagen,httpx,yaml,multipart,webview; print('[build] embedded interpreter OK')"
if ($LASTEXITCODE -ne 0) { throw "embedded interpreter import check failed" }

# Sanity: `import ripster` must resolve NATIVELY (via the `..` ._pth entry) from a
# FOREIGN cwd — this is exactly the user's launch scenario and the bug that the
# `..` entry fixes. Run from a dir that has no `ripster` of its own.
Push-Location $env:TEMP
try {
    & $py -c "import ripster; print('[build] native ripster import OK ->', ripster.__file__)"
    if ($LASTEXITCODE -ne 0) { throw "native 'import ripster' check failed — ._pth missing '..' app-dir entry" }
} finally { Pop-Location }
Write-Host "[build] done -> $dest"
