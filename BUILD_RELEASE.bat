@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" tools\build_public_release.py
) else (
  py -3.12 tools\build_public_release.py
)
if errorlevel 1 pause
