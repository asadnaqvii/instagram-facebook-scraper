@echo off
REM Launch TWO Chrome instances for Meta Scraper
REM  - Port 9222: Facebook (log into facebook.com here)
REM  - Port 9223: Instagram (log into instagram.com here)

echo ==========================================
echo  Meta Scraper - Chrome Launcher
echo ==========================================
echo.

REM Kill existing debug instances
taskkill /F /IM chrome.exe 2>nul
timeout /t 2 /nobreak >nul

REM Find Chrome
set "CHROME_EXE="
if exist "%ProgramFiles%\Google\Chrome\Application\chrome.exe" (
    set "CHROME_EXE=%ProgramFiles%\Google\Chrome\Application\chrome.exe"
) else if exist "%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe" (
    set "CHROME_EXE=%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"
) else if exist "%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe" (
    set "CHROME_EXE=%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"
) else (
    echo ERROR: Chrome not found!
    pause
    exit /b 1
)

REM Profile directories
set "FB_PROFILE=%LOCALAPPDATA%\Google\Chrome-MetaScraper-FB"
set "IG_PROFILE=%LOCALAPPDATA%\Google\Chrome-MetaScraper-IG"

if not exist "%FB_PROFILE%" mkdir "%FB_PROFILE%" 2>nul
if not exist "%IG_PROFILE%" mkdir "%IG_PROFILE%" 2>nul

echo Launching Facebook Chrome (port 9222)...
start "" "%CHROME_EXE%" --remote-debugging-port=9222 --user-data-dir="%FB_PROFILE%" --no-first-run --no-default-browser-check --window-position=0,0 --window-size=960,800 https://www.facebook.com

timeout /t 2 /nobreak >nul

echo Launching Instagram Chrome (port 9223)...
start "" "%CHROME_EXE%" --remote-debugging-port=9223 --user-data-dir="%IG_PROFILE%" --no-first-run --no-default-browser-check --window-position=960,0 --window-size=960,800 https://www.instagram.com

echo.
echo ==========================================
echo  Both browsers launched!
echo ==========================================
echo.
echo  Facebook:  port 9222 (left window)
echo  Instagram: port 9223 (right window)
echo.
echo  1. Log into Facebook in the LEFT window
echo  2. Log into Instagram in the RIGHT window
echo  3. Open http://localhost:8090/dashboard.html
echo.
pause
