@echo off
chcp 65001 >nul
title Restart Home and QQ
cd /d "%~dp0"
if /I "%~1"=="-CheckOnly" (
    powershell.exe -NoProfile -File "%~dp0aion-chat\restart-home-qq.ps1" -CheckOnly
    exit /b
)
echo.
echo 正在重启小家和 QQ，请稍等。正在生成的回复可能中断，无需等待群聊停止。
echo.
powershell.exe -NoProfile -File "%~dp0aion-chat\restart-home-qq.ps1" %*
if errorlevel 1 (
    echo.
    echo 重启未完成，请保留这个窗口，把上面的报错发给我。
) else (
    echo.
    echo 重启完成，现在可以刷新手机上的小家首页了。
)
echo.
pause
