@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" tools\stability_verdict.py
pause
