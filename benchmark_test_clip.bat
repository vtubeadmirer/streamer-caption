@echo off
setlocal
cd /d "%~dp0"
".venv\Scripts\python.exe" tools\benchmark_wav.py samples\test_clip.wav --repeat 3
pause

