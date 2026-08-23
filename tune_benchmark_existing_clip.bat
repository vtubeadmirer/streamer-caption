@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" tools\record_tune_benchmark.py --skip-record --apply-tuning
pause
