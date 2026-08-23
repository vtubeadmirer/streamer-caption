@echo off
setlocal
cd /d "%~dp0"
".venv\Scripts\python.exe" tools\summarize_benchmark.py --latest
pause

