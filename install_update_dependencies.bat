@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo Project virtual environment was not found.
  echo Run setup_4060ti_pc.bat first.
  pause
  exit /b 1
)

echo Installing or updating project dependencies...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
  echo Dependency update failed.
  pause
  exit /b %errorlevel%
)

echo Dependency update complete.
pause