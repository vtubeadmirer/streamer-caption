@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" tools\list_audio_sources.py
pause