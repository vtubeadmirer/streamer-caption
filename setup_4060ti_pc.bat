@echo off
setlocal
cd /d "%~dp0"

echo [1/4] Checking supported Python...
set "PYTHON_CMD="
py -3.13 --version >nul 2>&1
if not errorlevel 1 set "PYTHON_CMD=py -3.13"
if not defined PYTHON_CMD (
  py -3.12 --version >nul 2>&1
  if not errorlevel 1 set "PYTHON_CMD=py -3.12"
)
if not defined PYTHON_CMD (
  py -3.11 --version >nul 2>&1
  if not errorlevel 1 set "PYTHON_CMD=py -3.11"
)
if not defined PYTHON_CMD (
  echo Python 3.11, 3.12, or 3.13 was not found.
  echo Install one of those versions, then run this file again.
  exit /b 1
)
%PYTHON_CMD% --version

echo.
echo [2/4] Creating a clean virtual environment...
%PYTHON_CMD% -m venv --clear .venv
if errorlevel 1 exit /b %errorlevel%

echo.
echo [3/4] Installing dependencies...
".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 exit /b %errorlevel%
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 exit /b %errorlevel%

echo.
echo [4/4] Running health check...
".venv\Scripts\python.exe" tools\health_check.py
if errorlevel 1 exit /b %errorlevel%

echo.
echo Setup complete. Run START_HERE.bat next.
