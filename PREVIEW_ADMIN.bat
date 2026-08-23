@echo off
setlocal
cd /d "%~dp0"
echo Starting admin UI preview without GPU or microphone...
py -3 tools/preview_admin.py
if errorlevel 1 (
  echo.
  echo Preview failed. Make sure Python is installed.
  pause
)
