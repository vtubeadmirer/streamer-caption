@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" main.py --v2-light --max-seconds 1800 --show-levels
echo.
".venv\Scripts\python.exe" tools\summarize_memory.py --latest
echo.
".venv\Scripts\python.exe" tools\summarize_session.py --latest
echo.
".venv\Scripts\python.exe" tools\stability_verdict.py
pause
