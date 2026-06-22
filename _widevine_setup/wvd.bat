@echo off
REM Widevine L3 console — single entry point for the device.wvd pipeline.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0wvd_console.ps1"
