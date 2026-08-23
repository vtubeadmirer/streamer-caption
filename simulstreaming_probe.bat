@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" tools\simulstreaming_probe.py
pause