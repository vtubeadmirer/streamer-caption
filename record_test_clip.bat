@echo off
setlocal
cd /d "%~dp0"
".venv\Scripts\python.exe" tools\record_wav.py --seconds 10 --out samples\test_clip.wav
pause

