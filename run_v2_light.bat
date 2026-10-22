@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" tools\run_supervised.py
if errorlevel 76 if not errorlevel 77 exit /b 76
pause