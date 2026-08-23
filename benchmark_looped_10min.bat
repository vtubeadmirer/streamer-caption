@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" tools\make_looped_wav.py samples\test_clip.wav --seconds 600 --out samples\test_clip_loop_10min.wav
if errorlevel 1 exit /b %errorlevel%
".venv\Scripts\python.exe" tools\benchmark_streaming_wav.py samples\test_clip_loop_10min.wav
