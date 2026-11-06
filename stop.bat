@echo off
setlocal
cd /d "%~dp0"
where docker >nul 2>&1
if errorlevel 1 (
  echo Docker is not available on PATH.
  pause
  exit /b 1
)
docker compose down
