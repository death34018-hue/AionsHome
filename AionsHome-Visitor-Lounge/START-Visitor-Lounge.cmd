@echo off
setlocal
cd /d "%~dp0"

echo Starting Visitor Lounge...
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start.ps1"
set "lounge_exit_code=%ERRORLEVEL%"

if not "%lounge_exit_code%"=="0" (
    echo.
    echo Visitor Lounge could not be started. The window will stay open so you can read the error.
    pause
    exit /b %lounge_exit_code%
)

echo.
echo Visitor Lounge is ready:
echo   Public: https://visitor.aionshome.com
echo   Admin:  http://127.0.0.1:8002/admin
exit /b 0
