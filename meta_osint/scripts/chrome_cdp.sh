#!/bin/bash
# Launch the CDP Chrome windows meta_osint attaches to, on Linux.
#
# meta_osint attaches (never launches) to a real, human-logged-in Chrome over
# the DevTools Protocol — that's what keeps the session authenticated and under
# Meta's bot radar. This script starts one dedicated Chrome per platform with a
# persistent profile, then you log in ONCE per window.
#
#   Facebook  -> CDP port 9222   (profile: chrome-facebook)
#   Instagram -> CDP port 9223   (profile: chrome-instagram)
#
# These ports match meta_osint/config.py (CDP_PORT_FACEBOOK / CDP_PORT_INSTAGRAM).
#
# On a HEADLESS server (SSH/PuTTY, no monitor) you must run these under a virtual
# display so Chrome renders — headless mode gets bot-detected. Before `start`:
#     export DISPLAY=:99
#     Xvfb :99 -screen 0 1920x1080x24 &
#     fluxbox &                      # a minimal window manager
# then view :99 over VNC (x11vnc) once to log in. See LINUX_SCRAPING.md.
#
# Usage:  ./chrome_cdp.sh {start|stop|restart|status}

set -uo pipefail

# platform:port:profile
TARGETS=(
  "facebook:9222:chrome-facebook"
  "instagram:9223:chrome-instagram"
)

CHROME_BIN="${CHROME:-google-chrome}"
command -v "$CHROME_BIN" >/dev/null 2>&1 || CHROME_BIN="chromium"
command -v "$CHROME_BIN" >/dev/null 2>&1 || { echo "ERROR: no google-chrome or chromium on PATH"; exit 1; }

PROFILE_BASE="${META_OSINT_CHROME_BASE:-$HOME/.meta-osint}"

url_for() { [ "$1" = "instagram" ] && echo "https://www.instagram.com/" || echo "https://www.facebook.com/"; }

port_in_use() { lsof -Pi :"$1" -sTCP:LISTEN -t >/dev/null 2>&1; }

wait_for_cdp() {
  local port=$1
  for _ in $(seq 1 15); do
    curl -s "http://localhost:$port/json/version" >/dev/null 2>&1 && return 0
    sleep 1
  done
  return 1
}

launch_one() {
  local platform=$1 port=$2 profile=$3
  local dir="$PROFILE_BASE/$profile"
  mkdir -p "$dir"
  echo "Launching Chrome for $platform on CDP $port (profile: $profile)..."
  "$CHROME_BIN" \
    --remote-debugging-port="$port" \
    --user-data-dir="$dir" \
    --no-first-run \
    --no-default-browser-check \
    --disable-features=Translate \
    "$(url_for "$platform")" \
    >/dev/null 2>&1 &
  echo "  PID $!"
  sleep 2
}

ACTION="${1:-start}"
case "$ACTION" in
  start)
    if [ -z "${DISPLAY:-}" ]; then
      echo "WARNING: \$DISPLAY is unset. On a headless server, start Xvfb first"
      echo "         (see the header of this script) or Chrome won't render."
      echo ""
    fi
    started=0
    for t in "${TARGETS[@]}"; do
      IFS=":" read -r platform port profile <<< "$t"
      if port_in_use "$port"; then
        echo "  ✓ $platform already running on $port"
      else
        launch_one "$platform" "$port" "$profile"
        started=$((started+1))
      fi
    done
    echo ""
    echo "Waiting for CDP endpoints..."
    for t in "${TARGETS[@]}"; do
      IFS=":" read -r platform port profile <<< "$t"
      echo -n "  $platform ($port)... "
      if wait_for_cdp "$port"; then echo "ready"; else echo "NOT ready"; fi
    done
    echo ""
    echo "Next: log into each window ONCE (over VNC if headless), then run:"
    echo "  python -m meta_osint.main diagnose        # verify CDP is reachable"
    echo "  python -m meta_osint.main search -k \"nuclear\" -p instagram -n 8"
    ;;

  stop)
    for t in "${TARGETS[@]}"; do
      IFS=":" read -r platform port profile <<< "$t"
      pids=$(lsof -ti :"$port" 2>/dev/null)
      if [ -n "$pids" ]; then
        echo "Stopping $platform (port $port)..."
        kill $pids 2>/dev/null
      fi
    done
    sleep 2
    echo "Stopped."
    ;;

  restart)
    "$0" stop
    sleep 3
    "$0" start
    ;;

  status)
    for t in "${TARGETS[@]}"; do
      IFS=":" read -r platform port profile <<< "$t"
      if port_in_use "$port"; then
        if curl -s "http://localhost:$port/json/version" >/dev/null 2>&1; then
          ver=$(curl -s "http://localhost:$port/json/version" | grep -o '"Browser":"[^"]*"' | cut -d'"' -f4)
          echo "  ✓ $platform ($port): running - $ver"
        else
          echo "  ⚠ $platform ($port): port in use but CDP not responding"
        fi
      else
        echo "  ✗ $platform ($port): not running"
      fi
    done
    ;;

  *)
    echo "Usage: $0 {start|stop|restart|status}"
    exit 1
    ;;
esac
