@echo off
REM ============================================================
REM  Strat-Watch Meta — one-click launcher (Windows)
REM  Fresh computer? This installs everything, opens two Chrome
REM  windows for you to log in, and starts the dashboard.
REM  Ollama is NOT required.
REM ============================================================
setlocal
cd /d "%~dp0"
title Strat-Watch Meta

echo.
echo  ============================================================
echo    Strat-Watch Meta  -  Instagram ^& Facebook OSINT
echo  ============================================================
echo.

REM --- 1. Check Python ---
where python >nul 2>&1
if errorlevel 1 (
  echo  [X] Python is not installed or not on PATH.
  echo      Install Python 3.10+ from https://www.python.org/downloads/
  echo      ^(tick "Add Python to PATH" during install^), then re-run this.
  echo.
  pause
  exit /b 1
)
echo  [1/5] Python found.

REM --- 2. Install dependencies (first run only takes a minute) ---
echo  [2/5] Installing Python packages ^(first run only^)...
python -m pip install --quiet --disable-pip-version-check -r meta_osint\requirements.txt
if errorlevel 1 (
  echo  [X] Failed to install packages. Check your internet connection.
  pause
  exit /b 1
)

REM --- 3. Install the Playwright browser engine (idempotent) ---
echo  [3/5] Ensuring Playwright browser is installed...
python -m playwright install chromium >nul 2>&1

REM --- 4. Launch a logged-in Chrome per platform ---
echo  [4/5] Opening Chrome windows for Facebook ^(9222^) and Instagram ^(9223^)...
echo.
echo        ^>^>^> LOG IN to Facebook and Instagram in the two windows that open.
echo        ^>^>^> Leave both windows OPEN. Complete any 2FA / "confirm it's you".
echo.
call meta_osint\scripts\start_chrome_facebook.bat
call meta_osint\scripts\start_chrome_instagram.bat

echo.
echo        Waiting 20 seconds for you to start logging in...
timeout /t 20 /nobreak >nul

REM --- 5. Start the dashboard ---
echo  [5/5] Starting the dashboard...
echo.
echo  ============================================================
echo    Dashboard:  http://localhost:5000
echo    ^(opening your browser now^)
echo.
echo    Tip: run  python -m meta_osint.main diagnose   in another
echo         window to confirm both Chrome sessions are logged in.
echo  ============================================================
echo.
start "" http://localhost:5000
python -m meta_osint.main serve --port 5000

pause
