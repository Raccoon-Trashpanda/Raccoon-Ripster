@echo off
setlocal
set "PATH=%~dp0bin;%PATH%"
start "" "%~dp0python\pythonw.exe" "%~dp0ripster_launcher.py" %*
