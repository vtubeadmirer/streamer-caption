@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" tools\run_supervised.py -- --audio-source both
pause