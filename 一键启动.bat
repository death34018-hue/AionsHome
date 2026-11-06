@echo off
chcp 65001 >nul
call "%~dp0重启小家.bat" %*
exit /b %errorlevel%
