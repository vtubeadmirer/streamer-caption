@echo off
setlocal
cd /d "%~dp0"
".venv\Scripts\python.exe" tools\apply_vad_threshold.py samples\test_clip.wav --from-analysis
pause

