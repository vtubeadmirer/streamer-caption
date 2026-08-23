@echo off
setlocal
cd /d "%~dp0"
".venv\Scripts\python.exe" tools\voice_test_workflow.py --seconds 10 --out samples\test_clip.wav
pause

