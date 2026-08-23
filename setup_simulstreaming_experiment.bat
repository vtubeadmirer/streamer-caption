@echo off
setlocal
cd /d "%~dp0"

echo SimulStreaming experimental setup
echo This installs a separate CUDA PyTorch runtime and downloads large-v3.
echo Expect several GB of downloads and disk use.
echo.

set "PYTHON_CMD="
py -3.12 --version >nul 2>&1
if not errorlevel 1 set "PYTHON_CMD=py -3.12"
if not defined PYTHON_CMD (
  py -3.11 --version >nul 2>&1
  if not errorlevel 1 set "PYTHON_CMD=py -3.11"
)
if not defined PYTHON_CMD (
  py -3.13 --version >nul 2>&1
  if not errorlevel 1 set "PYTHON_CMD=py -3.13"
)
if not defined PYTHON_CMD (
  echo Python 3.11, 3.12, or 3.13 was not found.
  exit /b 1
)

set "RUNTIME=experiments\simulstreaming-runtime\.venv"
set "MODEL_DIR=%CD%\experiments\simulstreaming-runtime\models"
set "REPO_DIR=%CD%\experiments\vendor\SimulStreaming"

if not exist "%RUNTIME%\Scripts\python.exe" (
  echo [1/5] Creating isolated Python environment...
  %PYTHON_CMD% -m venv "%RUNTIME%"
  if errorlevel 1 exit /b %errorlevel%
) else (
  echo [1/5] Isolated Python environment already exists.
)

echo [2/5] Installing SimulStreaming and CUDA dependencies...
"%RUNTIME%\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 exit /b %errorlevel%
"%RUNTIME%\Scripts\python.exe" -m pip install -r requirements_simulstreaming_windows.txt
if errorlevel 1 exit /b %errorlevel%

echo [3/5] Verifying CUDA...
"%RUNTIME%\Scripts\python.exe" -c "import torch; assert torch.cuda.is_available(), 'CUDA is not available'; print(torch.__version__, torch.cuda.get_device_name(0))"
if errorlevel 1 exit /b %errorlevel%

echo [4/5] Preparing Silero VAC cache...
"%RUNTIME%\Scripts\python.exe" -c "import torch; torch.hub.load('snakers4/silero-vad','silero_vad',trust_repo=True); print('Silero VAC ready')"
if errorlevel 1 exit /b %errorlevel%

echo [5/5] Downloading Whisper large-v3 model if needed...
if not exist "%MODEL_DIR%\large-v3.pt" mkdir "%MODEL_DIR%"
if not exist "%MODEL_DIR%\large-v3.pt" (
  set "SIMUL_REPO=%REPO_DIR%"
  set "SIMUL_MODELS=%MODEL_DIR%"
  "%RUNTIME%\Scripts\python.exe" -c "import os,sys; sys.path.insert(0,os.environ['SIMUL_REPO']); from simulstreaming.whisper.simul_whisper.whisper import _MODELS,_download; print(_download(_MODELS['large-v3'],os.environ['SIMUL_MODELS'],False))"
  if errorlevel 1 exit /b %errorlevel%
) else (
  echo large-v3 model already exists.
)

echo.
echo SimulStreaming experiment setup complete.
echo Run START_HERE.bat and choose AJ, then AI.
pause