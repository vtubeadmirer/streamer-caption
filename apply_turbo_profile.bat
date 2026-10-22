@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" tools\apply_profile.py rtx_4060ti_turbo
echo The large-v3-turbo model (about 1.6 GB) downloads automatically on the next caption start.
pause
