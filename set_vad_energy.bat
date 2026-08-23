@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" tools\set_vad_engine.py energy
pause
