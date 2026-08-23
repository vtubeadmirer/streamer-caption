@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
echo Streamer Caption one-click setup is starting...
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\install.ps1"
if errorlevel 1 (
  echo.
  echo Installation failed. See the message above.
  pause
  exit /b 1
)
echo.
echo Installation completed.
pause
