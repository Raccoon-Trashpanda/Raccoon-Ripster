@echo off
REM Ripster - launcher (Windows).
REM 1. First run: create venv + install deps if .venv is missing.
REM 2. If pywebview is installed, open Ripster in its own native window.
REM 3. Otherwise fall back to the default browser + plain app.py.

cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [setup] First run - creating virtual environment...
    py -3 -m venv .venv 2>nul || python -m venv .venv
    .venv\Scripts\python.exe -m pip install --upgrade pip
    .venv\Scripts\python.exe -m pip install -r requirements.txt
)

if not exist "config.yaml" (
    if exist "config.example.yaml" copy "config.example.yaml" "config.yaml" >nul
)

REM Free port 7799 if a previous instance is still listening.
powershell -NoProfile -Command "Get-NetTCPConnection -LocalPort 7799 -State Listen -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }"

REM Prefer the native-window launcher (pywebview); else open the browser.
.venv\Scripts\python.exe -c "import webview" >nul 2>&1
if not errorlevel 1 (
    start "" ".venv\Scripts\pythonw.exe" "ripster_launcher.py"
    exit /b 0
)

echo pywebview not available - opening in your browser instead.
start "" "http://127.0.0.1:7799"
.venv\Scripts\python.exe app.py
pause
