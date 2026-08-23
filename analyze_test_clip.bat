@echo off
setlocal
cd /d "%~dp0"
".venv\Scripts\python.exe" tools\analyze_wav.py samples\test_clip.wav
pause

