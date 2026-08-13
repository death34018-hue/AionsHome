@echo off
setlocal
cd /d "%~dp0"

echo Stopping Visitor Lounge...
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\stop.ps1"
set "lounge_exit_code=%ERRORLEVEL%"

if not "%lounge_exit_code%"=="0" (
    echo.
    echo Visitor Lounge could not be stopped cleanly. The window will stay open so you can read the error.
    pause
    exit /b %lounge_exit_code%
)

echo.
echo Visitor Lounge is fully stopped. Shared Cloudflared was left running.
exit /b 0
