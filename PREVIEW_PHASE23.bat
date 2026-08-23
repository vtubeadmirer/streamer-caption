@echo off
setlocal
cd /d "%~dp0"
echo Starting Phase 2/3 history preview without GPU or microphone...
py -3 tools\phase23_demo.py --serve
if errorlevel 1 (
  echo.
  echo Preview failed. Make sure Python is installed.
  pause
)

