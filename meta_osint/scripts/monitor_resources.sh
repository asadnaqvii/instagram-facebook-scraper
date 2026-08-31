#!/usr/bin/env bash
# ============================================================
#  Resource monitor — file descriptors, memory, tabs, processes
#
#  Distinguishes a LEAK (numbers climb steadily) from a HIGH
#  BASELINE (numbers stable but near the ulimit). Run it in a
#  second terminal while scraping.
#
#    watch mode :  bash monitor_resources.sh          # refresh every 5s
#    one shot   :  bash monitor_resources.sh once
#    log to file:  bash monitor_resources.sh log > usage.csv
#
#  Reads CDP ports from .env / env, same as everything else.
# ============================================================
cd "$(dirname "$0")/../.." 2>/dev/null || true
[ -f .env ] && { set -a; . ./.env 2>/dev/null || true; set +a; }

FB_PORT="${CDP_PORT_FACEBOOK:-9222}"
IG_PORT="${CDP_PORT_INSTAGRAM:-9223}"
INTERVAL="${INTERVAL:-5}"

fd_count() { ls "/proc/$1/fd" 2>/dev/null | wc -l; }
rss_mb()   { awk '/VmRSS/{printf "%.0f", $2/1024}' "/proc/$1/status" 2>/dev/null || echo 0; }

tabs_on_port() {  # count open CDP targets (tabs) — the real "how many tabs" answer
  curl -s --max-time 2 "http://127.0.0.1:$1/json/list" 2>/dev/null \
    | grep -c '"type": "page"' || echo 0
}

snapshot() {
  local limit chrome_n chrome_fd chrome_mem py_n py_fd py_mem fb_tabs ig_tabs
  limit="$(ulimit -n)"

  chrome_n=0; chrome_fd=0; chrome_mem=0
  for p in $(pgrep -f "chrome|chromium" 2>/dev/null); do
    chrome_n=$((chrome_n+1))
    chrome_fd=$((chrome_fd + $(fd_count "$p")))
    chrome_mem=$((chrome_mem + $(rss_mb "$p")))
  done

  py_n=0; py_fd=0; py_mem=0
  for p in $(pgrep -f "meta_osint" 2>/dev/null); do
    py_n=$((py_n+1))
    py_fd=$((py_fd + $(fd_count "$p")))
    py_mem=$((py_mem + $(rss_mb "$p")))
  done

  fb_tabs="$(tabs_on_port "$FB_PORT")"
  ig_tabs="$(tabs_on_port "$IG_PORT")"

  printf '%s,%s,%s,%s,%s,%s,%s,%s,%s,%s\n' \
    "$(date +%H:%M:%S)" "$limit" \
    "$chrome_n" "$chrome_fd" "$chrome_mem" \
    "$py_n" "$py_fd" "$py_mem" \
    "$fb_tabs" "$ig_tabs"
}

human() {
  IFS=, read -r t limit cn cfd cmem pn pfd pmem fbt igt <<< "$(snapshot)"
  echo "─────────────────────────────────────────────────────────────"
  echo " $t     ulimit -n: $limit"
  echo "─────────────────────────────────────────────────────────────"
  printf "  Chrome     %2s proc   %5s fds   %6s MB\n" "$cn" "$cfd" "$cmem"
  printf "  meta_osint %2s proc   %5s fds   %6s MB\n" "$pn" "$pfd" "$pmem"
  printf "  Tabs open  facebook(%s): %-3s  instagram(%s): %-3s\n" "$FB_PORT" "$fbt" "$IG_PORT" "$igt"
  echo ""
  # warnings
  if [ "$limit" != "unlimited" ] 2>/dev/null; then
    worst=0
    for p in $(pgrep -f "chrome|chromium" 2>/dev/null); do
      n=$(fd_count "$p"); [ "$n" -gt "$worst" ] && worst=$n
    done
    if [ "$worst" -gt $((limit * 70 / 100)) ] 2>/dev/null; then
      echo "  [!] a Chrome process is at $worst fds vs limit $limit — raise it:"
      echo "      ulimit -n 65535   (or FD_LIMIT=65535 bash run.sh)"
    fi
  fi
  if [ "$fbt" -gt 5 ] 2>/dev/null || [ "$igt" -gt 5 ] 2>/dev/null; then
    echo "  [!] more than 5 tabs open — the scraper uses 1 per platform."
    echo "      Climbing tab count = leak; check orchestrator retry path."
  fi
  if [ "$cn" -gt 20 ] 2>/dev/null; then
    echo "  [!] $cn chrome processes — Chrome forks per tab/renderer, but if"
    echo "      this keeps growing you have stacked browsers: pkill -f remote-debugging-port"
  fi
}

case "${1:-watch}" in
  once) human ;;
  log)
    echo "time,ulimit,chrome_proc,chrome_fds,chrome_mb,py_proc,py_fds,py_mb,fb_tabs,ig_tabs"
    while true; do snapshot; sleep "$INTERVAL"; done ;;
  watch|*)
    echo "Monitoring every ${INTERVAL}s — Ctrl-C to stop."
    echo "Watch for CLIMBING numbers (leak) vs stable-but-high (raise ulimit)."
    while true; do human; sleep "$INTERVAL"; done ;;
esac
