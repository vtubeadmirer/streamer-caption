@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" tools\make_synthetic_voice_clip.py --out samples\synthetic_voice.wav
".venv\Scripts\python.exe" tools\tune_voice_wav.py samples\synthetic_voice.wav --vad-engine energy
".venv\Scripts\python.exe" tools\benchmark_streaming_wav.py samples\synthetic_voice.wav --vad-engine energy
pause
