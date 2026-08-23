@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" tools\make_handoff_bundle.py
pause
