@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" tools\apply_profile.py rtx_4060ti_16gb
pause
