@echo off
REM Detached, RESILIENT SDK component installer.
REM Retries on any failure (router drops, timeouts) — sdkmanager resumes
REM partial downloads from .temp. Writes DONE_MARKER_0 only on real success.
set "JAVA_HOME=C:\Android\jre17\jdk-17.0.19+10-jre"
set "PATH=%JAVA_HOME%\bin;%PATH%"
set "SDKM=C:\Android\Sdk\cmdline-tools\latest\bin\sdkmanager.bat"
set "LOG=%~dp0sdk_install.log"

echo [%date% %time%] resilient SDK install started > "%LOG%"
set /a TRY=0

:retry
set /a TRY+=1
echo. >> "%LOG%"
echo [%date% %time%] === attempt %TRY% === >> "%LOG%"
echo y| call "%SDKM%" "platform-tools" "emulator" "system-images;android-30;google_apis;x86_64" >> "%LOG%" 2>&1
set RC=%errorlevel%
echo [%date% %time%] attempt %TRY% exit=%RC% >> "%LOG%"
if not "%RC%"=="0" (
    echo [%date% %time%] retrying in 15s... >> "%LOG%"
    timeout /t 15 /nobreak >nul
    goto retry
)
echo DONE_MARKER_0 >> "%LOG%"
