@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" tools\make_update_zip.py
