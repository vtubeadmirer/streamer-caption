@echo off
setlocal
cd /d "%~dp0"
set /p path=Audio file path: 
".venv\Scripts\python.exe" tools\import_audio.py "%path%" --out samples\test_clip.wav
pause
