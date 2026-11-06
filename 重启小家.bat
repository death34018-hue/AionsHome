@echo off
chcp 65001 >nul
title Restart AionsHome
cd /d "%~dp0"
echo.
echo 正在重启小家：关闭旧服务和旧启动窗口，再启动一份新的服务。
echo 启动完成后，本窗口会自动关闭，小家会在后台继续运行。
echo.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\restart-aionshome.ps1" %*
if errorlevel 1 (
    echo.
    echo 重启未完成，请把上面的报错发给我。
    pause
    exit /b 1
)
exit /b 0
