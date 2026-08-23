@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" tools\compare_profiles.py samples\test_clip.wav
pause
