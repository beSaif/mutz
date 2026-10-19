#!/bin/sh
# Restart the filming instance of the app on an empty app DB (the gateway cache and the index are shared).
# Usage: capture/fresh.sh <db dir>     → app at http://localhost:8100, no basic auth
set -e
DB_DIR=${1:?db dir}
APP=$(cd "$(dirname "$0")/../.." && pwd)
PID=$(lsof -tiTCP:8100 -sTCP:LISTEN || true)
[ -n "$PID" ] && kill $PID && sleep 1
rm -f "$DB_DIR"/app.sqlite*
cd "$APP"
MUTZ_APP_DB="$DB_DIR/app.sqlite" MUTZ_BASIC_AUTH= nohup .venv/bin/uvicorn app.main:app --port 8100 > "$DB_DIR/server.log" 2>&1 &
until grep -q "warmup done" "$DB_DIR/server.log" 2>/dev/null; do sleep 1; done
echo "fresh app on :8100"
