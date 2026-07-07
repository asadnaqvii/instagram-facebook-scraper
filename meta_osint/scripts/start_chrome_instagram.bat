@echo off
REM Launch a dedicated Chrome for Instagram scraping, exposed over CDP on 9223.
REM Log into instagram.com in THIS window once; the session persists in its own
REM user-data dir so meta_osint can connect without ever handling your password.

set CHROME="C:\Program Files\Google\Chrome\Application\chrome.exe"
if not exist %CHROME% set CHROME="C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"

start "" %CHROME% ^
  --remote-debugging-port=9223 ^
  --user-data-dir="%LOCALAPPDATA%\MetaOsint\chrome-instagram" ^
  --no-first-run ^
  --no-default-browser-check ^
  https://www.instagram.com/

echo Chrome (Instagram) started on CDP port 9223.
echo Log in to Instagram in the opened window, then run the scraper.
