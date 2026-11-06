@echo off
chcp 65001 >nul
cd /d "%~dp0"

set "PREVIEW_PYTHON="
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -c "import sys" >nul 2>nul
  if not errorlevel 1 set "PREVIEW_PYTHON=%~dp0.venv\Scripts\python.exe"
)
if not defined PREVIEW_PYTHON if exist "%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" set "PREVIEW_PYTHON=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
if not defined PREVIEW_PYTHON (
  python -c "import sys" >nul 2>nul
  if not errorlevel 1 set "PREVIEW_PYTHON=python"
)
if not defined PREVIEW_PYTHON (
  echo 未找到可用的 Python。请先安装 Python 3.11 或更高版本。
  pause
  exit /b 1
)

"%PREVIEW_PYTHON%" "scripts\elevenlabs_tts_preview.py"
if errorlevel 1 pause
