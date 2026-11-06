@echo off
title Close AionsHome
cd /d "%~dp0"

echo ========================================
echo   Closing AionsHome processes...
echo ========================================

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\stop-aionshome.ps1" %*
if errorlevel 1 (
    echo.
    echo Some processes could not be stopped. Please check the error above.
    pause
    exit /b 1
)

echo.
echo Done. You can start AionsHome again with start script.
echo.
pause
