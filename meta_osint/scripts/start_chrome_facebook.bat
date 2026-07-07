@echo off
REM Launch a dedicated Chrome for Facebook scraping, exposed over CDP on 9222.
REM Log into facebook.com in THIS window once; the session persists in its own
REM user-data dir so meta_osint can connect without ever handling your password.

set CHROME="C:\Program Files\Google\Chrome\Application\chrome.exe"
if not exist %CHROME% set CHROME="C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"

start "" %CHROME% ^
  --remote-debugging-port=9222 ^
  --user-data-dir="%LOCALAPPDATA%\MetaOsint\chrome-facebook" ^
  --no-first-run ^
  --no-default-browser-check ^
  https://www.facebook.com/

echo Chrome (Facebook) started on CDP port 9222.
echo Log in to Facebook in the opened window, then run the scraper.
