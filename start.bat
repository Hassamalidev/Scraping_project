@echo off
rem Tradebook launcher: double-click this file to start everything.
cd /d "%~dp0"

echo Starting the database (Docker Desktop must be running)...
docker compose up -d --wait
if errorlevel 1 (
  echo.
  echo Could not start the database. Open Docker Desktop, wait until it says "running", then try again.
  pause
  exit /b 1
)

echo Updating the database...
uv run prodata db upgrade
if errorlevel 1 (
  pause
  exit /b 1
)

echo.
echo Tradebook is starting at http://127.0.0.1:8000
echo Keep this window open while you use the app. Close it to stop.
echo.
start "" http://127.0.0.1:8000
uv run prodata web
pause
