#!/usr/bin/env bash
# ============================================================
#  Strat-Watch Meta — single source of truth launcher
#
#  Everything is driven by .env. This script reads it, validates it,
#  reports what it resolved, brings up Chrome on the configured CDP
#  ports, checks the database + Ollama, and starts the dashboard.
#
#  .env keys it uses (all optional — sane defaults shown):
#    PORT=5000                     dashboard / API port
#    CDP_HOST=127.0.0.1            CDP host (IPv4; avoids ::1 refusals)
#    CDP_PORT_FACEBOOK=9222        Chrome CDP port for Facebook
#    CDP_PORT_INSTAGRAM=9223       Chrome CDP port for Instagram
#    META_OSINT_DB_BACKEND=sqlite  'sqlite' | 'mysql'
#    MYSQL_HOST/PORT/USER/PASSWORD/MYSQL_DB
#    OLLAMA_URL=http://localhost:11434
#    META_OSINT_CHROME_BASE=$HOME/.meta-osint   Chrome profile dir
#    SKIP_CHROME=1                 don't launch Chrome (API-only mode)
#    SKIP_INSTALL=1                skip pip/playwright install
#
#  Usage:   bash run.sh            (all config from .env)
#           PORT=5006 bash run.sh  (env overrides .env)
# ============================================================
cd "$(dirname "$0")"

# ── load .env (shell side; Python loads it too via python-dotenv) ──
# Real shell env wins over .env, so `PORT=5006 bash run.sh` overrides.
if [ -f .env ]; then
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in ''|\#*) continue ;; esac
    key="${line%%=*}"; val="${line#*=}"
    key="$(printf '%s' "$key" | tr -d ' ')"
    [ -z "$key" ] && continue
    # strip surrounding quotes if present
    val="${val%\"}"; val="${val#\"}"; val="${val%\'}"; val="${val#\'}"
    if [ -z "$(eval "printf '%s' \"\${$key:-}\"")" ]; then export "$key=$val"; fi
  done < .env
  ENV_STATUS="loaded .env"
else
  ENV_STATUS="no .env found (using defaults)"
fi

WEB_PORT="${PORT:-5000}"
CDP_HOST="${CDP_HOST:-127.0.0.1}"
FB_PORT="${CDP_PORT_FACEBOOK:-9222}"
IG_PORT="${CDP_PORT_INSTAGRAM:-9223}"
BACKEND="${META_OSINT_DB_BACKEND:-sqlite}"
OLLAMA="${OLLAMA_URL:-http://localhost:11434}"
PROFILE_BASE="${META_OSINT_CHROME_BASE:-$HOME/.meta-osint}"
PY="${PYTHON:-python3}"

hr() { echo " ------------------------------------------------------------"; }
echo
echo " ============================================================"
echo "   Strat-Watch Meta  -  Instagram & Facebook OSINT"
echo " ============================================================"

# ── 1. resolved configuration ──────────────────────────────────────
echo " [1/6] Configuration  ($ENV_STATUS)"
echo "       dashboard      : http://localhost:$WEB_PORT"
echo "       database       : $BACKEND"
if [ "$BACKEND" = "mysql" ]; then
  echo "                        ${MYSQL_USER:-?}@${MYSQL_HOST:-?}:${MYSQL_PORT:-3306}/${MYSQL_DB:-?}"
  MISSING=""
  for v in MYSQL_HOST MYSQL_USER MYSQL_PASSWORD MYSQL_DB; do
    [ -z "$(eval "printf '%s' \"\${$v:-}\"")" ] && MISSING="$MISSING $v"
  done
  [ -n "$MISSING" ] && echo "       [!] MySQL selected but missing:$MISSING"
fi
echo "       chrome (fb/ig) : $CDP_HOST:$FB_PORT / $CDP_HOST:$IG_PORT"
echo "       ollama         : $OLLAMA"
hr

# ── 2. python + deps ───────────────────────────────────────────────
if ! command -v "$PY" >/dev/null 2>&1; then
  echo " [X] $PY not found. Install Python 3.10+ and re-run."; exit 1
fi
echo " [2/6] Python: $($PY --version 2>&1)"
if [ "${SKIP_INSTALL:-0}" != "1" ]; then
  echo "       installing requirements (quiet)..."
  "$PY" -m pip install --quiet --disable-pip-version-check -r requirements.txt \
    || echo "       [!] pip reported problems — continuing"
  "$PY" -m playwright install chromium >/dev/null 2>&1 || true
else
  echo "       SKIP_INSTALL=1 — skipping pip/playwright"
fi
hr

# ── 3. database reachability ───────────────────────────────────────
echo " [3/6] Database check"
"$PY" - <<'PYCHK'
import sys
try:
    import meta_osint.config as c
    from meta_osint.database.db import PostDatabase
    print(f"       backend resolved by app: {c.DB_BACKEND}")
    if c.DB_BACKEND == "mysql":
        if not c.MYSQL_PASSWORD:
            print("       [!] MYSQL_PASSWORD empty — is python-dotenv installed?")
    with PostDatabase() as db:
        s = db.get_stats()
    print(f"       OK — posts={s['posts']} accounts={s['accounts']} comments={s['comments']}")
except Exception as e:
    print(f"       [X] DB ERROR: {type(e).__name__}: {e}")
    sys.exit(0)   # non-fatal: dashboard can still start and show the error
PYCHK
hr

# ── 4. ollama (optional) ───────────────────────────────────────────
echo " [4/6] Ollama"
if curl -s --max-time 3 "$OLLAMA/api/tags" >/dev/null 2>&1; then
  echo "       OK — reachable at $OLLAMA"
else
  echo "       [!] not reachable at $OLLAMA (AI enrichment disabled; scraping still works)"
fi
hr

# ── 5. chrome on the configured CDP ports ──────────────────────────
echo " [5/6] Chrome (CDP)"
if [ "${SKIP_CHROME:-0}" = "1" ]; then
  echo "       SKIP_CHROME=1 — not launching Chrome (API-only mode)"
else
  CHROME="${CHROME:-google-chrome}"
  command -v "$CHROME" >/dev/null 2>&1 || CHROME="chromium"
  command -v "$CHROME" >/dev/null 2>&1 || CHROME=""
  [ -z "${DISPLAY:-}" ] && echo "       [!] \$DISPLAY unset — if Chrome fails, start Xvfb (see LINUX_SCRAPING.md)"

  launch_chrome() {  # port profile url label
    if curl -s --max-time 2 "http://$CDP_HOST:$1/json/version" >/dev/null 2>&1; then
      echo "       $4: already up on $1 — reusing"; return
    fi
    if [ -z "$CHROME" ]; then
      echo "       [!] $4: no chrome/chromium found — start it manually on $1"; return
    fi
    mkdir -p "$2"
    nohup "$CHROME" --remote-debugging-port="$1" --user-data-dir="$2" \
      --no-first-run --no-default-browser-check \
      --disable-gpu --disable-dev-shm-usage --log-level=3 \
      --disable-extensions --disable-background-networking \
      --disable-background-timer-throttling --disable-renderer-backgrounding \
      --disable-backgrounding-occluded-windows --disable-sync \
      --disable-component-update --mute-audio \
      --js-flags="--max-old-space-size=${CHROME_HEAP_MB:-512}" \
      "$3" > "$HOME/chrome-$4.log" 2>&1 &
    echo "       $4: launching on $1 (log ~/chrome-$4.log)"
  }
  launch_chrome "$FB_PORT" "$PROFILE_BASE/chrome-facebook"  "https://www.facebook.com/"  "fb"
  launch_chrome "$IG_PORT" "$PROFILE_BASE/chrome-instagram" "https://www.instagram.com/" "ig"

  for pair in "fb:$FB_PORT" "ig:$IG_PORT"; do
    lbl="${pair%%:*}"; prt="${pair##*:}"; ok=""
    for _ in $(seq 1 15); do
      curl -s --max-time 2 "http://$CDP_HOST:$prt/json/version" >/dev/null 2>&1 && { ok=1; break; }
      sleep 1
    done
    if [ -n "$ok" ]; then echo "       $lbl ($prt): READY"
    else echo "       $lbl ($prt): NOT REACHABLE — scraping will skip this platform"; fi
  done
  echo
  echo "       >>> Log in to Facebook / Instagram in those windows (once)."
fi
hr

# ── 6. dashboard ───────────────────────────────────────────────────
echo " [6/6] Starting dashboard + API"
echo
echo " ============================================================"
echo "   Dashboard : http://localhost:$WEB_PORT"
echo "   API       : http://localhost:$WEB_PORT/api/v1"
echo " ============================================================"
echo
exec "$PY" -m meta_osint.main serve --port "$WEB_PORT"
