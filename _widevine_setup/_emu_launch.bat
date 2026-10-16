@echo off
REM Detached headless emulator launch for the Widevine AVD.
set "ANDROID_SDK_ROOT=C:\Android\Sdk"
set "ANDROID_AVD_HOME=%USERPROFILE%\.android\avd"
set "PATH=C:\Android\Sdk\platform-tools;C:\Android\Sdk\emulator;%PATH%"
REM No -wipe-data: keeps frida-server + Widevine provisioning across reboots.
"C:\Android\Sdk\emulator\emulator.exe" -avd wvd -no-window -no-snapshot -no-boot-anim -gpu swiftshader_indirect -no-audio > "%~dp0emu.log" 2>&1
