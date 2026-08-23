@echo off
setlocal
cd /d "%~dp0"
".venv\Scripts\python.exe" tools\probe_input_devices.py --seconds 2
pause

