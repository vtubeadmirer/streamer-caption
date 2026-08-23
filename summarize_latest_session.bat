@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" tools\summarize_session.py --latest
pause
