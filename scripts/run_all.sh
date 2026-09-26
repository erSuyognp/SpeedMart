#!/usr/bin/env bash
# Starts backend (uvicorn, 0.0.0.0:8000) + vision worker with the project venv, plus the ngrok HTTPS
# tunnel when config.json features.https_tunnel is true (domain = PUBLIC_ORIGIN in .env).
# Output is prefixed [api] / [vision] / [tunnel]. Ctrl+C stops all of them.
# Extra arguments go to the worker, e.g.  scripts/run_all.sh --no-window
# If the worker exits (q in its window) the backend keeps running; Ctrl+C to stop it.
# Works with macOS bash 3.2.

set -u
cd "$(dirname "$0")/.." || exit 1

if [ -x .venv/bin/python ]; then
  PY=.venv/bin/python
elif [ -x .venv/Scripts/python.exe ]; then
  PY=.venv/Scripts/python.exe
else
  echo "No venv at .venv. Create it: python3.11 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
  exit 1
fi
export PYTHONUNBUFFERED=1

prefix() { awk -v p="[$1] " '{ print p $0; fflush() }'; }

API_PID=""
VISION_PID=""
TUNNEL_PID=""

cleanup() {
  rc=$?
  trap - INT TERM EXIT
  echo "[run_all] stopping..."
  for pid in $TUNNEL_PID $VISION_PID $API_PID; do
    kill -TERM "$pid" 2>/dev/null
  done
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    alive=""
    for pid in $TUNNEL_PID $VISION_PID $API_PID; do
      kill -0 "$pid" 2>/dev/null && alive=1
    done
    [ -z "$alive" ] && break
    sleep 0.5
  done
  for pid in $TUNNEL_PID $VISION_PID $API_PID; do
    kill -KILL "$pid" 2>/dev/null
  done
  wait 2>/dev/null
  echo "[run_all] stopped"
  exit "$rc"
}
trap cleanup INT TERM EXIT

# --proxy-headers: behind the tunnel, FastAPI must see https (X-Forwarded-Proto) for secure cookies + WebAuthn.
"$PY" -m uvicorn backend.main:app --host 0.0.0.0 --port 8000 --no-access-log \
  --proxy-headers --forwarded-allow-ips "*" > >(prefix api) 2>&1 &
API_PID=$!

# give the backend a moment so the first snapshots do not all fail
for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
  "$PY" -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=0.5)" \
    >/dev/null 2>&1 && break
  kill -0 "$API_PID" 2>/dev/null || { echo "[run_all] backend failed to start" >&2; exit 1; }
  sleep 0.5
done

# --- HTTPS tunnel (F14, S3.1) ---
# ngrok static domain from PUBLIC_ORIGIN; the domain must never change or every passkey breaks.
TUNNEL_ON=$("$PY" -c "import json; print(int(bool(json.load(open('config.json'))['features'].get('https_tunnel'))))" 2>/dev/null)
if [ "$TUNNEL_ON" = "1" ]; then
  TUNNEL_DOMAIN=$("$PY" -c "
from dotenv import dotenv_values
o = (dotenv_values('.env').get('PUBLIC_ORIGIN') or '').strip().rstrip('/')
print(o.split('://', 1)[-1] if o.startswith('https://') else '')" 2>/dev/null)
  if [ -z "$TUNNEL_DOMAIN" ]; then
    echo "[run_all] https_tunnel is on but PUBLIC_ORIGIN in .env is not an https:// URL; tunnel not started" >&2
  elif ! command -v ngrok >/dev/null 2>&1; then
    echo "[run_all] https_tunnel is on but ngrok is not on PATH; tunnel not started" >&2
  else
    # ngrok >= 3.16 takes --url; older v3 releases only know --domain.
    if ngrok http --help 2>/dev/null | grep -q -- "--url"; then URL_FLAG="--url"; else URL_FLAG="--domain"; fi
    ngrok http "$URL_FLAG=$TUNNEL_DOMAIN" 8000 --log stdout --log-format logfmt > >(prefix tunnel) 2>&1 &
    TUNNEL_PID=$!
    echo "[run_all] tunnel pid $TUNNEL_PID: https://$TUNNEL_DOMAIN -> localhost:8000"
  fi
fi

"$PY" -m vision.worker "$@" > >(prefix vision) 2>&1 &
VISION_PID=$!

echo "[run_all] backend pid $API_PID, vision pid $VISION_PID. Ctrl+C stops both."

vision_reported=""
while kill -0 "$API_PID" 2>/dev/null; do
  if [ -z "$vision_reported" ] && ! kill -0 "$VISION_PID" 2>/dev/null; then
    echo "[run_all] vision worker exited; backend still running. Restart it with: $PY -m vision.worker"
    vision_reported=1
  fi
  sleep 1
done
echo "[run_all] backend exited" >&2
