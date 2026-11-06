@echo off
chcp 65001 >nul
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" "scripts\minimax_tts_preview.py"
) else (
  python "scripts\minimax_tts_preview.py"
)

if errorlevel 1 pause
