@echo off
setlocal
cd /d "%~dp0"
".venv\Scripts\python.exe" tools\transcribe_wav.py samples\test_clip.wav
pause

