@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" tools\import_latest_audio.py
pause
