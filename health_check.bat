@echo off
setlocal
cd /d "%~dp0"
".venv\Scripts\python.exe" tools\health_check.py
pause

