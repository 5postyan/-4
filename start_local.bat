@echo off
setlocal
cd /d "%~dp0"

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
  echo Python 3.10 or newer was not found. Install Python and enable the Python launcher or PATH option.
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo Creating the local Python environment...
  %PY_LAUNCHER% -m venv .venv
  if errorlevel 1 goto :error
)

echo Installing or checking required packages...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto :error

echo Starting the local dashboard at http://127.0.0.1:8001
echo Press Ctrl+C in this window to stop the application.
".venv\Scripts\python.exe" local_entrypoint.py
if errorlevel 1 goto :error
exit /b 0

:error
echo Startup failed. Check the message above and logs\application.log.
pause
exit /b 1

