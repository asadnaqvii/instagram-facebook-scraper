#!/usr/bin/env bash
# Launch a dedicated Chrome for Instagram scraping, exposed over CDP on $CDP_PORT_INSTAGRAM (default 9223).
# Log into instagram.com once; the session persists in its own user-data dir.
set -e

CHROME="${CHROME:-google-chrome}"
command -v "$CHROME" >/dev/null 2>&1 || CHROME="chromium"

"$CHROME" \
  --remote-debugging-port="${CDP_PORT_INSTAGRAM:-9223}" \
  --user-data-dir="${HOME}/.meta-osint/chrome-instagram" \
  --no-first-run \
  --no-default-browser-check \
  https://www.instagram.com/ &

echo "Chrome (Instagram) started on CDP port ${CDP_PORT_INSTAGRAM:-9223}. Log in, then run the scraper."
