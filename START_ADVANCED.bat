@echo off
setlocal
cd /d "%~dp0"

:menu
cls
echo streamer-caption-v2
echo.
echo 1. Health check
echo 2. Check microphone devices
echo 3. Probe input device levels
echo 4. Record test clip
echo 5. Analyze test clip
echo 6. Transcribe test clip
echo 7. Benchmark test clip warm
echo 8. Summarize latest benchmark
echo 9. Run live baseline
echo A. Run live baseline for 10 seconds with levels
echo B. Run v2_light
echo C. Run v2_light for 10 seconds with levels
echo D. Apply RTX 4060 Ti 16GB profile
echo E. Apply RTX 5070 Ti quality profile
echo F. Collect diagnostics report
echo G. Summarize latest memory log
echo H. Benchmark v2_light streaming test clip
echo I. Set VAD to Silero
echo J. Set VAD to Energy
echo K. Tune VAD from test clip
echo L. Apply VAD tuning from test clip
echo M. Run v2_light 10 minute stability test
echo N. Summarize latest session log
echo O. Record-tune-benchmark workflow
echo P. Tune-benchmark existing clip
echo Q. Compare 4060/5070 profiles
echo R. Progress report
echo S. Apply recommended 4060 Ti preset
echo T. Final check
echo U. Synthetic pipeline check
echo V. Apply stable 4060 Ti preset
echo W. Import audio file to test clip
echo X. Import latest audio from samples\incoming
echo Y. Backup config
echo Z. Make handoff bundle
echo AA. Benchmark looped 10 minute test clip
echo AB. Backup user settings
echo AC. Restore user settings
echo AD. Make update zip that keeps user settings
echo AE. Make support bundle for Codex
echo AF. Run listener mode (capture PC playback)
echo AG. List microphone and PC playback devices
echo AH. Install or update dependencies
echo AI. Run SimulStreaming experiment
echo AJ. Check SimulStreaming experiment readiness
echo AK. Setup SimulStreaming experiment
echo AL. Open live validation marker
echo AM. Validate latest broadcast report
echo AN. Open broadcast archive
echo 0. Exit
echo.
set /p choice=Choose: 

if "%choice%"=="1" ".venv\Scripts\python.exe" tools\health_check.py
if "%choice%"=="2" ".venv\Scripts\python.exe" mic_monitor.py --list-devices
if "%choice%"=="3" ".venv\Scripts\python.exe" tools\probe_input_devices.py --seconds 2
if "%choice%"=="4" ".venv\Scripts\python.exe" tools\record_wav.py --seconds 10 --out samples\test_clip.wav
if "%choice%"=="5" ".venv\Scripts\python.exe" tools\analyze_wav.py samples\test_clip.wav
if "%choice%"=="6" ".venv\Scripts\python.exe" tools\transcribe_wav.py samples\test_clip.wav
if "%choice%"=="7" ".venv\Scripts\python.exe" tools\benchmark_wav.py samples\test_clip.wav --repeat 4 --warm --discard-first
if "%choice%"=="8" ".venv\Scripts\python.exe" tools\summarize_benchmark.py --latest
if "%choice%"=="9" ".venv\Scripts\python.exe" main.py
if /I "%choice%"=="A" ".venv\Scripts\python.exe" main.py --max-seconds 10 --show-levels
if /I "%choice%"=="B" ".venv\Scripts\python.exe" tools\run_supervised.py
if /I "%choice%"=="C" ".venv\Scripts\python.exe" main.py --v2-light --max-seconds 10 --show-levels
if /I "%choice%"=="D" ".venv\Scripts\python.exe" tools\apply_profile.py rtx_4060ti_16gb
if /I "%choice%"=="E" ".venv\Scripts\python.exe" tools\apply_profile.py rtx_5070ti_quality
if /I "%choice%"=="F" ".venv\Scripts\python.exe" tools\collect_diagnostics.py
if /I "%choice%"=="G" ".venv\Scripts\python.exe" tools\summarize_memory.py --latest
if /I "%choice%"=="H" ".venv\Scripts\python.exe" tools\benchmark_streaming_wav.py samples\test_clip.wav
if /I "%choice%"=="I" ".venv\Scripts\python.exe" tools\set_vad_engine.py silero --speech-threshold 0.5
if /I "%choice%"=="J" ".venv\Scripts\python.exe" tools\set_vad_engine.py energy
if /I "%choice%"=="K" ".venv\Scripts\python.exe" tools\tune_voice_wav.py samples\test_clip.wav
if /I "%choice%"=="L" ".venv\Scripts\python.exe" tools\tune_voice_wav.py samples\test_clip.wav --apply
if /I "%choice%"=="M" call run_v2_light_10min.bat
if /I "%choice%"=="N" ".venv\Scripts\python.exe" tools\summarize_session.py --latest
if /I "%choice%"=="O" ".venv\Scripts\python.exe" tools\record_tune_benchmark.py --seconds 10 --apply-tuning
if /I "%choice%"=="P" ".venv\Scripts\python.exe" tools\record_tune_benchmark.py --skip-record --apply-tuning
if /I "%choice%"=="Q" ".venv\Scripts\python.exe" tools\compare_profiles.py samples\test_clip.wav
if /I "%choice%"=="R" ".venv\Scripts\python.exe" tools\progress_report.py
if /I "%choice%"=="S" ".venv\Scripts\python.exe" tools\apply_preset.py recommended_4060ti_v2_light
if /I "%choice%"=="T" ".venv\Scripts\python.exe" tools\final_check.py
if /I "%choice%"=="U" call synthetic_pipeline_check.bat
if /I "%choice%"=="V" ".venv\Scripts\python.exe" tools\apply_preset.py recommended_4060ti_stable
if /I "%choice%"=="W" call import_audio_to_test_clip.bat
if /I "%choice%"=="X" call import_latest_audio_to_test_clip.bat
if /I "%choice%"=="Y" call backup_config.bat
if /I "%choice%"=="Z" call make_handoff_bundle.bat
if /I "%choice%"=="AA" call benchmark_looped_10min.bat
if /I "%choice%"=="AB" call backup_user_settings.bat
if /I "%choice%"=="AC" call restore_user_settings.bat
if /I "%choice%"=="AD" call make_update_zip.bat
if /I "%choice%"=="AE" call make_support_bundle.bat
if /I "%choice%"=="AF" ".venv\Scripts\python.exe" tools\run_supervised.py -- --audio-source system
if /I "%choice%"=="AG" ".venv\Scripts\python.exe" tools\list_audio_sources.py
if /I "%choice%"=="AH" call install_update_dependencies.bat
if /I "%choice%"=="AI" call run_simulstreaming_experiment.bat
if /I "%choice%"=="AJ" call simulstreaming_probe.bat
if /I "%choice%"=="AK" call setup_simulstreaming_experiment.bat
if /I "%choice%"=="AL" start "" "http://127.0.0.1:8766/validate"
if /I "%choice%"=="AM" call VALIDATE_LATEST_BROADCAST.bat
if /I "%choice%"=="AN" start "" "http://127.0.0.1:8766/history"
if "%choice%"=="0" exit /b 0

echo.
pause
goto menu
