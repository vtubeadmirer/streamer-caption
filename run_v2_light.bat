@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" tools\run_supervised.py
pause