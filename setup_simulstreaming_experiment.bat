@echo off
setlocal
cd /d "%~dp0"

echo SimulStreaming (accuracy-first engine) developer setup
echo This installs a separate CUDA PyTorch runtime and downloads large-v3-turbo.
echo Expect about 4 GB of downloads and 7 GB of disk use.
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

set "RUNTIME_ROOT=%CD%\experiments\simulstreaming-runtime"
set "RUNTIME=%RUNTIME_ROOT%\.venv"
set "MODEL_DIR=%RUNTIME_ROOT%\models"
set "REPO_DIR=%CD%\experiments\vendor\SimulStreaming"
rem The app points TORCH_HOME here too, so nothing is looked up online at run time.
set "TORCH_HOME=%RUNTIME_ROOT%\torch_home"

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

if not exist "%MODEL_DIR%" mkdir "%MODEL_DIR%"
echo [4/5] Preparing the Silero VAD model for the VAC...
if not exist "%MODEL_DIR%\silero_vad.jit" (
  set "SIMUL_MODELS=%MODEL_DIR%"
  "%RUNTIME%\Scripts\python.exe" -c "import os,pathlib,shutil,torch; torch.hub.load('snakers4/silero-vad','silero_vad',trust_repo=True); src=next(pathlib.Path(torch.hub.get_dir()).glob('snakers4_silero-vad_*/src/silero_vad/data/silero_vad.jit')); shutil.copy(src, os.environ['SIMUL_MODELS']); print('Silero VAD ready')"
  if errorlevel 1 exit /b %errorlevel%
) else (
  echo Silero VAD model already exists.
)

echo [5/5] Downloading the Whisper large-v3-turbo model if needed...
rem Keep the file name: SimulStreaming chooses its alignment heads by it.
if not exist "%MODEL_DIR%\large-v3-turbo.pt" (
  set "SIMUL_REPO=%REPO_DIR%"
  set "SIMUL_MODELS=%MODEL_DIR%"
  "%RUNTIME%\Scripts\python.exe" -c "import os,sys; sys.path.insert(0,os.environ['SIMUL_REPO']); from simulstreaming.whisper.simul_whisper.whisper import _MODELS,_download; print(_download(_MODELS['large-v3-turbo'],os.environ['SIMUL_MODELS'],False))"
  if errorlevel 1 exit /b %errorlevel%
) else (
  echo large-v3-turbo model already exists.
)

echo.
echo Setup complete. In the admin page choose the caption engine "accuracy first",
echo or run START_HERE.bat and choose AJ (check), then AI (run once).
pause
