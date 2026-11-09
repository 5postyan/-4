@echo off
setlocal
cd /d "%~dp0"

set "VENV_PY=.venv\Scripts\python.exe"
if not exist "%VENV_PY%" goto :create_venv
goto :check_venv

:create_venv
set "PY_LAUNCHER="
where py >nul 2>&1
if not errorlevel 1 (
  py -3 -c "import sys; raise SystemExit(sys.version_info < (3, 10))" >nul 2>&1
  if not errorlevel 1 set "PY_LAUNCHER=py -3"
)
if not defined PY_LAUNCHER (
  where python >nul 2>&1
  if not errorlevel 1 (
    python -c "import sys; raise SystemExit(sys.version_info < (3, 10))" >nul 2>&1
    if not errorlevel 1 set "PY_LAUNCHER=python"
  )
)
if not defined PY_LAUNCHER (
  echo Python 3.10 or newer is required to create the project environment.
  goto :error
)
echo Creating the local Python environment...
%PY_LAUNCHER% -m venv .venv
if errorlevel 1 goto :error

:check_venv
"%VENV_PY%" -c "import sys; raise SystemExit(sys.version_info < (3, 10))" >nul 2>&1
if errorlevel 1 (
  echo The project virtual environment must contain Python 3.10 or newer.
  goto :error
)

echo Checking required packages...
"%VENV_PY%" -c "import numpy, pandas" >nul 2>&1
if errorlevel 1 (
  echo Installing missing packages...
  "%VENV_PY%" -m pip install --disable-pip-version-check -r requirements.txt
  if errorlevel 1 goto :error
)

echo Starting the local energy dashboard at http://127.0.0.1:8001/energy.html
echo If port 8001 is occupied, the app will select the next free port.
echo Press Ctrl+C in this window to stop the application.
"%VENV_PY%" local_entrypoint.py
if errorlevel 1 goto :error
exit /b 0

:error
echo Startup failed. Check the message above and logs\application.log.
pause
exit /b 1
