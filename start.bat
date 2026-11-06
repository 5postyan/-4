@echo off
setlocal
cd /d "%~dp0"

where docker >nul 2>&1
if errorlevel 1 (
  echo Docker Desktop is not installed or Docker is not on PATH.
  echo Install and start Docker Desktop, then run this file again.
  pause
  exit /b 1
)

docker info >nul 2>&1
if errorlevel 1 (
  echo Docker Desktop is installed but not running. Start it and retry.
  pause
  exit /b 1
)

echo Building and starting the launch prototype...
docker compose up --build -d
if errorlevel 1 (
  echo Container startup failed. See the error above.
  pause
  exit /b 1
)

start "Launch Prototype" "http://127.0.0.1:8000"
echo Dashboard: http://127.0.0.1:8000
echo Application log: logs\application.log
echo Press Ctrl+C to stop following logs. The container keeps running.
docker compose logs -f --tail=100 launch-prototype
