@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" tools\collect_diagnostics.py
".venv\Scripts\python.exe" tools\make_support_bundle.py
