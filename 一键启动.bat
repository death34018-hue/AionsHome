@echo off
chcp 65001 >nul
title Aion Chat
cd /d "%~dp0"

if exist "%~dp0.venv\Scripts\activate.bat" (
    call "%~dp0.venv\Scripts\activate.bat"
)

echo ========================================
echo   Aion Chat  正在启动...
echo   http://localhost:8080
echo   关闭此窗口即停止服务
echo ========================================

cd /d "%~dp0aion-chat"
python -u main.py
pause

import subprocess
subprocess.run([
    "powershell", "-Verb", "RunAs", "-ArgumentList",
    "netsh advfirewall firewall add rule name='MyApp 8080' dir=in action=allow protocol=TCP localport=8080"
])