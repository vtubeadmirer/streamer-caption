@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" tools\set_vad_engine.py silero --speech-threshold 0.5
pause
