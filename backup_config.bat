@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" tools\backup_config.py
pause
