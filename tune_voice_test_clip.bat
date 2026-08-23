@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" tools\tune_voice_wav.py samples\test_clip.wav
pause
