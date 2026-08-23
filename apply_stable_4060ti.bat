@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" tools\apply_preset.py recommended_4060ti_stable
pause
