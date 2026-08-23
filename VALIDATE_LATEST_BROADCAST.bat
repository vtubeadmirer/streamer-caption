@echo off
setlocal
cd /d "%~dp0"
echo Building latest broadcast validation report...
".venv\Scripts\python.exe" tools\broadcast_validation.py --latest
set "EXIT_CODE=%ERRORLEVEL%"
echo.
if "%EXIT_CODE%"=="0" (
  echo Validation report completed.
) else (
  echo Validation found a failure or could not run. Check the message above.
)
pause
exit /b %EXIT_CODE%
