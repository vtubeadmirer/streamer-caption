@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" tools\collect_diagnostics.py --model-load
pause
