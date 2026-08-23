@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" main.py --v2-light --max-seconds 10 --show-levels
pause
