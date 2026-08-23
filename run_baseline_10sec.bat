@echo off
setlocal
cd /d "%~dp0"
".venv\Scripts\python.exe" main.py --max-seconds 10 --show-levels
pause
