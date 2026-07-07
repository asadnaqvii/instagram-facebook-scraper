#!/usr/bin/env bash
# ============================================================
#  Strat-Watch Meta — one-click launcher (macOS / Linux)
#  Installs deps, opens two Chrome windows to log into, and
#  starts the dashboard. Ollama is NOT required.
# ============================================================
set -e
cd "$(dirname "$0")"

echo
echo " ============================================================"
echo "   Strat-Watch Meta  -  Instagram & Facebook OSINT"
echo " ============================================================"
echo

# 1. Python
if ! command -v python3 >/dev/null 2>&1; then
  echo " [X] Python 3 not found. Install Python 3.10+ and re-run."
  exit 1
fi
echo " [1/5] Python found."

# 2. Dependencies
echo " [2/5] Installing Python packages (first run only)..."
python3 -m pip install --quiet --disable-pip-version-check -r meta_osint/requirements.txt

# 3. Playwright browser
echo " [3/5] Ensuring Playwright browser is installed..."
python3 -m playwright install chromium >/dev/null 2>&1 || true

# 4. Launch Chrome per platform
echo " [4/5] Opening Chrome windows for Facebook (9222) and Instagram (9223)..."
echo
echo "       >>> LOG IN to Facebook and Instagram in the two windows that open."
echo "       >>> Leave both windows OPEN. Complete any 2FA."
echo
bash meta_osint/scripts/start_chrome_facebook.sh || true
bash meta_osint/scripts/start_chrome_instagram.sh || true

echo "       Waiting 20 seconds for you to start logging in..."
sleep 20

# 5. Dashboard
echo " [5/5] Starting the dashboard..."
echo
echo " ============================================================"
echo "   Dashboard:  http://localhost:5000"
echo " ============================================================"
echo
( sleep 2; (command -v open >/dev/null && open http://localhost:5000) || (command -v xdg-open >/dev/null && xdg-open http://localhost:5000) ) >/dev/null 2>&1 &
python3 -m meta_osint.main serve --port 5000
