@echo off
setlocal
cd /d "%~dp0"

:menu
cls
echo ============================================================
echo  Streamer Caption - Easy Start
echo ============================================================
echo.
echo  1. Start live captions
echo  2. Open admin page
echo  3. Caption PC playback / viewer mode
echo  4. Auto GPU preset for RTX 40/50 Series
echo  5. Check audio devices
echo  6. Validate latest broadcast
echo  7. Make support bundle for Codex
echo  8. Advanced tools
echo  9. Start microphone + PC dual captions (beta)
echo  R. Open 15-minute recap overlay (OBS browser source URL)
echo  T. Faster captions: switch to large-v3-turbo (downloads 1.6 GB once)
echo  0. Exit
echo.
echo  First run: choose 4 once, then choose 1.
echo  Korean help is available in the admin page.
echo.
set /p choice=Choose: 

if "%choice%"=="1" goto live
if "%choice%"=="2" goto admin
if "%choice%"=="3" goto listener
if "%choice%"=="4" goto gpu_auto
if "%choice%"=="5" goto devices
if "%choice%"=="6" goto validate
if "%choice%"=="7" goto support
if "%choice%"=="8" goto advanced
if "%choice%"=="9" goto dual
if /i "%choice%"=="R" goto recap
if /i "%choice%"=="T" goto turbo
if "%choice%"=="0" goto end
goto menu

:live
call run_v2_light.bat
if errorlevel 76 if not errorlevel 77 exit /b 76
pause
goto menu

:admin
start "" "http://127.0.0.1:8766/admin"
echo If the page does not open, start live captions first with option 1.
echo GPU-free preview is available with PREVIEW_ADMIN.bat.
pause
goto menu

:listener
call run_listener_mode.bat
pause
goto menu

:gpu_auto
call apply_auto_gpu_preset.bat
pause
goto menu

:devices
call list_audio_sources.bat
pause
goto menu

:validate
call VALIDATE_LATEST_BROADCAST.bat
pause
goto menu

:support
call make_support_bundle.bat
pause
goto menu

:dual
call run_dual_input.bat
pause
goto menu

:turbo
call apply_turbo_profile.bat
goto menu

:recap
start "" "http://127.0.0.1:8766/recap"
echo Add this URL as an OBS browser source: http://127.0.0.1:8766/recap
echo Recaps appear after each 15-minute block while live captions are running.
pause
goto menu

:advanced
call START_ADVANCED.bat
goto menu

:end
endlocal