#!/usr/bin/env bash
# Launch a dedicated Chrome for Facebook scraping, exposed over CDP on 9222.
# Log into facebook.com once; the session persists in its own user-data dir.
set -e

CHROME="${CHROME:-google-chrome}"
command -v "$CHROME" >/dev/null 2>&1 || CHROME="chromium"

"$CHROME" \
  --remote-debugging-port=9222 \
  --user-data-dir="${HOME}/.meta-osint/chrome-facebook" \
  --no-first-run \
  --no-default-browser-check \
  https://www.facebook.com/ &

echo "Chrome (Facebook) started on CDP port 9222. Log in, then run the scraper."
