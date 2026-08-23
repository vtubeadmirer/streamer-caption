@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" tools\apply_profile.py rtx_5070ti_quality
pause
