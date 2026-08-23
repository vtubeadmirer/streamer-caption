@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" tools\benchmark_streaming_wav.py samples\test_clip.wav
pause
