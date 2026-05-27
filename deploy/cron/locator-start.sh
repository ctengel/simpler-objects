#!/usr/bin/env bash
# Wrapper for simpler-objects locator; designed for @reboot cron.
# Copy to ~/bin/simpler-objects-locator-start, chmod +x, then edit
# VENV below if you installed the package somewhere other than ~/venv.

# ---- operator-configurable variables ----------------------------------------
VENV="${HOME}/venv"
LOG_FILE="${HOME}/logs/simpler-objects-locator.log"
PID_FILE="${HOME}/.run/simpler-objects-locator.pid"
# -----------------------------------------------------------------------------

ENV_FILE="${HOME}/.config/simpler-objects/locator.env"

mkdir -p "$(dirname "$LOG_FILE")" "$(dirname "$PID_FILE")"
exec >> "$LOG_FILE" 2>&1

log() { printf '[%s] %s\n' "$(date -Iseconds)" "$*"; }

log "locator-start: starting up"

[ -f "$ENV_FILE" ] || { log "FATAL: env file not found: $ENV_FILE"; exit 1; }
set -a
# shellcheck source=/dev/null
. "$ENV_FILE"
set +a

HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-29164}"
WORKERS="${WORKERS:-1}"

# Restart loop — mirrors Restart=on-failure, RestartSec=5 in the systemd unit.
while true; do
    log "Starting uvicorn on ${HOST}:${PORT} (workers=${WORKERS})"
    "$VENV/bin/uvicorn" simpler_objects.locator_api:app \
        --host "$HOST" --port "$PORT" --workers "$WORKERS" &
    echo $! > "$PID_FILE"
    wait $!
    rc=$?
    rm -f "$PID_FILE"
    if [ "$rc" -eq 0 ]; then
        log "uvicorn exited cleanly (rc=0); not restarting."
        exit 0
    fi
    log "uvicorn exited with rc=$rc; restarting in 5s"
    sleep 5
done
