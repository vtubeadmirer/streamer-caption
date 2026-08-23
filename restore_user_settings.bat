@echo off
cd /d "%~dp0"
set /p bundle=User settings zip path: 
".venv\Scripts\python.exe" tools\user_settings_bundle.py --restore "%bundle%"
