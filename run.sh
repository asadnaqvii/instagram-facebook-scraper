#!/usr/bin/env bash
# ============================================================
#  Strat-Watch Meta — one-click launcher (macOS / Linux)
#  Installs deps, opens the two CDP Chrome windows, and starts
#  the dashboard. Self-contained: launches Chrome itself, so the
#  ports always match what the scraper reads from .env.
#
#  Config (from .env or the shell):
#    PORT                 dashboard port      (default 5000)
#    CDP_PORT_FACEBOOK    Chrome CDP for FB   (default 9222)
#    CDP_PORT_INSTAGRAM   Chrome CDP for IG   (default 9223)
#
#  Usage:  PORT=5006 bash run.sh
# ============================================================
cd "$(dirname "$0")"

# Load .env so CDP_PORT_* / PORT are picked up here too (the Python side
# loads it separately via python-dotenv).
if [ -f .env ]; then
  set -a; . ./.env 2>/dev/null || true; set +a
fi

WEB_PORT="${PORT:-5000}"
FB_PORT="${CDP_PORT_FACEBOOK:-9222}"
IG_PORT="${CDP_PORT_INSTAGRAM:-9223}"
PROFILE_BASE="${META_OSINT_CHROME_BASE:-$HOME/.meta-osint}"

echo
echo " ============================================================"
echo "   Strat-Watch Meta  -  Instagram & Facebook OSINT"
echo " ============================================================"
echo

# 1. Python
if ! command -v python3 >/dev/null 2>&1; then
  echo " [X] Python 3 not found. Install Python 3.10+ and re-run."; exit 1
fi
echo " [1/5] Python found."

# 2. Dependencies
echo " [2/5] Installing Python packages (first run only)..."
python3 -m pip install --quiet --disable-pip-version-check -r requirements.txt || \
  echo "      (pip install had warnings — continuing)"

# 3. Playwright browser
echo " [3/5] Ensuring Playwright browser is installed..."
python3 -m playwright install chromium >/dev/null 2>&1 || true

# 4. Chrome — launched here so the ports ALWAYS match the scraper's config.
CHROME="${CHROME:-google-chrome}"
command -v "$CHROME" >/dev/null 2>&1 || CHROME="chromium"
command -v "$CHROME" >/dev/null 2>&1 || CHROME=""

launch_chrome() {  # $1=port  $2=profile  $3=url  $4=label
  if curl -s --max-time 2 "http://127.0.0.1:$1/json/version" >/dev/null 2>&1; then
    echo "       $4: already running on $1 — reusing it."
    return
  fi
  if [ -z "$CHROME" ]; then
    echo "       [!] $4: no google-chrome/chromium found — start it manually on port $1."
    return
  fi
  mkdir -p "$2"
  nohup "$CHROME" \
    --remote-debugging-port="$1" \
    --user-data-dir="$2" \
    --no-first-run --no-default-browser-check \
    --disable-gpu --disable-dev-shm-usage --log-level=3 \
    "$3" > "$HOME/chrome-$4.log" 2>&1 &
  echo "       $4: starting on port $1 (log: ~/chrome-$4.log)"
}

echo " [4/5] Opening Chrome — Facebook ($FB_PORT), Instagram ($IG_PORT)..."
if [ -z "${DISPLAY:-}" ]; then
  echo "       [!] \$DISPLAY is unset. If Chrome fails to start, this is why —"
  echo "           on a headless server run:  export DISPLAY=:99"
  echo "                                      Xvfb :99 -screen 0 1920x1080x24 & fluxbox &"
fi
launch_chrome "$FB_PORT" "$PROFILE_BASE/chrome-facebook"  "https://www.facebook.com/"  "fb"
launch_chrome "$IG_PORT" "$PROFILE_BASE/chrome-instagram" "https://www.instagram.com/" "ig"

echo "       Waiting for Chrome to expose CDP..."
for p in "$FB_PORT" "$IG_PORT"; do
  ok=""
  for _ in $(seq 1 15); do
    if curl -s --max-time 2 "http://127.0.0.1:$p/json/version" >/dev/null 2>&1; then ok=1; break; fi
    sleep 1
  done
  if [ -n "$ok" ]; then echo "       port $p: READY"
  else echo "       port $p: NOT reachable — check ~/chrome-*.log (scraping will be skipped)"; fi
done
echo
echo "       >>> LOG IN to Facebook and Instagram in those windows (once). Complete any 2FA."
echo

# 5. Dashboard
echo " [5/5] Starting the dashboard on port $WEB_PORT ..."
echo
echo " ============================================================"
echo "   Dashboard:  http://localhost:$WEB_PORT"
echo " ============================================================"
echo
exec python3 -m meta_osint.main serve --port "$WEB_PORT"
