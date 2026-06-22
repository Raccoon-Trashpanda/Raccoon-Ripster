@echo off
title Ripster - keep this window open
cd /d "%~dp0"
echo.
echo   Starting Ripster... a browser tab will open in a few seconds.
echo   Keep THIS window open while you use Ripster (closing it stops the server).
echo   Address: http://127.0.0.1:7799
echo.
start "" powershell -NoProfile -WindowStyle Hidden -Command "Start-Sleep 8; Start-Process 'http://127.0.0.1:7799'"
"%~dp0python\python.exe" app.py
